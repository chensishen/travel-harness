"""HTML PPT Studio renderer.

The project keeps the research and content model unchanged. This adapter maps
that model to the local html-ppt skill, renders the HTML with its own runtime,
and packages the rendered pages as a PowerPoint deck.
"""

from __future__ import annotations

import html
import json
import shutil
import subprocess
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pptx import Presentation
from pptx.util import Inches

SKILL_ROOT = Path(__file__).resolve().parents[2] / "third_party" / "html-ppt-skill"
THEMES = sorted(path.stem for path in (SKILL_ROOT / "assets" / "themes").glob("*.css"))
TEMPLATES = sorted(path.name for path in (SKILL_ROOT / "templates" / "full-decks").iterdir() if path.is_dir())


STYLE_PRESETS = {
    "shanghai-modern": {
        "label": "海派摩登",
        "theme": "magazine-bold",
        "template": "xhs-white-editorial",
        "animation": "rise-in",
        "class_name": "tpl-xhs-white-editorial",
        "description": "暖色杂志感、非对称图文与大字号章节页",
    },
    "official-formal": {
        "label": "官方庄重",
        "theme": "corporate-clean",
        "template": "weekly-report",
        "animation": "fade-up",
        "class_name": "tpl-weekly-report",
        "description": "克制、对称、条目化信息层级",
    },
    "modern-minimal": {
        "label": "现代简约",
        "theme": "swiss-grid",
        "template": "dir-key-nav-minimal",
        "animation": "fade-up",
        "class_name": "tpl-dir-key-nav-minimal",
        "description": "网格、留白与清晰信息层级",
    },
    "magazine-editorial": {
        "label": "杂志编辑",
        "theme": "editorial-serif",
        "template": "xhs-white-editorial",
        "animation": "fade-left",
        "class_name": "tpl-xhs-white-editorial",
        "description": "衬线标题、图文交错与章节叙事",
    },
    "tech-dark": {
        "label": "科技深色",
        "theme": "tokyo-night",
        "template": "graphify-dark-graph",
        "animation": "glitch-in",
        "class_name": "tpl-graphify-dark-graph",
        "description": "深色背景、数据卡片与动态入口",
    },
    "corporate": {
        "label": "商务汇报",
        "theme": "corporate-clean",
        "template": "pitch-deck",
        "animation": "fade-up",
        "class_name": "tpl-pitch-deck",
        "description": "适合正式汇报与结构化结论",
    },
}


def catalog() -> dict:
    return {
        "presets": [{"id": key, **value} for key, value in STYLE_PRESETS.items()],
        "themes": THEMES,
        "templates": TEMPLATES,
        "animations": [
            "fade-up",
            "fade-down",
            "fade-left",
            "fade-right",
            "rise-in",
            "zoom-pop",
            "blur-in",
            "glitch-in",
            "typewriter",
            "neon-glow",
            "shimmer-sweep",
            "stagger-list",
        ],
    }


def resolve_style(prompt: str, style: dict | None = None) -> dict:
    """Resolve explicit style settings first, then use stable prompt heuristics."""
    style = style or {}
    preset = style.get("preset")
    lowered = (prompt + " " + str(style.get("prompt", ""))).casefold()
    if not preset:
        if any(word in lowered for word in ("官方", "政务", "庄重", "政府", "汇报")):
            preset = "official-formal"
        elif any(word in lowered for word in ("上海", "上海风", "海派", "外滩", "石库门")):
            preset = "shanghai-modern"
        elif any(word in lowered for word in ("科技", "技术", "ai", "数据")):
            preset = "tech-dark"
        elif any(word in lowered for word in ("杂志", "编辑", "时尚")):
            preset = "magazine-editorial"
        else:
            preset = "modern-minimal"
    if preset not in STYLE_PRESETS:
        raise ValueError(f"未知 PPT 风格预设：{preset}")
    result = {**STYLE_PRESETS[preset], "preset": preset}
    for key in ("theme", "template", "animation"):
        if style.get(key):
            result[key] = style[key]
    if result["theme"] not in THEMES:
        raise ValueError(f"未知 html-ppt 主题：{result['theme']}")
    if result["template"] not in TEMPLATES:
        raise ValueError(f"未知 html-ppt 模板：{result['template']}")
    if style.get("prompt"):
        result["prompt"] = str(style["prompt"])[:500]
    if style.get("template_id"):
        result["template_id"] = str(style["template_id"])
    return result


def _esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def _copy_runtime(target: Path, style: dict) -> None:
    assets = target / "html-ppt" / "assets"
    themes = assets / "themes"
    animations = assets / "animations"
    themes.mkdir(parents=True, exist_ok=True)
    animations.mkdir(parents=True, exist_ok=True)
    for name in ("base.css", "fonts.css", "runtime.js"):
        shutil.copy2(SKILL_ROOT / "assets" / name, assets / name)
    shutil.copy2(SKILL_ROOT / "assets" / "animations" / "animations.css", animations / "animations.css")
    shutil.copy2(SKILL_ROOT / "assets" / "themes" / f"{style['theme']}.css", themes / f"{style['theme']}.css")
    template_css = SKILL_ROOT / "templates" / "full-decks" / style["template"] / "style.css"
    shutil.copy2(template_css, target / "html-ppt" / "template.css")


def _image_path(source: str, target: Path, index: int) -> str:
    if not source:
        return ""
    source_path = Path(source)
    if not source_path.is_file():
        return ""
    image_dir = target / "html-ppt" / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    name = f"image-{index}{source_path.suffix.lower() or '.jpg'}"
    shutil.copy2(source_path, image_dir / name)
    return f"html-ppt/images/{name}"


def _notes(item: dict) -> str:
    parts = [item.get("title", ""), *item.get("body", [])]
    parts += ["：".join(row) for row in item.get("facts", [])]
    if item.get("sources"):
        parts.append("来源：" + ", ".join(item["sources"]))
    return "\n".join(_esc(part) for part in parts if part)


def _image_markup(path: str, alt: str) -> str:
    if not path:
        return '<div class="image-placeholder"><span>文化阅读</span></div>'
    return f'<figure class="img-frame"><img src="{_esc(path)}" alt="{_esc(alt)}"></figure>'


def _slide(title: str, body: str, *, index: int, total: int, animation: str, notes: str = "") -> str:
    return f'''<section class="slide" data-title="{_esc(title)}">
      {body}
      <div class="deck-footer"><span class="dim2">{_esc(title)}</span><span class="slide-number" data-current="{index}" data-total="{total}"></span></div>
      <div class="notes">{notes}</div>
    </section>'''


def _build_html(document, content: list[dict], target: Path, style: dict) -> Path:
    total_hint = 3 + len([item for item in content if item["kind"] in {"overview", "day", "chapter", "poi", "timeline"}])
    slides: list[str] = []
    city = _esc(document.spec.city)
    kind_label = "旅游攻略" if document.spec.type == "travel_guide" else "文旅介绍"
    hero = next((item for item in content if item.get("image")), None)
    hero_path = _image_path(hero.get("image", "") if hero else "", target, 0)
    cls = style["class_name"]
    official = style["preset"] == "official-formal"

    cover_body = (f'''<div class="cover-head"><span class="logo">{_esc(kind_label)}</span><span class="week-chip">{_esc(style['label'])}</span></div>
      <div class="center tc" style="margin-top:120px"><p class="kicker">{_esc(kind_label)} · {_esc(style.get('prompt', ''))}</p>
        <h1 class="h1 anim-{style['animation']}" data-anim="{style['animation']}">{city}<br>{_esc(kind_label)}</h1>
        <p class="lede" style="margin:28px auto">基于来源的城市文化阅读与行程设计</p>
      </div><div style="margin:80px auto 0;max-width:760px">{_image_markup(hero_path, f'{city}主视觉')}</div>'''
        if official else f'''<div class="{cls}-cover-bg cover-bg"></div>
      <div class="deck-header"><span class="eyebrow">{_esc(style['label'])}</span><span class="eyebrow">HTML PPT Studio</span></div>
      <div class="grid g2" style="align-items:center;height:78%">
        <div class="anim-stagger-list"><p class="kicker">{_esc(kind_label)} · {_esc(style.get('prompt', ''))}</p>
          <h1 class="h1 anim-{style['animation']}" data-anim="{style['animation']}">{city}<br><span class="gradient-text">{_esc(kind_label)}</span></h1>
          <p class="lede">基于来源的城市文化阅读与行程设计</p>
          <div class="row wrap mt-l"><span class="pill pill-accent">{_esc(style['label'])}</span><span class="pill">{_esc(style['template'])}</span><span class="pill">{_esc(style['theme'])}</span></div>
        </div>
        <div>{_image_markup(hero_path, f'{city}主视觉')}</div>
      </div>''')
    slides.append(_slide(f"{document.spec.city}{kind_label}", cover_body, index=1, total=total_hint, animation=style["animation"], notes=f"{city}{kind_label}，使用 html-ppt 主题 {style['theme']} 和模板 {style['template']}。"))

    topics = [item["title"] for item in content if item["kind"] in {"chapter", "overview", "day", "timeline"}]
    cards = "".join(f'<div class="card"><h4>{i:02d} · {_esc(topic)}</h4><p class="dim">按页面意图选择不同布局，不重复使用同一版式。</p></div>' for i, topic in enumerate(topics[:6], 1))
    agenda_body = f'<p class="kicker">阅读路径</p><h2 class="h2">从城市背景到具体体验</h2><div class="grid g3 mt-l anim-stagger-list" data-anim-target>{cards or "<div class=\"card\"><h4>内容待补充</h4></div>"}</div>'
    slides.append(_slide("阅读导览", agenda_body, index=2, total=total_hint, animation=style["animation"], notes="目录页使用卡片网格布局。"))

    slide_index = 3
    for item in content:
        kind = item["kind"]
        if kind in {"overview", "day"}:
            lines = item.get("body", [])[:8]
            list_markup = "".join(f"<li>{_esc(line)}</li>" for line in lines)
            img = _image_path(item.get("image", ""), target, slide_index)
            body = (f'<p class="kicker">路线 / 结构</p><h2 class="h2">{_esc(item["title"])}</h2><div class="next-row mt-l"><span class="owner">条目</span><div class="task"><ol>{list_markup}</ol></div></div>'
                    if official else f'<p class="kicker">路线 / 结构</p><h2 class="h2">{_esc(item["title"])}</h2><div class="grid g2 mt-l" style="align-items:start"><div class="card"><ul class="mt-m">{list_markup}</ul></div><div>{_image_markup(img, item["title"])}</div></div>')
        elif kind == "timeline":
            events = item.get("events", [])[:5]
            event_markup = "".join(f'<div class="card"><h4>{_esc(event.get("year"))}</h4><p class="dim">{_esc(event.get("text"))}</p></div>' for event in events)
            official_events = "".join(f'<li>{_esc(event.get("text"))}</li>' for event in events)
            body = (f'<p class="kicker">历史脉络</p><h2 class="h2">{_esc(item["title"])}</h2><div class="ship-item mt-l"><span class="tag feat">时间</span><div><ol>{official_events}</ol></div></div>'
                    if official else f'<p class="kicker">历史脉络</p><h2 class="h2">{_esc(item["title"])}</h2><div class="grid g{min(3, max(1, len(events)))} mt-l anim-stagger-list" data-anim-target>{event_markup}</div>')
        elif kind == "chapter":
            img = _image_path(item.get("image", ""), target, slide_index)
            lines = item.get("body", [])[:4]
            text_markup = "".join(f'<p class="lede">{_esc(line)}</p>' for line in lines)
            body = (f'<p class="kicker">文化主题</p><h2 class="h2">{_esc(item["title"])}</h2><div class="card mt-l"><div class="ship-item"><span class="tag feat">正文</span><div>{text_markup or "<p class=\"lede\">文化主题完整资料见备注。</p>"}</div></div></div><div style="margin-top:18px;max-width:900px">{_image_markup(img, item["title"])}</div>'
                    if official else f'<p class="kicker">文化主题</p><h2 class="h2">{_esc(item["title"])}</h2><div class="grid g2 mt-l" style="align-items:center"><div>{text_markup or "<p class=\"lede\">文化主题完整资料见备注。</p>"}</div><div>{_image_markup(img, item["title"])}</div></div>')
        elif kind == "poi":
            img = _image_path(item.get("image", ""), target, slide_index)
            fact_lines = [f"{row[0]}：{row[1]}" for row in item.get("facts", [])]
            selected = (item.get("body", []) + item.get("highlights", []) + fact_lines)[:5]
            text_markup = "".join(f'<li>{_esc(line)}</li>' for line in selected)
            body = f'<p class="kicker">目的地 / 现场阅读</p><h2 class="h2">{_esc(item["title"])}</h2><div class="grid g2 mt-l"><div>{_image_markup(img, item["title"])}</div><div class="card"><ul>{text_markup or "<li>建议结合现场展陈与官方信息阅读。</li>"}</ul></div></div>'
        else:
            continue
        slides.append(_slide(item["title"], body, index=slide_index, total=total_hint, animation=style["animation"], notes=_notes(item)))
        slide_index += 1

    closing_body = f'<div class="center tc"><p class="kicker">Closing</p><h1 class="h1 anim-rise-in" data-anim="rise-in">带着问题，重新认识{city}</h1><p class="lede" style="margin:20px auto">完整来源、事实与备注已保留在交付文件中。</p></div>'
    slides.append(_slide("结束", closing_body, index=slide_index, total=slide_index, animation="rise-in", notes="结束页。"))

    html_path = target / "travel.html"
    html_path.write_text(
        "<!DOCTYPE html><html lang=\"zh-CN\" data-theme=\"" + _esc(style["theme"]) + "\"><head>"
        "<meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{city} · {kind_label}</title>"
        "<link rel=\"stylesheet\" href=\"html-ppt/assets/fonts.css\"><link rel=\"stylesheet\" href=\"html-ppt/assets/base.css\">"
        f"<link rel=\"stylesheet\" href=\"html-ppt/assets/themes/{_esc(style['theme'])}.css\"><link rel=\"stylesheet\" href=\"html-ppt/template.css\"><link rel=\"stylesheet\" href=\"html-ppt/assets/animations/animations.css\">"
        "</head><body class=\"" + _esc(cls) + f'\" data-themes=\"{_esc(style["theme"])}\" data-theme-base=\"html-ppt/assets/themes/\"><div class=\"deck\">' + "".join(slides)
        + "</div><script src=\"html-ppt/assets/runtime.js\"></script></body></html>",
        encoding="utf-8",
    )
    return html_path


def _render_pngs(html_path: Path, target: Path) -> list[Path]:
    png_dir = target / "html-ppt-png"
    if png_dir.exists():
        shutil.rmtree(png_dir)
    png_dir.mkdir(parents=True)
    slide_count = html_path.read_text(encoding="utf-8").count('class="slide"')
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        raise ValueError("未找到 Google Chrome，无法执行 html-ppt 页面渲染")

    def render_one(index: int) -> Path:
        output = png_dir / f"travel_{index:02d}.png"
        command = [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-sandbox",
            "--virtual-time-budget=500",
            "--window-size=1920,1080",
            f"--screenshot={output}",
            f"file://{html_path.resolve()}#/{index}",
        ]
        last_error = ""
        for _ in range(2):
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=90, check=False)
            except subprocess.TimeoutExpired as error:
                last_error = f"超时 {error.timeout}s"
                continue
            if completed.returncode == 0 and output.is_file() and output.stat().st_size:
                return output
            last_error = (completed.stderr or completed.stdout)[-300:]
        raise ValueError(f"html-ppt 第 {index} 页渲染失败：{last_error}")

    failures = []
    with ThreadPoolExecutor(max_workers=min(2, max(1, slide_count))) as pool:
        futures = {pool.submit(render_one, index): index for index in range(1, slide_count + 1)}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as error:
                failures.append(str(error))
    pngs = sorted(png_dir.glob("*.png"))
    if failures or len(pngs) != slide_count:
        detail = "; ".join(failures[:3]) or f"期望 {slide_count} 页，实际 {len(pngs)} 页"
        raise ValueError("html-ppt Chrome 渲染失败：" + detail)
    return pngs


def _pptx_from_pngs(pngs: list[Path], target: Path) -> Path:
    ppt = Presentation()
    ppt.slide_width, ppt.slide_height = Inches(13.333), Inches(7.5)
    ppt.core_properties.title = "文旅介绍"
    ppt.core_properties.subject = "基于来源的城市文化介绍"
    ppt.core_properties.creator = "知行文旅文档助手"
    ppt.core_properties.description = "由 html-ppt-skill 渲染生成"
    blank = ppt.slide_layouts[6]
    for path in pngs:
        slide = ppt.slides.add_slide(blank)
        slide.shapes.add_picture(str(path), 0, 0, width=ppt.slide_width, height=ppt.slide_height)
    output = target / "travel.pptx"
    ppt.save(output)
    return output


def render_html_ppt(document, content: list[dict], target: Path, style: dict | None = None) -> dict:
    from backend.qa.html_checks import inspect_html_deck
    from backend.style.compiler import compile_style

    resolved, style_spec = compile_style(document.spec.city + " " + document.spec.type, style)
    _copy_runtime(target, resolved)
    html_path = _build_html(document, content, target, resolved)
    qa_report = inspect_html_deck(html_path, resolved)
    if not qa_report["passed"]:
        raise ValueError("HTML deck 程序化检查失败：" + str(qa_report))
    (target / "html-qa.json").write_text(json.dumps(qa_report, ensure_ascii=False, indent=2), encoding="utf-8")
    (target / "DESIGN.md").write_text(style_spec.to_design_md(), encoding="utf-8")
    pngs = _render_pngs(html_path, target)
    pptx_path = _pptx_from_pngs(pngs, target)
    zip_path = target / "travel-html.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in [html_path, target / "DESIGN.md", target / "html-qa.json", *((target / "html-ppt").rglob("*")), *pngs]:
            if path.is_file():
                archive.write(path, path.relative_to(target))
    (target / "html-ppt-style.json").write_text(json.dumps(resolved, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "pptx": pptx_path.name,
        "html": html_path.name,
        "html_zip": zip_path.name,
        "design": "DESIGN.md",
        "qa": "html-qa.json",
        "style": resolved,
        "pngs": [path.name for path in pngs],
    }
