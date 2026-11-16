from pathlib import Path
from tempfile import TemporaryDirectory

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from fake_tool_calls import ScriptedModel, call, report


def check():
    config = {"configurable": {"thread_id": "interrupt-on"}}
    model = ScriptedModel(responses=[call("write_file", file_path="/approved.txt", content="approved"),
                                     AIMessage(content="Done.")])
    with TemporaryDirectory() as temporary, SqliteSaver.from_conn_string(str(Path(temporary) / "state.db")) as saver:
        target = Path(temporary) / "approved.txt"
        agent = create_deep_agent(model=model, backend=FilesystemBackend(root_dir=temporary, virtual_mode=True),
                                  interrupt_on={"write_file": True}, checkpointer=saver)
        paused = agent.invoke({"messages": [("user", "Write the approved file.")]}, config)
        assert paused["__interrupt__"][0].value["action_requests"][0]["name"] == "write_file"
        assert not target.exists()
        state = agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}), config)
        assert target.read_text() == "approved" and state["messages"][-1].content == "Done."
    return "write_file paused with its target absent. After approval the file contained the requested text."


if __name__ == "__main__":
    report(check)
