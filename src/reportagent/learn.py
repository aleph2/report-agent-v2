from collections import Counter, defaultdict
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
from difflib import SequenceMatcher
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import re
from shutil import copyfile
import subprocess
import sys
from tempfile import TemporaryDirectory
from uuid import uuid4

from .export.docx import clarification_numbers


def recorded_turns(transcript):
    turns = []
    results = {}
    for m in transcript:
        if m.get("type") == "ai":
            results = {}
            synthetic = (m.get("response_metadata", {}).get("budget_stop") or
                         (not m.get("tool_calls") and not m.get("usage_metadata")
                          and str(m.get("content")).startswith("Token cap reached")))
            if not synthetic:
                turns.append((m, results))
        elif m.get("type") == "tool":
            results[m["tool_call_id"]] = m
    return turns


def file_operations(turns):
    for turn, (message, results) in enumerate(turns, 1):
        for call in message.get("tool_calls") or []:
            tool = call["name"]
            if tool in ("edit_file", "write_file"):
                reply = results.get(call["id"], {})
                prefix = "Successfully replaced" if tool == "edit_file" else ("Updated file ", "Created file ", "Successfully wrote")
                success = reply.get("status") == "success" and str(reply.get("content", "")).startswith(prefix)
                yield turn, call, success if reply else None


def _phase_signal(log, phase, section, cap):
    transcript = _json_lines(log)
    turns = recorded_turns(transcript)
    audit = _json_lines(log.with_suffix(".requests.jsonl"))
    attempts = {}
    replies = []
    for line in audit:
        n = line.get("request", len(attempts) + 1)
        attempts.setdefault(n, {}).update(line)
        if line.get("event") == "response":
            replies.append(n)
    # Early logs only recorded guard decisions. Pair them only if there is no ambiguity.
    sent = [n for n, row in attempts.items() if row.get("decision") == "send"]
    if not replies and len(sent) == len(turns):
        replies = sent
    if not attempts:
        replies = list(range(1, len(turns) + 1))
        attempts = {n: {} for n in replies}
    target = {"verify-sources": f"reviews/{section}-sources.md", "review": f"reviews/{section}.md",
              "clarify": f"clarifications/{section}.md", "revise": f"sections/{section}.md",
              "plan": "plan.md", "gather": f"notes/{section}.md", "write": f"sections/{section}.md"}.get(phase)
    wrote = defaultdict(set)
    for turn, call, success in file_operations(turns):
        if success:
            wrote[turn].add(call["args"].get("file_path", "").lstrip("/"))
    models = set()
    for i, (n, (m, results)) in enumerate(zip(replies, turns), 1):
        row = attempts[n]
        usage = m.get("usage_metadata") or {}
        model = (m.get("response_metadata") or {}).get("model_name")
        if model:
            models.add(model)
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            row.setdefault(key, usage.get(key))
        row.setdefault("reasoning_tokens", (usage.get("output_token_details") or {}).get("reasoning"))
        calls = m.get("tool_calls") or []
        denied = [c for c in calls if results.get(c["id"], {}).get("status") == "error"
                  and str(results[c["id"]].get("content", "")).startswith(
                      ("This phase may read ", "This phase allows ", "This file is already in your first message"))]
        row["denied_paths"] = [c["args"].get("file_path", c["args"].get("path")) for c in denied]
        if f"work/{target}" in wrote[i] or (not section and phase in ("gather", "write") and any(
                p.startswith("work/notes/" if phase == "gather" else "work/sections/") for p in wrote[i])):
            label = "saved"
        elif calls and len(denied) == len(calls):
            label = "denied"
        elif calls and all(c["name"] in ("read_file", "ls", "glob", "grep") and
                           results.get(c["id"], {}).get("status") == "success" for c in calls):
            label = "read"
        elif not calls and not m.get("content"):
            label = "empty"
        else:
            label = "other"
            row["tool_results"] = list(results.values())
        row["outcome"] = label
    rows = []
    for n, data in sorted(attempts.items()):
        row = {k: data.get(k) for k in ("estimated_input_tokens", "input_tokens", "output_tokens", "reasoning_tokens",
                                        "total_tokens", "decision")}
        row.update(request=n, phase=phase, phase_cap=cap)
        row.update({k: data[k] for k in ("outcome", "denied_paths", "tool_results") if k in data})
        row["reasoning_share"] = (row["reasoning_tokens"] / row["output_tokens"]
                                  if row["reasoning_tokens"] is not None and row["output_tokens"] else None)
        rows.append(row)
    reserve = any(r["decision"] == "stop" for r in rows) or any(
        m.get("type") == "ai" and str(m.get("content", "")).startswith("Token cap reached")
        and any(s in str(m.get("content")) for s in ("reserve", "remaining <")) for m in transcript)
    return dict(phase=phase, phase_cap=cap, models=sorted(models), requests=rows,
                wasted_tokens=_token_sum(r for r in rows if r.get("outcome") in ("denied", "empty")),
                other_tokens=_token_sum(r for r in rows if r.get("outcome") == "other"),
                guard_stops=sum(r["decision"] == "stop" for r in rows),
                unanswered=sum(n not in replies for n in sent),
                resumes=sum(m.get("type") == "resume" for m in transcript),
                reserve_ended=reserve, reserve_before_save=reserve and not any(r.get("outcome") == "saved" for r in rows),
                partial=not audit or len(replies) != len(turns))


def _token_sum(rows):
    values = [r.get("total_tokens") for r in rows]
    return None if None in values else sum(values)


def _json_lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.is_file() else []


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    if path.is_file() and path.read_bytes() == encoded:
        return
    temp = path.with_name(f".{path.name}.{uuid4().hex}")
    temp.write_bytes(encoded)
    temp.replace(path)


def _finding_ids(review):
    findings = {}
    for line in review.splitlines():
        match = re.match(r"^\s*(?:[-*]\s*)?(?:\*\*)?(?:F\s*)?(\d+)(?:\*\*)?\s*[.、|｜:：]", line, re.I)
        if match and re.search(r"\bmust[- ]fix\b|已修复|\bfixed\b|resolved|已关闭", line, re.I):
            numbers = set()
            for a, b in re.findall(r"(?:\blines?\s*|行号\s*|\bsec(?:tion)?:\s*|/work/sections/[^｜|]+?\.md:)(\d+)(?:\s*[-–~～至]\s*(\d+))?", line, re.I):
                numbers.update(range(int(a), int(b or a) + 1))
            location_fields = re.split(r"[|｜]", line)
            if len(location_fields) > 1:
                for a, b in re.findall(r"第\s*(\d+)(?:\s*[-–~～至]\s*(\d+))?\s*行", location_fields[1]):
                    numbers.update(range(int(a), int(b or a) + 1))
            findings[f"F{match[1]}"] = dict(lines=sorted(numbers), text=line,
                                            fixed=bool(re.search(r"已修复|resolved-by-review|已关闭|(?:^|[|｜:：])\s*fixed\b|\bmarked fixed\b", line, re.I)))
    return findings


def _reported_baseline(lines, risk_statements):
    totals = [str(v) for l in lines if l.startswith("|") and re.search(r"合计|总分|total", l, re.I)
              for _, v, _ in risk_statements(l.replace("**", "")) if v is not None]
    grades = {g for l in lines for cond, _, g in risk_statements(l.replace("**", ""))
              if cond is not False and g}
    score = totals[0] if totals and len(set(totals)) == 1 else None
    grade = grades.pop() if len(grades) == 1 else None
    return {"score": score, "grade": grade}


def snapshot(case, section, check, risk_statements):
    if not section:
        return {}
    work = case / "work"
    body = work / "sections" / (section + ".md")
    questions = work / "clarifications" / (section + ".md")
    review = work / "reviews" / (section + ".md")
    value = {}
    if body.is_file():
        lines = body.read_text(encoding="utf-8").splitlines()
        value["body"] = lines
        value.update(_reported_baseline(lines, risk_statements))
    if review.is_file():
        txt = review.read_text(encoding="utf-8")
        value["findings"] = _finding_ids(txt)
        value["review_known"] = bool(value["findings"]) or bool(re.search(
            r"(?:must[- ]fix|必须整改项)\s*[:：]\s*(?:0|无|none)\b", txt, re.I))
    if questions.is_file():
        value["numbers"] = [int(i) for i in re.findall(r"(?m)^number:\s*(\d+)\s*$", questions.read_text(encoding="utf-8"))]
        value["conflicts"] = clarification_numbers(case)[1]
    if body.is_file() and (work / "notes" / (section + ".md")).is_file():
        with TemporaryDirectory() as tmp, redirect_stdout(StringIO()) as printed:
            path = check(case, section, output=Path(tmp) / "check.md")
            value["checks"] = _check_lines(path.read_text(encoding="utf-8"), printed.getvalue())
    return value


def _check_lines(check_text, arithmetic):
    return dict(lines=[l for l in check_text.splitlines() if l.startswith((
        "Findings: total=", "checklist entries=", "clarification ", "duplicate clarification "))],
                arithmetic=arithmetic.splitlines())


def begin_phase(receipt, phase, log, limit, before):
    observation = dict(phase=phase, log=os.path.relpath(log, receipt["root"]), cap=limit, before=before)
    receipt["phases"].append(observation)
    return observation


def finish_phase(receipt, observation, log, after):
    before = observation["before"]
    left, right = before.pop("body", []), after.pop("body", [])
    observation["changed_lines"] = [n for op, a, b, _, _ in SequenceMatcher(None, left, right, autojunk=False).get_opcodes()
                                    if op != "equal" for n in range(a + 1, b + 1)]
    observation["after"] = after
    if receipt["command"] == "replay":
        canonical = Path(receipt["root"])
        retained = canonical / "work/learn/imports/replay" / receipt["run_id"] / log.name
        retained.parent.mkdir(parents=True, exist_ok=True)
        for old, new in ((log, retained), (log.with_suffix(".requests.jsonl"), retained.with_suffix(".requests.jsonl"))):
            if old.is_file():
                copyfile(old, new)
        observation["log"] = retained.relative_to(canonical).as_posix()


@contextmanager
def observe_run(case, section, command, cap, task):
    case = case.resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    commit = subprocess.run(["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    source = dict(root=str(case), retries=0, run_id=run_id, section=section, command=command, cap=cap, task=task or "unknown",
                  commit=commit.stdout.strip() if commit.returncode == 0 else "unknown", phases=[],
                  provenance={"task": "operator --task" if task else "unknown", "commit": "git rev-parse HEAD"})
    try:
        yield source
    finally:
        if source["phases"]:
            _save_json(case / "work/learn/runs" / (run_id + ".json"), source)
            _learn_records(case, latest=source)


def _learn_record(case, source):
    phases, findings, checklog, events = [], {}, [], []
    models = set()
    for entry in source["phases"]:
        log = (case / entry["log"]).resolve()
        if not any(log.is_relative_to((case / directory).resolve()) for directory in ("work/runs", "work/learn/imports")):
            sys.exit(f"Learning transcript outside case run logs: {log}")
        phase = entry["phase"]
        section = source.get("section")
        metrics = _phase_signal(log, phase, section, entry.get("cap"))
        models.update(metrics.pop("models"))
        phases.append(metrics)
        before, after = entry.get("before", {}), entry.get("after", {})
        checklog.append(dict(phase=phase, before=before.get("checks"), after=after.get("checks")))
        saved = any(r.get("outcome") == "saved" for r in metrics["requests"])
        for fid, item in {**before.get("findings", {}), **after.get("findings", {})}.items():
            touched = phase == "revise" and saved and bool(set(entry.get("changed_lines", ())).intersection(item["lines"]))
            if touched or fid not in findings:
                findings[fid] = dict(status="touched" if touched else "open", citation=item["text"])
        if phase == "review" and saved and after.get("review_known"):
            for fid in findings:
                if fid not in after["findings"] or after["findings"][fid]["fixed"]:
                    findings[fid]["status"] = "resolved-by-review"
        prior = [before.get(k) for k in ("score", "grade")]
        later = [after.get(k) for k in ("score", "grade")]
        changed = (True if any(a != b for a, b in zip(prior, later) if a is not None and b is not None)
                   else False if None not in prior + later else None)
        events.append(dict(phase=phase, conflicts_before=before.get("conflicts"), numbers_before=before.get("numbers"), numbers_after=after.get("numbers"),
                           conflicts=after.get("conflicts"), F_grade_before=prior, F_grade_after=later,
                           F_grade_changed=changed if phase == "revise" else None, saved=saved,
                           review_ids=list(after["findings"]) if phase == "review" and saved and after.get("review_known") else None,
                           fixed_ids=[f for f, v in after.get("findings", {}).items() if v["fixed"]]))
    header = {k: source.get(k, "unknown") for k in ("run_id", "task", "commit", "cap", "command", "section", "provenance")}
    header.update(phases=[p["phase"] for p in phases], phase_caps=[p["phase_cap"] for p in phases],
                  model=(next(iter(models)) if len(models) == 1 else sorted(models) or source.get("model", "unknown")),
                  retries=source.get("retries", "unknown"), resumes=sum(p["resumes"] for p in phases),
                  reserve_ends=sum(p["reserve_ended"] for p in phases),
                  guard_stops=sum(p["guard_stops"] for p in phases), unanswered=sum(p["unanswered"] for p in phases),
                  total_tokens=_token_sum(r for p in phases for r in p["requests"] if r.get("outcome") or r.get("total_tokens") is not None),
                  partial=source.get("partial", False) or any(p["partial"] for p in phases))
    return dict(header=header, S1_S2_S3=phases, S4=source.get("checks", checklog), S5=findings, S6=[], S7=events,
                separate_mapping=source.get("separate_mapping"), clarify_sync_followed=None)


def _learn_records(case, latest=None):
    root = case / "work/learn"
    cached = []
    if latest is not None:
        sources = [latest]
        cached = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "signals").glob("*.json")
                  if p.stem != latest["run_id"]]
    else:
        sources = [dict(json.loads(p.read_text(encoding="utf-8")), run_id=p.stem) for p in sorted((root / "runs").glob("*.json"))]
    known = {(case / phase["log"]).resolve() for src in sources for phase in src["phases"]}
    for log in (() if latest else sorted((case / "work/runs").glob("*.jsonl"))):
        if log.name.endswith(".requests.jsonl") or log.resolve() in known:
            continue
        # Logs without an invocation manifest cannot establish grouping or historical case state.
        _, _, phase = log.stem.partition("-")
        sources.append(dict(run_id=log.stem, partial=True, phases=[dict(phase=phase, log=str(log))],
                            provenance={"grouping": "unattributed historical phase log"}))
    records = sorted(cached + [_learn_record(case, s) for s in sources], key=lambda r: r["header"]["run_id"])
    indexed = {r["header"]["run_id"]: r for r in records}
    # Once a historical phase is attributed to a real invocation, retire its derived placeholder.
    if latest is None:
        for path in (root / "signals").glob("*.json"):
            if path.stem not in indexed and (case / "work/runs" / (path.stem + ".jsonl")).resolve() in known:
                prior_header = json.loads(path.read_text(encoding="utf-8"))["header"]
                if prior_header.get("provenance") == {"grouping": "unattributed historical phase log"}:
                    path.unlink()
    prior = defaultdict(list)
    for record in records:
        h = record["header"]
        section = h.get("section")
        if not section or section == "unknown" or h["command"] == "replay":
            continue
        previous = prior[section]
        for event in record["S7"]:
            if event["review_ids"] is not None:
                for other in previous:
                    for fid, finding in other["S5"].items():
                        if finding["status"] != "resolved-by-owner" and (fid not in event["review_ids"] or fid in event["fixed_ids"]):
                            finding["status"] = "resolved-by-review"
        if previous and any(e["phase"] == "revise" for e in previous[-1]["S7"]):
            previous[-1]["clarify_sync_followed"] = (h["run_id"] if h["phases"] == ["clarify"]
                                                     and any(e["saved"] for e in record["S7"]) else False)
        previous.append(record)
    for file in sorted((root / "feedback").glob("*.json")):
        event = json.loads(file.read_text(encoding="utf-8"))
        record = indexed.get(event["run_id"])
        if record is None:
            continue
        if "finding_id" in event:
            finding = record["S5"].get(event["finding_id"])
            if finding:
                finding.update(status="resolved-by-owner", owner_event=event)
        else:
            if event not in record["S6"]:
                record["S6"].append(event)
    for record in records:
        record["S6"].sort(key=lambda e: (e["date"], e.get("recorded_at", "")))
        _save_json(root / "signals" / (record["header"]["run_id"] + ".json"), record)
    return records


def add_parser(commands):
    learn = commands.add_parser("learn", help="record offline run signals and owner feedback")
    actions = learn.add_subparsers(dest="action", required=True)
    for action in ("signals", "summary", "feedback"):
        item = actions.add_parser(action)
        item.add_argument("--case", type=Path, required=True)
        if action == "feedback":
            item.add_argument("--input", type=Path, required=True, help="owner feedback JSON")


def command(args):
    case = args.case.resolve()
    if args.action == "feedback":
        event = json.loads(args.input.read_text(encoding="utf-8"))
        fields = ["run_id", "section", "date", "source", "owner_reason"]
        fields += (["finding_id"] if "finding_id" in event else
                   ["indicator", "agent_value", "evidence", "band", "agent_reason", "owner_value"])
        if any(event.get(k) is None or not str(event[k]).strip() for k in fields):
            sys.exit("Required owner feedback fields: " + ", ".join(fields))
        records = _learn_records(case)
        record = next((r for r in records if r["header"]["run_id"] == event["run_id"] and
                       r["header"]["section"] == event["section"]), None)
        if record is None or ("finding_id" in event and event["finding_id"] not in record["S5"]):
            sys.exit("No corresponding run, section or finding for this feedback.")
        directory = case / "work/learn/feedback"
        # Identical re-imports are idempotent. Distinct owner decisions remain separate events.
        key = sha256(json.dumps(event, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if not (directory / (key + ".json")).exists():
            _save_json(directory / (key + ".json"), {**event, "recorded_at": datetime.now(timezone.utc).isoformat()})
    records = _learn_records(case)
    if args.action != "summary":
        print(f"{len(records)} signal records in {case / 'work/learn/signals'}")
        return
    _print_learn_summary(records)


def _print_learn_summary(records):
    phases = defaultdict(list)
    open_ids = defaultdict(dict)
    for record in records:
        for phase in record["S1_S2_S3"]:
            phases[phase["phase"]].extend(phase["requests"])
        h = record["header"]
        if h.get("section") not in (None, "unknown") and h.get("command") != "replay":
            open_ids[h["section"]].update({k: v["status"] for k, v in record["S5"].items()})
    for phase, rows in sorted(phases.items()):
        counts = Counter(r.get("outcome") for r in rows)
        pairs = [r for r in rows if r["reasoning_tokens"] is not None and r["output_tokens"] is not None]
        output = sum(r["output_tokens"] for r in pairs)
        share = f"{sum(r['reasoning_tokens'] for r in pairs) / output:.2%}" if output else "unknown"
        print(f"\n{phase}\nrequests\tsaved\tdenied\tread\tempty\tother\twasted_tokens\tother_tokens\treasoning_share")
        print(len(rows), *(counts[k] for k in ("saved", "denied", "read", "empty", "other")),
              _token_sum(r for r in rows if r.get("outcome") in ("denied", "empty")),
              _token_sum(r for r in rows if r.get("outcome") == "other"), share, sep="\t")
    print("\nsection\topen_must_fix")
    for section, statuses in sorted(open_ids.items()):
        print(section, sum(v in ("open", "touched") for v in statuses.values()), sep="\t")


# These scores describe a replay trace and file changes; the model makes the engineering decisions.
