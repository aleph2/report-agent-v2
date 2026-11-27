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
[source_titles]
"internal-notes.md" = "设计复核记录"
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
| X12 坡形 | 高 | 50–100 | 60 | 0.75 | 45.00 | 详细依据乙。原始资料未给出，按图注折算水平投影 L=7+2=9 m [design.pdf, page 1]。 |

基准 F=55.00，III级。
情景[不利] R11=60 F=60.00，III级。
"""
    (sections / "slope.table.md").write_text(handoff)
    (sections / "slope.md").write_text("# Slope\n\n> The design is reinforced. [design.pdf, page 1]\n\n[[drawing]]\n\n"
        "第一句。正文。[[note:待核实地下水]]\n\n*R*22=60，*F*=55，2*3=6，2**3=8 [sources/internal-notes.md, page 1]。\n\n[[说明 X11:测试高度。]]\n\n[[说明 X12:测试坡形。]]\n\n" + handoff)
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
    assert len(rendered.inline_shapes) == 1 and "design 第1页" in body
    assert "R22=60，F=55，2*3=6，2^3=8" in body
    assert "设计复核记录" in notes and ".md" not in notes and "**" not in body
    assert "Wrong figure" not in body and "断面图" in notes
    assert "表4.1.1.4-2" in body
    table, = rendered.tables
    assert len(table.columns) == 7
    section = rendered.sections[-1]
    available = (section.page_width - section.left_margin - section.right_margin) // 635
    for tr in table._tbl.tr_lst:
        assert sum(int(tc.tcPr.tcW.w) for tc in tr.tc_lst) + 20 == available
        assert tr.xpath("./w:trPr/w:cantSplit")
        assert all(p.xpath("./w:pPr/w:ind/@w:firstLine") == ["0"] for p in tr.xpath("./w:tc/w:p"))
    assert all(tr.xpath("./w:trPr/w:tblHeader") for tr in table._tbl.tr_lst[:2])
    assert "9 m" in notes and "按图注折算" in notes
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
    assert "缺少量测核查问题" in report
    assert "fabricated." in report and "没有这句话" in report and "仍待核实" in report and "待核实地下水" not in report

def test_measured_values_match_checked_evidence_and_real_questions(tmp_path):
    from reportagent.export.docx import _measurement_notes, _measurement_register, _settings, _xlsx

    cfg = _settings(tmp_path)
    workbook = tmp_path / "checkpoints.xlsx"
    headers = ["边坡", "指标", "取值（坐标与长度为 m）", "图纸与取值依据", "取值方法", "请核实事项"]
    group = ["S", "X12", "−19.25、8.75、37.4°、99.99", "图 A，第9页，sjx 图层", "按 CAD 量测。", "请核对 A 图取点。"]
    _xlsx(workbook, headers, [group], cfg)
    reason = ("当坡角＜5°时取低档：设计坡角 αd=37.4°，原始资料未给出，按 CAD 图纸 sjx 图层量测"
              "（图 A，第9页，坡脚 −19.25 m，水平距 8.75 m）；99.99 m 为直接标注。")
    for available in (True, False):
        doc = Document()
        value = doc.add_paragraph("45")
        body = doc.add_paragraph("X12 的水平距为 8.75 m。")
        extra = doc.add_paragraph("X12 按图注折算得 12.5 m，见图 A 第11页。")
        flags = []
        register = _measurement_register(workbook if available else tmp_path / "absent.xlsx")
        _measurement_notes(doc, "S-test", {"X12": (value, reason)}, [body, extra], register, cfg, flags)
        texts = [c.text for c in doc.comments]
        assert len(texts) == 4
        extra_note, = [v for v in texts if v.startswith("X12 12.5")]
        assert "第11页" in extra_note and "缺少对应" in extra_note
        texts.remove(extra_note)
        assert all("第9页" in v and "sjx" in v for v in texts)
        assert all(("请核对 A 图取点" in v) == available for v in texts)
        assert {v.split("：", 1)[0] for v in texts} == {"X12 −19.25", "X12 8.75", "X12 37.4°"}
        assert sum("缺少量测核查问题" in f for f in flags) == (1 if available else 4)
        assert any("99.99" in f for f in flags) == available
        assert body._p.xpath(".//w:commentRangeStart")
        assert value._p.xpath(".//w:commentRangeStart")


def test_copied_page_footer_references_styles_in_the_destination(tmp_path):
    from docx.enum.section import WD_SECTION
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from lxml import etree

    template = tmp_path / "commission/template"
    template.mkdir(parents=True)
    (template / "report-layout.toml").write_text("[sections.test]")
    (tmp_path / "work").mkdir()
    (tmp_path / "work/plan.md").write_text("---\nsection: 1|01-概述\n---\n")
    source = Document()
    source.add_heading("概述", 1)
    footer = source.sections[0].footer.paragraphs[0]
    source.styles["Footer"].element.set(qn("w:styleId"), "SourceFooterStyle")
    footer._p.get_or_add_pPr().get_or_add_pStyle().val = "SourceFooterStyle"
    box = OxmlElement("w:txbxContent")
    footer._p.append(box)
    para = OxmlElement("w:p")
    box.append(para)
    run = OxmlElement("w:r")
    para.append(run)
    field = OxmlElement("w:instrText")
    field.text = " PAGE "
    run.append(field)
    source.add_section(WD_SECTION.NEW_PAGE)
    source.save(template.parent / "fixed-text.docx")
    doc = Document(export_docx(tmp_path))
    ns = {"w": qn("w:p").split("}")[0][1:]}
    footers = [etree.fromstring(rel.target_part.blob) for rel in doc.part.rels.values() if rel.reltype == RT.FOOTER]
    copied, = [tree for tree in footers if tree.xpath("//w:txbxContent", namespaces=ns)]
    assert copied.xpath("//w:pStyle/@w:val", namespaces=ns) == [doc.styles["Footer"].style_id]
    assert copied.xpath("//w:txbxContent/w:p/w:pPr/w:ind/@w:firstLineChars", namespaces=ns) == ["0"]


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
    from PIL import Image
    import base64
    guide = tmp_path / "sources/reference"
    guide.mkdir()
    copyfile(cached.with_suffix(".pdf"), guide.with_suffix(".pdf"))
    (guide / "page-001.txt").write_text("independent guide evidence")
    Image.new("RGB", (40, 60), "white").save(guide / "page-001.png")
    config = tmp_path / "commission/template/report-layout.toml"
    config.write_text('[sync_guide_pages]\n"reference.pdf" = [1]\n[sections.Plain]\nexcerpts = {"design.pdf" = [1]}\n[sections.Scored]\n')
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
    assert all(isinstance(content, list) for content in prompts)
    for i, content in enumerate(prompts):
        text = "\n".join(b["text"] for b in content if b["type"] == "text")
        assert ("unique evidence" in text) == (i == 0)
        assert "adopted lesson sentinel" in text and "independent guide evidence" in text
        assert "reference" in text and "page-001" in text
        image, = [b for b in content if b["type"] == "image"]
        assert base64.b64decode(image["base64"]) == (guide / "page-001.png").read_bytes()
    (guide / "page-001.png").unlink()
    import pytest
    with pytest.raises(ValueError, match="page-001.png"):
        cli._stage_messages(tmp_path, "Plain", "sync", None, "", "")

def test_internal_channel_dry_run_and_export(tmp_path):
    import json
    import subprocess
    import sys
    from scripts.golden import word

    src = tmp_path / "work/sections"
    src.mkdir(parents=True)
    template = tmp_path / "commission/template"
    template.mkdir(parents=True)
    (template / "report-layout.toml").write_text('process_patterns=["复核人注","待核实","本次已读"]\n[sections.slope]\n')
    text = ("# Slope\n\n[[internal]]\n复核人注：已运行工具。 [private.pdf, page 8]\n[[/internal]]\n\n"
            "复核人注已登记，实测为 46.85 m。\n\n> 现场记录：待核实。 [design.pdf, page 1]\n\n"
            "本次已读资料未给出地下水位。待核实地下水位。\n\n"
            "基准 F=55.85，III级。[design.pdf, page 1][[note:Q135：按图折算 46.85 m，待核实。]]\n")
    (src / "slope.md").write_text(text)
    (src / "bad.md").write_text("# Bad\n\n[[internal]]\n待核实原文。\n[[mystery]]\n")
    (src / "nested.md").write_text("# Nested\n\n[[internal]]\n甲\n[[internal]]\n乙\n[[/internal]]\n[[/internal]]\n")
    cli = [sys.executable, "-m", "reportagent.export.docx", "--dry-run", str(tmp_path)]
    raw = subprocess.check_output(cli)
    assert raw == subprocess.check_output(cli)
    assert not (tmp_path / "out").exists()
    rows = [json.loads(line) for line in raw.splitlines()]
    omit, = [r for r in rows if r["rule"] == "internal block" and r["action"] == "omit"]
    assert omit["line"] == 3 and "private.pdf" in omit["before"] and not omit["after"].strip()
    hits = {(r["line"], r["rule"], r["action"]) for r in rows if r["section"] == "slope"}
    assert {(4, "复核人注", "omit"), (7, "复核人注", "review"), (9, "待核实", "keep"),
            (11, "待核实", "review"), (13, "待核实", "keep")} <= hits
    assert any(r["rule"] == "boundary" and r["action"] == "review" for r in rows if r["section"] == "bad")
    assert all(r["action"] != "omit" for r in rows if r["section"] in ("nested", "bad"))
    path = export_docx(tmp_path)
    first = word(path)
    doc = Document(path)
    body = "\n".join(p.text for p in doc.paragraphs)
    notes = "\n".join(n.text for n in doc.comments)
    assert "private.pdf" not in body + notes and "已运行工具" not in body + notes
    assert all(s in body for s in ("复核人注已登记，实测为 46.85 m。", "现场记录：待核实。", "本次已读资料未给出地下水位。待核实地下水位。",
                                  "基准 F=55.85，III级。", "[[mystery]]", "[[internal]]", "待核实原文。"))
    assert "Q135：按图折算 46.85 m，待核实。" in notes and "design.pdf, page 1" in notes
    assert "boundary" in (tmp_path / "out/client-readiness.txt").read_text()
    assert first == word(export_docx(tmp_path))
    assert (src / "slope.md").read_text() == text


def test_internal_markers_in_examples_and_independent_invalid_groups():
    from reportagent.export.docx import _channels

    literal = "```text\n[[internal]]\nexample\n[[/internal]]\n```\n"
    valid = "[[internal]]\nprivate search record\n[[/internal]]\n"
    unknown = "[[internal]]\n[[unknown]]\nvisible\n[[/internal]]\n"
    raw = literal + valid + unknown + "[internal]\n[[/internal]]\n"
    visible, spans, findings = _channels(raw)
    assert spans == [(5, 7)] and [n for n, _ in findings] == [9, 12, 13]
    assert literal in visible and unknown in visible and "[internal]" in visible
    assert "private search record" not in visible

    wrapped = "[[future:pending]]\n" + valid + "[[/future]]\n" + valid
    visible, spans, findings = _channels(wrapped)
    assert len(spans) == 1 and visible.count("private search record") == 1
    assert [n for n, _ in findings] == [0, 4]
