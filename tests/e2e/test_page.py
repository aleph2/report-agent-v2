import json
from http.client import HTTPConnection
from urllib.parse import urlsplit
import os
import subprocess
import sys
import time
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest
from docx import Document

from reportagent.export.docx import _old_answers, _settings, _xlsx

FAKE = """
from tests.fake_model import ScriptedModel, call
from reportagent import cli
from langchain_core.messages import AIMessage
def model(phase):
    files = {
        'plan': ('plan.md', '## Reservoir'),
        'gather': ('notes/Reservoir.md', 'Gate colour unknown'),
        'write': ('sections/Reservoir.md', '# Reservoir\\n\\nGate colour pending.'),
        'verify-sources': ('reviews/Reservoir-sources.md', 'Source review'),
        'review': ('reviews/Reservoir.md', 'Question for the operator'),
        'clarify': ('clarifications/Reservoir.md', 'number: 1\\nanchor: Gate colour pending.\\ncontent: Gate colour?\\nquestion: Gate colour?\\naudience: client'),
    }
    if phase == 'revise':
        op = call('edit_file', file_path='/work/sections/Reservoir.md', old_string='Gate colour pending.',
                  new_string='Gate colour is amber.')
    else:
        path, contents = files[phase]
        op = call('write_file', file_path='/work/'+path, content=contents)
    return ScriptedModel(responses=[m.model_copy(update={'usage_metadata':dict(input_tokens=5,output_tokens=2,total_tokens=7)})
                                    for m in (op, AIMessage(content='Saved.'))])
cli.make_model = model
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    cases = tmp_path / "cases"
    # Install the fake in child interpreters before the module entry point runs.
    (tmp_path / "sitecustomize.py").write_text(
        "import os,sys\nif 'stage1' in sys.argv or 'stage2' in sys.argv:\n"
        "    sys.addaudithook(lambda e,a: os._exit(92) if e=='socket.connect' else None)\n"
        "    try:\n" + "\n".join("        " + line for line in FAKE.splitlines()) +
        "\n    except BaseException:\n        os._exit(91)\n")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(tmp_path), str(Path.cwd()), os.getenv("PYTHONPATH", "")]))
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    cmd = [sys.executable, "-u", "-m", "reportagent", "serve", "--cases", str(cases), "--port", "0"]
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as server:
        try:
            url = server.stdout.readline().strip()
            assert url.startswith("http://127.0.0.1:"), url
            yield url, cases, server, cmd
        finally:
            server.terminate()
            server.wait(timeout=10)


def get(url, path):
    with urlopen(url + path, timeout=10) as response:
        return response.read()


def post(url, path, fields):
    request = Request(url + path, urlencode(fields).encode(), headers={"X-Report-Agent": "1"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def upload(url, path, fields, data):
    request = Request(url + path + "?" + urlencode(fields), data,
                      headers={"Content-Type": "application/octet-stream", "X-Report-Agent": "1"})
    with urlopen(request, timeout=10) as response:
        return json.load(response)


def test_local_page(workspace, tmp_path):
    url, cases, _, cmd = workspace
    name = "Reservoir survey 水库"
    case = cases / name
    key = [("case", name)]
    case_url = "/api/case?" + urlencode(key)
    def wait_idle():
        until = time.monotonic() + 30
        while time.monotonic() < until:
            detail = json.loads(get(url, case_url))
            if not detail["running"]:
                return detail
            time.sleep(.1)
        pytest.fail("Stage still running")

    page = get(url, "/").decode()
    assert 'lang="zh-CN"' in page and "上传客户答复" in page
    (cases / ".private").mkdir()
    assert json.loads(get(url, "/api/cases")) == []
    with pytest.raises(HTTPError, match="404"):
        get(url, "/api/case?case=.private")
    with pytest.raises(HTTPError, match="400"):
        post(url, "/create", {"case": ".hidden"})
    other = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert other.returncode and "using this cases folder" in other.stderr
    inputs = [("commission", ("brief.md", b"Survey the gate.")),
              ("sources", ("field.txt", b"Gate log")), ("sources", ("现场记录.txt", b"Notes"))]
    post(url, "/create", key)
    assert json.loads(get(url, case_url))["step"] == 1
    plan = [(group, filename) for group, (filename, _) in inputs]
    post(url, "/prepare", key + [("files", json.dumps(plan))])
    for group, (filename, data) in inputs:
        upload(url, "/upload", key + [("group", group), ("file", filename)], data)
    with pytest.raises(HTTPError) as duplicate:
        upload(url, "/upload", key + [("group", "sources"), ("file", "field.txt")], b"replacement")
    assert duplicate.value.code == 409 and json.load(duplicate.value) == "sources/ 中已有同名文件。"
    post(url, "/finish", key)
    assert (case / "sources/field.txt").read_bytes() == b"Gate log"
    assert (case / "sources/现场记录.txt").read_bytes() == b"Notes"
    assert json.loads(get(url, case_url))["step"] == 2
    template = case / "commission/template"
    template.mkdir()
    (template / "settings.txt").write_text("xlsx_file: Survey 答复.xlsx\n")
    with pytest.raises(HTTPError, match="409"):
        post(url, "/create", key)
    with pytest.raises(HTTPError, match="400"):
        upload(url, "/upload", key + [("group", "sources"), ("file", "../file")], b"x")
    assert not (cases / "Invalid").exists()
    opts = key + [("stage","stage1"), ("max_tokens","300000"), ("phase_caps","clarify=60000")]
    assert "--library" not in post(url, "/preview", opts)["command"]
    library = cases.parent / "lessons"
    library.mkdir()
    (library / "lessons.md").write_text("Synthetic library sentinel")
    preview = post(url, "/preview", opts)
    assert "--library" in preview["command"] and str(library) in preview["command"]
    assert "--phase-cap clarify=60000" in preview["command"]
    assert not (case / "work/runs").exists()
    post(url, "/run", opts)
    with pytest.raises(HTTPError, match="409"):
        post(url, "/run", opts)
    detail = wait_idle()
    assert detail["last"][2] == "export" and int(detail["last"][4]) > 0
    for phase in ("plan", "gather", "write", "verify-sources", "review", "clarify"):
        log, = (case / "work/runs").glob(f"*-{phase}.jsonl")
        prompt = str(json.loads(log.read_text().splitlines()[0])["content"])
        assert ("Synthetic library sentinel" in prompt) == (phase in ("plan", "gather", "write", "clarify"))
        assert ("confirm applicable DEFAULT" in prompt) == (phase == "clarify")
    assert detail["step"] == 3
    cfg = _settings(case)
    def download(filename):
        return get(url, "/download?" + urlencode(key + [("group","out"), ("file",filename)]))
    sheet = download(cfg["xlsx_file"])
    assert sheet == (case / "out" / cfg["xlsx_file"]).read_bytes()
    assert json.loads(get(url, case_url))["step"] == 4
    sent = tmp_path / "answered.xlsx"
    _xlsx(sent, cfg["sheet_columns"].split("|"), [[1,"Reservoir","","Colour?","","Open","amber","Operator"]], cfg)
    upload(url, "/answers", key + [("file", sent.name)], sent.read_bytes())
    assert json.loads(get(url, case_url))["step"] == 5
    dst = case / "out" / cfg["xlsx_file"]
    assert next(dst.parent.glob(dst.stem + ".before-answers-*.xlsx")).read_bytes() == sheet
    assert dst.read_bytes() == sent.read_bytes()
    assert _old_answers(dst, cfg)[1][1] == "amber"
    post(url, "/run", key + [("stage","stage2"),("max_tokens","300000"),("phase_caps","revise=220000")])
    detail = wait_idle()
    assert detail["last"][2] == "export"
    assert detail["case_tokens"] == 98 and int(detail["last"][4]) == 14
    assert detail["step"] == 6
    final = Document(BytesIO(download(cfg["docx_file"])))
    assert "Gate colour is amber." in [p.text for p in final.paragraphs]
    transcript, = (case / "work/runs").glob("*-revise.jsonl")
    revision_prompt = json.loads(transcript.read_text().splitlines()[0])["content"]
    assert "1: amber (Operator)" in revision_prompt and revision_prompt.count("Synthetic library sentinel") == 1
    assert "confirm applicable DEFAULT" not in revision_prompt
    assert len(list((case / "work/runs").glob("stage*-*.log"))) == 2
    upload(url, "/answers", key + [("file", sent.name)], sent.read_bytes())
    assert json.loads(get(url, case_url))["step"] == 5
    post(url, "/run", opts)
    post(url, "/stop", key)
    assert not wait_idle()["running"]


def test_large_upload_allows_status_and_retry(workspace):
    url, cases, server, _ = workspace
    key = {"case": "Large transfer"}
    path = "/upload?" + urlencode({**key, "group": "sources", "file": "large.pdf"})
    post(url, "/create", key)
    post(url, "/prepare", {**key, "files": json.dumps([["sources", "first.txt"], ["sources", "large.pdf"]])})
    upload(url, "/upload", {**key, "group": "sources", "file": "first.txt"}, b"retain")
    case = cases / key["case"]
    status = "/api/case?" + urlencode(key)
    size = 200 * 1024 * 1024
    chunk = b"x" * (1024 * 1024)

    def begin():
        conn = HTTPConnection(urlsplit(url).netloc, timeout=15)
        conn.putrequest("POST", path)
        for k, v in {"Content-Length": str(size), "Content-Type": "application/octet-stream",
                     "X-Report-Agent": "1"}.items():
            conn.putheader(k, v)
        conn.endheaders()
        conn.send(chunk)
        return conn

    def wait_for_received_bytes():
        deadline = time.monotonic() + 5
        while not any(p.stat().st_size for p in case.glob(".transfer-*/body")):
            assert time.monotonic() < deadline
            time.sleep(.01)

    first = begin()
    wait_for_received_bytes()
    first.close()
    deadline = time.monotonic() + 5
    while any(case.glob(".transfer-*")):
        assert time.monotonic() < deadline
        time.sleep(.01)
    with pytest.raises(HTTPError, match="409"):
        post(url, "/finish", key)
    with pytest.raises(HTTPError, match="409"):
        post(url, "/run", {**key, "stage":"stage1", "max_tokens":"0"})
    assert (case / "sources/first.txt").read_bytes() == b"retain"
    assert not (case / "sources/large.pdf").exists()

    proc = Path(f"/proc/{server.pid}/status")
    def rss():
        return {line.split(":")[0]: int(line.split()[1]) for line in proc.read_text().splitlines()
                if line.startswith(("VmRSS:", "VmHWM:"))} if proc.is_file() else {}

    before = rss()
    conn = begin()
    try:
        # Wait until the server has received bytes, then deliberately withhold the remainder.
        wait_for_received_bytes()
        started = time.monotonic()
        detail = json.loads(get(url, status))
        elapsed = time.monotonic() - started
        assert not detail["ready"] and detail["missing"] == [["sources","large.pdf"]]
        for _ in range(1, size // len(chunk)):
            conn.send(chunk)
        response = conn.getresponse()
        assert response.status == 201
        response.read()
    finally:
        conn.close()
    after = rss()
    print(f"200 MiB upload: server before {before}, after {after} (KiB); status latency {elapsed:.4f} seconds")
    assert (case / "sources/large.pdf").stat().st_size == size
    post(url, "/finish", key)
    assert json.loads(get(url, status))["ready"]
    (case / "sources/large.pdf").unlink()


def test_case_total_uses_recorded_responses(workspace):
    from reportagent import learn

    url, cases, *_ = workspace
    case = cases / "synthetic"
    logs = case / "work/runs"
    logs.mkdir(parents=True)
    (logs / "progress.txt").write_text("t\ts\tstage1\t999\t777\tdone\t-\n")
    def log(name, usages):
        path = logs / (name + ".jsonl")
        path.write_text("".join(json.dumps(dict(type="ai", content="Reply", usage_metadata={"total_tokens": use}))
                                + "\n" for use in usages))
        return path
    old = log("old-review", [7, None])
    log("legacy-write", [3])
    learn._learn_records(case)
    def total():
        return json.loads(get(url, "/api/case?case=synthetic"))["case_tokens"]
    assert total() == 10
    # An incremental receipt can coexist with a historical signal for its first phase.
    with learn.observe_run(case, None, "run", 50000, None) as receipt:
        learn.begin_phase(receipt, "review", old, 50000, {})
        learn.begin_phase(receipt, "write", log("new-write", [11, 5]), 50000, {})
    with learn.observe_run(case, None, "learn propose", 50000, None) as receipt:
        learn.begin_phase(receipt, "learn", log("new-learn", [13]), 50000, {})
    signals = case / "work/learn/signals"
    before = {p.name: p.stat().st_mtime_ns for p in signals.iterdir()}
    assert total() == json.loads(get(url, "/api/cases"))[0]["case_tokens"] == 39
    assert json.loads(get(url, "/api/case?case=synthetic"))["last"][4] == "777"
    assert before == {p.name: p.stat().st_mtime_ns for p in signals.iterdir()}
    assert 'title="含各阶段及经验学习运行"' in get(url, "/").decode()
