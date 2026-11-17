import json
from pathlib import Path
from shutil import copyfile
import sys
from types import SimpleNamespace

from docx import Document
from langchain_core.messages import AIMessage
import pytest

from reportagent import cli, models
from tests.fake_model import ScriptedModel, call


@pytest.mark.parametrize("cap,gathered,written", [(100, 3, 4), (6, 3, 3), (3, 3, 0), (2, 2, 0)])
def test_cli_run(tmp_path, monkeypatch, capsys, cap, gathered, written):
    (tmp_path / "sources").mkdir()
    (tmp_path / "commission.md").write_text("Write the hydrology section.")
    copyfile(Path(__file__).parents[1] / "fixtures/one-page.pdf", tmp_path / "sources/one-page.pdf")
    sentence = "The site drains toward the eastern channel."
    notes = f"{sentence} [one-page.pdf, page 1]\nMISSING: Drainage dimensions."
    section = f"# Hydrology\n\n{sentence} [one-page.pdf, page 1]\n\n## 缺失与待补充事项\n\nDrainage dimensions."
    replies = [call("read_file", file_path="/sources/one-page/page-001.txt"),
               call("write_file", file_path="/work/notes/hydrology.md", content=notes), AIMessage(content="Evidence saved."),
               call("read_file", file_path="/work/notes/hydrology.md"),
               call("write_file", file_path="/work/sections/01-hydrology.md", content=section),
               call("read_file", file_path="/work/sections/01-hydrology.md"), AIMessage(content="Done.")]
    model = ScriptedModel(responses=[reply.model_copy(update={"usage_metadata": dict(input_tokens=1, output_tokens=0, total_tokens=1)}) for reply in replies])
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
    assert len(made) == 1 and limits == [cap, max(0, cap - gathered)]
    assert (tmp_path / "work/notes/hydrology.md").read_text() == notes
    logs = sorted((tmp_path / "work/runs").glob("*.jsonl"))
    assert len(logs) == 2
    research, draft = [[json.loads(line) for line in log.read_text().splitlines()] for log in logs]
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
        assert sentence in "\n".join(p.text for p in Document(tmp_path / "out/report.docx").paragraphs)
    else:
        assert len(draft) == 2 and "cap=0" in draft[-1]["content"]
        assert not (tmp_path / "work/sections").exists()


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
