from importlib.resources import files
from pathlib import Path
from shutil import copytree

from deepagents import FilesystemPermission, create_deep_agent
from deepagents.backends import FilesystemBackend
from deepagents.middleware import FilesystemMiddleware


def build_agent(case_dir, model, middleware=(), *, checkpointer=None, phase=None, section=None):
    case_dir = Path(case_dir)
    backend = FilesystemBackend(root_dir=case_dir, virtual_mode=True)
    permissions, skills = None, ["/work/skills/"]
    prompt = "You work on this report using the files in this case. Follow the available skills and save your work as case files."
    if phase:
        review = f"/work/reviews/{section}.md"
        body = f"/work/sections/{section}.md"
        clarifications = f"/work/clarifications/{section}.md"
        target = {"verify-sources": f"/work/reviews/{section}-sources.md", "review": review,
                  "clarify": clarifications, "revise": body}[phase]
        reads = {"verify-sources": ["/sources/**", "/index.md", f"/work/checks/{section}.md"],
                 "clarify": [review, body, clarifications, "/commission/澄清答复.md"]}.get(phase)
        permissions = [FilesystemPermission(operations=["write"], paths=[target]),
                       FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]
        if reads:
            permissions += [FilesystemPermission(operations=["read"], paths=reads),
                            FilesystemPermission(operations=["read"], paths=["/**"], mode="deny")]
        # 0.7.19 replacement middleware must receive its own permissions explicitly.
        filesystem = FilesystemMiddleware(backend=backend, _permissions=permissions,
            tools=["read_file", "ls", "glob", "grep", "edit_file" if phase == "revise" else "write_file"])
        middleware = [filesystem, *middleware]
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
        backend=backend, permissions=permissions, skills=skills,
        system_prompt=prompt,
    )
