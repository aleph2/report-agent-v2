from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage
import pytest

from reportagent.agent import build_agent
from reportagent.budget import TokenCapMiddleware
from tests.fake_model import ScriptedModel, call


def test_token_cap(tmp_path, monkeypatch):
    (tmp_path / "commission.md").write_text("Write one section.")
    monkeypatch.setattr(SummarizationMiddleware, "wrap_model_call", lambda *args, **kwargs: pytest.fail("Automatic summarization bypasses the main token counter."))
    original = ScriptedModel.bind_tools
    def bind(self, tools, **kwargs):
        assert "task" not in [getattr(tool, "name", None) for tool in tools]
        return original(self, tools, **kwargs)
    monkeypatch.setattr(ScriptedModel, "bind_tools", bind)
    model = ScriptedModel(responses=[
        call("read_file", file_path="/commission.md").model_copy(update={"usage_metadata": dict(input_tokens=6, output_tokens=1, total_tokens=7)}),
        call("write_file", file_path="/work/sections/unused.md", content="unused").model_copy(update={"usage_metadata": dict(input_tokens=7, output_tokens=1, total_tokens=8)}),
        AIMessage(content="This response must remain unused."),
    ])
    result = build_agent(tmp_path, model, middleware=[TokenCapMiddleware(10), ModelCallLimitMiddleware(run_limit=200)]).invoke({"messages": [("user", "Write the section.")]})
    assert model.i == 2 and result["total_tokens"] == 15
    assert "cap" in result["messages"][-1].text.lower() and "10" in result["messages"][-1].text and "15" in result["messages"][-1].text
    assert not (tmp_path / "work/sections/unused.md").exists()


@pytest.mark.parametrize("cap", [0, -1])
def test_no_call_when_budget_is_already_exhausted(tmp_path, cap):
    model = ScriptedModel(responses=[AIMessage(content="unused")])
    result = build_agent(tmp_path, model, middleware=[TokenCapMiddleware(cap)]).invoke({"messages": [("user", "Write the section.")]})
    assert model.i == 0 and result["total_tokens"] == 0
