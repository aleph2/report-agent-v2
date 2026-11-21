from importlib.resources import files
from pathlib import Path
from shutil import copytree
from typing import NotRequired

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, ToolMessage

from .models import PHASE_SETTINGS


class PhaseTools(AgentMiddleware):
    def __init__(self, phase, policy, case, section, supplied_files):
        self.action = "edit_file" if phase == "revise" else "write_file"
        self.policy = policy
        self.case = case.resolve()
        self.supplied = frozenset(supplied_files)
        self.review_phase = phase in ("verify-sources", "review")
        self.target = None
        if self.review_phase and section and Path(section).name == section:
            suffix = "-sources.md" if phase == "verify-sources" else ".md"
            self.target = self.case / "work/reviews" / (section + suffix)

    def _model_request(self, request):
        if self.policy == "restricted":
            return request.override(tools=[t for t in request.tools if getattr(t, "name", None) in ("read_file", self.action)])
        if any(isinstance(m, AIMessage) for m in request.messages):
            return request
        # Bailian accepts a named choice only with thinking disabled on this request.
        body = {**(getattr(request.model, "extra_body", None) or {}), **(request.model_settings.get("extra_body") or {})}
        body.pop("thinking_budget", None)
        return request.override(tool_choice={"type": "function", "function": {"name": self.action}},
                                model_settings={**request.model_settings, "extra_body": {**body, "enable_thinking": False}})

    def wrap_model_call(self, request, handler):
        return handler(self._model_request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._model_request(request))

    def _denied(self, call):
        if self.policy != "restricted":
            return None
        relative = call["args"].get("file_path", "").lstrip("/")
        path = (self.case / relative).resolve()
        message = "This phase may read /sources/ pages (find them in /index.md) and save its own file."
        if call["name"] == "read_file":
            if path.is_relative_to(self.case / "sources") or (relative == "index.md" and path == self.case / "index.md"):
                return None
            if relative in self.supplied:
                message = "This file is already in your first message; use that copy."
        if call["name"] == self.action and (not self.review_phase or path == self.target):
            return None
        return ToolMessage(content=message, tool_call_id=call["id"], status="error")

    def wrap_tool_call(self, request, handler):
        return self._denied(request.tool_call) or handler(request)

    async def awrap_tool_call(self, request, handler):
        return self._denied(request.tool_call) or await handler(request)


PHASE_SKILLS = {"verify-sources": "verify-sources", "review": "review-section",
                "clarify": "clarify-section", "revise": "write-section"}


class RevisionState(AgentState):
    edit_batch: NotRequired[AIMessage | None]
    next_edit: NotRequired[int]


class RevisionEdits(AgentMiddleware[RevisionState]):
    """Dispatch one native edit per graph step, then restore the provider response."""

    state_schema = RevisionState

    def after_model(self, state, runtime):
        reply = state["messages"][-1]
        calls = reply.tool_calls
        if len(calls) > 1 and all(call["name"] == "edit_file" for call in calls):
            first = reply.model_copy(update={"tool_calls": calls[:1]})
            return {"edit_batch": reply, "next_edit": 1, "messages": [first]}

    @hook_config(can_jump_to=["tools"])
    def before_model(self, state, runtime):
        batch = state.get("edit_batch")
        if batch is None:
            return None
        index = state["next_edit"]
        if index == len(batch.tool_calls):
            return {"edit_batch": None, "messages": [batch]}
        current = batch.model_copy(update={"tool_calls": [batch.tool_calls[index]]})
        return {"next_edit": index + 1, "messages": [current], "jump_to": "tools"}


def build_agent(case_dir, model, middleware=(), *, checkpointer=None, phase=None, section=None, supplied_files=()):
    case_dir = Path(case_dir)
    backend = FilesystemBackend(root_dir=case_dir, virtual_mode=True)
    skills = ["/work/skills/"]
    prompt = "You work on this report using the files in this case. Follow the available skills and save your work as case files."
    if phase:
        skill = PHASE_SKILLS[phase]
        prompt = files("reportagent.skills").joinpath(skill, "SKILL.md").read_text(encoding="utf-8")
        skills = None
    else:
        for skill in ("plan-report", "gather-evidence", "write-section", "review-section", "clarify-section"):
            copytree(files("reportagent.skills").joinpath(skill),
                     case_dir / f"work/skills/{skill}", dirs_exist_ok=True)
    policy = PHASE_SETTINGS.get(phase, {}).get("tool_policy")
    if policy == "restricted" or (phase in ("clarify", "revise") and policy == "force-first"):
        middleware = [PhaseTools(phase, policy, case_dir, section, supplied_files), *middleware]
    return create_deep_agent(
        model=model,
        middleware=[RevisionEdits(), *middleware] if phase == "revise" else middleware,
        checkpointer=checkpointer,
        backend=backend, skills=skills,
        system_prompt=prompt,
    )
