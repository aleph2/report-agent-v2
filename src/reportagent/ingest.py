from collections import defaultdict
from contextlib import closing
from io import BytesIO
from itertools import combinations
import math
from multiprocessing import Pipe, Process
from pathlib import Path
import re
from statistics import mean

from docx import Document
from docx.drawing import Drawing
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.run import Run
from PIL import Image
import pdfplumber
from pdfminer.pdfdevice import PDFDevice
from pdfminer.pdfexceptions import PDFException
from pdfminer.pdfinterp import PDFPageInterpreter
from pdfminer.pdftypes import resolve1
from pdfminer.psexceptions import PSException
from pdfminer.psparser import PSLiteral
from pdfminer.utils import apply_matrix_pt, decode_text
from pdfplumber.utils.exceptions import PdfminerException
import pypdfium2 as pdfium


PDF_TEXT_TIMEOUT = 5


def _pdf_text(source, connection):
    with pdfplumber.open(source) as pdf:
        for number in iter(connection.recv, None):
            try:
                with closing(pdf.pages[number]) as page:
                    text = page.extract_text() or ""
            except Exception as error:
                detail = " ".join(str(error).splitlines())
                text = f"Text extraction failed: {detail}. See page-{number + 1:03d}.png."
            connection.send(text)


def _ingest_pdf(source, target):
    worker = None
    try:
        with pdfium.PdfDocument(source) as pdf:
            count = len(pdf)
            for number in range(count):
                if worker is None:
                    connection, child = Pipe()
                    worker = Process(target=_pdf_text, args=(source, child))
                    worker.start()
                    child.close()
                connection.send(number)
                if connection.poll(PDF_TEXT_TIMEOUT):
                    text = connection.recv()
                else:
                    text = f"Text extraction timed out. See page-{number + 1:03d}.png."
                    worker.terminate()
                    worker.join()
                    connection.close()
                    worker = None
                path = target / f"page-{number + 1:03d}.txt"
                path.write_text(text, encoding="utf-8")
                with closing(pdf[number]) as page:
                    with closing(page.render(scale=1600 / max(page.get_size()))) as bitmap:
                        with bitmap.to_pil() as image:
                            image.save(path.with_suffix(".png"))
        return count
    finally:
        if worker is not None:
            worker.terminate()
            worker.join()
            connection.close()


def _docx_text(parent, target, images):
    lines = []
    for block in parent.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                lines.append(" | ".join(_docx_text(cell, target, images).strip() for cell in row.cells))
            continue
        level = block._p.xpath("./w:pPr/w:outlineLvl")
        style = block.style
        while not level and style is not None:
            level = style.element.xpath("./w:pPr/w:outlineLvl")
            style = style.base_style
        heading = int(level[0].get(qn("w:val"))) + 1 if level else 0
        parts = []
        for item in block.iter_inner_content():
            for run in ([item] if isinstance(item, Run) else item.runs):
                for content in run.iter_inner_content():
                    if isinstance(content, str):
                        parts.append(content)
                    elif isinstance(content, Drawing) and content.has_picture:
                        name = f"image-{len(images) + 1:02d}.png"
                        with Image.open(BytesIO(content.image.blob)) as image:
                            with image.convert("RGBA") as png:
                                png.save(target / name)
                        images.append(name)
                        parts.append(f"\n[{name}]\n")
        lines.append(("#" * heading + " " if 0 < heading <= 9 else "") + "".join(parts))
    return "\n".join(lines) + "\n"


def ingest(case_dir):
    case_dir = Path(case_dir)
    index = ["# Case files\n"]
    for folder in ("commission", "sources", "reference"):
        root = case_dir / folder
        root.mkdir(exist_ok=True)
        for source in sorted(p for p in root.iterdir() if p.is_file()):
            target = root / (source.stem if source.suffix.lower() == ".pdf" else source.name + ".files")
            target.mkdir(exist_ok=True)
            relative = target.relative_to(case_dir).as_posix()
            details = f"Text: {relative}/text.md\n"
            try:
                if source.suffix.lower() == ".pdf":
                    count = _ingest_pdf(source, target)
                    details = (f"Pages: {count}\n\n"
                               f"Page paths: {relative}/page-001.txt ... page-{count:03d}.txt\n"
                               f"Page images: {relative}/page-001.png ... page-{count:03d}.png\n")
                elif source.suffix.lower() == ".md":
                    (target / "text.md").write_bytes(source.read_bytes())
                elif source.suffix.lower() == ".docx":
                    images = []
                    text = _docx_text(Document(source), target, images)
                    (target / "text.md").write_text(text, encoding="utf-8")
                    if images:
                        details += f"Images: {relative}/image-01.png ... {images[-1]}\n"
                else:
                    (target / "text.md").write_text(f"Could not read: unsupported file type {source.suffix}.\n", encoding="utf-8")
            except Exception as error:
                detail = " ".join(str(error).splitlines()) or type(error).__name__
                (target / "text.md").write_text(f"Could not read: {detail}\n", encoding="utf-8")
                details = f"Text: {relative}/text.md\n"
            index.append(f"## {source.relative_to(case_dir).as_posix()}\n\n{details}")
    target = case_dir / "index.md"
    target.write_text("\n".join(index) + "\n", encoding="utf-8")
    return target


class _ProfileInterpreter(PDFPageInterpreter):
    def do_Do(self, name):
        # After a nested form the shared device must use the parent transform again.
        super().do_Do(name)
        self.device.set_ctm(self.ctm)

    def do_BDC(self, tag, props):
        # CAD exports usually name the layer through an indirect page resource.
        if isinstance(props, PSLiteral):
            properties = resolve1(self.resources.get("Properties", {}))
            props = resolve1(properties.get(props.name, {}))
        super().do_BDC(tag, props)


class _ProfileGeometry(PDFDevice):
    def __init__(self, manager):
        super().__init__(manager)
        self.layers = []
        self.paths = defaultdict(list)

    def begin_tag(self, tag, props=None):
        name = resolve1(props.get("Name")) if isinstance(props, dict) else None
        inherited = self.layers[-1] if self.layers else ""
        self.layers.append(decode_text(name) if isinstance(name, bytes) else inherited)

    def end_tag(self):
        if self.layers:
            self.layers.pop()

    def paint_path(self, graphicstate, stroke, fill, evenodd, path):
        segments = []
        first = previous = None
        for operator, *coords in path:
            points = [apply_matrix_pt(self.ctm, p) for p in zip(coords[::2], coords[1::2])]
            if operator == "m":
                first = previous = points[0]
            elif previous is not None:
                if operator == "h":
                    points = [first]
                segments.append((operator, [previous, *points]))
                previous = points[-1]
        if segments:
            self.paths[self.layers[-1] if self.layers else ""].append(segments)


def _profile_fit(groups, minimum):
    # Find the most mutually consistent labelled ticks, then refit using all of them.
    best = 0
    fits = {}
    for group in groups:
        pairs = sorted(set(group))
        for (first, start), (last, end) in combinations(pairs, 2):
            if last - first < 2 or end <= start:
                continue
            scale = (end - start) / (last - first)
            ticks = tuple((x, v) for x, v in pairs if abs((v-start)/scale + first-x) <= 0.7)
            if len(ticks) < max(best, minimum) or ticks in fits:
                continue
            xm, vm = map(mean, zip(*ticks))
            slope = sum((x-xm)*(v-vm) for x, v in ticks) / sum((x-xm)**2 for x, _ in ticks)
            intercept = vm - slope*xm
            if len(ticks) > best:
                best, fits = len(ticks), {}
            fits[ticks] = dict(metres_per_point=slope, intercept_m=intercept,
                               max_residual_m=max(abs(v-slope*x-intercept) for x, v in ticks),
                               ticks=[dict(point=round(x, 4), metres=v) for x, v in ticks])
    if not fits:
        return None
    fit = min(fits.values(), key=lambda f: f["max_residual_m"])
    # Equally well-supported but different axes cannot establish one datum.
    x = fit["ticks"][0]["point"]
    for other in fits.values():
        difference = other["metres_per_point"] - fit["metres_per_point"]
        if (abs(difference) > 0.01 * fit["metres_per_point"] or
                abs(difference*x + other["intercept_m"] - fit["intercept_m"]) > fit["metres_per_point"]):
            return None
    return fit


def _profile_axes(sheet, paths):
    short_h, short_v, dividers = [], [], []
    for records in paths.values():
        for segments in records:
            for operator, points in segments:
                if operator != "l":
                    continue
                (x, y), (a, b) = points
                if abs(y-b) < .15 and 1 <= abs(x-a) <= 14:
                    short_h.append((min(x, a), max(x, a), y))
                if abs(x-a) < .15 and abs(y-b) >= 1:
                    (short_v if abs(y-b) <= 14 else dividers).append((min(y, b), max(y, b), x))
    xs, ys, offsets = defaultdict(list), defaultdict(list), []
    for word in sheet.extract_words():
        text = word["text"]
        x0, x1 = word["x0"], word["x1"]
        y0, y1 = sheet.height-word["bottom"], sheet.height-word["top"]
        x, y = (x0+x1)/2, (y0+y1)/2
        direction = word["direction"]
        offset = re.fullmatch(r"(左|右)(\d+(?:\.\d+)?)m?", text[::-1] if direction == "ttb" else text, re.I)
        if offset:
            # A rotated borehole offset labels its adjacent vertical divider.
            nearby = [v for a, b, v in dividers if abs(v-x0) < 2 and a < y < b]
            if nearby:
                value = float(offset[2]) * (-1 if offset[1] == "左" else 1)
                offsets.append((min(nearby, key=lambda v: abs(v-x0)), value))
        if direction != "ltr" or not re.fullmatch(r"[-−]?\d+(?:\.\d+)?", text):
            continue
        value = float(text.replace("−", "-"))
        for a, b, v in short_h:
            if abs(v-y) <= 2.5 and (0 <= a-x1 <= 12 or 0 <= x0-b <= 12):
                ys[round((a+b)/24)].append((v, value))
        for a, b, v in short_v:
            if abs(v-x) <= 4 and (0 <= a-y1 <= 14 or 0 <= y0-b <= 14):
                xs[round((a+b)/24)].append((v, value))
    xfit = _profile_fit([offsets], 2) or _profile_fit(xs.values(), 3)
    yfit = _profile_fit(ys.values(), 3)
    labels = {}
    for axis, fit, groups in (("x", xfit, [offsets, *xs.values()]), ("y", yfit, ys.values())):
        if fit is None:
            labels[axis] = sorted({v for group in groups for _, v in group})
    return xfit, yfit, labels


def _profile_chains(pieces):
    # CAD exporters may stroke each edge separately. Join only unambiguous, shared endpoints.
    outgoing, incoming = defaultdict(list), defaultdict(int)
    for i, (start, end) in enumerate(pieces):
        outgoing[start].append(i)
        incoming[end] += 1

    def interior(point):
        return incoming[point] == 1 and len(outgoing[point]) == 1

    visited = set()
    for i in sorted(range(len(pieces)), key=lambda i: interior(pieces[i][0])):
        if i in visited:
            continue
        points = [pieces[i][0]]
        while i not in visited:
            visited.add(i)
            end = pieces[i][1]
            points.append(end)
            if not interior(end):
                break
            i = outgoing[end][0]
        yield points


def _rdp_segments(points, tolerance):
    pending = [(0, len(points) - 1)]
    while pending:
        first, last = pending.pop()
        x, y = points[first]
        dx, dy = points[last][0] - x, points[last][1] - y
        squared = dx * dx + dy * dy
        farthest, pivot = tolerance, None
        for i in range(first + 1, last):
            px, py = points[i][0] - x, points[i][1] - y
            fraction = min(1.0, max(0.0, (px * dx + py * dy) / squared)) if squared else 0.0
            distance = math.hypot(px - fraction * dx, py - fraction * dy)
            if distance > farthest:
                farthest, pivot = distance, i
        if pivot is None:
            if points[first] != points[last]:
                yield points[first], points[last]
        else:
            pending.extend(((pivot, last), (first, pivot)))


def profile_data(case, file, page, layer=None, endpoints=None, min_segment_m=2.0, simplify_m=0.2):
    root = case.resolve()
    relative = Path(file.lstrip("/"))
    try:
        source = (root / relative).resolve()
    except (ValueError, OSError, RuntimeError):
        return {"unavailable": "The source path cannot be resolved."}
    if (relative.parts[:1] != ("sources",) or not source.is_relative_to(root / "sources")
            or source.suffix.lower() != ".pdf" or not source.is_file() or page < 1):
        return {"unavailable": "Choose an original PDF under /sources/ and a one-based page."}
    try:
        with pdfplumber.open(source, pages=[page]) as pdf:
            if not pdf.pages:
                return {"unavailable": "Page is outside the document."}
            sheet = pdf.pages[0]
            geometry = _ProfileGeometry(pdf.rsrcmgr)
            _ProfileInterpreter(pdf.rsrcmgr, geometry).process_page(sheet.page_obj)
            paths = geometry.paths
            xfit, yfit, unfitted_labels = _profile_axes(sheet, paths)
    except (OSError, PdfminerException, PDFException, PSException) as error:
        return {"unavailable": f"Could not read the PDF page ({type(error).__name__})."}
    info = dict(x_calibration=xfit, y_calibration=yfit)

    def calibrated(point, fit):
        return point*fit["metres_per_point"] + fit["intercept_m"]

    if layer is None or unfitted_labels:
        layers = {}
        for name, records in paths.items():
            xs, ys = zip(*(p for parts in records for _, points in parts for p in points))
            layers[name] = dict(path_count=len(records))
            for axis, values, fit in (("x_extent_m", xs, xfit), ("y_extent_m", ys, yfit)):
                layers[name][axis] = [calibrated(min(values), fit), calibrated(max(values), fit)] if fit else None
        if unfitted_labels:
            info.update(unavailable="Missing or ambiguous horizontal or elevation ticks.", unfitted_labels=unfitted_labels)
        return dict(info, layers=layers)
    if layer not in paths:
        return dict(info, unavailable="Layer is absent.", layers=list(paths))
    left, right = sorted(endpoints)
    if not all(map(math.isfinite, endpoints)) or left == right:
        return dict(info, unavailable="Choose two distinct finite endpoints in metres.")
    spans, pieces = [], []
    unavailable = None
    for segments in paths[layer]:
        for operator, raw in segments:
            points = [(calibrated(x, xfit), calibrated(y, yfit)) for x, y in raw]
            if max(x for x, _ in points) < left or min(x for x, _ in points) > right:
                continue
            if operator not in ("l", "h"):
                unavailable = "Selected interval contains a curve. Choose a polyline."
                continue
            (a, b), (c, d) = sorted(points)
            if c-a < 1e-8:
                if a == c and b == d:
                    continue
                if abs(b-d) >= .02:
                    unavailable = "A vertical segment makes the elevation ambiguous."
                pieces.append(tuple(points))
            else:
                spans.append((a, b, c, d))
                start, end = max(left, a), min(right, c)
                if start >= end:
                    continue
                slope = (d-b)/(c-a)
                start_y = b if start == a else b+(start-a)*slope
                end_y = d if end == c else b+(end-a)*slope
                pieces.append(((start, start_y), (end, end_y)))
    measured = []
    for chain in _profile_chains(pieces):
        for (start, start_y), (end, end_y) in _rdp_segments(chain, simplify_m):
            if (end-start)*(endpoints[1]-endpoints[0]) < 0:
                start, start_y, end, end_y = end, end_y, start, start_y
            length = abs(end-start)
            measured.append(dict(x_from=start, x_to=end, elevation_from_m=start_y, elevation_to_m=end_y,
                                 run_m=length, angle_deg=math.degrees(math.atan2(end_y-start_y, length))))
    measured.sort(key=lambda part: part["x_from"], reverse=endpoints[0] > endpoints[1])
    eligible = (part for part in measured if part["run_m"] >= min_segment_m)
    info.update(raw_piece_count=len(pieces), simplify_m=simplify_m, min_segment_m=min_segment_m,
                segments=measured, steepest_segment=max(eligible, key=lambda part: abs(part["angle_deg"]), default=None))
    if unavailable:
        return dict(info, unavailable=unavailable)
    knots = sorted({left, right, *(v for a, _, c, _ in spans for v in (a, c) if left < v < right)})

    def height(x):
        values = [b+(x-a)*(d-b)/(c-a) for a, b, c, d in spans if a-1e-8 <= x <= c+1e-8]
        return mean(values) if values and max(values)-min(values) < .02 else None

    # Check between knots too: two disjoint paths can have valid heights on both ends of a gap.
    if any(height(x) is None for x in [*knots, *((a+b)/2 for a, b in zip(knots, knots[1:]))]):
        return dict(info, unavailable="The range crosses a gap, overlapping profiles, or the layer boundary.")
    first, last = map(height, endpoints)
    rise, run = last-first, right-left
    return dict(info, layer=layer, x_from=endpoints[0], x_to=endpoints[1], elevation_from_m=first,
                elevation_to_m=last, H0_m=rise, L0_m=run, alpha0_deg=math.degrees(math.atan2(rise, run)),
                point_count=len(knots))
