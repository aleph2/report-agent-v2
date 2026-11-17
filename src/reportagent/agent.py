from importlib.resources import files
from pathlib import Path
from shutil import copytree

from deepagents import (
    GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile,
)
from deepagents.backends import FilesystemBackend


def build_agent(case_dir, model):
    case_dir = Path(case_dir)
    copytree(files("reportagent.skills").joinpath("write-section"),
             case_dir / "work/skills/write-section", dirs_exist_ok=True)
    # shortcut: profiles are provider-wide; scope per model when mixing agent types in one process.
    register_harness_profile(
        model._get_ls_params()["ls_provider"],
        HarnessProfile(general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)),
    )
    return create_deep_agent(
        model=model,
        backend=FilesystemBackend(root_dir=case_dir, virtual_mode=True),
        skills=["/work/skills/"],
        system_prompt=("You write an assessment section using the files in this case. "
                       "Follow the available writing skill and save your section as a case file."),
    )
