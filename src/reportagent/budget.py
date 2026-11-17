from typing import NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, ToolMessage


class TokenCapState(AgentState):
    total_tokens: NotRequired[int]
    usage_missing: NotRequired[bool]


class TokenCapMiddleware(AgentMiddleware[TokenCapState]):
    state_schema = TokenCapState

    def __init__(self, max_tokens):
        self.max_tokens = max_tokens

    def _end(self, total, tool_calls=()):
        return {"total_tokens": total, "jump_to": "end", "messages": [
            *[ToolMessage(content="Not executed: token cap reached.", tool_call_id=call["id"], status="error") for call in tool_calls],
            AIMessage(content=f"Token cap reached: cap={self.max_tokens}; reported total={total}."),
        ]}

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        total = state.get("total_tokens", 0)
        if total >= self.max_tokens:
            return self._end(total)

    @hook_config(can_jump_to=["end"])
    def after_model(self, state, runtime):
        reply = state["messages"][-1]
        reported = (reply.usage_metadata or {}).get("total_tokens")
        total = state.get("total_tokens", 0) + (reported or 0)
        update = {"total_tokens": total, "usage_missing": state.get("usage_missing", False) or reported is None}
        if total >= self.max_tokens:
            update.update(self._end(total, reply.tool_calls))
        return update

    def after_agent(self, state, runtime):
        if state.get("usage_missing"):
            reply = state["messages"][-1]
            text = reply.text + "\nUsage metadata was missing for some model calls; those calls counted as 0."
            return {"messages": [reply.model_copy(update={"content": text})]}
