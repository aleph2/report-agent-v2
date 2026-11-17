# Milestone 0 ground check

Python 3.12.14, with the five versions pinned in [pyproject.toml](../pyproject.toml). The end-to-end test passed. The eight checks returned seven `CONFIRMED` and one `FALSE`. Each process ran in a fresh Linux network namespace with an empty credential environment.

| Check | Result | Observed evidence |
| --- | --- | --- |
| [todo_tool](checks/todo_tool.py) | FALSE | The default agent rejected `write_todos`. Its tool list was `ls, read_file, write_file, edit_file, delete, glob, grep, execute, task`. |
| [sqlite_store](checks/sqlite_store.py) | CONFIRMED | After both connections closed, a reopened store returned the saved JSON and the graph recovered its final message through the checkpointer in the same SQLite file. |
| [shared_files](checks/shared_files.py) | CONFIRMED | The parent wrote a random marker, then called `task`. The delegated reader received that marker in its own `read_file` result. |
| [skills_on_disk](checks/skills_on_disk.py) | CONFIRMED | Distinct descriptions from two temporary `SKILL.md` files appeared in the actual parent and custom subagent system prompts. This verifies discovery of skill descriptions. |
| [interrupt_in_tool](checks/interrupt_in_tool.py) | CONFIRMED | The SQLite-backed run paused on a question inside a tool. Resuming the thread with an answer returned that answer as the tool result and the run finished. |
| [interrupt_on](checks/interrupt_on.py) | CONFIRMED | The graph requested approval for `write_file` while the target was absent. After an approve decision, the file contained the requested text. |
| [usage_metadata](checks/usage_metadata.py) | CONFIRMED | The `after_model` hook saw both scripted usage records in order, with token totals of 10 and 17. Live client token usage is recorded separately below; this hook check used scripted records. |
| [fake_tool_calls](checks/fake_tool_calls.py) | CONFIRMED | LangChain's `FakeMessagesListChatModel` returned the scripted tool name and arguments. DeepAgents also calls `bind_tools`, which the stock fake leaves unimplemented. The test fixture supplies this method. |

## What changes for the design

- Treat the built-in to-do tool as profile-dependent. The pinned release explicitly adds `TodoListMiddleware` in its [OpenAI Codex profile](https://github.com/langchain-ai/deepagents/blob/bb49cf50d3450a3bbebe8d25240cb7ef6379bd0d/libs/deepagents/deepagents/profiles/harness/_openai_codex.py). The generic agent in this probe has no default planning tool. The end-to-end test records the model stating a plan in its message, making one successful file write, and finishing.
- Qualify the design's LiteLLM licence entry. The core is MIT and the enterprise directory has separate terms. MinerU also has commercial thresholds and an attribution condition. See the [licence inventory](LICENCES.md).
- The [copied example](deep_research/agent.py) includes web-research tools. After the initial ground check, its model selection was adapted to read `.env`, and the notebook now reuses that model. The [offline tests](../tests/e2e/test_hello.py) cover the original scripted framework flow and the example entrypoint with scripted OpenAI-compatible HTTP responses. The configured Bailian model was checked separately below; the full web-research workflow remains unverified.

## Reproduce

From the repository root in an environment with the pinned dependencies and test extra:

```sh
python -m pytest -q tests/e2e
for check in spike/checks/*.py; do python "$check"; done
```

Each check prints its verdict and evidence. A false assumption exits with status 1. The fixture supplies every model response locally.

## Live API verification — 2026-11-16

The owner requested a live API check after configuring `.env`. The selected model
was `deepseek-v4.1-flash-1017`, served through the Beijing workspace OpenAI-compatible
endpoint with `langchain-openai==1.6.6`. Every response identified this same model.
The existing runtime reads its model, endpoint and key from `.env`; it needed no
client change.

| Check | Observed result | Input tokens | Output tokens |
| --- | --- | --- | --- |
| Basic chat | Returned `OK` | 35 | 12 |
| Function calling | Returned the requested `verification_echo` call and arguments | 315 | 44 |
| Tool result round trip | Accepted the tool result and returned `API_VERIFIED` | 110 | 25 |

Three requests were made with automatic retries disabled, a 30-second timeout,
and output caps of 256, 256 and 128 tokens respectively. The API reported
460 input tokens and 81 output tokens,
541 tokens in total. These are API-reported usage figures.
The API key and workspace-specific hostname are excluded from this report.

The checks used the same LangChain model initialization as `agent.py`, rather
than launching the research agent. Web search and the full assessment workflow
were not run; `TAVILY_API_KEY` remains unset. The scripted offline tests remain
independent of live credentials.

Provider reference: [DeepSeek API documentation](https://help.aliyun.com/en/model-studio/deepseek-api).
