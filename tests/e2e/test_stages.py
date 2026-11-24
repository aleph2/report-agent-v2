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
    assert lines[table_first + 1] == rule


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
