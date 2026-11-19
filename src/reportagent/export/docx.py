from importlib.resources import files
from io import BytesIO
from html import unescape
from pathlib import Path
from math import ceil
from unicodedata import east_asian_width
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile, ZIP_DEFLATED
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

CITATION = r"\[[^\]\n]*pages?\s*\d[^\]\n]*\]"


def _inline(document, paragraph, text, fallback=()):
    current, previous, bold, italic, offset = [], list(fallback), False, False, 0
    emphasis = {position for match in re.finditer(r"(?<![A-Za-z0-9_*])\*(?![\s*]).+?(?<![\s*])\*(?![A-Za-z0-9_*])|(?<![A-Za-z0-9_])_(?![\s_]).+?(?<![\s_])_(?![A-Za-z0-9_])", text) for position in (match.start(), match.end() - 1)}
    for token in re.split(r"(\*\*|\*|_|" + CITATION + r")", text, flags=re.I):
        position, offset = offset, offset + len(token)
        if token == "**":
            bold = not bold
        elif token in ("*", "_") and position in emphasis:
            italic = not italic
        elif re.fullmatch(CITATION, token, flags=re.I):
            anchor = current or previous or [paragraph.add_run("\u200b")]
            # https://python-docx.readthedocs.io/en/latest/user/comments.html
            document.add_comment(anchor, "来源：" + token[1:-1].strip(), author="Report Agent")
        else:
            token = re.sub(r"<(sub|sup)>(.*?)</\1>", lambda m: ("_" if m[1].lower() == "sub" else "^") + m[2], token, flags=re.I)
            token = unescape(re.sub(r"<br\s*/?>", "\n", token, flags=re.I))
            for part in re.split(r"([。！？!?]|\.(?=\s|$))", token):
                if not part:
                    continue
                run = paragraph.add_run(part)
                if bold:
                    run.bold = True
                if italic:
                    run.italic = True
                current.append(run)
                if re.fullmatch(r"[。！？!?]|\.", part):
                    previous, current = current, []
    return current or previous


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith(r"\|"):
        line = line[:-1]
    return [c.strip().replace(r"\|", "|") for c in re.split(r"(?<!\\)\|", line)]


# shortcut: report Markdown only; use a parser for nested lists or fenced blocks.
def _blocks(text):
    lines, i = text.splitlines(), 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)", line)
        if heading:
            yield "heading", (len(heading[1]), heading[2])
            i += 1
        elif i + 1 < len(lines) and "|" in line and all(re.fullmatch(r":?-{3,}:?", c) for c in _cells(lines[i + 1])):
            rows = [_cells(line)]
            alignment = [WD_ALIGN_PARAGRAPH.CENTER if c.startswith(":") and c.endswith(":") else WD_ALIGN_PARAGRAPH.RIGHT if c.endswith(":") else WD_ALIGN_PARAGRAPH.LEFT for c in _cells(lines[i + 1])]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_cells(lines[i]))
                i += 1
            yield "table", (rows, alignment)
        else:
            paragraph = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#|[-*] |\d+[.)、]|[（(]\d+[）)])", lines[i].strip()) and not (i + 1 < len(lines) and "|" in lines[i] and re.match(r"^[\s|:\-]+$", lines[i + 1])):
                paragraph.append(lines[i].strip())
                i += 1
            yield "paragraph", " ".join(paragraph)


def _table(document, rows, alignment):
    table = document.add_table(rows=0, cols=max(map(len, rows)))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    # Word autofit can collapse numeric columns until decimals wrap.
    table.autofit = False
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right"):
        border = OxmlElement("w:" + edge)
        border.set(qn("w:val"), "double")
        border.set(qn("w:sz"), "4")
        borders.append(border)
    table._tbl.tblPr.append(borders)
    for i, values in enumerate(rows):
        row, preceding = table.add_row(), []
        if i == 0:
            row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for j, cell in enumerate(row.cells):
            value = values[j] if j < len(values) else ""
            paragraph = cell.paragraphs[0]
            paragraph.style = document.styles["Table Text"]
            if j < len(alignment):
                paragraph.alignment = alignment[j]
            _inline(document, paragraph, value, preceding)
            preceding.extend(r for r in paragraph.runs if r.text)
            if i == 0:
                for run in paragraph.runs:
                    run.bold = True



def _field(paragraph, instruction):
    # Adapted from v1: native fields retain their instructions for Word refresh.
    for kind in ("begin", "separate", "end"):
        element = OxmlElement("w:fldChar")
        element.set(qn("w:fldCharType"), kind)
        if kind == "begin":
            element.set(qn("w:dirty"), "true")
        paragraph.add_run()._r.append(element)
        if kind == "begin":
            code = OxmlElement("w:instrText")
            code.set(qn("xml:space"), "preserve")
            code.text = " " + instruction + " "
            paragraph.add_run()._r.append(code)


def _heading_text(title):
    return re.sub(r"^(?:\d+(?:[.\-]\d+)*[.、]?\s+|\d+(?:-\d+)*-|[一二三四五六七八九十]+、)", "", title).strip()


def _ordered_sections(case):
    folder = case / "work/sections"
    plan = case / "work/plan.md"
    if not plan.exists():
        return [(p.stem, 1, p) for p in sorted(folder.glob("*.md"))], ""
    text = plan.read_text(encoding="utf-8")
    names = re.findall(r"^## (\d+(?:-\d+)*-[^\n]+)", text, re.M)
    subject = re.search(r"^报告对象[：:]\s*(.+)", text, re.M)
    return [(name, len(re.match(r"\d+(?:-\d+)*", name)[0].split("-")), folder / f"{name}.md") for name in names], subject[1].rstrip("。") if subject else ""


def _render(document, path, name, root_level, issues):
    previous, missing = [], None
    blocks = list(_blocks(path.read_text(encoding="utf-8")))
    for i, (kind, value) in enumerate(blocks):
        if kind == "table":
            _table(document, *value)
        elif kind == "heading":
            level, title = value
            level += root_level - 1
            if missing is not None and level <= missing:
                missing = None
            if "缺失与待补充事项" in title:
                missing = level
            previous = _inline(document, document.add_heading("", level), _heading_text(title), previous)
        else:
            item = re.match(r"^(?:[（(](\d+)[）)]|(\d+)[.)、]|[-*](?=\s))\s*", value)
            caption = i + 1 < len(blocks) and blocks[i + 1][0] == "table" and re.match(r"^(?:\*\*)?[表图]\s*\d", value)
            style = "List Paragraph" if item else "Caption" if caption else None
            if item and (item[1] or item[2]):
                value = f"（{item[1] or item[2]}）" + value[item.end():]
            paragraph = document.add_paragraph(style=style)
            previous = _inline(document, paragraph, value, previous)
            if style == "Caption":
                paragraph.paragraph_format.keep_with_next = paragraph.text.startswith("表")
            if missing is not None and item:
                number = len(issues) + 1
                content = re.sub(r"^[（(]\d+[）)]\s*|^[-*]\s*", "", paragraph.text)
                sources = "；".join(m[1:-1].strip() for m in re.findall(CITATION, value, flags=re.I))
                first = re.match(r".+?(?:[。！？!?]|\.(?=\s|$)|$)", content)[0]
                issues.append([number, name, first, content, sources, "待澄清"])
                document.add_comment([r for r in paragraph.runs if r.text], f"待澄清编号：{number}\n{content}", author="Report Agent")


def _xlsx(target, issues):
    # Single-sheet SpreadsheetML keeps this data export independent of extra packages.
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheet = Element("worksheet", xmlns=ns)
    view = SubElement(SubElement(sheet, "sheetViews"), "sheetView", workbookViewId="0", showGridLines="0")
    SubElement(view, "pane", xSplit="2", ySplit="1", topLeftCell="C2", activePane="bottomRight", state="frozen")
    widths = [8, 28, 42, 90, 64, 14]
    columns = SubElement(sheet, "cols")
    for i, width in enumerate(widths, 1):
        SubElement(columns, "col", min=str(i), max=str(i), width=str(width), customWidth="1")
    data = SubElement(sheet, "sheetData")
    rows = [["编号", "章节", "位置（段落首句）", "内容", "依据/来源", "状态"], *issues]
    for i, values in enumerate(rows, 1):
        lines = max(sum(max(1, ceil(sum(2 if east_asian_width(c) in "WF" else 1 for c in line) / (width - 2))) for line in str(value).split("\n")) for value, width in zip(values, widths))
        row = SubElement(data, "row", r=str(i), ht=str(30 if i == 1 else min(409.5, max(30, (lines + 1) * 15))), customHeight="1")
        for j, value in enumerate(values):
            cell = SubElement(row, "c", r=f"{chr(65+j)}{i}", t="n" if isinstance(value, int) else "inlineStr", s="2" if i == 1 else "3" if j == 0 else "1")
            if isinstance(value, int):
                SubElement(cell, "v").text = str(value)
            else:
                SubElement(SubElement(cell, "is"), "t", {"xml:space": "preserve"}).text = value
    SubElement(sheet, "autoFilter", ref=f"A1:F{len(rows)}")
    styles = Element("styleSheet", xmlns=ns)
    fonts = SubElement(styles, "fonts", count="2")
    for bold in (False, True):
        font = SubElement(fonts, "font")
        SubElement(font, "sz", val="11"); SubElement(font, "name", val="Arial")
        if bold:
            SubElement(font, "b")
    fills = SubElement(styles, "fills", count="3")
    for pattern in ("none", "gray125", "solid"):
        fill = SubElement(SubElement(fills, "fill"), "patternFill", patternType=pattern)
        if pattern == "solid":
            SubElement(fill, "fgColor", rgb="FFD9E2F3")
    SubElement(SubElement(styles, "borders", count="1"), "border")
    SubElement(SubElement(styles, "cellStyleXfs", count="1"), "xf", numFmtId="0", fontId="0", fillId="0", borderId="0")
    xfs = SubElement(styles, "cellXfs", count="4")
    for i in range(4):
        xf = SubElement(xfs, "xf", numFmtId="1" if i == 3 else "0", fontId="1" if i == 2 else "0", fillId="2" if i == 2 else "0", borderId="0", xfId="0", applyAlignment="1")
        SubElement(xf, "alignment", wrapText="1", vertical="center" if i in (2, 3) else "top", horizontal="center" if i == 2 else "right" if i == 3 else "left")
    SubElement(SubElement(styles, "cellStyles", count="1"), "cellStyle", name="Normal", xfId="0", builtinId="0")
    book = Element("workbook", {"xmlns": ns, "xmlns:r": rel})
    SubElement(SubElement(book, "sheets"), "sheet", {"name": "待澄清清单", "sheetId": "1", "r:id": "rId1"})
    relationships = Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    SubElement(relationships, "Relationship", Id="rId1", Type=rel + "/worksheet", Target="worksheets/sheet1.xml")
    SubElement(relationships, "Relationship", Id="rId2", Type=rel + "/styles", Target="styles.xml")
    root_rel = Element("Relationships", xmlns="http://schemas.openxmlformats.org/package/2006/relationships")
    SubElement(root_rel, "Relationship", Id="rId1", Type=rel + "/officeDocument", Target="xl/workbook.xml")
    types = Element("Types", xmlns="http://schemas.openxmlformats.org/package/2006/content-types")
    SubElement(types, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    SubElement(types, "Default", Extension="xml", ContentType="application/xml")
    for part, kind in [("/xl/workbook.xml", "sheet.main"), ("/xl/worksheets/sheet1.xml", "worksheet"), ("/xl/styles.xml", "styles")]:
        SubElement(types, "Override", PartName=part, ContentType=f"application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}+xml")
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name, element in [("[Content_Types].xml", types), ("_rels/.rels", root_rel), ("xl/workbook.xml", book), ("xl/_rels/workbook.xml.rels", relationships), ("xl/worksheets/sheet1.xml", sheet), ("xl/styles.xml", styles)]:
            archive.writestr(name, tostring(element, encoding="utf-8", xml_declaration=True))


def export_docx(case_dir):
    case_dir = Path(case_dir)
    document = Document(BytesIO(files("reportagent.export").joinpath("reference.docx").read_bytes()))
    ordered, title = _ordered_sections(case_dir)
    issues = []
    if (case_dir / "work/plan.md").exists():
        project, _, report = title.rpartition(" ")
        if project:
            document.add_paragraph(project, "Subtitle")
        document.add_paragraph(report or title or "报告", "Title")
        document.add_paragraph("目录", "TOC Heading")
        _field(document.add_paragraph(), r'TOC \o "1-3" \h \z \u')
        document.add_page_break()
        cells = document.sections[0].header.tables[0].rows[0].cells
        cells[0].paragraphs[0].add_run(project)
        cells[1].paragraphs[0].add_run(report or title)
    for name, level, path in ordered:
        if path.exists():
            _render(document, path, name, level, issues)
        else:
            code = re.match(r"\d+(?:-\d+)*", name)[0]
            document.add_heading(name[len(code) + 1:], level=level)
            if not any(other.startswith(code + "-") and candidate.exists() for other, _, candidate in ordered):
                document.add_paragraph("本章待编写")
    output = case_dir / "out"
    output.mkdir(parents=True, exist_ok=True)
    target = output / "report.docx"
    document.save(target)
    _xlsx(output / "待澄清清单.xlsx", issues)
    return target
