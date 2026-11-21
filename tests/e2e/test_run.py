import base64
from decimal import Decimal
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


def phase_transcripts(folder):
    return (path for path in folder.glob("*.jsonl") if not path.name.endswith(".requests.jsonl"))


@pytest.mark.parametrize("phase,target,tool", [
    ("verify-sources", "reviews/demo-sources", "write_file"),
    ("review", "reviews/demo", "write_file"),
    ("clarify", "clarifications/demo", "write_file"),
    ("revise", "sections/demo", "edit_file"),
])
def test_revision_phase_save_notice(tmp_path, phase, target, tool):
    from reportagent.budget import TokenCapMiddleware
    destination = f"/work/{target}.md"
    before, saved = ("Before first\nBefore second", "Saved first\nSaved second") if phase == "revise" else ("Before", "Saved")
    file = tmp_path / destination.lstrip("/"); file.parent.mkdir(parents=True, exist_ok=True); file.write_text(before)
    allowed = (call(tool, file_path=destination, old_string=before, new_string=saved) if tool == "edit_file"
               else call(tool, file_path=destination, content=saved))
    costs = dict(input_tokens=30, output_tokens=0, total_tokens=30)
    model = ScriptedModel(responses=[call("read_file", file_path="/index.md").model_copy(update={"usage_metadata": costs}),
                                    allowed.model_copy(update={"usage_metadata": dict(input_tokens=1, output_tokens=0, total_tokens=1)})])
    result = cli.build_agent(tmp_path, model, phase=phase, section="demo",
                             middleware=[TokenCapMiddleware(100, phase=phase)]).invoke({"messages": [("user", "Save.")]})
    notice = next(m.content for m in result["messages"] if m.type == "human" and "Token budget reached" in m.content)
    assert phase in notice and tool in notice and "one write" not in notice
    if phase == "revise":
        assert "separate edit_file calls in this one response" in notice
    if phase == "clarify":
        assert "official clarification file" in notice and "separate draft" in notice
    assert file.read_text() == saved and result["total_tokens"] == 31
    assert sum(m.type == "ai" and bool(m.usage_metadata) for m in result["messages"]) == 2


@pytest.mark.parametrize("cap,gather_costs,write_cost,written", [
    (300_000, [20_000] * 3, 20_000, 4), (180_000, [30_000] * 3, 30_000, 2),
    (120_000, [30_000] * 2, 30_000, 2), (90_000, [54_000], 30_000, 0),
])
def test_cli_run(tmp_path, monkeypatch, capsys, cap, gather_costs, write_cost, written):
    (tmp_path / "sources").mkdir()
    (tmp_path / "commission").mkdir(exist_ok=True)
    (tmp_path / "commission/brief.md").write_text("Write the hydrology section.")
    copyfile(Path(__file__).parents[1] / "fixtures/one-page.pdf", tmp_path / "sources/one-page.pdf")
    sentence = "The site drains toward the eastern channel."
    notes = f"{sentence} [one-page.pdf, page 1]\nMISSING: Drainage dimensions."
    section = f"# Hydrology\n\n## Drainage\n\n### Direction\n\n{sentence} [one-page.pdf, page 1]"
    gather = [call("read_file", file_path="/sources/one-page/page-001.txt"),
              call("write_file", file_path="/work/notes/hydrology.md", content=notes), AIMessage(content="Evidence saved.")]
    replies = [call("read_file", file_path="/work/notes/hydrology.md"),
               call("write_file", file_path="/work/sections/01-hydrology.md", content=section),
               call("read_file", file_path="/work/sections/01-hydrology.md"), AIMessage(content="Done.")]
    gathered = sum(gather_costs)
    replies = [*gather[:len(gather_costs)], *replies]
    costs = [*gather_costs, *([write_cost] * 4)]
    model = ScriptedModel(responses=[reply.model_copy(update={"usage_metadata": dict(input_tokens=cost, output_tokens=0, total_tokens=cost)}) for reply, cost in zip(replies, costs)])
    made, limits = [], []
    monkeypatch.setattr(cli, "make_model", lambda phase=None: made.append(True) or model)
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
    assert len(made) == (2 if written else 1) and limits == [int(cap * 0.6)] + ([max(0, cap - gathered)] if written else [])
    if len(gather_costs) > 1:
        assert (tmp_path / "work/notes/hydrology.md").read_text() == notes
    logs = sorted(phase_transcripts(tmp_path / "work/runs"))
    assert len(logs) == (2 if written else 1)
    research = [json.loads(line) for line in logs[0].read_text().splitlines()]
    draft = [json.loads(line) for line in logs[1].read_text().splitlines()] if written else []
    assert sentence in research[2]["content"]
    assert all(m["status"] == "success" for log in (research, draft) for m in log if m["type"] == "tool")
    assert research[0]["content"] == "Gather evidence for the section requested in /commission/."
    if written:
        assert draft[0]["content"] == "Write the section from the notes in /work/notes/."
        notices = [m["content"] for m in draft[1:] if m["type"] == "human"]
        remaining = cap - gathered - (written - 1) * write_cost
        save_notice = ("Token budget reached. Save your work now in one write; do not read anything more. "
                       f"Remaining allowance: {remaining} tokens.")
        assert notices == ([save_notice] if written < 4 else [])
    assert [sum((m.get("usage_metadata") or {}).get("total_tokens", 0) for m in log) for log in (research, draft)] == [gathered, written * write_cost]
    output = capsys.readouterr().out
    assert f"Reported tokens: {gathered + written * write_cost}" in output
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


@pytest.mark.parametrize("budget", [60_000, 300_000, 600_000])
def test_cli_revision(tmp_path, monkeypatch, budget):
    paths = {"check": "/work/checks/demo.md", "sources": "/work/reviews/demo-sources.md",
             "review": "/work/reviews/demo.md", "body": "/work/sections/demo.md",
             "clarify": "/work/clarifications/demo.md", "answers": "/commission/澄清答复.md"}
    for path in (paths["check"], paths["body"], paths["clarify"], paths["answers"], "/sources/design/page-001.txt", "/sources/design/page-002.txt"):
        file = tmp_path / path.lstrip("/"); file.parent.mkdir(parents=True, exist_ok=True); file.write_text("Before")
    top = "# Source pages to review\n- indicator X11 | design.pdf, page 1\n"
    (tmp_path / paths["check"].lstrip("/")).write_text(top + "\n## Findings\nFinding omitted from the first message.\n")
    reads = [[paths["check"], "/sources/design/page-001.txt", "/sources/design/page-002.txt"],
             [paths["sources"], paths["body"]], [],
             ["/sources/design/page-001.txt"]]
    saves = [call("write_file", file_path=paths[key], content="Checked") for key in ("sources", "review", "clarify")]
    saves.append(call("edit_file", file_path=paths["body"], old_string="Before", new_string="Revised"))
    replies = [reply for pages, save in zip(reads, saves) for reply in
               [*(call("read_file", file_path=path) for path in pages), save, AIMessage(content="Saved.")]]
    model = ScriptedModel(responses=[m.model_copy(update={"usage_metadata": dict(input_tokens=1, output_tokens=0, total_tokens=1)}) for m in replies])
    limits, original = [], cli.TokenCapMiddleware
    monkeypatch.setattr(cli, "make_model", lambda phase=None: model)
    monkeypatch.setattr(cli, "TokenCapMiddleware", lambda limit, **kw: limits.append(limit) or original(limit, **kw))
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--section", "demo", "--max-tokens", str(budget)])
    cli.main()
    expected, used = [], 0
    for maximum, pages in zip((80000, 100000, 40000, 80000), reads):
        expected.append(min(maximum, budget - used)); used += len(pages) + 2
    assert limits == expected and sum(limits) <= 300000
    logs = sorted(phase_transcripts(tmp_path / "work/runs"))
    assert sum(bool(json.loads(line).get("usage_metadata")) for log in logs for line in log.read_text().splitlines()) == len(replies)
    for log, phase, pages in zip(logs, ("verify-sources", "review", "clarify", "revise"), reads):
        messages = [json.loads(line) for line in log.read_text().splitlines()]
        assert log.name.endswith(f"-{phase}.jsonl") and phase in messages[int(phase == "verify-sources")]["content"]
        if phase == "verify-sources": assert messages[0]["content"] == top
        expected_status = ["success" if path.startswith("/sources/") else "error" for path in pages] + ["success"]
        assert [m["status"] for m in messages if m["type"] == "tool"] == expected_status
        assert next(m for m in messages if m["type"] == "ai")["tool_calls"][0]["args"]["file_path"] == (pages[0] if pages else paths["clarify"])
    assert (tmp_path / paths["body"].lstrip("/")).read_text() == "Revised"
    assert all((tmp_path / paths[k].lstrip("/")).read_text() == "Checked" for k in ("sources", "review", "clarify"))
    assert (tmp_path / paths["answers"].lstrip("/")).read_text() == "Before"


def test_verification_opens_with_cited_page(tmp_path, monkeypatch):
    source = tmp_path / "sources"
    source.mkdir()
    copyfile(Path(__file__).parents[1] / "fixtures/one-page.pdf", source / "design.pdf")
    cli.ingest(tmp_path)
    page = source / "design/page-001.txt"
    page.write_text("Table 4: design height 42 m.")
    (source / "design/page-002.txt").write_text("ANOTHER PAGE")
    body = tmp_path / "work/sections/demo.md"
    body.parent.mkdir(parents=True)
    claim = "| X11 | design height 42 m [page 018；design.pdf, page 001] |"
    body.write_text(claim + "\n| X12 | different claim [design.pdf, page 002] |")
    notes = tmp_path / "work/notes/demo.md"
    notes.parent.mkdir()
    notes.touch()
    top = cli.check(tmp_path, "demo").read_text().partition("\n## Findings")[0]
    model = ScriptedModel(responses=[AIMessage(content="Done.")])
    monkeypatch.setattr(cli, "make_model", lambda phase: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--section", "demo", "--max-tokens", "100000"])
    with patch.object(ScriptedModel, "_generate", autospec=True, side_effect=ScriptedModel._generate) as generated:
        cli.main()
    first = generated.call_args_list[0].args[1]
    blocks = [b for m in first if m.type == "human" for b in m.content_blocks]
    texts = [b["text"] for b in blocks if b["type"] == "text"]
    assert top in texts and any(claim in t for t in texts)
    assert any("/sources/design/page-001.txt" in t and page.read_text() in t for t in texts)
    assert sum("ANOTHER PAGE" in t for t in texts) == 1
    assert any("different claim" in t for t in texts)
    images = [b for b in blocks if b["type"] == "image"]
    assert len(images) == 1 and base64.b64decode(images[0]["base64"]) == page.with_suffix(".png").read_bytes()


def test_verification_attaches_only_available_explicit_pages(tmp_path):
    notes = tmp_path / "work/notes/demo.md"
    notes.parent.mkdir(parents=True)
    notes.write_text("Missing [page 18; ../private.pdf, page 1; a.pdf, page 2–9000, page 2; b.pdf, page 4].")
    sources = tmp_path / "sources"
    for filename in ("a", "b"):
        (sources / filename).mkdir(parents=True)
        (sources / (filename + ".pdf")).touch()
    secret = tmp_path / "private.pdf"
    secret.write_text("outside case sources")
    (sources / "a/page-002.txt").write_text("first page in range")
    (sources / "a/page-003.txt").write_text("later page")
    (sources / "a/page-9002.txt").write_text("outside range")
    (sources / "a/page-002.png").symlink_to(secret)
    (sources / "b/page-004.txt").write_bytes(bytes([255]))
    top = "- absence Missing | /work/notes/demo.md:1:1 | citations"
    blocks = cli._verification_input(tmp_path, "demo", top).content_blocks
    texts = [b["text"] for b in blocks]
    assert any("later page" in t for t in texts)
    assert sum("first page in range" in t for t in texts) == 1
    assert not any(s in t for t in texts for s in ("outside case sources", "outside range"))
    assert any("page-004.txt" in t and "unavailable" in t.lower() for t in texts)
    assert cli._verification_input(tmp_path, "demo", top.replace(":1:1", ":15:1")).content == top.replace(":1:1", ":15:1")



def test_absence_attaches_continuation_and_images_only_for_short_text(tmp_path):
    pages = tmp_path / "sources/design"
    pages.mkdir(parents=True)
    pages.with_suffix(".pdf").touch()
    body = tmp_path / "work/sections/demo.md"
    body.parent.mkdir(parents=True)
    body.write_text("Missing slope [design.pdf, page 22].\nRange [design.pdf, pages 25–27].")
    for n in (22, 23, 25, 26, 27, 28):
        (pages / f"page-{n:03d}.txt").write_text("图" * 300 if n == 22 else f"row {n}")
        (pages / f"page-{n:03d}.png").write_bytes(str(n).encode())
    top = "\n".join(["- indicator X11 | /work/sections/demo.md:1:1 | citation",
                     "- absence Missing | /work/sections/demo.md:1:1 | citation",
                     "- indicator X12 | /work/sections/demo.md:2:1 | citation"])
    blocks = cli._verification_input(tmp_path, "demo", top).content_blocks
    assert [base64.b64decode(b["base64"]) for b in blocks if b["type"] == "image"] == [b"23", b"25", b"26", b"27"]
    assert any(b.get("text", "").endswith("row 23") for b in blocks)
    assert sum(b.get("text", "").startswith("Claim to check") for b in blocks) == 2


def test_check_compares_stated_risk_and_counts_source_items(tmp_path):
    for key in ("sections", "notes", "reviews", "clarifications"):
        (tmp_path / "work" / key).mkdir(parents=True)
    (tmp_path / "work/sections/demo.md").write_text("算术基准值 F＝64.59，对应Ⅳ级。未见。未列。")
    (tmp_path / "work/notes/demo.md").touch()
    (tmp_path / "work/reviews/demo-sources.md").write_text("|1 (claim)|x|\n|1|another clause|\n")
    (tmp_path / "work/clarifications/demo.md").write_text(
        "number: 77\neffect: 现基准 64.59，Ⅲ级\nnumber: 78\neffect: F=61.25，对应Ⅳ级\n"
        "number: 79\neffect: 现基准 64.590，Ⅳ级\nnumber: 80\neffect: 当前风险等级为Ⅱ级\n")
    result = cli.check(tmp_path, "demo").read_text()
    assert "checklist entries=2; distinct source-check IDs=1; missing IDs=2" in result
    assert all(f"clarification {n} " in result for n in (77, 78, 80))
    assert "clarification 79 " not in result and "64.59" in result and "IV" in result
    (tmp_path / "work/sections/demo.md").write_text("F=64.59。")
    result = cli.check(tmp_path, "demo").read_text()
    assert "clarification 78 " in result and "clarification 77 " not in result


def test_verification_deduplicates_and_bounds_initial_pages(tmp_path):
    doc = tmp_path / "sources/a"
    doc.mkdir(parents=True)
    doc.with_suffix(".pdf").touch()
    (doc / "page-001.png").write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    notes = tmp_path / "work/notes/demo.md"
    notes.parent.mkdir(parents=True)
    for number in range(1, 12):
        (doc / f"page-{number:03d}.txt").write_text(f"source {number}")
    notes.write_text("\n".join(f"Claim {n} [a.pdf, page {n}]" for n in range(1, 12)))
    locations = [f"/work/notes/demo.md:{n}:1 | [a.pdf, page {n}]" for n in range(1, 12)]
    top = "- indicator X11 | " + "; ".join(locations) + "\n- absence missing | " + locations[0]
    blocks = cli._verification_input(tmp_path, "demo", top).content_blocks
    texts = [b["text"] for b in blocks]
    assert not any(b["type"] == "image" for b in blocks)
    assert sum(t.startswith("Claim to check") for t in texts) == 11
    assert sum(t.startswith("Source:") for t in texts) == 10
    assert any(t.endswith("source 10") for t in texts)
    assert not any(t.endswith("source 11") for t in texts)


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
    model = ScriptedModel(responses=[m.model_copy(update={"usage_metadata": dict(input_tokens=30000, output_tokens=0, total_tokens=30000)}) for m in replies])
    attempts, original = [], ScriptedModel._generate
    def generate(self, messages, **kwargs):
        attempts.append([(m.type, m.content) for m in messages])
        if (failure != "none" and len(attempts) == 2) or (failure == "twice" and len(attempts) == 3):
            raise RemoteProtocolError("No response.")
        return original(self, messages, **kwargs)
    monkeypatch.setattr(ScriptedModel, "_generate", generate)
    monkeypatch.setattr(cli, "make_model", lambda phase=None: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--max-tokens", "90000", "--write-only"])
    if failure == "twice":
        with pytest.raises(RemoteProtocolError):
            cli.main()
    else:
        cli.main()
    logs = list(phase_transcripts(tmp_path / "work/runs"))
    assert len(logs) == 1 and logs[0].name.endswith("-write.jsonl")
    transcript = [json.loads(line) for line in logs[0].read_text().splitlines()]
    events = [m for m in transcript if m["type"] == "resume"]
    assert len(attempts) == (2 if failure == "none" else 3) and len(events) == int(failure != "none")
    assert failure == "none" or (attempts[1] == attempts[2] and events[0]["total_tokens"] == 30000)
    assert f"Reported tokens: {30000 if failure == 'twice' else 60000}" in capsys.readouterr().out
    if failure == "twice":
        assert transcript[-1]["type"] == "resume" and not (tmp_path / "work/sections").exists()
        return
    assert transcript[0]["content"] == "Write the section from the notes in /work/notes/."
    assert notes in transcript[2]["content"] and transcript[-2]["type"] == "tool" and transcript[-2]["status"] == "success" and "previous input + output reserve" in transcript[-1]["content"]
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
    assert settings == dict(model="env-model", model_provider="openai", base_url="https://llm.example/v1", api_key="offline-key", temperature=0.0, max_retries=0, model_kwargs={"parallel_tool_calls": False}, default_headers={"Connection": "close"})
    for name in ("MODEL_NAME", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("MODEL_NAME=env-model\n")
    with pytest.raises(KeyError, match="OPENAI_BASE_URL"):
        models.make_model()


def test_failed_run_keeps_conversation(tmp_path, monkeypatch):
    def interrupted(state, **kwargs):
        yield {"messages": [*state["messages"], AIMessage(content="Before failure.")]}
        (tmp_path / "missing").read_text()
    monkeypatch.setattr(cli, "make_model", lambda phase=None: ScriptedModel(responses=[AIMessage(content="unused")]))
    monkeypatch.setattr(cli, "build_agent", lambda *args, **kwargs: SimpleNamespace(stream=interrupted))
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--max-tokens", "100"])
    with pytest.raises(FileNotFoundError):
        cli.main()
    transcript = [json.loads(line) for line in next(phase_transcripts(tmp_path / "work/runs")).read_text().splitlines()]
    assert transcript[0]["type"] == "human" and transcript[-1]["content"] == "Before failure."


def test_cli_call_limit_and_missing_usage(tmp_path, monkeypatch):
    (tmp_path / "commission").mkdir(exist_ok=True)
    (tmp_path / "commission/brief.md").write_text("Read the commission.")
    model = ScriptedModel(responses=[call("read_file", file_path="/commission/brief.md") for _ in range(401)])
    monkeypatch.setattr(cli, "make_model", lambda phase=None: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--max-tokens", "300000"])
    cli.main()
    logs = list(phase_transcripts(tmp_path / "work/runs"))
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
    monkeypatch.setattr(cli, "make_model", lambda phase=None: model)
    for command in ("plan", "run", "review"):
        section_args = ["--section", section] if command != "plan" else []
        monkeypatch.setattr(sys, "argv", ["reportagent", command, str(tmp_path), "--max-tokens", "100000", *section_args])
        cli.main()
    assert {path: (tmp_path / path.lstrip("/")).read_text() for path in outputs} == outputs
    logs = {p.stem.rsplit("-", 1)[1]: [json.loads(line) for line in p.read_text().splitlines()] for p in phase_transcripts(tmp_path / "work/runs")}
    sidecars = list((tmp_path / "work/runs").glob("*.requests.jsonl"))
    assert {p.name.split("-")[-1].split(".")[0] for p in sidecars} == {"plan", "gather", "write", "review"}
    for path in sidecars:
        first = json.loads(path.read_text().splitlines()[0])
        assert first["decision"] == "send"
        assert first["max_output_tokens"] == first["remaining_tokens"] - first["estimated_input_tokens"]
    assert set(logs) == {"plan", "gather", "write", "review"} and logs["plan"][0]["content"] == "Plan the report requested in /commission/."
    assert logs["review"][0]["content"].startswith(f"Review section {section}. Save the review to /work/reviews/{section}.md.")
    assert "(unavailable: the verify phase saved nothing)" in logs["review"][0]["content"]
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
    model = ScriptedModel(responses=[reply.model_copy(update={"usage_metadata": dict(input_tokens=1000, output_tokens=n-1000, total_tokens=n)}) for reply, n in zip(replies, [20000, 20000, 10000, 10000])])
    monkeypatch.setattr(cli, "make_model", lambda phase=None: model)
    section_args = ["--section", "01-synthetic"] if command == "review" else []
    monkeypatch.setattr(sys, "argv", ["reportagent", command, str(tmp_path), "--max-tokens", "100000", *section_args])
    cli.main()
    messages = [json.loads(line) for line in next(phase_transcripts(tmp_path / "work/runs")).read_text().splitlines()]
    notes = [m for m in messages if m["type"] == "human" and m["content"].startswith("You have used")]
    assert len(notes) == 1 and notes[0]["content"].startswith("You have used 50000 of 100000 tokens.")
    assert messages[messages.index(notes[0]) - 1]["type"] == "tool" and (tmp_path / target.lstrip("/")).read_text() == plan


@pytest.mark.parametrize("matching", [False, True])
def test_named_run_requires_notes_file(tmp_path, monkeypatch, capsys, matching):
    notes = tmp_path / "work/notes"; notes.mkdir(parents=True)
    (notes / ("02-synthetic.md" if matching else "01-other.md")).touch()
    model = ScriptedModel(responses=[AIMessage(content="Done.") for _ in range(3)])
    monkeypatch.setattr(cli, "make_model", lambda phase=None: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--section", "02-synthetic", "--max-tokens", "100000"])
    cli.main()
    assert model.i == (2 if matching else 1)
    assert len(list(phase_transcripts(tmp_path / "work/runs"))) == (2 if matching else 1)
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
    monkeypatch.setattr(cli, "make_model", lambda phase=None: pytest.fail("check must not construct a model"))
    monkeypatch.setattr(sys, "argv", ["reportagent", "check", str(tmp_path), "--section", "demo"])
    cli.main()
    report = (tmp_path / "work/checks/demo.md").read_text().split("## Findings\n", 1)[1].split("\nFindings:", 1)[0].strip()
    assert "sections/demo.md:2" in report and "23" in report
    assert all(f"sections/demo.md:{n}:" in report for n in (3, 4, 5, 6, 9, 11, 12, 13))
    assert all(f"sections/demo.md:{n}:" not in report for n in (1, 7, 8, 10, 14))
    assert ("notes/demo.md:2" not in report) is complete
    assert all(f"notes/demo.md:{n}:" in report for n in (3, 4, 5, 8, 9))
    assert all(f"notes/demo.md:{n}:" not in report for n in (1, 6, 7))
    assert all(p.read_bytes() == data for p, data in before.items())
    assert all(line.strip() and ".md:" in line and " | " in line for line in report.splitlines())


def test_verified_citations_produce_zero_findings(tmp_path):
    source = tmp_path / "sources"; source.mkdir()
    (source / "guide.pdf").touch(); (source / "guide").mkdir()
    (source / "guide/page-001.txt").write_text("42.6 m")
    sections = tmp_path / "work/sections"; sections.mkdir(parents=True)
    (sections / "demo.md").write_text("Height ４２．６ m [guide.pdf, page 1].")
    notes = tmp_path / "work/notes"; notes.mkdir()
    (notes / "demo.md").touch()
    assert cli.check(tmp_path, "demo").read_text().endswith("Findings: total=0; shown=0; omitted=0\n")


def test_check_clarification_numbers_and_previous_citations(tmp_path):
    source = tmp_path / "sources"; source.mkdir()
    (source / "guide.pdf").touch(); (source / "guide").mkdir()
    (source / "guide/page-001.txt").write_text("42.6")
    (source / "guide/page-002.txt").write_text("88")
    sections = tmp_path / "work/sections"; sections.mkdir(parents=True)
    (sections / "demo.md").write_text("\n".join([
        "Height 42.6，见第79项 [guide.pdf, page 1]。",
        "Height 42.6, item 80 [同上]。",
        "Height 42.6，见第81、82项 [同上页]。",
        "Value 88, item 83 [同上, page 2]。",
        "Height 42.6 [guide.pdf, page 1；同上页]。",
        "Wrong 999，见第84项 [同上页]。",
        "Height 42.6 [guide.pdf, page 1] [澄清答复, item 93]。",
    ]))
    notes = tmp_path / "work/notes"; notes.mkdir()
    (notes / "demo.md").write_text("Read [guide.pdf, page 1]\n未见 [同上页]")
    report = cli.check(tmp_path, "demo").read_text()
    assert "number 999 not found" in report
    assert all(f"number {n} not found" not in report for n in (79, 80, 81, 82, 83, 84, 93))
    assert "source file not found" not in report
    assert "number 42.6 not found" not in report and "number 88 not found" not in report


def test_check_lists_sources_before_capped_findings(tmp_path):
    source = tmp_path / "sources"; source.mkdir()
    (source / "guide.pdf").touch(); (source / "guide").mkdir()
    (source / "guide/page-001.txt").write_text("42.6")
    sections = tmp_path / "work/sections"; sections.mkdir(parents=True)
    (sections / "demo.md").write_text("\n".join([
        "| X11 Height | 42.6 [guide.pdf, page 1] |",
        "| X12 Slope | unconfirmed |",
        "未列、未见且缺数据 [同上页]。",
        *["Wrong 999 [guide.pdf, page 1]。"] * 45,
    ]))
    notes = tmp_path / "work/notes"; notes.mkdir()
    (notes / "demo.md").write_text("未提供 [guide.pdf, page 1]")
    report = cli.check(tmp_path, "demo").read_text()
    top, rest = report.split("## Findings\n", 1)
    assert "indicator X11" in top and "[guide.pdf, page 001]" in top
    assert "indicator X12 | (no file/page citation)" in top
    assert all(f"absence {phrase}" in top for phrase in ("未列", "未见", "缺", "未提供"))
    findings = rest.split("\nFindings:", 1)[0].strip().splitlines()
    assert len(findings) == 40
    assert "Findings: total=46; shown=40; omitted=6" in rest



def test_export_generic_template_and_explicit_items(tmp_path):
    from zipfile import ZipFile
    from xml.etree import ElementTree as E
    from docx.shared import Mm, Pt
    from docx.enum.style import WD_STYLE_TYPE
    template = tmp_path / "commission/template"; template.mkdir(parents=True)
    ref = Document(); ref.styles["Normal"].font.size = Pt(12)
    ref.styles["Heading 1"].font.size = Pt(18)
    ref.styles["Caption"].font.size = Pt(10.5)
    ref.styles.add_style("Table Text", WD_STYLE_TYPE.PARAGRAPH).font.size = Pt(10.5)
    ref.sections[0].page_width = Mm(210); ref.sections[0].page_height = Mm(297)
    ref.sections[0].right_margin = Mm(15)
    ref.sections[0].header.paragraphs[0].text = "{{project}} | {{title}}"
    ref.save(template / "reference.docx")
    settings = "toc_title: Contents\nplaceholder: Not written\ncaption_prefixes: Table|Figure\ncitation_prefix: Source: \ncomment_author: Renderer\nitem_prefix: Item \nsheet_name: Questions\nsheet_columns: Id|Section|Location|Content|Sources|Status|Answer|Responder\nxlsx_file: questions.xlsx\nitem_status: Open\ntable_new_page_rows: 10\n"
    assert not any(word in settings for word in ("缺失", "待澄清", "目录", "来源"))
    (template / "settings.txt").write_text(settings)
    work = tmp_path / "work"; sections = work / "sections"; sections.mkdir(parents=True)
    (work / "plan.md").write_text("---\ntitle: Sample\nproject: Site\nsection: 1|01-overview\nsection: 1|03-method\nsection: 1|02-context\nsection: 1|04-analysis\nsection: 2|04-1-item\nsection: 1|05-conclusion\nsection: 1|06-followup\n---\n")
    (sections / "03-method.md").write_text("# Method\n\nMethod text.")
    content = "# Context\n\nFirst sentence. Second sentence with **bold**, *f*<sub>a0</sub>, Q<sup>ml</sup> and R*W*X. [design.pdf, page 1]\n\n[design.pdf, page 5]\n\n## Parameters\n\nTable 2.1-1 has explanatory text.\n\n**Table 2.1-1 Parameters**\n\n| Item | Value | Source |\n| :--- | :---: | ---: |\n| Height | **42.6** | *note [design.pdf, page 2]* |\n| a\\|b | _italic_ | first<br>second |\n| only |\n| | | |\n||tail\\|||\n"
    wide = "\n\nTable 2.2-1 Scores\n\n| Indicator | Band | Range | Score | Weight | Result | Evidence | Note |\n| --- | --- | --- | ---: | ---: | ---: | --- | --- |\n" + "".join(f"| X{i} | band | 50~74 | 65.4 | 0.17 | 11.12 | evidence | note |\n" for i in range(1, 12))
    (sections / "02-context.md").write_text(content + wide)
    (sections / "04-1-item.md").write_text("# Item\n\nItem text.")
    clar = work / "clarifications"; clar.mkdir()
    anchor = "Second sentence with bold, f_a0, Q^ml and R*W*X."
    (clar / "02-context.md").write_text(f"number: 7\nanchor: 「{anchor}」\ncontent: Parameters & deformation need confirmation.\nsources: design.pdf, page 3\n\n---\n\nnumber: 9\nanchor: 「Not present.」\ncontent: Location needs confirmation.\nsources:\n")
    (clar / "04-1-item.md").write_text("number: 31\nanchor: 「」\ncontent: Lithology uncertain.\nsources: design.pdf, page 4\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    doc = Document(cli.export_docx(tmp_path)); table = doc.tables[0]
    assert len(doc.tables) == 2 and table.cell(1, 1).text == "42.6"
    assert table.rows[0]._tr.xpath("./w:trPr/w:tblHeader") and table.autofit is False
    assert [c.paragraphs[0].alignment for c in table.rows[0].cells] == [0, 1, 2]
    assert any(r.bold for r in table.cell(1, 1).paragraphs[0].runs)
    assert table.cell(1, 2).text == "note " and any(r.italic for r in table.cell(1, 2).paragraphs[0].runs)
    assert [c.text for c in table.rows[2].cells] == ["a|b", "italic", "first\nsecond"]
    assert [c.text for c in table.rows[3].cells] == ["only", "", ""]
    assert [c.text for c in table.rows[4].cells] == ["", "", ""]
    assert [c.text for c in table.rows[5].cells] == ["", "tail|", ""]
    assert len(doc.tables[1].rows) == 12 and len(doc.tables[1].columns) == 8
    assert sum(c.width for c in doc.tables[1].columns) <= doc.sections[0].page_width - doc.sections[0].left_margin - doc.sections[0].right_margin + Mm(1)
    body = "\n".join(p.text for p in doc.paragraphs)
    assert body.index("Method") < body.index("Context") and body.count("Not written") == 3
    assert "Parameters & deformation" not in body and "[design.pdf" not in body
    assert "f_a0" in body and "Q^ml" in body and "R*W*X" in body
    assert len(doc.comments) == 6 and all(c.author == "Renderer" for c in doc.comments)
    assert doc.sections[0].header.paragraphs[0].text == "Site | Sample"
    assert doc.styles["Normal"].font.size == Pt(12) and doc.styles["Heading 1"].font.size == Pt(18)
    assert next(p for p in doc.paragraphs if p.text == "Table 2.1-1 Parameters").style.name == "Caption"
    assert next(p for p in doc.paragraphs if p.text == "Table 2.1-1 Parameters").paragraph_format.keep_with_next
    assert next(p for p in doc.paragraphs if p.text == "Table 2.2-1 Scores").paragraph_format.page_break_before
    assert not next(p for p in doc.paragraphs if p.text == "Table 2.1-1 Parameters").paragraph_format.page_break_before
    assert next(p for p in doc.paragraphs if p.text == "Table 2.1-1 has explanatory text.").style.name == "Normal"
    positions = {}
    for comment in doc.comments:
        active, text = False, []
        for para in doc.element.xpath(".//w:p"):
            for child in para:
                if child.tag.endswith("}commentRangeStart") and child.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}id") == str(comment.comment_id): active = True
                elif child.tag.endswith("}commentRangeEnd") and child.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}id") == str(comment.comment_id): active = False
                elif active: text.extend(t.text or "" for t in child.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"))
        positions[comment.text.split("\n", 1)[0]] = "".join(text)
    assert positions["Source: design.pdf, page 1"].strip() == anchor and positions["Source: design.pdf, page 5"].strip() == anchor
    assert positions["Item 7"] == anchor and positions["Item 9"] == "Context" and positions["Item 31"] == "Item"
    assert len(doc.element.xpath(".//w:commentRangeStart")) == len(doc.element.xpath(".//w:commentRangeEnd")) == 6
    assert any("TOC " in n.text for n in doc.element.xpath(".//w:instrText"))
    with ZipFile(tmp_path / "out/questions.xlsx") as z:
        assert z.testzip() is None
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet = E.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows = [[c.findtext("s:v", namespaces=ns) if c.get("t") == "n" else "".join(t.text or "" for t in c.findall(".//s:t", ns)) for c in row] for row in sheet.findall("s:sheetData/s:row", ns)]
    assert rows[0] == ["Id", "Section", "Location", "Content", "Sources", "Status", "Answer", "Responder"]
    assert [r[0] for r in rows[1:]] == ["7", "9", "31"]
    assert [r[2] for r in rows[1:]] == [anchor, "Context", "Item"]
    assert all(r[5:] == ["Open", "", ""] for r in rows[1:])
    assert len(rows) - 1 == sum(c.text.startswith("Item ") for c in doc.comments)
    assert all(p.read_bytes() == data for p, data in before.items())


def test_export_explicit_numbers_preserve_owner_input(tmp_path, monkeypatch):
    from zipfile import ZipFile
    from xml.etree import ElementTree as E
    work = tmp_path / "work"; sections = work / "sections"; sections.mkdir(parents=True)
    clar = work / "clarifications"; clar.mkdir()
    plan = work / "plan.md"
    plan.write_text("---\nsection: 1|a\nsection: 1|b\n---\n")
    for name in ("a", "b"): (sections / f"{name}.md").write_text(f"# {name}\n\nBody sentence.")
    a = clar / "a.md"; a.write_text("number: 11\nanchor: 「Body sentence.」\ncontent: First.\nsources:\n")
    (clar / "b.md").write_text("number: 30\nanchor: 「」\ncontent: Second.\nsources:\n")
    cli.export_docx(tmp_path); target = tmp_path / "out/items.xlsx"
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}; tag = "{" + ns["s"] + "}"
    with ZipFile(target) as z: parts = {n: z.read(n) for n in z.namelist()}
    sheet = E.fromstring(parts["xl/worksheets/sheet1.xml"]); first = sheet.find("s:sheetData/s:row[@r='2']", ns)
    for address, value in (("F2", "Answered"), ("G2", "=1+1"), ("H2", "Owner")):
        cell = first.find(f"s:c[@r='{address}']", ns); cell.clear(); cell.attrib.update(r=address, t="inlineStr")
        E.SubElement(E.SubElement(cell, tag + "is"), tag + "t").text = value
    parts["xl/worksheets/sheet1.xml"] = E.tostring(sheet)
    with ZipFile(target, "w") as z:
        for n, data in parts.items(): z.writestr(n, data)
    plan.write_text("---\nsection: 1|b\nsection: 1|a\n---\n")
    a.write_text(a.read_text().replace("First.", "First, clarified.") + "\n---\nnumber: 44\nanchor: 「」\ncontent: New.\nsources:\n")
    word = Document(cli.export_docx(tmp_path))
    with ZipFile(target) as z:
        sheet = E.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows = [[c.findtext("s:v", namespaces=ns) if c.get("t") == "n" else "".join(t.text or "" for t in c.findall(".//s:t", ns)) for c in row] for row in sheet.findall("s:sheetData/s:row", ns)]
        assert b"<f>" not in z.read("xl/worksheets/sheet1.xml")
    assert [r[0] for r in rows[1:]] == ["30", "11", "44"]
    assert rows[2][5:] == ["Answered", "=1+1", "Owner"] and rows[3][5:] == ["Open", "", ""]
    assert [c.text.split("\n", 1)[0] for c in word.comments] == ["Item 30", "Item 11", "Item 44"]
    previous = target.read_bytes(); a.write_text(a.read_text() + "\n---\nnumber: 55\nanchor: 「」\ncontent: Pending.\nsources:\n")
    replace = Path.replace
    def locked(self, destination):
        if Path(destination) == target: raise PermissionError("Workbook is open")
        return replace(self, destination)
    with monkeypatch.context() as context:
        context.setattr(Path, "replace", locked)
        with pytest.raises(PermissionError, match="Workbook is open"): cli.export_docx(tmp_path)
    assert target.read_bytes() == previous


def test_export_sizes_columns_and_merges_categories(tmp_path):
    from docx.shared import Mm, Pt
    sections = tmp_path / "work/sections"; sections.mkdir(parents=True)
    explanation = "说明文字，保持来源与取值依据。" * 12
    (sections / "demo.md").write_text(
        "# Table\n\n| 分类 | 指标 | 分级 | 基本分值范围 | 取值 | 权重 | 评估分值 | 说明 |\n"
        "| --- | --- | --- | --- | ---: | ---: | ---: | --- |\n"
        f"| X1 建设规模 [design.pdf, page 1] | X11 高度 | 条件一 | 50~74 | 65.4 | 0.17 | 11.12 | {explanation} |\n"
        f"| X1 建设规模 [design.pdf, page 1] | X12 坡率 | 条件二 | 25~49 | 30 | 0.11 | 3.30 | {explanation} [design.pdf, page 2] |\n"
        f"| X2 地质 | X21 岩性 | 条件三 | 75~100 | 75 | 0.14 | 10.50 | {explanation} |\n")
    doc = Document(cli.export_docx(tmp_path)); table = doc.tables[0]
    widths = [c.width for c in table.columns]
    assert widths[-1] > 3 * max(widths[4:7]) and len(set(widths)) > 1
    section = doc.sections[0]
    assert sum(widths) <= section.page_width - section.left_margin - section.right_margin + Mm(1)
    assert table.cell(1, 0)._tc is table.cell(2, 0)._tc
    assert table.cell(2, 0)._tc is not table.cell(3, 0)._tc
    xml_text = "".join(table._tbl.xpath(".//w:t/text()"))
    assert xml_text.count("X1 建设规模") == 1 and table.cell(1, 4).text == "65.4"
    assert len(doc.comments) == 3
    assert len(doc.element.xpath(".//w:commentRangeStart")) == len(doc.element.xpath(".//w:commentRangeEnd")) == 3
    assert table.rows[0]._tr.xpath("./w:trPr/w:tblHeader")
    assert doc.styles["Table Text"].font.size == Pt(10.5)
    source = sections / "demo.md"
    source.write_text(source.read_text() + "\n\n| 项目 | 空值 |\n| --- | --- |\n| A | / |\n| B | / |\n")
    assert Document(cli.export_docx(tmp_path)).tables[1].cell(1, 1).text == "/"


def test_export_client_fields_and_agent_order(tmp_path):
    from zipfile import ZipFile
    from xml.etree import ElementTree as E
    template = tmp_path / "commission/template"; template.mkdir(parents=True)
    (template / "settings.txt").write_text("xlsx_file: client.xlsx\nsheet_columns: Id|Section|Object|Question|Requested|Effect|Sources|Status|Answer|Responder\nsheet_fields: number|section|object|question|required|effect|sources|status|answer|responder\nsheet_widths: 8|28|42|70|60|70|64|14|60|22\nsheet_sort: rank\nsheet_sort_default: 4\nanswers_file: commission/answers.txt\nanswer_author: Client\n")
    (tmp_path / "commission/answers.txt").write_text("9: Confirmed value.\n")
    sections = tmp_path / "work/sections"; sections.mkdir(parents=True)
    (sections / "item.md").write_text("# Slope\n\nA body sentence.")
    clar = tmp_path / "work/clarifications"; clar.mkdir()
    text = ""
    for number, rank, effect in ((101, "3", "The word grade appears but this is descriptive."), (77, "", ""), (9, "2", "Score only."), (5, "1", "Grade may change.")):
        if text: text += "\n---\n"
        text += f"number: {number}\nanchor: 「A body sentence.」\ncontent: Internal process detail {number}.\nsources: Evidence\nobject: Client slope\nquestion: {'Please confirm the height.' if rank else ''}\nrequired: Drawing\neffect: {effect}\nrank: {rank}\n"
        if number == 5: text += "status: Confirmed\n"
    (clar / "item.md").write_text(text)
    word = Document(cli.export_docx(tmp_path))
    with ZipFile(tmp_path / "out/client.xlsx") as z:
        ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        sheet = E.fromstring(z.read("xl/worksheets/sheet1.xml"))
        rows = [[c.findtext("s:v", namespaces=ns) if c.get("t") == "n" else "".join(t.text or "" for t in c.findall(".//s:t", ns)) for c in row] for row in sheet.findall("s:sheetData/s:row", ns)]
        assert sheet.find("s:autoFilter", ns).get("ref") == "A1:J5"
    assert [r[0] for r in rows[1:]] == ["5", "9", "101", "77"]
    assert all(len(r) == 10 for r in rows) and all(r[2] == "Client slope" for r in rows[1:])
    assert rows[1][3] == "Please confirm the height." and rows[-1][3] == rows[-1][5] == ""
    assert rows[2][7:] == ["Open", "Confirmed value.", "Client"]
    assert rows[1][7] == "Confirmed"
    assert "Internal process" not in str(rows)
    assert len(word.comments) == len(rows) - 1 == 4
    assert all("Internal process detail" in c.text for c in word.comments)


@pytest.mark.parametrize("phase,configured,expected", [
    ("verify-sources", "audit-model", "audit-model"),
    ("review", "audit-model", "audit-model"),
    ("revise", "audit-model", "audit-model"),
    ("clarify", "audit-model", "audit-model"),
    ("write", "audit-model", "default-model"),
    ("gather", "audit-model", "default-model"),
    ("plan", "audit-model", "default-model"),
    ("review", "", "default-model"),
    ("review", None, "default-model"),
])
def test_review_phase_model_from_env(tmp_path, monkeypatch, phase, configured, expected):
    monkeypatch.chdir(tmp_path)
    for key, value in {"MODEL_NAME": "default-model", "OPENAI_BASE_URL": "https://llm.example/v1",
                       "OPENAI_API_KEY": "offline-key"}.items():
        monkeypatch.setenv(key, value)
    if configured is None:
        monkeypatch.delenv("REVIEW_MODEL_NAME", raising=False)
    else:
        monkeypatch.setenv("REVIEW_MODEL_NAME", configured)
    settings = {}
    monkeypatch.setattr(models, "init_chat_model", lambda **kwargs: settings.update(kwargs) or "model")
    assert models.make_model(phase) == "model"
    assert settings["model"] == expected
    assert settings["model_kwargs"]["parallel_tool_calls"] == (phase == "revise")
    assert settings["base_url"] == "https://llm.example/v1" and settings["api_key"] == "offline-key"


def test_cli_revision_uses_review_model_for_all_four_phases(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key, value in {"MODEL_NAME": "default-model", "REVIEW_MODEL_NAME": "audit-model",
                       "OPENAI_BASE_URL": "https://llm.example/v1", "OPENAI_API_KEY": "offline-key"}.items():
        monkeypatch.setenv(key, value)
    (tmp_path / "work/sections").mkdir(parents=True)
    (tmp_path / "work/sections/demo.md").write_text("Before")
    (tmp_path / "work/checks").mkdir()
    (tmp_path / "work/checks/demo.md").write_text("# Source pages to review\n\n## Findings\n")
    audit = ScriptedModel(responses=[
        call("write_file", file_path="/work/reviews/demo-sources.md", content="Verified by audit model"), AIMessage(content="Saved"),
        call("write_file", file_path="/work/reviews/demo.md", content="Reviewed by audit model"), AIMessage(content="Saved"),
        call("write_file", file_path="/work/clarifications/demo.md", content="Clarified by audit model"), AIMessage(content="Saved"),
        call("edit_file", file_path="/work/sections/demo.md", old_string="Before", new_string="Revised by audit model"), AIMessage(content="Saved"),
    ])
    audit.responses = [reply.model_copy(update={"usage_metadata": dict(input_tokens=1, output_tokens=0, total_tokens=1)}) for reply in audit.responses]
    selected = []
    def init_model(**kwargs):
        selected.append(kwargs["model"])
        return audit
    monkeypatch.setattr(models, "init_chat_model", init_model)
    configs = []
    original_build = cli.build_agent
    def build(*args, **kwargs):
        agent = original_build(*args, **kwargs)
        def stream(state, **kwargs):
            configs.append(kwargs["config"])
            assert kwargs["config"]["max_concurrency"] == 1
            return agent.stream(state, **kwargs)
        return SimpleNamespace(stream=stream, get_state=agent.get_state)
    monkeypatch.setattr(cli, "build_agent", build)
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--section", "demo", "--max-tokens", "300000"])
    cli.main()
    assert selected == ["audit-model"] * 4
    log = next((tmp_path / "work/runs").glob("*-verify-sources.jsonl"))
    assert [r["initial_request_input_tokens"] for r in map(json.loads, log.read_text().splitlines()) if "initial_request_input_tokens" in r] == [1]
    assert len(configs) == 4
    assert (tmp_path / "work/sections/demo.md").read_text() == "Revised by audit model"
    assert (tmp_path / "work/clarifications/demo.md").read_text() == "Clarified by audit model"
    assert (tmp_path / "work/reviews/demo-sources.md").read_text() == "Verified by audit model"
    assert (tmp_path / "work/reviews/demo.md").read_text() == "Reviewed by audit model"


def test_clarification_collisions_check_export_and_resolution(tmp_path):
    from xml.etree import ElementTree as ET
    from zipfile import ZipFile

    def rows():
        cli.export_docx(tmp_path)
        with ZipFile(tmp_path / "out/items.xlsx") as book:
            sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
        return [["".join(cell.itertext()) for cell in row] for row in sheet.findall(".//{*}row")[1:]]

    for folder in ("work/sections", "work/notes", "work/clarifications", "commission/template"):
        (tmp_path / folder).mkdir(parents=True)
    for name in ("a", "b"):
        (tmp_path / f"work/sections/{name}.md").write_text("# Title\n\nText.")
    (tmp_path / "work/notes/a.md").write_text("")
    (tmp_path / "commission/template/settings.txt").write_text(
        "answers_file: commission/澄清答复.md\nanswer_author: Owner\n")
    answers = tmp_path / "commission/澄清答复.md"
    answers.write_text("94: Scope answer\n29: Valid answer\n88: First\n88: Second\n")
    item = "number: {}\nanchor: 「Text.」\ncontent: Question\n"
    a = tmp_path / "work/clarifications/a.md"
    a.write_text("\n---\n".join(item.format(i) for i in (94, 29, 88)))
    b = tmp_path / "work/clarifications/b.md"
    b.write_text(item.format(94) + "status: 已确认\n")
    check = cli.check(tmp_path, "a").read_text()
    assert "duplicate clarification 94: /work/clarifications/a.md:1" in check
    assert "/work/clarifications/b.md:1" in check and "/commission/澄清答复.md:1" in check
    assert "duplicate clarification 88:" in check and "/commission/澄清答复.md:3" in check and "/commission/澄清答复.md:4" in check
    assert "duplicate clarification 29:" not in check
    for _ in range(2):
        data = rows()
        assert [r[-3:] for r in data if r[0] in ("94", "88")] == [["编号冲突", "", ""]] * 3
        assert next(r[-2:] for r in data if r[0] == "29") == ["Valid answer", "Owner"]
    pending = b.with_name("a.pending.md")
    b.rename(pending)
    collision = next(line for line in cli.check(tmp_path, "a").read_text().splitlines() if "duplicate clarification 94:" in line)
    assert "/work/clarifications/a.md:1" in collision and "/work/clarifications/a.pending.md:1" in collision
    archive = tmp_path / "work/archive"
    archive.mkdir()
    pending.rename(archive / pending.name)
    assert "duplicate clarification 94:" not in cli.check(tmp_path, "a").read_text()
    b.write_text(item.format(96))
    answers.write_text("94: Scope answer\n29: Valid answer\n88: Settled\n")
    data = rows()
    assert next(r[-3:] for r in data if r[0] == "94") == ["Open", "Scope answer", "Owner"]
    assert next(r[-3:] for r in data if r[0] == "96") == ["Open", "", ""]


@pytest.mark.parametrize("answer_id,expected,total,clarify,revise", [
    (90, 96, 100000, 60000, 220000), (101, 102, 300000, 60000, 220000),
])
def test_selected_revision_phases_and_cap(tmp_path, monkeypatch, answer_id, expected, total, clarify, revise):
    work = tmp_path / "work"
    for folder in ("clarifications", "reviews", "sections"):
        (work / folder).mkdir(parents=True)
    (work / "reviews/demo.md").write_text("Preserved review")
    (work / "sections/demo.md").write_text("Before")
    (work / "clarifications/retired.txt").write_text("number: 95\n")
    (tmp_path / "commission").mkdir()
    (tmp_path / "commission/澄清答复.md").write_text(f"{answer_id}: Answer\n")
    model = ScriptedModel(responses=[AIMessage(content="Done", usage_metadata={
        "input_tokens": 6, "output_tokens": 1, "total_tokens": 7})])
    limits = []
    original = cli.TokenCapMiddleware
    def cap(limit, **kwargs):
        limits.append((kwargs["phase"], limit))
        return original(limit, **kwargs)
    monkeypatch.setattr(cli, "make_model", lambda phase: model)
    monkeypatch.setattr(cli, "TokenCapMiddleware", cap)
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--section", "demo",
                        "--max-tokens", str(total), "--phases", "clarify,revise",
                        "--phase-cap", f"clarify={clarify}", "--phase-cap", f"revise={revise}"])
    cli.main()
    assert limits == [("clarify", clarify), ("revise", min(total - 7, revise))]
    logs = sorted(phase_transcripts(work / "runs"))
    assert len(logs) == 2
    first = json.loads(logs[0].read_text().splitlines()[0])
    assert first["type"] == "human" and f"Next free clarification number: {expected}" in first["content"]
    assert (work / "reviews/demo.md").read_text() == "Preserved review"


@pytest.mark.parametrize("options", [
    ["--budget-info"], ["--budget-guard"], ["--no-budget-guard"],
    ["--phases", "unknown"], ["--phases", "revise,revise"], ["--phases", ""],
    ["--phase-cap", "revise"], ["--phase-cap", "unknown=20"], ["--phase-cap", "revise=0"],
    ["--phase-cap", "review=20", "--phase-cap", "review=30"], ["--phases", "revise", "--phase-cap", "clarify=10"],
])
def test_invalid_revision_phase_options(tmp_path, monkeypatch, options):
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--section", "s",
                                     "--max-tokens", "100", *options])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2 and not (tmp_path / "work/runs").exists()


@pytest.mark.parametrize("has_source_check", [True, False])
def test_review_clarify_and_revise_receive_case_files(tmp_path, monkeypatch, has_source_check):
    flag = "clarification 43 line 8: F/grade 65.91/—; section states 56.49/III"
    paths = {"work/checks/demo.md": "## Verification comparison\n" + flag + "\nNon-score finding",
             "work/checks/other.md": flag.replace("43 line", "108 line"),
             "work/reviews/demo-sources.md": "Target source check", "work/reviews/other-sources.md": "Unrelated evidence",
             "work/sections/demo.md": "# Section\n\n第1段\n第2段",
             "work/reviews/demo.md": "Review finding", "work/notes/demo.md": "Working notes",
             "work/clarifications/demo.md": "number: 94\ncontent: Design question",
             "work/clarifications/demo.pending.md": "number: 96\ncontent: Proposed question",
             "work/clarifications/method.md": "number: 2\ncontent: Other\n---\nnumber: 94\ncontent: Scope question\n  continuation\nstatus: Open\n---\nnumber: 3\ncontent: Other",
             "commission/澄清答复.md": "29: Earlier answer\n94: Owner scope answer"}
    if not has_source_check:
        del paths["work/reviews/demo-sources.md"]
    for rel, text in paths.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    model = ScriptedModel(responses=[AIMessage(content="Done", usage_metadata={
        "input_tokens": 1, "output_tokens": 0, "total_tokens": 1})])
    monkeypatch.setattr(cli, "make_model", lambda phase: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--section", "demo",
                                     "--max-tokens", "100000", "--phases", "review,clarify,revise"])
    cli.main()
    review, clarify, revise = [json.loads(f.read_text().splitlines()[0])["content"]
                       for f in sorted(phase_transcripts(tmp_path / "work/runs"))]
    for rel in ("work/reviews/demo.md", "work/sections/demo.md", "work/clarifications/demo.md",
                "work/clarifications/demo.pending.md", "commission/澄清答复.md"):
        assert rel in clarify and paths[rel] in clarify
    assert flag in clarify and flag not in review and flag not in revise
    assert "Non-score finding" not in clarify and paths["work/checks/other.md"] not in clarify
    first = cli._revision_messages(tmp_path, "demo", "clarify")[0]
    assert "work/checks/demo.md" not in first.response_metadata["supplied_files"]
    assert "Next free clarification number: 97" in clarify
    assert "/work/clarifications/method.md:4" in clarify
    assert "number: 94\ncontent: Scope question\n  continuation\nstatus: Open" in clarify
    assert "content: Other" not in clarify and "/commission/澄清答复.md:2" in clarify
    for rel in ("work/reviews/demo.md", "work/clarifications/demo.md", "work/notes/demo.md"):
        assert rel in revise and paths[rel] in revise
    assert "1\t# Section\n2\t\n3\t第1段\n4\t第2段" in revise
    assert "Proposed question" not in revise
    assert paths["commission/澄清答复.md"] in revise
    evidence = paths.get("work/reviews/demo-sources.md", "(unavailable: the verify phase saved nothing)")
    assert f"Case file: /work/reviews/demo-sources.md\n{evidence}" in review
    for rel in ("work/clarifications/demo.md", "commission/澄清答复.md", "work/notes/demo.md"):
        assert paths[rel] in review
    assert "3\t第1段" in review and "Unrelated evidence" not in review


@pytest.mark.parametrize("middle,expected", [("B", "Alpha\nBeta\nGamma"), ("absent", "Alpha\nB\nGamma")])
def test_batched_revision_dispatches_native_edits_in_order(tmp_path, monkeypatch, middle, expected):
    from deepagents.backends import FilesystemBackend
    from reportagent.budget import TokenCapMiddleware

    path = tmp_path / "section.md"
    path.write_text("A\nB\nC")
    edits = [dict(name="edit_file", id=f"edit-{i}", args=dict(file_path="/section.md", old_string=a, new_string=b))
             for i, (a, b) in enumerate([("A", "Alpha"), (middle, "Beta"), ("C", "Gamma")])]
    first = call("ls", path="/").model_copy(update={"usage_metadata": dict(input_tokens=30, output_tokens=0, total_tokens=30)})
    batch = AIMessage(content="", tool_calls=edits, usage_metadata=dict(input_tokens=6, output_tokens=1, total_tokens=7))
    order = []
    original = FilesystemBackend.edit
    def record(self, file_path, old_string, new_string, replace_all=False):
        order.append(old_string)
        return original(self, file_path, old_string, new_string, replace_all)
    monkeypatch.setattr(FilesystemBackend, "edit", record)
    model = ScriptedModel(responses=[first, batch])
    agent = cli.build_agent(tmp_path, model, phase="revise", middleware=[TokenCapMiddleware(100, phase="revise")])
    state = agent.invoke({"messages": [("user", "Revise the section")]}, config={"max_concurrency": 1})
    assert order == ["A", middle, "C"] and path.read_text() == expected
    messages = state["messages"]
    assert [len(m.tool_calls) for m in messages if m.type == "ai" and m.usage_metadata] == [1, 3]
    assert state["total_tokens"] == 37
    results = [m for m in messages if m.type == "tool" and m.name == "edit_file"]
    assert [m.tool_call_id for m in results] == ["edit-0", "edit-1", "edit-2"]
    assert "Successfully" in results[0].content and "Successfully" in results[2].content
    assert ("Successfully" in results[1].content) == (middle == "B")


def test_resume_between_revision_edits_preserves_batch(tmp_path, monkeypatch):
    from langgraph.checkpoint.memory import InMemorySaver
    from reportagent.budget import TokenCapMiddleware

    target = tmp_path / "section.md"
    target.write_text("one\ntwo")
    changes = [dict(name="edit_file", id=old, args=dict(file_path="/section.md", old_string=old, new_string=new))
               for old, new in (("one", "first"), ("two", "second"))]
    cost = dict(input_tokens=3, output_tokens=1, total_tokens=4)
    model = ScriptedModel(responses=[AIMessage(content="", tool_calls=changes, usage_metadata=cost),
                                     AIMessage(content="Done", usage_metadata=cost)])
    received = []
    generate = ScriptedModel._generate
    def record(self, messages, **kwargs):
        received.append(messages)
        return generate(self, messages, **kwargs)
    monkeypatch.setattr(ScriptedModel, "_generate", record)
    graph = cli.build_agent(tmp_path, model, phase="revise", checkpointer=InMemorySaver(),
                            middleware=[TokenCapMiddleware(1000, phase="revise")])
    config = {"configurable": {"thread_id": "batch"}, "max_concurrency": 1}
    graph.invoke({"messages": [("user", "Revise")]}, config, interrupt_after=["tools"], durability="sync")
    assert target.read_text() == "first\ntwo"
    final = graph.invoke(None, config, durability="sync")
    assert final["total_tokens"] == 8 and target.read_text() == "first\nsecond"
    assert len(received) == 2
    assert [[c["id"] for c in m.tool_calls] for m in received[1] if m.type == "ai"] == [["one", "two"]]
    assert [m.tool_call_id for m in received[1] if m.type == "tool"] == ["one", "two"]


@pytest.mark.parametrize("phase,write", [("clarify", "write_file"), ("revise", "edit_file")])
@pytest.mark.parametrize("policy", ["restricted", "force-first"])
def test_phase_tool_policy(tmp_path, monkeypatch, phase, write, policy):
    seen = []
    def bind(self, tools, **kwargs):
        seen.append(({t.name for t in tools}, kwargs))
        return self.bind(tools=tools, **kwargs)
    monkeypatch.setattr(ScriptedModel, "bind_tools", bind)
    if policy == "force-first":
        monkeypatch.setitem(models.PHASE_SETTINGS[phase], "tool_policy", policy)
    model = ScriptedModel(responses=[call("ls", path="/"), AIMessage(content="done")])
    state = cli.build_agent(tmp_path, model, phase=phase).invoke({"messages": [("human", "Work.")]})
    assert len(seen) == 2
    if policy == "restricted":
        assert all(names == {"read_file", write} for names, _ in seen)
        assert any(m.type == "tool" and m.status == "error" for m in state["messages"])
    else:
        assert "ls" in seen[0][0] and "ls" in seen[1][0]
        assert seen[0][1]["tool_choice"] == {"type": "function", "function": {"name": write}}
        assert not seen[1][1].get("tool_choice")


def test_score_arithmetic_only_reports_baseline_discrepancies(tmp_path, monkeypatch, capsys):
    folder = tmp_path / "work/sections"
    folder.mkdir(parents=True)
    path = folder / "demo.md"
    text = """| 指标 | 基本分值 Rij | 权重 γij | 评估分值 Xij |
|---|---|---|---|
| X11 | 74 | 0.5 | 37.00 |
| X21 | 68.5 | 0.5 | 34.25 |

## 风险计算与等级判定
*F* = Σ*X* = 30（X11）+32.21（X21）=**62.21**
敏感性核查：
若地下水位上升，F=53.00，风险等级为Ⅲ级。
## 评估结论
总体风险值 F=71.24，风险等级为Ⅲ级，若降雨则 F=53.00。
"""
    path.write_text(text)
    monkeypatch.setattr(sys, "argv", ["reportagent", "check", str(tmp_path), "--score-arithmetic"])
    cli.main()
    output = capsys.readouterr().out
    assert "71.25/IV" in output and "62.21" in output and "III" in output
    assert "53.00" not in output and "stated 30/" not in output and "line 9" not in output
    assert path.read_text() == text and list((tmp_path / "work").iterdir()) == [folder]
    notes = tmp_path / "work/notes"
    notes.mkdir()
    (notes / "demo.md").write_text("")
    original = cli.check(tmp_path, "demo").read_bytes()
    assert "71.25/IV" in capsys.readouterr().out
    monkeypatch.setitem(models.PHASE_SETTINGS["checks"], "score_arithmetic", False)
    assert cli.check(tmp_path, "demo").read_bytes() == original and not capsys.readouterr().out



@pytest.mark.parametrize("score,grade", [("30", "I"), ("45", "II"), ("60", "III"), ("60.02", "IV")])
def test_arithmetic_thresholds_and_exact_tolerance(tmp_path, score, grade):
    source = tmp_path / "risk.md"
    section = f"| 指标 | 基本分值 Rij | 权重 | 评估分值 |\n|---|---|---|---|\n| X11 | {score} | 1 | 0 |\n\n## 评估结论\n"
    source.write_text(section + f"F={Decimal(score)+Decimal('0.01')}，风险等级为{grade}级。")
    assert not list(cli._score_arithmetic(source))
    source.write_text(section + f"F={Decimal(score)+Decimal('0.02')}，风险等级为{grade}级。")
    mismatch, = cli._score_arithmetic(source)
    assert f"/{grade};" in mismatch
    source.write_text(section.replace(f"| {score} | 1 |", "| 未定 | 1 |") + "F=12")
    assert "unavailable" in next(cli._score_arithmetic(source))


FIRST_COPY = "This file is already in your first message; use that copy."
READ_SCOPE = "This phase may read /sources/ pages (find them in /index.md) and save its own file."


def test_custom_first_message_does_not_claim_file_provenance(tmp_path):
    (tmp_path / "notes.md").write_text("Unrelated text")
    model = ScriptedModel(responses=[call("read_file", file_path="/notes.md"),
                                     call("write_file", file_path="/out.md", content="Saved"), AIMessage(content="done")])
    result = cli.build_agent(tmp_path, model, phase="clarify").invoke(
        {"messages": [("human", "Case file: /notes.md\nUnrelated text")]})
    reply, save = [m for m in result["messages"] if m.type == "tool"]
    assert (reply.status, reply.content) == ("error", READ_SCOPE)
    assert save.status == "success" and (tmp_path / "out.md").read_text() == "Saved"


@pytest.mark.parametrize("phase,full_check", [
    ("verify-sources", True), ("verify-sources", False), ("review", False), ("clarify", False), ("revise", False),
])
def test_restricted_file_results_match_actual_inputs(tmp_path, monkeypatch, phase, full_check):
    work = tmp_path / "work"
    check = "work/checks/demo.md"
    review = "work/reviews/demo.md"
    sources = "work/reviews/demo-sources.md"
    section = "work/sections/demo.md"
    other = "work/clarifications/other.md"
    paths = {"index.md": "Source page index", "near/index.md": "Lookalike", "sources/page-001.txt": "Page content",
             check: "Checklist" + ("" if full_check else "\n## Findings\nDiagnostic omitted"), section: "Original",
             review: "Review", sources: "Verified", "work/clarifications/demo.md": "number: 2\ncontent: Official",
             other: "number: 2\ncontent: Excerpt\n---\nnumber: 4\ncontent: Extra"}
    for name, content in paths.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    supplied = {"verify-sources": check, "review": sources, "clarify": review, "revise": section}[phase]
    read = lambda name: call("read_file", file_path="/" + name)
    blocked = [read(supplied), read(other), read("near/index.md"), read("work/notes/demo.md"),
               read("sources/../near/index.md"), read("sources/../" + supplied)]
    expected = [("error", READ_SCOPE)] * len(blocked)
    if phase != "verify-sources" or full_check:
        expected[0] = ("error", FIRST_COPY)
    if phase in ("verify-sources", "review"):
        wrong = review if phase == "verify-sources" else sources
        blocked += [call("write_file", file_path="/" + name, content="forbidden") for name in (wrong, "work/reviews/other.md")]
        expected.extend([("error", READ_SCOPE)] * 2)
    target = {"verify-sources": sources, "review": review, "clarify": "work/clarifications/demo.md", "revise": section}[phase]
    save = (call("edit_file", file_path="/" + target, old_string="Original", new_string="Saved") if phase == "revise"
            else call("write_file", file_path="/" + target, content="Saved"))
    replies = [*blocked, read("index.md"), read("sources/page-001.txt"), save, AIMessage(content="done")]
    usage = {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}
    model = ScriptedModel(responses=[m.model_copy(update={"usage_metadata": usage}) for m in replies])
    monkeypatch.setattr(cli, "make_model", lambda _: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--section", "demo",
                                     "--phases", phase, "--phase-cap", f"{phase}=100000", "--max-tokens", "100000"])
    cli.main()
    log, = phase_transcripts(work / "runs")
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    tools = [m for m in messages if m["type"] == "tool"]
    assert [(m["status"], m["content"]) for m in tools[:len(blocked)]] == expected
    assert "Source page index" in tools[-3]["content"] and "Page content" in tools[-2]["content"]
    assert (tmp_path / target).read_text() == "Saved" and not (work / "reviews/other.md").exists()
    if phase == "clarify":
        assert "content: Excerpt" in messages[0]["content"] and "content: Extra" not in messages[0]["content"]
    if phase in ("verify-sources", "review"):
        assert (tmp_path / wrong).read_text() == paths[wrong]


@pytest.mark.parametrize("conditional_first", [False, True])
def test_arithmetic_mixed_conditional_and_adopted_baseline(tmp_path, conditional_first):
    path = tmp_path / "section.md"
    table = "| 指标 | 基本分值 Rij | 权重 |\n|---|---|---|\n| X11 | 64.59 | 1 |\n\n## 结论\n"
    conditional = "（四项全取下限时 F＝50.59、仍为Ⅲ级）"
    for score in ("64.59", "68.59"):
        adopted = f"总体风险算术基准值 F＝{score}，对应Ⅳ级"
        path.write_text(table + "；".join((conditional, adopted) if conditional_first else (adopted, conditional)) + "。")
        findings = list(cli._score_arithmetic(path))
        if not conditional_first:
            path.write_text(table + f"（{adopted}，若四项全取下限时 F＝50.59、仍为Ⅲ级）。")
            assert list(cli._score_arithmetic(path)) == findings
        if score == "64.59":
            assert not findings
        else:
            mismatch, = findings
            assert "68.59" in mismatch and "50.59" not in mismatch


@pytest.mark.parametrize("scenario", ["若边坡加高", "组合情形", "取较大高度时", "叠加情形"])
def test_conditional_score_chain_in_checks(tmp_path, scenario, capsys):
    work = tmp_path / "work"
    for kind in ("sections", "notes", "clarifications"):
        (work / kind).mkdir(parents=True)
    (work / "notes/demo.md").write_text("")
    (work / "sections/demo.md").write_text(
        "| 指标 | 基本分值 Rij | 权重 |\n|---|---|---|\n| X11 | 56.49 | 1 |\n\n## 结论\n"
        f"{scenario}：F=56.49+4.62+4.80=65.91，Ⅳ级；本坡 F=30.00+26.00=56.00，Ⅲ级。\n")
    (work / "clarifications/demo.md").write_text("number: 43\neffect: 组合情形 F=65.91，Ⅳ级。\n"
                                                "number: 108\neffect: 基准值 F=65.91，Ⅳ级。")
    check = cli.check(tmp_path, "demo").read_text()
    assert "clarification 43 line" not in check and "clarification 108 line" in check
    arithmetic = capsys.readouterr().out
    assert "56.49/III" in arithmetic and "56.00" in arithmetic and "65.91" not in arithmetic
