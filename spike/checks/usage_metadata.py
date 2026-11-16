from deepagents import create_deep_agent
from langchain.agents.middleware import after_model
from langchain_core.messages import AIMessage

from fake_tool_calls import ScriptedModel, call, report


def check():
    seen = []

    @after_model
    def record(state, runtime):
        seen.append(state["messages"][-1].usage_metadata)

    uses = [dict(input_tokens=7, output_tokens=3, total_tokens=10),
            dict(input_tokens=13, output_tokens=4, total_tokens=17)]
    first = call("ls", path="/").model_copy(update={"usage_metadata": uses[0]})
    model = ScriptedModel(responses=[first, AIMessage(content="Done.", usage_metadata=uses[1])])
    create_deep_agent(model=model, middleware=[record]).invoke({"messages": [("user", "List files.")]})
    assert seen == uses, f"middleware observed {seen!r}"
    return "after_model saw (input, output, total) of (7, 3, 10) and (13, 4, 17) on consecutive responses."


if __name__ == "__main__":
    report(check)
