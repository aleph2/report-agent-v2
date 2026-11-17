from contextlib import closing
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium


def ingest(case_dir):
    case_dir = Path(case_dir)
    sources = case_dir / "sources"
    index = ["# Sources\n"]
    for source in sorted(sources.iterdir()):
        if source.suffix.lower() != ".pdf":
            continue
        pages = sources / source.stem
        pages.mkdir(exist_ok=True)
        with pdfplumber.open(source) as pdf, pdfium.PdfDocument(source) as images:
            index.append(f"## {source.name}\n\nPages: {len(pdf.pages)}\n\n"
                         f"Page paths: {pages.relative_to(case_dir).as_posix()}/page-001.txt ... page-{len(pdf.pages):03d}.txt\n"
                         f"Page images: {pages.relative_to(case_dir).as_posix()}/page-001.png ... page-{len(pdf.pages):03d}.png\n")
            for number, page in enumerate(pdf.pages, 1):
                target = pages / f"page-{number:03d}.txt"
                try:
                    try:
                        text = page.extract_text() or ""
                    except Exception as error:
                        detail = " ".join(str(error).splitlines())
                        text = f"Text extraction failed: {detail}. See page-{number:03d}.png."
                    target.write_text(text, encoding="utf-8")
                    # https://pypdfium2.readthedocs.io/en/stable/python_api.html#pypdfium2._helpers.page.PdfPage.render
                    with closing(images[number - 1]) as image_page:
                        with closing(image_page.render(scale=1600 / max(image_page.get_size()))) as bitmap:
                            with bitmap.to_pil() as image:
                                image.save(target.with_suffix(".png"))
                finally:
                    page.close()
    target = sources / "index.md"
    target.write_text("\n".join(index) + "\n", encoding="utf-8")
    return target
