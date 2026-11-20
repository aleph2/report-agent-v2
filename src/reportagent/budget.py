from typing import NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, HumanMessage


class TokenCapState(AgentState):
    total_tokens: NotRequired[int]
    usage_missing: NotRequired[bool]
    budget_note_sent: NotRequired[bool]
    final_save_rule: NotRequired[str]


class TokenCapMiddleware(AgentMiddleware[TokenCapState]):
    state_schema = TokenCapState

    def __init__(self, max_tokens, *, remind=True, phase=None):
        self.max_tokens = max_tokens
        self.remind = remind
        self.phase = phase

    def _end(self, total, rule="reported total >= cap"):
        return {"total_tokens": total, "jump_to": "end", "messages": [
            AIMessage(content=f"Token cap reached: cap={self.max_tokens}; reported total={total}. "
                              f"Stop rule: {rule}."),
        ]}

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        total = state.get("total_tokens", 0)
        if state.get("final_save_rule"):
            return {"jump_to": "end"}
        if total >= self.max_tokens:
            return self._end(total)
        previous = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)
        last_tokens = ((previous.usage_metadata or {}).get("total_tokens") or 0) if previous else 0
        if total + 3 * last_tokens > self.max_tokens:
            action = "a single edit_file call containing all remaining changes" if self.phase == "revise" else "write_file"
            save = (f"In the {self.phase} phase, save your work now with {action}; do not read anything more."
                    if self.phase else "Save your work now in one write; do not read anything more.")
            return {"final_save_rule": "reported total + 3 * previous call > cap", "messages": [HumanMessage(
                content="Token budget reached. " + save
            )]}
        # Keep pending tool calls and their results together before adding the note.
        if self.remind and total >= self.max_tokens / 2 and not state.get("budget_note_sent"):
            return {"budget_note_sent": True, "messages": [HumanMessage(
                content=f"You have used {total} of {self.max_tokens} tokens. Save your work now from what you have; "
                        "mark anything missing as missing."
            )]}

    def after_model(self, state, runtime):
        reply = state["messages"][-1]
        reported = (reply.usage_metadata or {}).get("total_tokens")
        total = state.get("total_tokens", 0) + (reported or 0)
        update = {"total_tokens": total, "usage_missing": state.get("usage_missing", False) or reported is None}
        return update

    def after_agent(self, state, runtime):
        rule = state.get("final_save_rule")
        reply = self._end(state.get("total_tokens", 0), rule)["messages"][0] if rule else state["messages"][-1]
        if state.get("usage_missing"):
            text = reply.text + "\nUsage metadata was missing for some model calls; those calls counted as 0."
            reply = reply.model_copy(update={"content": text})
        if rule or state.get("usage_missing"):
            return {"messages": [reply]}
