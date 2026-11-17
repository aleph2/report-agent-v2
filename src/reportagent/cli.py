import argparse
from pathlib import Path

from .export.docx import export_docx
from .ingest import ingest


def main():
    parser = argparse.ArgumentParser(prog="reportagent")
    parser.add_argument("command", choices=("ingest", "export"))
    parser.add_argument("case", type=Path)
    args = parser.parse_args()
    print({"ingest": ingest, "export": export_docx}[args.command](args.case))
