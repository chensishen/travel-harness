"""Practical Word report with a native TOC, time tables, and reader-only appendices."""

from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT, WD_TAB_LEADER
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt

from backend.render.reader_content import basis, checks, citation, fact_rows, photo_caption
from backend.render.word import fact_table, paragraphs, styled_document


def render_word_v11(document, path, primary):
    doc = styled_document(document)
    doc.styles["Normal"].font.size = Pt(10.5)
    doc.styles["Normal"].paragraph_format.line_spacing = Pt(16)
    doc.styles["Normal"].paragraph_format.space_after = Pt(6)
    doc.styles["Heading 1"].font.size = Pt(20)
    doc.styles["Heading 2"].font.size = Pt(16)
    for name in ["Heading 1", "Heading 2"]:
        doc.styles[name].paragraph_format.space_before = Pt(10)
        doc.styles[name].paragraph_format.space_after = Pt(8)
    doc.sections[0].header.paragraphs[0].text = (
        document.spec.city
        + " · "
        + ("行程与参观" if document.spec.type == "travel_guide" else "城市文化")
    )
    guide = document.spec.type == "travel_guide"
    toc_entries = []

    def heading(text, level=1, new_page=False):
        p = doc.add_heading(text, level)
        if new_page:
            p.paragraph_format.page_break_before = True
        identifier = len(toc_entries) + 1
        start = OxmlElement("w:bookmarkStart")
        start.set(qn("w:id"), str(identifier))
        start.set(qn("w:name"), f"TravelHeading{identifier}")
        end = OxmlElement("w:bookmarkEnd")
        end.set(qn("w:id"), str(identifier))
        p._p.insert(0, start)
        p._p.append(end)
        toc_entries.append((text, f"TravelHeading{identifier}"))
        return p

    def image(path, caption, width=6.35, max_height=None):
        if max_height:
            from PIL import Image

            with Image.open(path) as source:
                width = min(width, max_height * source.width / source.height)
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.line_spacing = 1
        p.paragraph_format.keep_with_next = True
        p.add_run().add_picture(str(path), width=Inches(width))
        p = doc.add_paragraph(caption, "Caption")
        p.paragraph_format.keep_with_next = True

    def source_link(p, text, url):
        from docx.opc.constants import RELATIONSHIP_TYPE

        relationship = p.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        link = OxmlElement("w:hyperlink")
        link.set(qn("r:id"), relationship)
        run = OxmlElement("w:r")
        node = OxmlElement("w:t")
        node.text = text
        run.append(node)
        link.append(run)
        p._p.append(link)

    p = doc.add_paragraph(document.spec.city + ("旅游攻略" if guide else "城市文化"), "Title")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p = doc.add_paragraph(
        f"{document.spec.start_date} 起 · {document.spec.days} 天 · {document.spec.travelers} 人"
        if guide
        else "历史、空间与地方文化",
        "Subtitle",
    )
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hero = next((i for poi in document.pois for i in poi.images), None)
    hero_poi = next((p for p in document.pois if p.images), None)
    if document.decorations:
        from backend.render.documents import image_info

        hero_path = image_info(document.decorations[:1], path.parent)["image"]
        image(hero_path, "装饰插画 · 非实景")
    elif hero:
        image(hero.local_path, photo_caption(hero_poi, hero))
    doc.add_paragraph(
        "编制日期 " + document.created_at[:10] + "；参观信息以所列官方渠道和核验日期为依据。"
    )
    doc.add_page_break()
    doc.add_paragraph("目录", "Title")
    toc_marker = doc.add_paragraph()
    doc.add_page_break()

    if guide:
        heading("行程总览与出行准备")
        doc.add_paragraph(
            f"目的地：{document.spec.city}；日期：{document.spec.start_date} 起；人数：{document.spec.travelers}；预算偏好：{document.spec.budget_level}。"
        )
        for assumption in document.spec.assumptions:
            doc.add_paragraph(assumption)
        if len(document.days) > 1:
            image(
                path.parent / "overview.png",
                "各天采用不同颜色；实线为乘车参考，虚线为步行。© OpenStreetMap contributors",
            )
        for day in document.days:
            heading(f"第 {day.day} 天 · {day.date}", new_page=day.day > 1 or len(document.days) > 1)
            doc.add_paragraph(
                day.theme
                + f" · 全日 {day.total_minutes // 60} 小时 {day.total_minutes % 60} 分（游玩、交通与用餐；不含机动缓冲）",
                "Subtitle",
            )
            for note in day.notes:
                doc.add_paragraph(note, "Caption")
            image(
                day.map_png,
                (
                    "路线含OSRM步行路径；站序连线为线路示意，非轨迹。"
                    if any(
                        s.geometry_mode == "transit_schematic"
                        for s in ([day.arrival_segment] if day.arrival_segment else [])
                        + day.segments
                    )
                    else "实线为乘车参考，虚线为步行。"
                    if day.segments or day.arrival_segment
                    else "景点位置图。"
                )
                + "© OpenStreetMap contributors",
                width=5.1 if len(document.days) == 1 else 6.1,
            )
            doc.paragraphs[-1].paragraph_format.keep_with_next = False
            table = doc.add_table(rows=1, cols=5)
            table.style = "Light Shading Accent 1"
            table.autofit = False
            for column, width in zip(table.columns, [1.0, 1.65, 0.6, 1.3, 1.8], strict=True):
                column.width = Inches(width)
            for cell, text in zip(
                table.rows[0].cells, ["时间", "地点", "停留", "交通", "备注"], strict=True
            ):
                cell.text = text
            for entry in day.timeline:
                duration = (int(entry.end[:2]) * 60 + int(entry.end[3:])) - (
                    int(entry.start[:2]) * 60 + int(entry.start[3:])
                )
                note = "路网估算，不含排队" if entry.kind == "travel" else entry.note
                for cell, value in zip(
                    table.add_row().cells,
                    [
                        f"{entry.start}–{entry.end}",
                        entry.place,
                        f"{duration} 分钟",
                        entry.transport or "—",
                        note,
                    ],
                    strict=True,
                ):
                    cell.text = value
                    for p in cell.paragraphs:
                        p.paragraph_format.line_spacing = 1
                        p.paragraph_format.space_after = Pt(0)
                        p.paragraph_format.space_before = Pt(0)
                        for run in p.runs:
                            run.font.size = Pt(8.5)
            repeat = OxmlElement("w:tblHeader")
            table.rows[0]._tr.get_or_add_trPr().append(repeat)
            for row_index, row in enumerate(table.rows):
                row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
                for cell in row.cells:
                    for paragraph in cell.paragraphs:
                        paragraph.paragraph_format.keep_with_next = row_index < len(table.rows) - 1

    for index, poi in enumerate(document.pois):
        heading(poi.name, new_page=True)
        if poi.images:
            image(
                poi.images[0].local_path,
                photo_caption(poi, poi.images[0]),
                width=4.7,
                max_height=3.0,
            )
            for label, url in [
                (" 图片来源", poi.images[0].source_page),
                (" 授权说明", poi.images[0].license_url),
            ]:
                if str(url).startswith(("http://", "https://")):
                    source_link(doc.paragraphs[-1], label, str(url))
            if len(poi.images) > 1:
                image(
                    poi.images[1].local_path,
                    photo_caption(poi, poi.images[1]),
                    width=4.0,
                    max_height=2.6,
                )
        else:
            card = doc.add_table(rows=1, cols=1)
            cell = card.cell(0, 0)
            shading = OxmlElement("w:shd")
            shading.set(qn("w:fill"), "F1F3F4")
            cell._tc.get_or_add_tcPr().append(shading)
            card.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
            cell.paragraphs[0].text = f"{poi.name}\n{poi.photo_status}\n可在任务页上传实拍"
            if poi.map_thumbnail:
                p = cell.add_paragraph()
                p.paragraph_format.line_spacing = 1
                p.add_run().add_picture(poi.map_thumbnail, width=Inches(2.0))
                cell.add_paragraph("景点近景图 · © OpenStreetMap contributors", "Caption")
            for p in cell.paragraphs:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                p.paragraph_format.keep_with_next = True
        for b in poi.blocks[:4] if guide else poi.blocks:
            for text in paragraphs(b.text):
                doc.add_paragraph(text + citation(b.source_ids))
        fact_table(doc, fact_rows(poi, document.sources), primary)
        for cell in doc.tables[-1].rows[-1].cells:
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_with_next = True
        doc.paragraphs[-1].paragraph_format.keep_with_next = True
        p = doc.add_paragraph(f"建议停留：{poi.duration_min} 分钟（规划参考）。")
        p.paragraph_format.keep_with_next = bool(poi.official_url or poi.contact)
        if poi.official_url:
            p = doc.add_paragraph("官方参观渠道：", "Caption")
            p.paragraph_format.keep_with_next = bool(poi.contact)
            source_link(p, poi.official_url, poi.official_url)
        if poi.contact:
            doc.add_paragraph("官方咨询：" + poi.contact, "Caption")

    if guide:
        for category, title in [
            ("food", "餐饮安排"),
            ("budget", "预算"),
            ("transport", "到达与市内交通"),
            ("notice", "天气、排队与注意事项"),
        ]:
            items = [i for i in document.practical if i.category == category]
            if not items:
                continue
            heading(title, new_page=category == "food")
            if category == "budget":
                fact_table(
                    doc,
                    [
                        [i.label, i.value, basis(document, i.source_ids, i.estimated, i.basis)]
                        for i in items
                    ],
                    primary,
                    headers=["项目", "安排与参考", "依据"],
                )
            else:
                for item in items:
                    if item.label not in {title, "用餐安排", "到达与市内交通", "天气与排队"}:
                        doc.add_paragraph(item.label, "Heading 3")
                    for text in paragraphs(item.value):
                        doc.add_paragraph(
                            text
                            + "（"
                            + basis(document, item.source_ids, item.estimated, item.basis)
                            + "）"
                        )
        if document.alternatives:
            heading("备选与未纳入景点")
            for item in document.alternatives:
                doc.add_paragraph(
                    item["name"]
                    + "："
                    + item["reason"]
                    + ("；时段：" + item["replace_day"] if item.get("replace_day") else "")
                )
    else:
        for chapter in document.chapters:
            heading(chapter.title, new_page=chapter == document.chapters[0])
            if chapter.period:
                doc.add_paragraph(chapter.period, "Subtitle")
            for b in chapter.blocks:
                for text in paragraphs(b.text):
                    doc.add_paragraph(text + citation(b.source_ids))
        if document.alternatives:
            heading("未纳入详细专题的候选地点", new_page=True)
            for item in document.alternatives:
                doc.add_paragraph(item["name"] + "：" + item["reason"])

    heading("出行前须自行核实清单")
    reader_checks = checks(document)
    for index, (text, url) in enumerate(reader_checks):
        p = doc.add_paragraph(text + " ")
        p.paragraph_format.keep_with_next = index < len(reader_checks) - 1
        targets = (
            document.reader_checks[index].targets
            if document.prd_version == "1.3" and index < len(document.reader_checks)
            else []
        )
        if targets:
            for target in targets:
                source_link(p, target["poi"] + "官方核实链接 ", target["url"])
        else:
            source_link(p, "官方渠道", url)
    heading("参考来源", new_page=True)
    from urllib.parse import urlparse

    for index, source in enumerate(document.sources):
        p = doc.add_paragraph(f"[{source.id}] {source.title}")
        p.paragraph_format.keep_with_next = True
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.line_spacing = 1
        for run in p.runs:
            run.font.size = Pt(10)
        p = doc.add_paragraph(
            f"{source.level} · 来源发布 {source.published_at or '未载明'} / 访问 {source.fetched_at[:10]} · ",
            "Caption",
        )
        p.paragraph_format.line_spacing = 1
        p.paragraph_format.space_after = Pt(6)
        p.paragraph_format.keep_with_next = len(document.sources) - 3 <= index < len(document.sources) - 1
        source_link(p, urlparse(str(source.url)).hostname or "来源链接", str(source.url))

    if "TOC 1" not in doc.styles:
        doc.styles.add_style("TOC 1", WD_STYLE_TYPE.PARAGRAPH)
    toc_style = doc.styles["TOC 1"]
    toc_style.font.name = "Source Han Sans SC"
    toc_style.font.size = Pt(10)
    toc_style.paragraph_format.line_spacing = 1.1
    toc_style.paragraph_format.space_after = Pt(5)
    last = toc_marker
    for index, (text, anchor) in enumerate(toc_entries):
        p = doc.add_paragraph(style="TOC 1")
        p.paragraph_format.tab_stops.add_tab_stop(
            Inches(6.1), WD_TAB_ALIGNMENT.RIGHT, WD_TAB_LEADER.DOTS
        )
        if index == 0:
            begin = OxmlElement("w:fldChar")
            begin.set(qn("w:fldCharType"), "begin")
            p.add_run()._r.append(begin)
            instruction = OxmlElement("w:instrText")
            instruction.text = ' TOC \\o "1-2" \\h \\z \\u '
            p.add_run()._r.append(instruction)
            separate = OxmlElement("w:fldChar")
            separate.set(qn("w:fldCharType"), "separate")
            p.add_run()._r.append(separate)
        link = OxmlElement("w:hyperlink")
        link.set(qn("w:anchor"), anchor)
        run = OxmlElement("w:r")
        node = OxmlElement("w:t")
        node.text = text
        run.append(node)
        link.append(run)
        p._p.append(link)
        p.add_run("\t")
        p.add_run("0")
        last._p.addnext(p._p)
        last = p
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    last.add_run()._r.append(end)
    settings = doc.settings.element
    update = OxmlElement("w:updateFields")
    update.set(qn("w:val"), "true")
    settings.append(update)
    doc.save(path)
