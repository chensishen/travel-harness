from __future__ import annotations

import re
from pathlib import Path


def inspect_html_deck(html_path: Path, style: dict) -> dict:
    text = html_path.read_text(encoding="utf-8")
    slide_count = len(re.findall(r'<section[^>]+class="slide(?:\s|"|>)', text))
    missing_images = []
    for source in re.findall(r'<img[^>]+src="([^"]+)"', text):
        image = (html_path.parent / source).resolve()
        if not image.is_file():
            missing_images.append(source)
    forbidden = [token for token in ("lorem", "ipsum", "TODO", "[insert") if token.casefold() in text.casefold()]
    titles = re.findall(r'<(?:h1|h2)[^>]*>(.*?)</(?:h1|h2)>', text, flags=re.S)
    report = {
        "passed": slide_count > 0 and not missing_images and not forbidden,
        "slide_count": slide_count,
        "missing_images": missing_images,
        "forbidden_placeholders": forbidden,
        "title_count": len(titles),
        "theme": style.get("theme"),
        "template": style.get("template"),
        "checks": {
            "single_slide_root": text.count('class="slide"') == slide_count,
            "assets_exist": not missing_images,
            "no_placeholders": not forbidden,
        },
    }
    return report
