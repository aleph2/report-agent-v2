from decimal import Decimal, ROUND_HALF_UP
from importlib.resources import files
from pathlib import Path
import re
from shutil import copytree
from typing import Annotated, NotRequired

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain.agents.middleware import AgentMiddleware, AgentState, hook_config
from langchain_core.messages import AIMessage, ToolMessage

from langchain_core.tools import tool
from pydantic import Field

from .budget import CALCULATE_FIRST, successful_score
from .ingest import profile_data
from .models import PHASE_SETTINGS


Score = Annotated[Decimal, Field(ge=0, le=100)]
Weight = Annotated[Decimal, Field(ge=0, le=1)]
SCORE_PHASES = ("review", "revise", "clarify", "rescore")


def score_result(total):
    grading = PHASE_SETTINGS["grading"]
    grade_index = sum(total > limit for limit in grading["upper_bounds"])
    return {"F": str(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
            "grade": grading["grades"][grade_index]}


@tool
def calculate_score(scores: dict[str, Score], weights: dict[str, Weight],
                    scenarios: list[dict[str, Score]] | None = None, include_products: bool = False) -> dict:
    """Compute base F = sum(score * weight) and grade. Use identical indicator IDs and fractional weights; weights are used as supplied.
    Each scenario overrides only named base scores; results follow input order. Grade uses unrounded F.
    To calculate a range, supply its lower and upper endpoint scenarios. include_products returns each weighted Xij rounded to two decimals.
    """
    if not scores or scores.keys() != weights.keys():
        return {"error": "Supply the same nonempty set of indicator IDs for scores and weights."}
    scenarios = scenarios or []
    if any(overrides.keys() - scores.keys() for overrides in scenarios):
        return {"error": "A scenario may override only indicators in the base scores."}

    def evaluate(values):
        products = {key: values[key] * weight for key, weight in weights.items()}
        result = score_result(sum(products.values()))
        if include_products:
            result["Xij"] = {key: str(x.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)) for key, x in products.items()}
        return result

    return {"base": evaluate(scores), "scenarios": [evaluate(scores | overrides) for overrides in scenarios]}


def _profile_tools(case):
    @tool
    def list_profile_layers(file: str, page: int) -> dict:
        """List vector layers on a one-based page of an original /sources/ PDF, with metric extents and axis ticks."""
        return profile_data(case, file, page)

    @tool
    def measure_profile(file: str, page: int, x_from: float, x_to: float, layer: str = "dmx",
                        min_segment_m: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 2.0,
                        simplify_m: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.2) -> dict:
        """Measure the two-point chord and clipped profile of a chosen PDF layer in metres.
        Returns raw_piece_count and segments after Douglas–Peucker simplification at tolerance simplify_m.
        Angles follow the requested direction (verticals follow the path). The steepest segment has the
        largest absolute angle among simplified segments with horizontal run at least min_segment_m."""
        return profile_data(case, file, page, layer, (x_from, x_to), min_segment_m, simplify_m)

    return [list_profile_layers, measure_profile]


def supplied_notice(paths):
    return "Files already included in your first message: " + ", ".join("/" + path for path in paths)


class PhaseTools(AgentMiddleware):
    def __init__(self, phase, policy, case, section, supplied_files):
        self.score_tool = ("calculate_score",) if phase in SCORE_PHASES else ()
        self.profile_tools = ("list_profile_layers", "measure_profile") if phase == "rescore" else ()
        self.action = "edit_file" if phase in ("revise", "sync") else "write_file"
        self.two_step = phase in ("rescore", "sync")
        self.actions = (self.action, "edit_file") if phase == "rescore" else (self.action,)
        self.readable = set()
        self.phase = phase
        self.policy = policy
        self.case = case.resolve()
        self.supplied = supplied_files
        self.review_phase = phase in ("verify-sources", "review")
        self.learning = phase == "learn"
        self.target = self.case / "work/learn/proposals.md" if self.learning else None
        if self.review_phase and section and Path(section).name == section:
            suffix = "-sources.md" if phase == "verify-sources" else ".md"
            self.target = self.case / "work/reviews" / (section + suffix)

        if self.two_step and section and Path(section).name == section:
            body = self.case / "work/sections" / (section + ".md")
            table = body.with_suffix(".table.md")
            self.target = table if phase == "rescore" else body
            self.readable = {table, self.case / "work/notes" / body.name} if phase == "rescore" else {table, body}

    def _model_request(self, request):
        if self.policy == "restricted":
            allowed = {"read_file", *self.actions, *self.score_tool, *self.profile_tools}
            if self.phase == "rescore" and request.state.get("final_save_rule") and self.target and not self.target.is_file():
                allowed.discard("edit_file")
            return request.override(tools=[t for t in request.tools if getattr(t, "name", None) in allowed])
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

    def _denied(self, call, messages):
        if self.policy != "restricted":
            return None
        if call["name"] in (*self.score_tool, *self.profile_tools):
            return None
        relative = call["args"].get("file_path", "").lstrip("/")
        path = (self.case / relative).resolve()
        message = "This phase may read /sources/ pages (find them in /index.md) and save its own file."
        if self.two_step:
            if call["name"] in self.actions and path == self.target:
                if self.phase == "rescore" and not any(map(successful_score, messages)):
                    return ToolMessage(content=CALCULATE_FIRST, tool_call_id=call["id"], name=call["name"], status="error")
                return None
            if call["name"] == "read_file" and (path in self.readable or
                    (self.score_tool and (path.is_relative_to(self.case / "sources") or path == self.case / "index.md"))):
                return None
            return ToolMessage(content="This phase allows only its assigned files and tools.",
                               tool_call_id=call["id"], name=call["name"], status="error")
        if call["name"] == "read_file":
            if path.is_relative_to(self.case / "sources") or (relative == "index.md" and path == self.case / "index.md"):
                return None
            if self.supplied:
                message += " " + supplied_notice(self.supplied)
        if call["name"] == self.action and ((self.target is None and not self.review_phase) or path == self.target):
            return None
        return ToolMessage(content=message, tool_call_id=call["id"], status="error")

    def _proposal_status(self, request):
        call = request.tool_call
        if self.learning and call["name"] == "write_file" and isinstance(call["args"].get("content"), str):
            # A model can propose a lesson; only a subsequent human edit marks it for adoption.
            body = call["args"]["content"].replace("\r\n", "\n").replace("\r", "\n")
            body = re.sub(r"(?m)^status:[^\n]*", "status: proposed", body)
            return request.override(tool_call={**call, "args": {**call["args"], "content": body}})
        return request

    def wrap_tool_call(self, request, handler):
        return self._denied(request.tool_call, request.state["messages"]) or handler(self._proposal_status(request))

    async def awrap_tool_call(self, request, handler):
        return self._denied(request.tool_call, request.state["messages"]) or await handler(self._proposal_status(request))


PHASE_SKILLS = {"rescore": "rescore-section", "sync": "sync-section", "learn": "learn-from-answers", "verify-sources": "verify-sources", "review": "review-section",
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
    if phase in PHASE_SKILLS:
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
    tools = [calculate_score] if phase in SCORE_PHASES else []
    if phase in ("gather", "rescore"):
        tools.extend(_profile_tools(case_dir))
    return create_deep_agent(
        model=model, tools=tools,
        middleware=[RevisionEdits(), *middleware] if phase in ("revise", "sync") else middleware,
        checkpointer=checkpointer,
        backend=backend, skills=skills,
        system_prompt=prompt,
    )
