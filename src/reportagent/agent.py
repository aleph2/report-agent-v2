from importlib.resources import files
from pathlib import Path
from shutil import copytree

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend


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
        middleware=middleware,
        checkpointer=checkpointer,
        backend=backend, skills=skills,
        system_prompt=prompt,
    )
