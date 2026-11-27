from importlib.resources import files
from pathlib import Path
from shutil import copyfile
from decimal import Decimal

from docx import Document

from reportagent.export.docx import export_docx


def test_report_layout_preserves_checked_values_and_sources(tmp_path):
    case = tmp_path
    template = case / "commission/template"
    sections = case / "work/sections"
    pages = case / "sources/design"
    for directory in (template, sections, pages):
        directory.mkdir(parents=True)
    pdf = pages.with_suffix(".pdf")
    copyfile(Path(__file__).parents[1] / "fixtures/one-page.pdf", pdf)
    (pages / "page-001.txt").write_text("The   design is reinforced.")
    (template / "report-layout.toml").write_text("""
process_patterns = ["待核实"]
[sections.slope]
caption = "表4.1.1.4-2 测试边坡施工安全总体风险评估表"
drawing = {file = "design.pdf", page = 1, caption = "图4.1.1 K1+000 典型断面图"}
[sections.other]
[sections.missing]
[sections.wrong]
drawing = {file = "design.pdf", page = 1, caption = "Wrong figure"}
[bands]
X11 = [["高", "50–100"], ["低", "0–49"]]
X12 = [["高", "50–100"], ["低", "0–49"]]
""")
    handoff = """| 指标 | 档位 | 基本分值范围 | Rij | γij | Xij | 取值说明 |
|---|---|---|---|---|---|---|
| X11 高度 | 低 | 0–49 | 40 | 0.25 | 10.00 | 详细依据甲 |
| X12 坡形 | 高 | 50–100 | 60 | 0.75 | 45.00 | 详细依据乙 |

基准 F=55.00，III级。
情景[不利] R11=60 F=60.00，III级。
"""
    (sections / "slope.table.md").write_text(handoff)
    (sections / "slope.md").write_text("# Slope\n\n> The design is reinforced. [design.pdf, page 1]\n\n[[drawing]]\n\n"
        "第一句。正文。[[note:待核实地下水]]\n\n[[说明 X11:测试高度。]]\n\n[[说明 X12:测试坡形。]]\n\n" + handoff)
    (sections / "other.md").write_text("# Other\n\n> fabricated. [design.pdf, page 1]\n\n“没有这句话” [design.pdf, page 1]。仍待核实。")
    (sections / "missing.md").write_text("# Missing\n\n[[drawing]]")
    (sections / "wrong.md").write_text("# Wrong\n\n> text. [other.pdf, page 1]\n\n[[drawing]]")
    rendered = Document(export_docx(case))
    notes = "\n".join(c.text for c in rendered.comments)
    body = "\n".join(p.text for p in rendered.paragraphs)
    assert "待核实地下水" in notes and "待核实地下水" not in body
    comment = next(c for c in rendered.comments if c.text == "待核实地下水")
    start, = rendered.element.body.xpath(f'.//w:commentRangeStart[@w:id="{comment.comment_id}"]')
    text = []
    for node in start.itersiblings():
        if node.tag.endswith("}commentRangeEnd"):
            break
        text.extend(node.xpath(".//w:t/text()"))
    assert "".join(text) == "正文。"
    assert "情景[不利]" in notes and "情景[不利]" not in body
    assert len(rendered.inline_shapes) == 1 and "design.pdf 第1页" in body
    assert "Wrong figure" not in body and "断面图" in notes
    assert "表4.1.1.4-2" in body
    table, = rendered.tables
    assert len(table.columns) == 7
    assert [table.cell(i, 1).text for i in (2,3,4,5)] == ["高", "低", "高", "低"]
    assert table.cell(2, 3)._tc is table.cell(3, 3)._tc
    assert table.cell(2, 6).text == "测试高度。"
    assert table.cell(3, 2)._tc.xpath("./w:tcPr/w:shd")
    assert not table.cell(2, 2)._tc.xpath("./w:tcPr/w:shd")
    assert "详细依据甲" in notes
    total = sum(Decimal(table.cell(i, 3).text) * Decimal(table.cell(i, 4).text) for i in (2, 4))
    assert total == Decimal("55") and "55.00" in " ".join(c.text for c in table.rows[-1].cells)
    total_notes = table.rows[-1].cells[-1]._tc.xpath(".//w:commentRangeStart/@w:id")
    assert [c.text for c in rendered.comments if str(c.comment_id) in total_notes] == [
        "总分按各项取值×权重未经舍入求和；表中各项评估分值保留两位小数，合计可能相差0.01"
    ]
    assert (sections / "slope.table.md").read_text() == handoff
    report = (case / "out/client-readiness.txt").read_text()
    assert "fabricated." in report and "没有这句话" in report and "仍待核实" in report and "待核实地下水" not in report

def test_feedback_document_cli_without_workbook(tmp_path, monkeypatch):
    import sys
    from reportagent import cli

    case = tmp_path / "cases/test"
    case.mkdir(parents=True)
    library = tmp_path / "lessons"
    library.mkdir()
    (library / "lessons.md").write_text("Existing rule")
    feedback = tmp_path / "customer.docx"
    doc = Document()
    doc.add_paragraph("Customer observation")
    doc.add_table(rows=1, cols=1).cell(0, 0).text = "Screenshot one description"
    doc.save(feedback)
    captured = []
    monkeypatch.setattr(cli, "_preflight", lambda case, section, phase, messages, cap: captured.append(messages[0].content) or 1)
    monkeypatch.setattr(sys, "argv", ["reportagent", "learn", "propose", "--case", str(case),
        "--feedback", str(feedback), "--max-tokens", "150000", "--preflight"])
    cli.main()
    assert all(t in captured[0] for t in ("Customer observation", "Screenshot one description", "Existing rule", "sync-section"))
    assert not (case / "work").exists()
    from reportagent.export.docx import _settings, _xlsx
    from reportagent.learn import proposal_prompt
    cfg = _settings(case)
    (case / "out").mkdir()
    _xlsx(case / "out" / cfg["xlsx_file"], cfg["sheet_columns"].split("|"),
          [[17, "A", "", "workbook sentinel", "", "Open", "blue", "client"]], cfg)
    combined = proposal_prompt(case, library, [feedback])
    assert all(t in combined for t in ("workbook sentinel", "blue", "Customer observation"))
    skill = files("reportagent.skills").joinpath("learn-from-answers", "SKILL.md").read_text()
    assert all(field in skill for field in ("lesson:", "applies:", "effect:", "tier:", "overlap:", "status: proposed"))

def test_export_uses_accepted_chapters_even_when_drafts_change(tmp_path):
    import hashlib
    import pytest
    case = tmp_path
    template = case / "commission/template"
    template.mkdir(parents=True)
    drafts = case / "work/sections"
    drafts.mkdir(parents=True)
    source = template.parent / "fixed-text.docx"
    accepted = Document()
    titles = ["概述", "评估流程和方法"]
    for title in titles:
        accepted.add_heading(title, 1)
        accepted.add_paragraph("固定原文" + title)
    accepted.save(source)
    hash_ = hashlib.sha256(source.read_bytes()).hexdigest()
    (template / "report-layout.toml").write_text(f'freeze = ["01-概述", "03-评估流程和方法"]\n[fixed_hashes]\n"fixed-text.docx" = "{hash_}"\n')
    (case / "work/plan.md").write_text("---\nsection: 1|01-概述\nsection: 1|03-评估流程和方法\n---\n")
    for name in ("01-概述", "03-评估流程和方法"):
        (drafts / (name + ".md")).write_text("unaccepted replacement")
    actual = [p.text for p in Document(export_docx(case)).paragraphs]
    assert all("固定原文" + t in actual for t in titles) and "unaccepted replacement" not in actual
    accepted.add_paragraph("unapproved change")
    accepted.save(source)
    with pytest.raises(ValueError, match="fixed-text.docx"):
        export_docx(case)

def test_frozen_sections_protected_in_unrestricted_phase(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage
    from tests.fake_model import ScriptedModel, call
    from reportagent.agent import build_agent
    template = tmp_path / "commission/template"
    template.mkdir(parents=True)
    settings = template / "report-layout.toml"
    settings.write_text('freeze = ["01-A"]')
    model = ScriptedModel(responses=[
        call("write_file", file_path="/work/sections/01-A.md", content="overwrite"),
        call("edit_file", file_path="/commission/template/report-layout.toml", old_string="01-A", new_string="Other"),
        call("write_file", file_path="/work/sections/B.md", content="allowed"), AIMessage(content="Done")])
    state = build_agent(tmp_path, model, phase="write").invoke({"messages":[("user","Write sections")]})
    statuses = [m.status for m in state["messages"] if isinstance(m, ToolMessage)]
    assert statuses == ["error", "error", "success"]
    assert not (tmp_path / "work/sections/01-A.md").exists()
    assert (tmp_path / "work/sections/B.md").read_text().strip() == "allowed"
    assert "01-A" in settings.read_text()

def test_sync_only_targets_plain_and_scored_sections(tmp_path, monkeypatch):
    import sys
    from langchain_core.messages import AIMessage
    from reportagent import cli
    from tests.e2e.test_stages import score_rows
    from tests.fake_model import ScriptedModel, call
    work = tmp_path / "work"
    sections = work / "sections"
    sections.mkdir(parents=True)
    (work / "plan.md").write_text("## Plain\n## Scored\n")
    table = score_rows() + "\n\n基准 F=40.00，Ⅱ级。"
    target = sections / "Scored.table.md"
    target.write_text(table)
    for name, text in (("Plain","sample"),("Scored",table)):
        (sections / (name + ".md")).write_text("# " + name + "\n\n" + text)
    (tmp_path / "commission/template").mkdir(parents=True)
    (tmp_path / "commission/template/report-layout.toml").write_text('[sections.Plain]\n[sections.Scored]\n')
    cached = tmp_path / "sources/design"
    cached.mkdir(parents=True)
    copyfile(Path(__file__).parents[1] / "fixtures/one-page.pdf", cached.with_suffix(".pdf"))
    (cached / "page-001.txt").write_text("unique evidence")
    (tmp_path / "commission/template/report-layout.toml").write_text('[sections.Plain]\nexcerpts = {"design.pdf" = [1]}\n[sections.Scored]\n')
    library = tmp_path / "lessons"
    library.mkdir()
    (library / "lessons.md").write_text("adopted lesson sentinel")
    phases = []
    def model(phase, **kwargs):
        name = ["Plain", "Scored"][len(phases)]
        phases.append(phase)
        forbidden = call("edit_file", file_path="/work/sections/Scored.table.md", old_string="40", new_string="90")
        edit = call("edit_file", file_path=f"/work/sections/{name}.md", old_string=f"# {name}", new_string=f"# {name} updated")
        return ScriptedModel(responses=[m.model_copy(update={"usage_metadata":{"input_tokens":4,"output_tokens":1,"total_tokens":5}})
             for m in (forbidden, edit, AIMessage(content="Saved."))])
    monkeypatch.setattr(cli, "make_model", model)
    monkeypatch.setattr(sys, "argv", ["reportagent","stage2",str(tmp_path),"--sync-only","--section","Plain",
                                      "--section","Scored","--max-tokens","150000", "--library", str(library)])
    cli.main()
    assert phases == ["sync", "sync"] and target.read_text() == table
    assert all("updated" in (sections / (n + ".md")).read_text() for n in ("Plain","Scored"))
    assert not (tmp_path / ".stage2-complete").exists()
    from json import loads
    prompts = [loads(p.read_text().splitlines()[0])["content"] for p in sorted((work/"runs").glob("*-sync.jsonl"))]
    assert "unique evidence" in prompts[0] and "unique evidence" not in prompts[1]
    assert all("adopted lesson sentinel" in p for p in prompts)
