from __future__ import annotations

from backend.render.html_ppt import STYLE_PRESETS, resolve_style
from backend.style.spec import Grid, Palette, StyleSpec, Typography


def compile_style(prompt: str, style: dict | None = None) -> tuple[dict, StyleSpec]:
    style = style or {}
    resolved = resolve_style(prompt, {**style, "prompt": style.get("prompt") or prompt})
    preset = resolved["preset"]
    if preset == "shanghai-modern":
        spec = StyleSpec(
            id="haipai-deco",
            name="海派摩登",
            preset=preset,
            intensity=0.8,
            scenes=["文旅介绍", "城市宣传"],
            palette=Palette(bg="#F3EBDD", text="#1F2A2E", primary="#12403C", accent="#C9A24B", line="#8A6D2F"),
            typography=Typography(title=["Cambria", "Noto Serif SC"], body=["Arial", "Noto Sans SC"]),
            grid=Grid(align="asymmetric", margin=96, whitespace=(0.25, 0.45)),
            layouts=["asymmetric-cover", "image-text-split", "gallery", "timeline", "quote"],
            motifs=["stepped-lines", "fan-rays", "thin-gold-frame", "shikumen-arch"],
            imagery={"crop": "arched-or-cut-corner", "filter": "warm"},
            tone={"style": "short, evocative", "fact_preservation": True},
            rules={"must": ["标题使用细线或金色强调", "图片与文字交错"], "must_not": ["大面积霓虹", "拥挤排版"]},
        )
    elif preset == "official-formal":
        spec = StyleSpec(
            id="official-formal",
            name="官方庄重",
            preset=preset,
            intensity=0.7,
            scenes=["政务汇报", "官方文旅介绍"],
            palette=Palette(bg="#FAFBFC", text="#161E37", primary="#2E63EB", accent="#B42318", line="#D0D5DD"),
            typography=Typography(title=["Cambria", "Noto Serif SC"], body=["Arial", "Noto Sans SC"]),
            grid=Grid(align="symmetric", margin=88, whitespace=(0.2, 0.35)),
            layouts=["formal-cover", "structured-overview", "numbered-list", "timeline", "source-footnote"],
            motifs=["red-divider", "plain-header", "page-number"],
            imagery={"crop": "square", "filter": "none"},
            tone={"style": "formal, itemized", "fact_preservation": True},
            rules={"must": ["来源与数据可回查", "标题和条目层级清楚"], "must_not": ["渐变", "倾斜装饰", "花哨动画"]},
        )
    else:
        spec = StyleSpec(
            id=preset,
            name=STYLE_PRESETS[preset]["label"],
            preset=preset,
            scenes=["文旅介绍", "城市宣传"],
            layouts=["cover", "overview", "two-column", "gallery", "closing"],
        )
    return resolved, spec
