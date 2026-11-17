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


def test_cli_run(tmp_path, monkeypatch, capsys):
    (tmp_path / "sources").mkdir()
    (tmp_path / "commission.md").write_text("Write the hydrology section.")
    copyfile(Path(__file__).parents[1] / "fixtures/one-page.pdf", tmp_path / "sources/one-page.pdf")
    sentence = "The site drains toward the eastern channel."
    replies = [call("read_file", file_path="/sources/one-page/page-001.txt"),
               call("write_file", file_path="/work/sections/01-hydrology.md", content=f"# Hydrology\n\n{sentence} [one-page.pdf, page 1]"),
               AIMessage(content="Done.")]
    model = ScriptedModel(responses=[reply.model_copy(update={"usage_metadata": dict(input_tokens=1, output_tokens=0, total_tokens=1)}) for reply in replies])
    made = []
    monkeypatch.setattr(cli, "make_model", lambda: made.append(True) or model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path)])
    with pytest.raises(SystemExit) as missing:
        cli.main()
    assert missing.value.code == 2 and not made
    for args in (("ingest", str(tmp_path)), ("run", str(tmp_path), "--max-tokens", "100"), ("export", str(tmp_path))):
        monkeypatch.setattr(sys, "argv", ["reportagent", *args])
        cli.main()
    assert len(made) == 1 and (tmp_path / "work/sections/01-hydrology.md").exists()
    assert sentence in "\n".join(p.text for p in Document(tmp_path / "out/report.docx").paragraphs)
    transcript = [json.loads(line) for line in next((tmp_path / "work/runs").glob("*.jsonl")).read_text().splitlines()]
    assert [message["type"] for message in transcript] == ["human", "ai", "tool", "ai", "tool", "ai"]
    assert sentence in transcript[2]["content"] and transcript[-1]["content"] == "Done."
    assert "3" in capsys.readouterr().out


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
    model = ScriptedModel(responses=[call("read_file", file_path="/commission.md") for _ in range(201)])
    monkeypatch.setattr(cli, "make_model", lambda: model)
    monkeypatch.setattr(sys, "argv", ["reportagent", "run", str(tmp_path), "--max-tokens", "100000"])
    cli.main()
    transcript = [json.loads(line) for line in next((tmp_path / "work/runs").glob("*.jsonl")).read_text().splitlines()]
    assert model.i == 200
    assert "200" in transcript[-1]["content"] and "missing" in transcript[-1]["content"].lower()
    assert sum((message.get("usage_metadata") or {}).get("total_tokens", 0) for message in transcript) == 0
