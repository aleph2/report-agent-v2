import base64
import json
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
    (tmp_path / "commission.md").write_text("Write the hydrology section.")
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
    assert (tmp_path / "sources/index.md").read_text() == "# Sources\n\n## one-page.pdf\n\nPages: 1\n\nPage paths: sources/one-page/page-001.txt ... page-001.txt\nPage images: sources/one-page/page-001.png ... page-001.png\n\n"
    with Image.open(tmp_path / "sources/one-page/page-001.png") as image:
        assert image.format == "PNG" and max(image.size) == 1600
    assert len(made) == 1 and limits == [int(cap * 0.6), max(0, cap - gathered)]
    if len(gather_costs) > 1:
        assert (tmp_path / "work/notes/hydrology.md").read_text() == notes
    logs = sorted((tmp_path / "work/runs").glob("*.jsonl"))
    assert len(logs) == 2
    research, draft = [[json.loads(line) for line in log.read_text().splitlines()] for log in logs]
    assert sentence in research[2]["content"]
    assert all(m["status"] == "success" for log in (research, draft) for m in log if m["type"] == "tool")
    assert research[0]["content"] == "Gather evidence for the section requested in /commission.md."
    assert draft[0]["content"] == "Write the section requested in /commission.md from the notes in /work/notes/."
    assert [m["content"] for m in draft if m["type"] == "human"] == [draft[0]["content"]]
    assert [sum((m.get("usage_metadata") or {}).get("total_tokens", 0) for m in log) for log in (research, draft)] == [gathered, written]
    assert f"Reported tokens: {gathered + written}" in capsys.readouterr().out
    if written:
        assert draft[1]["tool_calls"][0]["args"]["file_path"] == "/work/notes/hydrology.md" and notes in draft[2]["content"]
        assert (tmp_path / "work/sections/01-hydrology.md").read_text() == section
        monkeypatch.setattr(sys, "argv", ["reportagent", "export", str(tmp_path)])
        cli.main()
        paragraphs = Document(tmp_path / "out/report.docx").paragraphs
        assert [(p.text, p.style.name) for p in paragraphs[:3]] == [("Hydrology", "Heading 1"), ("Drainage", "Heading 2"), ("Direction", "Heading 3")]
        assert sentence in "\n".join(p.text for p in paragraphs)
    else:
        assert len(draft) == 2 and "cap=0" in draft[-1]["content"]
        assert not (tmp_path / "work/sections").exists()


def test_cli_write_only(tmp_path, monkeypatch):
    (tmp_path / "commission.md").write_text("Write the hydrology section.")
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
    assert transcript[0]["content"] == "Write the section requested in /commission.md from the notes in /work/notes/."
    assert notes in transcript[2]["content"] and transcript[-1]["content"] == "Done."
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
    (tmp_path / "commission.md").write_text("Read the commission.")
    model = ScriptedModel(responses=[call("read_file", file_path="/commission.md") for _ in range(401)])
    monkeypatch.setattr(cli, "make_model", lambda: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--max-tokens", "100000"])
    cli.main()
    logs = list((tmp_path / "work/runs").glob("*.jsonl"))
    assert model.i == 400 and len(logs) == 2
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
    assert set(logs) == {"plan", "gather", "write"} and logs["plan"][0]["content"] == "Plan the report requested in /commission.md."
    for phase, folder in (("gather", "notes"), ("write", "sections")):
        prompt = logs[phase][0]["content"]
        assert section in prompt and f"## {section}" in prompt and "/work/plan.md" in prompt and f"/work/{folder}/{section}.md" in prompt
