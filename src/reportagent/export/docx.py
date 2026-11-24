from collections import Counter, defaultdict
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
import unicodedata
from decimal import Decimal

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import nsmap, qn
from docx.shared import Pt
from docx.opc.part import Part
from docx.text.paragraph import Paragraph


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
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
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


def _contents(document, page_break, levels):
    entry = page_break.insert_paragraph_before()
    _field(entry, f'TOC \\o "{levels}" \\h \\z \\u')
    bounds = re.fullmatch(r"([1-9])(?:-([1-9]))?", levels.strip())
    if not bounds:
        return
    # Keep the visible entries inside one refreshable Word field.
    end = entry.runs[-1]._r
    for p in document.paragraphs:
        heading = re.fullmatch(r"Heading ([1-9])", p.style.name)
        if not heading or not bounds[1] <= heading[1] <= (bounds[2] or bounds[1]) or not p.text:
            continue
        if entry.text:
            entry = page_break.insert_paragraph_before()
        style = "TOC " + heading[1]
        if style in document.styles:
            entry.style = style
        else:
            entry.paragraph_format.left_indent = Pt(12 * (int(heading[1]) - 1))
            entry.paragraph_format.first_line_indent = Pt(0)
        entry.add_run(p.text)
    entry._p.append(end)


def _fields(text):
    pairs = []
    for line in text.splitlines():
        match = re.match(r"^([a-z_]+):[ \t]*(.*)$", line)
        if match:
            pairs.append([match[1], match[2].replace(r"\n", "\n").replace(r"\u0020", " ")])
        elif pairs and line.startswith((" ", "\t")):
            pairs[-1][1] += "\n" + line.strip()
    return pairs


def _plan(case, *, use_headings=False):
    path = case / "work/plan.md"
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    front = re.match(r"\A---\r?\n(.*?)\r?\n---", text, re.S)
    pairs = _fields(front[1]) if front else []
    metadata = dict(pairs)
    order = [(int(value.partition("|")[0]), value.partition("|")[2].strip()) for key, value in pairs if key == "section"]
    if not order and use_headings:
        order = [(1, h.strip()) for h in re.findall(r"^##[ \t]+(.+)$", text, re.M)]
    elif not order:
        order = [(1, p.stem) for p in sorted((case / "work/sections").glob("*.md")) if not p.name.endswith(".table.md")]
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


def _sheet_rows(target, cfg, sheet_path="xl/worksheets/sheet1.xml"):
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(target) as archive:
        shared = ["".join(e.itertext()) for e in fromstring(archive.read("xl/sharedStrings.xml"))] if "xl/sharedStrings.xml" in archive.namelist() else []
        sheet = fromstring(archive.read(sheet_path))
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
    fields = dict(zip(cfg["sheet_columns"].split("|"), cfg["sheet_fields"].split("|")))
    columns = {}
    for col, label in (rows[0] if rows else {}).items():
        if label in fields:
            columns.setdefault(fields[label], col)
    return [{field: row.get(col, "") for field, col in columns.items()} for row in rows[1:]]


def answered_client_rows(target, cfg):
    if target.is_file():
        with ZipFile(target) as book:
            sheets = [name for name in book.namelist() if re.fullmatch(r"xl/worksheets/[^/]+\.xml", name)]
        for sheet in sheets:
            yield from (row for row in _sheet_rows(target, cfg, sheet) if row.get("answer", "").strip())


def _missing_client_answers(target, cfg, selected):
    previous = Counter(row.get("number", "").strip() for row in answered_client_rows(target, cfg))
    proposed = Counter(str(item["number"]) for item in selected if item.get("answer", "").strip())
    return sum((previous - proposed).values())


def _old_answers(target, cfg, sections=None):
    if not target.exists():
        return {}
    numbered = [(int(row["number"]), row) for row in _sheet_rows(target, cfg) if row.get("number")]
    counts = Counter(number for number, _ in numbered)
    valid = [(n, row) for n, row in numbered if counts[n] == 1 and row.get("status") != cfg["item_conflict_status"]]
    if sections is not None:
        sections.update((n, row.get("section", "")) for n, row in valid)
    return {n: [row.get(field, "") for field in ("status", "answer", "responder")] for n, row in valid}


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


def _settings(case):
    cfg = dict(_fields(files("reportagent.export").joinpath("settings.txt").read_text(encoding="utf-8")))
    settings = case / "commission/template/settings.txt"
    if settings.exists():
        cfg.update(_fields(settings.read_text(encoding="utf-8-sig")))
    return cfg


def clarification_numbers(case, cfg=None):
    """A question and its answer share a number; repetition within either role conflicts."""
    if cfg is None:
        cfg = _settings(case)
    case = case.resolve()
    definitions = sorted((case / "work/clarifications").rglob("*"))
    answer_files = {case / "commission/澄清答复.md"}
    if cfg["answers_file"]:
        answer_files.add((case / cfg["answers_file"]).resolve())
    locations = defaultdict(list)
    conflicts = set()
    for paths, pattern in ((definitions, r"^number:[ \t]*(\d+)[ \t]*$"),
                           (sorted(answer_files), r"^(\d+):")):
        seen = set()
        for path in paths:
            if not path.is_file():
                continue
            label = "/" + path.relative_to(case).as_posix() if path.is_relative_to(case) else str(path)
            for line_no, line in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
                match = re.match(pattern, line)
                if match:
                    number = int(match[1])
                    locations[number].append(f"{label}:{line_no}")
                    if number in seen:
                        conflicts.add(number)
                    seen.add(number)
    return max(locations, default=0) + 1, {n: locations[n] for n in sorted(conflicts)}


_SCORE = re.compile(
    r"(?P<baseline>(?:算术|现|当前)?基准值|(?:算术|现|当前)基准|baseline(?: score)?)\s*[:=为]?\s*(?:F\s*[:=为]\s*)?"
    r"|(?<![A-Za-z0-9])F\s*(?:值\s*)?(?:[:=为]|is\b)\s*", re.I)
_VALUE = re.compile(r"-?\d+(?:\.\d+)?(?![\d.]|\s*[+*/×÷~～−-])")
_NUMBER = r"-?\d+(?:\.\d+)?"
_CHAIN = re.compile(rf"{_NUMBER}(?:\s*[+*/×÷−-]\s*{_NUMBER})+\s*=\s*")
_SCENARIO = (r"若|假设|区间核算|同取区间|各指标同取|计入未定项区间|(?:组合|叠加)情形"
             r"|组合时|(?:^|(?<=[。;；:：,，]))\s*(?:敏感性(?:提示|校核|核算|分析|情形)?(?:\s*[:：]|下|时|\s+)"
             r"|(?:单独)?改按|与[^,，。;；:：()\n]*?组合)"
             r"|取[^,，。;；:：()\n]*?时|时\s*[,，]\s*F")
_GRADE = r"(?P<grade>IV|III|II|I|[1-4])(?:\s*级|\b)"
_GRADE_AFTER = re.compile(r"\s*(?:[>,≥≤<]\s*\d+(?:\.\d+)?)?[\s,，、:(]*(?:对应|为|属(?:于)?|grade\s*)?\s*" + _GRADE, re.I)
_GRADE_ONLY = re.compile(r"(?:风险等级|基准等级|risk grade|grade)\s*[:=为]?\s*" + _GRADE, re.I)


def _written_grade(match):
    grade = match["grade"].upper()
    return {"1": "I", "2": "II", "3": "III", "4": "IV"}.get(grade, grade)


def _risk_statements(line):
    line = unicodedata.normalize("NFKC", line)
    covered = set()

    def baseline(explicit, offset):
        prefix = re.split(r"[。;；]", line[:offset])[-1]
        if re.search(_SCENARIO, prefix):
            return False
        return True if explicit else None  # Unlabelled scores can supply a baseline if none is named.

    for label in _SCORE.finditer(line):
        chain = _CHAIN.match(line, label.end())
        value = _VALUE.match(line, chain.end() if chain else label.end())
        if not value:
            continue
        grade = _GRADE_AFTER.match(line, value.end())
        if grade:
            covered.add(grade.start("grade"))
        yield baseline(label["baseline"], label.start()), Decimal(value[0]), _written_grade(grade) if grade else None
    for grade in _GRADE_ONLY.finditer(line):
        if grade.start("grade") not in covered:
            yield baseline("基准" in grade[0], grade.start()), None, _written_grade(grade)


def _export_scores(text):
    yield from _risk_statements(text)
    # Match explicitly labelled intervals as a pair, independently of the individual scenarios.
    text = unicodedata.normalize("NFKC", text)
    for m in re.finditer(r"(?<![A-Za-z0-9])F\s*(?:值|范围|区间)?\s*[:=为]?\s*(\d+(?:\.\d+)?)\s*[~至–—-]\s*(\d+(?:\.\d+)?)", text, re.I):
        yield False, (Decimal(m[1]), Decimal(m[2])), None


def _material_pattern(case):
    source_names = {p.name for p in (case / "sources").iterdir() if p.is_file()} if (case / "sources").is_dir() else set()
    names = {p.name for p in (case / "work").rglob("*") if p.is_file()} - source_names
    # A replay can contain copies of the original sources. Their public filenames remain valid citations.
    names.update(p.name for p in (case / "commission").rglob("*.md") if p.is_file() and p.name not in source_names)
    options = sorted(map(re.escape, names), key=len, reverse=True)
    terms = r"(?<![A-Za-z0-9_.-])(?:[/\\]?(?:work|commission|sources)[/\\])"
    if options:
        terms += r"|(?<![A-Za-z0-9_.-])(?:" + "|".join(options) + r")(?![A-Za-z0-9_-]|\.[A-Za-z0-9_-])"
    return re.compile(terms)


def section_flags(case, name, pattern=None):
    pattern = pattern or _material_pattern(case)
    for folder in ("sections", "clarifications"):
        path = case / "work" / folder / (name + ".md")
        if not path.is_file():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
            for mention in dict.fromkeys(pattern.findall(line)):
                yield f"{folder}/{name}.md:{i} | {mention}"


def _export_flags(pattern, texts):
    for location, text in texts:
        for mention in dict.fromkeys(pattern.findall(text)):
            yield f"{location} | {mention}"


def sheet_section_title(path, cfg):
    first = next(_blocks(path.read_text(encoding="utf-8-sig"), cfg), None) if path.is_file() else None
    if not first or first[0] != "heading":
        return _title(path.stem, cfg)
    # The workbook stores the rendered Word heading, including any Markdown formatting removal.
    doc = Document()
    heading = doc.add_paragraph()
    _inline(doc, heading, _title(first[1][1], cfg), cfg)
    return heading.text


def export_answers(case, cfg, sections=None):
    xlsx = case / "out" / cfg["xlsx_file"]
    registers = case / "work/question-registers"
    destinations = {"client": xlsx, "method": registers / "chapter-03-method.xlsx",
                    "internal": registers / "internal.xlsx", "unclassified": registers / "unclassified.xlsx"}
    answers = {}
    # Keep one recoverable answer snapshot if a workbook is locked midway through audience changes.
    snapshot = registers / "answers.xlsx"
    for target in (snapshot, destinations["method"], destinations["internal"], destinations["unclassified"], xlsx):
        answers.update(_old_answers(target, cfg, sections))
    replies = case / cfg["answers_file"] if cfg["answers_file"] else None
    if replies and replies.exists():
        for number, answer in re.findall(r"(?m)^(\d+):[ \t]*(.+)$", replies.read_text(encoding="utf-8-sig")):
            status = answers.get(int(number), [cfg["item_status"]])[0]
            answers[int(number)] = [status, answer, cfg["answer_author"]]
    return destinations, snapshot, answers


FIXED_FIELDS = ("【项目名】", "【评估对象有几处，填在这里】")
_FIXED_HEADINGS = ("前言", "概述", "评估流程和方法", "风险控制措施建议", "评估结果自我评价及遗留问题说明")


def fixed_values(path):
    if not path.is_file():
        return {}
    return {old: new for line in path.read_text(encoding="utf-8-sig").splitlines()
            for old, sep, new in [line.partition("=>")] if sep and old}


def _replace_fixed_text(paragraph, values):
    nodes = list(paragraph.iter(qn("w:t")))
    for old in sorted(values, key=len, reverse=True):
        text = "".join(n.text or "" for n in nodes)
        for match in reversed(list(re.finditer(re.escape(old), text))):
            start = 0
            for n in nodes:
                value = n.text or ""
                end = start + len(value)
                if start < match.end() and end > match.start():
                    left = max(0, match.start() - start)
                    replacement = values[old] if start <= match.start() else ""
                    n.text = value[:left] + replacement + value[max(0, match.end() - start):]
                    if n.text and n.text != n.text.strip():
                        n.set(qn("xml:space"), "preserve")
                start = end
    return "".join(n.text or "" for n in nodes)


def _fixed_sections(case, document, flags):
    path = case / "commission/fixed-text.docx"
    if not path.is_file():
        return {}
    source = Document(path)
    values = fixed_values(path.with_name("fixed-text-values.txt"))
    groups = defaultdict(list)
    parts, styles, numbers, abstracts = {}, {}, {}, {}
    package = document.part.package

    def copy_part(part):
        if part in parts:
            return parts[part]
        name = part.partname
        if any(p.partname == name for p in package.iter_parts()):
            stem, dot, suffix = str(name).rpartition(".")
            name = package.next_partname(stem + "-fixed%d" + dot + suffix)
        cloned = parts[part] = Part(name, part.content_type, part.blob, package)
        for rel in part.rels.values():
            child = rel.target_ref if rel.is_external else copy_part(rel.target_part)
            cloned.load_rel(rel.reltype, child, rel.rId, rel.is_external)
        return cloned

    def copy_style(style_id):
        if style_id in styles:
            return styles[style_id]
        style = source.styles.element.get_by_id(style_id)
        if style is None:
            return style_id
        same = document.styles.element.get_by_name(style.name_val)
        if same is not None:
            styles[style_id] = same.styleId
        else:
            cloned = deepcopy(style)
            cloned.styleId = "Fixed_" + style_id
            while document.styles.element.get_by_id(cloned.styleId) is not None:
                cloned.styleId += "_"
            styles[style_id] = cloned.styleId
            document.styles.element.append(cloned)
            for ref in cloned.xpath("w:basedOn|w:next|w:link"):
                ref.set(qn("w:val"), copy_style(ref.get(qn("w:val"))))
            for num in cloned.xpath("w:pPr/w:numPr/w:numId"):
                num.val = copy_num(num.val)
        return styles[style_id]

    def copy_num(number):
        if not number or number in numbers:
            return numbers.get(number, number)
        src = source.part.numbering_part.element
        dst = document.part.numbering_part.element
        num = src.num_having_numId(number)
        aid = num.abstractNumId.val
        if aid not in abstracts:
            abstract = deepcopy(src.xpath(f'w:abstractNum[@w:abstractNumId="{aid}"]')[0])
            new_id = 1 + max((int(x.get(qn("w:abstractNumId"))) for x in dst.findall(qn("w:abstractNum"))), default=-1)
            abstract.set(qn("w:abstractNumId"), str(new_id))
            # Keep copied list definitions separate from the report's heading numbering.
            for ref in list(abstract.iter(qn("w:pStyle"))):
                ref.getparent().remove(ref)
            dst.insert(0, abstract)
            abstracts[aid] = new_id
        cloned = deepcopy(num)
        cloned.numId = dst._next_numId
        cloned.abstractNumId.val = abstracts[aid]
        dst.append(cloned)
        numbers[number] = cloned.numId
        return cloned.numId

    current = None
    for block in source.element.body:
        if block.tag not in (qn("w:p"), qn("w:tbl")):
            continue
        heading = Paragraph(block, source) if block.tag == qn("w:p") else None
        if heading is not None:
            title = re.sub(r"^[\d.、．\s]+", "", re.sub(r"\s+", "", heading.text))
            if title == "前言" or (title in _FIXED_HEADINGS and heading.style.name.startswith("Heading ")):
                current = title
            elif title and heading.style.name == "Heading 1":
                current = None
        if not current:
            continue
        cloned = deepcopy(block)
        for node in cloned.iter():
            for attr, rid in list(node.attrib.items()):
                if attr.startswith("{" + nsmap["r"] + "}"):
                    rel = source.part.rels[rid]
                    other = rel.target_ref if rel.is_external else copy_part(rel.target_part)
                    node.set(attr, document.part.relate_to(other, rel.reltype, rel.is_external))
            if node.tag in (qn("w:pStyle"), qn("w:rStyle"), qn("w:tblStyle")):
                node.set(qn("w:val"), copy_style(node.get(qn("w:val"))))
        for para in cloned.iter(qn("w:p")):
            text = _replace_fixed_text(para, values)
            flags.extend("固定文本保留占位符 " + token for token in dict.fromkeys(re.findall(r"【[^】]+】", text)))
            p = Paragraph(para, document)
            numbered = p.style.name.startswith("Heading ")
            prop = para.pPr
            num = prop.numPr if prop is not None else None
            if numbered and not text.strip():
                p.style = document.styles["Normal"]
                para.get_or_add_pPr().get_or_add_numPr().get_or_add_numId().val = 0
            elif numbered and re.match(r"^\s*\d+[.、．\d]*", text):
                para.get_or_add_pPr().get_or_add_numPr().get_or_add_numId().val = 0
            elif numbered and current != _FIXED_HEADINGS[-1]:
                if num is not None:
                    prop.remove(num)
            elif num is not None and num.numId is not None:
                num.numId.val = copy_num(num.numId.val)
        groups[current].append(cloned)
    for title in _FIXED_HEADINGS:
        if title not in groups:
            print(f"固定文本未找到“{title}”，该部分使用现有导出内容。")
    return groups


def _append_fixed(document, blocks):
    paragraphs = []
    for block in blocks:
        document.element.body.insert_element_before(block, "w:sectPr")
        paragraphs.extend(Paragraph(p, document) for p in block.iter(qn("w:p")))
    return paragraphs


def export_docx(case_dir):
    case = Path(case_dir)
    template = case / "commission/template"
    package = files("reportagent.export")
    cfg = _settings(case)
    _, conflicts = clarification_numbers(case, cfg)
    reference = template / "reference.docx"
    data = reference.read_bytes() if reference.exists() else package.joinpath("reference.docx").read_bytes()
    document = Document(BytesIO(data))
    metadata, order = _plan(case)
    toc_break = None
    if metadata:
        if metadata.get("project"):
            document.add_paragraph(metadata["project"], "Subtitle")
        if metadata.get("title"):
            document.add_paragraph(metadata["title"], "Title")
        if cfg["toc_title"]:
            document.add_paragraph(cfg["toc_title"], cfg["toc_style"])
            toc_break = document.add_page_break()
    for section in document.sections:
        for part in (section.header, section.footer):
            for node in part._element.iter(qn("w:t")):
                for key in cfg["header_fields"].split("|"):
                    node.text = (node.text or "").replace("{{" + key + "}}", metadata.get(key, ""))
        if cfg["footer_field"] and not any(cfg["footer_field"] in (n.text or "") for n in section.footer._element.iter(qn("w:instrText"))):
            _field(section.footer.paragraphs[0], cfg["footer_field"])
    output = case / "out"
    answer_sections = {}
    destinations, snapshot, answers = export_answers(case, cfg, answer_sections)
    xlsx = destinations["client"]
    registers = snapshot.parent
    records, scores, flags = [], {}, []
    pattern = _material_pattern(case)
    fixed = _fixed_sections(case, document, flags)
    _append_fixed(document, fixed.get("前言", []))
    in_conclusion = False
    fixed_paragraphs = []
    for i, (level, name) in enumerate(order):
        path = case / "work/sections" / (name + ".md")
        if fixed and level == 1:
            if in_conclusion:
                _append_fixed(document, fixed.get("风险控制措施建议", []))
            title = re.sub(r"^[\d.、．\s-]+", "", _title(name, cfg))
            in_conclusion = title == "评估结论及建议"
            fixed_paragraphs = (_append_fixed(document, fixed.get(title, []))
                                if title in ("概述", "评估流程和方法", "评估结果自我评价及遗留问题说明") else [])
        if fixed_paragraphs:
            heading, paragraphs = fixed_paragraphs[0], fixed_paragraphs
        elif path.exists():
            heading, paragraphs = _section(document, path, name, level, cfg)
        else:
            heading = document.add_heading(_title(name, cfg), level)
            paragraphs = [heading]
            if i + 1 == len(order) or order[i + 1][0] <= level:
                document.add_paragraph(cfg["placeholder"], cfg["body_style"])
        scores[name] = [entry for para in paragraphs for entry in _export_scores(para.text)]
        if path.is_file() and not fixed_paragraphs:
            flags.extend(_export_flags(pattern, ((f"Section {name}:{n}", line) for n, line in
                                                  enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1))))
        items = case / "work/clarifications" / (name + ".md")
        if not items.exists():
            continue
        for block in re.split(r"\n[ \t]*---[ \t]*\n", items.read_text(encoding="utf-8-sig")):
            item = dict(_fields(block))
            if not item:
                continue
            number = int(item["number"])
            audience = item.get("audience")
            if audience not in ("client", "method", "internal"):
                audience = "unclassified"
                flags.append(f"{name} | question {number} | audience unclassified")
            position = item.get("anchor", "")
            if audience == "client":
                anchor, position = _anchor(paragraphs, item["anchor"], heading)
                text = cfg["item_prefix"] + str(number) + "\n" + item["content"]
                if item.get("sources"):
                    text += "\n" + cfg["citation_prefix"] + item["sources"]
                document.add_comment(anchor, text, author=cfg["comment_author"])
            status, answer, responder = answers.get(number, [cfg["item_status"], "", ""])
            status = item.get("status", status)
            if number in conflicts:
                status, answer, responder = cfg["item_conflict_status"], "", ""
            records.append(dict(item, number=number, audience=audience, section_id=name, section=heading.text, position=position, status=status, answer=answer, responder=responder))
    if in_conclusion:
        _append_fixed(document, fixed.get("风险控制措施建议", []))
    output.mkdir(parents=True, exist_ok=True)
    target = output / cfg["docx_file"]
    if toc_break is not None:
        _contents(document, toc_break, cfg["toc_levels"])
    document.save(target)
    if cfg["sheet_sort"]:
        records.sort(key=lambda item: float(item.get(cfg["sheet_sort"]) or cfg["sheet_sort_default"]))
    fields = cfg["sheet_fields"].split("|")
    if any(answer or responder or status != cfg["item_status"] for status, answer, responder in answers.values()):
        registers.mkdir(parents=True, exist_ok=True)
        saved = [dict(number=n, section=answer_sections.get(n, ""), status=status, answer=answer, responder=responder)
                 for n, (status, answer, responder) in answers.items()]
        _xlsx(snapshot, cfg["sheet_columns"].split("|"), [[item.get(key, "") for key in fields] for item in saved], cfg)
    for audience, destination in destinations.items():
        selected = [item for item in records if item["audience"] == audience]
        if not selected and audience != "client" and not destination.exists():
            continue
        if audience == "client" and (missing := _missing_client_answers(destination, cfg, selected)):
            flags.append(f"Client workbook preserved: proposed export would omit {missing} existing answer(s).")
            continue
        rows = [[item.get(field, "") for field in fields] for item in selected]
        destination.parent.mkdir(parents=True, exist_ok=True)
        _xlsx(destination, cfg["sheet_columns"].split("|"), rows, cfg)
        if audience == "client":
            for row, (values, item) in enumerate(zip(rows, selected), 2):
                for col, text in enumerate(values):
                    cell = f"Excel {xlsx.name}!{chr(65 + col)}{row} (Q{item['number']})"
                    flags.extend(_export_flags(pattern, [(cell, str(text))]))
                    if fields[col] in ("answer", "responder", "sources", "status"):
                        continue
                    for baseline, score, grade in _export_scores(str(text)):
                        candidates = scores[item["section_id"]]
                        if baseline is True:
                            explicit = [entry for entry in candidates if entry[0] is True]
                            candidates = explicit or [entry for entry in candidates if entry[0] is None]
                        if not any((score is None or score == s) and (grade is None or grade == g)
                                   for _, s, g in candidates):
                            flags.append(f"{cell} | F/grade {score}/{grade} has no matching value in Word section {item['section_id']}")
    with ZipFile(target) as doc:
        for part in doc.namelist():
            if re.fullmatch(r"word/(?:document|comments|header\d+|footer\d+|footnotes|endnotes)\.xml", part):
                paras = fromstring(doc.read(part)).iter(qn("w:p"))
                flags.extend(_export_flags(pattern, ((f"Word {part} paragraph {n}", "".join(t.text or "" for t in para.iter(qn("w:t"))))
                                                      for n, para in enumerate(paras, 1))))
    status = "NOT CLIENT-READY" if flags else "No outstanding mechanical client-readiness flags"
    (output / "client-readiness.txt").write_text("\n".join([status, *flags]) + "\n", encoding="utf-8")
    if flags:
        print(f"{status} | {output / 'client-readiness.txt'}")
    return target
