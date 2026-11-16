from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.messages import AIMessage, ToolMessage

from fake_tool_calls import ScriptedModel, call


def test_hello(tmp_path):
    model = ScriptedModel(responses=[
        call("write_file", file_path="/hello.txt", content="Hello from the agent.").model_copy(
            update={"content": "I will write the greeting in hello.txt, then stop."}
        ),
        AIMessage(content="Done."),
    ])
    agent = create_deep_agent(model=model, backend=FilesystemBackend(root_dir=tmp_path, virtual_mode=True))
    result = agent.invoke({"messages": [("user", "Plan, write one greeting, and stop.")]})

    tools = [(m.name, m.status) for m in result["messages"] if isinstance(m, ToolMessage)]
    assert tools == [("write_file", "success")]
    assert result["messages"][1].content == "I will write the greeting in hello.txt, then stop."
    assert result["messages"][-1].content == "Done."
    assert list(tmp_path.iterdir()) == [tmp_path / "hello.txt"]
    assert (tmp_path / "hello.txt").read_text() == "Hello from the agent."


def test_research_model_uses_env(monkeypatch):
    import json
    from pathlib import Path
    import runpy
    import sys
    from types import ModuleType

    import httpx
    import langchain.chat_models
    from langchain_core.messages import convert_to_openai_messages
    from langchain_core.tools import Tool

    monkeypatch.setenv("MODEL_NAME", "qwen-env-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://bailian.example/compatible-mode/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-key")
    # Web search is outside this model-configuration test.
    tools = ModuleType("research_agent.tools")
    tools.tavily_search = Tool(name="tavily_search", description="Offline search", func=lambda _: "")
    tools.think_tool = Tool(name="think_tool", description="Offline thought", func=lambda _: "")
    monkeypatch.setitem(sys.modules, tools.__name__, tools)
    requests = []
    replies = iter(convert_to_openai_messages([
        call("write_file", file_path="/hello.txt", content="Hello from Bailian."),
        AIMessage(content="Done."),
    ]))

    def reply(request):
        requests.append(request)
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0, "model": "qwen-env-test",
            "choices": [{"index": 0, "message": next(replies), "finish_reason": "stop"}],
        })

    initialize = langchain.chat_models.init_chat_model

    def connect(**kwargs):
        assert kwargs["model"] == "qwen-env-test"
        return initialize(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(reply)))

    monkeypatch.setattr(langchain.chat_models, "init_chat_model", connect)
    example = Path(__file__).resolve().parents[2] / "spike/deep_research"
    monkeypatch.syspath_prepend(str(example))
    agent = runpy.run_path(str(example / "agent.py"))["agent"]
    result = agent.invoke({"messages": [("user", "Write one greeting, and stop.")]})
    assert result["files"]["/hello.txt"]["content"] == "Hello from Bailian."
    assert result["messages"][-1].content == "Done."
    assert len(requests) == 2
    assert all(str(request.url) == "https://bailian.example/compatible-mode/v1/chat/completions"
               and json.loads(request.content)["model"] == "qwen-env-test"
               and request.headers["authorization"] == "Bearer offline-key" for request in requests)
