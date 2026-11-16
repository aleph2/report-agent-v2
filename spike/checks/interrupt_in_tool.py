from pathlib import Path
from tempfile import TemporaryDirectory

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command, interrupt

from fake_tool_calls import ScriptedModel, call, report


@tool
def ask_owner() -> str:
    """Ask the owner for an answer."""
    return interrupt("Which answer?")


def check():
    model = ScriptedModel(responses=[call("ask_owner"), AIMessage(content="Done.")])
    config = {"configurable": {"thread_id": "interrupt-in-tool"}}
    with TemporaryDirectory() as temporary, SqliteSaver.from_conn_string(str(Path(temporary) / "state.db")) as saver:
        agent = create_deep_agent(model=model, tools=[ask_owner], checkpointer=saver)
        paused = agent.invoke({"messages": [("user", "Ask the owner.")]}, config)
        assert paused["__interrupt__"][0].value == "Which answer?"
        completed = agent.invoke(Command(resume="owner answer"), config)
        assert any(m.name == "ask_owner" and m.content == "owner answer"
                   for m in completed["messages"] if isinstance(m, ToolMessage))
        assert completed["messages"][-1].content == "Done."
    return "ask_owner paused inside the tool. Resuming the SQLite thread returned the owner's answer to the agent."


if __name__ == "__main__":
    report(check)
