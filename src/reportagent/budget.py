import json
from typing import NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.messages.utils import count_tokens_approximately

from .models import PHASE_SETTINGS


class TokenCapState(AgentState):
    total_tokens: NotRequired[int]
    usage_missing: NotRequired[bool]
    budget_note_sent: NotRequired[bool]
    final_save_rule: NotRequired[str]


class TokenCapMiddleware(AgentMiddleware[TokenCapState]):
    state_schema = TokenCapState

    def __init__(self, max_tokens, *, remind=True, phase=None, info=False, guard=False, request_log=None):
        self.max_tokens = max_tokens
        self.remind = remind
        self.phase = phase
        self.info = info
        self.guard = guard
        self.request_log = request_log
        self.output_reserve = PHASE_SETTINGS.get(phase, {}).get("output_reserve", 8000)
        self.request_number = 0

    def _log(self, **fields):
        if self.request_log is not None:
            with self.request_log.open("a", encoding="utf-8") as log:
                log.write(json.dumps(dict(request=self.request_number, **fields)) + "\n")

    def _prepare(self, request):
        if not (self.info or self.guard):
            return request
        messages = ([request.system_message] if request.system_message else []) + request.messages
        # Chinese engineering text is denser than the default English estimate.
        estimate = count_tokens_approximately(messages, tools=request.tools, chars_per_token=1.5, tokens_per_image=16384)
        remaining = self.max_tokens - request.state.get("total_tokens", 0)
        available = remaining - estimate
        stop = self.guard and available < self.output_reserve
        output = None if stop else (available if self.guard else remaining)
        self.request_number += 1
        self._log(estimated_input_tokens=estimate, remaining_tokens=remaining, output_reserve=self.output_reserve,
                  max_output_tokens=output, decision="stop" if stop else "send")
        if stop:
            return AIMessage(content=f"Token cap reached before send: estimated input {estimate} + {self.output_reserve} output "
                                     f"reserve exceeds {remaining} remaining tokens.",
                             response_metadata={"budget_stop": True})
        return request.override(model_settings={**request.model_settings, "max_completion_tokens": output})

    def wrap_model_call(self, request, handler):
        prepared = self._prepare(request)
        return prepared if isinstance(prepared, AIMessage) else handler(prepared)

    async def awrap_model_call(self, request, handler):
        prepared = self._prepare(request)
        return prepared if isinstance(prepared, AIMessage) else await handler(prepared)

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
        last_input = (previous.usage_metadata or {}).get("input_tokens") if previous else None
        if last_input is not None and self.max_tokens - total < 2 * (last_input + self.output_reserve):
            if self.phase == "revise":
                save = "In the revise phase, make all remaining edits now, as separate edit_file calls in this one response."
            elif self.phase == "clarify":
                save = ("In the clarify phase, use write_file to save the complete official clarification file now. "
                        "Keep every existing item and its number, except an evidenced collision renumbering. "
                        "Do not write a separate draft.")
            elif self.phase:
                save = f"In the {self.phase} phase, save your work now with write_file; do not read anything more."
            else:
                save = "Save your work now in one write; do not read anything more."
            if self.info:
                save += f" Remaining allowance: {self.max_tokens - total} tokens."
            return {"final_save_rule": "remaining < 2 * (previous input + output reserve)", "messages": [HumanMessage(
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
        if reply.response_metadata.get("budget_stop"):
            return {"total_tokens": state.get("total_tokens", 0)}
        usage = reply.usage_metadata or {}
        reported = usage.get("total_tokens")
        if self.request_number:
            self._log(event="response", input_tokens=usage.get("input_tokens"), output_tokens=usage.get("output_tokens"),
                      reasoning_tokens=(usage.get("output_token_details") or {}).get("reasoning"), total_tokens=reported)
        total = state.get("total_tokens", 0) + (reported or 0)
        update = {"total_tokens": total, "usage_missing": state.get("usage_missing", False) or reported is None}
        return update

    def after_agent(self, state, runtime):
        reply = state["messages"][-1]
        rule = state.get("final_save_rule") and not reply.response_metadata.get("budget_stop")
        if rule:
            reply = self._end(state.get("total_tokens", 0), state["final_save_rule"])["messages"][0]
        if state.get("usage_missing"):
            text = reply.text + "\nUsage metadata was missing for some model calls; those calls counted as 0."
            reply = reply.model_copy(update={"content": text})
        if rule or state.get("usage_missing"):
            return {"messages": [reply]}
