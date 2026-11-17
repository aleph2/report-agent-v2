from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.messages import AIMessage, ToolMessage

from tests.fake_model import ScriptedModel, call


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
