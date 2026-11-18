"""Editable editorial slides. Full research stays in notes rather than overflowing pages."""

import re

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt

INK, MUTED, PAPER = "182B31", "56656B", "F2F5F3"


def add_text(slide, text, x, y, w, h, size=20, color=INK, bold=False):
    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    frame = box.text_frame
    frame.word_wrap = True
    frame.margin_left = frame.margin_right = 0
    frame.margin_top = frame.margin_bottom = 0
    for i, line in enumerate(text.split("\n")):
        p = frame.paragraphs[0] if i == 0 else frame.add_paragraph()
        p.text = line
        p.font.name, p.font.size, p.font.bold = "Source Han Sans SC", Pt(size), bold
        p.font.color.rgb = RGBColor.from_string(color)
        p.line_spacing = 1.3
        p.space_after = Pt(10)
        ea = OxmlElement("a:ea")
        ea.set("typeface", "Source Han Sans SC")
        p._p.get_or_add_pPr().get_or_add_defRPr().append(ea)
    return box


def rectangle(slide, x, y, w, h, color):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = RGBColor.from_string(color)
    shape.line.fill.background()
    return shape


def picture(slide, path, x, y, w, h, crop=True):
    with Image.open(path) as image:
        iw, ih = image.size
    if not crop:
        scale = min(w / iw, h / ih)
        return slide.shapes.add_picture(
            path,
            Inches(x + (w - iw * scale) / 2),
            Inches(y + (h - ih * scale) / 2),
            width=Inches(iw * scale),
            height=Inches(ih * scale),
        )
    result = slide.shapes.add_picture(path, Inches(x), Inches(y), width=Inches(w), height=Inches(h))
    if iw / ih > w / h:
        result.crop_left = result.crop_right = (1 - (w / h) / (iw / ih)) / 2
    else:
        result.crop_top = result.crop_bottom = (1 - (iw / ih) / (w / h)) / 2
    return result


def excerpts(lines, count=3, limit=95):
    """Select complete sentences verbatim without cutting off conditions or qualifiers."""
    result = []
    for line in lines:
        advice = "编辑建议：" if line.startswith("编辑建议：") else ""
        sentences = re.findall(r"[^。！？\n]+[。！？]?", line.removeprefix("编辑建议："))
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) > limit:
                continue
            sentence = advice + sentence
            if sentence not in result:
                result.append(sentence)
            if len(result) == count:
                return result
    return result


def notes(slide, item):
    parts = [item.get("title", ""), item.get("period", ""), *item.get("body", [])]
    parts += ["：".join(row) for row in item.get("facts", [])]
    parts += [item.get("image_credit", ""), "来源：" + ", ".join(item.get("sources", []))]
    slide.notes_slide.notes_text_frame.text = "\n".join(p for p in parts if p)


def render_slides(document, content, path, primary):
    if document.prd_version in {"1.1", "1.3"}:
        from backend.render.slides_v11 import render_slides_v11
        return render_slides_v11(document, path, primary)
    ppt = Presentation()
    ppt.slide_width, ppt.slide_height = Inches(13.333), Inches(7.5)
    title = document.spec.city + (
        "旅游攻略" if document.spec.type == "travel_guide" else "文旅介绍"
    )
    chapters = [s for s in content if s["kind"] == "chapter"]
    pois = [s for s in content if s["kind"] == "poi"]
    hero = next((s for s in chapters + pois if s.get("image")), None)

    def page(title="", dark=False):
        slide = ppt.slides.add_slide(ppt.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string(primary if dark else "FFFFFF")
        color = "FFFFFF" if dark else INK
        if title:
            add_text(slide, title, 0.65, 0.48, 12, 1.05, 30 if len(title) < 25 else 24, color, True)
        add_text(
            slide, f"{len(ppt.slides):02d}", 12, 7.03, 0.7, 0.25, 10, "FFFFFF" if dark else MUTED
        )
        return slide

    def footer(slide, item, dark=False):
        refs = ", ".join(item.get("sources", []))
        text = " · ".join(
            t for t in [item.get("caption", ""), f"来源：{refs}" if refs else ""] if t
        )
        add_text(slide, text, 0.65, 6.83, 10.9, 0.48, 9, "FFFFFF" if dark else MUTED)
        notes(slide, item)

    slide = page(dark=True)
    if hero:
        picture(slide, hero["image"], 5.5, 0, 7.833, 7.5)
        rectangle(slide, 0, 0, 5.5, 7.5, primary)
        add_text(slide, hero["caption"], 5.85, 7.08, 6, 0.25, 9, INK)
    add_text(slide, document.spec.city, 0.7, 1.35, 4.6, 1.25, 56, "FFFFFF", True)
    add_text(
        slide,
        "旅游攻略" if document.spec.type == "travel_guide" else "文旅介绍",
        0.75,
        2.8,
        4.3,
        0.8,
        32,
        "FFFFFF",
    )
    add_text(
        slide,
        "路线与文化阅读" if document.spec.type == "travel_guide" else "城市文化阅读",
        0.75,
        4.55,
        4.3,
        0.6,
        18,
        "FFFFFF",
    )
    add_text(slide, document.created_at[:10], 0.75, 6.35, 4, 0.4, 13, "FFFFFF")
    slide.notes_slide.notes_text_frame.text = (
        title + "\n编制说明：\n" + "\n".join(document.spec.assumptions)
    )

    slide = page("阅读导览")
    topics = (
        [s["title"] for s in chapters]
        or [s["title"] for s in content if s["kind"] == "day"]
        or [s["title"] for s in pois]
    )
    # Overview is an index, so long outlines continue without clipping or dropping topics.
    for start in range(0, len(topics), 8):
        if start:
            slide = page("阅读导览 续")
        for i, topic in enumerate(topics[start : start + 8]):
            x, y = 0.75 + (i // 4) * 6.2, 1.8 + (i % 4) * 1.12
            add_text(slide, f"{start + i + 1:02d}", x, y, 0.7, 0.5, 24, primary, True)
            add_text(slide, topic, x + 0.9, y, 4.65, 0.88, 18)
    add_text(
        slide,
        "正文展示重点；完整研究文字、参观信息与来源见 Word。",
        0.75,
        6.5,
        11.5,
        0.4,
        13,
        MUTED,
    )

    for item in [s for s in content if s["kind"] in {"overview", "day"}]:
        slide = page(item["title"])
        if item.get("image"):
            picture(slide, item["image"], 4.7, 1.6, 8, 4.95, crop=False)
        lines = (
            [f"第 {day.day} 天 · {len(day.stops)} 个景点" for day in document.days]
            if item["kind"] == "overview"
            else item["stops"]
        )
        spacing = min(0.65, 4.7 / max(len(lines), 1))
        for i, line in enumerate(lines):
            add_text(slide, f"{i + 1:02d}", 0.7, 1.85 + i * spacing, 0.6, 0.42, 18, primary, True)
            add_text(slide, line, 1.45, 1.85 + i * spacing, 3.1, 0.55, 14)
        footer(slide, item)

    for item in [s for s in content if s["kind"] == "timeline"]:
        slide = page(item["title"])
        rectangle(slide, 3.4, 2, 0.025, (len(item["events"]) - 1) * 1.13 + 0.15, "D4DFD9")
        for i, event in enumerate(item["events"]):
            y = 1.85 + i * 1.13
            add_text(slide, event["year"], 0.75, y, 2.4, 0.6, 26, primary, True)
            dot = slide.shapes.add_shape(
                MSO_SHAPE.OVAL, Inches(3.28), Inches(y + 0.12), Inches(0.27), Inches(0.27)
            )
            dot.fill.solid()
            dot.fill.fore_color.rgb = RGBColor.from_string(primary)
            dot.line.fill.background()
            add_text(slide, event["text"], 3.95, y, 8.6, 0.95, 17)
        footer(slide, item)

    for index, item in enumerate(chapters):
        dark = index % 3 == 2
        slide = page(item["title"], dark=dark)
        color = "FFFFFF" if dark else INK
        lines = excerpts(item["body"], count=2, limit=90)
        left_image = index % 3 == 1
        image_x, text_x = (0.65, 7.1) if left_image else (6.5, 0.75)
        if item.get("image"):
            picture(slide, item["image"], image_x, 1.7, 5.95, 4.85)
        else:
            rectangle(slide, image_x, 1.7, 5.95, 4.85, PAPER)
            add_text(slide, f"{index + 1:02d}", image_x + 0.5, 2.3, 4.8, 1.4, 64, primary, True)
            add_text(slide, "文化主题", image_x + 0.5, 4.5, 4.8, 0.6, 24, INK)
        add_text(slide, item.get("period") or "文化阅读", text_x, 1.72, 5.3, 0.5, 14, color)
        for i, line in enumerate(lines):
            add_text(slide, line, text_x, 2.45 + i * 2.0, 5.25, 1.8, 18, color)
        if not lines:
            add_text(slide, "该主题完整材料见 Word 与本页备注。", text_x, 2.6, 5.2, 1, 20, color)
        footer(slide, item, dark)

    # Two destinations per spread; empty POI prose never creates an empty slide.
    for start in range(0, len(pois), 2):
        pair = pois[start : start + 2]
        slide = page(
            "景点阅读" if len(pois) <= 2 else f"景点阅读 {start + 1:02d}—{start + len(pair):02d}"
        )
        full_notes = []
        for i, item in enumerate(pair):
            x = 0.65 + i * 6.18
            if item.get("image"):
                picture(slide, item["image"], x, 1.6, 5.8, 2.65)
            else:
                rectangle(slide, x, 1.6, 5.8, 2.65, PAPER)
                add_text(slide, f"{start + i + 1:02d}", x + 0.35, 2, 5, 1.5, 64, primary)
            add_text(
                slide,
                item["title"],
                x,
                4.48,
                5.8,
                0.7,
                24 if len(item["title"]) < 18 else 19,
                INK,
                True,
            )
            selected = excerpts(item["body"] + item["highlights"], count=1, limit=100)
            description = (
                selected[0]
                if selected
                else f"编辑建议：可预留约 {item['duration']} 分钟参观（规划估算）。"
            )
            add_text(slide, description, x, 5.2, 5.8, 1.25, 16)
            add_text(slide, item.get("caption", ""), x, 4.22, 5.8, 0.23, 8, MUTED)
            add_text(
                slide,
                "参观信息见 Word"
                + (" · " + ", ".join(item["sources"]) if item["sources"] else " · 以官方渠道为准"),
                x,
                6.55,
                5.8,
                0.4,
                9,
                MUTED,
            )
            notes(slide, item)
            full_notes.append(slide.notes_slide.notes_text_frame.text)
        slide.notes_slide.notes_text_frame.text = "\n\n".join(full_notes)

    slide = page("出行前复核", dark=True)
    add_text(slide, f"{len(document.warnings):02d}", 0.8, 1.8, 3.5, 1.5, 72, "FFFFFF", True)
    add_text(slide, "项研究与规划提示", 0.85, 3.5, 4, 0.6, 22, "FFFFFF")
    for i, text in enumerate(
        [
            "票价、开放时间与预约以官方渠道为准。",
            "停留时长和公共交通时间为规划估算。",
            "插画为文化意象；完整待核验清单见 Word 附录及本页备注。",
        ]
    ):
        add_text(slide, text, 5.5, 1.9 + i * 1.4, 6.7, 1.1, 22, "FFFFFF")
    slide.notes_slide.notes_text_frame.text = (
        "编制说明\n"
        + "\n".join(document.spec.assumptions)
        + "\n待核验清单\n"
        + "\n".join(document.warnings)
    )

    for start in range(0, len(document.sources), 10):
        batch = document.sources[start : start + 10]
        slide = page("参考来源" + (f" {start // 10 + 1}" if len(document.sources) > 10 else ""))
        for i, source in enumerate(batch):
            rows = (len(batch) + 1) // 2
            x, y = 0.7 + (i // rows) * 6.2, 1.65 + (i % rows) * 0.97
            label = f"[{source.id}] {source.title}"
            box = add_text(
                slide, label[:56] + ("…" if len(label) > 56 else ""), x, y, 5.7, 0.65, 14
            )
            run = box.text_frame.paragraphs[0].runs[0]
            run.hyperlink.address = str(source.url)
            run.font.color.rgb = RGBColor.from_string(primary)
            run.font.underline = False
            add_text(
                slide,
                f"{source.level}　访问 {source.fetched_at[:10]}",
                x,
                y + 0.65,
                5.7,
                0.25,
                10,
                MUTED,
            )
        slide.notes_slide.notes_text_frame.text = "\n\n".join(
            f"[{s.id}] {s.title}\n{s.url}\n访问日期 {s.fetched_at[:10]} · {s.level}" for s in batch
        )
    ppt.save(path)
