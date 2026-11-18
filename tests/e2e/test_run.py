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


@pytest.mark.parametrize("cap,gather_costs,written", [(100, [1, 1, 1], 4), (6, [1, 1, 1], 3), (4, [1, 1], 2), (3, [3], 0)])
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
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path)])
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
        assert draft[0]["content"] == "Write the section requested in /commission/ from the notes in /work/notes/."
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


def test_cli_write_only(tmp_path, monkeypatch):
    (tmp_path / "commission").mkdir(exist_ok=True)
    (tmp_path / "commission/brief.md").write_text("Write the hydrology section.")
    notes = "The site drains east. [one-page.pdf, page 1]"
    (tmp_path / "work/notes").mkdir(parents=True)
    (tmp_path / "work/notes/hydrology.md").write_text(notes)
    section = f"# Hydrology\n\n{notes}"
    replies = [call("read_file", file_path="/work/notes/hydrology.md"),
               call("write_file", file_path="/work/sections/01-hydrology.md", content=section), AIMessage(content="Done.")]
    model = ScriptedModel(responses=[m.model_copy(update={"usage_metadata": dict(input_tokens=1, output_tokens=0, total_tokens=1)}) for m in replies])
    monkeypatch.setattr(cli, "make_model", lambda: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--max-tokens", "3", "--write-only"])
    cli.main()
    logs = list((tmp_path / "work/runs").glob("*.jsonl"))
    assert len(logs) == 1 and logs[0].name.endswith("-write.jsonl")
    transcript = [json.loads(line) for line in logs[0].read_text().splitlines()]
    assert transcript[0]["content"] == "Write the section requested in /commission/ from the notes in /work/notes/."
    assert notes in transcript[2]["content"] and transcript[-2]["content"] == "Done." and "2 * previous call" in transcript[-1]["content"]
    assert (tmp_path / "work/sections/01-hydrology.md").read_text() == section
    assert (tmp_path / "work/notes/hydrology.md").read_text() == notes


def test_model_from_current_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in ("MODEL_NAME", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text("MODEL_NAME=env-model\nOPENAI_BASE_URL=https://llm.example/v1\nOPENAI_API_KEY=offline-key\n")
    settings = {}
    monkeypatch.setattr(models, "init_chat_model", lambda **kwargs: settings.update(kwargs) or "model")
    assert models.make_model() == "model"
    assert settings == dict(model="env-model", model_provider="openai", base_url="https://llm.example/v1", api_key="offline-key", temperature=0.0)
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
    outputs = {"/work/plan.md": f"## {section}\nDrainage. [guide.pdf, page 1]\n", f"/work/notes/{section}.md": "Evidence.", f"/work/sections/{section}.md": "# Hydrology"}
    model = ScriptedModel(responses=[reply for path, text in outputs.items() for reply in (call("write_file", file_path=path, content=text), AIMessage(content="Saved."))])
    monkeypatch.setattr(cli, "make_model", lambda: model)
    for command in (["plan", str(tmp_path), "--max-tokens", "10"], ["run", str(tmp_path), "--max-tokens", "10", "--section", section]):
        monkeypatch.setattr(sys, "argv", ["reportagent", *command])
        cli.main()
    assert {path: (tmp_path / path.lstrip("/")).read_text() for path in outputs} == outputs
    logs = {p.stem.rsplit("-", 1)[1]: [json.loads(line) for line in p.read_text().splitlines()] for p in (tmp_path / "work/runs").glob("*.jsonl")}
    assert set(logs) == {"plan", "gather", "write"} and logs["plan"][0]["content"] == "Plan the report requested in /commission/."
    for phase, folder in (("gather", "notes"), ("write", "sections")):
        prompt = logs[phase][0]["content"]
        assert section in prompt and f"## {section}" in prompt and "/work/plan.md" in prompt and f"/work/{folder}/{section}.md" in prompt



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



def test_plan_receives_half_budget_note(tmp_path, monkeypatch):
    (tmp_path / "commission").mkdir()
    (tmp_path / "commission/brief.md").write_text("Plan one synthetic section.")
    plan = "## 01-synthetic\nSynthetic guidance."
    replies = [call("read_file", file_path="/commission/brief.md"), call("write_file", file_path="/work/plan.md", content=plan), AIMessage(content="Saved.")]
    model = ScriptedModel(responses=[reply.model_copy(update={"usage_metadata": dict(input_tokens=n, output_tokens=0, total_tokens=n)}) for reply, n in zip(replies, [3, 2, 1])])
    monkeypatch.setattr(cli, "make_model", lambda: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "plan", str(tmp_path), "--max-tokens", "10"])
    cli.main()
    messages = [json.loads(line) for line in next((tmp_path / "work/runs").glob("*.jsonl")).read_text().splitlines()]
    notes = [m for m in messages if m["type"] == "human" and m["content"].startswith("You have used")]
    assert len(notes) == 1 and notes[0]["content"].startswith("You have used 5 of 10 tokens.")
    assert messages[messages.index(notes[0]) - 1]["type"] == "tool" and (tmp_path / "work/plan.md").read_text() == plan


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
