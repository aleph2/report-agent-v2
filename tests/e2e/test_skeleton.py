from pathlib import Path
from shutil import copyfile

from docx import Document
from langchain_core.messages import AIMessage, ToolMessage

from reportagent.agent import build_agent
from reportagent.export.docx import export_docx
from reportagent.ingest import ingest
from tests.fake_model import ScriptedModel, call


def test_skeleton(tmp_path):
    case = tmp_path / "case"
    (case / "sources").mkdir(parents=True)
    (case / "commission.md").write_text("Write the hydrology section from the supplied source.")
    copyfile(Path(__file__).parents[1] / "fixtures/one-page.pdf", case / "sources/one-page.pdf")
    sentence = "The site drains toward the eastern channel."
    ingest(case)
    assert sentence in (case / "sources/one-page/page-001.txt").read_text()
    assert (case / "sources/index.md").read_text() == "# Sources\n\n## one-page.pdf\n\nPages: 1\n\nPage paths: sources/one-page/page-001.txt ... page-001.txt\n\n"
    section = f"# Hydrology\n\n## Drainage\n\n### Direction\n\n{sentence} [one-page.pdf, page 1]"
    model = ScriptedModel(responses=[
        call("read_file", file_path="/commission.md"),
        call("read_file", file_path="/sources/index.md"),
        call("read_file", file_path="/work/skills/write-section/SKILL.md"),
        call("read_file", file_path="/sources/one-page/page-001.txt"),
        call("write_file", file_path="/work/sections/01-hydrology.md", content=section),
        AIMessage(content="Done."),
    ])
    result = build_agent(case, model).invoke({"messages": [("user", "Write the requested section.")]})
    tools = [message for message in result["messages"] if isinstance(message, ToolMessage)]
    assert len(tools) == 5 and all(message.status == "success" for message in tools)
    assert sentence in tools[-2].content
    assert (case / "work/sections/01-hydrology.md").read_text() == section
    assert result["messages"][-1].content == "Done."
    export_docx(case)
    paragraphs = Document(case / "out/report.docx").paragraphs
    assert [(p.text, p.style.name) for p in paragraphs[:3]] == [
        ("Hydrology", "Heading 1"), ("Drainage", "Heading 2"), ("Direction", "Heading 3"),
    ]
    assert sentence in "\n".join(p.text for p in paragraphs)
