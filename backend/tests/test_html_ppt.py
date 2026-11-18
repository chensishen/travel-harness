from pathlib import Path

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.qa.diversity import compare_styles
from backend.qa.html_checks import inspect_html_deck
from backend.render.html_ppt import catalog, resolve_style


def test_html_ppt_catalog_and_prompt_resolution():
    data = catalog()
    assert len(data["themes"]) == 36
    assert len(data["templates"]) == 15
    assert resolve_style("做一个上海的文旅介绍")["preset"] == "shanghai-modern"
    assert resolve_style("官方风格的上海文旅介绍")["preset"] == "official-formal"


def test_html_ppt_diversity_and_programmatic_qa(tmp_path: Path):
    html = tmp_path / "travel.html"
    html.write_text(
        '<section class="slide"><h1>标题</h1></section>'
        '<section class="slide"><h2>正文</h2></section>',
        encoding="utf-8",
    )
    report = inspect_html_deck(html, {"theme": "magazine-bold", "template": "xhs-white-editorial"})
    assert report["passed"] is True
    assert compare_styles(
        {"preset": "shanghai-modern", "theme": "magazine-bold", "template": "xhs-white-editorial", "animation": "rise-in", "class_name": "tpl-xhs-white-editorial"},
        {"preset": "official-formal", "theme": "corporate-clean", "template": "weekly-report", "animation": "fade-up", "class_name": "tpl-weekly-report"},
    )["passed"] is True


def test_style_api_exposes_compile_and_compare():
    with TestClient(create_app()) as client:
        compiled = client.post("/api/styles/compile", json={"prompt": "官方风格的上海文旅介绍"})
        compared = client.post(
            "/api/styles/compare",
            json={"prompt_a": "上海文旅介绍", "prompt_b": "官方风格的上海文旅介绍"},
        )
    assert compiled.status_code == 200
    assert compiled.json()["style"]["preset"] == "official-formal"
    assert compared.status_code == 200
    assert compared.json()["difference"]["passed"] is True
