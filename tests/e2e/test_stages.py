import json
import sys

from docx import Document
from langchain_core.messages import AIMessage

from reportagent import cli
from reportagent.export.docx import _old_answers, _settings, _xlsx
from tests.fake_model import ScriptedModel, call


def run(case, command, cap, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["reportagent", command, str(case), "--max-tokens", str(cap)])
    cli.main()


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
        target, value = {
            "plan": ("/work/plan.md", plan), "gather": (f"/work/notes/{name}.md", "Synthetic notes"),
            "write": (body, f"# {name}\n\nDraft text"),
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
    assert len(lines) == 13
    running = 0
    for line in lines:
        _, _, _, used, total, status, _ = line.split("\t")
        running += int(used)
        assert int(total) == running and status == "done"
    printed = capsys.readouterr().out
    assert all(line in printed for line in lines)
    run(case, "stage1", 300000, monkeypatch)
    assert phases == ["plan", *section_phases, *section_phases]
    # Replace the client sheet as an upload would, then add an owner answer at the configured path.
    headers = cfg["sheet_columns"].split("|")
    _xlsx(book, headers, [[1, "South", "", "Colour?", "", "Open", "Amber", "Client"],
                          [2, "North", "", "Colour?", "", "Open", "", ""]], cfg)
    (case / "commission/answer-key.md").write_text("2: Indigo\n")
    run(case, "stage2", 300000, monkeypatch)
    assert phases[-2:] == ["revise", "revise"]
    for log in (case / "work/runs").glob("*-revise.jsonl"):
        prompt = json.loads(log.read_text().splitlines()[0])["content"]
        assert "Amber" in prompt and "Indigo" in prompt
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
    assert "Unfinished sections: A (revise), B (revise)" in capsys.readouterr().out
    assert "\trevise\t0\t0\tunfinished" in (tmp_path / "work/runs/progress.txt").read_text()
    assert body.read_text() == "Original draft"
    assert len(list((tmp_path / "work/runs").glob("*-revise.jsonl"))) == 1
