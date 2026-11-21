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
    clarification.write_text("number: 1\nanchor: Title\ncontent: Question\n")
    export_docx(tmp_path)
    monkeypatch.setattr(sys, "argv", ["golden", "record", str(tmp_path), "test", "--commit", "HEAD",
                                     "--task", "1", "--model", "scripted", "--date", "2026-11-20"])
    main()
    monkeypatch.setattr(sys, "argv", ["golden", "compare", str(tmp_path)])
    assert main() == 0
    section.write_text("# Title\n\nAfter")
    clarification.write_text("number: 1\nanchor: Title\ncontent: Updated question\n")
    assert main() == 1 and "Updated question" in capsys.readouterr().out
