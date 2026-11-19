from copy import deepcopy
from importlib.resources import files
from io import BytesIO
from html import unescape
from pathlib import Path
from math import ceil
from unicodedata import east_asian_width
from xml.etree.ElementTree import Element, SubElement, fromstring, tostring
from zipfile import ZipFile, ZIP_DEFLATED
import re

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt


def _inline(document, paragraph, text, cfg, fallback=()):
    current, previous, bold, italic, offset = [], list(fallback), False, False, 0
    emphasis = {position for match in re.finditer(r"(?<![A-Za-z0-9_*])\*(?![\s*]).+?(?<![\s*])\*(?![A-Za-z0-9_*])|(?<![A-Za-z0-9_])_(?![\s_]).+?(?<![\s_])_(?![A-Za-z0-9_])", text) for position in (match.start(), match.end() - 1)}
    for token in re.split(r"(\*\*|\*|_|" + cfg["citation_pattern"] + r")", text, flags=re.I):
        position, offset = offset, offset + len(token)
        if token == "**":
            bold = not bold
        elif token in ("*", "_") and position in emphasis:
            italic = not italic
        elif re.fullmatch(cfg["citation_pattern"], token, flags=re.I):
            anchor = (current if any(r.text.strip() for r in current) else previous) or [paragraph.add_run("\u200b")]
            # https://python-docx.readthedocs.io/en/latest/user/comments.html
            document.add_comment(anchor, cfg["citation_prefix"] + token[1:-1].strip(), author=cfg["comment_author"])
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
    return current if any(r.text.strip() for r in current) else previous


def _cells(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith(r"\|"):
        line = line[:-1]
    return [c.strip().replace(r"\|", "|") for c in re.split(r"(?<!\\)\|", line)]


# shortcut: report Markdown only; use a parser for nested lists or fenced blocks.
def _blocks(text, cfg):
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
            while i < len(lines) and lines[i].strip() and not re.match(r"^#", lines[i].strip()) and not re.match(cfg["list_pattern"], lines[i].strip()) and not (i + 1 < len(lines) and "|" in lines[i] and re.match(r"^[\s|:\-]+$", lines[i + 1])):
                paragraph.append(lines[i].strip())
                i += 1
            yield "paragraph", " ".join(paragraph)


def _column_widths(document, rows, cfg):
    section = document.sections[-1]
    available = (section.page_width - section.left_margin - section.right_margin) / 12700
    size, padding = float(cfg["table_size"]), float(cfg["table_padding"])
    # Character-width estimates size content; Word rendering verifies the final fit.
    def measure(value):
        return max(sum(2 if east_asian_width(c) in "WF" else 1 for c in line) * size / 2 for line in value.split("\n"))
    clean = [[unescape(re.sub(r"<[^>]+>|\*|_", "", re.sub(cfg["citation_pattern"], "", value, flags=re.I))) for value in row] for row in rows]
    columns = [[row[j] if j < len(row) else "" for row in clean] for j in range(max(map(len, rows)))]
    maxima = [max(map(measure, column)) for column in columns]
    widths, text_columns = [], []
    for j, column in enumerate(columns):
        values = [v for v in column[1:] if v.strip()]
        numeric = values and all(re.fullmatch(r"[\d\s.+−–—~～/%:×=<>≤≥／-]+", v) for v in values)
        if numeric:
            content = max((measure(part) for value in values for part in re.split(r"[\s/／]+", value) if part), default=0)
            widths.append(max(2 * size, content) + padding)
        else:
            text_columns.append(j)
            widths.append(min(max(2 * size, maxima[j]) + padding, available / len(columns)))
    if text_columns:
        longest = max(text_columns, key=lambda j: maxima[j])
        widths[longest] = max(2 * size + padding, available - sum(widths) + widths[longest])
    else:
        widths = [w + max(0, available - sum(widths)) / len(widths) for w in widths]
    return [Pt(w * available / sum(widths)) for w in widths]


def _table(document, rows, alignment, cfg):
    table = document.add_table(rows=0, cols=max(map(len, rows)))
    table.alignment = getattr(WD_TABLE_ALIGNMENT, cfg["table_alignment"].upper())
    # Word autofit can collapse numeric columns until decimals wrap.
    table.autofit = False
    for column, width in zip(table.columns, _column_widths(document, rows, cfg)):
        column.width = width
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right"):
        border = OxmlElement("w:" + edge)
        border.set(qn("w:val"), cfg["table_border"])
        border.set(qn("w:sz"), cfg["table_border_size"])
        borders.append(border)
    table._tbl.tblPr.append(borders)
    for i, values in enumerate(rows):
        row, preceding = table.add_row(), []
        repeated = i > 1 and values[0] and values[0] == rows[i - 1][0]
        if repeated:
            table.cell(i - 1, 0).merge(row.cells[0])
        if i == 0:
            row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for j, cell in enumerate(row.cells):
            value = values[j] if j < len(values) else ""
            if repeated and j == 0:
                value = "".join(re.findall(cfg["citation_pattern"], value, flags=re.I))
            paragraph = cell.paragraphs[0]
            paragraph.style = cfg["table_style"] if cfg["table_style"] in document.styles else cfg["body_style"]
            if j < len(alignment):
                paragraph.alignment = alignment[j]
            _inline(document, paragraph, value, cfg, preceding or tuple(paragraph.runs))
            for run in paragraph.runs:
                run.font.size = Pt(float(cfg["table_size"]))
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


def _fields(text):
    pairs = []
    for line in text.splitlines():
        match = re.match(r"^([a-z_]+):[ \t]*(.*)$", line)
        if match:
            pairs.append([match[1], match[2].replace(r"\n", "\n").replace(r"\u0020", " ")])
        elif pairs and line.startswith((" ", "\t")):
            pairs[-1][1] += "\n" + line.strip()
    return pairs


def _plan(case):
    path = case / "work/plan.md"
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    front = re.match(r"\A---\r?\n(.*?)\r?\n---", text, re.S)
    pairs = _fields(front[1]) if front else []
    metadata = dict(pairs)
    order = [(int(value.partition("|")[0]), value.partition("|")[2].strip()) for key, value in pairs if key == "section"]
    if not order:
        order = [(1, p.stem) for p in sorted((case / "work/sections").glob("*.md"))]
    return metadata, order


def _title(text, cfg):
    return re.sub(cfg["heading_pattern"], "", text).strip() if cfg["heading_pattern"] else text


def _section(document, path, name, root_level, cfg):
    blocks = list(_blocks(path.read_text(encoding="utf-8-sig"), cfg))
    paragraphs, heading, previous = [], None, []
    if not blocks or blocks[0][0] != "heading":
        heading = document.add_heading(_title(name, cfg), root_level)
        paragraphs.append(heading)
    for i, (kind, value) in enumerate(blocks):
        if kind == "table":
            _table(document, *value, cfg)
            paragraphs.extend(p for row in document.tables[-1].rows for cell in row.cells for p in cell.paragraphs)
            continue
        if kind == "heading":
            level, title = value
            paragraph = document.add_heading("", level + root_level - 1)
            previous = _inline(document, paragraph, _title(title, cfg), cfg, previous)
            heading = heading or paragraph
        else:
            item = re.match(cfg["list_pattern"], value)
            caption = i + 1 < len(blocks) and blocks[i + 1][0] == "table" and any(re.match(r"^(?:\*\*)?" + re.escape(prefix) + r"\s*\d", value) for prefix in cfg["caption_prefixes"].split("|") if prefix)
            style = cfg["list_style"] if item else cfg["caption_style"] if caption else cfg["body_style"]
            if item and item.groupdict().get("number"):
                value = cfg["list_format"].format(number=item["number"]) + value[item.end():]
            paragraph = document.add_paragraph(style=style)
            previous = _inline(document, paragraph, value, cfg, previous)
            if caption:
                paragraph.paragraph_format.keep_with_next = True
                threshold = int(cfg["table_new_page_rows"])
                paragraph.paragraph_format.page_break_before = bool(threshold and len(blocks[i + 1][1][0]) - 1 >= threshold)
        paragraphs.append(paragraph)
    return heading, paragraphs


def _anchor(paragraphs, sentence, heading):
    sentence = sentence.strip().removeprefix("「").removesuffix("」")
    for paragraph in paragraphs:
        start = paragraph.text.find(sentence) if sentence else -1
        if start < 0:
            continue
        end = start + len(sentence)
        for boundary in (end, start):
            offset = 0
            for run in paragraph.runs:
                length = len(run.text)
                if offset < boundary < offset + length:
                    cut = boundary - offset
                    tail = paragraph.add_run(run.text[cut:])
                    if run._r.rPr is not None:
                        tail._r.insert(0, deepcopy(run._r.rPr))
                    run.text = run.text[:cut]
                    run._r.addnext(tail._r)
                    break
                offset += length
        anchor, offset = [], 0
        for run in paragraph.runs:
            if offset < end and offset + len(run.text) > start:
                anchor.append(run)
            offset += len(run.text)
        return anchor, sentence
    return [r for r in heading.runs if r.text] or [heading.add_run("\u200b")], heading.text


def _old_answers(target, cfg):
    if not target.exists():
        return {}
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(target) as archive:
        shared = ["".join(e.itertext()) for e in fromstring(archive.read("xl/sharedStrings.xml"))] if "xl/sharedStrings.xml" in archive.namelist() else []
        sheet = fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows = []
        for row in sheet.findall("s:sheetData/s:row", ns):
            values = {}
            for cell in row:
                value = cell.findtext("s:v", "", ns)
                if cell.get("t") == "s":
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(t.text or "" for t in cell.findall(".//s:t", ns))
                values[re.sub(r"\d", "", cell.get("r"))] = value
            rows.append(values)
    labels = dict(zip(cfg["sheet_fields"].split("|"), cfg["sheet_columns"].split("|")))
    columns = {field: next((col for col, label in rows[0].items() if label == labels.get(field)), None) for field in ("number", "status", "answer", "responder")}
    return {int(row[columns["number"]]): [row.get(columns[field], "") for field in ("status", "answer", "responder")] for row in rows[1:] if row.get(columns["number"])}


def _xlsx(target, headers, rows, cfg):
    # Single-sheet SpreadsheetML keeps this data export independent of extra packages.
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    sheet = Element("worksheet", xmlns=ns)
    view = SubElement(SubElement(sheet, "sheetViews"), "sheetView", workbookViewId="0", showGridLines="0")
    SubElement(view, "pane", xSplit="2", ySplit="1", topLeftCell="C2", activePane="bottomRight", state="frozen")
    widths = [float(value) for value in cfg["sheet_widths"].split("|")]
    columns = SubElement(sheet, "cols")
    for i, width in enumerate(widths, 1):
        SubElement(columns, "col", min=str(i), max=str(i), width=str(width), customWidth="1")
    data = SubElement(sheet, "sheetData")
    rows = [headers, *rows]
    for i, values in enumerate(rows, 1):
        lines = max(sum(max(1, ceil(sum(2 if east_asian_width(c) in "WF" else 1 for c in line) / (width - 2))) for line in str(value).split("\n")) for value, width in zip(values, widths))
        row = SubElement(data, "row", r=str(i), ht=str(30 if i == 1 else min(409.5, max(30, (lines + 1) * 15))), customHeight="1")
        for j, value in enumerate(values):
            cell = SubElement(row, "c", r=f"{chr(65+j)}{i}", t="n" if isinstance(value, int) else "inlineStr", s="2" if i == 1 else "3" if j == 0 else "1")
            if isinstance(value, int):
                SubElement(cell, "v").text = str(value)
            else:
                SubElement(SubElement(cell, "is"), "t", {"xml:space": "preserve"}).text = value
    # shortcut: at most 26 columns; use an Excel column converter for wider sheets.
    SubElement(sheet, "autoFilter", ref=f"A1:{chr(64 + len(headers))}{len(rows)}")
    styles = Element("styleSheet", xmlns=ns)
    fonts = SubElement(styles, "fonts", count="2")
    for bold in (False, True):
        font = SubElement(fonts, "font")
        SubElement(font, "sz", val=cfg["sheet_size"]); SubElement(font, "name", val=cfg["sheet_font"])
        if bold:
            SubElement(font, "b")
    fills = SubElement(styles, "fills", count="3")
    for pattern in ("none", "gray125", "solid"):
        fill = SubElement(SubElement(fills, "fill"), "patternFill", patternType=pattern)
        if pattern == "solid":
            SubElement(fill, "fgColor", rgb=cfg["sheet_header_fill"])
    SubElement(SubElement(styles, "borders", count="1"), "border")
    SubElement(SubElement(styles, "cellStyleXfs", count="1"), "xf", numFmtId="0", fontId="0", fillId="0", borderId="0")
    xfs = SubElement(styles, "cellXfs", count="4")
    for i in range(4):
        xf = SubElement(xfs, "xf", numFmtId="1" if i == 3 else "0", fontId="1" if i == 2 else "0", fillId="2" if i == 2 else "0", borderId="0", xfId="0", applyAlignment="1")
        SubElement(xf, "alignment", wrapText="1", vertical="center" if i in (2, 3) else "top", horizontal="center" if i == 2 else "right" if i == 3 else "left")
    SubElement(SubElement(styles, "cellStyles", count="1"), "cellStyle", name="Normal", xfId="0", builtinId="0")
    book = Element("workbook", {"xmlns": ns, "xmlns:r": rel})
    SubElement(SubElement(book, "sheets"), "sheet", {"name": cfg["sheet_name"], "sheetId": "1", "r:id": "rId1"})
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
    temporary = target.with_suffix(".xlsx.tmp")
    with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
        for name, element in [("[Content_Types].xml", types), ("_rels/.rels", root_rel), ("xl/workbook.xml", book), ("xl/_rels/workbook.xml.rels", relationships), ("xl/worksheets/sheet1.xml", sheet), ("xl/styles.xml", styles)]:
            archive.writestr(name, tostring(element, encoding="utf-8", xml_declaration=True))
    temporary.replace(target)


def export_docx(case_dir):
    case = Path(case_dir)
    template = case / "commission/template"
    package = files("reportagent.export")
    cfg = dict(_fields(package.joinpath("settings.txt").read_text(encoding="utf-8")))
    settings = template / "settings.txt"
    if settings.exists():
        cfg.update(_fields(settings.read_text(encoding="utf-8-sig")))
    reference = template / "reference.docx"
    data = reference.read_bytes() if reference.exists() else package.joinpath("reference.docx").read_bytes()
    document = Document(BytesIO(data))
    metadata, order = _plan(case)
    if metadata:
        if metadata.get("project"):
            document.add_paragraph(metadata["project"], "Subtitle")
        if metadata.get("title"):
            document.add_paragraph(metadata["title"], "Title")
        if cfg["toc_title"]:
            document.add_paragraph(cfg["toc_title"], cfg["toc_style"])
            _field(document.add_paragraph(), 'TOC \\o "' + cfg["toc_levels"] + '" \\h \\z \\u')
            document.add_page_break()
    for section in document.sections:
        for part in (section.header, section.footer):
            for node in part._element.iter(qn("w:t")):
                for key in cfg["header_fields"].split("|"):
                    node.text = (node.text or "").replace("{{" + key + "}}", metadata.get(key, ""))
        if cfg["footer_field"] and not any(cfg["footer_field"] in (n.text or "") for n in section.footer._element.iter(qn("w:instrText"))):
            _field(section.footer.paragraphs[0], cfg["footer_field"])
    output = case / "out"
    xlsx = output / cfg["xlsx_file"]
    answers, records = _old_answers(xlsx, cfg), []
    replies = case / cfg["answers_file"] if cfg["answers_file"] else None
    if replies and replies.exists():
        for number, answer in re.findall(r"(?m)^(\d+):[ \t]*(.+)$", replies.read_text(encoding="utf-8-sig")):
            status = answers.get(int(number), [cfg["item_status"]])[0]
            answers[int(number)] = [status, answer, cfg["answer_author"]]
    for i, (level, name) in enumerate(order):
        path = case / "work/sections" / (name + ".md")
        if path.exists():
            heading, paragraphs = _section(document, path, name, level, cfg)
        else:
            heading = document.add_heading(_title(name, cfg), level)
            paragraphs = [heading]
            if i + 1 == len(order) or order[i + 1][0] <= level:
                document.add_paragraph(cfg["placeholder"], cfg["body_style"])
        items = case / "work/clarifications" / (name + ".md")
        if not items.exists():
            continue
        for block in re.split(r"\n[ \t]*---[ \t]*\n", items.read_text(encoding="utf-8-sig")):
            item = dict(_fields(block))
            if not item:
                continue
            number = int(item["number"])
            anchor, position = _anchor(paragraphs, item["anchor"], heading)
            text = cfg["item_prefix"] + str(number) + "\n" + item["content"]
            if item.get("sources"):
                text += "\n" + cfg["citation_prefix"] + item["sources"]
            document.add_comment(anchor, text, author=cfg["comment_author"])
            status, answer, responder = answers.get(number, [cfg["item_status"], "", ""])
            status = item.get("status", status)
            records.append(dict(item, number=number, section=heading.text, position=position, status=status, answer=answer, responder=responder))
    output.mkdir(parents=True, exist_ok=True)
    target = output / cfg["docx_file"]
    document.save(target)
    if cfg["sheet_sort"]:
        records.sort(key=lambda item: float(item.get(cfg["sheet_sort"]) or cfg["sheet_sort_default"]))
    rows = [[item.get(field, "") for field in cfg["sheet_fields"].split("|")] for item in records]
    _xlsx(xlsx, cfg["sheet_columns"].split("|"), rows, cfg)
    return target
