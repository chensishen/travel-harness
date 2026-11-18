from types import SimpleNamespace

import httpx
import pytest

from backend.app.config import Settings
from backend.app.store import Store
from backend.harness.graph import Harness
from backend.harness.reviewer import validate_facts
from backend.harness.schemas import POI, Block, Deliverable, SourceRef, TaskSpec, now
from backend.tools.geo import Geo
from backend.tools.images import generated_image


async def test_geocode_uses_grounded_alias_and_excludes_bus_stop(tmp_path, monkeypatch):
    source = SourceRef(
        id="S1",
        title="场馆",
        url="https://museum.example",
        level="L3",
        fetched_at=now(),
        text="中共一大会址纪念馆现称中共一大纪念馆。",
    )

    async def aliases(instruction, data, schema):
        return schema.model_validate(
            {"aliases": [{"name": "中共一大纪念馆", "source_id": "S1", "evidence": source.text}]}
        )

    calls = []

    async def get(client, url, **kwargs):
        query = kwargs["params"]["q"]
        calls.append(query)
        values = (
            []
            if "会址纪念馆" in query
            else [
                {
                    "lat": "31.2",
                    "lon": "121.4",
                    "name": "中共一大纪念馆",
                    "class": "highway",
                    "type": "bus_stop",
                    "address": {"city": "上海市"},
                },
                {
                    "lat": "31.22",
                    "lon": "121.47",
                    "name": "中共一大纪念馆",
                    "class": "tourism",
                    "type": "museum",
                    "address": {"city": "上海市"},
                    "osm_type": "way",
                    "osm_id": 123,
                },
            ]
        )
        return httpx.Response(200, json=values, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    geo = Geo(Settings(_env_file=None), Store(tmp_path), SimpleNamespace(json=aliases))
    geo.last = -100
    poi = POI(id="P1", name="中共一大会址纪念馆")
    await geo.locate("上海", poi, [source])
    assert poi.lat == 31.22 and poi.lng == 121.47
    assert "中共一大纪念馆" in poi.location_note
    assert poi.location_source.endswith("/way/123")
    assert len(calls) == 2


def test_advice_is_labeled_and_factual_sources_still_required():
    doc = Deliverable(
        spec=TaskSpec(type="culture_intro", city="苏州"),
        pois=[
            POI(
                id="P1",
                name="园林",
                blocks=[Block(text="建议观察园林借景。", source_ids=[], kind="advice")],
            )
        ],
    )
    validate_facts(doc)
    doc.pois[0].blocks[0].kind = "fact"
    with pytest.raises(ValueError, match="来源"):
        validate_facts(doc)


async def test_reviewer_repairs_only_flagged_block(tmp_path):
    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    doc = Deliverable(
        spec=TaskSpec(type="travel_guide", city="苏州"),
        sources=[
            SourceRef(
                id="S1",
                title="园林",
                url="https://test.gov.cn",
                level="L1",
                fetched_at=now(),
                text="苏州园林具有文化价值。",
            ),
            SourceRef(
                id="S2", title="无关材料", url="https://test.gov.cn/other",
                level="L1", fetched_at=now(), text="未被正文引用。",
            ),
        ],
        pois=[
            POI(
                id="P1",
                name="园林",
                blocks=[
                    Block(text="苏州园林具有文化价值。", source_ids=["S1"]),
                    Block(text="官方认定建城史逾5000年。", source_ids=["S1"]),
                ],
            )
        ],
    )
    seen = []

    async def model(instruction, data, schema):
        seen.append(data)
        assert [source["id"] for source in data["sources"]] == ["S1"]
        if "repairs" in schema.model_fields:
            return schema.model_validate(
                {
                    "repairs": [
                        {
                            "block_id": "poi:P1:1",
                            "block": {
                                "text": "建议慢步欣赏园林。",
                                "kind": "advice",
                                "source_ids": [],
                            },
                        }
                    ]
                }
            )
        return schema.model_validate(
            {
                "unsupported": []
                if len(seen) > 1
                else [{"block_id": "poi:P1:1", "reason": "来源不支持5000年"}],
                "warnings": [],
            }
        )

    harness.models.json = model

    async def whole_rewrite(*args):
        raise AssertionError("must not rewrite whole document")

    harness.writer = whole_rewrite
    state = {
        "document": doc.model_dump(mode="json"),
        "sources": [s.model_dump(mode="json") for s in doc.sources],
        "warnings": [],
        "outline": {},
    }
    await harness.reviewer({"id": "review-test"}, state, tmp_path)
    result = Deliverable.model_validate(state["document"])
    assert result.pois[0].blocks[0] == doc.pois[0].blocks[0]
    assert result.pois[0].blocks[1].kind == "advice"
    assert len(seen) == 3


async def test_image_mcp_waits_existing_job_and_marks_generated(tmp_path):
    from contextlib import asynccontextmanager

    from PIL import Image

    output = None
    calls = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name=name, inputSchema={})
                    for name in ["generate_image", "generation_result"]
                ]
            )

        async def call_tool(self, name, args):
            nonlocal output
            calls.append((name, args))
            if name == "generate_image":
                output = __import__("pathlib").Path(args["path"])
                text = (
                    'The image is still being made. call generation_result with id "generation-123"'
                )
            else:
                Image.new("RGB", (30, 20), "green").save(output)
                text = '{"path":"' + str(output) + '"}'
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text=text)])

    class MCP:
        config = Settings(_env_file=None)

        @asynccontextmanager
        async def session(self, server):
            assert server == "magpie-image"
            yield Session()

    image = await generated_image(MCP(), "苏州", "园林", tmp_path, "generated")
    assert [c[0] for c in calls] == ["generate_image", "generation_result"]
    assert image.generated and "AI" in image.license
    assert image.local_path == str(output)


@pytest.mark.parametrize(
    "case,found", [("cafe", True), ("street", True), ("disconnected", False), ("other_city", False)]
)
async def test_geocode_disambiguates_landmarks_and_contiguous_road(
    tmp_path, monkeypatch, case, found
):
    name = "田子坊" if case == "cafe" else "武康路"
    base = {
        "name": name,
        "lat": "31.2",
        "lon": "121.4",
        "class": "highway",
        "type": "residential",
        "address": {"city": "上海市"},
        "boundingbox": ["31.20", "31.21", "121.4", "121.41"],
    }
    second = {**base, "lat": "31.215", "boundingbox": ["31.21", "31.22", "121.4", "121.41"]}
    values = [base, second]
    if case == "cafe":
        values = [
            {**base, "class": "tourism", "type": "attraction"},
            {**second, "class": "amenity", "type": "cafe"},
        ]
    if case == "disconnected":
        values[1] = {**second, "boundingbox": ["32.0", "32.1", "121.4", "121.41"]}
    if case == "other_city":
        values = [{**base, "address": {"city": "苏州市"}}]

    async def get(client, url, **kwargs):
        return httpx.Response(200, json=values, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    result = await Geo(Settings(_env_file=None), Store(tmp_path)).search("上海", name)
    assert (result is not None) is found
    if case == "cafe":
        assert result["class"] == "tourism"


async def test_image_model_error_is_explicit_and_not_retried(tmp_path):
    from contextlib import asynccontextmanager

    from backend.tools.mcp_search import MCPToolError

    calls = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(name=n, inputSchema={})
                    for n in ["generate_image", "generation_result"]
                ]
            )

        async def call_tool(self, name, args):
            calls.append(name)
            return SimpleNamespace(
                isError=True,
                content=[
                    SimpleNamespace(
                        type="text",
                        text='no image: magpie knows no model "codex/gpt-image-2" to draw with',
                    )
                ],
            )

    class MCP:
        config = Settings(_env_file=None)

        @asynccontextmanager
        async def session(self, server):
            yield Session()

    with pytest.raises(MCPToolError, match="生图模型未配置"):
        await generated_image(MCP(), "苏州", "园林", tmp_path, "failed")
    assert calls == ["generate_image"]


async def test_persistently_unsupported_paragraph_is_omitted_then_reaudited(tmp_path):
    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    doc = Deliverable(
        spec=TaskSpec(type="travel_guide", city="苏州"),
        sources=[
            SourceRef(
                id="S1",
                title="来源",
                url="https://test.gov.cn",
                level="L1",
                fetched_at=now(),
                text="园林。",
            )
        ],
        pois=[
            POI(
                id="P1",
                name="园林",
                blocks=[
                    Block(text="园林。", source_ids=["S1"]),
                    Block(text="没有依据的说法。", source_ids=["S1"]),
                ],
            )
        ],
    )
    audits = 0

    async def model(instruction, data, schema):
        nonlocal audits
        if "repairs" in schema.model_fields:
            return schema.model_validate(
                {
                    "repairs": [
                        {
                            "block_id": "poi:P1:1",
                            "block": {"text": "仍无依据的说法。", "source_ids": ["S1"]},
                        }
                    ]
                }
            )
        audits += 1
        return schema.model_validate(
            {
                "unsupported": []
                if audits == 4
                else [{"block_id": "poi:P1:1", "reason": "缺少来源"}],
                "warnings": [],
            }
        )

    harness.models.json = model
    state = {
        "document": doc.model_dump(mode="json"),
        "sources": [s.model_dump(mode="json") for s in doc.sources],
        "warnings": [],
    }
    await harness.reviewer({"id": "review-test"}, state, tmp_path)
    assert len(state["document"]["pois"][0]["blocks"]) == 1
    assert audits == 4
    assert any("已省略" in w for w in state["warnings"])


async def test_address_fallback_requires_trusted_quote_and_building(tmp_path, monkeypatch):
    source = SourceRef(
        id="S1",
        title="官方场馆",
        url="https://test.gov.cn",
        level="L1",
        fetched_at=now(),
        text="1933老场坊位于沙泾路10号。",
    )

    async def model(instruction, data, schema):
        return schema.model_validate(
            {
                "aliases": [],
                "addresses": [
                    {
                        "street": "沙泾路",
                        "house_number": "10",
                        "source_id": "S1",
                        "evidence": source.text,
                    }
                ],
            }
        )

    async def get(client, url, **kwargs):
        values = []
        if kwargs["params"].get("street"):
            values = [
                {
                    "name": "19叁III",
                    "class": "building",
                    "type": "yes",
                    "lat": "31.2565",
                    "lon": "121.4877",
                    "address": {"city": "上海市", "road": "沙泾路", "house_number": "10"},
                    "osm_type": "way",
                    "osm_id": 123,
                }
            ]
        return httpx.Response(200, json=values, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    geo = Geo(Settings(_env_file=None), Store(tmp_path), SimpleNamespace(json=model))
    poi = POI(id="P1", name="1933老场坊")
    await geo.locate("上海", poi, [source])
    assert poi.lat == 31.2565 and "来源地址" in poi.location_note
    source.level = "L3"
    with pytest.raises(ValueError, match="无法唯一"):
        await geo.locate("上海", POI(id="P2", name="另一场馆"), [source])


async def test_address_search_does_not_use_same_address_cafe(tmp_path, monkeypatch):
    async def get(client, url, **kwargs):
        return httpx.Response(
            200,
            json=[
                {
                    "name": "同址咖啡馆",
                    "class": "amenity",
                    "type": "cafe",
                    "lat": "31.2",
                    "lon": "121.4",
                    "address": {"city": "上海市", "road": "沙泾路", "house_number": "10"},
                }
            ],
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    assert (
        await Geo(Settings(_env_file=None), Store(tmp_path)).search("上海", "沙泾路", "10") is None
    )


def test_completed_job_serves_generated_illustration(tmp_path):
    from fastapi.testclient import TestClient
    from PIL import Image

    from backend.app.main import create_app

    store = Store(tmp_path)
    store.save({"id": "illustrated", "status": "completed"})
    folder = tmp_path / "jobs" / "illustrated"
    folder.mkdir(parents=True)
    Image.new("RGB", (20, 20), "green").save(folder / "illustration-0-abcd.png")
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        response = client.get("/api/jobs/illustrated/files/illustration-0-abcd.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert client.get("/api/jobs/illustrated/files/state.json").status_code == 404
