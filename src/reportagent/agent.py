from importlib.resources import files
from pathlib import Path
from shutil import copytree

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend


def build_agent(case_dir, model, middleware=(), *, checkpointer=None):
    case_dir = Path(case_dir)
    for skill in ("plan-report", "gather-evidence", "write-section", "review-section"):
        copytree(files("reportagent.skills").joinpath(skill),
                 case_dir / f"work/skills/{skill}", dirs_exist_ok=True)
    return create_deep_agent(
        model=model,
        middleware=middleware,
        checkpointer=checkpointer,
        backend=FilesystemBackend(root_dir=case_dir, virtual_mode=True),
        skills=["/work/skills/"],
        system_prompt=("You work on this report using the files in this case. "
                       "Follow the available skills and save your work as case files."),
    )
