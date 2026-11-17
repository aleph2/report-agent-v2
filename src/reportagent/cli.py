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


def main():
    parser = argparse.ArgumentParser(prog="reportagent")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "export", "run"):
        command = commands.add_parser(name)
        command.add_argument("case", type=Path)
        if name == "run":
            command.add_argument("--max-tokens", type=int, required=True)
    args = parser.parse_args()
    if args.command != "run":
        print({"ingest": ingest, "export": export_docx}[args.command](args.case))
        return
    model = make_model()
    remaining = args.max_tokens
    total = 0
    folder = args.case / "work/runs"
    folder.mkdir(parents=True, exist_ok=True)
    for phase, prompt in (
        ("gather", "Gather evidence for the section requested in /commission.md."),
        ("write", "Write the section requested in /commission.md from the notes in /work/notes/."),
    ):
        cap = TokenCapMiddleware(remaining, remind=phase == "gather")
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
