from docx import Document
from pptx import Presentation

from backend.harness.schemas import Block, CultureChapter, Deliverable, SourceRef, TaskSpec, now
from backend.render.documents import render


def test_editorial_deck_keeps_full_material_without_mechanical_pagination(tmp_path):
    text = "园林以水池为中心组织建筑与游览空间。" * 100
    document = Deliverable(
        spec=TaskSpec(type="culture_intro", city="测试城市"),
        chapters=[CultureChapter(title="园林空间", blocks=[Block(text=text, source_ids=["S1"])])],
        sources=[
            SourceRef(
                id="S1",
                title="测试来源",
                url="https://test.gov.cn/garden",
                level="L1",
                fetched_at=now(),
            )
        ],
        warnings=[f"测试待核验事项 {i}" for i in range(40)],
    )
    render(document, tmp_path)
    deck = Presentation(tmp_path / "travel.pptx")
    assert len(deck.slides) <= 8, "研究正文和待核验清单不应机械切成数十张幻灯片"
    notes = "\n".join(s.notes_slide.notes_text_frame.text for s in deck.slides)
    assert text in notes and document.warnings[-1] in notes
    word = Document(tmp_path / "travel.docx")
    word_text = "\n".join(p.text for p in word.paragraphs)
    assert text in word_text.replace("\n", "")
    assert document.warnings[-1] in word_text
    assert "https://test.gov.cn/garden" in word_text


def test_slide_selection_preserves_conditions_and_advice_labels():
    from backend.render.slides import excerpts

    long_sentence = "门票免费，" + "活动规则以官方公布的说明为准，" * 10 + "但须提前预约。"
    selected = excerpts([long_sentence, "编辑建议：可沿池边观察借景。"], count=2, limit=60)
    assert selected == ["编辑建议：可沿池边观察借景。"]


def test_illustrated_outputs_stay_editable_and_keep_original_images(tmp_path):
    from PIL import Image

    from backend.harness.schemas import POI, ImageRef

    image_path = tmp_path / "original.png"
    Image.new("RGB", (2400, 1600), "#769c87").save(image_path)
    image = ImageRef(
        url="https://example.com/image",
        author="测试作者",
        license="AI 生成插画 · 非景点实拍",
        source_page="https://example.com/",
        local_path=str(image_path),
        generated=True,
    )
    document = Deliverable(
        spec=TaskSpec(type="culture_intro", city="测试城市"),
        pois=[POI(id="P1", name="测试园林", images=[image])],
    )
    render(document, tmp_path / "output")
    with Image.open(image_path) as original:
        assert original.size == (2400, 1600)
    deck = Presentation(tmp_path / "output/travel.pptx")
    visible = "\n".join(
        shape.text for slide in deck.slides for shape in slide.shapes if shape.has_text_frame
    )
    assert "测试园林" in visible and "AI 生成插画 · 非景点实拍" in visible
    assert "规划估算" in visible
    for slide in deck.slides:
        for shape in slide.shapes:
            assert shape.left >= 0 and shape.top >= 0
            assert shape.left + shape.width <= deck.slide_width + 100
            assert shape.top + shape.height <= deck.slide_height + 100
    word = Document(tmp_path / "output/travel.docx")
    assert word.inline_shapes
    assert "pBdr" not in word.styles["Title"]._element.xml


def test_timeline_uses_sourced_fact_dates_in_chronological_order(tmp_path):
    document = Deliverable(
        spec=TaskSpec(type="culture_intro", city="测试城市"),
        chapters=[
            CultureChapter(
                title="历史",
                blocks=[
                    Block(
                        text="1997年，园林列入遗产名录。前514年，开始筑城。825年，修筑街道。",
                        source_ids=["S1"],
                    ),
                    Block(
                        text="编辑建议：2000年风格可以作为配色参考。", source_ids=[], kind="advice"
                    ),
                ],
            )
        ],
    )
    render(document, tmp_path)
    deck = Presentation(tmp_path / "travel.pptx")
    timeline = next(
        slide
        for slide in deck.slides
        if any(shape.has_text_frame and shape.text == "历史节点节选" for shape in slide.shapes)
    )
    text = "\n".join(shape.text for shape in timeline.shapes if shape.has_text_frame)
    assert text.index("前514年") < text.index("825年") < text.index("1997年")
    assert "2000年" not in text
    assert "S1" in timeline.notes_slide.notes_text_frame.text


def test_preview_rejects_stale_pdf_after_unsuccessful_conversion(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest

    from backend.render import preview

    (tmp_path / "pdf").mkdir()
    (tmp_path / "pdf/travel.pdf").write_bytes(b"old PDF")
    monkeypatch.setattr(preview, "available", lambda: True)
    monkeypatch.setattr(preview.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    with pytest.raises(ValueError, match="pptx"):
        preview.render_previews(tmp_path)
