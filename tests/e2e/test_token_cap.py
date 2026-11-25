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
    cap = TokenCapMiddleware(12)
    cap.output_reserve = 0  # This fixture uses single-digit token counts.
    result = build_agent(tmp_path, model, middleware=[cap]).invoke({"messages": [("user", "Write the section.")]})
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
    cap = TokenCapMiddleware(limit)
    cap.output_reserve = 1  # One token reserved for the scripted write.
    result = build_agent(tmp_path, model, middleware=[cap]).invoke({"messages": [("user", "Read the case.")]})
    assert model.i == calls and result["total_tokens"] == used
    notes = [m for m in result["messages"] if isinstance(m, HumanMessage) and m.text == "This is the last response this phase can make; save your file now. Save your work now in one write; do not read anything more."]
    assert len(notes) == int(saved) and (tmp_path / "saved.md").exists() is (saved and writing)
    if saved:
        if writing:
            assert (tmp_path / "saved.md").read_text() == "Saved." and result["messages"][-2].type == "tool" and result["messages"][-2].status == "success"
        assert "previous input + output reserve" in result["messages"][-1].text
    else:
        assert "reported total >= cap" in result["messages"][-1].text


@pytest.mark.parametrize("info,guard", [(True, False), (False, True), (True, True)])
def test_preflight_budget_at_provider_boundary(tmp_path, monkeypatch, info, guard):
    import httpx
    from functools import partial
    from reportagent import models
    model_name = "qwen4-max" if guard else "offline"
    monkeypatch.setenv("REVIEW_MODEL_NAME", model_name)
    monkeypatch.setenv("THINKING_BUDGET_MODEL", "qwen4-max")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://offline.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "offline")

    log = tmp_path / "requests.jsonl"
    cap = TokenCapMiddleware(60_000, phase="revise", info=info, guard=guard, request_log=log)
    payloads = []

    def reply(request):
        body = json.loads(request.content)
        assert body["model"] == model_name
        assert body.get("thinking_budget") == (6000 if model_name == "qwen4-max" else None)
        assert any("Submit your edits in your first response" in str(m["content"])
                   for m in body["messages"] if m["role"] in ("system", "developer"))
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
            "usage": {"prompt_tokens": tokens - 1, "completion_tokens": 1, "completion_tokens_details": {"reasoning_tokens": 1}, "total_tokens": tokens},
        })

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        monkeypatch.setattr(models, "init_chat_model", partial(models.init_chat_model, http_client=client))
        model = models.make_model("revise")
        result = build_agent(tmp_path, model, phase="revise", middleware=[cap]).invoke(
            {"messages": [("user", "Revise.")]}
        )
    records = [json.loads(line) for line in log.read_text().splitlines()]
    responses = [row for row in records if row.get("event") == "response"]
    assert [row["request"] for row in responses] == list(range(1, len(payloads) + 1))
    assert all(row["reasoning_tokens"] == 1 for row in responses)
    entries = [row for row in records if "decision" in row]
    assert all(row["output_reserve"] == 24_000 for row in entries)
    assert [entry["remaining_tokens"] for entry in entries] == [60_000, 1_000]
    assert responses[0]["input_tokens"] <= entries[1]["estimated_input_tokens"] < 60_000
    if guard:
        assert len(payloads) == 1 and result["total_tokens"] == 59_000
        assert entries[-1]["decision"] == "stop" and "cap" in result["messages"][-1].text.lower()
    else:
        assert len(payloads) == 2 and result["total_tokens"] == 59_010
        assert payloads[-1]["messages"][-1]["content"].endswith("Remaining allowance: 1000 tokens.")
        assert [t["function"]["name"] for t in payloads[-1]["tools"]] == ["edit_file"]


@pytest.mark.parametrize("words", [10, 3000])
def test_cli_guard_stops_oversized_first_input(tmp_path, monkeypatch, capsys, words):
    import sys
    from reportagent import cli

    section = tmp_path / "work/sections/left.md"
    section.parent.mkdir(parents=True)
    section.write_text("stratum " * words)
    model = ScriptedModel(responses=[AIMessage(content="Unused.")])
    monkeypatch.setattr(cli, "make_model", lambda phase: model)
    monkeypatch.setattr(ScriptedModel, "_generate", lambda *args, **kwargs: pytest.fail("Guard sent oversized input"))
    monkeypatch.setattr(sys, "argv", ["reportagent", "revise", str(tmp_path), "--section", "left",
                                     "--phases", "revise", "--phase-cap", "revise=40000",
                                     "--max-tokens", "20000"])
    cli.main()
    sidecar, = (tmp_path / "work/runs").glob("*-revise.requests.jsonl")
    entry = json.loads(sidecar.read_text())
    assert entry["decision"] == "stop" and entry["remaining_tokens"] == 20_000
    assert (entry["estimated_input_tokens"] > 20_000) == (words == 3000)
    assert entry["estimated_input_tokens"] + entry["output_reserve"] > entry["remaining_tokens"]
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
        return next(row["estimated_input_tokens"] for line in reversed(log.read_text().splitlines())
                    if "decision" in (row := json.loads(line)))

    text = [{"type": "text", "text": "One claim."}]
    base = estimate("Review.", "Read.", text)
    assert estimate("Review. " * 500, "Read.", text) > base + 100
    assert estimate("Review.", "Read. " * 500, text) > base + 100
    image_url = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+nm1cAAAAASUVORK5CYII="
    assert estimate("Review.", "Read.", [*text, {"type": "image_url", "image_url": {"url": image_url}}]) > base


@pytest.mark.parametrize("phase,expected,reserve", [
    ("verify-sources", 6000, 24000), ("review", 8000, 16000), ("clarify", 4000, 24000),
    ("revise", 6000, 24000), ("write", None, 8000),
])
def test_phase_thinking_budget_opt_in(monkeypatch, phase, expected, reserve):
    from reportagent import models
    monkeypatch.setenv("MODEL_NAME", "draft")
    monkeypatch.setenv("REVIEW_MODEL_NAME", "review-model")
    monkeypatch.setenv("THINKING_BUDGET_MODEL", "review-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://offline.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "offline")
    monkeypatch.setattr(models, "init_chat_model", lambda **options: options)
    assert models.make_model(phase).get("extra_body", {}).get("thinking_budget") == expected
    assert TokenCapMiddleware(100000, phase=phase).output_reserve == reserve
    monkeypatch.setenv("REVIEW_MODEL_NAME", "other")
    assert "extra_body" not in models.make_model(phase)


def test_provider_anchor_prices_only_new_image_tiles(tmp_path):
    from base64 import b64encode
    from io import BytesIO
    from types import SimpleNamespace
    from PIL import Image
    from langchain.agents.middleware import ModelRequest

    data = BytesIO()
    Image.new("RGB", (1120, 1600)).save(data, format="PNG")
    block = {"type": "image", "mime_type": "image/png", "base64": b64encode(data.getvalue()).decode()}
    cap = TokenCapMiddleware(100000, phase="revise", guard=True, request_log=tmp_path / "calls")
    q = HumanMessage("Review this page.")
    request = ModelRequest(model=SimpleNamespace(model_name="qwen4-max"), messages=[q], tools=[], state={"total_tokens": 0})
    cap._prepare(request)
    reply = AIMessage(content="Read", usage_metadata={"input_tokens": 20000, "output_tokens": 10, "total_tokens": 20010})
    cap.after_model({"messages": [reply]}, None)
    added = HumanMessage(content=[block, block, block])
    cap._prepare(request.override(messages=[q, reply, added], state={"total_tokens": 20010}))
    rows = [json.loads(l) for l in (tmp_path / "calls").read_text().splitlines()]
    assert rows[-1]["decision"] == "send"
    assert 25256 <= rows[-1]["estimated_input_tokens"] < 25300
    # A different prefix cannot inherit the old provider count.
    cap._prepare(request.override(messages=[HumanMessage(content=[block])]))
    assert 1752 <= json.loads((tmp_path / "calls").read_text().splitlines()[-1])["estimated_input_tokens"] < 1770


def test_unavailable_image_uses_detail_ceiling():
    from reportagent.budget import _image_tokens

    remote = {"type": "image_url", "image_url": {"url": "https://example.invalid/page.png", "detail": "low"}}
    assert _image_tokens(remote, 32) == 2562
    assert _image_tokens({"type": "image", "base64": "invalid"}, 32) == 16386


@pytest.mark.parametrize("phase", [None, "verify-sources", "review", "revise", "clarify"])
def test_oversized_pdf_returns_page_route_and_allows_next_read(tmp_path, phase):
    from pathlib import Path

    sources = tmp_path / "sources"
    pages = sources / "guide"
    pages.mkdir(parents=True)
    fixture = Path(__file__).parents[1] / "fixtures/one-page.pdf"
    (sources / "guide.pdf").write_bytes(fixture.read_bytes().replace(b"%%EOF", b"%" + b"0" * 160000 + b"\n%%EOF"))
    (pages / "page-001.txt").write_text("Evidence from page one.")
    model = ScriptedModel(responses=[
        call("read_file", file_path="/sources/guide.pdf"),
        call("read_file", file_path="/sources/guide/page-001.txt"),
        AIMessage(content="Finished."),
    ])
    cap = TokenCapMiddleware(100000, phase=phase, guard=True)
    state = build_agent(tmp_path, model, phase=phase, middleware=[cap]).invoke({"messages": [("user", "Read the guide.")]})
    results = [msg for msg in state["messages"] if msg.type == "tool"]
    assert len(results) == 2 and state["messages"][-1].text.startswith("Finished.")
    notice = results[0].content
    assert isinstance(notice, str) and len(notice) < 600
    assert all(word in notice for word in ("/sources/guide.pdf", "1 page", "tokens", "/sources/guide/page-001.txt"))
    assert "Evidence from page one." in results[1].content


def test_async_read_retries_a_text_file_with_a_line_window(tmp_path):
    import asyncio

    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "record.txt").write_text(("field observation " * 10 + "\n") * 500)
    model = ScriptedModel(responses=[
        call("read_file", file_path="/sources/record.txt"),
        call("read_file", file_path="/sources/record.txt", offset=1, limit=1),
        AIMessage(content="Done."),
    ])
    cap = TokenCapMiddleware(30000, phase="clarify", guard=True)
    graph = build_agent(tmp_path, model, phase="clarify", middleware=[cap])
    state = asyncio.run(graph.ainvoke({"messages": [("user", "Read one observation.")]}))
    full, window = [m.content for m in state["messages"] if m.type == "tool"]
    assert "estimated" in full and "offset" in full and "limit" in full
    assert "field observation" in window and "field observation" not in full


# At 130k the first read leaves 102k: enough for two 41k requests, below the 123k research threshold.
@pytest.mark.parametrize("limit,calculation_output,can_save", [(130000, 1000, True), (103000, 1000, True), (103000, 30000, False)])
def test_rescore_final_calculation_can_feed_one_budgeted_save(tmp_path, monkeypatch, limit, calculation_output, can_save):
    section = tmp_path / "work/sections/A.table.md"
    section.parent.mkdir(parents=True)
    (tmp_path / "index.md").write_text("Evidence")
    read = call("read_file", file_path="/index.md")
    calculate = call("calculate_score", scores={"X12": 50}, weights={"X12": 1}, include_products=True)
    write = call("write_file", file_path="/work/sections/A.table.md", content="F=50.00")
    replies = []
    for item, inputs, outputs in [(read, 25000, 3000), (calculate, 26000, calculation_output), (write, 26000, 15)]:
        replies.append(item.model_copy(update={"usage_metadata": dict(input_tokens=inputs, output_tokens=outputs,
                                                                      total_tokens=inputs+outputs)}))
    offered, seen = [], []
    generate = ScriptedModel._generate

    def capture(self, messages, **kwargs):
        offered.append({t.name for t in kwargs["tools"]})
        seen.append(messages)
        return generate(self, messages, **kwargs)

    monkeypatch.setattr(ScriptedModel, "_generate", capture)
    log = tmp_path / "requests.jsonl"
    model = ScriptedModel(responses=replies)
    cap = TokenCapMiddleware(limit, phase="rescore", guard=True, request_log=log)
    state = build_agent(tmp_path, model, phase="rescore", section="A", middleware=[cap]).invoke(
        {"messages": [("user", "Rescore section A.")]})
    assert offered[1] == {"calculate_score", "write_file", "edit_file"}
    result, = [m for m in state["messages"] if m.type == "tool" and m.name == "calculate_score"]
    assert json.loads(result.text)["base"]["F"] == "50.00"
    decisions = [row["decision"] for line in log.read_text().splitlines() if "decision" in (row := json.loads(line))]
    if can_save:
        assert offered[2:] == [{"write_file", "edit_file"}]
        assert result in seen[2]
        assert decisions == ["send", "send", "send"] and section.read_text() == "F=50.00"
    else:
        assert len(offered) == 2 and decisions == ["send", "send", "stop"]
        assert not section.exists()
    assert state["total_tokens"] == 54000 + calculation_output + (26015 if can_save else 0)
