from tempfile import TemporaryDirectory
from uuid import uuid4

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import before_model
from langchain_core.messages import AIMessage, ToolMessage

from fake_tool_calls import ScriptedModel, call, report


def check():
    marker = uuid4().hex
    reads = []

    @before_model
    def observe_child(state, runtime):
        reads.extend(m.content for m in state["messages"] if isinstance(m, ToolMessage) and m.name == "read_file")

    reader = ScriptedModel(responses=[call("read_file", file_path="/shared.txt"), AIMessage(content="Read it.")])
    parent = ScriptedModel(responses=[
        call("write_file", file_path="/shared.txt", content=marker),
        call("task", subagent_type="reader", description="Read /shared.txt."),
        AIMessage(content="Done."),
    ])
    with TemporaryDirectory() as temporary:
        agent = create_deep_agent(model=parent, backend=FilesystemBackend(root_dir=temporary, virtual_mode=True),
                                  subagents=[{"name": "reader", "description": "Read files", "system_prompt": "Read the file.",
                                              "model": reader, "middleware": [observe_child]}])
        state = agent.invoke({"messages": [("user", "Write the file and ask the reader to inspect it.")]})
    assert any(m.name == "task" and m.status == "success" for m in state["messages"] if isinstance(m, ToolMessage))
    assert any(marker in content for content in reads), "reader did not observe the parent's marker"
    return "The delegated reader's read_file tool result contained the random marker written by the parent."


if __name__ == "__main__":
    report(check)
