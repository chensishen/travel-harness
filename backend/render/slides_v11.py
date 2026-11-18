"""Practical reader-first decks; map and timeline share a page."""

import math
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.util import Inches, Pt

from backend.render.reader_content import (
    basis,
    brief_value,
    checks,
    fact_rows,
    photo_caption,
)
from backend.render.slides import INK, MUTED, PAPER, add_text, excerpts, picture, rectangle


def render_slides_v11(document, path, primary):
    ppt = Presentation()
    ppt.slide_width, ppt.slide_height = Inches(13.333), Inches(7.5)
    guide = document.spec.type == "travel_guide"

    def page(title="", dark=False):
        slide = ppt.slides.add_slide(ppt.slide_layouts[6])
        slide.background.fill.solid()
        slide.background.fill.fore_color.rgb = RGBColor.from_string(primary if dark else "FFFFFF")
        if title:
            add_text(
                slide,
                title,
                0.6,
                0.42,
                12.1,
                0.95,
                29 if len(title) < 28 else 23,
                "FFFFFF" if dark else INK,
                True,
            )
        add_text(
            slide, f"{len(ppt.slides):02}", 12.1, 7.04, 0.6, 0.25, 10, "FFFFFF" if dark else MUTED
        )
        return slide

    def references(slide, ids):
        ids = list(dict.fromkeys(ids))
        add_text(slide, "正文来源：" + "、".join(ids), 0.65, 6.93, 10.9, 0.3, 10, MUTED)
        by_id = {s.id: s for s in document.sources}
        slide.notes_slide.notes_text_frame.text = "\n".join(
            f"[{r}] {by_id[r].title}\n{by_id[r].url}" for r in ids if r in by_id
        )

    def table(slide, rows, x, y, widths, font_size=15):
        heights = [0.62] * len(rows)
        if document.prd_version == "1.3":
            heights = []
            for values in rows:
                lines = max(
                    sum(
                        max(
                            1,
                            math.ceil(
                                sum(1 if ord(c) > 255 else 0.55 for c in line)
                                / (width * 72 / font_size - 2)
                            ),
                        )
                        for line in str(value).split("\n")
                    )
                    for value, width in zip(values, widths, strict=True)
                )
                heights.append(max(0.48, (lines * font_size * 1.15 + 12) / 72))
            if sum(heights) > 4.95:
                raise ValueError("幻灯片表格内容超出可读高度，需要分页，不能缩字截断")
        height = min(4.9, sum(heights))
        shape = slide.shapes.add_table(
            len(rows), len(widths), Inches(x), Inches(y), Inches(sum(widths)), Inches(height)
        )
        grid = shape.table
        for col, width in zip(grid.columns, widths, strict=True):
            col.width = Inches(width)
        for row_index, values in enumerate(rows):
            if document.prd_version == "1.3":
                grid.rows[row_index].height = Inches(heights[row_index])
            for cell, value in zip(grid.rows[row_index].cells, values, strict=True):
                cell.text = str(value)
                cell.fill.solid()
                cell.fill.fore_color.rgb = RGBColor.from_string(
                    primary if row_index == 0 else PAPER if row_index % 2 else "FFFFFF"
                )
                for p in cell.text_frame.paragraphs:
                    p.font.name = "Source Han Sans SC"
                    p.font.size = Pt(font_size)
                    p.font.bold = row_index == 0
                    p.font.color.rgb = RGBColor.from_string("FFFFFF" if row_index == 0 else INK)
        return shape

    slide = page(dark=True)
    rectangle(slide, 0, 0, 5.3, 7.5, primary)
    hero = next((i for p in document.pois for i in p.images), None)
    decorative = bool(document.decorations)
    hero = document.decorations[0] if document.decorations else hero
    if hero:
        from backend.render.documents import image_info

        image = image_info([hero], Path(path).parent)["image"]
        picture(slide, image, 5.3, 0, 8.033, 6.88)
        rectangle(slide, 5.3, 6.9, 8.033, 0.6, "FFFFFF")
        caption = (
            "装饰插画 · 非实景"
            if decorative
            else photo_caption(next(p for p in document.pois if hero in p.images), hero)
        )
        add_text(slide, caption, 5.55, 7.05, 7.5, 0.25, 10, MUTED)
    add_text(slide, document.spec.city, 0.7, 1.35, 4.2, 1.2, 54, "FFFFFF", True)
    add_text(slide, "旅游攻略" if guide else "城市文化", 0.75, 2.9, 4.1, 0.8, 30, "FFFFFF")
    subtitle = (
        f"{document.spec.days} 天 · {document.spec.travelers} 人"
        if guide
        else "历史、空间与地方文化"
    )
    add_text(slide, subtitle, 0.75, 4.7, 4.1, 0.8, 19, "FFFFFF")
    add_text(slide, document.created_at[:10], 0.75, 6.45, 4.1, 0.4, 13, "FFFFFF")

    slide = page("阅读导航")
    topics = (
        [
            "行程总览与出行准备",
            "每日路线与时间轴",
            "景点亮点与参观信息",
            "餐饮安排",
            "预算",
            "出行前必核",
            "参考来源",
        ]
        if guide
        else [c.title for c in document.chapters] + ["景点与参观信息", "参考来源"]
    )
    for index, item in enumerate(topics[:9]):
        add_text(slide, f"{index + 1:02}", 0.8, 1.65 + index * 0.54, 0.65, 0.4, 19, primary, True)
        add_text(slide, item, 1.7, 1.65 + index * 0.54, 10.4, 0.45, 18)

    if guide:
        slide = page("出行准备")
        rows = [
            ["项目", "安排"],
            ["日期", f"{document.spec.start_date} 起，共 {document.spec.days} 天"],
            ["每日出发", document.spec.departure_time],
            ["人数与预算", f"{document.spec.travelers} 人 · {document.spec.budget_level}"],
            ["交通", "以每日路线页的方式为准；估算不等于公交时刻表"],
            ["到达安排", "根据实际抵达站与班次，调整前往首站的时间"],
        ]
        table(slide, rows, 0.7, 1.65, [2.2, 9.6], 19)
        if len(document.days) > 1:
            slide = page("行程总览")
            picture(
                slide, str(Path(path).parent / "overview.png"), 0.65, 1.6, 8.0, 4.95, crop=False
            )
            for i, day in enumerate(document.days):
                add_text(
                    slide,
                    f"第 {day.day} 天 · {day.date}\n" + " → ".join(p.name for p in day.stops),
                    9,
                    1.8 + i * 1.3,
                    3.6,
                    1.15,
                    16,
                )
        for day in document.days:
            slide = page(f"第 {day.day} 天 · {day.date}")
            has_lines = bool(day.segments or day.arrival_segment)
            if document.prd_version == "1.3":
                picture(slide, day.map_png, 0.55, 1.55, 7.2, 4.9, crop=False)
                segments = iter(([day.arrival_segment] if day.arrival_segment else []) + day.segments)
                timeline = []
                for entry in day.timeline:
                    value = entry.place.split('（')[0]
                    if entry.kind == 'travel':
                        segment = next(segments)
                        legs = [leg for leg in segment.transit.get('legs', []) if leg.get('mode') in {'subway', 'bus'}]
                        limits = segment.transit.get('total_min_range', [round(segment.duration_min)] * 2)
                        tier = {'official': 'A', 'osm_graph': 'B', 'fallback': 'C'}.get(segment.transit.get('confidence'), '待核')
                        if legs:
                            mode = '地铁' if all(leg['mode'] == 'subway' for leg in legs) else '公共交通'
                            mode += '/'.join(dict.fromkeys(str(leg.get('line', '')) for leg in legs))
                            places = legs[0]['from'] + '→' + legs[-1]['to']
                        else:
                            mode = {'foot': '步行', 'car': '打车'}.get(segment.geometry_mode, segment.mode)
                            places = segment.from_name + '→' + segment.to_name
                        value = f'{mode} · {limits[0]}–{limits[1]}分 · {tier}\n{places}'
                    timeline.append([f'{entry.start}\n{entry.end}', value])
                table(slide, [["时间", "地点与交通"], *timeline[:7]], 8.05, 1.55, [1.05, 3.7], 11)
                add_text(
                    slide,
                    f"游玩、交通与用餐 {day.total_minutes // 60}小时{day.total_minutes % 60}分；机动缓冲{day.buffer_minutes}分钟。"
                    + (
                        "A官方 / B线路估算 / C步行或打车估算；完整换乘及步行见交通章节。"
                        if has_lines
                        else "景点位置图。"
                    ),
                    0.65,
                    6.55,
                    11.9,
                    0.4,
                    11,
                    MUTED,
                )
                for offset in range(7, len(timeline), 7):
                    s = page(f"第{day.day}天时间轴续表")
                    table(s, [["时间", "地点与交通"], *timeline[offset:offset + 7]], 0.65, 1.6, [1.4, 10.5], 12)
            else:
                picture(slide, day.map_png, 0.55, 1.5, 7.7, 4.95, crop=False)
                timeline = [["时间", "地点与交通"]] + [
                    [f"{e.start}\n{e.end}", e.place + ("\n" + e.transport if e.transport else "")]
                    for e in day.timeline
                ]
                table(slide, timeline, 8.55, 1.5, [1.05, 3.15], 11 if len(timeline) > 9 else 13)
                add_text(
                    slide,
                    f"全日 {day.total_minutes // 60} 小时 {day.total_minutes % 60} 分 · "
                    + ("虚线：步行；实线：乘车参考" if has_lines else "景点位置图"),
                    0.65,
                    6.62,
                    11.9,
                    0.35,
                    11,
                    MUTED,
                )

    for poi in document.pois:
        slide = page(poi.name)
        if poi.images:
            picture(slide, poi.images[0].local_path, 0.65, 1.6, 5.8, 2.9, crop=False)
            caption = add_text(
                slide, photo_caption(poi, poi.images[0]), 0.65, 4.65, 5.8, 0.4, 9, MUTED
            )
            paragraph = caption.text_frame.paragraphs[0]
            paragraph.runs[0].hyperlink.address = str(poi.images[0].source_page)
            if poi.images[0].license_url.startswith(("http://", "https://")):
                run = paragraph.add_run()
                run.text = " 授权说明"
                run.hyperlink.address = poi.images[0].license_url
                run.font.size = Pt(9)
        else:
            rectangle(slide, 0.65, 1.6, 5.8, 3.25, PAPER)
            day = next((d for d in document.days if any(p.id == poi.id for p in d.stops)), None)
            if poi.map_thumbnail:
                picture(slide, poi.map_thumbnail, 1.9, 2.95, 3.3, 1.55, crop=False)
            add_text(slide, f"{poi.name} · {poi.photo_status}", 0.9, 2.0, 5.1, 0.45, 17, MUTED)
            add_text(slide, "可在任务页上传实拍图", 0.9, 2.5, 5.1, 0.3, 12, MUTED)
        rows = fact_rows(poi, document.sources)
        summary = []
        for label, value, source_info in rows:
            refs = next(
                (
                    f.source_ids
                    for f in poi.facts
                    if {
                        "门票": "ticket",
                        "开放时间": "hours",
                        "预约": "booking",
                        "地址": "address",
                    }.get(label)
                    == f.field
                ),
                [],
            )
            published = next(
                (s.published_at for s in document.sources if s.id in refs and s.published_at), None
            )
            accessed = next(
                (str(f.verified_at) for f in poi.facts if f.source_ids == refs and refs), "未核实"
            )
            summary.append(
                [
                    label,
                    brief_value(label, value) + f"\n发布 {published or '未载明'} / 访问 {accessed}",
                ]
            )
        table(
            slide,
            [
                ["参观项目", "信息（完整规则见Word）"],
                *summary,
                ["建议停留", f"{poi.duration_min} 分钟（规划参考）"],
            ],
            6.85,
            1.6,
            [1.25, 4.55],
            13,
        )
        highlights = excerpts([b.text for b in poi.blocks], count=4, limit=64)
        for i, text in enumerate(highlights):
            box = add_text(slide, f"{i + 1:02}  {text}", 0.75, 5.15 + i * 0.4, 5.6, 0.4, 11)
            for paragraph in box.text_frame.paragraphs:
                paragraph.line_spacing = 1.05
                paragraph.space_after = Pt(0)
        references(
            slide,
            [r for b in poi.blocks for r in b.source_ids]
            + [r for f in poi.facts for r in f.source_ids],
        )
        slide.notes_slide.notes_text_frame.text += "\n完整参观规则\n" + "\n".join(
            "：".join(row) for row in rows
        )

    if guide:
        for categories, heading in [
            ({"budget"}, "预算"),
            ({"food"}, "餐饮安排"),
            ({"transport"}, "到达与市内交通"),
            ({"notice"}, "天气与注意事项"),
        ]:
            entries = [i for i in document.practical if i.category in categories]
            capacity = 6 if categories == {"budget"} else 4
            batch_size = (
                math.ceil(len(entries) / max(1, math.ceil(len(entries) / capacity)))
                if entries
                else 1
            )
            for start in range(0, len(entries), batch_size):
                batch = entries[start : start + batch_size]
                slide = page(
                    heading + (f" {start // batch_size + 1}" if len(entries) > batch_size else "")
                )
                rows = [["项目", "安排与参考", "依据"]]
                for item in batch:
                    selection = excerpts([item.value], count=1, limit=95)
                    value = selection[0] if selection else item.value
                    rows.append(
                        [
                            item.label,
                            value,
                            basis(document, item.source_ids, item.estimated, item.basis),
                        ]
                    )
                table(slide, rows, 0.65, 1.65, [2.6, 7.1, 2.2], 14)
                references(slide, [r for i in batch for r in i.source_ids])
        alternative_batch = math.ceil(len(document.alternatives) / max(1, math.ceil(len(document.alternatives) / 5))) or 1
        for start in range(0, len(document.alternatives), alternative_batch):
            slide = page("备选与未纳入景点")
            batch = document.alternatives[start : start + alternative_batch]
            table(
                slide,
                [
                    ["景点", "替换依据 / 未纳入原因", "替换时段 / 状态"],
                    *[
                        [i["name"], i["reason"], i.get("replace_day", "需确认后替换")]
                        for i in batch
                    ],
                ],
                0.65,
                1.65,
                [3.1, 5.3, 3.5],
                14,
            )
    else:
        for chapter in document.chapters:
            slide = page(chapter.title)
            lines = excerpts([b.text for b in chapter.blocks], count=3, limit=145)
            for i, text in enumerate(lines):
                rectangle(slide, 0.7, 1.65 + i * 1.6, 0.65, 0.65, primary)
                add_text(slide, f"{i + 1:02}", 0.82, 1.76 + i * 1.6, 0.45, 0.35, 18, "FFFFFF", True)
                add_text(slide, text, 1.75, 1.65 + i * 1.6, 10.7, 1.4, 20)
            references(slide, [r for b in chapter.blocks for r in b.source_ids])
        for start in range(0, len(document.alternatives), 5):
            slide = page("未纳入详细专题的候选地点")
            table(
                slide,
                [["地点", "未纳入原因"], *[
                    [item["name"], item["reason"]]
                    for item in document.alternatives[start:start + 5]
                ]],
                0.65, 1.65, [3.6, 8.3], 14,
            )

    reader_checks = checks(document)
    for start in range(0, len(reader_checks), 4):
        slide = page("出行前必核" + (f" {start // 4 + 1}" if len(reader_checks) > 4 else ""))
        for i, (text, url) in enumerate(reader_checks[start : start + 4]):
            y = 1.65 + i * 1.25
            add_text(slide, f"{start + i + 1:02}", 0.75, y, 0.6, 0.4, 22, primary, True)
            box = add_text(slide, text, 1.65, y, 10.6, 0.65, 17)
            targets = (
                document.reader_checks[start + i].targets
                if document.prd_version == "1.3" and start + i < len(document.reader_checks)
                else []
            )
            if targets:
                links = add_text(slide, "", 1.65, y + 0.7, 10.6, 0.4, 12, primary)
                paragraph = links.text_frame.paragraphs[0]
                for target in targets:
                    run = paragraph.add_run()
                    run.text = target["poi"] + " 官方核实链接  "
                    run.hyperlink.address = target["url"]
                    run.font.size = Pt(12)
            else:
                box.text_frame.paragraphs[0].runs[0].hyperlink.address = url
    source_batch_size = math.ceil(
        len(document.sources) / max(1, math.ceil(len(document.sources) / 10))
    )
    for start in range(0, len(document.sources), max(1, source_batch_size)):
        batch = document.sources[start : start + source_batch_size]
        slide = page("参考来源")
        column_rows = math.ceil(len(batch) / 2)
        for i, source in enumerate(batch):
            x, y = 0.7 + (i // column_rows) * 6.2, 1.6 + (i % column_rows) * 1.0
            box = add_text(
                slide, f"[{source.id}] {source.title[:45]}", x, y, 5.7, 0.62, 13, primary
            )
            box.text_frame.paragraphs[0].runs[0].hyperlink.address = str(source.url)
            add_text(
                slide,
                f"{source.level} · 发布 {source.published_at or '未载明'} / 访问 {source.fetched_at[:10]}",
                x,
                y + 0.65,
                5.7,
                0.28,
                10,
                MUTED,
            )
    ppt.save(path)
