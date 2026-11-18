"""Checks on the Office bytes that are actually published."""

from zipfile import ZipFile

from docx import Document
from pptx import Presentation

from backend.harness.quality import INTERNAL


def inspect_office(folder):
    for suffix in ["pptx", "docx"]:
        with ZipFile(folder / f"travel.{suffix}") as archive:
            if archive.testzip() is not None:
                raise ValueError("Office 文件完整性检查未通过")
    word = Document(folder / "travel.docx")
    text = [p.text for p in word.paragraphs]
    text.extend(c.text for t in word.tables for r in t.rows for c in r.cells)
    ppt = Presentation(folder / "travel.pptx")
    for slide in ppt.slides:
        for shape in slide.shapes:
            if shape.left < 0 or shape.top < 0 or shape.left + shape.width > ppt.slide_width + 100 or shape.top + shape.height > ppt.slide_height + 100:
                raise ValueError("幻灯片对象越出页面")
            if shape.has_text_frame:
                text.append(shape.text)
            if shape.has_table:
                text.extend(c.text for r in shape.table.rows for c in r.cells)
    if any(INTERNAL.search(t) for t in text):
        raise ValueError("实际 Office 文件包含内部日志词，未发布")
    toc = [p for p in word.paragraphs if p.style.name == "TOC 1"]
    if not toc or any(not any(r.text.isdigit() and int(r.text) > 0 for r in p.runs) for p in toc):
        raise ValueError("Word 目录没有经实际页码更新")
    return {"ppt_slides": len(ppt.slides), "toc_entries": len(toc), "office_integrity": True, "reader_text_clean": True}
