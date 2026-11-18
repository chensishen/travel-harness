"""Populate native Word TOC cached page numbers from actual PDF outline destinations."""

from docx import Document
from pypdf import PdfReader


def update_toc(path, pdf):
    doc = Document(path)
    entries = [p for p in doc.paragraphs if p.style.name == "TOC 1"]
    if not entries:
        return False
    reader = PdfReader(pdf)
    pages = {}

    def visit(items):
        for entry in items:
            if isinstance(entry, list):
                visit(entry)
            else:
                pages[str(entry.title)] = reader.get_destination_page_number(entry) + 1

    visit(reader.outline)
    changed = False
    for paragraph in entries:
        title = "".join(paragraph._p.xpath(".//w:hyperlink//w:t/text()"))
        if title not in pages:
            raise ValueError("无法从实际 PDF 核对目录页码：" + title)
        page_run = next((r for r in paragraph.runs if r.text.isdigit()), None)
        if page_run is None:
            raise ValueError("目录缺少页码域缓存")
        value = str(pages[title])
        if page_run.text != value:
            page_run.text = value
            changed = True
    if changed:
        doc.save(path)
    return changed
