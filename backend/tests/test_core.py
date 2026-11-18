import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from types import SimpleNamespace

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient
from PIL import Image
from pptx import Presentation
from pydantic import ValidationError
from tenacity import wait_none

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.store import Store
from backend.harness.graph import FactSet, Harness, RevisionPlan, safe_error, source_level
from backend.harness.reviewer import route_issues, validate_facts
from backend.harness.schemas import (
    NODES,
    POI,
    Block,
    DayPlan,
    Deliverable,
    FactRecord,
    Segment,
    SourceRef,
    TaskSpec,
    now,
)
from backend.kb.ingest import KnowledgeBase, chunks, normalize_url
from backend.render.documents import render
from backend.render.map_renderer import geojson
from backend.tools.fetch import FetchProvider, public_url
from backend.tools.geo import Geo
from backend.tools.llm import Models
from backend.tools.mcp_search import MCPProvider, MCPToolError


@pytest.fixture
def document():
    """Synthetic evidence is test-only and never seeded into the application."""
    source = SourceRef(
        id="S1",
        title="测试官方页面",
        url="https://test.gov.cn/tickets",
        level="L1",
        fetched_at=now(),
        text="测试展馆成人票30元，学生票15元。周二至周日9:00至17:00开放。",
    )
    poi = POI(
        id="P1",
        name="测试展馆",
        lat=31.23,
        lng=121.47,
        blocks=[Block(text="测试展馆成人票30元。", source_ids=["S1"])],
        facts=[
            FactRecord(
                field="ticket",
                value="成人票30元，学生票15元",
                source_ids=["S1"],
                evidence="成人票30元，学生票15元",
                confidence=0.6,
            )
        ],
    )
    return Deliverable(
        spec=TaskSpec(type="travel_guide", city="测试城市"),
        sources=[source],
        pois=[poi],
        days=[DayPlan(day=1, theme="测试路线", stops=[poi])],
    )


def test_spec_defaults_and_bounds():
    spec = TaskSpec(type="travel_guide", city="上海")
    assert spec.days == 3
    assert spec.assumptions == ["默认行程 3 天 2 晚"]
    assert TaskSpec(type="culture_intro", city="苏州", days=3).days is None
    with pytest.raises(ValidationError):
        TaskSpec(type="travel_guide", city="上海", days=0)


def test_valid_evidence_passes(document):
    validate_facts(document)


async def test_revision_city_and_date_change_overrides_original_options(tmp_path, monkeypatch):
    from backend.harness.schemas import TaskSpec

    store=Store(tmp_path)
    folder=tmp_path/'jobs'/'revision-date'
    folder.mkdir(parents=True)
    previous=TaskSpec(type='travel_guide',city='上海',days=1,start_date=date(2026,11,14))
    (folder/'state.json').write_text(json.dumps({'prd_version':'1.3',
         'spec':previous.model_dump(mode='json'),'warnings':[],'outline':{}}))
    instruction='目的地改为苏州，出发日期改到2027年2月6日'
    store.save({'id':'revision-date','status':'queued','kind':'document',
                'prompt':'2026年11月14日上海一日游\n修改要求：'+instruction,
                'options':{'season_or_dates':'2026-11-14'},'revision_instruction':instruction})
    harness=Harness(Settings(_env_file=None,data_dir=tmp_path),store)

    async def model(instruction,data,schema):
        if schema is RevisionPlan:
            return RevisionPlan(start_node='Router',reason='修改城市与日期',
                                spec=TaskSpec(type='travel_guide',city='苏州',days=1,
                                              start_date=date(2027,2,6)))
        assert schema is TaskSpec
        return TaskSpec(type='travel_guide',city='苏州',days=1,start_date=date(2026,11,14))

    async def stop_after_router(*args):
        raise ValueError('fixture stops before external research')

    monkeypatch.setattr(harness.models,'json',model)
    monkeypatch.setattr(harness,'planner',stop_after_router)
    await harness.run('revision-date','Revision')
    job=store.get('revision-date')
    assert job['status']=='failed' and 'fixture stops' in job['error']
    assert job['spec']['city']=='苏州'
    assert job['spec']['start_date']=='2027-02-06'


@pytest.mark.parametrize(
    "failure", ["unknown_source", "bad_quote", "L3", "expired", "future", "fabricated_price"]
)
def test_ungrounded_facts_rejected(document, failure):
    fact = document.pois[0].facts[0]
    if failure == "unknown_source":
        fact.source_ids = ["MISSING"]
    if failure == "bad_quote":
        fact.evidence = "凭空编造的文字"
    if failure == "L3":
        document.sources[0].level = "L3"
    if failure == "expired":
        fact.verified_at = date.today() - timedelta(days=91)
    if failure == "future":
        fact.verified_at = date.today() + timedelta(days=1)
    if failure == "fabricated_price":
        fact.value = "成人票99元"
    with pytest.raises(ValueError):
        validate_facts(document)


def test_unknown_fact_must_be_explicit(document):
    fact = document.pois[0].facts[0]
    fact.source_ids = []
    with pytest.raises(ValueError):
        validate_facts(document)
    fact.value = "暂未核实，请以官方渠道为准"
    validate_facts(document)


def test_unsourced_sentence_rejected(document):
    document.pois[0].blocks[0].source_ids = []
    with pytest.raises(ValueError):
        validate_facts(document)


def test_route_rules_report_concrete_exceptions(document):
    day = document.days[0]
    day.stops[0].duration_min = 480
    day.segments = [
        Segment(
            from_poi="P1",
            to_poi="P2",
            duration_min=50,
            distance_m=5000,
            polyline=[[121.47, 31.23], [121.48, 31.24]],
            source="估算",
        )
    ]
    issues = "；".join(route_issues(document))
    assert "景点数量" in issues and "10 小时" in issues and "40 分钟" in issues and "估算" in issues


def test_chunk_overlap_and_url_normalization():
    text = "文" * 1100
    parts = chunks(text)
    assert parts[0][-50:] == parts[1][:50]
    assert len(parts) == 3 and all(len(p) <= 500 for p in parts)
    assert normalize_url("https://Example.COM/path/#x") == "https://example.com/path"


def test_store_event_replay_and_expiry(tmp_path):
    store = Store(tmp_path)
    store.save({"id": "job", "status": "running"})
    seq = store.event("job", node="Router", status="running")
    store.event("job", node="Router", status="completed")
    assert len(store.events("job", seq)) == 1
    store.put_cache("x", {"a": 1})
    assert store.cached("x") == {"a": 1}
    assert store.cached("x", -1) is None


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1", "http://169.254.169.254/latest", "http://[::1]", "file:///etc/passwd"],
)
async def test_fetch_rejects_private_or_non_http(url):
    with pytest.raises(ValueError):
        await public_url(url)


async def test_fetch_proxy_fake_dns_is_opt_in(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: [(2, 1, 6, "", ("198.18.1.147", 443))])
    with pytest.raises(ValueError):
        await public_url("https://www.shanghaimuseum.cn")
    await public_url("https://www.shanghaimuseum.cn", proxy_fake_ip=True)
    with pytest.raises(ValueError):
        await public_url("http://198.18.1.147", proxy_fake_ip=True)
    monkeypatch.setattr("socket.getaddrinfo", lambda *args: [(2, 1, 6, "", ("192.168.1.1", 443))])
    with pytest.raises(ValueError):
        await public_url("https://private.example", proxy_fake_ip=True)


def test_nested_service_errors_preserve_status_without_credentials():
    request = httpx.Request("GET", "https://service.example/?key=private-test-key")
    response = httpx.Response(429, request=request)
    error = ExceptionGroup(
        "transport group",
        [
            ExceptionGroup(
                "session",
                [httpx.HTTPStatusError("private-test-key", request=request, response=response)],
            )
        ],
    )
    message = safe_error(error, Settings())
    assert "429" in message and "private-test-key" not in message


async def test_kb_search_keeps_vector_order_when_rerank_times_out():
    class Models:
        async def embed(self, texts):
            return [[0.1, 0.2] for _ in texts]

        async def rerank(self, query, texts):
            raise httpx.ReadTimeout("rerank timed out")

    entity = {
        "text": "票价30元",
        "source_url": "https://museum.example/ticket",
        "fetched_at": "2026-11-01",
        "valid_until": "2999-01-01",
        "source_level": "L1",
        "poi_name": "测试馆",
    }
    kb = KnowledgeBase(Settings(_env_file=None), Models())
    kb.client = SimpleNamespace(search=lambda *args, **kwargs: [[{"entity": entity}]])
    kb.ensure = lambda dimension: None
    hits = await kb.search("上海", "票价")
    assert [hit["text"] for hit in hits] == ["票价30元"]


async def test_verifier_batches_pois_and_limits_each_call_to_relevant_sources(tmp_path):
    from backend.harness.graph import verifier_sources

    names = [f"场馆{i}" for i in range(1, 8)]
    sources = [
        {"id": f"S{i}", "title": f"场馆{i}官网", "level": "L1", "text": f"场馆{i} 票价 30 元",
         "url": f"https://museum{i}.example/visit", "fetched_at": "2026-11-10T00:00:00+00:00"}
        for i in range(1, 8)
    ] + [{"id": "S9", "title": "无关网页", "level": "L3", "text": "其他内容",
          "url": "https://other.example/page", "fetched_at": "2026-11-10T00:00:00+00:00"}]
    harness = Harness(Settings(data_dir=tmp_path), Store(tmp_path))
    calls = []

    async def model(instruction, data, schema):
        calls.append(data)
        return FactSet(pois=[POI(id=f"X{i}", name=name) for i, name in enumerate(data["outline"]["poi_names"], 1)])

    harness.models.json = model
    state = {
        "spec": TaskSpec(type="travel_guide", city="上海", days=2).model_dump(mode="json"),
        "outline": {"poi_names": names},
        "warnings": [],
        "sources": sources,
    }
    await harness.verifier({}, state, tmp_path)
    assert [len(call["outline"]["poi_names"]) for call in calls] == [3, 3, 1]
    assert all(len(call["sources"]) <= 7 for call in calls)
    assert "S9" not in {s["id"] for s in calls[0]["sources"]}
    assert [poi["name"] for poi in state["document"]["pois"]] == names
    assert [poi["id"] for poi in state["document"]["pois"]] == [f"P{i}" for i in range(1, 8)]
    picked = verifier_sources(sources, ["场馆1"], per_poi=1, max_chars=100)
    assert [s["id"] for s in picked] == ["S1"]


async def test_verifier_halves_batches_when_the_model_returns_no_json(tmp_path):
    names = [f"场馆{i}" for i in range(1, 7)]
    sources = [
        {"id": f"S{i}", "title": f"场馆{i}官网", "level": "L1", "text": f"场馆{i} 票价 30 元",
         "url": f"https://museum{i}.example/visit", "fetched_at": "2026-11-10T00:00:00+00:00"}
        for i in range(1, 7)
    ]
    harness = Harness(Settings(data_dir=tmp_path), Store(tmp_path))
    calls = []

    async def model(instruction, data, schema):
        batch = data["outline"]["poi_names"]
        calls.append(list(batch))
        if len(batch) > 2:
            raise ValueError("模型未返回结构化内容（FactSet），请重试当前节点或更换模型")
        return FactSet(pois=[POI(id=f"X{i}", name=name) for i, name in enumerate(batch, 1)])

    harness.models.json = model
    state = {
        "spec": TaskSpec(type="travel_guide", city="上海", days=2).model_dump(mode="json"),
        "outline": {"poi_names": names},
        "warnings": [],
        "sources": sources,
    }
    await harness.verifier({}, state, tmp_path)
    assert calls == [names[0:3], names[0:1], names[1:3], names[3:6], names[3:4], names[4:6]]
    assert [poi["name"] for poi in state["document"]["pois"]] == names


async def test_verifier_resumes_from_checkpoint_without_repeating_completed_batches(tmp_path):
    names = [f"场馆{i}" for i in range(1, 5)]
    sources = [
        {"id": f"S{i}", "title": f"场馆{i}官网", "level": "L1", "text": f"场馆{i} 票价 30 元",
         "url": f"https://museum{i}.example/visit", "fetched_at": "2026-11-10T00:00:00+00:00"}
        for i in range(1, 5)
    ]
    harness = Harness(Settings(data_dir=tmp_path), Store(tmp_path))
    calls = []
    fail_once = {"pending": True}

    async def model(instruction, data, schema):
        batch = data["outline"]["poi_names"]
        calls.append(list(batch))
        if batch == names[3:4] and fail_once["pending"]:
            fail_once["pending"] = False
            raise ValueError("外部服务请求超时，可重试当前节点")
        return FactSet(pois=[POI(id="X", name=name) for name in batch])

    harness.models.json = model
    state = {
        "spec": TaskSpec(type="travel_guide", city="上海", days=2).model_dump(mode="json"),
        "outline": {"poi_names": names},
        "warnings": [],
        "sources": sources,
    }
    with pytest.raises(ValueError, match="超时"):
        await harness.verifier({}, state, tmp_path)
    assert calls == [names[0:3], names[3:4]]

    calls.clear()
    await harness.verifier({}, state, tmp_path)
    assert calls == [names[3:4]]
    assert [poi["name"] for poi in state["document"]["pois"]] == names


async def test_empty_model_content_is_retried_once_then_fails_clearly():
    from pydantic import BaseModel

    from backend.tools.llm import Models

    class Reply(BaseModel):
        ok: bool

    replies = [
        {"choices": [{"finish_reason": "stop", "message": {"content": None, "reasoning_content": "只思考"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}]},
    ]
    calls = []

    async def post(*args):
        calls.append(1)
        return replies[len(calls) - 1]

    models = Models(Settings(_env_file=None))
    models.post = post
    assert (await models.json("指令", {}, Reply)).ok is True
    assert len(calls) == 2

    async def always_empty(*args):
        return {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}

    models.post = always_empty
    with pytest.raises(ValueError, match="模型未返回结构化内容"):
        await models.json("指令", {}, Reply)


def test_model_allowance_429_reports_reset_time_without_leaking_body():
    body = ('{"error":{"message":"WorkBuddy: 您的使用量已超出频率限制，'
            '将在 2026-11-12 01:02:02 UTC+8 重置，您也可以切换其他模型继续使用。'
            ' (out of its allowance: not asked again before Nov 12 01:02 UTC+8) sk-test-private"}}')
    response = httpx.Response(429, request=httpx.Request("POST", "http://127.0.0.1:3425/v1/chat/completions"),
                              text=body)
    error = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
    message = safe_error(error, Settings(_env_file=None))
    assert "2026-11-12 01:02:02" in message and "切换其他模型" in message
    assert "sk-test-private" not in message


def test_assumed_date_does_not_require_month_climate():
    from backend.harness.graph import climate_status

    document = Deliverable(
        spec=TaskSpec(type="travel_guide", city="上海", days=2, start_date=date(2026, 11, 14), date_assumed=True),
        prd_version="1.3",
    )
    assert climate_status(document) == "assumed"
    document.spec.date_assumed = False
    assert climate_status(document) == "missing"


def test_connect_error_is_actionable_and_does_not_echo_url_credentials():
    request = httpx.Request("GET", "https://service.example/?key=private-test-key")
    error = httpx.ConnectError("connection failed", request=request)
    message = safe_error(error, Settings())
    assert "无法连接外部服务" in message and "service.example" in message
    assert "private-test-key" not in message


async def test_three_day_planner_rejects_oversized_candidate_pool(tmp_path):
    harness = Harness(Settings(data_dir=tmp_path), Store(tmp_path))

    async def oversized(instruction, data, schema):
        return schema.model_validate(
            {
                "title": "上海",
                "poi_names": [f"景点{i}" for i in range(36)],
                "themes": ["历史"],
                "queries": ["上海历史"],
            }
        )

    harness.models.json = oversized
    state = {"spec": TaskSpec(type="travel_guide", city="上海", days=3).model_dump()}
    with pytest.raises(ValidationError):
        await harness.planner({"prompt": "上海三日", "options": {}}, state, tmp_path)
    assert "outline" not in state


async def test_mcp_retries_wrapped_connection_failure_but_not_invalid_data():
    config = Settings()
    provider = MCPProvider(config, Models(config))
    attempts = 0

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[])

    @asynccontextmanager
    async def flaky(server):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ExceptionGroup("transport", [httpx.ConnectError("TLS failed")])
        yield Session()

    provider.session = flaky
    discover = MCPProvider.discover.retry_with(wait=wait_none())
    assert await discover(provider, "test") == []
    assert attempts == 3

    @asynccontextmanager
    async def invalid(server):
        nonlocal attempts
        attempts += 1
        raise ExceptionGroup("validation", [ValueError("invalid tool arguments")])
        yield

    provider.session = invalid
    attempts = 0
    with pytest.raises(ExceptionGroup):
        await discover(provider, "test")
    assert attempts == 1


async def test_mcp_fetch_tool_error_uses_checked_http_fallback(tmp_path, monkeypatch):
    async def checked(url, **kwargs):
        assert url == "https://museum.example/visit"

    class MCP:
        async def discover(self, server):
            return [{"name": "web_fetch_exa"}]

        async def call(self, server, request):
            raise ExceptionGroup("session", [MCPToolError("page unavailable")])

    def respond(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200,
            text="<html><body><article><p>"
            + "博物馆每周二至周日开放，参观须遵守馆方公布的预约规则。" * 20
            + "</p></article></body></html>",
        )

    client = httpx.AsyncClient
    monkeypatch.setattr("backend.tools.fetch.public_url", checked)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    provider = FetchProvider(Settings(), Store(tmp_path), MCP())
    body = await provider.fetch("https://museum.example/visit")
    assert "博物馆每周二至周日开放" in body


async def test_verifier_marks_l3_sensitive_claim_unverified(tmp_path, document):
    harness = Harness(Settings(data_dir=tmp_path), Store(tmp_path))
    document.sources[0].level = "L3"

    async def extract(*args):
        return FactSet(pois=document.pois)

    harness.models.json = extract
    state = {
        "spec": document.spec.model_dump(),
        "outline": {},
        "warnings": [],
        "sources": [s.model_dump(mode="json") for s in document.sources],
    }
    await harness.verifier({}, state, tmp_path)
    result = Deliverable.model_validate(state["document"])
    ticket = next(f for f in result.pois[0].facts if f.field == "ticket")
    assert ticket.value == "暂未核实，请以官方渠道为准"
    assert ticket.source_ids == [] and ticket.confidence == 0
    assert any("等级不足" in w for w in state["warnings"])
    validate_facts(result)


def test_evidence_allows_chinese_layout_spaces_but_not_changed_content(document):
    document.sources[0].text = "测试展馆成人票 30 元，\n学生票 15 元。"
    validate_facts(document)
    document.pois[0].facts[0].value = "成人票31元，学生票15元"
    with pytest.raises(ValueError, match="数值与引文不符"):
        validate_facts(document)
    fact = document.pois[0].facts[0]
    fact.value = fact.evidence = "artist important"
    document.sources[0].text = "art is important"
    with pytest.raises(ValueError, match="证据无法"):
        validate_facts(document)
    fact.evidence = " \n "
    with pytest.raises(ValueError, match="证据无法"):
        validate_facts(document)


def test_source_trust_cannot_be_spoofed():
    assert source_level("https://test.gov.cn/a") == "L1"
    assert source_level("https://zh.wikipedia.org/wiki/a") == "L2"
    assert source_level("https://test.gov.cn.attacker.com") == "L3"
    assert source_level("https://gov.cn@attacker.com") == "L3"


async def test_osrm_failure_is_explicitly_estimated(tmp_path, document, monkeypatch):
    async def fail(*args, **kwargs):
        raise httpx.ConnectError("network unavailable")

    monkeypatch.setattr(httpx.AsyncClient, "get", fail)
    a = document.pois[0]
    b = a.model_copy(update={"id": "P2", "lng": 121.48})
    geo = Geo(Settings(_env_file=None), Store(tmp_path))
    segment = await geo.segment(a, b)
    assert segment.estimated and "直线距离" in segment.source
    assert segment.duration_min > 0 and segment.distance_m > 0


def test_render_two_formats_from_same_content(tmp_path, document):
    output = render(document, tmp_path)
    ppt = Presentation(tmp_path / output["pptx"])
    word = Document(tmp_path / output["docx"])
    assert round(ppt.slide_width / ppt.slide_height, 3) == round(16 / 9, 3)
    ppt_text = "\n".join(
        shape.text for slide in ppt.slides for shape in slide.shapes if shape.has_text_frame
    )
    word_text = "\n".join(p.text for p in word.paragraphs)
    assert "测试展馆成人票30元" in ppt_text and "测试展馆成人票30元" in word_text
    assert "S1" in ppt_text and "S1" in word_text
    assert "https://test.gov.cn/tickets" in word_text
    assert word.tables and "成人票30元" in word.tables[0].cell(1, 1).text
    assert "Source Han Sans SC" in ppt.slides[1]._element.xml
    assert 'a:ea typeface="Source Han Sans SC"' in ppt.slides[1]._element.xml
    saved = Deliverable.model_validate_json((tmp_path / output["json"]).read_text())
    assert saved.pois[0].facts[0].value == document.pois[0].facts[0].value
    with Image.open(tmp_path / "overview.png") as image:
        assert image.size == (1600, 1000)
    assert geojson(saved)["features"][0]["geometry"]["coordinates"] == [121.47, 31.23]


async def test_failed_node_resume_does_not_replay_completed_nodes(tmp_path):
    config = Settings(_env_file=None, data_dir=tmp_path)
    store = Store(tmp_path)
    store.save({"id": "resume", "prompt": "测试", "options": {}, "status": "queued"})
    harness = Harness(config, store)
    calls = []
    for name in NODES:

        async def node(job, state, folder, name=name):
            calls.append(name)
            if name == "Writer":
                raise ValueError("测试故障")

        setattr(harness, name.lower(), node)
    await harness.run("resume")
    assert store.get("resume")["status"] == "failed"
    assert calls == NODES[:5]

    async def fixed(job, state, folder):
        calls.append("Writer")

    harness.writer = fixed
    await harness.run("resume", "Writer")
    assert calls == NODES[:5] + NODES[4:]
    assert store.get("resume")["status"] == "completed"


def test_api_persistence_sse_and_download_guards(tmp_path):
    config = Settings(_env_file=None, data_dir=tmp_path, llm_api_key="", siliconflow_api_key="")
    app = create_app(config)
    with TestClient(app) as client:
        health = client.get("/api/health").json()
        assert health["configured"]["llm"] is False
        assert health["models"] == {
            "llm": config.llm_model,
            "embedding": config.embed_model,
            "rerank": config.rerank_model,
            "vision": config.vision_model,
        }
        assert client.post("/api/jobs", json={"prompt": ""}).status_code == 422
        result = client.post("/api/jobs", json={"prompt": "做一个上海的旅游攻略"})
        assert result.status_code == 202
        job_id = result.json()["job_id"]
        for _ in range(50):
            job = client.get(f"/api/jobs/{job_id}").json()
            if job["status"] == "failed":
                break
        assert job["status"] == "failed" and "密钥" in job["error"]
        event_response = client.get(f"/api/jobs/{job_id}/events")
        assert "text/event-stream" in event_response.headers["content-type"]
        assert "event: end" in event_response.text and "Router" in event_response.text
        replay = client.get(f"/api/jobs/{job_id}/events", headers={"Last-Event-ID": "999999"})
        assert "Router" not in replay.text
        assert client.get(f"/api/jobs/{job_id}/files/travel.docx").status_code == 409
        assert client.get("/api/jobs/missing").status_code == 404
        assert len(client.get("/api/jobs").json()) == 1


def test_restart_marks_unfinished_jobs_interrupted(tmp_path):
    store = Store(tmp_path)
    store.save({"id": "pending", "status": "running", "node": "Writer"})
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        assert client.get("/api/jobs/pending").json()["status"] == "interrupted"


@pytest.mark.parametrize('finished', [None, '2026-11-05T17:43:00+00:00'])
def test_restart_does_not_add_queue_time_to_previous_execution(tmp_path, finished):
    store = Store(tmp_path)
    store.save({'id':'waiting', 'status':'queued', 'node':'Researcher',
                'run_finished_at':finished})
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        job = client.get('/api/jobs/waiting').json()
        assert job['status'] == 'interrupted'
        assert job['run_finished_at'] == finished


@pytest.mark.parametrize(
    "scope, expected, version",
    [
        ("Assembler", ["Assembler"], "1.1"),
        ("Writer", ["Writer", "Reviewer", "Assembler"], "1.1"),
        ("Illustrator", ["Illustrator", "Reviewer", "Assembler"], "1.1"),
        ("Assembler", NODES[1:], "1.0"),
    ],
)
async def test_revision_only_replays_affected_nodes(tmp_path, document, scope, expected, version):
    store = Store(tmp_path)
    folder = tmp_path / "jobs" / "revision"
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(
        json.dumps(
            {
                "prd_version": version,
                "spec": document.spec.model_dump(mode="json"),
                "document": document.model_dump(mode="json"),
                "warnings": [],
                "outline": {"title": "测试版本"},
            }
        )
    )
    store.save(
        {
            "id": "revision",
            "prompt": "修改",
            "options": {},
            "status": "queued",
            "revision_instruction": "修改主题颜色",
        }
    )
    harness = Harness(Settings(_env_file=None, data_dir=tmp_path), store)

    async def plan(*args):
        return RevisionPlan(
            start_node=scope, reason="测试修改范围", spec=document.spec, primary_color="#123456"
        )

    harness.models.json = plan
    calls = []
    for name in NODES:

        async def node(job, state, folder, name=name):
            calls.append(name)
            assert state["primary_color"] == "#123456"

        setattr(harness, name.lower(), node)
    await harness.run("revision", "Revision")
    assert store.get("revision")["status"] == "completed"
    assert calls == expected


@pytest.mark.skipif(
    not os.environ.get("MILVUS_TEST_URI"), reason="Set MILVUS_TEST_URI for isolated DB integration"
)
async def test_real_milvus_ingest_recall_and_dimension_guard(document):
    from pymilvus import MilvusClient

    uri = os.environ["MILVUS_TEST_URI"]
    admin = MilvusClient(uri=uri)
    database = "test_travel_" + uuid.uuid4().hex
    admin.create_database(database)
    client = MilvusClient(uri=uri, db_name=database)

    class TestModels:
        async def embed(self, texts):
            return [[0.1, 0.3, 0.5, 0.7] for _ in texts]

        async def rerank(self, query, texts):
            return [{"index": i} for i in range(min(8, len(texts)))]

    kb = KnowledgeBase(Settings(_env_file=None, milvus_uri=uri), TestModels())
    kb.client = client
    try:
        assert await kb.ingest("测试城市", "测试展馆", document.sources[0]) > 0
        client.flush("travel_kb")
        hits = await kb.search("测试城市", "票价")
        assert hits and "30元" in hits[0]["text"]
        assert hits[0]["source_url"] == str(document.sources[0].url)
        assert await kb.search("其他城市", "票价") == []
        assert kb.coverage("测试城市")["chunks"] > 0
        with pytest.raises(ValueError, match="维度"):
            kb.ensure(8)
    finally:
        if client.has_collection("travel_kb"):
            client.drop_collection("travel_kb")
        client.close()
        admin.drop_database(database)
        admin.close()


def test_schema_error_reports_field_without_echoing_untrusted_input():
    from backend.tools.mcp_search import ToolCall

    with pytest.raises(ValidationError) as caught:
        ToolCall.model_validate({'name': 'fetch', 'arguments': 'private-page-input'})
    message = safe_error(caught.value, Settings(_env_file=None))
    assert 'ToolCall' in message and 'arguments' in message and 'dict_type' in message
    assert 'private-page-input' not in message


def test_null_fact_verification_date_uses_system_default_but_rejects_invalid_dates():
    base = {'field': 'ticket', 'value': '免费', 'source_ids': ['S1'], 'evidence': '免费参观'}
    fact = FactRecord.model_validate({**base, 'verified_at': None})
    assert fact.verified_at == date.today()
    assert FactRecord.model_validate({**base, 'verified_at': '2026-10-29'}).verified_at == date(2026, 10, 29)
    with pytest.raises(ValidationError):
        FactRecord.model_validate({**base, 'verified_at': 'not-a-date'})


@pytest.mark.parametrize('verified', [False, True])
def test_retry_assembly_with_missing_visit_evidence_returns_to_verifier(tmp_path, monkeypatch, verified):
    nodes = []

    async def run(self, job_id, start='Router'):
        nodes.append(start)

    monkeypatch.setattr(Harness, 'run', run)
    config = Settings(_env_file=None, data_dir=tmp_path)
    store = Store(tmp_path)
    store.save({'id': 'quality-retry', 'status': 'failed', 'node': 'Assembler'})
    folder = tmp_path / 'jobs' / 'quality-retry'
    folder.mkdir(parents=True)
    doc = Deliverable(prd_version='1.3', spec=TaskSpec(type='culture_intro', city='苏州'),
        pois=[POI(id='P1', name='测试馆', facts=[FactRecord(field=field, value='已核实',
              source_ids=['S1'] if verified else []) for field in ['ticket','hours','booking']])],
        sources=[SourceRef(id='S1', title='参观须知', url='https://museum.gov.cn/visit',
                           level='L1', fetched_at=now(), text='官方测试资料')])
    (folder / 'state.json').write_text(json.dumps({'document': doc.model_dump(mode='json')}))
    with TestClient(create_app(config)) as client:
        assert client.post('/api/jobs/quality-retry/retry').status_code == 202
        client.get('/api/health')
    assert nodes == ['Assembler' if verified else 'Verifier']
