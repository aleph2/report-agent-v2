# Rules for this repository

Read this file at the start of every task. The design is "Report agent v2: design". If a task conflicts with a rule here, stop and ask the owner.

## What this system is

An agent system that writes an assessment report from supplied source documents and delivers it as Word and Excel. Agents make every judgement. Code only stores data, limits runs and renders files.

## Principles

1. **Code must not judge.** No check on report content, on evidence, or on who may act.
2. **Agents are intelligent.** Give them information and let them decide. Return a missing or odd value to the agent as data. Do not refuse it.
3. **Borrow before building.** Use the framework first, then an open-source project, then v1 code. Write new code last.
4. **Plain-text handoff.** Agents exchange plain-text files (notes, sections), not structured data; no stores or schemas between phases. Evidence notes pass research into a fresh drafting phase; gathering and writing are fixed phases.
5. **Small tool surface.** Report know-how goes into skill files, not tools or prompts in code.
6. **Human input is data.** Answers and approvals are saved as plain-text files. Code never tests whether an approval exists.
7. **One live variant.** No version numbers for prompts, schemas or interfaces. No backward compatibility. Git holds history.
8. **End to end first.** Every change is shown to work by an end-to-end run.
9. **A failed run is answered with information first:** a sentence in a skill, then a clearer tool description or result, then code. A new check in code is last and needs the owner's decision.

## How you work here

- You get one small task at a time from the owner. Do that task only.
- Do not choose the next task, widen the scope, refactor nearby code, or add "safety" checks.
- Do not add a dependency, a module, a tool, a `raise` or a database table unless the task says so. If you think one is needed, stop and ask.
- If you are unsure what the owner wants, ask before writing code.
- One task is one commit on a branch. The owner and Claude review it before anything else starts.
- Before every merge and on the merged commit, run the offline canonical check/export comparison in [the merge regression routine](docs/regression.md). Include every difference in the task report. Explain unexpected differences to Claude before merging.
- With each commit, say in plain words: what you changed, how you ran it, what you did not do, and anything you are unsure about.
- No paid model call, ever, unless the owner approved that run in writing. Tests use a scripted fake model.

## Budgets

| Measure | Budget |
|---|---|
| Our own code in `src/reportagent/`, excluding `export/` | 2,500 lines |
| Ported code in `src/reportagent/export/` | 1,700 lines |
| Modules | 7 plus `export/` |
| Our own tools | 5 |
| `raise` statements in our own code, excluding `export/` | 15, each for a system limit or the token cap |
| Database tables we define | 0 |
| Test code | less than the source it tests |

Going over a budget needs the owner's decision, recorded below.

## Code style

- Plain, short Python. Prefer a function to a class, and a library call to our own code.
- A tool fails only for a real system limit, such as a missing file, and says so in plain words.
- Let library validation errors reach the agent unchanged.
- No `try/except` that hides an error. No defensive checks for cases that have not happened in a run.
- Comments say why, not what.

## Decisions by the owner

- Task 80 (2026-11-21): Under the owner's delegation, Claude approved a separate `src/reportagent/learn.py` for learning records. This permits eight core Python modules for this extraction.

- Task 116 (2026-11-23): The owner chose a team-only browser UI served on localhost on one machine. The team uploads materials, runs stage 1, downloads the draft and client question sheet, uploads the answered sheet, runs stage 2, and downloads the final report. The UI shows progress and token use. Task 116 supplies the two serial stage commands. The local page is Task 117, after review and merge of Task 116.
