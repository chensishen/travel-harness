from __future__ import annotations


def compare_styles(first: dict, second: dict) -> dict:
    dimensions = {
        "palette": first.get("theme") != second.get("theme"),
        "template": first.get("template") != second.get("template"),
        "animation": first.get("animation") != second.get("animation"),
        "layout_grammar": first.get("preset") != second.get("preset"),
        "motifs": first.get("class_name") != second.get("class_name"),
    }
    score = sum(dimensions.values())
    return {"score": score, "threshold": 4, "passed": score >= 4, "dimensions": dimensions}
