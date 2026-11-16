from deepagents import create_deep_agent
from langchain_core.messages import AIMessage, ToolMessage

from fake_tool_calls import ScriptedModel, call, report


def check():
    todos = [{"content": "Check the default planning tool", "status": "completed"}]
    model = ScriptedModel(responses=[call("write_todos", todos=todos), AIMessage(content="Done")])
    state = create_deep_agent(model=model).invoke({"messages": [("user", "Plan the work.")]})
    response = next(m for m in state["messages"] if isinstance(m, ToolMessage))
    assert response.status == "success", response.content
    assert state["todos"] == todos
    return "Default write_todos updated the graph's todos field with the scripted item."


if __name__ == "__main__":
    report(check)
