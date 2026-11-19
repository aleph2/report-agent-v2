import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

from httpx import NetworkError, RemoteProtocolError, TimeoutException
from langgraph.checkpoint.memory import InMemorySaver
from openai import APIConnectionError

from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import HumanMessage

from .agent import build_agent
from .budget import TokenCapMiddleware
from .export.docx import export_docx
from .ingest import ingest
from .models import make_model


GATHER_BUDGET_FRACTION = 0.6


def main():
    parser = argparse.ArgumentParser(prog="reportagent")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "export", "plan", "run", "review"):
        command = commands.add_parser(name)
        command.add_argument("case", type=Path)
        if name in ("plan", "run", "review"):
            command.add_argument("--max-tokens", type=int, required=True)
        if name == "review":
            command.add_argument("--section", required=True)
        if name == "run":
            command.add_argument("--write-only", action="store_true")
            command.add_argument("--section")
    args = parser.parse_args()
    if args.command in ("ingest", "export"):
        print({"ingest": ingest, "export": export_docx}[args.command](args.case))
        return
    model = make_model()
    remaining = args.max_tokens
    total = 0
    folder = args.case / "work/runs"
    folder.mkdir(parents=True, exist_ok=True)
    if args.command == "plan":
        phases = (("plan", "Plan the report requested in /commission/."),)
    elif args.command == "review":
        name = args.section
        phases = (("review", f"Review section {name}. "
                             f"Save the review to /work/reviews/{name}.md."),)
    elif args.section:
        name = args.section
        requirements = f"Its required contents are under heading ## {name} in /work/plan.md."
        phases = (
            ("gather", f"Gather evidence for section {name} requested in /commission/. "
             f"{requirements} Save evidence notes to /work/notes/{name}.md."),
            ("write", f"Write section {name}. {requirements} "
             f"Use the evidence notes in /work/notes/{name}.md. "
             f"Save the section to /work/sections/{name}.md."),
        )
    else:
        phases = (
            ("gather", "Gather evidence for the section requested in /commission/."),
            ("write", "Write the section from the notes in /work/notes/."),
        )
    for phase, prompt in phases:
        gathering = phase == "gather"
        if gathering and args.write_only:
            continue
        if phase == "write" and not args.write_only:
            notes = args.case / "work/notes"
            notes_files = [notes / f"{args.section}.md"] if args.section else notes.glob("*.md")
            if not any(path.is_file() for path in notes_files):
                print(f"No notes for {args.section or 'requested section'}; writing skipped.")
                continue
        limit = int(args.max_tokens * GATHER_BUDGET_FRACTION) if gathering else remaining
        cap = TokenCapMiddleware(limit, remind=phase in ("plan", "gather", "review"))
        calls = ModelCallLimitMiddleware(run_limit=200, exit_behavior="end")
        state = {"messages": [HumanMessage(content=prompt)]}
        log = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{phase}.jsonl")
        config = {"configurable": {"thread_id": log.stem}}
        resume = None
        try:
            agent = build_agent(args.case, model, middleware=[cap, calls], checkpointer=InMemorySaver())
            # Flush each checkpoint before advancing; avoids queued background writes.
            try:
                for state in agent.stream(state, config=config, stream_mode="values", durability="sync"):
                    pass
            except (APIConnectionError, NetworkError, RemoteProtocolError, TimeoutException) as error:
                state = agent.get_state(config).values
                resume = (len(state["messages"]), json.dumps({
                    "type": "resume", "phase": phase, "thread_id": log.stem,
                    "error": type(error).__name__, "total_tokens": state.get("total_tokens", 0),
                }))
                # https://docs.langchain.com/oss/python/langgraph/checkpointers#choose-how-to-continue-a-thread
                for state in agent.stream(None, config=config, stream_mode="values", durability="sync"):
                    pass
        finally:
            lines = [message.model_dump_json() + "\n" for message in state["messages"]]
            if resume:
                lines.insert(resume[0], resume[1] + "\n")
            log.write_text("".join(lines), encoding="utf-8")
            used = state.get("total_tokens", 0)
            total += used
            remaining = max(0, remaining - used)
            print(f"{phase} phase tokens: {used}")
            print(f"Reported tokens: {total}")
            print("Run ended:", "error" if sys.exception() else state["messages"][-1].text)
