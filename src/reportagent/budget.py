from base64 import b64decode
from io import BytesIO
import json
import math
from pathlib import PurePosixPath
from typing import NotRequired

import pypdfium2 as pdfium
from PIL import Image

from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from .models import PHASE_SETTINGS


def _image_tokens(block, tile):
    # https://www.alibabacloud.com/help/en/model-studio/vision/ — resized tiles plus two framing tokens.
    image_url = block.get("image_url") or {}
    detail = block.get("detail") or (image_url.get("detail") if isinstance(image_url, dict) else None)
    max_pixels = block.get("max_pixels")
    limit = max_pixels if isinstance(max_pixels, int) and max_pixels > 0 else (2621440 if detail in ("low", "auto") else 16777216)
    limit = min(limit, 16777216)
    ceiling = math.ceil(limit / tile**2) + 2
    source = block.get("source")
    data = block.get("base64") or (source.get("data") if isinstance(source, dict) else None)
    if not data:
        url = image_url.get("url", "") if isinstance(image_url, dict) else image_url
        if not isinstance(url, str) or not url.startswith("data:") or ";base64," not in url:
            return ceiling
        data = url.partition(",")[2]
    try:
        # shortcut: an unavailable header (including an unusually large EXIF segment) costs the ceiling.
        with Image.open(BytesIO(b64decode(data[:131072]))) as image:
            width, height = image.size
    except (OSError, ValueError, Image.DecompressionBombError):
        return ceiling
    cols, rows = max(1, round(width / tile)), max(1, round(height / tile))
    minimum = block.get("min_pixels")
    minimum = min(minimum if isinstance(minimum, int) and minimum > 0 else 65536, limit)
    if cols * rows * tile**2 > limit:
        scale = math.sqrt(limit / (width * height))
        cols, rows = math.floor(width * scale / tile), math.floor(height * scale / tile)
    elif cols * rows * tile**2 < minimum:
        scale = math.sqrt(minimum / (width * height))
        cols, rows = math.ceil(width * scale / tile), math.ceil(height * scale / tile)
    return min(ceiling, max(1, cols) * max(1, rows) + 2)


def estimate_input(messages, tools, model, chars_per_token=1.5):
    tile = PHASE_SETTINGS["image_tokens"].get(getattr(model, "model_name", ""))
    tokens = count_tokens_approximately(messages, tools=tools, chars_per_token=chars_per_token,
                                        tokens_per_image=0 if tile else 16384)
    if tile:
        tokens += sum(_image_tokens(block, tile) for message in messages if isinstance(message.content, list)
                      for block in message.content if isinstance(block, dict) and block.get("type") in ("image", "image_url"))
    return tokens


class TokenCapState(AgentState):
    total_tokens: NotRequired[int]
    usage_missing: NotRequired[bool]
    budget_note_sent: NotRequired[bool]
    final_save_rule: NotRequired[str]
    final_calculation_used: NotRequired[bool]
    final_save_retry_used: NotRequired[bool]


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
        self._anchor = self._pending = None

    def _log(self, **fields):
        if self.request_log is not None:
            with self.request_log.open("a", encoding="utf-8") as log:
                log.write(json.dumps(dict(request=self.request_number, **fields)) + "\n")

    def _prepare(self, request):
        # Price the usual tool set conservatively even on a restricted final request.
        tools = request.tools
        if request.state.get("final_save_rule"):
            saves = ("edit_file",) if self.phase in ("revise", "sync") else ("write_file",)
            if self.phase == "rescore":
                saves = ("write_file", "edit_file")
                if not request.state.get("final_calculation_used"):
                    saves += ("calculate_score",)
            request = request.override(tools=[tool for tool in request.tools if getattr(tool, "name", None) in saves])
        guard = self.guard or request.state.get("final_save_retry_used", False)
        if not (self.info or guard):
            return request
        messages = ([request.system_message] if request.system_message else []) + request.messages
        prompt = (messages, tools, request.model)
        base, chars_per_token = 0, 1.5
        if self._anchor is not None:
            (previous, previous_tools, previous_model), tokens = self._anchor
            if previous_model is request.model and previous_tools == tools and messages[:len(previous)] == previous:
                base, messages, tools = tokens, messages[len(previous):], None
                # The Chinese text delta in Task 60 review call 7 used 18% more than the full-prompt text heuristic.
                chars_per_token = 1.25
        estimate = base + estimate_input(messages, tools, request.model, chars_per_token)
        remaining = self.max_tokens - request.state.get("total_tokens", 0)
        available = remaining - estimate
        stop = guard and available < self.output_reserve
        output = None if stop else (available if guard else remaining)
        self.request_number += 1
        self._log(estimated_input_tokens=estimate, remaining_tokens=remaining, output_reserve=self.output_reserve,
                  max_output_tokens=output, decision="stop" if stop else "send")
        if stop:
            return AIMessage(content=f"Token cap reached before send: estimated input {estimate} + {self.output_reserve} output "
                                     f"reserve exceeds {remaining} remaining tokens.",
                             response_metadata={"budget_stop": True})
        self._pending = prompt
        return request.override(model_settings={**request.model_settings, "max_completion_tokens": output})

    def wrap_model_call(self, request, handler):
        prepared = self._prepare(request)
        return prepared if isinstance(prepared, AIMessage) else handler(prepared)

    async def awrap_model_call(self, request, handler):
        prepared = self._prepare(request)
        return prepared if isinstance(prepared, AIMessage) else await handler(prepared)

    def _limit_read(self, request, result):
        if (not self.guard or request.tool_call["name"] != "read_file"
                or not isinstance(result, ToolMessage) or result.status == "error"):
            return result
        remaining = max(0, self.max_tokens - request.state.get("total_tokens", 0) - self.output_reserve)
        model = self._pending[2] if self._pending else None
        estimate = estimate_input([result], None, model, chars_per_token=1.25)
        if estimate <= remaining:
            return result
        path = PurePosixPath(request.tool_call["args"]["file_path"])
        pages = "page count unavailable"
        hint = "Read a smaller line range with offset and limit."
        if isinstance(result.content, list):
            hint = "Use /index.md to select a smaller source or page."
            for block in result.content:
                if isinstance(block, dict) and block.get("mime_type") == "application/pdf":
                    try:
                        with pdfium.PdfDocument(b64decode(block["base64"])) as pdf:
                            pages = f"{len(pdf)} pages" if len(pdf) != 1 else "1 page"
                    except (pdfium.PdfiumError, ValueError):
                        pass
                    hint = (f"Request specific pages or a page range by reading their indexed page files, "
                            f"such as {path.with_suffix('')}/page-001.txt (or .png). See /index.md.")
                    break
        notice = f"{path}: {pages}, estimated {estimate} input tokens; {remaining} available for a read. {hint}"
        smaller = ToolMessage(content=notice, tool_call_id=result.tool_call_id, name=result.name)
        # A shorter native result is better than the notice, even after the allowance runs out.
        return smaller if estimate_input([smaller], None, model, 1.25) < estimate else result

    def wrap_tool_call(self, request, handler):
        return self._limit_read(request, handler(request))

    async def awrap_tool_call(self, request, handler):
        return self._limit_read(request, await handler(request))

    def _end(self, total, rule="reported total >= cap"):
        return {"total_tokens": total, "jump_to": "end", "messages": [
            AIMessage(content=f"Token cap reached: cap={self.max_tokens}; reported total={total}. "
                              f"Stop rule: {rule}."),
        ]}

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        total = state.get("total_tokens", 0)
        if state.get("final_save_rule"):
            last = state["messages"][-1]
            if self.phase == "rescore" and total < self.max_tokens and isinstance(last, ToolMessage):
                if last.name == "calculate_score" and last.status == "success" and not state.get("final_calculation_used"):
                    return {"final_calculation_used": True}
                if last.name in ("write_file", "edit_file") and last.status == "error" and not state.get("final_save_retry_used"):
                    error = last.model_copy(update={"response_metadata": {**last.response_metadata, "rescore_save_retry": True}})
                    return {"final_save_retry_used": True, "final_calculation_used": True, "messages": [error]}
            return {"jump_to": "end"}
        if total >= self.max_tokens:
            return self._end(total)
        previous = next((m for m in reversed(state["messages"]) if isinstance(m, AIMessage)), None)
        last_input = (previous.usage_metadata or {}).get("input_tokens") if previous else None
        turns = 3 if self.phase == "rescore" else 2
        if last_input is not None and self.max_tokens - total < turns * (last_input + self.output_reserve):
            notice = "This is the last response this phase can make; save your file now. "
            if self.phase in ("revise", "sync"):
                save = f"In the {self.phase} phase, make all remaining edits now, as separate edit_file calls in this one response."
            elif self.phase == "rescore":
                notice = "Finish the scoring table now. "
                save = ("If you still need results, call calculate_score with all baseline and scenario inputs, "
                        "then save its results with write_file or edit_file if the budget permits.")
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
            return {"final_save_rule": f"remaining < {turns} * (previous input + output reserve)", "messages": [HumanMessage(
                content=notice + save
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
        measured = usage.get("input_tokens")
        self._anchor = (self._pending, measured) if self._pending is not None and measured is not None else None
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
