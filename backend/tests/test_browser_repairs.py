from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from backend.app.config import Settings
from backend.harness.graph import Harness, safe_error
from backend.harness.schemas import POI, Block, Deliverable, TaskSpec
from backend.tools.mcp_search import MCPProvider, MCPToolError
from backend.tools.photos import subject_supported
from backend.tools.sources import source_level


async def test_remote_mcp_reuses_http_connection_pool_and_closes_on_shutdown(tmp_path, monkeypatch):
    import json

    config = tmp_path/'mcp.toml'
    config.write_text('[mcp_servers.exa]\nurl="https://example.com/mcp"\n')
    created = []
    real_client = httpx.AsyncClient

    def handle(request):
        body = json.loads(request.content)
        if 'id' not in body:
            return httpx.Response(202)
        result = ({'protocolVersion':'2025-06-18', 'capabilities':{'tools':{}},
                   'serverInfo':{'name':'test','version':'1'}} if body['method']=='initialize'
                  else {'tools':[]})
        return httpx.Response(200, json={'jsonrpc':'2.0','id':body['id'],'result':result})

    def client(**kwargs):
        value = real_client(transport=httpx.MockTransport(handle), **kwargs)
        created.append(value)
        return value

    monkeypatch.setattr('backend.tools.mcp_search.httpx.AsyncClient', client)
    provider = MCPProvider(Settings(_env_file=None, mcp_config_path=config), None)
    for _ in range(2):
        async with provider.session('exa') as session:
            assert not (await session.list_tools()).tools
    assert len(created) == 1
    assert not created[0].is_closed
    config.write_text('[mcp_servers.exa]\nurl="https://example.com/mcp"\nhttp_headers={Authorization="Bearer fixture-token"}\n')
    async with provider.session('exa') as session:
        assert not (await session.list_tools()).tools
    assert len(created) == 2
    assert 'Authorization' not in created[0].headers
    assert created[1].headers['Authorization'] == 'Bearer fixture-token'
    await provider.aclose()
    assert all(c.is_closed for c in created)


def test_culture_exports_disclose_unselected_candidates(tmp_path):
    from zipfile import ZipFile

    from docx import Document
    from lxml import etree

    from backend.render.slides_v11 import render_slides_v11
    from backend.render.word_v11 import render_word_v11

    alternatives = [{'name':f'其他候选地点{i}', 'reason':'资料尚不完整，未设独立参观专题。'} for i in range(11)]
    document = Deliverable(prd_version='1.3', spec=TaskSpec(type='culture_intro', city='鄂尔多斯'),
                           alternatives=alternatives)
    pptx, docx = tmp_path/'culture.pptx', tmp_path/'culture.docx'
    render_slides_v11(document, pptx, '176B5B')
    render_word_v11(document, docx, '176B5B')
    with ZipFile(pptx) as z:
        slide_text = '\n'.join(' '.join(etree.fromstring(z.read(n)).xpath('//*[local-name()="t"]/text()'))
                               for n in z.namelist() if n.startswith('ppt/slides/slide') and n.endswith('.xml'))
    word_text = '\n'.join(p.text for p in Document(docx).paragraphs)
    for item in alternatives:
        assert item['name'] in slide_text
        assert item['name'] in word_text


def test_government_linked_ordos_museum_domain_is_official():
    assert source_level('http://ordosbwg.org.cn/cgxz/notice.html') == 'L1'
    assert source_level('https://www.ordosbwg.org.cn/about/') == 'L1'
    assert source_level('https://ordosbwg.org.cn.example.com/') == 'L3'


@pytest.mark.parametrize('protection', ['none', 'named', 'revision'])
async def test_culture_keeps_supported_representatives_but_not_omit_requested_sites(tmp_path, monkeypatch, protection):
    from backend.harness.schemas import CultureChapter, FactRecord, SourceRef

    blocks = [Block(text=f'馆藏专题{i}。', source_ids=['S1']) for i in range(4)]
    address = FactRecord(field='address', value='鄂尔多斯市测试路1号',
                         evidence='鄂尔多斯市测试路1号', source_ids=['S1'])
    kept = POI(id='P1', name='资料完整馆', facts=[address], blocks=blocks)
    missing = POI(id='P2', name='资料缺失馆')
    chapters = [CultureChapter(title=f'文化主题{i}', blocks=[Block(text=f'文化专题{i}。', source_ids=['S1'])]) for i in range(5)]
    source = SourceRef(id='S1', title='官方资料', url='https://www.gov.cn/test', level='L1',
                       fetched_at='2026-11-04T00:00:00+00:00', text=address.evidence+'\n'+'\n'.join(b.text for b in blocks))
    document = Deliverable(prd_version='1.3', spec=TaskSpec(type='culture_intro', city='鄂尔多斯'),
                           pois=[kept, missing], sources=[source])

    async def model(instruction, data, schema):
        if "chapters" in schema.model_fields:
            return schema(chapters=chapters)
        assert len(data["facts"]) <= 2
        return schema(pois=[
            {"id": poi.id, "name": poi.name, "blocks": poi.blocks}
            for poi in [kept, missing]
        ])

    monkeypatch.setattr('backend.harness.graph.prepare', lambda doc: doc)
    state = {'document':document.model_dump(mode='json'), 'sources':[source.model_dump(mode='json')],
             'spec':document.spec.model_dump(mode='json'), 'outline':{}, 'research_replenishment':2}
    job = {'prompt':'介绍鄂尔多斯，至少5个文化主题'+('，必须包含资料缺失馆' if protection == 'named' else '')}
    if protection == 'revision':
        job['revision_instruction'] = '保留全部地点和事实，只改讲解风格'
    harness = SimpleNamespace(models=SimpleNamespace(json=model))
    if protection != 'none':
        with pytest.raises(ValueError, match='两轮补查后'):
            await Harness.writer(harness, job, state, tmp_path)
    else:
        await Harness.writer(harness, job, state, tmp_path)
        assert [p['name'] for p in state['document']['pois']] == ['资料完整馆']
        assert len(state['document']['chapters']) == 5
        assert any(p['name'] == '资料缺失馆' for p in state['document']['alternatives'])


@pytest.mark.parametrize('resume', [False, True])
async def test_visit_enrichment_preserves_verified_cultural_facts(tmp_path, resume):
    import json

    from backend.app.store import Store
    from backend.harness.enrichment import enrich
    from backend.harness.schemas import FactRecord, SourceRef

    history = FactRecord(field='历史沿革', value='测试馆成立于1952年。',
                         evidence='测试馆成立于1952年。', source_ids=['S1'])
    values = {'ticket':'免费', 'hours':'09:00–17:00', 'booking':'无需预约',
              'address':'鄂尔多斯市测试路1号'}
    visits = [FactRecord(field=k, value=v, evidence=v, source_ids=['S1']) for k, v in values.items()]
    original = Deliverable(prd_version='1.3', spec=TaskSpec(type='culture_intro', city='鄂尔多斯'),
                           pois=[POI(id='P1', name='测试馆', facts=[history])],
                           sources=[SourceRef(id='S1', title='测试馆', url='https://www.gov.cn/test', fetched_at='2026-11-04T00:00:00+00:00',
                                              level='L1', text=history.evidence+'\n'+'\n'.join(values.values()))])
    calls = 0

    async def model(instruction, data, schema):
        nonlocal calls
        calls += 1
        return schema(facts=visits)

    harness = SimpleNamespace(models=SimpleNamespace(json=model), store=Store(tmp_path))
    result = await enrich(harness, original.model_copy(deep=True), tmp_path, search_missing=False)
    if resume:
        checkpoint = tmp_path/'enrichment-progress.json'
        saved = json.loads(checkpoint.read_text())
        saved['document']['pois'][0]['facts'] = [f for f in saved['document']['pois'][0]['facts'] if f['field'] in values]
        checkpoint.write_text(json.dumps(saved))
        result = await enrich(harness, original.model_copy(deep=True), tmp_path, search_missing=False)
    assert calls == 1
    assert [f for f in result.pois[0].facts if f.field == '历史沿革'] == [history]
    assert {f.field:f.value for f in result.pois[0].facts if f.field in values} == values


@pytest.mark.parametrize('attempts', [0, 2])
async def test_culture_writer_backtracks_for_missing_poi_content_before_images(tmp_path, attempts):
    from backend.harness.graph import ResearchBacktrack

    document = Deliverable(prd_version='1.3', spec=TaskSpec(type='culture_intro', city='鄂尔多斯'),
                           pois=[POI(id='P1', name='测试博物馆')])

    async def model(instruction, data, schema):
        return schema(pois=[], chapters=[], practical=[])

    state = {'document':document.model_dump(mode='json'), 'sources':[],
             'spec':document.spec.model_dump(mode='json'), 'outline':{'queries':['旧查询']},
             'research_replenishment':attempts}
    harness = SimpleNamespace(models=SimpleNamespace(json=model))
    if attempts == 0:
        with pytest.raises(ResearchBacktrack, match='文化景点资料不足'):
            await Harness.writer(harness, {}, state, tmp_path)
        assert state['research_replenishment'] == 1
        assert '测试博物馆' in state['outline']['queries'][0]
        assert state['culture_research_targets'] == ['测试博物馆']
        assert not state.get('reuse_verified_pois')
    else:
        with pytest.raises(ValueError, match='两轮补查后'):
            await Harness.writer(harness, {}, state, tmp_path)
        assert state['research_replenishment'] == 2


@pytest.mark.parametrize("changed_source", [False, True])
async def test_culture_targeted_recheck_preserves_other_verified_places(
    tmp_path, monkeypatch, changed_source,
):
    from backend.harness.schemas import PracticalItem, SourceRef

    names = ["甲馆", "乙馆", "丙馆"]
    spec = TaskSpec(type="culture_intro", city="上海")
    source = SourceRef(
        id="S1", title="甲馆", url="https://example.gov.cn/visit", level="L1",
        text="甲馆馆藏介绍", fetched_at="2026-11-10T00:00:00+00:00",
    )
    previous = Deliverable(
        prd_version="1.3", spec=spec, sources=[source],
        pois=[POI(id=f"P{i}", name=name) for i, name in enumerate(names, 1)],
        practical=[PracticalItem(
            category="notice", label="甲馆 · 参观提示", value="甲馆馆藏介绍", source_ids=["S1"],
        )],
    )
    calls = []

    async def model(instruction, data, schema):
        calls.extend(data["outline"]["poi_names"])
        return schema(pois=[POI(id="X", name=name) for name in data["outline"]["poi_names"]])

    async def enrich(harness, document, folder, *, verified_names=()):
        assert set(verified_names) == (set() if changed_source else {"甲馆", "丙馆"})
        return document

    monkeypatch.setattr("backend.harness.graph.enrich", enrich)
    old = source.model_dump(mode="json")
    state = {
        "spec": spec.model_dump(mode="json"), "outline": {"poi_names": names},
        "sources": [{**old, "text": "已改写" if changed_source else old["text"]},
                    {**old, "id": "S2", "url": "https://example.gov.cn/new", "text": "乙馆新资料"}],
        "warnings": [], "prd_version": "1.3", "culture_research_targets": ["乙馆"],
        "document": previous.model_dump(mode="json"),
    }
    await Harness.verifier(SimpleNamespace(models=SimpleNamespace(json=model)), {}, state, tmp_path)
    assert calls == (names if changed_source else ["乙馆"])
    assert [p["id"] for p in state["document"]["pois"]] == ["P1", "P2", "P3"]
    assert len(state["document"]["practical"]) == (0 if changed_source else 1)
    assert "culture_research_targets" not in state


def test_municipal_photo_name_keeps_full_venue_identity():
    photo = {'title':'File:康巴什 鄂尔多斯市博物院 01.jpg',
             'subject_names':['鄂尔多斯博物院'], 'subject_city':'鄂尔多斯'}
    assert subject_supported(photo)
    assert not subject_supported({**photo, 'title':'File:鄂尔多斯市街景.jpg'})
    assert not subject_supported({**photo, 'title':'File:呼和浩特市博物院.jpg'})


@pytest.mark.parametrize('task_type', ['culture_intro', 'travel_guide'])
async def test_both_document_types_expand_missing_visit_details(tmp_path, monkeypatch, task_type):
    async def model(instruction, data, schema):
        return schema(pois=[])

    async def enrich(harness, document, folder, *, search_missing=True):
        assert search_missing is True
        return document

    monkeypatch.setattr('backend.harness.graph.enrich', enrich)
    state = {'spec':TaskSpec(type=task_type, city='鄂尔多斯').model_dump(mode='json'),
             'outline':{}, 'sources':[], 'warnings':[], 'prd_version':'1.3'}
    await Harness.verifier(SimpleNamespace(models=SimpleNamespace(json=model)), {}, state, tmp_path)


@pytest.mark.parametrize('context_only, expected_verifications', [(True, 1), (False, 2)])
async def test_research_backtrack_reuses_pois_only_for_context(tmp_path, context_only, expected_verifications):
    import asyncio

    from backend.app.store import Store
    from backend.harness.graph import ResearchBacktrack
    from backend.harness.schemas import NODES

    store = Store(tmp_path)
    store.save({'id':'test', 'status':'queued', 'node':'Verifier'})
    harness = Harness.__new__(Harness)
    harness.store = store
    harness.limit = asyncio.Semaphore(1)
    calls = []
    writes = 0

    async def stage(job, state, folder):
        nonlocal writes
        calls.append(job['node'])
        if job['node'] == 'Verifier':
            state['document'] = {'verified_pois':['P1']}
        if job['node'] == 'Writer':
            writes += 1
            assert state['document']['verified_pois'] == ['P1']
            if writes == 1:
                state['reuse_verified_pois'] = context_only
                raise ResearchBacktrack('补充研究')

    for node in NODES:
        setattr(harness, node.lower(), stage)
    await harness._run('test', 'Verifier')
    assert store.get('test')['status'] == 'completed'
    assert calls.count('Verifier') == expected_verifications
    assert calls.count('Researcher') == 1
    assert 'Researcher' in store.get('test')['planned_nodes']


async def test_revision_writer_receives_existing_cited_highlights(tmp_path):
    original = Block(text='馆藏背景与当期展览应区分。', source_ids=['S1'])
    document = Deliverable(prd_version='1.3', spec=TaskSpec(type='travel_guide', city='上海'),
                           pois=[POI(id='P1', name='测试馆', blocks=[original])])

    class Captured(Exception):
        pass

    async def model(instruction, data, schema):
        assert data['facts'][0]['existing_blocks'] == [original.model_dump(mode='json')]
        raise Captured

    harness = SimpleNamespace(models=SimpleNamespace(json=model))
    state = {'document':document.model_dump(mode='json'), 'sources':[],
             'spec':document.spec.model_dump(mode='json'), 'outline':{}}
    with pytest.raises(Captured):
        await Harness.writer(harness, {'revision_instruction':'只改讲解风格，保留事实'}, state, tmp_path)


async def test_mcp_call_normalizes_session_wrapped_tool_failure():
    class Models:
        async def json(self, *args):
            return SimpleNamespace(name='search', arguments={'query': '包头 音乐广场'})

    class Session:
        async def list_tools(self):
            tool = SimpleNamespace(name='search', inputSchema={'type': 'object'})
            tool.model_dump = lambda: {'name': 'search', 'inputSchema': tool.inputSchema}
            return SimpleNamespace(tools=[tool])

        async def call_tool(self, *args):
            return SimpleNamespace(isError=True)

    @asynccontextmanager
    async def session(server):
        try:
            yield Session()
        except Exception as error:
            raise ExceptionGroup('session cleanup', [error]) from error

    provider = MCPProvider(Settings(_env_file=None), Models())
    provider.session = session
    with pytest.raises(MCPToolError):
        await provider.call('search', '补查官方渠道')


async def test_mcp_call_does_not_hide_wrapped_programming_failure():
    provider = MCPProvider(Settings(_env_file=None), None)

    async def broken(*args, **kwargs):
        raise ExceptionGroup('unexpected', [TypeError('programming error')])

    provider._call = broken
    with pytest.raises(ExceptionGroup):
        await provider.call('search', '补查官方渠道')


async def test_unread_streaming_429_can_be_reported_in_nested_mcp_errors(tmp_path, monkeypatch):
    from backend.app.store import Store
    from backend.harness.progress import progress_snapshot

    request = httpx.Request('POST', 'https://example.org/mcp')
    response = httpx.Response(429, request=request, stream=httpx.ByteStream(b'private body'))
    error = httpx.HTTPStatusError('rate limited', request=request, response=response)
    grouped = ExceptionGroup('MCP task', [ExceptionGroup('transport', [error])])
    message = safe_error(grouped, Settings(_env_file=None))
    assert '429' in message and '稍后' in message
    assert 'private body' not in message
    store = Store(tmp_path)
    store.save({'id':'stream-error', 'status':'queued', 'node':'Researcher', 'kind':'document'})
    harness = Harness(Settings(_env_file=None), store)

    async def failed_search(*args):
        raise grouped

    monkeypatch.setattr(harness, 'researcher', failed_search)
    await harness.run('stream-error', 'Researcher')
    job = store.get('stream-error')
    assert job['status'] == 'failed' and job['run_finished_at']
    assert store.events('stream-error')[-1]['status'] == 'failed'
    assert progress_snapshot(job, store.events('stream-error'), {})['estimated_finish_at'] is None


@pytest.mark.parametrize('preference,expected', [
    ('步行优先，公共交通补充', True), ('优先步行，公共交通补充', True),
    ('以步行为主，公共交通补充', True), ('优先公共交通', False),
])
def test_walking_primary_does_not_require_public_transport_quota(preference, expected):
    from backend.harness.quality import public_transport_sufficient

    segment=SimpleNamespace(geometry_mode='foot',duration_min=30,transit={'confidence':'fallback'})
    day=SimpleNamespace(arrival_segment=None,segments=[segment])
    assert public_transport_sufficient(TaskSpec(type='travel_guide',city='上海',
                                               transport_preference=preference),[day]) is expected


async def test_walking_primary_uses_feasible_walk_before_querying_transit(tmp_path, monkeypatch):
    from backend.app.store import Store
    from backend.harness.schemas import Segment
    from backend.tools.transit import OfficialHintProvider, TransitProvider

    async def walk(geo,a,b,foot):
        assert foot
        return Segment(from_poi=a.id,to_poi=b.id,from_name=a.name,to_name=b.name,
                       mode='步行',duration_min=30,distance_m=2000,
                       polyline=[[a.lng,a.lat],[b.lng,b.lat]],source='fixture',
                       route_verified=True,geometry_mode='foot')

    async def unexpected(*args):
        pytest.fail('A feasible primary walk should be used before transit research')

    monkeypatch.setattr('backend.tools.planning.routed',walk)
    monkeypatch.setattr(OfficialHintProvider,'get_route',unexpected)
    geo=SimpleNamespace(store=Store(tmp_path),config=Settings(_env_file=None),
                        configured_preference='步行优先，公共交通补充')
    a,b=POI(id='A',name='起点',lat=31,lng=121),POI(id='B',name='终点',lat=31,lng=121.02)
    result=await TransitProvider(geo,None).get_route(a,b)
    assert result.geometry_mode == 'foot' and result.route_verified


@pytest.mark.parametrize('unavailable',[False,True])
async def test_walking_primary_can_use_public_transport_when_walk_is_not_feasible(tmp_path, monkeypatch, unavailable):
    from backend.app.store import Store
    from backend.harness.schemas import Segment
    from backend.tools.transit import OfficialHintProvider, TransitProvider

    a,b=POI(id='A',name='起点',lat=31,lng=121),POI(id='B',name='终点',lat=31,lng=121.02)
    base=dict(from_poi=a.id,to_poi=b.id,from_name=a.name,to_name=b.name,
              distance_m=3000,polyline=[[a.lng,a.lat],[b.lng,b.lat]],source='fixture',route_verified=True)

    async def walk(*args):
        if unavailable:
            raise ValueError('walk unavailable')
        return Segment(**base,mode='步行',duration_min=60,geometry_mode='foot')

    async def transit(*args):
        return Segment(**base,mode='地铁',duration_min=20,geometry_mode='subway')

    monkeypatch.setattr('backend.tools.planning.routed',walk)
    monkeypatch.setattr(OfficialHintProvider,'get_route',transit)
    geo=SimpleNamespace(store=Store(tmp_path),config=Settings(_env_file=None),
                        configured_preference='步行优先，公共交通补充')
    assert (await TransitProvider(geo,None).get_route(a,b)).geometry_mode == 'subway'


def test_quota_429_is_actionable_and_does_not_echo_response_secrets():
    response = httpx.Response(429, request=httpx.Request('POST', 'https://example.org/v1/chat/completions'),
                             text='Free usage limit reached. Secret key sk-test-private')
    error = httpx.HTTPStatusError('request failed', request=response.request, response=response)
    message = safe_error(error, Settings(_env_file=None))
    assert '额度' in message and '恢复' in message and '429' in message
    assert 'sk-test-private' not in message


def test_ordinary_rate_limit_is_distinguished_from_exhausted_quota():
    response = httpx.Response(429, request=httpx.Request('POST', 'https://example.org/v1/chat/completions'),
                             text='Too many requests')
    error = httpx.HTTPStatusError('request failed', request=response.request, response=response)
    message = safe_error(error, Settings(_env_file=None))
    assert '频繁' in message and '额度已' not in message


async def test_commons_search_requests_chinese_subject_metadata():
    from backend.tools.photos import Photos

    photos = Photos(None, None, None)
    calls = []

    async def api(url, params, provider):
        calls.append(params)
        description = ('现存于鄂尔多斯青铜器博物馆。' if params.get('iiextmetadatalanguage') == 'zh'
                       else 'Preserved in the Ordos Bronze Ware Museum.')
        return {'query': {'pages': {'1': {
            'title': 'File:Bronze arrowheads.jpg',
            'imageinfo': [{'url': 'https://example.org/photo.jpg',
                           'descriptionurl': 'https://example.org/page',
                           'extmetadata': {'ImageDescription': {'value': description}}}],
        }}}}

    photos.api = api
    batches = photos.search('鄂尔多斯', '鄂尔多斯青铜器博物馆')
    rows = await anext(batches)
    await batches.aclose()
    assert subject_supported(rows[0])
    assert calls[0]['iiextmetadatalanguage'] == 'zh'


async def test_captcha_pages_are_not_reused_or_stored_as_source(tmp_path, monkeypatch):
    from backend.app.store import Store
    from backend.tools.fetch import FetchProvider

    store = Store(tmp_path)
    url = 'https://museum.example/visit'
    key = store.key('fetch', url)
    store.put_cache(key, '# 🧩 请完成拼图验证\n\n拖动蓝色拼图块到正确位置')
    provider = FetchProvider(Settings(_env_file=None), store)
    assert provider.cached_page(key) is None

    async def checked(*args, **kwargs):
        pass

    def response(request):
        if request.url.path == '/robots.txt':
            return httpx.Response(404)
        return httpx.Response(200, text='<html><title>🧩 请完成拼图验证</title><article><h1>🧩 请完成拼图验证</h1><p>请将下方的拼图块拖动到图片中的正确位置。拖动蓝色拼图块到正确位置。剩余尝试次数3。</p></article></html>')

    real = httpx.AsyncClient
    monkeypatch.setattr('backend.tools.fetch.public_url', checked)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real(transport=httpx.MockTransport(response), **kwargs))
    with pytest.raises(ValueError, match='验证页面'):
        await provider.fetch(url)


def test_kb_coverage_excludes_old_captcha_rows():
    from backend.kb.ingest import KnowledgeBase

    kb = KnowledgeBase(None, None)
    kb.client = SimpleNamespace(
        has_collection=lambda _: True,
        query=lambda *a, **kw: [
            {'source_url':'https://museum.example/visit', 'fetched_at':'2026-11-04', 'text':'# 🧩 请完成拼图验证\n拖动拼图块'},
            {'source_url':'https://museum.example/about', 'fetched_at':'2026-11-04', 'text':'博物馆基本展览介绍'},
        ],
    )
    result = kb.coverage('呼和浩特')
    assert result['chunks'] == 1
    assert result['sources'] == ['https://museum.example/about']


@pytest.mark.parametrize('days', [1, 2])
async def test_route_planner_rejects_lunchless_full_day_before_network_and_replacements(tmp_path, monkeypatch, days):
    from datetime import date

    import backend.tools.planning_v13 as planning
    from backend.harness.schemas import OpeningHours

    async def origin(*args):
        return POI(id='ORIGIN', name='车站', lat=40, lng=109, location_verified=True)

    class Routes:
        def __init__(self, *args):
            pass

        async def get_route(self, *args):
            pytest.fail('Without any dining data this full-day order cannot pass publication')

    monkeypatch.setattr(planning, 'origin_point', origin)
    monkeypatch.setattr(planning, 'TransitProvider', Routes)
    pois = [POI(id=str(i), name=f'场馆{i}', lat=40, lng=109 + i * .001,
                location_verified=True, duration_min=120,
                hours=OpeningHours(open='09:00', close='18:00')) for i in range(days * 3)]
    if days == 2:
        pois[0].dining = [{'name':'已定位餐厅', 'source':'https://www.openstreetmap.org/node/1'}]
    spec = TaskSpec(type='travel_guide', city='包头', days=days, start_date=date(2026, 11, 14))
    with pytest.raises(planning.CandidateShortage, match='餐饮'):
        await planning.plan(SimpleNamespace(), spec, pois, [], None, tmp_path)


async def test_missing_meal_venues_do_not_trigger_unrelated_poi_research(tmp_path, monkeypatch):
    import backend.tools.planning_v13 as planning
    from backend.app.store import Store

    async def unavailable(*args, **kwargs):
        raise planning.MealVenueShortage('2天行程仅1处候选有附近具名餐饮资料')

    monkeypatch.setattr('backend.harness.graph.plan_itinerary', unavailable)
    h = Harness(Settings(_env_file=None, data_dir=tmp_path), Store(tmp_path))
    d = Deliverable(prd_version='1.3', spec=TaskSpec(type='travel_guide', city='包头', days=2))
    try:
        with pytest.raises(ValueError, match='餐饮资料') as error:
            await h.routeplanner({'id':'fixture'}, {'document':d.model_dump(mode='json'),
                                 'candidate_funnel':{'pool':[{'name':'其他候选公园'}]},
                                 'outline':{'poi_names':[]}}, tmp_path)
        assert type(error.value) is ValueError
    finally:
        await h.models.aclose()


@pytest.mark.parametrize('rounds', [0, 2])
@pytest.mark.parametrize('stage', ['writer', 'routeplanner'])
async def test_inadequate_visit_facts_are_researched_before_route_calls(tmp_path, monkeypatch, rounds, stage):
    from backend.app.store import Store
    from backend.harness.graph import ResearchBacktrack

    async def must_not_route(*args, **kwargs):
        pytest.fail('Even the best six candidates cannot meet the existing 75% fact gate')

    monkeypatch.setattr('backend.harness.graph.plan_itinerary', must_not_route)
    h = Harness(Settings(_env_file=None, data_dir=tmp_path), Store(tmp_path))
    h.models.json = must_not_route
    d = Deliverable(prd_version='1.3', spec=TaskSpec(type='travel_guide', city='包头', days=2),
                    pois=[POI(id=str(i), name=f'场馆{i}') for i in range(8)])
    state = {'document':d.model_dump(mode='json'), 'outline':{'poi_names':[p.name for p in d.pois]},
             'research_replenishment':rounds, 'reuse_verified_pois':True, 'sources':[], 'spec':d.spec.model_dump(mode='json')}
    try:
        expected = ResearchBacktrack if rounds == 0 else ValueError
        with pytest.raises(expected, match='参观资料'):
            await getattr(h, stage)({'id':'fixture'}, state, tmp_path)
        if rounds == 0:
            assert state['research_replenishment'] == 1
            assert not state.get('reuse_verified_pois')
            assert all(any(p.name in query for query in state['outline']['queries']) for p in d.pois)
            assert state['outline']['poi_names'] == [p.name for p in d.pois]
    finally:
        await h.models.aclose()


async def test_route_shortage_does_not_backtrack_to_unlocatable_candidates(tmp_path, monkeypatch):
    from backend.app.store import Store
    from backend.tools.planning_v13 import CandidateShortage

    async def shortage(*args, **kwargs):
        raise CandidateShortage('所有候选组合均不能满足每天至少3站')

    monkeypatch.setattr('backend.harness.graph.plan_itinerary', shortage)
    monkeypatch.setattr('backend.harness.graph.require_visit_coverage', lambda *args: None)
    h = Harness(Settings(_env_file=None, data_dir=tmp_path), Store(tmp_path))
    d = Deliverable(prd_version='1.3', spec=TaskSpec(type='travel_guide', city='上海', days=2),
                    pois=[POI(id=str(i), name=f'场馆{i}') for i in range(6)])
    state = {'document':d.model_dump(mode='json'), 'outline':{'poi_names':[p.name for p in d.pois]},
             'research_replenishment':0, 'sources':[], 'spec':d.spec.model_dump(mode='json'),
             'candidate_funnel': {'pool': [
                 {'name': '场馆0', 'lat': 31.2},
                 {'name': '无坐标候选', 'lat': None},
             ]}}
    with pytest.raises(ValueError, match='可定位0个'):
        await h.routeplanner({'id':'fixture'}, state, tmp_path)
    assert state['research_replenishment'] == 0
    await h.models.aclose()


async def test_unselected_unverified_candidates_do_not_block_route_search(tmp_path, monkeypatch):
    from backend.app.store import Store
    from backend.harness.schemas import FactRecord, SourceRef

    async def route_called(*args, **kwargs):
        raise RuntimeError('route search reached')

    monkeypatch.setattr('backend.harness.graph.plan_itinerary', route_called)
    h = Harness(Settings(_env_file=None, data_dir=tmp_path), Store(tmp_path))
    pois = [POI(id=str(i), name=f'场馆{i}', facts=[
        FactRecord(field=field, value='有来源资料', source_ids=['S1'])
        for field in ('ticket', 'hours', 'booking')
    ] if i < 6 else []) for i in range(12)]
    d = Deliverable(prd_version='1.3', spec=TaskSpec(type='travel_guide', city='包头', days=2),
                    pois=pois, sources=[SourceRef(id='S1', title='参观须知', url='https://example.gov.cn/visit',
                                                level='L1', fetched_at='2026-11-04', text='来源夹具')])
    try:
        with pytest.raises(RuntimeError, match='route search reached'):
            await h.routeplanner({'id':'fixture'}, {'document':d.model_dump(mode='json')}, tmp_path)
    finally:
        await h.models.aclose()


@pytest.mark.parametrize('name,other_city', [('青春广场', '怀化'), ('迎宾广场', '北京')])
@pytest.mark.parametrize('local', [False, True])
def test_public_space_facts_require_target_city_context(name, other_city, local):
    from backend.harness.enrichment import source_matches_poi, supported_visit_fact
    from backend.harness.schemas import FactRecord, SourceRef

    source_city = '包头' if local else other_city
    source = SourceRef(id='S1', title=f'{source_city}{name}介绍', url='https://example.gov.cn/visit',
                       level='L1', fetched_at='2026-11-04', text=f'{source_city}{name}\n开放时间：全天开放')
    poi = POI(id='P1', name=name)
    fact = FactRecord(field='hours', value='全天开放', source_ids=['S1'], evidence='开放时间：全天开放')
    assert source_matches_poi(source, poi, '包头') is local
    assert supported_visit_fact(fact, source, poi, '包头') is local


@pytest.mark.parametrize('kind', ['foreign', 'group'])
async def test_legacy_completed_enrichment_rechecks_unsupported_visit_facts(tmp_path, kind):
    import json

    from backend.app.store import Store
    from backend.harness.enrichment import enrich
    from backend.harness.schemas import FactRecord, SourceRef

    calls = 0

    async def extract(*args):
        nonlocal calls
        calls += 1
        return args[2](facts=[])

    h = Harness(Settings(_env_file=None, data_dir=tmp_path), Store(tmp_path))
    h.models.json = extract
    source = SourceRef(id='S1', title='北京世界公园', url='https://example.gov.cn/visit', level='L1',
                       fetched_at='2026-11-04', text='北京世界公园设有迎宾广场。开放时间：非全时开放')
    name = '迎宾广场'
    fact = FactRecord(field='hours', value='非全时开放', source_ids=['S1'], evidence='开放时间：非全时开放')
    if kind == 'group':
        name = '包头博物馆'
        source.title = name
        source.text = '单位或旅行社组织团体参观，需提前三天电话预约。'
        fact = FactRecord(field='booking', value='普通观众免费免票，团体需提前三天预约',
                          source_ids=['S1'], evidence=source.text)
    d = Deliverable(prd_version='1.3', spec=TaskSpec(type='travel_guide', city='包头'),
                    pois=[POI(id='P1', name=name)], sources=[source])
    try:
        await enrich(h, d, tmp_path, search_missing=False)
        previous_calls = calls
        progress_path = tmp_path/'enrichment-progress.json'
        progress = json.loads(progress_path.read_text())
        progress['document']['pois'][0]['facts'] = [fact.model_dump(mode='json')]
        progress_path.write_text(json.dumps(progress, ensure_ascii=False))
        result = await enrich(h, d, tmp_path, search_missing=False)
        assert calls > previous_calls
        assert not any(f.source_ids for f in result.pois[0].facts)
    finally:
        await h.models.aclose()


@pytest.mark.parametrize('value', ['团体需提前三天预约', '普通观众免费免票，团体需提前三天预约'])
def test_group_only_evidence_cannot_confirm_ordinary_booking(value):
    from backend.harness.enrichment import supported_visit_fact
    from backend.harness.schemas import FactRecord, SourceRef

    evidence = '本馆实行免费免票。单位或旅行社组织团体参观，需提前三天电话预约。'
    source = SourceRef(id='S1', title='包头博物馆参观须知', url='https://example.gov.cn/visit',
                       level='L1', fetched_at='2026-11-04', text=evidence)
    fact = FactRecord(field='booking', value=value, evidence=evidence, source_ids=['S1'])
    assert not supported_visit_fact(fact, source, POI(id='P1', name='包头博物馆'), '包头')


def test_explicit_ordinary_and_group_booking_evidence_is_accepted():
    from backend.harness.enrichment import supported_visit_fact
    from backend.harness.schemas import FactRecord, SourceRef

    evidence = '普通观众无需预约，团体需提前三天电话预约。'
    source = SourceRef(id='S1', title='包头博物馆参观须知', url='https://example.gov.cn/visit',
                       level='L1', fetched_at='2026-11-04', text=evidence)
    fact = FactRecord(field='booking', value=evidence, evidence=evidence, source_ids=['S1'])
    assert supported_visit_fact(fact, source, POI(id='P1', name='包头博物馆'), '包头')


async def test_dining_uses_traceable_places_when_osm_is_empty(tmp_path, monkeypatch):
    from backend.app.store import Store
    from backend.harness.visit_context import dining_near

    raw = '''Title: Nearby restaurants - Exa Places
URL: https://exa.ai/library/places?q=fixture
Highlights:
## 近处面馆
- Categories: Chinese Restaurant
- Address: 实际街道1号, CN (40.64018, 109.83424)
## 超距餐厅
- Categories: Chinese Restaurant
- Address: 另一街道, CN (40.69000, 109.89000)
## 同坐标非餐厅
- Categories: Bank
- Address: 实际街道1号, CN (40.64018, 109.83424)
'''
    calls = []

    async def overpass(query):
        return {'elements':[]}

    async def search(query):
        calls.append(query)
        return raw

    h = SimpleNamespace(store=Store(tmp_path), osm=SimpleNamespace(overpass=overpass),
                        search=SimpleNamespace(search=search))
    p = POI(id='P1', name='音乐广场', lat=40.6407164, lng=109.834815)
    rows = await dining_near(h, p, '包头')
    assert [r['name'] for r in rows] == ['近处面馆']
    assert rows[0]['distance_m'] < 100
    assert rows[0]['source'] == 'https://exa.ai/library/places?q=fixture'
    assert rows[0]['provider'] == 'Exa Places'
    assert await dining_near(h, p) == rows
    assert len(calls) == 1
    assert '包头 音乐广场' in calls[0]


async def test_places_food_keeps_l3_and_all_shared_page_records(tmp_path, monkeypatch):
    from datetime import date

    from backend.harness.visit_context import Context, enrich_context

    async def model(*args, **kwargs):
        return Context()

    async def dining(*args):
        return [{'name':n, 'lat':40., 'lng':109., 'distance_m':10, 'kind':'Restaurant',
                 'source':'https://exa.ai/library/places?q=fixture', 'provider':'Exa Places',
                 'evidence':n + '；坐标40.0,109.0'} for n in ['甲餐厅', '乙餐厅']]

    monkeypatch.setattr('backend.harness.visit_context.dining_near', dining)
    d = Deliverable(prd_version='1.3', spec=TaskSpec(type='travel_guide', city='包头', start_date=date(2026,11,14)),
                    pois=[POI(id='P1',name='目标馆',lat=40,lng=109)])
    result = await enrich_context(SimpleNamespace(models=SimpleNamespace(json=model)), d, tmp_path)
    place = next(s for s in result.sources if 'exa.ai' in str(s.url))
    assert place.level == 'L3'
    assert '甲餐厅' in place.text and '乙餐厅' in place.text


async def test_overpass_posts_query_without_url_filter_rejection(tmp_path, monkeypatch):
    from urllib.parse import parse_qs

    from backend.app.store import Store
    from backend.tools.osm import OSM

    query = '[out:json];node(1);out;'
    requests = []

    def respond(request):
        requests.append(request)
        if request.method != 'POST':
            return httpx.Response(406)
        assert parse_qs(request.content.decode())['data'] == [query]
        return httpx.Response(200, json={'elements':[{'type':'node','id':1}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(respond),**kw))
    osm = OSM(Settings(_env_file=None),Store(tmp_path))
    assert (await osm.overpass(query))['elements'][0]['id'] == 1
    assert await osm.overpass(query) == {'elements':[{'type':'node','id':1}]}
    assert len(requests) == 1


@pytest.mark.parametrize('engine', ['direct', 'graph'])
@pytest.mark.parametrize('gap', ['none', 'after_trip', 'inside_trip', 'out_of_order'])
async def test_bus_platform_members_are_ordered_stops_with_real_geometry(tmp_path, monkeypatch, engine, gap):
    from backend.app.store import Store
    from backend.harness.schemas import Segment
    from backend.tools.planning import transit_segment
    from backend.tools.transit import TransitProvider

    async def walk(geo, a, b, foot=True):
        if abs(a.lng-b.lng) > .002:
            raise ValueError('station exceeds walking limit')
        return Segment(from_poi=a.id,to_poi=b.id,duration_min=2,distance_m=80,
                       source='walk fixture',route_verified=True,geometry_mode='foot',
                       polyline=[[a.lng,a.lat],[(a.lng+b.lng)/2,a.lat],[b.lng,b.lat]])

    monkeypatch.setattr('backend.tools.planning.routed',walk)
    relation={'type':'relation','id':15656340,'tags':{'route':'bus','ref':'1'},'members':[
        {'type':'node','ref':1,'role':'platform_entry_only','name':'起站','lat':40.,'lon':109.},
        {'type':'node','ref':2,'role':'platform','name':'中站','lat':40.,'lon':109.01},
        {'type':'node','ref':3,'role':'platform_exit_only','name':'终站','lat':40.,'lon':109.02},
        {'type':'way','role':'','geometry':[{'lat':40.,'lon':109.},{'lat':40.,'lon':109.01},{'lat':40.,'lon':109.02}]},
    ]}
    if gap == 'after_trip':
        relation['members'].append({'type':'way','role':'','geometry':[{'lat':40.,'lon':110.},{'lat':40.,'lon':110.01}]})
    elif gap == 'inside_trip':
        relation['members'][-1]['geometry'] = [{'lat':40.,'lon':109.},{'lat':40.,'lon':109.01}]
        relation['members'].append({'type':'way','role':'','geometry':[{'lat':40.,'lon':109.015},{'lat':40.,'lon':109.02}]})
    elif gap == 'out_of_order':
        relation['members'][-1]['geometry'] = [{'lat':40.,'lon':109.},{'lat':40.,'lon':109.005}]
        relation['members'].append({'type':'way','role':'','geometry':[{'lat':40.,'lon':109.015},{'lat':40.,'lon':109.02}]})
        relation['members'].append({'type':'way','role':'','geometry':[{'lat':40.,'lon':109.005},{'lat':40.,'lon':109.01},{'lat':40.,'lon':109.015}]})
    geo=SimpleNamespace(config=Settings(_env_file=None),store=Store(tmp_path))
    a,b=POI(id='A',name='甲',lat=40,lng=109),POI(id='B',name='乙',lat=40,lng=109.02)
    data={'elements':[relation]}
    route=transit_segment if engine=='direct' else lambda geo,a,b,data:TransitProvider(geo,None).osm_route(a,b,data)
    result=await route(geo,a,b,data)
    if gap == 'inside_trip':
        assert result is None if engine == 'direct' else result.geometry_mode == 'transit_schematic'
        return
    assert result and result.geometry_mode=='bus'
    assert result.transit['confidence']=='osm_graph'
    assert result.transit['legs'][1]['stops']==2
    assert await route(geo,b,a,data) is None


async def test_route_search_tries_another_order_to_meet_public_transport_coverage(tmp_path, monkeypatch):
    from datetime import date

    import backend.tools.planning_v13 as planning
    from backend.app.store import Store
    from backend.harness.schemas import OpeningHours, Segment

    async def origin(*args):
        return POI(id='ORIGIN',name='站',lat=40,lng=109,location_verified=True)

    class Routes:
        def __init__(self,*args):
            pass

        async def get_route(self,a,b,*args):
            good = a.id != 'ORIGIN' or b.id == 'B'
            return Segment(from_poi=a.id,to_poi=b.id,duration_min=20,distance_m=800,
                           source='真实路由夹具',route_verified=True,geometry_mode='bus' if good else 'car',
                           polyline=[[a.lng,a.lat],[(a.lng+b.lng)/2,40.0001],[b.lng,b.lat]],
                           transit={'confidence':'osm_graph' if good else 'fallback','note':'估算'})

    async def basemap(*args):
        return {'image':'fixture.png'}

    async def replacements(*args):
        return []

    monkeypatch.setattr(planning,'origin_point',origin)
    monkeypatch.setattr(planning,'TransitProvider',Routes)
    monkeypatch.setattr(planning,'verified_replacements',replacements)
    pois=[POI(id=n,name=n,lat=40,lng=109+(i+1)*.001,location_verified=True,
              duration_min=30,hours=OpeningHours(open='09:00',close='18:00')) for i,n in enumerate('ABC')]
    days,_,_=await planning.plan(SimpleNamespace(store=Store(tmp_path)),
              TaskSpec(type='travel_guide',city='包头',days=1,start_date=date(2026,11,14)),
              pois,[],SimpleNamespace(basemap=basemap),tmp_path)
    assert days[0].stops[0].id == 'B'


async def test_route_search_covers_different_stop_sets_before_requesting_more_pois(tmp_path, monkeypatch):
    from datetime import date

    import backend.tools.planning_v13 as planning
    from backend.app.store import Store
    from backend.harness.schemas import OpeningHours, Segment
    from backend.tools.planning_v13 import plan

    async def origin(*args):
        return POI(id='ORIGIN',name='车站',lat=40,lng=109,location_verified=True)

    class Routes:
        def __init__(self,*args):
            pass

        async def get_route(self,a,b,*args):
            return Segment(from_poi=a.id,to_poi=b.id,duration_min=50 if 'A' in {a.id,b.id} else 5,
                           distance_m=500,source='路由夹具',route_verified=True,geometry_mode='foot',
                           polyline=[[a.lng,a.lat],[(a.lng+b.lng)/2,40.0001],[b.lng,b.lat]],
                           transit={'confidence':'fallback','note':'步行估算'})

    async def basemap(*args):
        return {'image':'fixture.png'}

    async def replacements(*args):
        return []

    monkeypatch.setattr(planning,'origin_point',origin)
    monkeypatch.setattr(planning,'TransitProvider',Routes)
    monkeypatch.setattr(planning,'verified_replacements',replacements)
    pois=[POI(id=n,name=n,lat=40,lng=109+(i+1)*.001,location_verified=True,
              duration_min=30,planning_priority=100-i*10,
              hours=OpeningHours(open='09:00',close='18:00')) for i,n in enumerate('ABCD')]
    days,_,_=await plan(SimpleNamespace(store=Store(tmp_path)),
              TaskSpec(type='travel_guide',city='包头',days=1,start_date=date(2026,11,14)),
              pois,[],SimpleNamespace(basemap=basemap),tmp_path)
    assert {p.id for p in days[0].stops} == set('BCD')


@pytest.mark.parametrize('authenticated', [False, True])
def test_streaming_exa_429_explains_anonymous_limit_without_leaking_credentials(authenticated):
    from backend.harness.graph import safe_error

    headers = {'Authorization': 'Bearer private-test-key'} if authenticated else {}
    request = httpx.Request('POST', 'https://mcp.exa.ai/mcp', headers=headers)
    response = httpx.Response(429, request=request, stream=httpx.ByteStream(b'unread'))
    error = httpx.HTTPStatusError('rate limited', request=request, response=response)
    message = safe_error(error, Settings(_env_file=None))
    assert '429' in message and 'private-test-key' not in message
    if authenticated:
        assert '匿名' not in message
    else:
        assert 'Exa' in message and '匿名' in message and 'API key' in message
