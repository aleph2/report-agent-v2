from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage


class ScriptedModel(FakeMessagesListChatModel):
    # The stock fake inherits an unimplemented bind_tools.
    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=tools, **kwargs)


def call(name, **args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])
