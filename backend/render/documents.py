"""Prepare shared, source-preserving content for two distinct reading formats."""

import hashlib
import re
from pathlib import Path

from PIL import Image

from backend.render.map_renderer import render_map
from backend.render.slides import render_slides
from backend.render.word import render_word

LABELS = {
    "ticket": "票价与优惠",
    "hours": "开放时间",
    "booking": "预约规则",
    "address": "地址",
    "history": "历史渊源",
    "culture": "文化价值",
    "historical_culture": "历史与文化",
    "history_culture": "历史与文化",
    "intro": "简介",
}


def image_info(images, folder):
    if not images or not images[0].local_path:
        return {"image": "", "caption": ""}
    image = images[0]
    path = Path(image.local_path)
    # Resize the embedded copy only; keep research originals and their metadata intact.
    assets = folder / "assets"
    assets.mkdir(exist_ok=True)
    name = hashlib.sha256(path.read_bytes()).hexdigest()[:16] + ".jpg"
    target = assets / name
    if not target.exists():
        with Image.open(path) as source:
            source.thumbnail((1600, 1200))
            rgb = Image.new("RGB", source.size, "white")
            if source.mode == "RGBA":
                rgb.paste(source, mask=source.getchannel("A"))
            else:
                rgb.paste(source.convert("RGB"))
            rgb.save(target, quality=86, optimize=True)
    return {
        "image": str(target),
        "caption": "AI 生成插画 · 非景点实拍"
        if image.generated
        else f"{image.author} · {image.license}",
        "image_credit": f"{image.author} · {image.license} · {image.source_page}",
    }


def sections(document, folder):
    if document.prd_version in {"1.1", "1.3"}:
        for day in document.days:
            day.map_png = render_map([day], folder / f"day-{day.day}.png", f"第 {day.day} 天")
        if len(document.days) > 1:
            overview_days = [d.model_copy(update={"basemap": document.overview_basemap}) for d in document.days]
            render_map(overview_days, folder / "overview.png", document.spec.city + "行程总览")
    if document.days:
        yield {
            "kind": "overview",
            "title": "行程总览",
            "body": [f"第 {d.day} 天：{d.theme}" for d in document.days],
            "image": render_map(
                document.days, folder / "overview.png", f"{document.spec.city} 行程总览"
            ),
            "caption": "路线示意图，无地理底图；公共交通为估算。",
            "sources": [],
        }
    for day in document.days:
        day.map_png = render_map([day], folder / f"day-{day.day}.png", f"第 {day.day} 天 路线")
        yield {
            "kind": "day",
            "title": f"第 {day.day} 天路线",
            "theme": day.theme,
            "body": [
                *[
                    f"{i + 1}. {p.name} · 建议停留 {p.duration_min} 分钟（估算）"
                    for i, p in enumerate(day.stops)
                ],
                *[
                    f"路段：{s.duration_min} 分钟 / {s.distance_m / 1000:.1f} 公里 · {s.source}"
                    for s in day.segments
                ],
                *day.meals,
                *day.notes,
            ],
            "stops": [f"{p.name}　{p.duration_min} 分钟" for p in day.stops],
            "image": day.map_png,
            "caption": "停留时长与公共交通时间为规划估算。",
            "sources": [],
        }
    events = {}
    for chapter in document.chapters:
        for block in chapter.blocks:
            if block.kind != "fact" or not block.source_ids:
                continue
            for sentence in re.findall(r"[^。！？\n]+[。！？]?", block.text):
                sentence = sentence.strip()
                match = re.match(r"^(?:公元)?(前)?\s*(\d{1,4})\s*年", sentence)
                if match and len(sentence) <= 85:
                    year = int(match[2]) * (-1 if match[1] else 1)
                    events.setdefault(
                        year,
                        {
                            "year": f"{'前' if year < 0 else ''}{abs(year)}年",
                            "text": sentence,
                            "sources": block.source_ids,
                        },
                    )
    if len(events) >= 3:
        ordered = [events[year] for year in sorted(events)]
        selected = (
            [ordered[round(i * (len(ordered) - 1) / 3)] for i in range(4)]
            if len(ordered) > 4
            else ordered
        )
        yield {
            "kind": "timeline",
            "title": "历史节点节选",
            "events": selected,
            "body": [event["text"] for event in selected],
            "sources": list(dict.fromkeys(s for event in selected for s in event["sources"])),
        }
    for chapter in document.chapters:
        yield {
            "kind": "chapter",
            "title": chapter.title,
            "period": chapter.period,
            "body": [("编辑建议：" if b.kind == "advice" else "") + b.text for b in chapter.blocks],
            "sources": list(dict.fromkeys(s for b in chapter.blocks for s in b.source_ids)),
            **image_info(chapter.images, folder),
        }
    for poi in document.pois:
        # Repeated unknown fields share one row, with their original date and source status.
        rows = {}
        for fact in poi.facts:
            key = (fact.value, str(fact.verified_at), tuple(fact.source_ids))
            rows.setdefault(key, [])
            label = LABELS.get(fact.field, fact.field)
            if label not in rows[key]:
                rows[key].append(label)
        yield {
            "kind": "poi",
            "title": poi.name,
            "body": [("编辑建议：" if b.kind == "advice" else "") + b.text for b in poi.blocks],
            "facts": [
                ["、".join(labels), value, f"{verified} / {', '.join(refs) or '未核实'}"]
                for (value, verified, refs), labels in rows.items()
            ],
            "highlights": [
                f.value
                for f in poi.facts
                if f.source_ids and f.field in {"history", "historical_culture", "culture", "intro"}
            ],
            "duration": poi.duration_min,
            "sources": list(
                dict.fromkeys(
                    [s for b in poi.blocks for s in b.source_ids]
                    + [s for f in poi.facts for s in f.source_ids]
                )
            ),
            **image_info(poi.images, folder),
        }


def render(document, folder: Path, primary="#176B5B", *, html_ppt=False, style=None):
    folder.mkdir(parents=True, exist_ok=True)
    primary = primary.lstrip("#")
    if len(primary) != 6 or any(c not in "0123456789abcdefABCDEF" for c in primary):
        raise ValueError("PRIMARY_COLOR 必须是六位十六进制颜色")
    content = list(sections(document, folder))
    render_slides(document, content, folder / "travel.pptx", primary)
    html_result = None
    if html_ppt:
        from backend.render.html_ppt import render_html_ppt

        html_result = render_html_ppt(document, content, folder, style)
    render_word(document, content, folder / "travel.docx", primary)
    (folder / "deliverable.json").write_text(document.model_dump_json(indent=2))
    result = {"pptx": "travel.pptx", "docx": "travel.docx"}
    if html_result:
        result.update({key: value for key, value in html_result.items() if key in {"html", "html_zip", "design", "qa"}})
        result["pptx"] = html_result["pptx"]
    if document.prd_version not in {"1.1", "1.3"}:
        result["json"] = "deliverable.json"
    return result
