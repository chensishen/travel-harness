from __future__ import annotations

from pathlib import Path

from pptx import Presentation


def analyze_template(path: Path) -> dict:
    presentation = Presentation(path)
    slides = []
    for index, slide in enumerate(presentation.slides, start=1):
        text_slots = []
        image_slots = 0
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                text = " ".join(p.text.strip() for p in shape.text_frame.paragraphs).strip()
                text_slots.append({"shape_id": shape.shape_id, "max_chars": max(40, len(text) or 80), "sample": text[:80]})
            if getattr(shape, "shape_type", None) == 13:
                image_slots += 1
        slides.append({"slide_id": index, "role": "content" if index > 1 else "cover", "slots": text_slots, "image_slots": image_slots})
    return {
        "filename": path.name,
        "slide_count": len(presentation.slides),
        "slide_width_emu": presentation.slide_width,
        "slide_height_emu": presentation.slide_height,
        "slides": slides,
        "editable": True,
        "note": "已提取页面槽位。复杂母版和动画保留在原模板中，自动填充需要进一步匹配槽位。",
    }
