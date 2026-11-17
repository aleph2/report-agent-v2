"""Task 11 probes; keep all outputs outside Git. One image request per output folder.

API references:
https://help.aliyun.com/zh/model-studio/deepseek-api
https://docling-project.github.io/docling/reference/document_converter/
https://docling-project.github.io/docling/_generated/examples/export_tables/
"""
import argparse
import base64
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import resource
import time


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def image_probe(args):
    from dotenv import dotenv_values
    from openai import OpenAI
    from PIL import Image, ImageDraw, ImageFont

    settings = dotenv_values(args.env)
    fixture = Image.new("RGB", (64, 64), "white")
    ImageDraw.Draw(fixture).text((32, 32), "37", font=ImageFont.load_default(size=20), anchor="mm", fill="black")
    fixture.save(args.output / "tiny-number.png")
    buffer = BytesIO()
    fixture.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()
    assert len(encoded) < 1024
    result = dict(model=settings["MODEL_NAME"], expected="37", prompt="What number is shown?",
                  cap=2000, output_limit=128, retries=0, image_bytes=len(buffer.getvalue()),
                  image_sha256=hashlib.sha256(buffer.getvalue()).hexdigest(),
                  started_utc=datetime.now(timezone.utc).isoformat(), status="dispatching")
    client = OpenAI(api_key=settings["OPENAI_API_KEY"], base_url=settings["OPENAI_BASE_URL"], max_retries=0, timeout=90)
    marker = args.output / "image-attempt.json"
    with marker.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    started = time.monotonic()
    try:
        reply = client.chat.completions.create(
            model=settings["MODEL_NAME"], temperature=0, max_tokens=128,
            extra_body={"enable_thinking": False},
            messages=[{"role": "user", "content": [
                {"type": "text", "text": result["prompt"]},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded, "detail": "low"}},
            ]}],
        )
        result.update(accepted=True, response=reply.model_dump(),
                      usage=reply.usage.model_dump() if reply.usage else None,
                      within_cap=reply.usage.total_tokens <= 2000 if reply.usage else None)
    except Exception as error:
        result.update(accepted=False, error_type=type(error).__name__,
                      status_code=getattr(error, "status_code", None),
                      error=str(error).replace(settings["OPENAI_API_KEY"], "[REDACTED]"), usage=None)
    finally:
        client.close()
        result.update(status="finished", elapsed_seconds=round(time.monotonic() - started, 3))
        save(marker, result)
    return result


def docling_probe(args):
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.settings import settings
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions(artifacts_path=settings.cache_dir / "models", do_ocr=False, do_table_structure=True,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=2))
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(
        pipeline_options=options, backend_options=ThreadedDoclingParseBackendOptions(parser_threads=2))})
    converted = converter.convert(args.pdf, page_range=(args.page, args.page))
    document = converted.document
    (args.output / "page.md").write_text(document.export_to_markdown(), encoding="utf-8")
    save(args.output / "document.json", document.export_to_dict())
    tables = []
    for number, table in enumerate(document.tables):
        frame = table.export_to_dataframe(doc=document)
        frame.to_csv(args.output / f"table-{number}.csv", index=False)
        (args.output / f"table-{number}.html").write_text(table.export_to_html(doc=document), encoding="utf-8")
        tables.append(dict(index=number, rows=table.data.num_rows, columns=table.data.num_cols))
    return dict(status=str(converted.status), page=args.page, tables=tables, device="cpu", model_threads=2, parser_threads=2, ocr=False)


def pdfium_probe(args):
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(args.pdf)
    page = pdf[args.page - 1]
    textpage = page.get_textpage()
    text = textpage.get_text_range()
    (args.output / "page.txt").write_text(text, encoding="utf-8")
    bitmap = page.render(scale=2)
    bitmap.to_pil().save(args.output / "page.png")
    result = dict(status="read and rendered", page=args.page, pages=len(pdf), characters=len(text))
    bitmap.close()
    textpage.close()
    page.close()
    pdf.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    for name in ("image", "docling", "pdfium"):
        command = commands.add_parser(name)
        command.add_argument("--output", type=Path, required=True)
        if name == "image":
            command.add_argument("--env", type=Path, required=True)
        else:
            command.add_argument("--pdf", type=Path, required=True)
            command.add_argument("--page", type=int, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = {"mode": args.mode}
    if args.mode == "image":
        result.update(image_probe(args))
    else:
        try:
            result.update({"docling": docling_probe, "pdfium": pdfium_probe}[args.mode](args))
        except Exception as error:
            result.update(status="error", error_type=type(error).__name__, error=str(error))
    result.update(wall_seconds=round(time.monotonic() - started, 3),
                  process_peak_rss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)
    save(args.output / "result.json", result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
