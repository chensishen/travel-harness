from datetime import date
from types import SimpleNamespace

import pytest

from backend.harness.schemas import (
    POI,
    Block,
    DayPlan,
    Deliverable,
    OpeningHours,
    SourceRef,
    TaskSpec,
)
from backend.tools.planning import schedule


def test_short_day_is_not_padded_to_dinner():
    poi = POI(id="P1", name="测试场馆", hours=OpeningHours(open="09:00", close="17:00"))
    timeline, meals, total = schedule([poi], [], date(2026, 11, 7), "09:00")
    from backend.harness.quality import minutes

    waits = sum(minutes(x.end) - minutes(x.start) for x in timeline if x.kind == "wait")
    assert waits <= 60
    assert total == sum(minutes(x.end) - minutes(x.start) for x in timeline if x.kind != "wait")
    assert not any("晚餐" in meal for meal in meals)


def test_v13_rejects_one_stop_even_with_complete_visit_facts():
    from backend.harness.quality import quality_issues

    doc = Deliverable(
        spec=TaskSpec(type="travel_guide", city="测试城市", days=1),
        prd_version="1.3",
        candidate_count=3,
        days=[DayPlan(day=1, theme="文化", stops=[POI(id="P1", name="馆")])],
    )
    issues = quality_issues(doc, require_maps=False)
    assert any("候选池" in x for x in issues)
    assert any("至少3" in x for x in issues)


def test_old_fact_exposes_publication_date_instead_of_freshness_claim():
    from backend.harness.schemas import FactRecord
    from backend.render.reader_content import fact_rows
    from backend.tools.sources import normalize_facts

    text = "免费开放。"
    src = SourceRef(
        id="S1",
        title="参观公告",
        url="https://museum.gov.cn/202410/t20241029_123.html",
        level="L1",
        fetched_at="2026-10-31",
        text=text,
    )
    p = POI(
        id="P1",
        name="测试馆",
        facts=[FactRecord(field="ticket", value="0元", evidence=text, source_ids=["S1"])],
    )
    normalize_facts(p, [src])
    row = fact_rows(p, [src])[0]
    assert "免费" in row[1] and "2024-10" in row[1]
    assert "发布" in row[2] and "访问" in row[2]


def test_l3_single_source_is_never_a_v13_body_fact():
    import pytest

    from backend.harness.reviewer import validate_facts

    src = SourceRef(
        id="S1",
        title="新闻",
        url="https://news.example/story",
        level="L3",
        fetched_at="2026-10-31",
        text="展厅介绍。",
    )
    doc = Deliverable(
        spec=TaskSpec(type="culture_intro", city="测试城"),
        prd_version="1.3",
        pois=[POI(id="P1", name="馆", blocks=[Block(text="展厅介绍。", source_ids=["S1"])])],
        sources=[src],
    )
    with pytest.raises(ValueError, match="L3"):
        validate_facts(doc)


def test_group_budget_scales_with_travelers_and_keeps_experience_separate():
    from backend.harness.schemas import FactRecord
    from backend.harness.visit_context import group_budget

    p = POI(id="P1", name="馆", facts=[FactRecord(field="ticket", value="免费", source_ids=["S1"])])
    doc = Deliverable(spec=TaskSpec(type="travel_guide", city="城", days=1, travelers=2), pois=[p])
    group_budget(doc)
    assert doc.practical[-1].amount_range == [160, 360]
    assert "2人1天合计" in doc.practical[-1].label
    assert not doc.practical[0].estimated
    assert all(x.estimated and "经验" in (x.label + x.basis) for x in doc.practical[1:])


def test_assumptions_remove_weekday_conflicts_and_source_fragments():
    from backend.harness.visit_context import normalize_assumptions

    spec = TaskSpec(
        type="travel_guide",
        city="城",
        days=1,
        start_date=date(2026, 11, 7),
        origin="中心站",
        assumptions=[
            "按非周一工作日规划",
            "按下个周六安排",
            "停车预约不等同于个人参观预约",
            "预算适中",
            "预算适中",
        ],
    )
    doc = Deliverable(spec=spec)
    normalize_assumptions(doc)
    assert len(spec.assumptions) <= 6 and len(spec.assumptions) == len(set(spec.assumptions))
    assert not any("工作日" in s or "停车预约" in s for s in spec.assumptions)
    assert any("2026-11-07" in s for s in spec.assumptions)


async def test_station_graph_returns_line_stop_names_and_confidence(tmp_path, monkeypatch):
    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.schemas import Segment
    from backend.tools.geo import distance
    from backend.tools.transit import TransitProvider

    async def walk(geo, a, b, foot=True):
        metres = distance(a, b)
        return Segment(
            from_poi=a.id,
            to_poi=b.id,
            duration_min=max(1, metres / 70),
            distance_m=metres,
            polyline=[[a.lng, a.lat], [(a.lng + b.lng) / 2, (a.lat + b.lat) / 2], [b.lng, b.lat]],
            source="test",
            route_verified=True,
            geometry_mode="foot",
        )

    monkeypatch.setattr("backend.tools.planning.routed", walk)
    geo = SimpleNamespace(config=Settings(_env_file=None), store=Store(tmp_path))
    nodes = [
        {
            "type": "node",
            "id": i + 1,
            "lat": 31,
            "lon": 121 + i * 0.01,
            "tags": {"name": f"车站{i + 1}"},
        }
        for i in range(3)
    ]
    data = {
        "elements": [
            *nodes,
            {
                "type": "relation",
                "id": 10,
                "tags": {"route": "subway", "ref": "1号线"},
                "members": [
                    {
                        "type": "node",
                        "ref": x["id"],
                        "lat": x["lat"],
                        "lon": x["lon"],
                        "role": "stop",
                        "name": x["tags"]["name"],
                    }
                    for x in nodes
                ],
            },
        ]
    }
    a = POI(id="A", name="起点", lat=31, lng=121)
    b = POI(id="B", name="终点", lat=31, lng=121.02)
    provider = TransitProvider(geo, None)
    segment = await provider.osm_route(a, b, data)
    assert segment is not None and segment.transit["confidence"] == "osm_graph"
    assert any(
        leg.get("line") == "1号线" and leg.get("stops") == 2 for leg in segment.transit["legs"]
    )
    assert all("from" in leg and "to" in leg for leg in segment.transit["legs"])
    assert "估算" in segment.transit["note"]

    calls = 0

    async def network(a, b):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("Overpass unavailable")
        return data

    from backend.tools.transit import OsmGraphProvider

    events = []
    provider = TransitProvider(geo, SimpleNamespace(transit=network), events.append)
    first = await OsmGraphProvider(provider).get_route(a, b, "09:00")
    cached = await OsmGraphProvider(provider).get_route(a, b, "09:00")
    assert cached.transit["legs"] == first.transit["legs"]
    assert cached.transit["confidence"] == "osm_graph"
    assert any(
        e.get("status") == "cached_network" and e.get("error_type") == "ValueError" for e in events
    )


async def test_failed_public_provider_is_reported_and_fallback_is_not_bus(tmp_path, monkeypatch):
    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.schemas import Segment
    from backend.tools.transit import TransitProvider

    async def unavailable(*args):
        raise ValueError("Overpass超时")

    async def route(geo, a, b, foot=True):
        return Segment(
            from_poi=a.id,
            to_poi=b.id,
            from_name=a.name,
            to_name=b.name,
            mode="步行（估算）" if foot else "打车（估算）",
            duration_min=10,
            distance_m=600,
            polyline=[[0, 0], [0.001, 0.001], [0.002, 0.002]],
            source="OSRM",
            geometry_mode="foot" if foot else "car",
            route_verified=True,
        )

    monkeypatch.setattr("backend.tools.planning.routed", route)
    logs = []
    geo = SimpleNamespace(config=Settings(_env_file=None), store=Store(tmp_path))
    segment = await TransitProvider(
        geo, SimpleNamespace(transit=unavailable), logs.append
    ).get_route(POI(id="A", name="起点"), POI(id="B", name="终点"))
    assert segment.transit["confidence"] == "fallback"
    assert "公交" not in segment.mode
    assert any(x.get("status") == "error" for x in logs)


def test_slide_summary_retains_free_ticket_exceptions_and_full_notes_hint():
    from backend.render.reader_content import brief_value

    value = "免费开放；部分临时展览另行收费，须按展览公告购票。" * 5
    summary = brief_value("门票", value)
    assert len(summary) <= 42 and "收费" in summary and "Word" in summary


async def test_candidate_pool_keeps_twenty_real_records_and_logs_duplicates(tmp_path, monkeypatch):
    from backend.harness.candidates import discover

    async def center(*args):
        return {"lat": 31.0, "lng": 121.0}

    monkeypatch.setattr("backend.harness.candidates.city_center", center)
    elements = [
        {
            "type": "node",
            "id": i,
            "lat": 31 + i * 0.0001,
            "lon": 121.0,
            "tags": {"name": f"公园{i}", "leisure": "park"},
        }
        for i in range(25)
    ]
    elements.append({**elements[0], "id": 90})

    async def overpass(query):
        return {"elements": elements}

    harness = SimpleNamespace(geo=None, osm=SimpleNamespace(overpass=overpass))
    result = await discover(
        harness,
        TaskSpec(type="travel_guide", city="城", days=1),
        {"poi_names": ["公园0"]},
        tmp_path,
    )
    assert len(result["pool"]) == 25
    assert any(x["reason_code"] == "DUPLICATE" for x in result["candidates"])
    assert (tmp_path / "candidate-funnel.json").is_file()


def test_published_metadata_does_not_substitute_modified_date():
    from backend.tools.fetch import preserve_publication

    value = preserve_publication(
        b'<html><head><meta property="article:published_time" content="2024-10-29T09:00:00+08:00"><meta property="article:modified_time" content="2026-10-31"></head><body>notice</body></html>',
        "notice",
    )
    assert "2024-10-29" in value and "2026-10-31" not in value


def test_assumptions_are_idempotent_after_route_and_review():
    from backend.harness.visit_context import normalize_assumptions

    doc = Deliverable(
        spec=TaskSpec(type="travel_guide", city="城", start_date=date(2026, 11, 7), origin="火车站",
                      assumptions=["起点未指定，行程从市区中心出发", "未指定具体出行日期，按春秋宜人季节推荐"])
    )
    normalize_assumptions(doc)
    before = list(doc.spec.assumptions)
    normalize_assumptions(doc)
    assert doc.spec.assumptions == before
    assert not any("市区中心" in text or "未指定具体出行日期" in text for text in before)


def test_full_fare_is_included_in_group_total():
    from backend.harness.schemas import FactRecord
    from backend.harness.visit_context import group_budget

    doc = Deliverable(
        spec=TaskSpec(type="travel_guide", city="城", days=1, travelers=2),
        pois=[
            POI(
                id="P1",
                name="馆",
                facts=[FactRecord(field="ticket", value="全票10元，半票5元", source_ids=["S1"])],
            )
        ],
    )
    group_budget(doc)
    assert doc.practical[-1].amount_range == [180, 380]
    assert "未核实门票" not in doc.practical[-1].value


async def test_city_name_is_not_accepted_as_a_concrete_origin(tmp_path, monkeypatch):
    import httpx

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.tools.geo import Geo
    from backend.tools.planning_v13 import origin_point

    store = Store(tmp_path)
    queries = []

    async def get(client, url, params):
        queries.append(params["q"])
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json=[
                {
                    "osm_type": "node",
                    "osm_id": 123,
                    "name": "测试火车站",
                    "class": "railway",
                    "type": "station",
                    "lat": 31,
                    "lon": 121,
                }
            ],
        )

    async def within(*args):
        return True

    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(Geo, "within_city", within)
    geo = Geo(Settings(_env_file=None), store)
    spec = TaskSpec(type="travel_guide", city="测试", origin="测试")
    origin = await origin_point(geo, spec)
    assert queries == ["测试火车站"]
    assert origin.name == spec.origin == "测试火车站"


def test_uploaded_photo_only_rerenders_without_new_research(tmp_path, monkeypatch):
    import io

    from fastapi.testclient import TestClient
    from PIL import Image

    from backend.app.config import Settings
    from backend.app.main import create_app
    from backend.app.store import save_json
    from backend.harness.graph import Harness

    calls = []

    async def run(self, job_id, node):
        calls.append((job_id, node))

    monkeypatch.setattr(Harness, "run", run)
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    store = app.state.store
    store.save({"id": "upload-test", "status": "failed", "kind": "document", "node": "Assembler"})
    folder = tmp_path / "jobs" / "upload-test"
    folder.mkdir(parents=True)
    doc = Deliverable(spec=TaskSpec(type="travel_guide", city="城"), pois=[POI(id="P1", name="馆")])
    save_json(folder / "state.json", {"document": doc.model_dump(mode="json")})
    image = io.BytesIO()
    Image.new("RGB", (1600, 1200), "white").save(image, format="PNG")
    with TestClient(app) as client:
        response = client.post(
            "/api/jobs/upload-test/pois/P1/images",
            files={"file": ("photo.png", image.getvalue(), "image/png")},
        )
        assert response.status_code == 202
    assert calls == [("upload-test", "Assembler")]
    import json

    saved = json.loads((folder / "state.json").read_text())["document"]["pois"][0]
    assert saved["images"][0]["author"] == "用户提供"
    assert saved["photo_status"] == "用户提供实拍"


async def test_official_pdf_source_is_read_as_text_instead_of_html(tmp_path, monkeypatch):
    import io

    import httpx
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.tools.fetch import FetchProvider

    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (Monthly climate 2024) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)

    async def public(*args, **kwargs):
        pass

    async def get(client, url, **kwargs):
        return httpx.Response(
            404 if url.endswith("/robots.txt") else 200,
            request=httpx.Request("GET", url),
            content=b"" if url.endswith("/robots.txt") else output.getvalue(),
        )

    monkeypatch.setattr("backend.tools.fetch.public_url", public)
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    text = await FetchProvider(Settings(_env_file=None), Store(tmp_path)).fetch(
        "https://weather.gov.cn/monthly.pdf"
    )
    assert "Monthly climate 2024" in text


async def test_router_uses_explicit_user_date_and_discards_model_relative_date(
    tmp_path, monkeypatch
):
    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.graph import Harness

    async def model(*args, **kwargs):
        return TaskSpec(
            type="travel_guide",
            city="城",
            days=1,
            start_date=date(2026, 5, 9),
            season_or_dates="2026-05-09",
        )

    harness = Harness(Settings(_env_file=None, data_dir=tmp_path), Store(tmp_path))
    monkeypatch.setattr(harness.models, "json", model)
    state = {}
    await harness.router({"prompt": "下一个周六去城旅游", "options": {}}, state, tmp_path)
    assert state["spec"]["start_date"] is None
    await harness.router(
        {"prompt": "去城旅游", "options": {"season_or_dates": "2027年2月6日"}}, state, tmp_path
    )
    assert state["spec"]["start_date"] == "2027-02-06"
    assert state["spec"]["season_or_dates"] == "2027-02-06"
    assert state["spec"]["date_assumed"] is False

    await harness.router(
        {"prompt": "2026年11月14日去城旅游", "options": {"season_or_dates": "未指定"}},
        state, tmp_path,
    )
    assert state["spec"]["start_date"] == "2026-11-14"
    assert state["spec"]["date_assumed"] is False
    await harness.router(
        {"prompt":"2026年11月14日去城旅游\n修改要求：出发日期改到2027年2月6日",
         "options":{"season_or_dates":"2026-11-14"},
         "revision_instruction":"出发日期改到2027年2月6日"}, state, tmp_path,
    )
    assert state["spec"]["start_date"] == "2027-02-06"
    assert state["spec"]["season_or_dates"] == "2027-02-06"


async def test_router_reports_unrecognized_city_as_unrecognized(tmp_path, monkeypatch):
    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.graph import Harness

    async def model(*args, **kwargs):
        return TaskSpec(type="travel_guide", city="未指定")

    harness = Harness(Settings(_env_file=None, data_dir=tmp_path), Store(tmp_path))
    monkeypatch.setattr(harness.models, "json", model)
    with pytest.raises(ValueError, match="未能从需求中识别目的地城市"):
        await harness.router({"prompt": "做一个火星三日游", "options": {}}, {}, tmp_path)


async def test_previous_year_forecast_is_not_presented_as_monthly_climate(tmp_path):
    from backend.harness.visit_context import Context, PracticalCandidate, enrich_context

    text = "预计11月平均气温20.6℃，较常年偏高0.5℃。"
    source = SourceRef(
        id="S1",
        title="本月将有4次冷空气",
        url="https://weather.gov.cn/old.html",
        level="L1",
        text=text,
        fetched_at="2026-11-02",
        published_at=date(2024, 11, 4),
    )

    async def model(*args, **kwargs):
        return Context(
            practical=[
                PracticalCandidate(
                    category="climate",
                    label="11月气候",
                    value=text,
                    source_ids=["S1"],
                    evidence=text,
                )
            ]
        )

    doc = Deliverable(
        spec=TaskSpec(type="travel_guide", city="城", start_date=date(2026, 11, 7)),
        sources=[source],
    )
    result = await enrich_context(
        SimpleNamespace(models=SimpleNamespace(json=model)), doc, tmp_path
    )
    assert not result.practical


def test_monthly_climate_derivation_reports_its_arithmetic():
    from backend.harness.visit_context import monthly_normal

    doc = Deliverable(
        spec=TaskSpec(type="travel_guide", city="城", start_date=date(2026, 11, 7)),
        sources=[
            SourceRef(
                id="S1",
                title="官方农业气象月报",
                url="https://weather.gov.cn/month.pdf",
                level="L1",
                fetched_at="2026-11-02",
                text="气温：11 月全市平均气温 21.1℃，较常年同期偏高 1.0℃。",
            )
        ],
    )
    climate = monthly_normal(doc)
    assert climate.source_ids == ["S1"]
    assert (
        "20.1℃" in climate.value and "推算" in climate.value and "非本次出行预报" in climate.value
    )


def test_lunch_can_use_next_stop_dining_when_travel_enters_lunch_window():
    hours = OpeningHours(open="09:00", close="17:00")
    a = POI(id="A", name="甲馆", duration_min=140, hours=hours)
    b = POI(id="B", name="乙馆", duration_min=90, hours=hours, dining=[{"name": "实际餐厅"}])
    from backend.harness.schemas import Segment

    timeline, meals, _ = schedule(
        [a, b],
        [
            Segment(
                from_poi="A",
                to_poi="B",
                duration_min=20,
                distance_m=1000,
                source="OSRM",
                polyline=[],
            )
        ],
        date(2026, 11, 7),
        "09:00",
    )
    meal = next(e for e in timeline if e.kind == "meal")
    assert meal.start == "11:40" and "实际餐厅" in meal.place
    assert timeline[-1].kind == "visit" and meals


async def test_nearest_three_stations_are_distinct_not_duplicate_platforms(tmp_path, monkeypatch):
    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.schemas import Segment
    from backend.tools.transit import TransitProvider

    async def walk(geo, a, b, foot=True):
        return Segment(
            from_poi=a.id,
            to_poi=b.id,
            duration_min=20 if a.id == "A" and b.name == "受阻站" else 1,
            distance_m=60,
            polyline=[[a.lng, a.lat], [b.lng, b.lat], [b.lng, b.lat]],
            source="OSRM",
            route_verified=True,
            geometry_mode="foot",
        )

    monkeypatch.setattr("backend.tools.planning.routed", walk)
    elements = []
    for i in range(3):
        members = [
            {
                "type": "node",
                "ref": i + 1,
                "lat": 31,
                "lon": 121 + i * 0.0001,
                "role": "stop",
                "name": "受阻站",
            },
            {
                "type": "node",
                "ref": 10,
                "lat": 31,
                "lon": 121.008,
                "role": "stop",
                "name": "可达站",
            },
            {"type": "node", "ref": 20, "lat": 31, "lon": 121.02, "role": "stop", "name": "终点站"},
        ]
        elements.append(
            {
                "type": "relation",
                "id": 100 + i,
                "tags": {"route": "subway", "ref": "1号线"},
                "members": members,
            }
        )
    geo = SimpleNamespace(config=Settings(_env_file=None), store=Store(tmp_path))
    result = await TransitProvider(geo, None).osm_route(
        POI(id="A", name="起点", lat=31, lng=121),
        POI(id="B", name="终点", lat=31, lng=121.02),
        {"elements": elements},
    )
    assert result and result.transit["legs"][0]["to"] == "可达站"


async def test_osm_complete_way_geometry_is_preserved_in_transit(tmp_path, monkeypatch):
    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.schemas import Segment
    from backend.tools.planning import transit_segment

    async def walk(geo, a, b, foot=True):
        return Segment(
            from_poi=a.id,
            to_poi=b.id,
            duration_min=2,
            distance_m=100,
            polyline=[[a.lng, a.lat], [b.lng, b.lat], [b.lng, b.lat]],
            source="OSRM",
            route_verified=True,
            geometry_mode="foot",
        )

    monkeypatch.setattr("backend.tools.planning.routed", walk)
    stops = [
        {
            "type": "node",
            "ref": i + 1,
            "lat": 31,
            "lon": 121 + i * 0.01,
            "role": "stop",
            "name": f"站{i + 1}",
        }
        for i in range(3)
    ]
    shape = [{"lon": 121, "lat": 31}, {"lon": 121.01, "lat": 31.001}, {"lon": 121.02, "lat": 31}]
    relation = {
        "type": "relation",
        "id": 10,
        "tags": {"route": "subway", "ref": "1号线"},
        "members": [
            *stops,
            {
                "type": "way",
                "role": "platform",
                "geometry": [{"lon": 125, "lat": 32}, {"lon": 125.01, "lat": 32}],
            },
            {"type": "way", "role": "", "geometry": shape},
        ],
    }
    result = await transit_segment(
        SimpleNamespace(config=Settings(_env_file=None), store=Store(tmp_path)),
        POI(id="A", name="起点", lat=31, lng=121),
        POI(id="B", name="终点", lat=31, lng=121.02),
        {"elements": [relation]},
    )
    assert result.geometry_mode == "subway" and result.transit["confidence"] == "osm_graph"
    assert result.parts[1]["coordinates"] == [[p["lon"], p["lat"]] for p in shape]
    assert result.transit["legs"][1]["stops"] == 2


def test_nearby_street_photo_needs_subject_metadata_not_just_coordinates():
    from backend.tools.photos import subject_supported

    generic = {
        "title": "File:Guangzhou (31938).jpg",
        "description": "A residential street in Guangzhou",
        "categories": "Guangzhou streets",
        "subject_names": ["广州 流花湖公园", "流花湖公园", "Liuhua Lake Park"],
    }
    assert not subject_supported(generic)
    assert subject_supported({**generic, "title": "File:Liuhua Lake Park Guangzhou.jpg"})
    assert subject_supported({**generic, "subject_evidence": "wikidata_p18"})


def test_gate_name_matches_official_site_with_parentheses_or_split_name():
    from backend.harness.enrichment import source_matches_poi

    poi = POI(id="P1", name="西安城墙永宁门")
    official = SourceRef(
        id="S1",
        title="西安城墙参观须知",
        url="https://tour.gov.cn/wall",
        level="L1",
        fetched_at="2026-11-02",
        text="西安城墙的南门为永宁门。永宁门开放时间另见公告。",
    )
    assert source_matches_poi(official, poi, "西安")
    assert source_matches_poi(
        official.model_copy(update={"text": "西安城墙（永宁门）游客须知。"}), poi, "西安"
    )
    assert not source_matches_poi(
        official.model_copy(update={"title": "南京城墙参观须知", "text": "南京城墙的永宁门。"}),
        poi,
        "西安",
    )


async def test_writer_does_not_leak_l3_ids_through_mixed_fact_references(tmp_path, monkeypatch):
    import pytest

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.graph import Harness
    from backend.harness.schemas import FactRecord

    refs = [
        SourceRef(
            id="S1",
            title="官方馆藏",
            url="https://museum.gov.cn/x",
            level="L1",
            fetched_at="2026-11-02",
            text="展品说明。",
        ),
        SourceRef(
            id="S2",
            title="转载",
            url="https://news.example/x",
            level="L3",
            fetched_at="2026-11-02",
            text="展品说明。",
        ),
    ]
    doc = Deliverable(
        spec=TaskSpec(type="culture_intro", city="城"),
        prd_version="1.3",
        sources=refs,
        pois=[
            POI(
                id="P1",
                name="馆",
                facts=[
                    FactRecord(
                        field="intro",
                        value="展品说明。",
                        evidence="展品说明。",
                        source_ids=["S1", "S2"],
                    )
                ],
            )
        ],
    )
    captured = {}

    async def model(instruction, data, schema):
        captured.update(data)
        raise RuntimeError("capture boundary")

    harness = Harness(Settings(_env_file=None, data_dir=tmp_path), Store(tmp_path))
    monkeypatch.setattr(harness.models, "json", model)
    with pytest.raises(RuntimeError, match="capture boundary"):
        await harness.writer(
            {},
            {
                "document": doc.model_dump(mode="json"),
                "spec": doc.spec.model_dump(mode="json"),
                "outline": {},
                "sources": [r.model_dump(mode="json") for r in refs],
            },
            tmp_path,
        )
    assert captured["facts"][0]["facts"][0]["source_ids"] == ["S1"]
    assert [s["id"] for s in captured["sources"]] == ["S1"]
    assert set(captured["facts"][0]) == {"id", "name", "facts"}


def test_visit_fields_are_scoped_to_target_table_row_and_entity():
    from backend.harness.enrichment import scoped_source_text, supported_visit_fact
    from backend.harness.schemas import FactRecord

    museum = POI(id="P1", name="陕西历史博物馆")
    tower = POI(id="P2", name="大雁塔")
    own = "| 1 | 陕西历史博物馆 | 小寨东路91号 | 09:00-17:00 |"
    other = "| 13 | 西安中国书法艺术博物馆 | 自强东路585号 | 08:30-18:00 |"
    src = SourceRef(
        id="S1",
        title="博物馆介绍",
        url="https://tour.gov.cn/list",
        level="L1",
        fetched_at="2026-11-02",
        text="| 序号 | 博物馆名称 | 地址 | 开放时间 |\n" + own + "\n" + other,
    )
    text = scoped_source_text(src, museum, "西安", [museum, tower])
    assert own in text and other not in text
    bad = FactRecord(field="hours", value="08:30-18:00", evidence=other, source_ids=["S1"])
    assert not supported_visit_fact(bad, src, museum, "西安", [museum, tower])
    correct = FactRecord(field="hours", value="09:00-17:00", evidence=own, source_ids=["S1"])
    assert supported_visit_fact(correct, src, museum, "西安", [museum, tower])
    unrelated = src.model_copy(
        update={
            "title": "陕西历史博物馆参观须知",
            "text": "陕西历史博物馆位于大雁塔西北侧。地址小寨东路91号。",
        }
    )
    assert scoped_source_text(unrelated, tower, "西安", [museum, tower]) == ""


async def test_geocoder_accepts_missing_name_details(tmp_path, monkeypatch):
    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.tools.geo import Geo

    store = Store(tmp_path)
    store.put_cache(
        store.key("geo-v2", "测试市测试馆"),
        [
            {
                "osm_type": "node",
                "osm_id": 1,
                "name": "测试馆",
                "namedetails": None,
                "class": "tourism",
                "lat": "31",
                "lon": "121",
            }
        ],
    )

    async def within(*args):
        return True

    monkeypatch.setattr(Geo, "within_city", within)
    result = await Geo(Settings(_env_file=None), store).search("测试市", "测试馆")
    assert result["osm_id"] == 1


async def test_late_visit_fact_recovery_preserves_written_highlights():
    from backend.harness.enrichment import VisitInformation, enrich
    from backend.harness.schemas import FactRecord
    from backend.tools.llm import IncompleteModelOutput

    source = SourceRef(
        id="S1",
        title="测试馆参观须知",
        url="https://museum.gov.cn/visit",
        level="L1",
        fetched_at="2026-11-02",
        text="测试馆免费开放，09:00-17:00，无需预约，地址测试路1号。",
    )
    facts = [
        FactRecord(field=k, value=v, evidence=source.text, source_ids=["S1"])
        for k, v in [
            ("ticket", "免费"),
            ("hours", "09:00-17:00"),
            ("booking", "无需预约"),
            ("address", "测试路1号"),
        ]
    ]
    blocks = [Block(text=f"已有文化亮点{i}", source_ids=["S1"]) for i in range(4)]
    doc = Deliverable(
        prd_version="1.3",
        spec=TaskSpec(type="culture_intro", city="测试市"),
        sources=[source],
        pois=[POI(id="P1", name="测试馆", facts=facts, blocks=blocks)],
    )

    calls = []

    async def model(instruction, payload, schema):
        calls.append(payload)
        if len(calls) == 1:
            raise IncompleteModelOutput("模型输出被截断（VisitInformation，4157 字符）")
        assert "返回紧凑 JSON" in instruction
        return VisitInformation(facts=facts, introduction=Block(text="新简介", source_ids=["S1"]))

    result = await enrich(
        SimpleNamespace(models=SimpleNamespace(json=model)), doc, search_missing=False
    )
    assert result.pois[0].blocks == blocks
    assert len(calls) == 2


def test_photo_attribution_hyperlinks_survive_both_office_exports(tmp_path):
    from docx import Document
    from PIL import Image
    from pptx import Presentation

    from backend.harness.schemas import ImageRef
    from backend.render.slides_v11 import render_slides_v11
    from backend.render.word_v11 import render_word_v11

    photo = tmp_path / "photo.jpg"
    Image.new("RGB", (1200, 800), "green").save(photo)
    image = ImageRef(
        url="https://example.com/photo.jpg",
        author="Photographer",
        license="CC BY 4.0",
        source_page="https://example.com/source",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        local_path=str(photo),
    )
    document = Deliverable(
        spec=TaskSpec(type="culture_intro", city="测试城"),
        prd_version="1.3",
        pois=[POI(id="P1", name="测试馆", images=[image])],
    )
    slides = tmp_path / "travel.pptx"
    word = tmp_path / "travel.docx"
    render_slides_v11(document, slides, "224466")
    render_word_v11(document, word, "224466")
    for package in (Presentation(slides), Document(word)):
        links = [
            str(r.target_ref)
            for part in package.part.package.iter_parts()
            for r in part.rels.values()
            if r.is_external
        ]
        assert str(image.source_page) in links
        assert image.license_url in links


def test_group_fast_track_is_not_personal_booking():
    from backend.harness.enrichment import supported_visit_fact
    from backend.harness.schemas import FactRecord

    text = "对审批通过的团队提供预约快速通道。"
    source = SourceRef(
        id="S1",
        title="城墙游客服务",
        url="https://example.gov.cn/visit",
        level="L1",
        fetched_at="2026-11-02",
        text=text,
    )
    fact = FactRecord(field="booking", value=text, evidence=text, source_ids=["S1"])
    assert not supported_visit_fact(fact, source, POI(id="P1", name="城墙"), "西安")


def test_nearby_fast_food_does_not_replace_sourced_local_cuisine():
    from backend.harness.quality import quality_issues
    from backend.harness.schemas import PracticalItem

    doc = Deliverable(
        spec=TaskSpec(type="travel_guide", city="测试城"),
        prd_version="1.3",
        sources=[
            SourceRef(
                id="S1",
                title="餐厅",
                url="https://www.openstreetmap.org/node/123",
                level="L2",
                fetched_at="2026-11-02",
                text="附近餐厅",
            )
        ],
        practical=[
            PracticalItem(category="food", label="快餐店", value="距路线100米", source_ids=["S1"])
        ],
    )
    assert "缺少L1或L2来源的本地特色饮食" in quality_issues(doc, require_maps=False)
    doc.sources.append(
        SourceRef(
            id="S2",
            title="本地饮食文化",
            url="https://example.gov.cn/food",
            level="L1",
            fetched_at="2026-11-02",
            text="早茶搭配广式点心",
        )
    )
    doc.practical.append(
        PracticalItem(category="food", label="早茶", value="广式点心", source_ids=["S2"])
    )
    assert "缺少L1或L2来源的本地特色饮食" not in quality_issues(doc, require_maps=False)


async def test_route_threshold_counts_city_prefixed_alias_only_once(tmp_path):
    import pytest

    from backend.tools.planning_v13 import CandidateShortage, plan

    pois = [
        POI(id="P1", name="豫园", lat=31.22, lng=121.49, location_verified=True),
        POI(id="P2", name="上海豫园", lat=31.2201, lng=121.4901, location_verified=True),
    ]
    pois += [
        POI(
            id=f"P{i + 3}",
            name=f"独立场馆{i}",
            lat=31.3 + i * 0.01,
            lng=121.5,
            location_verified=True,
        )
        for i in range(7)
    ]
    with pytest.raises(CandidateShortage, match="仅8个"):
        await plan(
            None, TaskSpec(type="travel_guide", city="上海", days=3), pois, [], None, tmp_path
        )


def test_city_transit_cache_keeps_original_freshness_across_resumes(tmp_path):
    from datetime import UTC, datetime, timedelta

    from backend.app.store import Store
    from backend.tools.transit import TransitProvider

    store = Store(tmp_path)
    geo = SimpleNamespace(store=store, routing_city="上海")
    element = {"type": "node", "id": 1, "lat": 31.2, "lon": 121.4}
    key = store.key("transit-city-v13", "上海")
    expired = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    store.put_cache(key, {"elements": [element], "fetched_at": expired})
    assert not TransitProvider(geo, None).transit_data
    original = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    store.put_cache(key, {"elements": [element], "fetched_at": original})
    resumed = TransitProvider(geo, None)
    assert resumed.transit_data[("node", 1)] == element
    resumed.remember_network({"elements": [element], "_fetched_at": datetime.now(UTC).isoformat()})
    assert TransitProvider(geo, None).network_fetched_at == original


async def test_official_closure_is_not_replaced_by_daytime_assumption(tmp_path):
    import pytest

    from backend.harness.schemas import FactRecord
    from backend.tools.planning_v13 import CandidateShortage, plan

    source = SourceRef(
        id="S1",
        url="https://example.gov.cn/closure",
        title="闭馆公告",
        level="L1",
        text="闭馆修缮，开放另行通知",
        fetched_at="2026-11-03",
    )
    pois = [
        POI(id=f"P{i}", name=f"场馆{i}", lat=31 + i * 0.01, lng=121, location_verified=True)
        for i in range(3)
    ]
    pois[0].facts = [
        FactRecord(field="closure", value=source.text, evidence=source.text, source_ids=["S1"])
    ]
    with pytest.raises(CandidateShortage, match="仅2个"):
        await plan(
            None, TaskSpec(type="travel_guide", city="测试城"), pois, [source], None, tmp_path
        )


async def test_multiday_route_backtracks_instead_of_stranding_last_day(tmp_path, monkeypatch):
    import pytest

    import backend.tools.planning_v13 as planning
    from backend.harness.schemas import Segment

    origin = POI(id="ORIGIN", name="车站", lat=31, lng=121, location_verified=True)

    async def start(geo, spec):
        return origin

    route_calls = []

    class Routes:
        def __init__(self, *args):
            pass

        async def get_route(self, a, b, depart):
            route_calls.append((a.id, b.id))
            return Segment(
                from_poi=a.id,
                to_poi=b.id,
                from_name=a.name,
                to_name=b.name,
                duration_min=50 if {a.id, b.id} <= {"D", "E", "F"} else 10,
                distance_m=500,
                mode="步行",
                source="测试路由",
                route_verified=True,
                geometry_mode="foot",
                polyline=[[a.lng, a.lat], [a.lng + 0.0001, a.lat], [b.lng, b.lat]],
                transit={"confidence": "fallback", "note": "测试路由"},
            )

    map_failed = True

    async def basemap(*args):
        if map_failed:
            raise ValueError("地图服务不可用")
        return {"image": "test.png"}

    monkeypatch.setattr(planning, "origin_point", start)
    monkeypatch.setattr(planning, "TransitProvider", Routes)
    pois = [
        POI(
            id=name,
            name="景点" + name,
            lat=31,
            lng=121 + (i + 1) * 0.001,
            location_verified=True,
            duration_min=45,
            hours=OpeningHours(open="09:00", close="17:00"),
        )
        for i, name in enumerate("ABCDEF")
    ]
    spec = TaskSpec(type="travel_guide", city="测试城", days=2, start_date=date(2026, 11, 7))
    with pytest.raises(ValueError, match="地图服务不可用"):
        await planning.plan(SimpleNamespace(), spec, pois, [], SimpleNamespace(basemap=basemap), tmp_path)
    completed_calls = len(route_calls)
    assert completed_calls > 0 and (tmp_path / "route-plan-v13.json").exists()
    map_failed = False
    days, _, _ = await planning.plan(
        SimpleNamespace(),
        spec,
        pois,
        [],
        SimpleNamespace(basemap=basemap),
        tmp_path,
    )
    assert [len(d.stops) for d in days] == [3, 3]
    assert {p.id for p in days[0].stops} != {"A", "B", "C"}
    assert len({p.id for d in days for p in d.stops}) == 6
    assert all(s.duration_min <= 40 for d in days for s in d.segments)
    assert len(route_calls) == completed_calls
    spec.departure_time = "08:30"
    await planning.plan(SimpleNamespace(), spec, pois, [], SimpleNamespace(basemap=basemap), tmp_path)
    assert len(route_calls) > completed_calls


def test_citywalk_sections_do_not_mix_service_center_and_park_hours():
    from backend.harness.enrichment import scoped_source_text

    source = SourceRef(
        id="S1",
        url="https://example.gov.cn/citywalk",
        title="虹口路线",
        level="L1",
        fetched_at="2026-11-03",
        text="路线介绍\n📍虹口区旅游公共服务中心\n开放9:00-17:00\n📍上海鲁迅公园\n四川北路2288号\n5:00-21:00\n📍凯德虹口商业中心\n10:00-22:00",
    )
    scoped = scoped_source_text(source, POI(id="P1", name="鲁迅公园"), "上海")
    assert "5:00-21:00" in scoped and "四川北路2288号" in scoped
    assert "9:00-17:00" not in scoped and "10:00-22:00" not in scoped


def test_government_information_date_is_distinct_from_fetch_date():
    from backend.tools.sources import publication_date

    source = SourceRef(
        id="S1",
        title="公园时段",
        url="https://example.gov.cn/notice",
        level="L1",
        fetched_at="2026-11-03",
        text="信息时间：2026-08-21\n公园开放时段",
    )
    assert publication_date(source) == date(2026, 8, 21)


async def test_only_complete_short_cached_transit_can_skip_network(monkeypatch):
    from backend.harness.schemas import Segment
    from backend.tools.transit import OsmGraphProvider, TransitProvider

    for mode, minutes, cache_error, expected_calls in [
        ("subway", 20, False, 0),
        ("subway", 45, False, 1),
        ("transit_schematic", 20, False, 1),
        ("subway", 20, True, 1),
    ]:
        calls = 0
        attempts = 0
        data = {"elements": [{"type": "node", "id": 1}]}

        async def network(a, b):
            nonlocal calls
            calls += 1
            return data

        async def route(*args):
            nonlocal attempts
            attempts += 1
            if cache_error and attempts == 1:
                raise ValueError("缓存步行接驳不可用")
            return Segment(
                from_poi="A",
                to_poi="B",
                duration_min=minutes,
                distance_m=1000,
                mode="公共交通",
                source="OSM",
                route_verified=True,
                geometry_mode=mode,
                polyline=[[121, 31], [121.01, 31], [121.02, 31]],
                transit={"confidence": "osm_graph"},
            )

        monkeypatch.setattr("backend.tools.planning.transit_segment", route)
        parent = TransitProvider(SimpleNamespace(), SimpleNamespace(transit=network))
        parent.transit_data = {("node", 1): data["elements"][0]}
        result = await OsmGraphProvider(parent).get_route(None, None, "09:00")
        assert calls == expected_calls and result.route_verified


async def test_slow_direct_transit_does_not_hide_valid_transfer(monkeypatch):
    from backend.harness.schemas import Segment
    from backend.tools.transit import OsmGraphProvider, TransitProvider

    def segment(minutes):
        return Segment(
            from_poi="A",
            to_poi="B",
            duration_min=minutes,
            distance_m=1000,
            mode="公共交通",
            source="OSM",
            route_verified=True,
            geometry_mode="subway",
            polyline=[[121, 31], [121.01, 31], [121.02, 31]],
            transit={"confidence": "osm_graph"},
        )

    async def direct(*args):
        return segment(50)

    async def graph(*args):
        return segment(30)

    async def network(*args):
        raise AssertionError("已有满足约束的实际路径，不应请求外部服务")

    monkeypatch.setattr("backend.tools.planning.transit_segment", direct)
    parent = TransitProvider(SimpleNamespace(), SimpleNamespace(transit=network))
    parent.transit_data = {("node", 1): {"type": "node", "id": 1}}
    parent.osm_route = graph
    result = await OsmGraphProvider(parent).get_route(None, None, "09:00")
    assert result.duration_min == 30 and result.geometry_mode == "subway"


async def test_dns_lookup_failure_is_a_recoverable_network_error(monkeypatch):
    import socket

    import httpx
    import pytest

    from backend.tools.fetch import public_url

    def unavailable(*args):
        raise socket.gaierror(socket.EAI_NONAME, "DNS unavailable")

    monkeypatch.setattr("backend.tools.fetch.socket.getaddrinfo", unavailable)
    with pytest.raises(httpx.ConnectError, match="域名"):
        await public_url("https://example.org/notice")


async def test_photo_entity_selection_distinguishes_museum_from_same_named_paper():
    from backend.tools.photos import Photos

    async def choose(prompt, payload, schema):
        assert payload['lat'] == 31.23
        return schema(id='Q1051293')

    photos = Photos(None, None, SimpleNamespace(json=choose))

    async def api(url, params, provider):
        if params.get('action') == 'wbsearchentities':
            return {'search': [
                {'id': 'Q63483610', 'label': '上海博物馆', 'description': 'scientific article', 'match': {'text': '上海博物馆'}},
                {'id': 'Q1051293', 'label': 'Shanghai Museum', 'description': 'museum in Shanghai, China', 'match': {'text': '上海博物馆'}},
            ]}
        if params.get('action') == 'wbgetentities':
            assert params['ids'] == 'Q1051293'
            return {'entities': {'Q1051293': {'claims': {'P18': [{'mainsnak': {'datavalue': {'value': 'Museum.jpg'}}}]}}}}
        return {'query': {'pages': {'1': {'title': 'File:Museum.jpg', 'imageinfo': [{'url': 'https://example.org/museum.jpg', 'descriptionurl': 'https://example.org/page'}]}}}}

    photos.api = api
    poi = POI(id='P1', name='上海博物馆', lat=31.23, lng=121.47)
    batches = photos.search('上海', poi.name, poi)
    first = await anext(batches)
    await batches.aclose()
    assert poi.wikidata == 'Q1051293'
    assert first[0]['subject_evidence'] == 'wikidata_p18'


def test_early_lunch_gap_uses_named_dining_instead_of_skipping_lunch():
    from backend.harness.schemas import Segment

    hours = OpeningHours(open='09:00', close='17:00')
    a = POI(id='A', name='甲馆', duration_min=90, hours=hours,
            dining=[{'name': '附近实名餐厅', 'source': 'https://www.openstreetmap.org/node/1', 'distance_m': 100}])
    b = POI(id='B', name='乙馆', duration_min=120, hours=hours)
    leg = Segment(from_poi='A', to_poi='B', duration_min=32, distance_m=2000, source='OSRM', polyline=[])
    timeline, meals, _ = schedule([a, b], [leg], date(2026, 11, 7), '09:40')
    assert len(meals) == 1
    lunch = next(e for e in timeline if e.kind == 'meal')
    assert lunch.start == '11:10' and '附近实名餐厅' in lunch.place


def test_full_day_without_named_lunch_fails_publication():
    from backend.harness.quality import quality_issues
    from backend.harness.schemas import ScheduleEntry

    doc = Deliverable(spec=TaskSpec(type='travel_guide', city='测试城', days=1), prd_version='1.3',
                      days=[DayPlan(day=1, theme='文化', stops=[], timeline=[
                          ScheduleEntry(start='09:00', end='14:00', kind='visit', place='场馆', poi_id='P1')
                      ])])
    assert any('具名午餐点' in issue for issue in quality_issues(doc, require_maps=False))


def test_city_arrival_requires_more_than_origin_to_first_sight():
    from backend.harness.quality import quality_issues
    from backend.harness.schemas import PracticalItem

    doc = Deliverable(spec=TaskSpec(type='travel_guide', city='上海', days=1), prd_version='1.3',
                      practical=[PracticalItem(category='transport', label='上海火车站→豫园',
                                               value='地铁换乘', source_ids=['S1'])])
    assert '缺少有来源的到达城市接驳指引' in quality_issues(doc, require_maps=False)
    doc.practical.append(PracticalItem(category='transport', label='浦东机场→市区（地铁）',
                                       value='地铁2号线前往人民广场', source_ids=['S1']))
    assert '缺少有来源的到达城市接驳指引' not in quality_issues(doc, require_maps=False)
    assert '缺少有来源的月份气候与穿衣建议' in quality_issues(doc, require_maps=False)
    doc.spec.date_assumed = True
    assert '缺少有来源的月份气候与穿衣建议' not in quality_issues(doc, require_maps=False)


def test_official_tourism_directory_is_not_a_venue_owned_channel():
    from backend.harness.schemas import FactRecord
    from backend.tools.sources import normalize_facts, source_level

    url = 'https://www.meet-in-shanghai.net/cn/tourist-attraction/tower/'
    source = SourceRef(id='S1', title='东方明珠参观信息', url=url, level=source_level(url),
                       fetched_at='2026-11-03', text='每日8:00至22:00。')
    poi = POI(id='P1', name='东方明珠', official_url=url,
              facts=[FactRecord(field='hours', value='8:00至22:00', evidence='每日8:00至22:00。', source_ids=['S1'])])
    normalize_facts(poi, [source])
    assert source.level == 'L1'
    assert not poi.official_url and poi.verification_url == url


@pytest.mark.parametrize("failure", ["none", "geocoding", "second_search"])
async def test_official_candidate_search_continues_when_first_directory_is_remote(
    tmp_path, monkeypatch, failure
):
    from backend.harness.candidates import discover

    queries = []
    looked_up = []
    local = [f'市中心场馆{i}' for i in range(5)]
    remote = [f'远郊景区{i}' for i in range(20)]
    nearby = [f'市中心博物馆{i}' for i in range(4)]
    directories = [remote, nearby]

    async def center(*args):
        return {'lat': 31, 'lng': 121}

    async def overpass(*args):
        return {'elements': [
            {'type': 'node', 'id': i, 'lat': 31, 'lon': 121 + i * .001,
             'tags': {'name': name, 'tourism': 'museum'}}
            for i, name in enumerate(local)
        ]}

    async def search(query):
        queries.append(query)
        if failure == "second_search" and len(queries) == 2:
            import httpx

            raise httpx.ConnectError("service unavailable")
        return f'https://travel.gov.cn/list{len(queries)}'

    async def fetch(url):
        return '\n'.join(directories[int(url[-1]) - 1])

    async def model(instruction, data, schema):
        if 'results' in data:
            return schema(sources=[dict(id='', title='官方名录', url=data['results'],
                                        level='L3', fetched_at='')])
        return schema(places=[
            dict(name=name, category='博物馆', source_id=source['id'], evidence=name)
            for source in data['sources'] for name in source['text'].splitlines()
        ])

    async def locate(city, poi):
        looked_up.append(poi.name)
        if failure == "geocoding" and poi.name == nearby[0]:
            raise ValueError("ambiguous place")
        poi.lat = 31.5 if poi.name in remote else 31.001
        poi.lng = 121
        poi.location_verified = True

    monkeypatch.setattr('backend.harness.candidates.city_center', center)
    harness = SimpleNamespace(
        geo=SimpleNamespace(locate=locate), osm=SimpleNamespace(overpass=overpass),
        search=SimpleNamespace(search=search), fetch=SimpleNamespace(fetch=fetch),
        models=SimpleNamespace(json=model),
    )
    result = await discover(harness, TaskSpec(type='travel_guide', city='城', days=1),
                            {'poi_names': [local[0]]}, tmp_path)
    assert len(queries) == 2
    assert {p['name'] for p in result['pool']} >= set(remote)
    if failure == "second_search":
        assert set(result['shortlist']) >= set(local)
        assert looked_up == remote
        assert result['provider_errors'][-1]['error'] == 'ConnectError'
        return
    assert set(result['shortlist']) <= set(local + nearby)
    assert looked_up == remote + nearby
    if failure == "geocoding":
        assert nearby[0] not in result['shortlist']
        assert result['provider_errors'] == [
            {'provider': 'OSM candidate lookup', 'name': nearby[0], 'error': 'ValueError'}
        ]
