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

Current application design uses plain-text evidence notes and section files, passed between fixed gather-then-write phases. The capability checks above are historical probes, not application stores or a to-do workflow.

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

## Task 11 — image input and local PDF extraction, 2026-11-17

Probe only: application source, skills, dependencies and case files are unchanged.
The [probe script](task11_probe.py) keeps outputs outside Git. One configured-model
request accepted a synthetic 64×64 PNG and correctly read its number: 193 input
and 11 output tokens (204 total), below the authorized 2,000-token cap. Output was
limited to 128 tokens; retries and thinking were disabled. This checks image
transport and one trivial reading, not engineering-drawing competence.

Docling 2.150.1 ran in an isolated CPU environment (PyTorch 2.15.0+cpu,
Transformers 5.23.0), with Heron layout and TableFormer V1 accurate, OCR off,
table cell matching on, two model threads and two parser threads. Weights were
prefetched; each physical PDF page ran in a separate process with networking
disabled. Time includes imports, model loading, conversion and exports, excludes
downloads; memory is Linux process peak RSS. These are single measurements,
with filesystem caches shared between processes, not throughput benchmarks.

| Physical page | Seconds | Peak RSS (GiB) | Compared with the original render and current page text |
| --- | ---: | ---: | --- |
| 10 | 5.449 | 1.218 | Separates the two columns; current text interleaves them. |
| 95 | 47.234 | 1.954 | Detects rows, but 20 columns instead of 21; damages headers and merges adjacent cells in several populated rows. |
| 96 | 13.253 | 1.338 | Eight columns; all six populated rows retain the checked location, length, height, geology, scheme and remark associations. Header/unit rows still need interpretation. |
| 97 | 34.035 | 1.665 | Fifteen columns instead of 14; splits one descriptive field into two, including duplicated cells. Checked numeric rows and totals remain in their corresponding numeric fields. |
| 98 | 8.331 | 1.508 | No table detected; Markdown is only an image placeholder. JSON retains 95 text items inside that picture, without a table grid; current page text also retains its labels and values. |
| 99 | 6.538 | 1.524 | Separates numbered prose from the diagram in Markdown, omitting picture-contained labels there; no data table detected. JSON retains additional text items. |

Inspection covered Markdown, CSV, HTML and JSON against page images, not an
exhaustive character-level accuracy score. The structural errors on pages 95
and 97 also occur in JSON/HTML; they are not merely Markdown's documented
flattening of merged cells. Docling improves column order but is not a reliable
replacement for the current extractor on this sample. No parser was replaced.

The earlier survey failure record says 59 pages completed, then physical page
60 raised `Invalid octal`. pypdfium2 5.15.2 read and rendered both pages offline:
page 59 took 0.213 s / 0.062 GiB; page 60 took 5.286 s / 0.117 GiB. Page 60 is
raster content with zero native text; successful rendering does not recover its
meaning or establish that the full PDF can be ingested. Docling was not needed
for this alternative-reader check; OCR and map interpretation were not tested.

One initial offline Docling setup failed to locate the locally downloaded
weights; setting the documented `artifacts_path` resolved it. That failed setup
and the local image-size preflight are preserved privately; neither sent a
provider request. The single paid authorization is consumed; no retry or other
model was called. Licence identifiers and exact used weight revisions are in
[LICENCES.md](LICENCES.md). Selected raw extracts are sent privately for review;
case text, PDFs, model weights, credentials and probe outputs are excluded here.

To reproduce an offline page check after installing these versions and fetching
the two weight sets into Docling's local model directory:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2 python spike/task11_probe.py docling --pdf /path/to/input.pdf --page 10 --output /private/probe/page-010
python spike/task11_probe.py pdfium --pdf /path/to/input.pdf --page 60 --output /private/probe/page-060
```

Run conversion with networking disabled; the offline flags alone are not network
isolation. Inspect `result.json` for conversion status. Image mode makes a paid
request and requires a fresh written authorization; do not rerun this consumed
probe. References: [converter API](https://docling-project.github.io/docling/reference/document_converter/),
[local artifacts and threads](https://docling-project.github.io/docling/usage/advanced_options/),
[serialization limits](https://docling-project.github.io/docling/concepts/serialization/),
[provider image API](https://help.aliyun.com/zh/model-studio/deepseek-api).
