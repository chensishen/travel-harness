"""A readable illustrated report, with full evidence and review details in appendices."""

import re

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

MUTED = "56656B"


def field(paragraph, instruction):
    element = OxmlElement("w:fldSimple")
    element.set(qn("w:instr"), instruction)
    paragraph._p.append(element)


def paragraphs(text, budget=240):
    """Break long research blocks at sentence boundaries, preserving every character."""
    buffer = ""
    for sentence in re.findall(r"[^。！？]+[。！？]?", text):
        if buffer and len(buffer + sentence) > budget:
            yield buffer
            buffer = ""
        buffer += sentence
    if buffer:
        yield buffer


def add_image(doc, item, width=6.35):
    if not item.get("image"):
        return
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.line_spacing = 1.0
    p.paragraph_format.keep_with_next = True
    p.add_run().add_picture(item["image"], width=Inches(width))
    credit = item.get("image_credit") or item.get("caption", "")
    if item.get("sources"):
        credit += " · 来源：" + ", ".join(item["sources"])
    caption = doc.add_paragraph(credit, "Caption")
    caption.paragraph_format.keep_with_next = True


def fact_table(doc, rows, primary, headers=None):
    table = doc.add_table(rows=1, cols=3)
    table.autofit = False
    widths = [1.15, 3.85, 1.35]
    for column, width in zip(table.columns, widths, strict=True):
        column.width = Inches(width)
    for cell, text in zip(table.rows[0].cells, headers or ["项目", "参观信息", "来源发布 / 访问"], strict=True):
        cell.text = text
    repeat = OxmlElement("w:tblHeader")
    table.rows[0]._tr.get_or_add_trPr().append(repeat)
    for values in rows:
        for cell, text in zip(table.add_row().cells, values, strict=True):
            cell.text = text
    for index, row in enumerate(table.rows):
        for cell, width in zip(row.cells, widths, strict=True):
            cell.width = Inches(width)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            props = cell._tc.get_or_add_tcPr()
            shade = OxmlElement("w:shd")
            shade.set(
                qn("w:fill"), primary if index == 0 else ("F2F5F3" if index % 2 else "FFFFFF")
            )
            props.append(shade)
            margins = OxmlElement("w:tcMar")
            for side in ("top", "left", "bottom", "right"):
                margin = OxmlElement("w:" + side)
                margin.set(qn("w:w"), "90")
                margin.set(qn("w:type"), "dxa")
                margins.append(margin)
            props.append(margins)
            borders = OxmlElement("w:tcBorders")
            for side in ("top", "left", "bottom", "right"):
                edge = OxmlElement("w:" + side)
                edge.set(qn("w:val"), "single")
                edge.set(qn("w:sz"), "4")
                edge.set(qn("w:color"), "D9D9D9")
                borders.append(edge)
            props.append(borders)
            for p in cell.paragraphs:
                p.paragraph_format.space_after = Pt(3)
                p.paragraph_format.line_spacing = 1.2
                for run in p.runs:
                    run.font.size = Pt(9)
                    run.font.bold = index == 0
                    run.font.color.rgb = RGBColor.from_string("FFFFFF" if index == 0 else "182B31")
    for row_index,row in enumerate(table.rows):
        row._tr.get_or_add_trPr().append(OxmlElement('w:cantSplit'))
        for cell in row.cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_with_next=row_index<len(table.rows)-1
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def styled_document(document):
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.top_margin, section.bottom_margin = Inches(0.75), Inches(0.7)
    section.left_margin = section.right_margin = Inches(1.075)
    section.header_distance = section.footer_distance = Inches(0.32)
    for name in (
        "Normal",
        "Title",
        "Subtitle",
        "Heading 1",
        "Heading 2",
        "Heading 3",
        "Caption",
        "Header",
        "Footer",
        "List Bullet",
    ):
        style = doc.styles[name]
        style.font.name = "Source Han Sans SC"
        fonts = style._element.get_or_add_rPr().rFonts
        for attr in ("asciiTheme", "eastAsiaTheme", "hAnsiTheme", "cstheme"):
            fonts.attrib.pop(qn("w:" + attr), None)
        fonts.set(qn("w:eastAsia"), "Source Han Sans SC")
        style.font.color.rgb = RGBColor.from_string("000000")
    for name in ("Title", "Heading 1", "Heading 2", "Heading 3"):
        props = doc.styles[name]._element.get_or_add_pPr()
        for border in list(props.findall(qn("w:pBdr"))):
            props.remove(border)
    doc.styles["Subtitle"].font.size = Pt(13)
    doc.styles["Subtitle"].font.italic = False
    doc.styles["Subtitle"].paragraph_format.keep_with_next = True
    doc.styles["Caption"].font.bold = False
    for name, size in (("Title", 36), ("Heading 1", 25), ("Heading 2", 17), ("Heading 3", 13)):
        style = doc.styles[name]
        style.font.size = Pt(size)
        style.font.bold = True
        style.paragraph_format.line_spacing = 1.2
        style.paragraph_format.space_before = Pt(18)
        style.paragraph_format.space_after = Pt(12)
        style.paragraph_format.keep_with_next = True
    normal = doc.styles["Normal"]
    normal.font.size = Pt(11)
    normal.paragraph_format.line_spacing = Pt(18)
    normal.paragraph_format.space_after = Pt(8)
    normal.paragraph_format.widow_control = True
    for name in ("Caption", "Header", "Footer"):
        doc.styles[name].font.size = Pt(8)
        doc.styles[name].font.color.rgb = RGBColor.from_string(MUTED)
    doc.styles["Caption"].paragraph_format.space_after = Pt(6)
    doc.styles["Caption"].paragraph_format.line_spacing = Pt(11)
    section.header.paragraphs[0].text = document.spec.city + "  文旅阅读"
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    footer.add_run("第 ")
    field(footer, "PAGE")
    footer.add_run(" 页")

    return doc


def render_word(document, content, path, primary):
    if document.prd_version in {"1.1", "1.3"}:
        from backend.render.word_v11 import render_word_v11
        return render_word_v11(document, path, primary)
    doc = styled_document(document)

    chapters = [s for s in content if s["kind"] == "chapter"]
    pois = [s for s in content if s["kind"] == "poi"]
    hero = next((s for s in chapters + pois if s.get("image")), None)
    title = document.spec.city + (
        "旅游攻略" if document.spec.type == "travel_guide" else "文旅介绍"
    )
    doc.add_paragraph(title, "Title")
    doc.add_paragraph(
        "路线与文化阅读" if document.spec.type == "travel_guide" else "城市文化阅读", "Subtitle"
    )
    doc.add_paragraph("编制日期 " + document.created_at[:10])
    if hero:
        add_image(doc, hero)
    doc.add_paragraph(
        "本报告汇集文化主题、景点资料"
        + ("与每日路线" if document.spec.type == "travel_guide" else "")
        + "，供出行前阅读和规划参考。正文保留研究内容，参观信息附核验日期与来源；未核实项目集中注明，出行前请查阅官方渠道。"
    )
    doc.add_page_break()
    doc.add_heading("阅读导引", 1)
    for number, label in enumerate(
        [
            *(
                s["title"]
                for s in content
                if s["kind"] in {"overview", "day", "timeline", "chapter"}
            ),
            "景点与参观信息",
            "参考来源",
            "编制说明与待核验事项",
        ],
        1,
    ):
        doc.add_paragraph(f"{number:02d}　{label}")
    doc.add_paragraph("PPT 展示重点，完整正文、引用及待核验信息以本报告为准。")
    doc.add_page_break()

    poi_heading = False
    for item in content:
        kind = item["kind"]
        if kind == "poi":
            if not poi_heading:
                doc.add_heading("景点与参观信息", 1).paragraph_format.page_break_before = True
                poi_heading = True
            doc.add_heading(item["title"], 2)
            density = sum(len(line) for line in item["body"]) + sum(
                len(row[1]) for row in item.get("facts", [])
            )
            add_image(doc, item, width=4.0 if density > 350 else 6.0)
        else:
            if kind == "chapter":
                heading = re.split("[：:]", item["title"], maxsplit=1)
                doc.add_heading(heading[0], 1)
                if len(heading) == 2:
                    doc.add_paragraph(heading[1], "Subtitle")
                if item.get("period"):
                    doc.add_paragraph(
                        item["period"], "Caption"
                    ).paragraph_format.keep_with_next = True
            else:
                doc.add_heading(item["title"], 1)
                if item.get("theme"):
                    doc.add_paragraph(item["theme"], "Subtitle")
            add_image(doc, item, width=5.8 if kind == "chapter" else 6.35)
        for line in item["body"]:
            for paragraph in paragraphs(line):
                doc.add_paragraph(paragraph)
        if item.get("facts"):
            practical_labels = {"票价与优惠", "开放时间", "预约规则", "地址"}
            practical = [
                row for row in item["facts"] if set(row[0].split("、")) <= practical_labels
            ]
            if practical:
                fact_table(doc, practical, primary)
            for row in item["facts"]:
                if row not in practical:
                    p = doc.add_paragraph()
                    p.add_run(row[0] + "：").bold = True
                    p.add_run(row[1])
                    run = p.add_run("（" + row[2] + "）")
                    run.font.size = Pt(8)
        if item["sources"] and not item.get("image"):
            doc.add_paragraph("来源：" + ", ".join(item["sources"]), "Caption")

    doc.add_heading("参考来源", 1).paragraph_format.page_break_before = True
    for source in document.sources:
        p = doc.add_paragraph()
        p.paragraph_format.keep_with_next = True
        p.add_run(f"[{source.id}] {source.title}").bold = True
        p = doc.add_paragraph(str(source.url))
        p.paragraph_format.keep_with_next = True
        for run in p.runs:
            run.font.size = Pt(9)
        doc.add_paragraph(f"访问日期 {source.fetched_at[:10]}　来源层级 {source.level}", "Caption")
    doc.add_heading("编制说明与待核验事项", 1).paragraph_format.page_break_before = True
    spec = document.spec
    doc.add_paragraph(
        f"目的地：{spec.city}；人数：{spec.travelers}；预算：{spec.budget_level}；日期或季节：{spec.season_or_dates}。"
    )
    for assumption in spec.assumptions:
        doc.add_paragraph(assumption, "List Bullet")
    doc.add_paragraph(
        "票价、开放时间、预约与交通信息可能变化。停留时长及公共交通时间为规划估算，路线图为示意图。AI 插画不代表景点实拍。"
    )
    for i, warning in enumerate(document.warnings, 1):
        doc.add_paragraph(f"{i:02d}　{warning}")
    doc.save(path)
