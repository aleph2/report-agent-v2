from contextlib import closing
from io import BytesIO
from multiprocessing import Pipe, Process
from pathlib import Path

from docx import Document
from docx.drawing import Drawing
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.run import Run
from PIL import Image
import pdfplumber
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
