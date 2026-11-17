from importlib.resources import files
from pathlib import Path
from shutil import copytree

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend


def build_agent(case_dir, model, middleware=()):
    case_dir = Path(case_dir)
    copytree(files("reportagent.skills").joinpath("write-section"),
             case_dir / "work/skills/write-section", dirs_exist_ok=True)
    return create_deep_agent(
        model=model,
        middleware=middleware,
        backend=FilesystemBackend(root_dir=case_dir, virtual_mode=True),
        skills=["/work/skills/"],
        system_prompt=("You write an assessment section using the files in this case. "
                       "Follow the available writing skill and save your section as a case file."),
    )
