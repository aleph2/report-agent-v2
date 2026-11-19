import base64
import json
from io import BytesIO
import time
from pathlib import Path
from shutil import copyfile
import sys
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
import pypdfium2 as pdfium

from docx import Document
from langchain_core.messages import AIMessage
import pytest

from reportagent import cli, models
from tests.fake_model import ScriptedModel, call


@pytest.mark.parametrize("cap,gather_costs,written", [(100, [1, 1, 1], 4), (6, [1, 1], 3), (4, [1, 1], 2), (3, [3], 0)])
def test_cli_run(tmp_path, monkeypatch, capsys, cap, gather_costs, written):
    (tmp_path / "sources").mkdir()
    (tmp_path / "commission").mkdir(exist_ok=True)
    (tmp_path / "commission/brief.md").write_text("Write the hydrology section.")
    copyfile(Path(__file__).parents[1] / "fixtures/one-page.pdf", tmp_path / "sources/one-page.pdf")
    sentence = "The site drains toward the eastern channel."
    notes = f"{sentence} [one-page.pdf, page 1]\nMISSING: Drainage dimensions."
    section = f"# Hydrology\n\n## Drainage\n\n### Direction\n\n{sentence} [one-page.pdf, page 1]\n\n## 缺失与待补充事项\n\nDrainage dimensions."
    gather = [call("read_file", file_path="/sources/one-page/page-001.txt"),
              call("write_file", file_path="/work/notes/hydrology.md", content=notes), AIMessage(content="Evidence saved.")]
    replies = [call("read_file", file_path="/work/notes/hydrology.md"),
               call("write_file", file_path="/work/sections/01-hydrology.md", content=section),
               call("read_file", file_path="/work/sections/01-hydrology.md"), AIMessage(content="Done.")]
    gathered = sum(gather_costs)
    replies = [*gather[:len(gather_costs)], *replies]
    costs = [*gather_costs, 1, 1, 1, 1]
    model = ScriptedModel(responses=[reply.model_copy(update={"usage_metadata": dict(input_tokens=cost, output_tokens=0, total_tokens=cost)}) for reply, cost in zip(replies, costs)])
    made, limits = [], []
    monkeypatch.setattr(cli, "make_model", lambda: made.append(True) or model)
    middleware = cli.TokenCapMiddleware
    monkeypatch.setattr(cli, "TokenCapMiddleware", lambda limit, **kw: limits.append(limit) or middleware(limit, **kw))
    for required in (["run"], ["review"], ["review", "--section", "01-hydrology"], ["review", "--max-tokens", "100"]):
        monkeypatch.setattr(sys, "argv", ["reportagent", *required, str(tmp_path)])
        with pytest.raises(SystemExit) as missing:
            cli.main()
        assert missing.value.code == 2 and not made
    for args in (("ingest", str(tmp_path)), ("run", str(tmp_path), "--max-tokens", str(cap))):
        monkeypatch.setattr(sys, "argv", ["reportagent", *args])
        cli.main()
    assert (tmp_path / "index.md").read_text() == "# Case files\n\n## commission/brief.md\n\nText: commission/brief.md.files/text.md\n\n## sources/one-page.pdf\n\nPages: 1\n\nPage paths: sources/one-page/page-001.txt ... page-001.txt\nPage images: sources/one-page/page-001.png ... page-001.png\n\n"
    with Image.open(tmp_path / "sources/one-page/page-001.png") as image:
        assert image.format == "PNG" and max(image.size) == 1600
    assert len(made) == 1 and limits == [int(cap * 0.6)] + ([max(0, cap - gathered)] if written else [])
    if len(gather_costs) > 1:
        assert (tmp_path / "work/notes/hydrology.md").read_text() == notes
    logs = sorted((tmp_path / "work/runs").glob("*.jsonl"))
    assert len(logs) == (2 if written else 1)
    research = [json.loads(line) for line in logs[0].read_text().splitlines()]
    draft = [json.loads(line) for line in logs[1].read_text().splitlines()] if written else []
    assert sentence in research[2]["content"]
    assert all(m["status"] == "success" for log in (research, draft) for m in log if m["type"] == "tool")
    assert research[0]["content"] == "Gather evidence for the section requested in /commission/."
    if written:
        assert draft[0]["content"] == "Write the section from the notes in /work/notes/."
        assert [m["content"] for m in draft if m["type"] == "human"] == [draft[0]["content"]] + (["Token budget reached. Save your work now in one write; do not read anything more."] if written < 4 else [])
    assert [sum((m.get("usage_metadata") or {}).get("total_tokens", 0) for m in log) for log in (research, draft)] == [gathered, written]
    output = capsys.readouterr().out
    assert f"Reported tokens: {gathered + written}" in output
    if written:
        assert draft[1]["tool_calls"][0]["args"]["file_path"] == "/work/notes/hydrology.md" and notes in draft[2]["content"]
        assert (tmp_path / "work/sections/01-hydrology.md").read_text() == section
        monkeypatch.setattr(sys, "argv", ["reportagent", "export", str(tmp_path)])
        cli.main()
        paragraphs = Document(tmp_path / "out/report.docx").paragraphs
        assert [(p.text, p.style.name) for p in paragraphs[:3]] == [("Hydrology", "Heading 1"), ("Drainage", "Heading 2"), ("Direction", "Heading 3")]
        assert sentence in "\n".join(p.text for p in paragraphs)
    else:
        assert not draft and "No notes for requested section; writing skipped." in output
        assert not (tmp_path / "work/sections").exists()


@pytest.mark.parametrize("failure", ["none", "once", "twice"])
def test_cli_write_only(tmp_path, monkeypatch, capsys, failure):
    from httpx import RemoteProtocolError
    (tmp_path / "commission").mkdir(exist_ok=True)
    (tmp_path / "commission/brief.md").write_text("Write the hydrology section.")
    notes = "The site drains east. [one-page.pdf, page 1]"
    (tmp_path / "work/notes").mkdir(parents=True)
    (tmp_path / "work/notes/hydrology.md").write_text(notes)
    section = f"# Hydrology\n\n{notes}"
    replies = [call("read_file", file_path="/work/notes/hydrology.md"),
               call("write_file", file_path="/work/sections/01-hydrology.md", content=section), AIMessage(content="Done.")]
    model = ScriptedModel(responses=[m.model_copy(update={"usage_metadata": dict(input_tokens=1, output_tokens=0, total_tokens=1)}) for m in replies])
    attempts, original = [], ScriptedModel._generate
    def generate(self, messages, **kwargs):
        attempts.append([(m.type, m.content) for m in messages])
        if (failure != "none" and len(attempts) == 2) or (failure == "twice" and len(attempts) == 3):
            raise RemoteProtocolError("No response.")
        return original(self, messages, **kwargs)
    monkeypatch.setattr(ScriptedModel, "_generate", generate)
    monkeypatch.setattr(cli, "make_model", lambda: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--max-tokens", "3", "--write-only"])
    if failure == "twice":
        with pytest.raises(RemoteProtocolError):
            cli.main()
    else:
        cli.main()
    logs = list((tmp_path / "work/runs").glob("*.jsonl"))
    assert len(logs) == 1 and logs[0].name.endswith("-write.jsonl")
    transcript = [json.loads(line) for line in logs[0].read_text().splitlines()]
    events = [m for m in transcript if m["type"] == "resume"]
    assert len(attempts) == (2 if failure == "none" else 3) and len(events) == int(failure != "none")
    assert failure == "none" or (attempts[1] == attempts[2] and events[0]["total_tokens"] == 1)
    assert f"Reported tokens: {1 if failure == 'twice' else 2}" in capsys.readouterr().out
    if failure == "twice":
        assert transcript[-1]["type"] == "resume" and not (tmp_path / "work/sections").exists()
        return
    assert transcript[0]["content"] == "Write the section from the notes in /work/notes/."
    assert notes in transcript[2]["content"] and transcript[-2]["type"] == "tool" and transcript[-2]["status"] == "success" and "3 * previous call" in transcript[-1]["content"]
    assert (tmp_path / "work/sections/01-hydrology.md").read_text() == section
    assert (tmp_path / "work/notes/hydrology.md").read_text() == notes


@pytest.mark.parametrize("constructed", [False, True])
def test_model_from_current_dotenv(tmp_path, monkeypatch, constructed):
    monkeypatch.chdir(tmp_path)
    for name in ("MODEL_NAME", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("MODEL_NAME=env-model\nOPENAI_BASE_URL=https://llm.example/v1\nOPENAI_API_KEY=offline-key\n")
    settings = {}
    original = models.init_chat_model
    monkeypatch.setattr(models, "init_chat_model", lambda **kwargs: settings.update(kwargs) or (original(**kwargs) if constructed else "model"))
    model = models.make_model()
    assert model.max_retries == model.root_client.max_retries == model.root_async_client.max_retries == 0 and all(client.default_headers["Connection"] == "close" and not client._client._transport._pool._http2 for client in (model.root_client, model.root_async_client)) if constructed else model == "model"
    assert settings == dict(model="env-model", model_provider="openai", base_url="https://llm.example/v1", api_key="offline-key", temperature=0.0, max_retries=0, default_headers={"Connection": "close"})
    for name in ("MODEL_NAME", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("MODEL_NAME=env-model\n")
    with pytest.raises(KeyError, match="OPENAI_BASE_URL"):
        models.make_model()


def test_failed_run_keeps_conversation(tmp_path, monkeypatch):
    def interrupted(state, **kwargs):
        yield {"messages": [*state["messages"], AIMessage(content="Before failure.")]}
        (tmp_path / "missing").read_text()
    monkeypatch.setattr(cli, "make_model", lambda: ScriptedModel(responses=[AIMessage(content="unused")]))
    monkeypatch.setattr(cli, "build_agent", lambda *args, **kwargs: SimpleNamespace(stream=interrupted))
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--max-tokens", "100"])
    with pytest.raises(FileNotFoundError):
        cli.main()
    transcript = [json.loads(line) for line in next((tmp_path / "work/runs").glob("*.jsonl")).read_text().splitlines()]
    assert transcript[0]["type"] == "human" and transcript[-1]["content"] == "Before failure."


def test_cli_call_limit_and_missing_usage(tmp_path, monkeypatch):
    (tmp_path / "commission").mkdir(exist_ok=True)
    (tmp_path / "commission/brief.md").write_text("Read the commission.")
    model = ScriptedModel(responses=[call("read_file", file_path="/commission/brief.md") for _ in range(401)])
    monkeypatch.setattr(cli, "make_model", lambda: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--max-tokens", "100000"])
    cli.main()
    logs = list((tmp_path / "work/runs").glob("*.jsonl"))
    assert model.i == 200 and len(logs) == 1
    for log in logs:
        transcript = [json.loads(line) for line in log.read_text().splitlines()]
        assert "200" in transcript[-1]["content"] and "missing" in transcript[-1]["content"].lower()
        assert sum((message.get("usage_metadata") or {}).get("total_tokens", 0) for message in transcript) == 0


def test_failed_text_continues_and_png_reaches_model(tmp_path):
    (tmp_path / "sources").mkdir()
    with pdfium.PdfDocument.new() as pdf:
        for _ in range(2): pdf.new_page(300, 200).close()
        pdf.save(tmp_path / "sources/two.pdf")
    with patch("pdfplumber.page.Page.extract_text", side_effect=[RuntimeError("broken\ntext"), "Second page"]):
        cli.ingest(tmp_path)
    assert [(tmp_path / f"sources/two/page-{n:03d}.txt").read_text() for n in (1, 2)] == ["Text extraction failed: broken text. See page-001.png.", "Second page"]
    with patch.object(ScriptedModel, "_generate", autospec=True, side_effect=ScriptedModel._generate) as generated:
        cli.build_agent(tmp_path, ScriptedModel(responses=[call("read_file", file_path="/sources/two/page-001.png"), AIMessage(content="Done.")])).invoke({"messages": [{"role": "user", "content": "Read the failed page image."}]})
    block = generated.call_args_list[1].args[1][-1].content_blocks[0]
    assert block["type"] == "image" and block["mime_type"] == "image/png"
    assert base64.b64decode(block["base64"]) == (tmp_path / "sources/two/page-001.png").read_bytes()


def test_plan_then_named_section(tmp_path, monkeypatch):
    section = "01-hydrology"
    outputs = {"/work/plan.md": f"## {section}\nDrainage. [guide.pdf, page 1]\n", f"/work/notes/{section}.md": "Evidence.",
               f"/work/sections/{section}.md": "# Hydrology", f"/work/reviews/{section}.md": "method | Hydrology | Missing band. [guide.pdf, page 1] | must-fix"}
    model = ScriptedModel(responses=[reply for path, text in outputs.items() for reply in (call("write_file", file_path=path, content=text), AIMessage(content="Saved."))])
    monkeypatch.setattr(cli, "make_model", lambda: model)
    for command in ("plan", "run", "review"):
        section_args = ["--section", section] if command != "plan" else []
        monkeypatch.setattr(sys, "argv", ["reportagent", command, str(tmp_path), "--max-tokens", "10", *section_args])
        cli.main()
    assert {path: (tmp_path / path.lstrip("/")).read_text() for path in outputs} == outputs
    logs = {p.stem.rsplit("-", 1)[1]: [json.loads(line) for line in p.read_text().splitlines()] for p in (tmp_path / "work/runs").glob("*.jsonl")}
    assert set(logs) == {"plan", "gather", "write", "review"} and logs["plan"][0]["content"] == "Plan the report requested in /commission/."
    assert logs["review"][0]["content"] == f"Review section {section}. Save the review to /work/reviews/{section}.md."
    for phase, folder in (("gather", "notes"), ("write", "sections")):
        prompt = logs[phase][0]["content"]
        assert section in prompt and f"## {section}" in prompt and "/work/plan.md" in prompt and f"/work/{folder}/{section}.md" in prompt and (phase != "write" or "/commission/" not in prompt)



def test_mixed_case_inputs_and_commission_image(tmp_path):
    image = BytesIO()
    Image.new("RGB", (3, 2), (17, 34, 51)).save(image, "PNG")
    original = {}
    for folder in ("commission", "sources", "reference"):
        root = tmp_path / folder; root.mkdir()
        (root / "brief.md").write_bytes(b"Synthetic scope.\n")
        doc = Document(); doc.add_heading("Synthetic heading", 2)
        table = doc.add_table(rows=1, cols=2); table.cell(0, 0).text = "A"; table.cell(0, 1).text = "B"
        paragraph = doc.add_paragraph("Before"); paragraph.add_run().add_picture(BytesIO(image.getvalue())); paragraph.add_run("After")
        doc.save(root / "outline.docx")
        (root / "broken.docx").write_text("Not a DOCX package.")
        (root / "other.bin").write_bytes(b"Unknown file type.")
        copyfile(Path(__file__).parents[1] / "fixtures/one-page.pdf", root / "one.pdf")
        original.update({p: p.read_bytes() for p in root.iterdir()})
    index = cli.ingest(tmp_path)
    assert index == tmp_path / "index.md"
    for folder in ("commission", "sources", "reference"):
        root = tmp_path / folder
        assert (root / "brief.md.files/text.md").read_bytes() == b"Synthetic scope.\n"
        text = (root / "outline.docx.files/text.md").read_text()
        assert "## Synthetic heading" in text and "A | B" in text and "Before\n[image-01.png]\nAfter" in text
        with Image.open(root / "outline.docx.files/image-01.png") as actual: assert actual.convert("RGB").getpixel((0, 0)) == (17, 34, 51)
        assert (root / "one/page-001.txt").read_text().strip() and (root / "one/page-001.png").exists()
        for name in ("broken.docx", "other.bin"): assert (root / f"{name}.files/text.md").read_text().startswith("Could not read: ")
        assert all(f"{folder}/{name}" in index.read_text() for name in ("brief.md", "outline.docx", "broken.docx", "other.bin", "one.pdf"))
    assert all(p.read_bytes() == data for p, data in original.items())
    with patch.object(ScriptedModel, "_generate", autospec=True, side_effect=ScriptedModel._generate) as generated:
        cli.build_agent(tmp_path, ScriptedModel(responses=[call("read_file", file_path="/commission/outline.docx.files/image-01.png"), AIMessage(content="Done.")])).invoke({"messages": [("user", "Read the instruction image.")]})
    assert base64.b64decode(generated.call_args_list[1].args[1][-1].content_blocks[0]["base64"]) == (tmp_path / "commission/outline.docx.files/image-01.png").read_bytes()


def test_pdf_timeout_continues(tmp_path, monkeypatch):
    import reportagent.ingest as module
    root = tmp_path / "sources"; root.mkdir()
    with pdfium.PdfDocument.new() as pdf:
        for _ in range(2): pdf.new_page(300, 200).close()
        pdf.save(root / "two.pdf")
    monkeypatch.setattr(module, "PDF_TEXT_TIMEOUT", 0.2, raising=False)
    with patch("pdfplumber.page.Page.extract_text", autospec=True, side_effect=lambda page: time.sleep(1) if page.page_number == 1 else "Second page"):
        cli.ingest(tmp_path)
    assert (root / "two/page-001.txt").read_text() == "Text extraction timed out. See page-001.png."
    assert (root / "two/page-001.png").exists() and (root / "two/page-002.txt").read_text() == "Second page"


@pytest.mark.parametrize("command", ["plan", "review"])
def test_plan_and_review_receive_half_budget_note(tmp_path, monkeypatch, command):
    (tmp_path / "commission").mkdir()
    (tmp_path / "commission/brief.md").write_text("Plan one synthetic section.")
    plan = "## 01-synthetic\nSynthetic guidance."
    target = "/work/plan.md" if command == "plan" else "/work/reviews/01-synthetic.md"
    replies = [call("read_file", file_path="/commission/brief.md"), call("read_file", file_path="/commission/brief.md"), call("write_file", file_path=target, content=plan), AIMessage(content="Saved.")]
    model = ScriptedModel(responses=[reply.model_copy(update={"usage_metadata": dict(input_tokens=n, output_tokens=0, total_tokens=n)}) for reply, n in zip(replies, [2, 2, 1, 1])])
    monkeypatch.setattr(cli, "make_model", lambda: model)
    section_args = ["--section", "01-synthetic"] if command == "review" else []
    monkeypatch.setattr(sys, "argv", ["reportagent", command, str(tmp_path), "--max-tokens", "10", *section_args])
    cli.main()
    messages = [json.loads(line) for line in next((tmp_path / "work/runs").glob("*.jsonl")).read_text().splitlines()]
    notes = [m for m in messages if m["type"] == "human" and m["content"].startswith("You have used")]
    assert len(notes) == 1 and notes[0]["content"].startswith("You have used 5 of 10 tokens.")
    assert messages[messages.index(notes[0]) - 1]["type"] == "tool" and (tmp_path / target.lstrip("/")).read_text() == plan


@pytest.mark.parametrize("matching", [False, True])
def test_named_run_requires_notes_file(tmp_path, monkeypatch, capsys, matching):
    notes = tmp_path / "work/notes"; notes.mkdir(parents=True)
    (notes / ("02-synthetic.md" if matching else "01-other.md")).touch()
    model = ScriptedModel(responses=[AIMessage(content="Done.") for _ in range(3)])
    monkeypatch.setattr(cli, "make_model", lambda: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--section", "02-synthetic", "--max-tokens", "100"])
    cli.main()
    assert model.i == (2 if matching else 1)
    assert len(list((tmp_path / "work/runs").glob("*.jsonl"))) == (2 if matching else 1)
    assert ("No notes for 02-synthetic; writing skipped." in capsys.readouterr().out) is (not matching)


@pytest.mark.parametrize("complete", [False, True])
def test_cli_citation_check(tmp_path, monkeypatch, complete):
    source = tmp_path / "sources"; source.mkdir()
    for name in ("guide.pdf", "other.pdf"):
        (source / name).write_bytes(b"Synthetic source.")
        (source / Path(name).stem).mkdir()
    texts = {1: "Height 42.6 m length 140 m angle 182°∠50° and 123.\n20~30\n", 2: "88\nsecond line\n", 3: "7\n", 5: "", 6: "Image page\n", 7: "a\nb\n", 18: "20~30", 19: "20~30", 20: "20~30", 95: "20~30", 96: "20~30"}
    for n, text in texts.items():
        (source / f"guide/page-{n:03d}.txt").write_text(text)
    (source / "other/page-001.txt").write_text("88")
    (source / "other/page-002.txt").write_text("−4.5")
    reference = tmp_path / "reference"; reference.mkdir()
    (reference / "outside.pdf").write_text("777")
    (reference / "outside").mkdir(); (reference / "outside/page-001.txt").write_text("777")
    sections = tmp_path / "work/sections"; sections.mkdir(parents=True)
    (sections / "demo.md").write_text("\n".join([
        "Height４２．６m length１４０m angle１８２°∠５０° [guide.pdf, page 001–003]。",
        "Number 23 [guide.pdf, page 1]。",
        "Value 999 [missing.pdf, page 1]。",
        "Missing text [guide.pdf, page 004]。",
        "Outside 777 [../reference/outside.pdf, page 1]。",
        "Alias [同上, page 1]。",
        "Range 20～30 [guide.pdf, page 018–020, page 95, page 96]。",
        "Angle 182∠50 and value 88 [guide.pdf, page 1；other.pdf, page 1]。",
        "Unqualified [page 1]。",
        "Uncited 999. Height 42.6 [guide.pdf, page 1].",
        "Reversed [guide.pdf, page 020–018]。",
        "Huge range [guide.pdf, page 1–999999999]。",
        "Negative -140 [guide.pdf, page 1]。",
        "Signed -4.5 [other.pdf, page 2]。",
    ]))
    notes = tmp_path / "work/notes"; notes.mkdir()
    (notes / "demo.md").write_text("\n".join(["MISSING [guide.pdf, page 1]", "Not found [guide.pdf, page 2]", "Absent [guide.pdf, page 3]", "MISSING page 4", "MISSING no page citation", "MISSING [guide.pdf, page 5]", "MISSING [guide.pdf, page 6]", "MISSING [guide.pdf, page 7]", "## Absent [guide.pdf, page 3]"]))
    calls, results = [], []
    def read(n, start, end, body):
        key = f"read-{len(calls)}"
        calls.append(dict(name="read_file", id=key, args=dict(file_path=f"/sources/guide/page-{n:03d}.txt", offset=start - 1, limit=end - start + 1)))
        results.append(dict(type="tool", tool_call_id=key, status="success", content=body))
    read(1, 1, 2, "@@ lines 1-2 of 2 @@\n" + texts[1].rstrip("\n"))
    read(2, 1, 1, "@@ lines 1-1 of 2 | next offset 1 @@\n88")
    if complete:
        read(2, 2, 2, "@@ lines 2-2 of 2 @@\nsecond line")
    read(3, 1, 1, "@@ lines 1-1 of 1 @@\n7"); results[-1]["status"] = "error"
    read(5, 1, 1, "System reminder: File exists but has empty contents")
    read(7, 1, 2, "@@ lines 1-2 of 2 @@\na")
    image = BytesIO(); Image.new("RGB", (2, 2)).save(image, "PNG")
    (source / "guide/page-006.png").write_bytes(image.getvalue())
    calls.append(dict(name="read_file", id="image", args=dict(file_path="/sources/guide/page-006.png")))
    results.append(dict(type="tool", tool_call_id="image", status="success", content=[dict(type="image_url", image_url=dict(url="data:image/png;base64," + base64.b64encode(image.getvalue()).decode()))]))
    runs = tmp_path / "work/runs"; runs.mkdir()
    rows = [dict(type="human", content="Gather evidence for section demo. Save notes to /work/notes/demo.md."), dict(type="ai", tool_calls=calls), dict(type="resume", total_tokens=0), *results]
    (runs / "1-gather.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    (runs / "2-gather.jsonl").write_text("\n".join(json.dumps(row) for row in [dict(type="human", content="Gather evidence for section other. Save notes to /work/notes/other.md."), dict(type="ai", tool_calls=[calls[-4]]), dict(type="tool", tool_call_id=calls[-4]["id"], status="success", content="@@ lines 1-1 of 1 @@\n7")]))
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(cli, "make_model", lambda: pytest.fail("check must not construct a model"))
    monkeypatch.setattr(sys, "argv", ["reportagent", "check", str(tmp_path), "--section", "demo"])
    cli.main()
    report = (tmp_path / "work/checks/demo.md").read_text()
    assert "sections/demo.md:2" in report and "23" in report
    assert all(f"sections/demo.md:{n}:" in report for n in (3, 4, 5, 6, 9, 11, 12, 13))
    assert all(f"sections/demo.md:{n}:" not in report for n in (1, 7, 8, 10, 14))
    assert ("notes/demo.md:2" not in report) is complete
    assert all(f"notes/demo.md:{n}:" in report for n in (3, 4, 5, 8, 9))
    assert all(f"notes/demo.md:{n}:" not in report for n in (1, 6, 7))
    assert all(p.read_bytes() == data for p, data in before.items())
    assert all(line.strip() and ".md:" in line and " | " in line for line in report.splitlines())


def test_verified_citations_produce_empty_check(tmp_path):
    source = tmp_path / "sources"; source.mkdir()
    (source / "guide.pdf").touch(); (source / "guide").mkdir()
    (source / "guide/page-001.txt").write_text("42.6 m")
    sections = tmp_path / "work/sections"; sections.mkdir(parents=True)
    (sections / "demo.md").write_text("Height ４２．６ m [guide.pdf, page 1].")
    notes = tmp_path / "work/notes"; notes.mkdir()
    (notes / "demo.md").touch()
    assert cli.check(tmp_path, "demo").read_text() == ""
