import argparse
import base64
import json
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys

from httpx import NetworkError, RemoteProtocolError, TimeoutException
from langgraph.checkpoint.memory import InMemorySaver
from openai import APIConnectionError

from langchain.agents.middleware import ModelCallLimitMiddleware
from langchain_core.messages import HumanMessage

from .agent import build_agent
from .budget import TokenCapMiddleware
from .export.docx import clarification_numbers, export_docx
from .ingest import ingest
from .models import make_model


GATHER_BUDGET_FRACTION = 0.6
REVISION_CAPS = {"verify-sources": 80000, "review": 100000, "clarify": 40000, "revise": 80000}


PAGE = re.compile(r"pages?\s*(\d+)(?:\s*[-–—~～]\s*(?:pages?\s*)?(\d+))?", re.I)
ABSENCE = re.compile(r"\b(?:missing|absent|not\s+found)\b|未(?:列|见|提供|找到|检索到|发现)|缺|不存在|无直接依据", re.I)


def _citations(text, previous=None):
    if previous is None:
        previous = {}
    for bracket in re.finditer(r"\[([^\]\n]+)\]", text):
        filename, offset = "", bracket.start() + 1
        for part in re.split("[;；]", bracket[1]):
            matches = list(PAGE.finditer(part))
            prefix = part[:matches[0].start()] if matches else part
            prefix = prefix.strip().rstrip(",，").strip()
            repeated = prefix in ("同上", "同上页")
            if matches or repeated:
                filename = previous.get("filename", "") if repeated else prefix or filename
                ranges = [(int(m[1]), int(m[2] or m[1])) for m in matches] if matches else previous.get("ranges", [])
                previous.update(filename=filename, ranges=ranges)
                yield offset, filename, ranges, {offset + m.start() for m in matches}
            offset += len(part) + 1


def _source_document(sources, filename):
    path = (sources / filename.removeprefix("/sources/").removeprefix("sources/")).resolve()
    if path.is_relative_to(sources) and path.is_file():
        return path


def _verification_input(case, name, top):
    root = case.resolve()
    sources = root / "sources"
    blocks = [{"type": "text", "text": top}]
    documents, claims, pages = {}, set(), {}
    pattern = r"(?: \| |; )(/work/(?:sections|notes)/" + re.escape(name) + r"\.md):(\d+):\d+ \| "
    # A location may appear first as an indicator, then as an absence entry needing its continuation.
    available = {}
    for entry in top.splitlines():
        for location in re.finditer(pattern, entry):
            path, number = location[1], int(location[2])
            if path not in documents:
                file = (root / path.lstrip("/")).resolve()
                if not file.is_relative_to(root / "work") or not file.is_file():
                    continue
                try:
                    documents[path] = file.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeError):
                    continue
            if not 1 <= number <= len(documents[path]):
                continue
            claim = documents[path][number - 1]
            if (path, number) not in claims:
                claims.add((path, number))
                blocks.append({"type": "text", "text": f"Claim to check ({path}:{number}):\n{claim}"})
            for _, filename, ranges, _ in _citations(claim):
                if not filename.lower().endswith(".pdf"):
                    continue
                pdf = _source_document(sources, filename)
                if not pdf or not pdf.with_suffix("").resolve().is_relative_to(sources):
                    continue
                if pdf not in available:
                    available[pdf] = sorted({int(p.stem[5:]) for p in pdf.with_suffix("").glob("page-*")
                                             if re.fullmatch(r"page-\d+\.(?:txt|png)", p.name)})
                for start, end in ranges:
                    if 1 <= start <= end:
                        last = end + int(entry.startswith("- absence "))
                        for page_number in available[pdf]:
                            if start <= page_number <= last:
                                pages.setdefault((pdf, page_number), filename)

    # Bound initial request size; remaining pages are available to read_file.
    available_bytes = 8 * 1024 * 1024
    attached = 0
    for (pdf, start), filename in pages.items():
        if attached == 10:
            break
        before = len(blocks)
        needs_image = True
        for ext in ("txt", "png"):
            if ext == "png" and not needs_image:
                continue
            page = pdf.with_suffix("") / f"page-{start:03d}.{ext}"
            if not page.resolve().is_relative_to(sources) or not page.is_file():
                continue
            label = f"Source: {filename}, PDF page {start} (/{page.relative_to(root).as_posix()})"
            try:
                size = page.stat().st_size
                if size > available_bytes:
                    if ext == "txt":
                        needs_image = False
                    continue
                if ext == "txt":
                    page_text = page.read_text(encoding="utf-8")
                    needs_image = len(page_text.strip()) < 300
                    blocks.append({"type": "text", "text": label + "\n" + page_text})
                else:
                    image = base64.b64encode(page.read_bytes()).decode("ascii")
                    blocks.extend([{"type": "text", "text": label},
                                   {"type": "image", "mime_type": "image/png", "base64": image}])
                available_bytes -= size
            except (OSError, UnicodeError) as error:
                blocks.append({"type": "text", "text": f"{label} unavailable ({type(error).__name__})"})
        attached += len(blocks) > before

    return HumanMessage(content=blocks if len(blocks) > 1 else top)


def _revision_input(case, name, phase, prompt):
    root = case.resolve()
    review = f"work/reviews/{name}.md"
    section = f"work/sections/{name}.md"
    official = f"work/clarifications/{name}.md"
    paths = [review, section, official]
    blocks = [prompt]
    contents = {}

    def read(relative):
        if relative not in contents:
            path = (root / relative).resolve()
            text = None
            if path.is_relative_to(root) and path.is_file():
                text = path.read_text(encoding="utf-8-sig", errors="replace")
            contents[relative] = text
        return contents[relative]

    conflicts = {}
    if phase == "clarify":
        next_number, conflicts = clarification_numbers(case)
        blocks.append(f"Next free clarification number: {next_number}.")
        folder = root / "work/clarifications"
        paths.extend(path.relative_to(root).as_posix() for path in sorted(folder.glob("*.md"))
                     if path.name.startswith(f"{name}.pending."))
        paths.append("commission/澄清答复.md")
    else:
        paths.append(f"work/notes/{name}.md")

    for relative in paths:
        text = read(relative)
        if text is not None and phase == "revise" and relative == section:
            text = "\n".join(f"{line_no}\t{line}" for line_no, line in enumerate(text.splitlines(), 1))
        blocks.append(f"Case file: /{relative}\n" + (text if text is not None else "(unavailable)"))

    for number, locations in conflicts.items():
        for location in locations:
            label, _, line_no = location.rpartition(":")
            relative = label.lstrip("/")
            text = read(relative)
            excerpt = "(unavailable)"
            if text is not None:
                lines = text.splitlines()
                start = int(line_no) - 1
                end = start + 1
                if relative.startswith("work/clarifications/"):
                    while start > 0 and lines[start - 1].strip() != "---":
                        start -= 1
                    while end < len(lines) and lines[end].strip() != "---":
                        end += 1
                excerpt = "\n".join(lines[start:end])
            blocks.append(f"Duplicate clarification {number}: {location}\n{excerpt}")
    return "\n\n".join(blocks)


def _numbers(text):
    text = unicodedata.normalize("NFKC", text).replace("∠", "<").replace("−", "-")
    text = re.sub(r"(?<=\d)\s+(?=\.)|(?<=\.)\s+(?=\d)", "", text)
    return set(re.findall(r"(?<!\d)-?\d+(?:\.\d+)?", text))


def _full_source_reads(case, name):
    sources, opened, coverage = (case / "sources").resolve(), set(), {}
    for log in sorted((case / "work/runs").glob("*-gather.jsonl")):
        rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        first = next((r.get("content", "") for r in rows if r.get("type") == "human"), "")
        if f"/work/notes/{name}.md" not in first:
            continue
        pending = {}
        for row in rows:
            pending.update({call["id"]: call for call in row.get("tool_calls", [])})
            call = pending.get(row.get("tool_call_id"))
            if not call or call["name"] != "read_file" or row.get("status") != "success":
                continue
            path = (case / call["args"]["file_path"].lstrip("/")).resolve()
            if not path.is_relative_to(sources) or not path.is_file():
                continue
            content = row.get("content", "")
            if path.suffix == ".png" and isinstance(content, list):
                if any(block.get("type") in ("image", "image_url") for block in content):
                    opened.add(path.with_suffix(".txt"))
            if path.suffix != ".txt" or not isinstance(content, str):
                continue
            lines = path.read_text(encoding="utf-8").splitlines()
            if not lines and content == "System reminder: File exists but has empty contents":
                opened.add(path)
            header = re.match(r"@@ lines (\d+)-(\d+) of (\d+)(?: \|[^\n@]*)? @@\n", content)
            if header:
                start, end, total = map(int, header.groups())
                if 1 <= start <= end <= total == len(lines) and content[header.end():].splitlines() == lines[start - 1:end]:
                    coverage.setdefault(path, set()).update(range(start, end + 1))
                    if len(coverage[path]) == total:
                        opened.add(path)
    return opened



# Compare written statements only. Never derive a risk grade or evaluate a scoring formula.
_SCORE = re.compile(
    r"(?P<baseline>(?:算术|现|当前)?基准值|(?:算术|现|当前)基准|baseline(?: score)?)\s*[:=为]?\s*(?:F\s*[:=为]\s*)?"
    r"|(?<![A-Za-z0-9])F\s*(?:值\s*)?(?:[:=为]|is\b)\s*", re.I)
_VALUE = re.compile(r"-?\d+(?:\.\d+)?(?![\d.]|\s*[+*/×÷~～−-])")
_GRADE = r"(?P<grade>IV|III|II|I|[1-4])(?:\s*级|\b)"
_GRADE_AFTER = re.compile(r"\s*(?:[>,≥≤<]\s*\d+(?:\.\d+)?)?[\s,，、:(]*(?:对应|为|属(?:于)?|grade\s*)?\s*" + _GRADE, re.I)
_GRADE_ONLY = re.compile(r"(?:风险等级|基准等级|risk grade|grade)\s*[:=为]?\s*" + _GRADE, re.I)


def _written_grade(match):
    grade = match["grade"].upper()
    return {"1": "I", "2": "II", "3": "III", "4": "IV"}.get(grade, grade)


def _risk_statements(line):
    line = unicodedata.normalize("NFKC", line)
    covered = set()
    for label in _SCORE.finditer(line):
        value = _VALUE.match(line, label.end())
        if not value:
            continue
        grade = _GRADE_AFTER.match(line, value.end())
        if grade:
            covered.add(grade.start("grade"))
        yield bool(label["baseline"]), Decimal(value[0]), _written_grade(grade) if grade else None
    for grade in _GRADE_ONLY.finditer(line):
        if grade.start("grade") not in covered:
            yield "基准" in grade[0], None, _written_grade(grade)


def _verification_comparison(case, name, section, count):
    output = []
    note = case / f"work/reviews/{name}-sources.md"
    if note.is_file():
        rows = re.findall(r"^\s*\|\s*(\d+)(?=\s|\||[.、(（])", note.read_text(encoding="utf-8"), re.M)
        ids = set(map(int, rows))
        expected = set(range(1, count + 1))
        output.append(f"checklist entries={count}; distinct source-check IDs={len(ids)}; "
                      f"missing IDs={','.join(map(str, sorted(expected - ids))) or 'none'}; "
                      f"extra IDs={','.join(map(str, sorted(ids - expected))) or 'none'}; numbered rows={len(rows)}")
    clarifications = case / f"work/clarifications/{name}.md"
    if clarifications.is_file():
        section_values = list(dict.fromkeys(item for line in section for item in _risk_statements(line)))
        baseline = [item for item in section_values if item[0]]
        number = "?"
        for line_no, line in enumerate(clarifications.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("number:"):
                number = line.partition(":")[2].strip()
            for is_baseline, score, grade in _risk_statements(line):
                reference = baseline if is_baseline and baseline else section_values
                pairs = [(s, g) for _, s, g in reference]
                comparable = [(s, g) for s, g in pairs if (score is None or s is not None)
                              and (grade is None or g is not None)]
                if comparable:
                    differs = not any((score is None or score == s) and (grade is None or grade == g) for s, g in comparable)
                else:
                    scores = {s for s, _ in pairs if s is not None}
                    grades = {g for _, g in pairs if g is not None}
                    differs = (score is not None and scores and score not in scores) or (grade and grades and grade not in grades)
                if differs:
                    stated = ", ".join(dict.fromkeys(f"{s if s is not None else '—'}/{g or '—'}" for s, g in pairs))
                    output.append(f"clarification {number} line {line_no}: F/grade {score if score is not None else '—'}/{grade or '—'}; "
                                  f"section states {stated}")
    return output


def check(case, name):
    case, findings = Path(case), []
    sources = (case / "sources").resolve()
    page_files, page_numbers = {}, {}
    indicators, inventory = {}, []

    def listed_pages(text, refs, location):
        # shortcut: recognise Xnn labels in pipe tables; extend for another indicator syntax.
        indicator = re.search(r"\bX\d{2}\b", text) if text.lstrip().startswith("|") else None
        labels = [f"absence {match[0]}" for match in ABSENCE.finditer(text)]
        citations = "; ".join(
            f"[{filename or '(unspecified)'}, " + ", ".join(f"page {a:03d}" + (f"–{b:03d}" if a != b else "") for a, b in ranges) + "]"
            for _, filename, ranges, _ in refs
        ) or "(no file/page citation)"
        if indicator:
            entries = indicators.setdefault(indicator[0], [])
            if refs:
                entries.append(f"{location} | {citations}")
        inventory.extend(f"- {label} | {location} | {citations}" for label in labels)

    def pages(filename, ranges, location):
        if filename not in page_files:
            source = _source_document(sources, filename)
            page_files[filename] = None
            if source is not None:
                page_files[filename] = {
                    int(p.stem[5:]): p.resolve() for p in source.with_suffix("").glob("page-*.txt")
                    if re.fullmatch(r"page-\d+", p.stem) and p.resolve().is_relative_to(sources)
                }
        available = page_files[filename]
        if available is None:
            findings.append(f"{location} | source file not found in /sources: {filename or '(unspecified)'}")
            return []
        selected = set()
        for start, end in ranges:
            found = {p for n, p in available.items() if start <= n <= end}
            if start > end or len(found) != end - start + 1:
                findings.append(f"{location} | page {start:03d}–{end:03d}: missing page text or reversed range in {filename}")
            selected.update(found)
        return sorted(selected)

    section_path = f"/work/sections/{name}.md"
    previous = {}
    section = (case / section_path.lstrip("/")).read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(section, 1):
        listed_pages(line, list(_citations(line, previous.copy())), f"{section_path}:{line_number}:1")
        for sentence in re.finditer(r".+?(?:[。！？!?]|\.(?=\s|$)|$)", line):
            refs = list(_citations(sentence[0], previous))
            if not refs:
                continue
            evidence = set()
            for offset, filename, ranges, _ in refs:
                location = f"{section_path}:{line_number}:{sentence.start() + offset + 1}"
                for page in pages(filename, ranges, location):
                    if page not in page_numbers:
                        text = page.read_text(encoding="utf-8")
                        page_numbers[page] = _numbers(text) | _numbers(re.sub(r"\s+", "", text))
                    evidence.update(page_numbers[page])
            body = re.sub(r"\[[^\]\n]*\]", "", sentence[0])
            body = re.sub(r"第\s*\d+(?:\s*[、，,～~–—-]\s*\d+)*\s*项|\bitem\s+\d+\b|\bX\d{2}\b", "", body, flags=re.I)
            for number in sorted(_numbers(body) - evidence):
                findings.append(f"{section_path}:{line_number}:{sentence.start() + 1} | number {number} not found on the cited page texts")

    opened = _full_source_reads(case, name)
    # shortcut: recognise explicit absence markers; extend them when new wording appears.
    notes_path = f"/work/notes/{name}.md"
    previous = {}
    for line_number, line in enumerate((case / notes_path.lstrip("/")).read_text(encoding="utf-8").splitlines(), 1):
        refs = list(_citations(line, previous))
        location = f"{notes_path}:{line_number}:1"
        listed_pages(line, refs, location)
        if (line.lstrip().startswith("#") and not PAGE.search(line)) or not ABSENCE.search(line):
            continue
        positions = set()
        for _, filename, ranges, offsets in refs:
            positions.update(offsets)
            for page in pages(filename, ranges, location):
                if page not in opened:
                    findings.append(f"{location} | no full read_file delivery in this section's gather logs: /{page.relative_to(case.resolve())}")
        bare = [m for m in PAGE.finditer(line) if m.start() not in positions]
        for match in bare:
            findings.append(f"{location} | source file unspecified for absence wording: {match[0]}")
        if not refs and not bare:
            findings.append(f"{location} | no explicit file/page citation for absence wording")
    output = case / f"work/checks/{name}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    inventory = [f"- indicator {label} | " + ("; ".join(entries) or "(no file/page citation)") for label, entries in indicators.items()] + inventory
    comparison = _verification_comparison(case, name, section, len(inventory))
    _, conflicts = clarification_numbers(case)
    duplicates = [f"duplicate clarification {number}: " + "; ".join(locations)
                  for number, locations in conflicts.items()]
    output.write_text(
        "# Source pages to review\n" + "\n".join(inventory) + "\n\n## Findings\n"
        + "".join(line + "\n" for line in findings[:40])
        + f"\nFindings: total={len(findings)}; shown={min(40, len(findings))}; omitted={max(0, len(findings) - 40)}\n"
        + ("\n## Verification comparison\n" + "\n".join(comparison) + "\n" if comparison else "")
        + ("\n## Duplicate clarification numbers\n" + "\n".join(duplicates) + "\n" if duplicates else ""),
        encoding="utf-8",
    )
    return output


def main():
    parser = argparse.ArgumentParser(prog="reportagent")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "export", "plan", "run", "review", "revise", "check"):
        command = commands.add_parser(name)
        command.add_argument("case", type=Path)
        if name in ("plan", "run", "review", "revise"):
            command.add_argument("--max-tokens", type=int, required=True)
        if name in ("review", "revise", "check"):
            command.add_argument("--section", required=True)
        if name == "revise":
            command.add_argument("--phases", metavar="LIST", help="Comma-separated phases in the revision workflow")
            command.add_argument("--phase-cap", action="append", default=[], metavar="NAME=N",
                                 help="Override one selected phase's token cap (repeatable)")
        if name == "run":
            command.add_argument("--write-only", action="store_true")
            command.add_argument("--section")
    args = parser.parse_args()
    if args.command in ("ingest", "export"):
        print({"ingest": ingest, "export": export_docx}[args.command](args.case))
        return
    if args.command == "check":
        print(check(args.case, args.section))
        return
    phase_caps = REVISION_CAPS.copy()
    if args.command == "revise":
        selected = [s.strip() for s in args.phases.split(",")] if args.phases is not None else list(phase_caps)
        if len(set(selected)) != len(selected) or any(s not in phase_caps for s in selected):
            parser.error("--phases needs distinct names from " + ", ".join(phase_caps))
        overrides = {}
        for spec in args.phase_cap:
            name, _, value = spec.partition("=")
            if name not in selected or name in overrides or not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
                parser.error("--phase-cap needs one selected NAME=positive_integer per override")
            overrides[name] = int(value)
        phase_caps.update(overrides)
    remaining = args.max_tokens
    total = 0
    folder = args.case / "work/runs"
    folder.mkdir(parents=True, exist_ok=True)
    if args.command == "plan":
        phases = (("plan", "Plan the report requested in /commission/."),)
    elif args.command == "revise":
        name = args.section
        phases = (
            ("verify-sources", f"verify-sources for section {name} using the supplied list from /work/checks/{name}.md. "
             f"Save the source checks to /work/reviews/{name}-sources.md."),
            ("review", f"review section {name}: read /work/reviews/{name}-sources.md first. "
             f"Save the review to /work/reviews/{name}.md."),
            ("clarify", f"clarify section {name}: complete /work/clarifications/{name}.md "
             f"from /work/reviews/{name}.md, /work/sections/{name}.md and /commission/澄清答复.md."),
            ("revise", f"revise section {name} using /work/reviews/{name}.md and the completed "
             f"/work/clarifications/{name}.md. Save edits to /work/sections/{name}.md."),
        )
        phases = [(phase, prompt) for phase, prompt in phases if phase in selected]
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
        if phase in REVISION_CAPS:
            limit = min(limit, phase_caps[phase])
        cap = TokenCapMiddleware(limit, remind=phase in ("plan", "gather", "verify-sources", "review"),
                                 phase=phase if phase in REVISION_CAPS else None)
        calls = ModelCallLimitMiddleware(run_limit=200, exit_behavior="end")
        if phase in ("clarify", "revise"):
            prompt = _revision_input(args.case, args.section, phase, prompt)
        state = {"messages": [HumanMessage(content=prompt)]}
        if phase == "verify-sources":
            top = (args.case / f"work/checks/{args.section}.md").read_text(encoding="utf-8").partition("\n## Findings")[0]
            state["messages"].insert(0, _verification_input(args.case, args.section, top))
        log = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{phase}.jsonl")
        config = {"configurable": {"thread_id": log.stem}, "max_concurrency": 1}
        resume = None
        try:
            model = make_model(phase)
            agent = build_agent(args.case, model, middleware=[cap, calls], checkpointer=InMemorySaver(),
                                phase=phase if phase in REVISION_CAPS else None)
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
            if phase == "verify-sources":
                for i, message in enumerate(state["messages"]):
                    if message.type == "ai":
                        record = json.loads(lines[i])
                        record["initial_request_input_tokens"] = (message.usage_metadata or {}).get("input_tokens")
                        lines[i] = json.dumps(record, ensure_ascii=False) + "\n"
                        break
            if resume:
                lines.insert(resume[0], resume[1] + "\n")
            log.write_text("".join(lines), encoding="utf-8")
            used = state.get("total_tokens", 0)
            total += used
            remaining = max(0, remaining - used)
            print(f"{phase} phase tokens: {used}")
            print(f"Reported tokens: {total}")
            print("Run ended:", "error" if sys.exception() else state["messages"][-1].text)
