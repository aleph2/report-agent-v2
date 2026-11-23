import sys

from docx import Document

from reportagent.export.docx import _settings, _xlsx, export_docx
from scripts.golden import compare_word, compare_excel, main


def test_word_detects_table_text_and_moved_or_changed_comments(tmp_path):
    def specimen(name, cell_text, anchor, note):
        doc = Document()
        runs = [doc.add_paragraph().add_run("same text") for _ in range(2)]
        doc.add_table(rows=1, cols=1).cell(0, 0).text = cell_text
        doc.add_comment(runs[anchor], note, author="Owner")
        doc.core_properties.title = name
        path = tmp_path / name
        doc.save(path)
        return path

    old = specimen("old.docx", "before", 0, "why\nanswer")
    same = specimen("same.docx", "before", 0, "why\nanswer")
    moved = specimen("moved.docx", "before", 1, "why\nanswer")
    changed = specimen("new.docx", "after", 0, "why\nnew answer")
    assert not compare_word(old, same)[1]
    assert compare_word(old, moved)[1]
    diff = "\n".join(compare_word(old, changed)[1])
    assert all(term in diff for term in ["before", "after", "answer", "new answer"])


def test_excel_reports_addresses_and_all_changes(tmp_path):
    cfg = _settings(tmp_path)
    old, new = tmp_path / "old.xlsx", tmp_path / "new.xlsx"
    _xlsx(old, ["ID", "Answer"], [[1, "keep"], [2, "old"]], cfg)
    _xlsx(new, ["ID", "Answer"], [[1, "keep"], [2, "new"], [3, "added"]], cfg)
    assert not compare_excel(old, old)[1]
    diff = "\n".join(compare_excel(old, new)[1])
    assert all(term in diff for term in ["B3", "old", "new", "A4", "B4", "added"])
    assert "B2" not in diff


def test_record_and_compare_case(tmp_path, monkeypatch, capsys):
    for folder in ("sections", "clarifications", "notes"):
        (tmp_path / "work" / folder).mkdir(parents=True)
    (tmp_path / "work/notes/test.md").touch()
    section = tmp_path / "work/sections/test.md"
    section.write_text("# Title\n\nBefore")
    clarification = tmp_path / "work/clarifications/test.md"
    clarification.write_text("number: 1\naudience: client\nanchor: Title\ncontent: Question\n")
    export_docx(tmp_path)
    monkeypatch.setattr(sys, "argv", ["golden", "record", str(tmp_path), "test", "--commit", "HEAD",
                                     "--task", "1", "--model", "scripted", "--date", "2026-11-20"])
    main()
    monkeypatch.setattr(sys, "argv", ["golden", "compare", str(tmp_path)])
    assert main() == 0
    section.write_text("# Title\n\nAfter")
    clarification.write_text("number: 1\naudience: client\nanchor: Title\ncontent: Updated question\n")
    assert main() == 1 and "Updated question" in capsys.readouterr().out


def test_client_audience_flags_and_feedback(tmp_path):
    from reportagent.cli import _revision_messages
    from reportagent.export.docx import _old_answers

    work = tmp_path / "work"
    for folder in ("sections", "clarifications", "notes"):
        (work / folder).mkdir(parents=True)
    (work / "notes/private-17.md").touch()
    (work / "notes/demo.md").touch()
    (work / "sections/demo.md").write_text("# Demo\n\nSee private-17.md. Baseline F=51.00 III.")
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources/public.pdf").touch()
    (work / "notes/public.pdf").touch()
    official = work / "clarifications/demo.md"
    rows = [(1, "client"), (2, "method"), (3, "internal"), (4, "")]
    official.write_text("\n---\n".join(
        f"number: {n}\naudience: {kind}\nanchor: Demo\ncontent: Question {n}. private-17.md /work/notes and public.pdf. 若 F=50.50–61.00。Baseline F=64.00 IV.\neffect: baseline F=64.00 IV\n"
        for n, kind in rows))
    target = export_docx(tmp_path)
    cfg = _settings(tmp_path)
    sheet = target.parent / cfg["xlsx_file"]
    assert set(_old_answers(sheet, cfg)) == {1}
    register = work / "question-registers"
    assert list(_old_answers(register / "chapter-03-method.xlsx", cfg)) == [2]
    assert list(_old_answers(register / "internal.xlsx", cfg)) == [3]
    assert list(_old_answers(register / "unclassified.xlsx", cfg)) == [4]
    assert len(Document(target).comments) == 1
    status = (target.parent / "client-readiness.txt").read_text()
    assert "NOT CLIENT-READY" in status and "private-17.md" in status
    assert "Word word/comments.xml" in status and "Excel items.xlsx!D2 (Q1) | private-17.md" in status
    assert "public.pdf" not in status and "64.00/IV" in status and "/work/" in status
    assert "private-17.md" in _revision_messages(tmp_path, "demo", "revise")[0].text
    assert "50.50" in status and "61.00" in status
    (work / "sections/demo.md").write_text("# Demo\n\nBaseline F=51.00 III. 若 F=50.50–61.00。")
    official.write_text(official.read_text().replace("64.00 IV", "51.00 III").replace("private-17.md /work/notes", "source").replace("audience: \n", "audience: internal\n"))
    export_docx(tmp_path)
    assert "NOT CLIENT-READY" not in (target.parent / "client-readiness.txt").read_text()

    fields = cfg["sheet_fields"].split("|")
    answer = dict(number=1, status="Answered", answer="Updated after site inspection", responder="Owner")
    _xlsx(sheet, cfg["sheet_columns"].split("|"), [[answer.get(k, "") for k in fields]], cfg)
    official.write_text(official.read_text().replace("audience: client", "audience: method"))
    export_docx(tmp_path)
    assert _old_answers(register / "chapter-03-method.xlsx", cfg)[1][1:] == [answer["answer"], "Owner"]
    official.write_text(official.read_text().replace("audience: method", "audience: client", 1))
    export_docx(tmp_path)
    assert _old_answers(sheet, cfg)[1][1:] == [answer["answer"], "Owner"]


def test_readiness_flags_case_paths_and_commission_markdown(tmp_path):
    from reportagent.export.docx import section_flags

    (tmp_path / "commission").mkdir()
    (tmp_path / "commission/owner-notes.md").touch()
    (tmp_path / "work/sections").mkdir(parents=True)
    (tmp_path / "work/sections/demo.md").write_text(
        "commission/owner-notes.md\nowner-notes.md\n/sources/design.pdf\n"
        "work\\notes\\page.txt\ndesign.pdf\nresources/design.pdf\n"
    )
    flags = list(section_flags(tmp_path, "demo"))
    assert flags == [
        "sections/demo.md:1 | commission/", "sections/demo.md:1 | owner-notes.md",
        "sections/demo.md:2 | owner-notes.md", "sections/demo.md:3 | /sources/",
        "sections/demo.md:4 | work\\",
    ]
