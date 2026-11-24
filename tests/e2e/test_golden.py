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
    uploaded = sheet.read_bytes()
    official.write_text(official.read_text().replace("audience: client", "audience: method"))
    export_docx(tmp_path)
    assert sheet.read_bytes() == uploaded
    assert "preserved" in (target.parent / "client-readiness.txt").read_text()
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


def test_contents_has_visible_headings_and_tables_have_inner_borders(tmp_path):
    from docx.oxml.ns import qn

    work = tmp_path / "work"
    sections = work / "sections"
    sections.mkdir(parents=True)
    (work / "plan.md").write_text("---\ntitle: Example\nsection: 1|one\nsection: 2|missing\n---\n")
    (sections / "one.md").write_text(
        "# Actual title\n\n## Subheading\n\n#### Below TOC depth\n\n"
        "| A | B |\n|---|---|\n| 1 | 2 |\n"
    )
    doc = Document(export_docx(tmp_path))
    before_break = []
    for p in doc.paragraphs:
        if p._p.xpath(".//w:br[@w:type='page']"):
            break
        before_break.append(p)
    start = next(i for i, p in enumerate(before_break) if p._p.xpath(".//w:instrText[contains(., 'TOC ')]"))
    entries = before_break[start:]
    assert [p.text for p in entries] == ["Actual title", "Subheading", "missing"]
    assert entries[0]._p.xpath(".//w:fldChar[@w:fldCharType='separate']")
    assert entries[-1]._p.xpath(".//w:fldChar[@w:fldCharType='end']")
    borders = doc.tables[0]._tbl.tblPr.find(qn("w:tblBorders"))
    for edge in ("insideH", "insideV"):
        assert borders.find(qn("w:" + edge)).attrib == borders.find(qn("w:top")).attrib


def test_client_guard_counts_answers_by_question_across_sheets(tmp_path):
    from xml.etree import ElementTree as ET
    from zipfile import ZipFile
    from reportagent.export.docx import _missing_client_answers

    cfg = _settings(tmp_path)
    fields, headers = cfg["sheet_fields"].split("|"), cfg["sheet_columns"].split("|")
    target = tmp_path / "uploaded.xlsx"
    sheets = []
    for n in (1, 2):
        sheet = tmp_path / f"{n}.xlsx"
        item = dict(number=n, answer=f"Client answer {n}")
        _xlsx(sheet, headers, [[item.get(field, "") for field in fields]], cfg)
        with ZipFile(sheet) as book:
            sheets.append({path: book.read(path) for path in book.namelist()})
    parts = sheets[0]
    for path, node_name, updates in (
            ("xl/workbook.xml", "sheet", {"name": "Other answers", "sheetId": "2",
             "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id": "rId7"}),
            ("xl/_rels/workbook.xml.rels", "Relationship", {"Id": "rId7", "Target": "worksheets/sheet2.xml"}),
            ("[Content_Types].xml", "Override", {"PartName": "/xl/worksheets/sheet2.xml"})):
        root = ET.fromstring(parts[path])
        original = next(el for el in root.iter() if el.tag.endswith("}" + node_name) and
                        (node_name == "sheet" or "sheet1.xml" in " ".join(el.attrib.values())))
        parent = next(el for el in root.iter() if original in list(el))
        ET.SubElement(parent, original.tag, original.attrib | updates)
        parts[path] = ET.tostring(root)
    parts["xl/worksheets/sheet2.xml"] = sheets[1]["xl/worksheets/sheet1.xml"]
    with ZipFile(target, "w") as book:
        for path, data in parts.items():
            book.writestr(path, data)
    selected = [dict(number=1, answer="A"), dict(number=3, answer="New answer")]
    assert _missing_client_answers(target, cfg, selected) == 1
    selected[1]["number"] = 2
    assert _missing_client_answers(target, cfg, selected) == 0


def test_client_fixed_text_preserves_word_parts(tmp_path, capsys):
    from docx.oxml import parse_xml
    from docx.oxml.ns import qn
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part

    case = tmp_path
    commission = case / "commission"
    commission.mkdir()
    sections = case / "work/sections"
    sections.mkdir(parents=True)
    names = ["01-概述", "02-工程概况", "03-评估流程和方法", "04-范围", "05-评估结论及建议", "06-评估结果自我评价及遗留问题说明"]
    order = [(1, n) for n in names]
    for i, n in ((5, "05-4-保留"), (3, "03-1-不再导出")):
        order.insert(i, (2, n))
        (sections / (n + ".md")).write_text("Child " + n)
    (case / "work/plan.md").write_text("---\n" + "".join(f"section: {level}|{n}\n" for level, n in order) + "---")
    for name in names:
        (sections / (name + ".md")).write_text("Draft " + name)
    src = Document()
    src.add_paragraph("前 言")
    para = src.add_paragraph("湖【项")
    para.add_run("目名】（【缺项】）").bold = True
    src.add_heading("概述", 1)
    item = src.add_paragraph("Synthetic numbered item")
    num = item._p.get_or_add_pPr().get_or_add_numPr()
    num.get_or_add_numId().val = 5
    src.add_heading("", 1)
    src.add_heading("工程概况", 1)
    src.add_paragraph("Excluded stub")
    src.add_heading("评估流程和方法", 1)
    tbl = src.add_table(rows=2, cols=2)
    tbl.cell(0, 0).merge(tbl.cell(0, 1)).text = "Merged 【项目名】"
    equation = src.add_paragraph()
    base = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
    def relate(name, content_type, data, kind):
        part = Part(PackURI(name), content_type, data, src.part.package)
        return src.part.relate_to(part, base + kind)
    obj = relate("/word/embeddings/oleObject1.bin", "application/vnd.openxmlformats-officedocument.oleObject", b"synthetic-ole", "oleObject")
    img = relate("/word/media/image1.wmf", "image/x-wmf", b"synthetic-preview", "image")
    equation.add_run()._r.append(parse_xml(f'<w:object xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="{base[:-1]}" xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:v="urn:schemas-microsoft-com:vml"><v:shape><v:imagedata r:id="{img}"/></v:shape><o:OLEObject Type="Embed" ProgID="Synthetic.Equation" r:id="{obj}"/></w:object>'))
    src.add_heading("5.5风险控制措施建议", 2)
    src.add_paragraph("Value 【评估对象有几处，填在这里】")
    src.add_heading("6、评估结果自我评价及遗留问题说明", 1)
    src.add_paragraph("Fixed end")
    src.save(commission / "fixed-text.docx")
    (commission / "fixed-text-values.txt").write_text("【项目名】=>Test\n【评估对象有几处，填在这里】=>5\n湖【项目名】=>【项目名】\n")
    doc = Document(export_docx(case))
    body = [p.text for p in doc.paragraphs]
    assert [p for p in body if p.startswith("Draft ")] == ["Draft " + names[i] for i in (1, 3, 4)]
    assert body.index("Test（【缺项】）") < body.index("Synthetic numbered item") < body.index("Draft " + names[1])
    assert body.index("Draft " + names[4]) < body.index("Value 5") < body.index("Fixed end") and "Excluded stub" not in body
    assert doc.tables[0].cell(0, 0).text == "Merged Test" and doc.tables[0].cell(0, 0)._tc.grid_span == 2
    for tag, payload in [("OLEObject", b"synthetic-ole"), ("imagedata", b"synthetic-preview")]:
        el, = doc.element.xpath('.//*[local-name()="' + tag + '"]')
        assert doc.part.related_parts[el.get(qn("r:id"))].blob == payload
    title = next(p for p in doc.paragraphs if p.style.name == "Heading 2" and p.text.startswith("5.5"))
    assert title._p.pPr.numPr.numId.val == 0
    assert "Child 03-1-不再导出" not in body
    assert body.index("Draft " + names[4]) < body.index("Child 05-4-保留") < body.index("Value 5")
    assert all(p.text.strip() for p in doc.paragraphs if p.style.name.startswith("Heading "))
    copied = next(p for p in doc.paragraphs if p.text == item.text)
    num_id = copied._p.pPr.numPr.numId.val
    assert num_id != 5 and doc.part.numbering_part.element.xpath(f'w:num[@w:numId="{num_id}"]')
    assert "【缺项】" in (case / "out/client-readiness.txt").read_text()
    src.paragraphs[-2]._p.getparent().remove(src.paragraphs[-2]._p)
    src.save(commission / "fixed-text.docx")
    export_docx(case)
    assert "评估结果自我评价及遗留问题说明" in capsys.readouterr().out
