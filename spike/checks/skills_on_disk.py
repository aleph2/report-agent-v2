from pathlib import Path
from tempfile import TemporaryDirectory

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import AIMessage, SystemMessage

from fake_tool_calls import ScriptedModel, call, report


class Prompts(BaseCallbackHandler):
    def __init__(self):
        self.system = []

    def on_chat_model_start(self, serialized, messages, **kwargs):
        for batch in messages:
            self.system.append(" ".join(m.text for m in batch if isinstance(m, SystemMessage)))


def check():
    prompts = Prompts()
    with TemporaryDirectory() as temporary:
        for role in ("parent", "child"):
            path = Path(temporary, role, f"{role}-skill", "SKILL.md")
            path.parent.mkdir(parents=True)
            path.write_text(f"---\nname: {role}-skill\ndescription: Disk description for {role}\n---\nSkill body.\n")
        child = ScriptedModel(responses=[AIMessage(content="Done.")])
        parent = ScriptedModel(responses=[call("task", subagent_type="reader", description="Check skills."),
                                          AIMessage(content="Done.")])
        agent = create_deep_agent(model=parent, system_prompt="PARENT_MARKER", skills=["/parent/"],
                                  backend=FilesystemBackend(root_dir=temporary, virtual_mode=True),
                                  subagents=[{"name": "reader", "description": "Read", "system_prompt": "CHILD_MARKER",
                                              "model": child, "skills": ["/child/"]}])
        agent.invoke({"messages": [("user", "Delegate to reader.")]}, {"callbacks": [prompts]})
    for role in ("parent", "child"):
        assert any(f"{role.upper()}_MARKER" in prompt and f"Disk description for {role}" in prompt
                   for prompt in prompts.system), f"{role} disk skill missing from model input"
    return "Each model received its own on-disk SKILL.md description in its system prompt."


if __name__ == "__main__":
    report(check)
