from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage
import pytest

from reportagent.agent import build_agent
from reportagent.budget import TokenCapMiddleware
from tests.fake_model import ScriptedModel, call


def test_token_cap(tmp_path, monkeypatch):
    (tmp_path / "commission").mkdir(exist_ok=True)
    (tmp_path / "commission/brief.md").write_text("Write one section.")
    monkeypatch.setattr(SummarizationMiddleware, "wrap_model_call", lambda *args, **kwargs: pytest.fail("Automatic summarization bypasses the main token counter."))
    original = ScriptedModel.bind_tools
    def bind(self, tools, **kwargs):
        assert "task" not in [getattr(tool, "name", None) for tool in tools]
        return original(self, tools, **kwargs)
    monkeypatch.setattr(ScriptedModel, "bind_tools", bind)
    model = ScriptedModel(responses=[
        call("read_file", file_path="/commission/brief.md").model_copy(update={"usage_metadata": dict(input_tokens=3, output_tokens=1, total_tokens=4)}),
        call("write_file", file_path="/work/sections/at-cap.md", content="Saved at cap.").model_copy(update={"usage_metadata": dict(input_tokens=5, output_tokens=1, total_tokens=6)}),
        AIMessage(content="This response must remain unused."),
    ])
    result = build_agent(tmp_path, model, middleware=[TokenCapMiddleware(10), ModelCallLimitMiddleware(run_limit=200)]).invoke({"messages": [("user", "Write the section.")]})
    assert model.i == 2 and result["total_tokens"] == 10
    assert "cap" in result["messages"][-1].text.lower() and "10" in result["messages"][-1].text and "reported total=10" in result["messages"][-1].text
    assert (tmp_path / "work/sections/at-cap.md").read_text() == "Saved at cap."


@pytest.mark.parametrize("cap", [0, -1])
def test_no_call_when_budget_is_already_exhausted(tmp_path, cap):
    model = ScriptedModel(responses=[AIMessage(content="unused")])
    result = build_agent(tmp_path, model, middleware=[TokenCapMiddleware(cap)]).invoke({"messages": [("user", "Write the section.")]})
    assert model.i == 0 and result["total_tokens"] == 0


def test_half_budget_note_preserves_pending_tools(tmp_path):
    (tmp_path / "commission").mkdir(exist_ok=True)
    (tmp_path / "commission/brief.md").write_text("Write one section.")
    replies = [call("read_file", file_path="/commission/brief.md"),
               call("write_file", file_path="/pending.txt", content="Tool executed."),
               call("read_file", file_path="/pending.txt"), AIMessage(content="Done.")]
    model = ScriptedModel(responses=[reply.model_copy(update={"usage_metadata": dict(input_tokens=tokens, output_tokens=0, total_tokens=tokens)})
                                     for reply, tokens in zip(replies, [4, 2, 1, 1])])
    result = build_agent(tmp_path, model, middleware=[TokenCapMiddleware(12)]).invoke({"messages": [("user", "Write the section.")]})
    notes = [(i, m) for i, m in enumerate(result["messages"]) if isinstance(m, HumanMessage) and m.text.startswith("You have used")]
    assert len(notes) == 1 and notes[0][0] == 5
    assert notes[0][1].text == "You have used 6 of 12 tokens. Save your work now from what you have; mark anything missing as missing."
    pending = result["messages"][notes[0][0] - 1]
    assert pending.type == "tool" and pending.tool_call_id == "write_file" and pending.status == "success"
    assert (tmp_path / "pending.txt").read_text() == "Tool executed."
    assert sum(isinstance(m, AIMessage) for m in result["messages"]) == 4 and result["total_tokens"] == 8 and result["budget_note_sent"]


@pytest.mark.parametrize("costs,limit,calls,used,saved,writing", [
    ([2, 3, 4], 10, 3, 9, True, True),
    ([2, 2, 2], 6, 3, 6, True, True),
    ([12, 3, 3], 10, 1, 12, False, True),
    ([None, 6, 3], 10, 3, 9, True, True),
    ([3, 6, 12], 10, 3, 21, True, True),
    ([2, 3, 4], 10, 3, 9, True, False),
])
def test_final_save_turn(tmp_path, costs, limit, calls, used, saved, writing):
    replies = [call("ls", path="/"), call("ls", path="/"), call("write_file", file_path="/saved.md", content="Saved.") if writing else AIMessage(content="No tool call.")]
    replies = [reply.model_copy(update={"usage_metadata": dict(input_tokens=n, output_tokens=0, total_tokens=n) if n is not None else None}) for reply, n in zip(replies, costs)]
    model = ScriptedModel(responses=[*replies, AIMessage(content="Unused.")])
    result = build_agent(tmp_path, model, middleware=[TokenCapMiddleware(limit)]).invoke({"messages": [("user", "Read the case.")]})
    assert model.i == calls and result["total_tokens"] == used
    notes = [m for m in result["messages"] if isinstance(m, HumanMessage) and m.text == "Token budget reached. Save your work now in one write; do not read anything more."]
    assert len(notes) == int(saved) and (tmp_path / "saved.md").exists() is (saved and writing)
    if saved:
        if writing:
            assert (tmp_path / "saved.md").read_text() == "Saved." and result["messages"][-2].type == "tool" and result["messages"][-2].status == "success"
        assert "2 * previous call" in result["messages"][-1].text
    else:
        assert "reported total >= cap" in result["messages"][-1].text
