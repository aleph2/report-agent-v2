from decimal import Decimal
from importlib.resources import files
import json
import sys

from docx import Document
from langchain_core.messages import AIMessage

from reportagent import cli
from reportagent.export.docx import _old_answers, _settings, _xlsx
from tests.fake_model import ScriptedModel, call


def run(case, command, cap, monkeypatch, *options):
    monkeypatch.setattr(sys, "argv", ["reportagent", command, str(case), "--max-tokens", str(cap), *options])
    cli.main()


def test_packaged_revise_skill_requires_cross_slope_rules():
    skill = files("reportagent.skills").joinpath(cli.PHASE_SKILLS["revise"], "SKILL.md").read_text(encoding="utf-8")
    rule = (
        "Client answers filed under another slope or under 评估流程和方法 often set rules for every slope (for "
        "example: drainage is treated as effective, excavation avoids the rainy season, annual rainfall "
        "is taken at its least favourable value, joints that do not run through mean lower risk). Before "
        "scoring each indicator, check all answers for such a rule; apply it to this slope and cite its "
        "question number in that row."
    )
    lines = skill.splitlines()
    table_first = next(i for i, line in enumerate(lines) if line.startswith("When client answers change an indicator,"))
    limits = [
        "An answer that states an expected score or grade for a slope is never a scoring rule; do not use it in any row.",
        "Only method rules transfer between slopes (how to choose a source, how to compute, which assumption to use when data are missing). Facts about another slope's site, drawings or joints never transfer.",
    ]
    assert lines[table_first + 1:table_first + 4] == [*limits, rule]
    for phase in ("rescore", "sync"):
        text = files("reportagent.skills").joinpath(cli.PHASE_SKILLS[phase], "SKILL.md").read_text(encoding="utf-8")
        assert "\n".join(limits) in text
        assert "If reviewer notes are supplied, apply each one and state in your final reply which notes you applied." in text


def test_two_stages(tmp_path, monkeypatch, capsys):
    case = tmp_path
    settings = case / "commission/template/settings.txt"
    settings.parent.mkdir(parents=True)
    settings.write_text("answers_file: commission/answer-key.md\n")
    plan = "---\nsection: 1|South\nsection: 1|North\n---\n## South\n\n## North\n"
    names = iter(["South"] * 5 + ["North"] * 5 + ["South", "North"])
    phases = []
    def fake(phase):
        phases.append(phase)
        name = "" if phase == "plan" else next(names)
        body = f"/work/sections/{name}.md"
        heading = "**Southern** reservoir" if name == "South" else name
        target, value = {
            "plan": ("/work/plan.md", plan), "gather": (f"/work/notes/{name}.md", "Synthetic notes"),
            "write": (body, f"# {heading}\n\nDraft text"),
            "verify-sources": (f"/work/reviews/{name}-sources.md", "Source check"),
            "review": (f"/work/reviews/{name}.md", "Review"),
            "clarify": (f"/work/clarifications/{name}.md",
                        f"number: {1 if name == 'South' else 2}\nanchor: Draft text\ncontent: Colour?\nquestion: Colour?\naudience: client"),
            "revise": (body, "Revised text"),
        }[phase]
        operation = (call("edit_file", file_path=target, old_string="Draft text", new_string=value)
                     if phase == "revise" else call("write_file", file_path=target, content=value))
        replies = [operation, AIMessage(content="Saved")]
        if phase == "write":
            # Initial drafting may create preliminary questions as well as the body.
            replies.insert(1, call("write_file", file_path=f"/work/clarifications/{name}.md", content="Preliminary"))
        usage = dict(input_tokens=5, output_tokens=2, total_tokens=7)
        return ScriptedModel(responses=[m.model_copy(update={"usage_metadata": usage}) for m in replies])

    monkeypatch.setattr(cli, "make_model", fake)
    run(case, "stage1", 300000, monkeypatch)
    section_phases = ["gather", "write", "verify-sources", "review", "clarify"]
    assert phases == ["plan", *section_phases, *section_phases]
    cfg = _settings(case)
    book = case / "out" / cfg["xlsx_file"]
    assert set(_old_answers(book, cfg)) == {1, 2}
    assert "Draft text" in [p.text for p in Document(case / "out/report.docx").paragraphs]
    progress = case / "work/runs/progress.txt"
    lines = progress.read_text().splitlines()
    assert len(lines) == 14
    assert "\tstage1\t0\t0\tstart\t" in lines[0]
    running = 0
    for line in lines[1:]:
        _, _, _, used, total, status, _ = line.split("\t")
        running += int(used)
        assert int(total) == running and status == "done"
    printed = capsys.readouterr().out
    assert all(line in printed for line in lines)
    run(case, "stage1", 300000, monkeypatch)
    assert phases == ["plan", *section_phases, *section_phases]
    # Replace the client sheet as an upload would, then add an owner answer at the configured path.
    headers = cfg["sheet_columns"].split("|")
    _xlsx(book, headers, [[1, "Southern reservoir", "", "Colour?", "", "Open", "Amber", "Client"],
                          [2, "North", "", "Colour?", "", "Open", "", ""],
                          [3, "评估流程和方法", "", "Method?", "", "Open", "Universal rule", "Client"],
                          [4, "", "", "Other?", "", "Open", "Unlabelled answer", "Client"],
                          [5, "North", "", "Unanswered?", "", "Open", "", ""]], cfg)
    (case / "commission/answer-key.md").write_text("2: Indigo\n")
    run(case, "stage2", 300000, monkeypatch)
    assert phases[-2:] == ["revise", "revise"]
    logs = list((case / "work/runs").glob("*-revise.jsonl"))
    assert len(logs) == 2
    for log in logs:
        prompt = json.loads(log.read_text().splitlines()[0])["content"]
        answers = prompt.split("Case answers by question number:\n")[1]
        assert answers.splitlines() == [
            "1: Amber (Client)", f"2: Indigo ({cfg['answer_author']})",
            "3: Universal rule (Client)", "4: Unlabelled answer (Client)"]
    labels = {}
    _old_answers(case / "work/question-registers/answers.xlsx", cfg, labels)
    assert labels == {1: "Southern reservoir", 2: "North", 3: "评估流程和方法", 4: "", 5: "North"}
    assert [p.text for p in Document(case / "out/report.docx").paragraphs].count("Revised text") == 2
    printed = capsys.readouterr().out
    assert all(line in printed for line in progress.read_text().splitlines()[len(lines):])
    run(case, "stage2", 300000, monkeypatch)
    assert phases.count("revise") == 2
    # A new answer reopens revision even though a revised draft already exists.
    (case / "commission/answer-key.md").write_text("2: Violet\n")
    monkeypatch.setattr(cli, "make_model", lambda phase: phases.append(phase) or ScriptedModel(
        responses=[AIMessage(content="No further edit", usage_metadata=dict(input_tokens=3, output_tokens=1, total_tokens=4))]))
    run(case, "stage2", 300000, monkeypatch)
    assert phases.count("revise") == 4
    run(case, "stage2", 300000, monkeypatch)
    assert phases.count("revise") == 4


def test_budget_exports_partial_and_resumes(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    work.mkdir()
    (work / "plan.md").write_text("---\nsection: 1|A\nsection: 1|B\n---\n## A\n## B\n")
    (work / "notes").mkdir()
    (work / "notes/A.md").write_text("Existing notes")
    phases = []
    def consume(case, phase, messages, limit, folder, previous_total=0, section=None, receipt=None):
        phases.append((phase, section, limit, previous_total))
        target = work / f"sections/{section}.md"
        target.parent.mkdir(exist_ok=True)
        target.write_text("Draft A")
        return folder / "fake.jsonl", limit

    monkeypatch.setattr(cli, "_run_phase", consume)
    run(tmp_path, "stage1", 50000, monkeypatch)
    assert phases == [("write", "A", 50000, 0)]
    assert (tmp_path / "out/report.docx").exists()
    output = capsys.readouterr().out
    assert "Unfinished sections: A (verify-sources), B (gather)" in output
    assert "\twrite\t50000\t50000\tdone" in (work / "runs/progress.txt").read_text()


def test_small_cap_does_not_mark_existing_draft_revised(tmp_path, monkeypatch, capsys):
    body = tmp_path / "work/sections/A.md"
    body.parent.mkdir(parents=True)
    body.write_text("Original draft")
    (tmp_path / "work/plan.md").write_text("## A\n## B\n")
    model = ScriptedModel(responses=[AIMessage(content="unused")])
    monkeypatch.setattr(cli, "make_model", lambda phase: model)
    run(tmp_path, "stage2", 1, monkeypatch)
    assert not (tmp_path / ".stage2-complete").exists()
    assert "Unfinished sections: A (revise), B (revise)" in capsys.readouterr().out
    assert "\trevise\t0\t0\tunfinished" in (tmp_path / "work/runs/progress.txt").read_text()
    assert body.read_text() == "Original draft"
    assert len(list((tmp_path / "work/runs").glob("*-revise.jsonl"))) == 1


def test_stage_phase_caps_apply_per_section_within_remaining_budget(tmp_path, monkeypatch, capsys):
    work = tmp_path / "work"
    for folder in ("sections", "clarifications", "reviews", "notes"):
        (work / folder).mkdir(parents=True)
    (work / "plan.md").write_text("## A\n## B\n")
    for name in ("A", "B"):
        for stem in (f"sections/{name}", f"clarifications/{name}", f"reviews/{name}", f"reviews/{name}-sources"):
            (work / (stem + ".md")).write_text("Synthetic")
    calls = []
    def consume(case, phase, messages, limit, folder, previous_total, section, receipt):
        calls.append((phase, section, limit))
        (work / "notes" / (section + ".md")).write_text("Notes")
        log = folder / "fake.jsonl"
        log.write_text(AIMessage(content="Done", usage_metadata=dict(input_tokens=limit, output_tokens=0,
                                                                     total_tokens=limit)).model_dump_json())
        return log, limit

    monkeypatch.setattr(cli, "_run_phase", consume)
    run(tmp_path, "stage1", 100, monkeypatch, "--phase-cap", "gather=90")
    run(tmp_path, "stage2", 300000, monkeypatch, "--phase-cap", "revise=220000")
    assert calls == [("gather", "A", 90), ("gather", "B", 10),
                     ("revise", "A", 220000), ("revise", "B", 80000)]
    lines = (work / "runs/progress.txt").read_text().splitlines()
    starts = [line for line in lines if "\tstart\t" in line]
    assert len(starts) == 2 and lines[0] == starts[0]
    assert "max-tokens=100 plan=100 gather=90 write=100 verify-sources=100 review=100 clarify=100" in starts[0]
    assert "max-tokens=300000 revise=220000" in starts[1]
    output = capsys.readouterr().out
    assert all(line in output for line in starts)


def test_stage_section_selection(tmp_path, monkeypatch, capsys):
    import pytest

    work = tmp_path / "work"
    calls = []
    def phase_run(case, phase, messages, limit, folder, previous_total, section, receipt):
        calls.append((phase, section, previous_total))
        (work / "notes" / (section + ".md")).write_text("Gathered")
        log = folder / "synthetic.jsonl"
        log.write_text(AIMessage(content="No revision needed", usage_metadata=dict(
            total_tokens=7, input_tokens=5, output_tokens=2)).model_dump_json())
        return log, 7
    monkeypatch.setattr(cli, "_run_phase", phase_run)
    with pytest.raises(SystemExit):
        run(tmp_path, "stage1", 100, monkeypatch, "--section", "B")
    assert not calls and not (work / "runs/progress.txt").exists()
    for group in ("sections", "clarifications", "reviews", "notes"):
        (work / group).mkdir(parents=True)
    (work / "plan.md").write_text("## A\n## B\n## C\n")
    for name in ("A", "B", "C"):
        for stem in (f"sections/{name}", f"clarifications/{name}", f"reviews/{name}", f"reviews/{name}-sources"):
            (work / (stem + ".md")).write_text("Synthetic")
    for stage in ("stage1", "stage2"):
        with pytest.raises(SystemExit) as error:
            run(tmp_path, stage, 100, monkeypatch, "--section", "B", "--section", "Unknown")
        assert error.value.code == 2 and "A, B, C" in capsys.readouterr().err
    assert not calls and not (work / "runs/progress.txt").exists()
    select = ("--section", "C", "--section", "A", "--section", "C")
    for stage, phase in (("stage1", "gather"), ("stage2", "revise")):
        calls.clear()
        run(tmp_path, stage, 100, monkeypatch, *select)
        assert calls == [(phase, "A", 0), (phase, "C", 7)]
        assert (tmp_path / "out/report.docx").is_file()
        assert not (tmp_path / ".stage2-complete").exists()
        run(tmp_path, stage, 100, monkeypatch, *select)
        assert len(calls) == 2
        run(tmp_path, stage, 100, monkeypatch, "--section", "B")
        assert calls[-1] == (phase, "B", 0)
    assert (tmp_path / ".stage2-complete").is_file()


# Synthetic fixtures use the eleven fractional weights in the published ranking.
WEIGHTS = {
    "X11": ".17", "X21": ".16", "X12": ".14", "X22": ".12", "X41": ".11", "X31": ".09",
    "X32": ".07", "X42": ".06", "X23": ".04", "X51": ".03", "X52": ".01",
}


def score_rows(scores=None, weights=None):
    rows = ["| 指标 | 档位 | 基本分值范围 | Rij | γij | Xij | 依据 |", "|---|---|---|---|---|---|---|"]
    for key, weight in (weights or WEIGHTS).items():
        r = (scores or {}).get(key, "40")
        rows.append(f"| {key} | B | 25–49 | {r} | {weight} | {Decimal(r) * Decimal(weight):.2f} | evidence |")
    return "\n".join(rows)


def test_rescore_and_sync_resume_with_a_checked_table(tmp_path, monkeypatch, capsys):
    case = tmp_path
    work = case / "work"
    sections = work / "sections"
    sections.mkdir(parents=True)
    (work / "plan.md").write_text("## A\n")
    notes = work / "notes"
    notes.mkdir()
    (notes / "A.md").write_text("Evidence sentinel")
    library = tmp_path / "library"
    library.mkdir()
    (library / "lessons.md").write_text("Library sentinel")
    weights = WEIGHTS
    scores = dict.fromkeys(weights, "40")
    table = score_rows() + "\n\n基准 F=40.00，Ⅱ级。"
    old = table.replace("evidence", "unverified").replace("40.00，Ⅱ级", "55.00，Ⅲ级")
    body = sections / "A.md"
    body.write_text("# A\n\nPrivate prose sentinel\n\n## 适用性\n适用行 sentinel\n\n## 评分\n" + old + "\n\n本坡为Ⅲ级。")
    commission = case / "commission"
    commission.mkdir()
    (commission / "澄清答复.md").write_text("Case-only answer")
    cfg = _settings(case)
    out = case / "out"
    out.mkdir()
    _xlsx(out / cfg["xlsx_file"], cfg["sheet_columns"].split("|"),
          [[7, "Elsewhere", "", "Q?", "", "Open", "Numbered answer", "Client"]], cfg)
    phases = []
    end = AIMessage(content="Saved.", usage_metadata=dict(input_tokens=15, output_tokens=2, total_tokens=17))
    def fake(phase):
        phases.append(phase)
        if phase == "rescore":
            ops = [call("calculate_score", scores=scores, weights=weights, include_products=True),
                   call("write_file", file_path="/work/sections/A.table.md", content=table)]
        elif phase == "sync":
            before, after = ("适用行 sentinel", "核实后的适用行") if phases.count("sync") == 1 else ("本坡为Ⅲ级。", "本坡为Ⅱ级。")
            ops = [call("edit_file", file_path="/work/sections/A.md", old_string=before, new_string=after)]
        else:
            raise AssertionError(phase)
        return ScriptedModel(responses=[m.model_copy(update={"usage_metadata": end.usage_metadata}) for m in ops] + [end])
    monkeypatch.setattr(cli, "make_model", fake)
    run(case, "stage2", 650000, monkeypatch, "--library", str(library))
    assert phases == ["rescore", "sync"]
    progress = (work / "runs/progress.txt").read_text()
    last_line = len(body.read_text().splitlines())
    assert "rescore=450000 sync=200000" in progress
    assert f"line {last_line}" in progress and "\tunfinished\t" in progress and f"line {last_line}" in capsys.readouterr().out
    assert not (case / ".stage2-complete").exists()
    assert table in body.read_text() and "unverified" not in body.read_text()
    def first(phase):
        path, = (work / "runs").glob("*-" + phase + ".jsonl")
        return json.loads(path.read_text().splitlines()[0])["content"]
    assert all(v in first("rescore") for v in ("适用行 sentinel", "Evidence sentinel", "Library sentinel", "7: Numbered answer", "section: Elsewhere", "Q?"))
    assert all("Reviewer notes for this section" not in first(phase) for phase in ("rescore", "sync"))
    assert "Private prose sentinel" not in first("rescore")
    assert "Private prose sentinel" in first("sync") and table in first("sync")
    assert all(v not in first("sync") for v in ("Evidence sentinel", "Library sentinel", "7: Numbered answer"))
    run(case, "stage2", 300000, monkeypatch, "--library", str(library))
    assert phases == ["rescore", "sync", "sync"] and (case / ".stage2-complete").is_file()
    run(case, "stage2", 300000, monkeypatch, "--library", str(library))
    assert phases == ["rescore", "sync", "sync"]


def test_stage2_reviewer_notes_invalidate_both_phases(tmp_path, monkeypatch):
    work = tmp_path / "work"
    sections = work / "sections"
    sections.mkdir(parents=True)
    (work / "plan.md").write_text("## A\n")
    review = work / "reviews/A.notes.md"
    review.parent.mkdir()
    (review.parent / "B.notes.md").write_text("Another section's review")
    weights = WEIGHTS
    table = score_rows() + "\n基准 F=40.00，Ⅱ级。"
    (sections / "A.table.md").write_text(table)
    (sections / "A.md").write_text("# A\n\n" + table)

    phases = []
    def model(phase):
        phases.append(phase)
        ops = []
        if phase == "rescore":
            ops = [call("calculate_score", scores=dict.fromkeys(weights, "40"), weights=weights, include_products=True),
                   call("edit_file", file_path="/work/sections/A.table.md", old_string="40.00，Ⅱ级", new_string="40.00，Ⅱ级")]
        usage = dict(input_tokens=15, output_tokens=2, total_tokens=17)
        return ScriptedModel(responses=[m.model_copy(update={"usage_metadata": usage}) for m in [*ops, AIMessage(content="Checked")]])
    monkeypatch.setattr(cli, "make_model", model)
    heading = "Reviewer notes for this section (apply each one; cite the source the note points to; a note never overrides a source or the scoring rules):"
    # Begin with a valid imported handoff and no completion receipt. The same table bytes are returned each time.
    for text in ("Check source A.\nCheck source B.", "Check source C.", None):
        if text is None:
            review.unlink()
        else:
            review.write_text(text)
        phases.clear()
        run(tmp_path, "stage2", 650000, monkeypatch)
        assert phases == ["rescore", "sync"] and (tmp_path / ".stage2-complete").is_file()
        for phase in phases:
            path = max((work / "runs").glob("*-" + phase + ".jsonl"))
            first = json.loads(path.read_text().splitlines()[0])["content"]
            assert "Another section's review" not in first
            if text is None:
                assert heading not in first
            else:
                assert first.endswith(heading + "\n" + text) and first.count(heading) == 1
        phases.clear()
        run(tmp_path, "stage2", 650000, monkeypatch)
        assert phases == []


def test_table_gate_product_weight_boundary_scenarios_and_scope(tmp_path):
    scores = dict.fromkeys(WEIGHTS, "30")
    scores["X11"] = "30.001"  # Rounded F is 30, but the grade uses the unrounded value.
    text = score_rows(scores) + "\n基准 F=30.00，Ⅱ级。\n情景[低] R11=29，F=29.83，Ⅰ级。"
    assert cli._table_gate(text) == []
    for modified, error in [(text.replace("| 5.10 |", "| 5.00 |"), "weighted X"),
                            (text.replace(".04 | 1.20", ".03 | 0.90", 1), "weights sum"),
                            (text.replace("X52", "X51"), "duplicate"),
                            (text.replace("29.83", "30.00"), "低 F="),
                            (text + "\n本坡维持Ⅲ级。", "baseline F="),
                            (text + "\nF=ΣX=31.15，Ⅱ级。", "baseline F="),
                            (text.replace("30.00，Ⅱ", "30.00，Ⅰ"), "baseline F=")]:
        assert any(error in line for line in cli._table_gate(modified)), (error, modified)
    assert cli._table_gate(text + "\n情景[低] F=29.83，Ⅰ级。", text) == []
    assert cli._table_gate(text.replace("evidence", "different", 1), text)
    assert cli._table_gate(text, calculations=[])
    from reportagent.agent import PhaseTools
    for phase, target in [("rescore", "A.table.md"), ("sync", "A.md")]:
        middleware = PhaseTools(phase, "restricted", tmp_path, "A", ())
        def denied(tool, path):
            return middleware._denied({"id":"test", "name":tool, "args":{"file_path": path}}, [])
        assert (denied("edit_file", "/work/sections/" + target) is None) == (phase == "sync")
        assert denied("edit_file", "/work/sections/B.md").status == "error"
        assert denied("read_file", "/commission/unrelated.txt").status == "error"
        assert (denied("read_file", "/sources/a/page.png") is None) == (phase == "rescore")
        assert (denied("calculate_score", "") is None) == (phase == "rescore")


def test_table_gate_requires_the_assigned_weight_even_if_sum_and_products_agree():
    swapped = dict(WEIGHTS, X12=WEIGHTS["X23"], X23=WEIGHTS["X12"])
    errors = cli._table_gate(score_rows(weights=swapped) + "\n基准 F=40.00，Ⅱ级。")
    for key in ("X12", "X23"):
        assert any(key in line for line in errors), errors


def test_gate_reads_formatted_results_and_scopes_each_named_scenario():
    handoff = score_rows() + (
        "\n基准 F=40.00，Ⅱ级。\n情景[加深] R11=100，F=50.20，Ⅲ级。\n情景[减浅] R11=0，F=33.20，Ⅱ级。"
    )
    prose = (
        "\n按 F＝ΣXij 计算。Ⅳ级（极高）F＞60；45＜F≤60 为Ⅲ级；Ⅱ级（中度）30＜F≤45。"
        "\n基础结果 **F**＝36＋4＝**40.00**（Ⅱ级）。另列计算过程 F＝36＋4。"
        "\n情况分别为情景[加深] _F_＝50.20，为**Ⅲ级**、情景[减浅]（F＝33.20）为Ⅱ级。"
    )
    assert cli._table_gate(handoff + prose, handoff) == []
    for original, wrong in (("50.20，为", "50.21，为"), ("为**Ⅲ级**", "为**Ⅱ级**"),
                            ("**40.00**（Ⅱ级）", "**41.00**（Ⅱ级）"),
                            ("33.20）为Ⅱ级", "33.20）为Ⅰ级"),
                            ("F＝33.20）", "F＝-33.20）")):
        assert cli._table_gate(handoff + prose.replace(original, wrong), handoff), wrong

    shared = "情景[加深] F＝50.20、情景[减浅] F＝33.20，两种情景均为Ⅱ级。"
    assert any("加深" in err for err in cli._table_gate(handoff + "\n" + shared, handoff))
    compatible = handoff + "\n情景[微调] R11=50，F=41.70，Ⅱ级。"
    shared_ok = "情景[微调] F＝41.70、情景[减浅] F＝33.20，两种情景均为Ⅱ级。"
    assert cli._table_gate(compatible + "\n" + shared_ok, compatible) == []
    adjacent = "规定 F＞60 为Ⅳ级，本坡 F＝40.00，为Ⅱ级。"
    assert cli._table_gate(handoff + "\n" + adjacent, handoff) == []
    for wrong in (adjacent.replace("40.00", "42.00"), adjacent.replace("为Ⅱ级", "为Ⅲ级")):
        assert cli._table_gate(handoff + "\n" + wrong, handoff), wrong

    separate = handoff + "\n结果分别为情景[加深]、情景[减浅] F=33.20。"
    assert any("加深" in error for error in cli._table_gate(separate, handoff))
    assert not cli._table_gate(handoff + "\n可另见情景[加深]、情景[减浅]。", handoff)

    missing = handoff.replace("R11=100，F=50.20", "R11=100")
    errors = cli._table_gate(missing + "\n并列记录情景[加深]、情景[减浅] F=33.20。")
    assert any("加深" in error and "missing" in error for error in errors), errors
    assert any("missing" in e for e in cli._table_gate(missing + "\n另处提及情景[加深] F=50.20，Ⅲ级。"))


def test_sync_table_places_one_checked_block_and_keeps_editorial_text():
    checked = score_rows() + "\n\n基准 F=40.00，Ⅱ级。\n情景[加深] R11=100，F=50.20，Ⅲ级。"
    handoff = "# 复核用标题\n\n" + checked + "\n\n备注：仅供核查。"
    typed = checked.replace("evidence", "旧依据").replace("50.20", "51.20")
    intro, ending = "# 章节\n\n前文。\n\n", "\n\n## 讨论\n保留这句。\n"
    section = intro + typed + ending
    synced = cli._sync_table(section, handoff)
    assert synced == intro + checked + ending
    assert cli._sync_table(synced, handoff) == synced
    assert cli._sync_table(section.replace("\n", "\r\n"), handoff) == synced.replace("\n", "\r\n")
    prose = "基准 F=40.00，Ⅱ级。本段另有独立的现场说明。"
    prose_section = intro + score_rows() + "\n\n" + prose + ending
    assert cli._sync_table(prose_section, handoff) == intro + checked + "\n\n" + prose + ending
    for ambiguous in (intro + ending, section + "\n" + typed):
        assert cli._sync_table(ambiguous, handoff) == ambiguous
    invalid = handoff.replace("| .17 | 6.80 |", "| .16 | 6.40 |")
    assert cli._sync_table(section, invalid) == section
