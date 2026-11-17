from pathlib import Path
import re

from docx import Document


def export_docx(case_dir):
    case_dir = Path(case_dir)
    document = Document()
    for section in sorted((case_dir / "work/sections").iterdir()):
        if section.suffix != ".md":
            continue
        paragraph = []
        for line in section.read_text(encoding="utf-8").splitlines() + [""]:
            heading = re.match(r"^(#{1,3}) (.*)$", line)
            if heading or not line.strip():
                if paragraph:
                    document.add_paragraph(" ".join(paragraph))
                    paragraph = []
                if heading:
                    document.add_heading(heading[2], level=len(heading[1]))
            else:
                paragraph.append(line.strip())
    output = case_dir / "out"
    output.mkdir(parents=True, exist_ok=True)
    target = output / "report.docx"
    document.save(target)
    return target
