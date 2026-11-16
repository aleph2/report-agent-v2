from pathlib import Path
from tempfile import TemporaryDirectory

from deepagents import create_deep_agent
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.store.sqlite import SqliteStore

from fake_tool_calls import ScriptedModel, report


def check():
    config = {"configurable": {"thread_id": "ground-check"}}
    with TemporaryDirectory() as temporary:
        database = str(Path(temporary) / "case.sqlite")
        with SqliteStore.from_conn_string(database) as store, SqliteSaver.from_conn_string(database) as saver:
            store.put(("case",), "sample", {"text": "stored"})
            agent = create_deep_agent(model=ScriptedModel(responses=[AIMessage(content="checkpointed")]),
                                      store=store, checkpointer=saver)
            agent.invoke({"messages": [("user", "Finish.")]}, config)
        with SqliteStore.from_conn_string(database) as store, SqliteSaver.from_conn_string(database) as saver:
            assert store.get(("case",), "sample").value == {"text": "stored"}
            reopened = create_deep_agent(model=ScriptedModel(responses=[]), store=store, checkpointer=saver)
            assert reopened.get_state(config).values["messages"][-1].content == "checkpointed"
    return "SqliteStore and SqliteSaver both recovered their data after reopening the same case.sqlite."


if __name__ == "__main__":
    report(check)
