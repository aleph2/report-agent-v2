import os
import tomllib
from importlib.resources import files
from pathlib import Path

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
from dotenv import load_dotenv
from langchain.chat_models import init_chat_model

PHASE_SETTINGS = tomllib.loads(files("reportagent").joinpath("phase-settings.toml").read_text(encoding="utf-8"))


# https://docs.langchain.com/oss/python/deepagents/profiles
RUN_PROFILE = HarnessProfile(
    general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    excluded_middleware={"SummarizationMiddleware"},
)


def make_model(phase=None):
    load_dotenv(Path.cwd() / ".env")
    name = os.environ.get("REVIEW_MODEL_NAME") if phase in ("verify-sources", "review", "clarify", "revise") else None
    name = name or os.environ["MODEL_NAME"]
    # Helpers and automatic summaries would bypass the main token counter.
    register_harness_profile(f"openai:{name}", RUN_PROFILE)
    options = {}
    if phase in PHASE_SETTINGS and name == os.getenv("THINKING_BUDGET_MODEL"):
        options["extra_body"] = {"thinking_budget": PHASE_SETTINGS[phase]["thinking_budget"]}
    return init_chat_model(
        model=name, model_provider="openai", base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"], temperature=0.0, max_retries=0,
        model_kwargs={"parallel_tool_calls": phase == "revise"},
        **options,
        # Close HTTP/1.1 sockets after each response, retaining framework settings.
        # https://www.rfc-editor.org/rfc/rfc9112.html#section-9.6
        # shortcut: use zero pool limits instead if HTTP/2 is enabled.
        default_headers={"Connection": "close"},
    )
