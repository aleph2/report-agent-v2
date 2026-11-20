import json

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
               call("read_file", file_path="/commission/brief.md"),
               call("write_file", file_path="/pending.txt", content="Tool executed."), AIMessage(content="Done.")]
    model = ScriptedModel(responses=[reply.model_copy(update={"usage_metadata": dict(input_tokens=tokens, output_tokens=0, total_tokens=tokens)})
                                     for reply, tokens in zip(replies, [2, 2, 2, 1])])
    result = build_agent(tmp_path, model, middleware=[TokenCapMiddleware(12)]).invoke({"messages": [("user", "Write the section.")]})
    notes = [(i, m) for i, m in enumerate(result["messages"]) if isinstance(m, HumanMessage) and m.text.startswith("You have used")]
    assert len(notes) == 1 and notes[0][0] == 7
    assert notes[0][1].text == "You have used 6 of 12 tokens. Save your work now from what you have; mark anything missing as missing."
    pending = result["messages"][notes[0][0] - 1]
    assert pending.type == "tool" and pending.tool_call_id == "write_file" and pending.status == "success"
    assert (tmp_path / "pending.txt").read_text() == "Tool executed."
    assert sum(isinstance(m, AIMessage) for m in result["messages"]) == 4 and result["total_tokens"] == 7 and result["budget_note_sent"]


@pytest.mark.parametrize("costs,limit,calls,used,saved,writing", [
    ([2, 3, 4], 10, 3, 9, True, True),
    ([1, 2, 3], 6, 3, 6, True, True),
    ([12, 3, 3], 10, 1, 12, False, True),
    ([None, 6, 3], 10, 3, 9, True, True),
    ([3, 6, 12], 12, 3, 21, True, True),
    ([1, 2, 3], 8, 3, 6, True, False),
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
        assert "3 * previous call" in result["messages"][-1].text
    else:
        assert "reported total >= cap" in result["messages"][-1].text


@pytest.mark.parametrize("info,guard", [(True, False), (False, True), (True, True)])
def test_preflight_budget_at_provider_boundary(tmp_path, info, guard):
    import httpx
    from langchain_openai import ChatOpenAI
    from deepagents import register_harness_profile
    from reportagent.models import RUN_PROFILE
    register_harness_profile("openai:offline", RUN_PROFILE)

    log = tmp_path / "requests.jsonl"
    cap = TokenCapMiddleware(60_000, phase="revise", info=info, guard=guard, request_log=log)
    payloads = []

    def reply(request):
        body = json.loads(request.content)
        entry = json.loads(log.read_text().splitlines()[-1])
        remaining = 1_000 if payloads else 60_000
        assert entry["decision"] == "send" and entry["remaining_tokens"] == remaining
        assert entry["estimated_input_tokens"] > 100  # tools and system prompt dwarf the user text
        limit = body.get("max_completion_tokens", body.get("max_tokens"))
        assert limit == entry["max_output_tokens"]
        if guard:
            assert 0 < limit <= remaining - entry["estimated_input_tokens"]
        else:
            assert limit == remaining
        payloads.append(body)
        if len(payloads) == 1:
            message = {"role": "assistant", "content": None, "tool_calls": [
                {"id": "list", "type": "function", "function": {"name": "ls", "arguments": '{"path":"/"}'}}
            ]}
            tokens = 59_000
        else:
            message = {"role": "assistant", "content": "Done."}
            tokens = 10
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 1, "model": "offline",
            "choices": [{"index": 0, "finish_reason": "tool_calls" if "tool_calls" in message else "stop", "message": message}],
            "usage": {"prompt_tokens": tokens - 1, "completion_tokens": 1, "total_tokens": tokens},
        })

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        model = ChatOpenAI(model="offline", api_key="offline", base_url="https://offline.invalid/v1",
                           http_client=client, max_retries=0)
        result = build_agent(tmp_path, model, phase="revise", middleware=[cap]).invoke(
            {"messages": [("user", "Revise.")]}
        )
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [entry["remaining_tokens"] for entry in entries] == [60_000, 1_000]
    if guard:
        assert len(payloads) == 1 and result["total_tokens"] == 59_000
        assert entries[-1]["decision"] == "stop" and "cap" in result["messages"][-1].text.lower()
    else:
        assert len(payloads) == 2 and result["total_tokens"] == 59_010
        assert "1000" in payloads[-1]["messages"][-1]["content"].replace(",", "")


def test_cli_guard_stops_oversized_first_input(tmp_path, monkeypatch, capsys):
    import sys
    from reportagent import cli

    section = tmp_path / "work/sections/left.md"
    section.parent.mkdir(parents=True)
    section.write_text("stratum " * 3_000)
    model = ScriptedModel(responses=[AIMessage(content="Unused.")])
    monkeypatch.setattr(cli, "make_model", lambda phase: model)
    monkeypatch.setattr(ScriptedModel, "_generate", lambda *args, **kwargs: pytest.fail("Guard sent oversized input"))
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--section", "left",
                                     "--phases", "revise", "--phase-cap", "revise=40000",
                                     "--max-tokens", "20000", "--budget-guard"])
    cli.main()
    sidecar, = (tmp_path / "work/runs").glob("*-revise.requests.jsonl")
    entry = json.loads(sidecar.read_text())
    assert entry["decision"] == "stop" and entry["remaining_tokens"] == 20_000
    assert entry["estimated_input_tokens"] > 20_000
    assert "Reported tokens: 0" in capsys.readouterr().out


def test_estimate_includes_system_tools_and_image(tmp_path):
    from langchain.agents import create_agent
    from langchain_core.tools import StructuredTool

    def estimate(system, description, blocks):
        tool = StructuredTool.from_function(lambda: "ok", name="example", description=description)
        log = tmp_path / "estimate.jsonl"
        cap = TokenCapMiddleware(60_000, info=True, request_log=log)
        agent = create_agent(ScriptedModel(responses=[AIMessage(content="Done.")]), tools=[tool],
                             system_prompt=system, middleware=[cap])
        agent.invoke({"messages": [HumanMessage(content=blocks)]})
        return json.loads(log.read_text().splitlines()[-1])["estimated_input_tokens"]

    text = [{"type": "text", "text": "One claim."}]
    base = estimate("Review.", "Read.", text)
    assert estimate("Review. " * 500, "Read.", text) > base + 100
    assert estimate("Review.", "Read. " * 500, text) > base + 100
    image_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nm1cAAAAASUVORK5CYII="
    assert estimate("Review.", "Read.", [*text, {"type": "image_url", "image_url": {"url": image_url}}]) > base
