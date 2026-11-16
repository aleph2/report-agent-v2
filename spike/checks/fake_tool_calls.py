"""Shared scripted model for the offline probes."""
import sys

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


class ScriptedModel(FakeMessagesListChatModel):
    # The stock fake returns scripted messages but inherits an unimplemented bind_tools.
    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])


def report(check):
    try:
        evidence = check()
    except Exception as exc:
        print("FALSE")
        print(f"{type(exc).__name__}: {exc}".splitlines()[0])
        sys.exit(1)
    print("CONFIRMED")
    print(evidence)


def check():
    fake = FakeMessagesListChatModel(responses=[call("read_file", file_path="/demo")])
    tool = fake.invoke("read the file").tool_calls[0]
    assert (tool["name"], tool["args"]) == ("read_file", {"file_path": "/demo"})
    return "FakeMessagesListChatModel returned the scripted read_file call and arguments."


if __name__ == "__main__":
    report(check)
