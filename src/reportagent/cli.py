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
from shutil import copyfile, copytree
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
from .budget import CALCULATE_FIRST, TokenCapMiddleware, estimate_input
from .export.docx import (FIXED_FIELDS, _SCENARIO, _VALUE, _plan, _risk_statements, _settings, answered_client_rows,
                          clarification_numbers, export_answers, export_docx, fixed_values, section_flags)
from .ingest import ingest
from .models import PHASE_SETTINGS, make_model


GATHER_BUDGET_FRACTION = 0.6
REVISION_CAPS = {"verify-sources": 80000, "review": 100000, "clarify": 40000, "revise": 80000}

STAGE_PHASES = {"stage1": ("plan", "gather", "write", "verify-sources", "review", "clarify"), "stage2": ("revise", "rescore", "sync")}

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

_SCORE_IDS = ("X11", "X12", "X21", "X22", "X23", "X31", "X32", "X41", "X42", "X51", "X52")
_DECIMAL_CELL = r"(?:\d+(?:\.\d+)?|\.\d+)"


def _score_text(line):
    return unicodedata.normalize("NFKC", re.sub(r"</?su[bp]>|\*\*|\*([RXF])\*", r"\1", line)).strip()


def _scoring_table(lines):
    for start, raw in enumerate(lines):
        if not raw.lstrip().startswith("|"):
            continue
        cells = _score_text(raw).strip("|").split("|")
        columns = [next((i for i, c in enumerate(cells) if re.search(pattern, c, re.I)), None)
                   for pattern in (r"指标|indicator", r"\bR(?:ij)?\b", r"权重|γ|weight", r"\bX(?:ij)?\b")]
        if None in columns or len(set(columns)) != 4:
            continue
        end = start + 1
        while end < len(lines) and lines[end].lstrip().startswith("|"):
            end += 1
        return start, end, columns


def _score_excerpt(lines):
    frame = _scoring_table(lines)
    if not frame:
        return ""
    start, end, _ = frame
    excerpt = []
    include = False
    for i, line in enumerate(lines):
        relevant = re.search("适用性|取值说明", line)
        if re.match(r"^#{1,6}\s", line):
            include = bool(relevant)
        if start <= i < end or include or (line.startswith("|") and relevant):
            excerpt.append(line)
    return "\n".join(excerpt)


def _table_gate(text, handoff=None, calculations=None):
    """Compare numeric assertions to the saved baseline and named scenario overrides."""
    lines = text.splitlines()
    frame = _scoring_table(lines)
    if not frame:
        return ["line 1: no scoring table with indicator, R, weight and X columns"]
    start, end, (indicator, r_col, w_col, x_col) = frame
    errors = []
    if _scoring_table(lines[end:]):
        errors.append(f"line {end+1}: more than one scoring table in this section")
    values, weights = {}, {}
    summaries = []
    for n in range(start + 2, end):
        cells = [c.strip() for c in _score_text(lines[n]).strip("|").split("|")]
        if any("合计" in c or c.lower() == "total" for c in cells[:2]):
            summaries.append(n)
            continue
        match = re.search(r"\bX\s*(\d{2})(?!\d)", cells[indicator] if indicator < len(cells) else "", re.I)
        if (not match or max(r_col, w_col, x_col) >= len(cells) or
                not all(re.fullmatch(_DECIMAL_CELL, cells[i]) for i in (r_col, w_col, x_col))):
            errors.append(f"line {n+1}: expected one indicator and numeric baseline R, weight and X")
            continue
        key = "X" + match[1]
        r, w, x = (Decimal(cells[i]) for i in (r_col, w_col, x_col))
        if key in values or key not in _SCORE_IDS or not (0 <= r <= 100 and 0 <= w <= 1):
            errors.append(f"line {n+1}: duplicate/unknown indicator or out-of-range R/weight for {key}")
            continue
        values[key], weights[key] = r, w
        product = score_result(r*w)["F"]
        if x != Decimal(product):
            errors.append(f"line {n+1}: {key} weighted X should be {product}")
    if set(values) != set(_SCORE_IDS):
        errors.append(f"line {start+1}: expected the eleven distinct indicators")
    if sum(weights.values()) != 1:
        errors.append(f"line {start+1}: weights sum to {sum(weights.values())}, expected 1.00")
    if errors:
        return errors
    if handoff is not None and handoff.strip() not in text:
        errors.append(f"line {start+1}: section does not contain the complete checked table and results")

    def result(rows):
        return score_result(sum(v * weights[k] for k, v in rows.items()))

    baseline = result(values)
    scenarios = {}
    required = [values]
    label = r"情景[\[【]([^\]】]+)[\]】]"
    for n, raw in enumerate((handoff if handoff is not None else text).splitlines(), 1):
        line = _score_text(raw)
        scenario = re.match(label, line)
        if not scenario:
            continue
        changes = re.findall(r"\bR(\d{2})\s*=\s*(" + _DECIMAL_CELL + r")", line)
        if not changes:
            continue
        overrides = {"X" + k: Decimal(v) for k, v in changes}
        if (overrides.keys() - values.keys() or len(changes) != len(overrides)
                or any(v > 100 for v in overrides.values()) or scenario[1] in scenarios):
            errors.append(f"line {n}: invalid or duplicate scenario overrides for {scenario[1]}")
        else:
            required.append(values | overrides)
            scenarios[scenario[1]] = result(required[-1])
    if calculations is not None:
        for scores in required:
            if (scores, weights) not in calculations:
                errors.append(f"line {start+1}: baseline or scenario inputs missing from successful calculate_score results")
    seen_f = set()
    seen_grade = set()
    for n, raw in enumerate(lines):
        if n in (start, start+1):
            continue
        if start+1 < n < end and n not in summaries:
            raw = raw.strip().strip("|").split("|")[-1]
        for clause in re.split(r"[。；;]", _score_text(raw)):
            scenario = re.search(label, clause)
            name = scenario[1] if scenario else ""
            expected = scenarios.get(name) if name else baseline
            # Remove threshold definitions, retaining any adjacent statement about this slope.
            clause = re.sub(r"(?:IV|III|II|I)\s*级[^,，;。()]{0,10}F\s*[<>≤≥]\s*\d+(?:\.\d+)?", "", clause)
            clause = re.sub(r"(?:\d+(?:\.\d+)?\s*[<≤]\s*)?F\s*[<>≤≥]\s*\d+(?:\.\d+)?\s*(?:为|对应)?\s*(?:IV|III|II|I)级", "", clause)
            grades = re.findall(r"(?<![A-Za-z])((?:IV|III|II|I))\s*级", clause, re.I)
            statements = list(_risk_statements(clause))
            grades.extend(g for _, _, g in statements if g)
            scores = [value for _, value, _ in statements if value is not None]
            marks = list(re.finditer(r"(?<![A-Za-z])F\s*[=:为]\s*", clause, re.I))
            for m in marks.copy():
                formula = re.match(r"(?:[Σ∑]|sum\b)[^,，;。]*", clause[m.end():], re.I)
                if formula:
                    last = formula[0].rsplit("=", 1)
                    if len(last) == 2 and (v := re.match(_DECIMAL_CELL, last[1].strip())):
                        scores.append(Decimal(v[0]))
                    elif len(last) == 1:
                        marks.remove(m)
            if (len(marks) > len(scores) or re.search(r"\bF\s*=\s*[\d.]+\s*[~～至–—]\s*[\d.]+", clause)):
                errors.append(f"line {n+1}: ambiguous F statement; state each endpoint as a named scenario")
            if not scores and not grades:
                continue
            if expected is None:
                errors.append(f"line {n+1}: unknown scenario {name}")
                continue
            if scores:
                seen_f.add(name)
            if grades:
                seen_grade.add(name)
            if any(v != Decimal(expected["F"]) for v in scores) or any(g.upper() != expected["grade"] for g in grades):
                errors.append(f"line {n+1}: expected {name or 'baseline'} F={expected['F']}, {expected['grade']}级")
    for name in ("", *scenarios):
        if name not in seen_f or name not in seen_grade:
            errors.append(f"line {start+1}: missing F or grade for {name or 'baseline'}")
    return list(dict.fromkeys(errors))


def _calculated_inputs(log):
    inputs = []
    messages = [json.loads(line) for line in log.read_text().splitlines()]
    for reply, results in learn.recorded_turns(messages):
        for call in reply.get("tool_calls", []):
            response = results.get(call["id"], {})
            if call["name"] != "calculate_score" or response.get("status") != "success":
                continue
            result = json.loads(response["content"])
            if not result.get("base", {}).get("Xij"):
                continue
            args = call["args"]
            def decimals(items):
                return {k: Decimal(str(v)) for k, v in items.items()}
            base, weights = decimals(args["scores"]), decimals(args["weights"])
            inputs.append((base, weights))
            inputs.extend((base | decimals(changes), weights) for changes in args.get("scenarios") or [])
    return inputs


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

    floor = 0

    @wrap_model_call
    def inspect_request(request, handler):
        prompt = ([request.system_message] if request.system_message else []) + request.messages
        nonlocal floor
        first = estimate_input(prompt, request.tools, request.model)
        floor = 3 * (first + reserve)
        # k inputs, k-1 estimated completed outputs, one reserve for the final send.
        affordable = max(0, (cap - reserve + expected) // (first + expected))
        conservative = max(0, cap // (first + reserve))
        print(f"phase={phase} cap={cap} estimated_first_input={first} output_reserve={reserve} "
              f"expected_output={expected} affordable_requests={affordable} conservative_requests={conservative} "
              f"conservative_floor_3={floor}")
        if conservative < 2:
            print(f"{phase}: conservative estimate affords fewer than 2 requests.")
        return AIMessage(content="Preflight complete.")

    with tracing_context(enabled=False):
        agent = build_agent(case, make_model(phase, offline=True), middleware=[inspect_request], phase=phase,
                            section=section, supplied_files=messages[0].response_metadata.get("supplied_files", ()))
        agent.invoke({"messages": messages})
    return floor


def _propose(args, parser):
    case = args.case.resolve()
    library = args.library or case.parent.parent / "lessons"
    messages = [HumanMessage(content=learn.proposal_prompt(case, library))]
    floor = _preflight(case, None, "learn", messages, args.max_tokens)
    if args.preflight:
        return
    if args.max_tokens < floor:
        parser.error(f"learn needs a cap of at least {floor} for three conservative requests")
    runs = case / "work/runs"
    runs.mkdir(parents=True, exist_ok=True)
    target = case / "work/learn/proposals.md"
    with learn.observe_run(case, None, "learn propose", args.max_tokens, None) as receipt:
        if target.is_file():
            backup = target.with_name(f"proposals-{receipt['run_id']}.md")
            copyfile(target, backup)
        _run_phase(case, "learn", messages, args.max_tokens, runs, receipt=receipt)


def _run_phase(case, phase, messages, limit, folder, previous_total=0, section=None, receipt=None):
    state = {"messages": messages}
    calls = ModelCallLimitMiddleware(run_limit=200, exit_behavior="end")
    log = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + f"-{phase}.jsonl")
    if receipt is not None:
        before = learn.snapshot(case, section, check, _risk_statements)
        observation = learn.begin_phase(receipt, phase, log, limit, before)
    cap = TokenCapMiddleware(limit, remind=phase in ("plan", "gather", "verify-sources", "review"),
                             phase=phase if phase in PHASE_SKILLS else None, info=True, guard=True,
                             request_log=log.with_suffix(".requests.jsonl"))
    config = {"configurable": {"thread_id": log.stem}, "max_concurrency": 1}
    resume = None
    try:
        model = make_model(phase)
        agent = build_agent(case, model, middleware=[cap, calls], checkpointer=InMemorySaver(),
                            phase=phase, section=section,
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


PLAN_PROMPT = "Plan the report requested in /commission/."


def _section_phases(name):
    requirements = f"Its required contents are under heading ## {name} in /work/plan.md."
    return (
        ("gather", f"Gather evidence for section {name} requested in /commission/. "
         f"{requirements} Save evidence notes to /work/notes/{name}.md."),
        ("write", f"Write section {name}. {requirements} "
         f"Use the evidence notes in /work/notes/{name}.md. "
         f"Save the section to /work/sections/{name}.md."),
    )


def _phase_messages(case, name, phase, prompt):
    if phase in ("rescore", "sync"):
        body = f"work/sections/{name}.md"
        table = body[:-3] + ".table.md"
        notes = f"work/notes/{name}.md"
        paths = [notes] if phase == "rescore" else [body, table]
        blocks = ([f"Rescore {name}. Save /{table}.", "Current scoring and applicability excerpt:\n" +
                   _score_excerpt((case / body).read_text(encoding="utf-8-sig").splitlines())]
                  if phase == "rescore" else [f"Synchronize the following section /{body} to the checked table /{table}."])
        for path in paths:
            value = (case / path).read_text(encoding="utf-8-sig") if (case / path).is_file() else "(unavailable)"
            blocks.append(f"/{path}\n{value}")
        return [HumanMessage(content="\n\n".join(blocks), response_metadata={"supplied_files": [p for p in paths if (case / p).is_file()]})]
    messages = (_revision_messages(case, name, phase, prompt) if phase in REVISION_CAPS
                else [HumanMessage(content=prompt)])
    first = messages[0]
    if supplied := first.response_metadata.get("supplied_files"):
        notice = supplied_notice(supplied)
        first.content = ([{"type": "text", "text": notice}, *first.content] if isinstance(first.content, list)
                         else notice + "\n\n" + first.content)
    return messages


def _rescore_answers(case, cfg):
    blocks = []
    for row in answered_client_rows(case / "out" / cfg["xlsx_file"], cfg):
        fields = [f"{k}: {v}" for k, v in row.items() if v and k not in ("number", "answer")]
        blocks.append("\n".join([f"Q{row.get('number') or '?'}: {row['answer']}", *fields]))
    return "Answered client workbook rows:\n" + "\n\n".join(blocks) if blocks else ""


def _stage_messages(case, name, phase, prompt, lessons, extra):
    messages = _phase_messages(case, name, phase, prompt)
    if lessons and phase in ("plan", "gather", "write", "clarify", "revise", "rescore"):
        messages[0].content += "\n\nOwner-adopted lessons for reference:\n" + lessons
        if phase == "clarify":
            messages[0].content += "\nAsk the client to confirm applicable DEFAULT lessons for this case."
    if extra:
        messages[0].content += "\n\n" + extra
    return messages


def _updated_in_place(log, target, phase):
    # The draft and preliminary questions can exist before their final phase starts.
    messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    turns = learn.recorded_turns(messages)
    operations = []
    retry_at = None
    for turn, call, ok in learn.file_operations(turns):
        if call["args"].get("file_path", "").lstrip("/") != target:
            continue
        reply = turns[turn-1][1].get(call["id"], {})
        if phase == "rescore" and not ok and reply.get("content") == CALCULATE_FIRST:
            continue
        if reply.get("response_metadata", {}).get("rescore_save_retry"):
            retry_at = len(operations)
        operations.append(ok)
    if phase == "rescore" and retry_at == len(operations)-2 and operations[-2:] == [False, True]:
        operations.pop(retry_at)
    if phase in ("clarify", "rescore"):
        return bool(operations) and all(operations)
    # A clean no-edit decision is possible; a budget stop after a partial edit is unfinished.
    last = messages[-1]
    return (last.get("type") == "ai" and not last.get("tool_calls") and bool(last.get("usage_metadata"))
            and not last.get("response_metadata", {}).get("budget_stop") and all(operations))


def _phase_overrides(specs, selected, parser):
    overrides = {}
    for spec in specs:
        name, _, value = spec.partition("=")
        if name not in selected or name in overrides or not re.fullmatch(r"[0-9]+", value) or int(value) < 1:
            parser.error("--phase-cap needs one selected NAME=positive_integer per override")
        overrides[name] = int(value)
    return overrides


def _stage(case, stage, cap, specs, parser, library=None, sections=None):
    if cap < 0:
        parser.error("--max-tokens must be nonnegative")
    selected = STAGE_PHASES[stage]
    overrides = _phase_overrides(specs, selected, parser)

    def plan_names():
        _, order = _plan(case, use_headings=True)
        return list(dict.fromkeys(name for _, name in order))

    if sections:
        names = plan_names()
        unknown = [name for name in dict.fromkeys(sections) if name not in names]
        if unknown:
            parser.error(f"Unknown --section: {', '.join(unknown)}. Valid plan sections: {', '.join(names) or '(none)'}")

    work = case / "work"
    runs = work / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    final = case / ".stage2-complete"
    final.unlink(missing_ok=True)
    if stage == "stage1":
        (case / ".draft-in-progress").touch()
        (case / ".client-sheet-served").unlink(missing_ok=True)
    progress = runs / "progress.txt"
    completed = {}
    if progress.is_file():
        for line in progress.read_text(encoding="utf-8").splitlines():
            fields = line.split("\t")
            if len(fields) == 7 and fields[5] == "done":
                completed[fields[1], fields[2]] = fields[6]
                if fields[2] == "review":
                    completed.pop((fields[1], "clarify"), None)
            elif len(fields) == 7 and fields[2] == "rescore" and fields[5] == "unfinished":
                completed[fields[1], "rescore"] = "unfinished"
    total = 0

    def record(name, phase, used, status="done", signature=""):
        line = "\t".join((datetime.now(timezone.utc).isoformat(), name, phase, str(used),
                          str(total), status, signature))
        with progress.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
        print(line, flush=True)
        if status == "done":
            completed[name, phase] = signature
            if phase == "review":
                completed.pop((name, "clarify"), None)

    def phase_limit(phase, remaining):
        if phase == "gather" and phase not in overrides:
            return int(remaining * GATHER_BUDGET_FRACTION)
        default = {"rescore": 450000, "sync": 200000}.get(phase, REVISION_CAPS.get(phase, remaining))
        return min(remaining, overrides.get(phase, default))

    lessons = learn.library_text(library)
    limits = " ".join(f"{phase}={phase_limit(phase, cap)}" for phase in selected)
    record("-", stage, 0, "start", f"max-tokens={cap} {limits}")

    def run_phase(name, phase, prompt, extra=""):
        limit = phase_limit(phase, cap - total)
        messages = _stage_messages(case, name, phase, prompt, lessons, extra)
        with learn.observe_run(case, name, stage, limit, None) as receipt:
            return _run_phase(case, phase, messages, limit, runs, total, name, receipt)

    if stage == "stage1":
        if not (case / "index.md").is_file():
            ingest(case)
            record("-", "ingest", 0)
        if not (work / "plan.md").is_file() and total < cap:
            _, used = run_phase(None, "plan", PLAN_PROMPT)
            total += used
            record("-", "plan", used, "done" if (work / "plan.md").is_file() else "unfinished")

    names = plan_names()
    if any(not n or Path(n).name != n or "\t" in n or "\\" in n for n in names):
        parser.error("plan section names must be single filename stems")
    extra = ""
    client_rows = None
    answers_key = b""
    if stage == "stage2":
        cfg = _settings(case)
        _, _, answers = export_answers(case, cfg)
        rows = [f"{n}: {answer} ({author})" for n, (_, answer, author) in sorted(answers.items()) if answer]
        if rows:
            extra = "Case answers by question number:\n" + "\n".join(rows)
        owner = case / "commission/澄清答复.md"
        answers_key = sha256((owner.read_bytes() if owner.is_file() else b"") + b"\x00" + extra.encode()).digest()

    unfinished = []
    pending_elsewhere = False
    stopped = False
    for name in names:
        body = work / "sections" / (name + ".md")
        notes = work / "notes" / (name + ".md")
        questions = work / "clarifications" / (name + ".md")
        reviews = work / "reviews"
        review_file = reviews / (name + ".notes.md")
        phase_extras = {}
        if stage == "stage2" and any(not p.resolve().is_relative_to(case.resolve())
                                     for p in (body, notes, body.with_suffix(".table.md"), review_file)):
            parser.error("section tables, drafts, evidence notes and reviewer notes must be inside the case")
        if stage == "stage1":
            phases = [(phase, prompt, path) for (phase, prompt), path in zip(_section_phases(name), (notes, body))]
            phases.extend([("verify-sources", None, reviews / (name + "-sources.md")),
                           ("review", None, reviews / (name + ".md")), ("clarify", None, questions)])
        elif body.is_file() and (body.with_suffix(".table.md").is_file() or
                                 _scoring_table(body.read_text(encoding="utf-8-sig").splitlines())):
            table = body.with_suffix(".table.md")
            if client_rows is None:
                client_rows = _rescore_answers(case, cfg)
            # Sync edits the draft. Only inputs external to sync invalidate its saved handoff.
            context_key = sha256(b"\x00".join((client_rows.encode(), lessons.encode(), notes.read_bytes() if notes.is_file() else b""))).digest()
            reviewer_notes = ""
            if review_file.is_file():
                data = review_file.read_bytes()
                context_key += sha256(data).digest()
                reviewer_notes = "Reviewer notes for this section (apply each one; cite the source the note points to; a note never overrides a source or the scoring rules):\n" + data.decode("utf-8-sig")
            phase_extras = {"sync": reviewer_notes, "rescore": "\n\n".join(filter(None, (client_rows, reviewer_notes)))}
            phases = [("rescore", None, table), ("sync", None, body)]
        else:
            phases = [("revise", None, body)]
        for phase, prompt, target in phases:
            signature = ""
            existed = target.is_file()
            if existed:
                if phase == "rescore":
                    saved_table = table.read_text(encoding="utf-8-sig")
                    signature = sha256(context_key + table.read_bytes()).hexdigest()
                    prior = completed.get((name, phase))
                    if (prior == signature or (prior is None and not reviewer_notes)) and not _table_gate(saved_table):
                        if prior is None and (not sections or name in sections):
                            record(name, phase, 0, "done", signature)
                        continue
                elif phase in ("revise", "sync"):
                    key = answers_key if phase == "revise" else context_key + table.read_bytes()
                    signature = sha256(key + body.read_bytes()).hexdigest()
                    if completed.get((name, phase)) == signature:
                        continue
                elif phase != "clarify" or (name, "review") not in completed or (name, phase) in completed:
                    continue
            if sections and name not in sections:
                pending_elsewhere = True
                break
            if stopped or total >= cap or (phase == "revise" and not existed):
                unfinished.append(f"{name} ({phase})")
                break
            if phase == "verify-sources":
                check(case, name)
            log, used = run_phase(name, phase, prompt, phase_extras.get(phase, extra))
            total += used
            done = target.is_file()
            if existed or phase == "rescore":
                done = done and _updated_in_place(log, target.relative_to(case).as_posix(), phase)
                if phase in ("revise", "sync"):
                    key = answers_key if phase == "revise" else context_key + table.read_bytes()
                    signature = sha256(key + body.read_bytes()).hexdigest()
            if phase in ("rescore", "sync") and target.is_file():
                handoff = table.read_text(encoding="utf-8-sig")
                findings = _table_gate(target.read_text(encoding="utf-8-sig"), handoff if phase == "sync" else None,
                                       _calculated_inputs(log) if phase == "rescore" else None)
                if phase == "rescore":
                    signature = sha256(context_key + table.read_bytes()).hexdigest()
                if findings:
                    done, signature = False, " | ".join(findings)
            record(name, phase, used, "done" if done else "unfinished", signature)
            if not done:
                print(f"{name}: {phase} ended without a completed output.")
                unfinished.append(f"{name} ({phase})")
                stopped = True
                break
    print(export_docx(case))
    record("-", "export", 0)
    if not names or not (work / "plan.md").is_file():
        print("Unfinished sections: unavailable without a section plan.")
    elif unfinished:
        print("Unfinished sections: " + ", ".join(unfinished))
    elif pending_elsewhere:
        print(f"{stage} complete for selected sections. Other plan sections are pending.")
    else:
        if stage == "stage1":
            (case / ".draft-in-progress").unlink(missing_ok=True)
        else:
            final.touch()
        print(f"{stage} complete: {len(names)} sections.")


def _serve(cases, port):
    import os
    import shlex
    import signal
    import subprocess
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import PureWindowsPath
    from shutil import copyfileobj
    from threading import Lock
    from urllib.parse import parse_qs, parse_qsl, quote, urlsplit

    cases = cases.resolve()
    cases.mkdir(parents=True, exist_ok=True)
    groups = {"out": "out", "registers": "work/question-registers", "logs": "work/runs"}
    jobs = {}
    answering = set()
    lock = Lock()
    stopping = False

    def basename(name):
        if (not isinstance(name, str) or not name or name in (".", "..") or Path(name).name != name or PureWindowsPath(name).name != name
                or any(ord(c) < 32 for c in name)):
            raise ValueError("请只填写名称，不能包含路径或控制字符。")
        return name

    def location(case, relative):
        path = case / relative
        if not path.resolve().is_relative_to(case) or path.is_symlink():
            raise ValueError("文件必须位于案件目录内。")
        return path

    def active(case):
        job = jobs.get(case)
        return job if job and job.poll() is None else None

    def stop(job):
        if os.name == "posix":
            try:
                os.killpg(job.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            job.terminate()
        try:
            job.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(job.pid, signal.SIGKILL)
            else:
                job.kill()
            job.wait()

    def read_plan(marker):
        try:
            text = marker.read_text(encoding="utf-8")
        except FileNotFoundError:
            return True, None
        return False, json.loads(text) if text else None

    def missing(case, plan):
        return [item for item in plan if not location(case, Path(item[0]) / basename(item[1])).is_file()]

    def case_tokens(case):
        totals, historical = {}, {}
        manifests = location(case, "work/learn/runs")
        for path in location(case, "work/learn/signals").glob("*.json"):
            if path.is_symlink() or not path.is_file():
                continue
            try:
                record = json.loads(path.read_bytes())
            except FileNotFoundError:
                continue  # A rebuild may retire a historical signal during a page refresh.
            phases = record["S1_S2_S3"]
            usage = [sum(r["total_tokens"] or 0 for r in p["requests"]) for p in phases]
            if record["header"].get("provenance") == {"grouping": "unattributed historical phase log"}:
                historical[(case / "work/runs" / (path.stem + ".jsonl")).resolve()] = sum(usage)
                continue
            manifest = manifests / path.name
            entries = json.loads(manifest.read_bytes())["phases"] if manifest.is_file() and not manifest.is_symlink() else []
            for i, tokens in enumerate(usage):
                key = (case / entries[i]["log"]).resolve() if i < len(entries) else (path, i)
                totals[key] = tokens
        # An attributed phase supersedes the historical placeholder for that transcript.
        return sum((historical | totals).values())

    def status(case, detail=False):
        progress = location(case, "work/runs/progress.txt")
        lines = progress.read_text(encoding="utf-8").splitlines() if progress.is_file() else []
        rows = [line.split("\t") for line in lines if line.count("\t") == 6]
        job = active(case)
        marker = case / ".pending-uploads"
        ready, plan = read_plan(marker) if detail else (not marker.exists(), None)
        result = dict(name=case.name, running=job.args[3] if job else None, last=rows[-1] if rows else [],
                      ready=ready, case_tokens=case_tokens(case))
        if detail:
            result.update(planned=isinstance(plan, list), file_count=len(plan or []), missing=missing(case, plan or []))
            files = {}
            for group, relative in groups.items():
                folder = location(case, relative)
                paths = folder.glob("stage*-*.log" if group == "logs" else "*")
                files[group] = sorted(p.name for p in paths if p.is_file() and not p.is_symlink())
            cfg = _settings(case)
            draft = cfg["docx_file"] in files["out"]
            sheet = cfg["xlsx_file"] in files["out"]
            answered = (case / ".answers-uploaded").is_file() or any(
                n.startswith(Path(cfg["xlsx_file"]).stem + ".before-answers-") and n.endswith(".xlsx") for n in files["out"])
            if not ready:
                step = 1
            elif draft and (case / ".stage2-complete").is_file():
                step = 6
            elif (case / ".draft-in-progress").is_file():
                step = 2
            elif draft and sheet and answered:
                step = 5
            elif draft and sheet:
                step = 4 if (case / ".client-sheet-served").is_file() else 3
            else:
                step = 2
            values = fixed_values(location(case, "commission/fixed-text-values.txt"))
            result.update(rows=rows, files=files, step=step, fixed_values=[values.get(k, "") for k in FIXED_FIELDS])
        return result

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, data, kind="application/json; charset=utf-8"):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def local(self):
            host = self.headers.get("Host", "")
            expected = {f"{name}:{self.server.server_port}" for name in ("localhost", "127.0.0.1")}
            if self.server.server_port == 80:
                expected.update(("localhost", "127.0.0.1"))
            return host in expected and self.headers.get("Origin", "http://" + host) == "http://" + host

        def case(self, name):
            path = location(cases, basename(name))
            if name.startswith(".") or not path.is_dir():
                raise FileNotFoundError("找不到案件目录。")
            return path

        def do_GET(self):
            if not self.local():
                return self.reply(403, "请从本机打开此页面。")
            url = urlsplit(self.path)
            args = parse_qs(url.query)
            try:
                if url.path == "/":
                    return self.reply(200, Path(__file__).with_name("page.html").read_bytes(), "text/html; charset=utf-8")
                if url.path == "/api/cases":
                    with lock:
                        folders = [p for p in sorted(cases.iterdir())
                                   if not p.name.startswith(".") and p.is_dir() and not p.is_symlink()]
                    return self.reply(200, [status(p) for p in folders])
                if url.path not in ("/api/case", "/download"):
                    return self.reply(404, "找不到页面。")
                case = self.case(args.get("case", [""])[0])
                if url.path == "/api/case":
                    return self.reply(200, status(case, detail=True))
                group = args.get("group", [""])[0]
                name = basename(args.get("file", [""])[0])
                if group not in groups or (group == "logs" and not Path(name).match("stage*-*.log")):
                    return self.reply(404, "找不到文件。")
                path = location(case, Path(groups[group]) / name)
                receipt = location(case, ".client-sheet-served") if group == "out" and name == _settings(case)["xlsx_file"] else None
                with path.open("rb") as stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(name, safe=""))
                    self.send_header("Content-Length", str(os.fstat(stream.fileno()).st_size))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    copyfileobj(stream, self.wfile)
                    if receipt:
                        receipt.touch()
            except (FileNotFoundError, IsADirectoryError):
                self.reply(404, "找不到文件或案件。")
            except ValueError as error:
                self.reply(400, str(error) if re.search(r"[一-龥]", str(error)) else "请求数据格式无效。")

        def error(self, message):
            raise ValueError("--phase-cap 的格式应为 阶段名=正整数；每个所选阶段只能填写一次。")

        def upload(self, action, args):
            case = self.case(args.get("case", ""))
            name = basename(args.get("file", ""))
            group = args.get("group")
            marker = case / ".pending-uploads"
            if action == "/upload":
                if group not in ("commission", "sources"):
                    return 400, "请选择委托资料或源资料。"
                target = location(case, Path(group) / name)
                _, plan = read_plan(marker)
            else:
                target = location(case, Path("out") / basename(_settings(case)["xlsx_file"]))
            if self.headers.get_content_type() != "application/octet-stream":
                return 400, "每次请求只能上传一个文件。"
            remaining = int(self.headers.get("Content-Length", "-1"))
            if remaining < 0:
                return 411, "上传请求缺少文件大小。"
            with lock:
                if stopping or active(case) or case in answering:
                    return 409, "案件正在处理中，请稍后再试。"
                if action == "/upload":
                    if target.exists():
                        return 409, f"{group}/ 中已有同名文件。"
                    if not marker.exists() or [group, name] not in (plan or []):
                        return 409, "此文件不在待上传清单中。"
                else:
                    if marker.exists():
                        return 409, "请先完成案件资料上传。"
                    answering.add(case)
            try:
                with TemporaryDirectory(dir=case, prefix=".transfer-") as tmp:
                    saved = Path(tmp) / "body"
                    with saved.open("wb") as stream:
                        while remaining:
                            chunk = self.rfile.read(min(remaining, 1024 * 1024))
                            if not chunk:
                                return 400, "文件上传中断，请重试。"
                            stream.write(chunk)
                            remaining -= len(chunk)
                    with lock:
                        if action == "/upload":
                            if target.exists():
                                return 409, f"{group}/ 中已有同名文件。"
                            saved.rename(target)
                        else:
                            target.parent.mkdir(exist_ok=True)
                            backup = target.with_name(f"{target.stem}.before-answers-{self.timestamp()}.xlsx")
                            if target.exists():
                                target.rename(backup)
                            try:
                                location(case, ".stage2-complete").unlink(missing_ok=True)
                                saved.replace(target)
                                location(case, ".answers-uploaded").touch()
                            finally:
                                if not target.exists() and backup.exists():
                                    backup.rename(target)
                return 201, dict(name=target.name)
            finally:
                if action == "/answers":
                    with lock:
                        answering.discard(case)

        def control(self, action, fields):
            name = basename(fields.get("case", ""))
            if action == "/create":
                if name.startswith("."):
                    return 400, "案件名称不能以点开头。"
                case = location(cases, name)
                with TemporaryDirectory(dir=staging) as tmp:
                    staged = Path(tmp) / "case"
                    for folder in ("commission", "sources"):
                        (staged / folder).mkdir(parents=True)
                    template = location(cases.parent, "templates/fixed-text.docx")
                    if template.is_file():
                        copyfile(template, staged / "commission/fixed-text.docx")
                    (staged / ".pending-uploads").touch()
                    with lock:
                        if case.exists():
                            return 409, "已有同名案件。"
                        staged.rename(case)
                return 201, dict(name=name)
            case = self.case(name)
            marker = case / ".pending-uploads"
            if action == "/fixed-values":
                values = [fields.get(k) for k in ("project", "count")]
                if any(v is None or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\ufffe\uffff]", v) for v in values):
                    return 400, "请提交两个单行字段。"
                with lock:
                    if active(case) or case in answering:
                        return 409, "请等待本案件运行或答复上传完成。"
                    target = location(case, "commission/fixed-text-values.txt")
                    lines = target.read_text(encoding="utf-8-sig").splitlines() if target.is_file() else []
                    lines = [line for line in lines if not ("=>" in line and line.partition("=>")[0] in FIXED_FIELDS)]
                    lines.extend(k + "=>" + v for k, v in zip(FIXED_FIELDS, values) if v)
                    target.parent.mkdir(exist_ok=True)
                    with TemporaryDirectory(dir=target.parent) as tmp:
                        saved = Path(tmp) / "values"
                        saved.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
                        saved.replace(target)
                return 200, dict(name=name)
            if action == "/stop":
                with lock:
                    job = active(case)
                if job:
                    stop(job)
                return 200, dict(name=name)
            if action == "/finish":
                ready, plan = read_plan(marker)
                if ready:
                    return 200, dict(name=name)
                if plan is None or missing(case, plan):
                    return 409, "仍有案件资料待上传。"
                with lock:
                    marker.unlink(missing_ok=True)
                return 200, dict(name=name)
            if action == "/prepare":
                names = json.loads(fields.get("files", "null"))
                if not isinstance(names, list):
                    return 400, "请选择要上传的文件。"
                seen = set()
                for item in names:
                    if not isinstance(item, list) or len(item) != 2 or item[0] not in ("commission", "sources"):
                        return 400, "请为每个文件选择委托资料或源资料。"
                    group, filename = item
                    key = group, basename(filename)
                    if key in seen:
                        return 409, f"{group}/ 中已有同名文件。"
                    seen.add(key)
                with TemporaryDirectory(dir=case) as tmp:
                    updated = Path(tmp) / "plan"
                    updated.write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
                    with lock:
                        if not marker.exists():
                            return 409, "案件资料已就绪。"
                        if marker.stat().st_size == 0:
                            updated.replace(marker)
                            return 200, dict(name=name)
                _, plan = read_plan(marker)
                if plan != names:
                    return 409, "请继续上传原清单中的文件。"
                return 200, dict(name=name)
            stage = fields.get("stage", "")
            cap = fields.get("max_tokens", "")
            if stage not in STAGE_PHASES or not re.fullmatch("[0-9]+", cap):
                return 400, "请选择运行阶段，并输入非负整数作为 token 上限。"
            specs = [s.strip() for s in fields.get("phase_caps", "").splitlines() if s.strip()]
            _phase_overrides(specs, STAGE_PHASES[stage], self)
            argv = [sys.executable, "-m", "reportagent", stage, str(case), "--max-tokens", cap]
            library = cases.parent / "lessons"
            if library.is_dir():
                argv.extend(("--library", str(library)))
            for spec in specs:
                argv.extend(("--phase-cap", spec))
            command = shlex.join(argv)
            with lock:
                if stopping:
                    return 503, "工作区服务正在停止。"
                if marker.exists() or case in answering:
                    return 409, "请先完成案件资料上传。"
                if action == "/preview":
                    return 200, dict(command=command, cap=cap)
                if active(case):
                    return 409, "本案件已有阶段正在运行。"
                runs = location(case, "work/runs")
                runs.mkdir(parents=True, exist_ok=True)
                with (runs / f"{stage}-{self.timestamp()}.log").open("xb") as stream:
                    jobs[case] = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT,
                                                 stdin=subprocess.DEVNULL, start_new_session=os.name == "posix",
                                                 pass_fds=(lease.fileno(),) if os.name == "posix" else (),
                                                 env={**os.environ, "PYTHONUNBUFFERED": "1"})
            return 202, dict(command=command)

        def do_POST(self):
            if not self.local() or self.headers.get("X-Report-Agent") != "1":
                return self.reply(403, "请从本机的报告工作区提交。")
            url = urlsplit(self.path)
            try:
                if stopping:
                    return self.reply(503, "工作区服务正在停止。")
                if url.path in ("/upload", "/answers"):
                    result = self.upload(url.path, dict(parse_qsl(url.query)))
                elif url.path in ("/create", "/prepare", "/finish", "/preview", "/run", "/stop", "/fixed-values"):
                    if self.headers.get_content_type() != "application/x-www-form-urlencoded":
                        return self.reply(400, "请将案件信息与文件分开提交。")
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0:
                        return self.reply(400, "表单长度无效。")
                    fields = dict(parse_qsl(self.rfile.read(length).decode("utf-8"), keep_blank_values=url.path == "/fixed-values"))
                    result = self.control(url.path, fields)
                else:
                    result = 404, "找不到此操作。"
            except FileNotFoundError:
                result = 404, "找不到案件或文件。"
            except OSError:
                result = 400, "无法读写案件文件，请检查目录和文件权限。"
            except ValueError as error:
                result = 400, str(error) if re.search(r"[一-龥]", str(error)) else "请求数据格式无效。"
            try:
                self.reply(*result)
            except (BrokenPipeError, ConnectionResetError):
                pass

        @staticmethod
        def timestamp():
            return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")

    # A single service owns the process handles for this cases folder. On WSL its children inherit the lease.
    with location(cases, ".reportagent-serve.lock").open("a+b") as lease:
        try:
            if os.name == "posix":
                import fcntl
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt
                lease.seek(0)
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, PermissionError):
            sys.exit("Another Report Agent service or stage is using this cases folder.")
        with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server, TemporaryDirectory(dir=cases, prefix=".upload-") as staging:
            print(f"http://127.0.0.1:{server.server_port}", flush=True)
            previous = signal.signal(signal.SIGTERM, signal.default_int_handler)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                with lock:
                    stopping = True
                    for job in jobs.values():
                        if job.poll() is None:
                            stop(job)
                signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser(prog="reportagent")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("ingest", "export", "plan", "run", "review", "revise", "check", "stage1", "stage2"):
        command = commands.add_parser(name)
        command.add_argument("case", type=Path)
        if name in ("plan", "run", "review", "revise", "stage1", "stage2"):
            command.add_argument("--max-tokens", type=int, required=True)
        if name in ("stage1", "stage2"):
            command.add_argument("--section", action="append", metavar="NAME", help="select a plan section (repeatable)")
        if name in ("review", "revise", "check"):
            command.add_argument("--section", required=name != "check")
        if name == "check":
            command.add_argument("--score-arithmetic", action="store_true", help="only print score discrepancies; omit --section for all sections")
        if name == "revise":
            command.add_argument("--phases", metavar="LIST", help="Comma-separated phases in the revision workflow")
            command.add_argument("--preflight", action="store_true", help="estimate affordable requests without calling the model")
        if name in ("revise", "stage1", "stage2"):
            command.add_argument("--phase-cap", action="append", default=[], metavar="NAME=N",
                                 help="Override one selected phase's token cap (repeatable)")
        if name in STAGE_PHASES:
            command.add_argument("--library", type=Path, help="directory containing owner-adopted lessons.md")
        if name == "run":
            command.add_argument("--write-only", action="store_true")
            command.add_argument("--section")
    serve = commands.add_parser("serve", help="open the local case workspace")
    serve.add_argument("--cases", required=True, type=Path)
    serve.add_argument("--port", type=int, default=8765)
    replay = commands.add_parser("replay", help="compare one skill on frozen phase inputs")
    replay.add_argument("case", type=Path)
    selection = replay.add_mutually_exclusive_group(required=True)
    selection.add_argument("--skill", choices=sorted(PHASE_SKILLS[p] for p in REPLAY_CAPS))
    selection.add_argument("--batch", choices=REPLAY_BATCHES)
    replay.add_argument("--section", help="exact frozen section name (required for a batch)")
    replay.add_argument("--historical-input", action="store_true", help="use the exact recorded leading messages")
    learn.add_parser(commands)
    for name in ("plan", "run", "review", "revise", "replay"):
        commands.choices[name].add_argument("--task", type=int, help="operator-supplied task number for learning records")
    args = parser.parse_args()
    if args.command == "serve":
        return _serve(args.cases, args.port)
    if args.command == "learn":
        return _propose(args, parser) if args.action == "propose" else learn.command(args)
    if args.command in ("plan", "run", "review", "revise", "replay") and not getattr(args, "preflight", False):
        with learn.observe_run(args.case, getattr(args, "section", None), args.command, getattr(args, "max_tokens", None), args.task) as receipt:
            return _execute(args, parser, receipt)
    return _execute(args, parser, None)


def _execute(args, parser, receipt):
    if args.command in ("stage1", "stage2"):
        return _stage(args.case, args.command, args.max_tokens, args.phase_cap, parser, args.library, args.section)
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
            paths = [folder / f"{args.section}.md"] if args.section else sorted(p for p in folder.glob("*.md") if not p.name.endswith(".table.md"))
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
        phase_caps.update(_phase_overrides(args.phase_cap, selected, parser))
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
        phases = (("plan", PLAN_PROMPT),)
    elif args.command == "revise":
        phases = [(phase, None) for phase in REVISION_CAPS if phase in selected]
    elif args.command == "review":
        name = args.section
        phases = (("review", f"Review section {name}. "
                             f"Save the review to /work/reviews/{name}.md."),)
    elif args.section:
        name = args.section
        phases = _section_phases(name)
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
        messages = _phase_messages(args.case, getattr(args, "section", None), phase, prompt)
        if preflight:
            _preflight(args.case, args.section, phase, messages, limit)
            continue
        _, used = _run_phase(args.case, phase, messages, limit, folder, total, getattr(args, "section", None), receipt)
        total += used
        remaining = max(0, remaining - used)
