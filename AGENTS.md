# Rules for this repository

Read this file at the start of every task. The design is "Report agent v2: design". If a task conflicts with a rule here, stop and ask the owner.

## What this system is

An agent system that writes an assessment report from supplied source documents and delivers it as Word and Excel. Agents make every judgement. Code only stores data, limits runs and renders files.

## Principles

1. **Code must not judge.** No check on report content, on evidence, or on who may act.
2. **Agents are intelligent.** Give them information and let them decide. Return a missing or odd value to the agent as data. Do not refuse it.
3. **Borrow before building.** Use the framework first, then an open-source project, then v1 code. Write new code last.
4. **Three stores.** All working data is a file, a to-do item or an assertion. The plan is a view of the to-do items.
5. **Small tool surface.** Report know-how goes into skill files, not tools or prompts in code.
6. **Human input is data.** Answers and approvals are saved as assertions. Code never tests whether an approval exists.
7. **One live variant.** No version numbers for prompts, schemas or interfaces. No backward compatibility. Git holds history.
8. **End to end first.** Verify changes to report behavior with an end-to-end run using the scripted fake model. For documentation-only changes, verify the committed file directly.
9. **Diagnose a failed run first.** Use the trace and saved output to find the cause. If an agent lacked information, clarify the skill, tool description or result. If the implementation failed, fix the code responsible. A new check in code needs the owner's decision.

## How you work here

- You get one small task at a time from the owner. Do that task only.
- Do not choose the next task, widen the scope, refactor nearby code, or add "safety" checks.
- Do not add a dependency, a module, a tool, a `raise` or a database table unless the task says so. If you think one is needed, stop and ask.
- If you are unsure what the owner wants, ask before writing code.
- One task is one commit on a branch. The owner and Claude review it before anything else starts.
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

(none yet)
