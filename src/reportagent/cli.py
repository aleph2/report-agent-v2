import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys

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
    for name in ("ingest", "export", "plan", "run"):
        command = commands.add_parser(name)
        command.add_argument("case", type=Path)
        if name in ("plan", "run"):
            command.add_argument("--max-tokens", type=int, required=True)
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
    elif args.section:
        name = args.section
        requirements = f"Its required contents are under heading ## {name} in /work/plan.md."
        phases = (
            ("gather", f"Gather evidence for section {name} requested in /commission/. "
             f"{requirements} Save evidence notes to /work/notes/{name}.md."),
            ("write", f"Write section {name} requested in /commission/. {requirements} "
             f"Use the evidence notes in /work/notes/{name}.md. "
             f"Save the section to /work/sections/{name}.md."),
        )
    else:
        phases = (
            ("gather", "Gather evidence for the section requested in /commission/."),
            ("write", "Write the section requested in /commission/ from the notes in /work/notes/."),
        )
    for phase, prompt in phases:
        gathering = phase == "gather"
        if gathering and args.write_only:
            continue
        limit = int(args.max_tokens * GATHER_BUDGET_FRACTION) if gathering else remaining
        cap = TokenCapMiddleware(limit, remind=gathering)
        calls = ModelCallLimitMiddleware(run_limit=200, exit_behavior="end")
        state = {"messages": [HumanMessage(content=prompt)]}
        log = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{phase}.jsonl")
        try:
            agent = build_agent(args.case, model, middleware=[cap, calls])
            for state in agent.stream(state, stream_mode="values"):
                pass
        finally:
            log.write_text("".join(message.model_dump_json() + "\n" for message in state["messages"]), encoding="utf-8")
            used = state.get("total_tokens", 0)
            total += used
            remaining = max(0, remaining - used)
            print(f"{phase} phase tokens: {used}")
            print(f"Reported tokens: {total}")
            print("Run ended:", "error" if sys.exception() else state["messages"][-1].text)
