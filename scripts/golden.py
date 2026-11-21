"""Freeze accepted exports and compare the canonical case without invoking a model."""
import argparse
from datetime import date
from difflib import unified_diff
from hashlib import sha256
import json
from pathlib import Path
from posixpath import normpath
from shutil import copyfile
from subprocess import check_output
from tempfile import TemporaryDirectory
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

from reportagent.cli import check
from reportagent.export.docx import _settings, export_docx


def printable(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def word(path):
    document = Document(path)
    paragraphs, comments, indices = [], [], {}
    anchors = {qn("w:" + k): k for k in ("commentRangeStart", "commentRangeEnd", "commentReference")}
    for p in document.element.body.iter(qn("w:p")):
        marks, offset = [], 0
        for el in p.iter():
            if el.tag in anchors:
                ident = int(el.get(qn("w:id")))
                marks.append((offset, anchors[el.tag], indices.setdefault(ident, len(indices) + 1)))
            elif el.tag == qn("w:t"):
                offset += len(el.text or "")
            elif el.tag in (qn("w:tab"), qn("w:br"), qn("w:cr")):
                offset += 1
        paragraphs.append(printable([Paragraph(p, None).text, marks]))
    for note in document.comments:
        index = indices.setdefault(note.comment_id, len(indices) + 1)
        comments.append((index, note.author, note.initials, [p.text for p in note.paragraphs]))
    return paragraphs, [printable(c) for c in sorted(comments)]


def compare_word(old, new):
    before, after = word(old), word(new)
    changes = []
    for title, left, right in zip(("body paragraphs", "comments"), before, after):
        changes.extend(unified_diff(left, right, "accepted " + title, "current " + title, n=0, lineterm=""))
    return f"Word paragraphs {len(before[0])} -> {len(after[0])}, comments {len(before[1])} -> {len(after[1])}", changes


def excel(path):
    sheets, cells = [], {}
    def string(el):
        return "".join(t.text or "" for t in el.findall("{*}t") + el.findall("{*}r/{*}t"))
    with ZipFile(path) as book:
        def xml(name):
            return ET.fromstring(book.read(name))
        links = {r.get("Id"): r.get("Target") for r in xml("xl/_rels/workbook.xml.rels")}
        shared = [string(si) for si in xml("xl/sharedStrings.xml")] if "xl/sharedStrings.xml" in book.namelist() else []
        for sheet in xml("xl/workbook.xml").findall("{*}sheets/{*}sheet"):
            name = sheet.attrib["name"]
            sheets.append(name)
            target = links[sheet.attrib[qn("r:id")]]
            part = target.lstrip("/") if target.startswith("/") else normpath("xl/" + target)
            for cell in xml(part).findall("{*}sheetData/{*}row/{*}c"):
                kind, val = cell.get("t", "n"), cell.findtext("{*}v")
                if kind == "s":
                    kind, val = "str", shared[int(val)]
                elif kind == "inlineStr":
                    kind, val = "str", string(cell.find("{*}is"))
                formula = cell.find("{*}f")
                if val not in (None, "") or formula is not None:
                    expr = (formula.text, formula.attrib) if formula is not None else None
                    cells[name, cell.attrib["r"]] = (kind, val, expr)
    return sheets, cells


def compare_excel(old, new):
    sheets, left = excel(old)
    new_sheets, right = excel(new)
    changes = [] if sheets == new_sheets else [f"Sheet names/order: {printable(sheets)} -> {printable(new_sheets)}"]
    for key in sorted(left.keys() | right.keys()):
        if left.get(key) != right.get(key):
            changes.append(f"{key[0]}!{key[1]}: {printable(left.get(key))} -> {printable(right.get(key))}")
    return f"Excel sheets {len(sheets)} -> {len(new_sheets)}, populated cells {len(left)} -> {len(right)}", changes


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def record(args, golden, cfg):
    section = args.section
    if Path(section).name != section or not (args.case / "work/sections" / (section + ".md")).is_file():
        raise SystemExit("Supply the existing section filename stem.")
    target = golden / section
    if target.exists():
        raise SystemExit(f"Baseline already exists: {target}")
    git = ["git", "-C", str(Path(__file__).resolve().parents[1])]
    commit = check_output([*git, "rev-parse", "--verify", args.commit + "^{commit}"], text=True).strip()
    skills = check_output([*git, "ls-tree", "-r", "--name-only", commit, "--", "skills"], text=True).splitlines()
    manifest = dict(section=section, code_commit=commit, task=args.task, export_task=args.export_task or args.task,
                    date=args.date.isoformat(), models=args.model, files={},
                    skill_sha256={p: sha256(check_output([*git, "show", f"{commit}:{p}"])).hexdigest()
                                 for p in skills if p.endswith("/SKILL.md")})
    golden.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=golden, prefix=".record-") as staging:
        tmp = Path(staging) / "accepted"
        tmp.mkdir()
        for field, read in (("docx_file", word), ("xlsx_file", excel)):
            filename = cfg[field]
            copyfile(args.case / "out" / filename, tmp / filename)
            read(tmp / filename)
            manifest["files"][field] = dict(name=filename, sha256=digest(tmp / filename))
        (tmp / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.rename(target)
    print(target)


def compare(case, golden, cfg):
    folders = sorted(p for p in golden.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not folders:
        raise SystemExit(f"No accepted baselines in {golden}")
    baselines = []
    for folder in folders:
        data = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        for field in ("docx_file", "xlsx_file"):
            info = data["files"][field]
            if digest(folder / info["name"]) != info["sha256"]:
                raise SystemExit(f"Baseline hash mismatch: {folder / info['name']}")
        baselines.append((folder, data))
        print("Check:", check(case, data["section"]))
    export_docx(case)
    changed = False
    for folder, data in baselines:
        print("\nAccepted section:", data["section"])
        for field, compare_files in (("docx_file", compare_word), ("xlsx_file", compare_excel)):
            counts, differences = compare_files(folder / data["files"][field]["name"], case / "out" / cfg[field])
            print(counts)
            print("\n".join(differences) if differences else "No differences")
            changed |= bool(differences)
    return int(changed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("record")
    capture.add_argument("case", type=Path)
    capture.add_argument("section")
    capture.add_argument("--commit", required=True)
    capture.add_argument("--task", type=int, required=True)
    capture.add_argument("--export-task", type=int)
    capture.add_argument("--date", type=date.fromisoformat, required=True)
    capture.add_argument("--model", action="append", required=True, help="phase and actual recorded model")
    commands.add_parser("compare").add_argument("case", type=Path)
    args = parser.parse_args()
    golden, cfg = args.case / "work/golden", _settings(args.case)
    if args.command == "record":
        return record(args, golden, cfg)
    return compare(args.case, golden, cfg)


if __name__ == "__main__":
    raise SystemExit(main())
