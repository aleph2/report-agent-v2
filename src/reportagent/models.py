import os
from pathlib import Path

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

# https://docs.langchain.com/oss/python/deepagents/profiles
RUN_PROFILE = HarnessProfile(
    general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    excluded_middleware={"SummarizationMiddleware"},
)


def make_model():
    load_dotenv(Path.cwd() / ".env")
    name = os.environ["MODEL_NAME"]
    # Helpers and automatic summaries would bypass the main token counter.
    register_harness_profile(f"openai:{name}", RUN_PROFILE)
    return init_chat_model(
        model=name, model_provider="openai", base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"], temperature=0.0,
    )
