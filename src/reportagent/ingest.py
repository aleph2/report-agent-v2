from pathlib import Path

import pdfplumber


def ingest(case_dir):
    case_dir = Path(case_dir)
    sources = case_dir / "sources"
    index = ["# Sources\n"]
    for source in sorted(sources.iterdir()):
        if source.suffix.lower() != ".pdf":
            continue
        pages = sources / source.stem
        pages.mkdir(exist_ok=True)
        with pdfplumber.open(source) as pdf:
            index.append(f"## {source.name}\n\nPages: {len(pdf.pages)}\n")
            for number, page in enumerate(pdf.pages, 1):
                target = pages / f"page-{number:03d}.txt"
                target.write_text(page.extract_text() or "", encoding="utf-8")
                index.append(f"- {target.relative_to(case_dir).as_posix()}")
    target = sources / "index.md"
    target.write_text("\n".join(index) + "\n", encoding="utf-8")
    return target
