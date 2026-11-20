from importlib.resources import files
from pathlib import Path
from shutil import copytree
from typing import NotRequired

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage


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


def build_agent(case_dir, model, middleware=(), *, checkpointer=None, phase=None):
    case_dir = Path(case_dir)
    backend = FilesystemBackend(root_dir=case_dir, virtual_mode=True)
    skills = ["/work/skills/"]
    prompt = "You work on this report using the files in this case. Follow the available skills and save your work as case files."
    if phase:
        skill = {"verify-sources": "verify-sources", "review": "review-section",
                 "clarify": "clarify-section", "revise": "write-section"}[phase]
        prompt = files("reportagent.skills").joinpath(skill, "SKILL.md").read_text(encoding="utf-8")
        skills = None
    else:
        for skill in ("plan-report", "gather-evidence", "write-section", "review-section", "clarify-section"):
            copytree(files("reportagent.skills").joinpath(skill),
                     case_dir / f"work/skills/{skill}", dirs_exist_ok=True)
    return create_deep_agent(
        model=model,
        middleware=[RevisionEdits(), *middleware] if phase == "revise" else middleware,
        checkpointer=checkpointer,
        backend=backend, skills=skills,
        system_prompt=prompt,
    )
