import argparse
import base64
import json
from hashlib import file_digest, sha256
from itertools import count
import re
import unicodedata
from datetime import datetime, timezone
from difflib import SequenceMatcher
from decimal import Decimal
from pathlib import Path
from shutil import copytree
from tempfile import TemporaryDirectory, mkdtemp
import sys

from httpx import NetworkError, RemoteProtocolError, TimeoutException
from langgraph.checkpoint.memory import InMemorySaver
from openai import APIConnectionError

from langchain.agents.middleware import ModelCallLimitMiddleware, wrap_model_call
from langchain_core.messages import AIMessage, HumanMessage
from langsmith import tracing_context

from . import learn
from .agent import PHASE_SKILLS, build_agent, score_result, supplied_notice
from .budget import TokenCapMiddleware, estimate_input
from .export.docx import _SCENARIO, _VALUE, _risk_statements, clarification_numbers, export_docx, section_flags
from .ingest import ingest
from .models import PHASE_SETTINGS, make_model


GATHER_BUDGET_FRACTION = 0.6
REVISION_CAPS = {"verify-sources": 80000, "review": 100000, "clarify": 40000, "revise": 80000}

REPLAY_CAPS = {"verify-sources": 90000, "review": 100000, "clarify": 100000, "revise": 130000}
REPLAY_BATCHES = {"verify-review": ("verify-sources", "review"), "clarify-revise": ("clarify", "revise")}

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
    numbers = count(1)
    numbered = re.sub(r"(?m)^- ", lambda _: f"{next(numbers)}. ", top)
    blocks = [{"type": "text", "text": numbered}]
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
    supplied = []
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
                supplied.append(page.relative_to(root).as_posix())
                available_bytes -= size
            except (OSError, UnicodeError) as error:
                blocks.append({"type": "text", "text": f"{label} unavailable ({type(error).__name__})"})
        attached += len(blocks) > before

    return HumanMessage(content=blocks if len(blocks) > 1 else numbered, response_metadata={"supplied_files": supplied})


def _revision_messages(case, name, phase, prompt=None):
    if prompt is None:
        prompt = {
            "verify-sources": f"verify-sources for section {name} using the supplied list from /work/checks/{name}.md. "
                              f"Save the source checks to /work/reviews/{name}-sources.md.",
            "review": f"review section {name} using the supplied case files. Save the review to /work/reviews/{name}.md.",
            "clarify": f"clarify section {name}: complete /work/clarifications/{name}.md "
                       f"from /work/reviews/{name}.md, /work/sections/{name}.md and /commission/澄清答复.md.",
            "revise": f"revise section {name} using /work/reviews/{name}.md and the completed "
                      f"/work/clarifications/{name}.md. Save edits to /work/sections/{name}.md.",
        }[phase]
    if phase == "verify-sources":
        checklist = f"work/checks/{name}.md"
        text = (case / checklist).read_text(encoding="utf-8")
        top = text.partition("\n## Findings")[0]
        message = _verification_input(case, name, top)
        if top == text:
            message.response_metadata["supplied_files"].insert(0, checklist)
        return [message, HumanMessage(content=prompt)]
    return [_revision_input(case, name, phase, prompt)]


def _revision_input(case, name, phase, prompt):
    root = case.resolve()
    review = f"work/reviews/{name}.md"
    section = f"work/sections/{name}.md"
    official = f"work/clarifications/{name}.md"
    sources = f"work/reviews/{name}-sources.md"
    paths = [sources if phase == "review" else review, section, official]
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

    if phase == "review":
        pattern = rf"^##[ \t]+{re.escape(name)}[ \t]*(?:\n|\Z).*?(?=^#{{1,2}}[ \t]+|\Z)"
        entry = re.search(pattern, read("work/plan.md") or "", re.MULTILINE | re.DOTALL)
        blocks.append("Supplied section entry from /work/plan.md:\n" + entry[0].rstrip("\n") if entry
                      else f"No entry for ## {name} in /work/plan.md.")

    if phase == "revise":
        flags = list(section_flags(root, name))
        if flags:
            blocks.append("Internal file mentions to review for client-facing wording:\n" + "\n".join(flags))
    conflicts = {}
    labels = {}
    if phase == "clarify":
        labels = {review: "Review findings", section: "Section draft", official: "Official clarifications",
                  "commission/澄清答复.md": "Owner answers"}
        blocks.append("Use the text supplied under each labelled file path below. Missing files are marked unavailable.")
        next_number, conflicts = clarification_numbers(case)
        blocks.append(f"Next free clarification number: {next_number}.")
        check_path = f"work/checks/{name}.md"
        check = read(check_path) or ""
        flags = [line for line in check.splitlines() if re.match(r"^clarification (?:\d+|\?) line \d+: F/grade ", line)]
        if flags:
            blocks.append(f"Cross-file F/grade observations from /{check_path} (excerpt):\n" + "\n".join(flags))
        folder = root / "work/clarifications"
        for path in sorted(folder.glob("*.md")):
            if path.name.startswith(f"{name}.pending."):
                relative = path.relative_to(root).as_posix()
                paths.append(relative)
                labels[relative] = "Pending clarifications"
    else:
        paths.append(f"work/notes/{name}.md")
    paths.append("commission/澄清答复.md")

    for relative in paths:
        text = read(relative)
        if text is not None and phase in ("review", "revise") and relative == section:
            text = "\n".join(f"{line_no}\t{line}" for line_no, line in enumerate(text.splitlines(), 1))
        missing = "(unavailable: the verify phase saved nothing)" if relative == sources else "(unavailable)"
        blocks.append(f"{labels.get(relative, 'Case file')}: /{relative}\n" + (text if text is not None else missing))

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
    # Excerpts and missing-file notices do not supply the whole file.
    supplied = [p for p in paths if contents[p] is not None]
    return HumanMessage(content="\n\n".join(blocks), response_metadata={"supplied_files": supplied})


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
def _score_arithmetic(path):
    lines = [unicodedata.normalize("NFKC", re.sub(r"</?su[bp]>|\*\*|\*([FRX])\*", r"\1", line)).strip()
             for line in path.read_text(encoding="utf-8-sig").splitlines()]
    number = r"\d+(?:\.\d+)?"
    total = Decimal(0)
    rows = 0
    start = None
    for i, line in enumerate(lines):
        cells = [s.strip() for s in line.strip("|").split("|")]
        if start is None:
            if line.startswith("|") and "权重" in line and "指标" in line:
                score_col = next((j for j, c in enumerate(cells) if "Rij" in c and "Xij" not in c), None)
                if score_col is not None:
                    weight_col = next(j for j, c in enumerate(cells) if "权重" in c)
                    start = i
            continue
        if not line.startswith("|"):
            break
        if not any(re.match(r"X\d{2}(?!\d)", c) for c in cells[:2]):
            continue
        score = cells[score_col] if score_col < len(cells) else ""
        weight = cells[weight_col] if weight_col < len(cells) else ""
        adopted = (re.fullmatch(rf"({number})(?:/\s*{number}\s*[~～至–-]\s*{number})?", score)
                   or re.search(rf"中点\s*({number})\s*计基准", score))
        if not adopted or not re.fullmatch(number, weight):
            yield f"{path.stem} line {i+1}: score arithmetic unavailable for this row"
            return
        total += Decimal(adopted[1]) * Decimal(weight)
        rows += 1
    if not rows:
        if start is not None:
            yield f"{path.stem} line {start + 1}: score arithmetic unavailable; no indicator rows"
        return
    result = score_result(total)
    grade, display = result["grade"], result["F"]
    baseline = True
    compared = False
    continuation = False
    for n, line in enumerate(lines[i:], i + 1):
        if line.startswith("#"):
            baseline = any(word in line for word in ("计算", "结论", "等级判定"))
            continuation = False
        if re.match(r"^(?:\(\d+\)\s*)?敏感性", line) and (line.endswith(":") or line in ("敏感性分析", "敏感性核查")):
            baseline = False
        if not baseline or not line or line.startswith(("#", "|")):
            continue
        line = re.sub(r"\(([^()]*?)(?:" + _SCENARIO + r")[^()]*\)", r"(\1)", line)
        statements = []
        for clause in re.split(r"[。；;]", line):
            clause = re.split(_SCENARIO, clause, maxsplit=1)[0]
            # A reported range or a list of thresholds is not a single adopted grade.
            clause = re.sub(r"(?:IV|III|II|I)级\s*[~～–—至]\s*(?:IV|III|II|I)级", "", clause)
            clause = clause.replace("风险等级判定为", "风险等级为")
            statements.extend((s, g) for _, s, g in _risk_statements(clause))
            tail = _VALUE.match(clause.rsplit("=", 1)[-1].lstrip()) if "=" in clause else None
            if tail and (re.search(r"\bF\s*=.*=", clause) or continuation and clause.startswith("=")):
                statements.append((Decimal(tail[0]), None))
        continuation = (bool(re.search(r"\bF\s*=|^=", line)) and bool(re.search(r"[×+∑Σ]", line)))
        compared |= bool(statements)
        mismatches = [(s, g) for s, g in dict.fromkeys(statements)
                      if (s is not None and abs(s - total) > Decimal("0.01")) or (g and g != grade)]
        if mismatches:
            stated = ", ".join(f"{s if s is not None else '—'}/{g or '—'}" for s, g in mismatches)
            yield f"{path.stem} line {n}: table F/grade {display}/{grade}; stated {stated}"
    if not compared:
        yield f"{path.stem}: table F/grade {display}/{grade}; no comparable baseline statement found"

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
    for number, line_no, score, grade, pairs, agrees in _grade_comparisons(case, name, section):
        if agrees is False:
            stated = ", ".join(dict.fromkeys(f"{s if s is not None else '—'}/{g or '—'}" for s, g in pairs))
            output.append(f"clarification {number} line {line_no}: F/grade {score if score is not None else '—'}/{grade or '—'}; "
                          f"section states {stated}")
    return output


def _grade_comparisons(case, name, section):
    clarifications = case / f"work/clarifications/{name}.md"
    if clarifications.is_file():
        section_values = list(dict.fromkeys(item for line in section for item in _risk_statements(line)))
        baseline = [item for item in section_values if item[0]]
        if not baseline:
            baseline = [item for item in section_values if item[0] is None]
        number = "?"
        for line_no, line in enumerate(clarifications.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("number:"):
                number = line.partition(":")[2].strip()
            for is_baseline, score, grade in _risk_statements(line):
                reference = baseline if is_baseline else section_values
                pairs = [(s, g) for _, s, g in reference]
                if is_baseline is False:
                    # Questions can introduce alternatives. Only compare them to the same written score or standalone grade.
                    pairs = [(s, g) for s, g in pairs if (s == score if score is not None else g == grade)]
                comparable = [(s, g) for s, g in pairs if (score is None or s is not None)
                              and (grade is None or g is not None)]
                if comparable:
                    differs = not any((score is None or score == s) and (grade is None or grade == g) for s, g in comparable)
                else:
                    scores = {s for s, _ in pairs if s is not None}
                    grades = {g for _, g in pairs if g is not None}
                    differs = (score is not None and scores and score not in scores) or (grade and grades and grade not in grades)
                agrees = not differs if comparable or differs else None
                yield number, line_no, score, grade, pairs, agrees


def check(case, name, *, output=None):
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
    output = output or case / f"work/checks/{name}.md"
    output.parent.mkdir(parents=True, exist_ok=True)
    inventory = [f"- indicator {label} | " + ("; ".join(entries) or "(no file/page citation)") for label, entries in indicators.items()] + inventory
    comparison = _verification_comparison(case, name, section, len(inventory))
    material_flags = list(section_flags(case, name))
    _, conflicts = clarification_numbers(case)
    duplicates = [f"duplicate clarification {number}: " + "; ".join(locations)
                  for number, locations in conflicts.items()]
    output.write_text(
        "# Source pages to review\n" + "\n".join(inventory) + "\n\n## Findings\n"
        + "".join(line + "\n" for line in findings[:40])
        + f"\nFindings: total={len(findings)}; shown={min(40, len(findings))}; omitted={max(0, len(findings) - 40)}\n"
        + ("\n## Verification comparison\n" + "\n".join(comparison) + "\n" if comparison else "")
        + ("\n## Internal file mentions\n" + "\n".join(material_flags) + "\n" if material_flags else "")
        + ("\n## Duplicate clarification numbers\n" + "\n".join(duplicates) + "\n" if duplicates else ""),
        encoding="utf-8",
    )
    if PHASE_SETTINGS["checks"]["score_arithmetic"]:
        for finding in _score_arithmetic(case / section_path.lstrip("/")):
            print(finding)
    return output


def _preflight(case, section, phase, messages, cap):
    settings = PHASE_SETTINGS[phase]
    reserve, expected = settings["output_reserve"], settings["thinking_budget"]

    @wrap_model_call
    def inspect_request(request, handler):
        prompt = ([request.system_message] if request.system_message else []) + request.messages
        first = estimate_input(prompt, request.tools, request.model)
        # k inputs, k-1 estimated completed outputs, one reserve for the final send.
        affordable = max(0, (cap - reserve + expected) // (first + expected))
        conservative = max(0, cap // (first + reserve))
        print(f"phase={phase} cap={cap} estimated_first_input={first} output_reserve={reserve} "
              f"expected_output={expected} affordable_requests={affordable} conservative_requests={conservative} "
              f"conservative_floor_3={3 * (first + reserve)}")
        if conservative < 2:
            print(f"{phase}: conservative estimate affords fewer than 2 requests.")
        return AIMessage(content="Preflight complete.")

    with tracing_context(enabled=False):
        agent = build_agent(case, make_model(phase, offline=True), middleware=[inspect_request], phase=phase,
                            section=section, supplied_files=messages[0].response_metadata.get("supplied_files", ()))
        agent.invoke({"messages": messages})


def _run_phase(case, phase, messages, limit, folder, previous_total=0, section=None, receipt=None):
    state = {"messages": messages}
    calls = ModelCallLimitMiddleware(run_limit=200, exit_behavior="end")
    log = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{phase}.jsonl")
    if receipt is not None:
        before = learn.snapshot(case, section, check, _risk_statements)
        observation = learn.begin_phase(receipt, phase, log, limit, before)
    cap = TokenCapMiddleware(limit, remind=phase in ("plan", "gather", "verify-sources", "review"),
                             phase=phase if phase in REVISION_CAPS else None, info=True, guard=True,
                             request_log=log.with_suffix(".requests.jsonl"))
    config = {"configurable": {"thread_id": log.stem}, "max_concurrency": 1}
    resume = None
    try:
        model = make_model(phase)
        agent = build_agent(case, model, middleware=[cap, calls], checkpointer=InMemorySaver(),
                            phase=phase if phase in REVISION_CAPS else None, section=section,
                            supplied_files=messages[0].response_metadata.get("supplied_files", ()))
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
        if receipt is not None:
            after = learn.snapshot(case, section, check, _risk_statements)
            learn.finish_phase(receipt, observation, log, after)
        used = state.get("total_tokens", 0)
        print(f"{phase} phase tokens: {used}")
        print(f"Reported tokens: {previous_total + used}")
        print("Run ended:", "error" if sys.exception() else state["messages"][-1].text)
    return log, used


def _replay_metrics(case, name, phase, criteria, before, transcript):
    target = {"verify-sources": f"work/reviews/{name}-sources.md", "review": f"work/reviews/{name}.md",
              "clarify": f"work/clarifications/{name}.md", "revise": f"work/sections/{name}.md"}[phase]
    turns = learn.recorded_turns(transcript)
    edits = applied = rejected = 0
    saves = []
    for turn, call, success in learn.file_operations(turns):
        if call["name"] == "edit_file":
            edits += 1
            applied += int(success is True)
            rejected += int(success is False)
        if success and Path(call["args"].get("file_path", "").lstrip("/")).as_posix() == target:
            saves.append(turn)

    section = (case / f"work/sections/{name}.md").read_text(encoding="utf-8").splitlines()
    agreement = [row[-1] for row in _grade_comparisons(case, name, section)]
    usage = [m.get("usage_metadata") or {} for m, _ in turns]
    totals = [u.get("total_tokens") for u in usage]
    reasoning = [(u.get("output_token_details") or {}).get("reasoning") for u in usage]
    scores = dict(edits_submitted=edits, edits_applied=applied, edits_rejected=rejected,
                  target_saved=int(bool(saves)), first_response_saved=int(1 in saves),
                  duplicate_numbers=len(clarification_numbers(case)[1]), f_grade_agreements=agreement.count(True),
                  f_grade_differences=agreement.count(False), total_tokens=sum(totals) if None not in totals else None,
                  reasoning_tokens=sum(reasoning) if None not in reasoning else None)
    details = dict(target=target, saved_on_responses=sorted(set(saves)), provider_responses=len(turns),
                   edits_without_result=edits - applied - rejected, f_grade_unpaired=agreement.count(None),
                   reasoning_tokens_reported=sum(n for n in reasoning if n is not None),
                   reasoning_missing_responses=reasoning.count(None))
    if phase == "verify-sources":
        text = (case / target).read_text(encoding="utf-8") if saves else ""
        ids = set(map(int, re.findall(r"^\s*\|\s*(\d+)(?=\s|\||[.、(（])", text, re.M)))
        expected = set(criteria["expected_ids"])
        scores["source_ids_saved"] = len(expected & ids)
        details.update(saved_ids=sorted(ids), missing_ids=sorted(expected - ids), extra_ids=sorted(ids - expected))
    if phase == "revise":
        changes = {line for tag, a, b, _, _ in SequenceMatcher(None, before, section, autojunk=False).get_opcodes()
                   if tag != "equal" for line in range(a + 1, b + 1)}
        touched = [key for key, lines in criteria["finding_lines"].items() if saves and changes.intersection(lines)]
        scores["must_fix_touched"] = len(touched)
        details["touched_findings"] = touched
    return scores, details


def _fixture_digest(root):
    digest = sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        with path.open("rb") as stream:
            digest.update(file_digest(stream, "sha256").digest())
    return digest.hexdigest()


def _replay(case, phases, historical_input, section, receipt=None):
    case = case.resolve()
    root = case / "work/replay"
    packs = sorted(p for p in root.glob("*") if p.is_dir() and not p.name.startswith(".") and (section is None or p.name == section))
    if len(packs) > 1:
        sys.exit("Select one frozen section with --section.")
    if not packs:
        sys.exit(f"No frozen replay cases in {root}")
    jobs = []
    pack, = packs
    shared_hash = _fixture_digest(pack / "shared")
    for phase in phases:
        fixture = pack / phase
        criteria = json.loads((fixture / "criteria.json").read_text(encoding="utf-8"))
        baseline = json.loads((fixture / "baseline.json").read_text(encoding="utf-8"))
        manifest = json.loads((fixture / "manifest.json").read_text(encoding="utf-8"))
        expected = manifest["files"]
        files_match = all(sha256((fixture / f).read_bytes()).hexdigest() == expected[f]
                          for f in ("inputs.jsonl", "criteria.json", "baseline.json"))
        if not files_match or shared_hash != manifest["shared_sha256"] or _fixture_digest(fixture / "state") != manifest["state_sha256"]:
            sys.exit(f"Frozen replay input changed: {fixture}")
        jobs.append((phase, criteria, baseline))

    output = Path(mkdtemp(prefix="reportagent-replay-", dir=case.parent))
    print("Replay output:", output)
    reports = []
    remaining = sum(REPLAY_CAPS[phase] for phase in phases)
    name = pack.name
    if receipt is not None:
        receipt.update(section=name, cap=remaining)
    for phase, criteria, baseline in jobs:
        with TemporaryDirectory(dir=output) as directory:
            clone = Path(directory) / "case"
            copytree(pack / "shared", clone)
            copytree(pack / phase / "state", clone, dirs_exist_ok=True)
            before = (clone / f"work/sections/{name}.md").read_text(encoding="utf-8").splitlines()
            logs = output / name
            logs.mkdir(exist_ok=True)
            if historical_input:
                messages = [HumanMessage.model_validate_json(line) for line in
                            (pack / phase / "inputs.jsonl").read_text(encoding="utf-8").splitlines()]
            else:
                messages = _revision_messages(clone, name, phase)
            limit = min(REPLAY_CAPS[phase], remaining)
            log, used = _run_phase(clone, phase, messages, limit, logs, section=name, receipt=receipt)
            remaining = max(0, remaining - used)
            transcript = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            scores, details = _replay_metrics(clone, name, phase, criteria, before, transcript)
            requests = [json.loads(line) for line in log.with_suffix(".requests.jsonl").read_text(encoding="utf-8").splitlines()]
            stopped = any(row.get("decision") == "stop" for row in requests)
            details["execution_status"] = ("blocked_before_send" if stopped and not details["provider_responses"]
                                           else "stopped_by_guard" if stopped else "completed")
            comparison = {}
            for key, actual in scores.items():
                prior = baseline[key]
                if prior is None or actual is None:
                    change = "unknown"
                elif actual == prior:
                    change = "same"
                else:
                    lower = key in ("duplicate_numbers", "f_grade_differences", "edits_submitted",
                                    "edits_rejected", "total_tokens", "reasoning_tokens")
                    change = "better" if (actual < prior) == lower else "worse"
                comparison[key] = dict(baseline=prior, actual=actual, change=change)
            if phase == "revise":
                comparison["must_fix_touched"]["label"] = "edits touching finding lines"
            result = dict(section=name, phase=phase, phase_cap=limit, input_mode="historical" if historical_input else "current",
                          metrics=comparison, **details)
            reports.append(result)
            (output / "results.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(prog="reportagent")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "export", "plan", "run", "review", "revise", "check"):
        command = commands.add_parser(name)
        command.add_argument("case", type=Path)
        if name in ("plan", "run", "review", "revise"):
            command.add_argument("--max-tokens", type=int, required=True)
        if name in ("review", "revise", "check"):
            command.add_argument("--section", required=name != "check")
        if name == "check":
            command.add_argument("--score-arithmetic", action="store_true", help="only print score discrepancies; omit --section for all sections")
        if name == "revise":
            command.add_argument("--phases", metavar="LIST", help="Comma-separated phases in the revision workflow")
            command.add_argument("--preflight", action="store_true", help="estimate affordable requests without calling the model")
            command.add_argument("--phase-cap", action="append", default=[], metavar="NAME=N",
                                 help="Override one selected phase's token cap (repeatable)")
        if name == "run":
            command.add_argument("--write-only", action="store_true")
            command.add_argument("--section")
    replay = commands.add_parser("replay", help="compare one skill on frozen phase inputs")
    replay.add_argument("case", type=Path)
    selection = replay.add_mutually_exclusive_group(required=True)
    selection.add_argument("--skill", choices=sorted(set(PHASE_SKILLS.values())))
    selection.add_argument("--batch", choices=REPLAY_BATCHES)
    replay.add_argument("--section", help="exact frozen section name (required for a batch)")
    replay.add_argument("--historical-input", action="store_true", help="use the exact recorded leading messages")
    learn.add_parser(commands)
    for name in ("plan", "run", "review", "revise", "replay"):
        commands.choices[name].add_argument("--task", type=int, help="operator-supplied task number for learning records")
    args = parser.parse_args()
    if args.command == "learn":
        return learn.command(args)
    if args.command in ("plan", "run", "review", "revise", "replay") and not getattr(args, "preflight", False):
        with learn.observe_run(args.case, getattr(args, "section", None), args.command, getattr(args, "max_tokens", None), args.task) as receipt:
            return _execute(args, parser, receipt)
    return _execute(args, parser, None)


def _execute(args, parser, receipt):
    if args.command == "replay":
        if args.batch and not args.section:
            parser.error("--batch requires --section")
        phases = REPLAY_BATCHES[args.batch] if args.batch else [p for p, skill in PHASE_SKILLS.items() if skill == args.skill]
        return _replay(args.case, phases, args.historical_input, args.section, receipt)
    if args.command in ("ingest", "export"):
        print({"ingest": ingest, "export": export_docx}[args.command](args.case))
        return
    if args.command == "check":
        if args.score_arithmetic:
            folder = args.case / "work/sections"
            paths = [folder / f"{args.section}.md"] if args.section else sorted(folder.glob("*.md"))
            for path in paths:
                for finding in _score_arithmetic(path):
                    print(finding)
            return
        if not args.section:
            parser.error("check requires --section unless --score-arithmetic is selected")
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
    preflight = args.command == "revise" and args.preflight
    if preflight:
        print(f"Shared run cap={remaining}. Each phase uses current files and the available cap before other phases spend. "
              "Assume first input repeats and use the phase thinking budget as a proxy for each prior output. "
              "The conservative count uses output_reserve for each prior output. "
              "Estimate k with k * input + (k - 1) * expected_output + output_reserve <= cap.")
    else:
        folder.mkdir(parents=True, exist_ok=True)
    if args.command == "plan":
        phases = (("plan", "Plan the report requested in /commission/."),)
    elif args.command == "revise":
        phases = [(phase, None) for phase in REVISION_CAPS if phase in selected]
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
        messages = (_revision_messages(args.case, args.section, phase, prompt) if phase in REVISION_CAPS
                    else [HumanMessage(content=prompt)])
        first = messages[0]
        if supplied := first.response_metadata.get("supplied_files"):
            notice = supplied_notice(supplied)
            first.content = ([{"type": "text", "text": notice}, *first.content] if isinstance(first.content, list)
                             else notice + "\n\n" + first.content)
        if preflight:
            _preflight(args.case, args.section, phase, messages, limit)
            continue
        _, used = _run_phase(args.case, phase, messages, limit, folder, total, getattr(args, "section", None), receipt)
        total += used
        remaining = max(0, remaining - used)
