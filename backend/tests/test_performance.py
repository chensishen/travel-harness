import asyncio
import json

import httpx
import pytest

from backend.app.config import Settings
from backend.app.store import Store
from backend.harness.graph import Harness
from backend.harness.metrics import job_metrics
from backend.harness.progress import progress_snapshot
from backend.harness.schemas import PHOTO_SKIPPED, POI, Deliverable, ImageRef, TaskSpec
from backend.tools.fetch import FetchProvider
from backend.tools.llm import Models


async def test_transit_graph_build_does_not_block_loop_and_reuses_unchanged_network(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    from backend.tools.transit import TransitProvider

    calls = []

    def section(parts, start, end):
        calls.append((start.id, end.id))
        time.sleep(.08)
        return [[start.lng, start.lat], [end.lng, end.lat]]

    async def no_walk(*args):
        raise ValueError('no walk in graph-build test')

    monkeypatch.setattr('backend.tools.planning.route_section', section)
    monkeypatch.setattr('backend.tools.planning.routed', no_walk)
    nodes = [{'type':'node', 'ref':i, 'lat':31, 'lon':121+i*.01,
              'role':'stop', 'name':f'站{i}'} for i in range(3)]
    relation = {'type':'relation', 'id':1, 'tags':{'route':'subway','ref':'1'},
                'members':[*nodes, {'type':'way', 'geometry':[
                    {'lat':31,'lon':121}, {'lat':31,'lon':121.02}]}]}
    data = {'elements':[relation]}
    provider = TransitProvider(SimpleNamespace(config=Settings(_env_file=None), store=Store(tmp_path)), None)
    a = POI(id='A',name='起点',lat=31,lng=121)
    b = POI(id='B',name='终点',lat=31,lng=121.02)
    started = time.monotonic()
    task = asyncio.create_task(provider.osm_route(a,b,data))
    await asyncio.sleep(.01)
    responsive = time.monotonic() - started < .1
    await task
    assert responsive, 'Transit graph construction blocked the event loop'
    assert len(calls) == 2
    await provider.osm_route(b,a,{'elements':list(data['elements'])})
    assert len(calls) == 2
    changed = {**relation, 'tags':{'route':'subway','ref':'2'}}
    await provider.osm_route(a,b,{'elements':[changed]})
    assert len(calls) == 4


async def test_transit_path_search_keeps_event_loop_responsive(tmp_path, monkeypatch):
    import heapq
    import time
    from types import SimpleNamespace

    from backend.harness.schemas import Segment
    from backend.tools.transit import TransitProvider

    original_pop = heapq.heappop

    def slow_pop(queue):
        time.sleep(.04)
        return original_pop(queue)

    async def walk(geo, a, b, foot):
        if (a.id == 'A' and b.id != '0') or (b.id == 'B' and a.id != '2'):
            raise ValueError('Only endpoint stations are walkable')
        return Segment(from_poi=a.id,to_poi=b.id,from_name=a.name,to_name=b.name,
                       mode='步行',duration_min=1,distance_m=10,
                       polyline=[[a.lng,a.lat],[b.lng,b.lat]],source='fixture')

    monkeypatch.setattr('backend.tools.transit.heapq',
                        SimpleNamespace(heappop=slow_pop,heappush=heapq.heappush))
    monkeypatch.setattr('backend.tools.planning.routed', walk)
    members = [{'type':'node','ref':i,'lat':31,'lon':121+i*.001,
                'role':'stop','name':f'站{i}'} for i in range(3)]
    data = {'elements':[{'type':'relation','id':1,'tags':{'route':'subway','ref':'1'},
                         'members':members}]}
    provider = TransitProvider(SimpleNamespace(config=Settings(_env_file=None),store=Store(tmp_path)),None)
    a,b=POI(id='A',name='起点',lat=31,lng=121),POI(id='B',name='终点',lat=31,lng=121.002)
    pauses=[]
    task=asyncio.create_task(provider.osm_route(a,b,data))
    while not task.done():
        started=time.monotonic()
        await asyncio.sleep(.005)
        pauses.append(time.monotonic()-started)
    segment=await task
    assert segment is not None and segment.transit['legs'][1]['stops'] == 2
    assert max(pauses) < .08, 'Transit path search blocked the event loop'


def test_transit_path_bound_preserves_better_route_with_shorter_exit_walk(tmp_path):
    from types import SimpleNamespace

    from backend.tools.transit import TransitProvider

    provider=TransitProvider(SimpleNamespace(config=Settings(_env_file=None),store=Store(tmp_path)),None)
    stops={key:SimpleNamespace(name=key) for key in ['start','first','better','far']}
    edges={'start':[('first',1,'subway','1'),('better',3,'subway','2'),('far',100,'subway','3')]}
    result=provider._shortest_path(stops,edges,[('start',SimpleNamespace(duration_min=1))],
                                  [('first',SimpleNamespace(duration_min=10)),
                                   ('better',SimpleNamespace(duration_min=1))])
    assert result[0] == 5 + provider.geo.config.transit_wait_min
    assert result[3][-1][1] == 'better'


async def test_vector_cache_reuses_exact_texts_and_invalidates_model(tmp_path):
    config = Settings(_env_file=None)
    models = Models(config, Store(tmp_path))
    inputs = []

    async def post(base, endpoint, key, body):
        inputs.append(body['input'])
        return {'data': [{'index': i, 'embedding': [float(len(text)), 1.0]} for i, text in reversed(list(enumerate(body['input'])))]}

    models.post = post
    first = await models.embed(['甲', '乙乙'])
    assert await models.embed(['乙乙', '甲', '新资料']) == [first[1], first[0], [3.0, 1.0]]
    assert inputs == [['甲', '乙乙'], ['新资料']]
    config.embed_model = 'changed-model'
    await models.embed(['甲'])
    assert inputs[-1] == ['甲'] and len(inputs) == 3
    # A fresh provider instance uses persistent cached results.
    restored = Models(Settings(_env_file=None), Store(tmp_path))
    restored.post = post
    assert await restored.embed(['甲']) == [first[0]]
    assert len(inputs) == 3


async def test_malformed_vectors_are_not_cached(tmp_path):
    models = Models(Settings(_env_file=None), Store(tmp_path))

    async def broken(*args):
        return {'data': [{'index': 0, 'embedding': [1, 2]}]}

    models.post = broken
    with pytest.raises(ValueError, match='数量或索引'):
        await models.embed(['甲', '乙'])
    assert models.store.cached(models.store.key('unused', 'unused')) is None
    with models.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM cache').fetchone()[0] == 0


async def test_model_pool_limits_concurrency_and_closes_without_logging_secrets(tmp_path):
    store = Store(tmp_path)
    models = Models(Settings(_env_file=None), store)
    active = peak = 0
    two_started, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            two_started.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json={'ok': True})

    models.client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    pooled = models.client
    with job_metrics(store, 'pooled', 'Researcher'):
        tasks = [asyncio.create_task(models.post('https://models.example', '/chat/completions', 'private-secret', {'text': 'private-body'})) for _ in range(5)]
        await asyncio.wait_for(two_started.wait(), 1)
        release.set()
        assert all(await asyncio.gather(*tasks))
    assert peak == 2 and models.client is pooled
    events = store.events('pooled')
    assert len([e for e in events if e['operation'] == '模型生成']) == 5
    assert 'private-secret' not in json.dumps(events) and 'private-body' not in json.dumps(events)
    assert all('duration_s' not in event for event in events)
    await models.aclose()
    assert pooled.is_closed and models.client is None
    # Operation events cannot become completed phase timing samples.
    assert store.timing_samples() == {}
    assert progress_snapshot({'status':'running', 'node':'Researcher'}, events, {})['completed_stages'] == 0


async def test_fetch_overlaps_hosts_and_deduplicates_same_url(tmp_path, monkeypatch):
    async def public(*args, **kwargs):
        pass

    monkeypatch.setattr('backend.tools.fetch.public_url', public)
    active = peak = requests = 0
    entered, release = asyncio.Event(), asyncio.Event()

    async def respond(request):
        nonlocal active, peak, requests
        if request.url.path == '/robots.txt':
            return httpx.Response(404)
        requests += 1
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, text='<html><body><article><p>' + '博物馆参观规则来自官方公开页面。' * 40 + '</p></article></body></html>')

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    provider = FetchProvider(Settings(_env_file=None), Store(tmp_path))
    tasks = [asyncio.create_task(provider.fetch(url)) for url in ['https://one.example/page', 'https://two.example/page', 'https://one.example/page']]
    await asyncio.wait_for(entered.wait(), 1)
    release.set()
    results = await asyncio.gather(*tasks)
    assert peak == 2 and requests == 2 and len(set(results)) == 1


async def test_research_preserves_source_order_kb_visibility_and_limits_searches(tmp_path):
    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    active = peak = 0
    both_started, release = asyncio.Event(), asyncio.Event()
    indexed, seen_at_query = [], []

    async def search(query):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            both_started.set()
        await release.wait()
        active -= 1
        return 'https://one.example/' + query + ' https://shared.example/'

    async def extract(instruction, data, schema):
        urls = ['https://one.example/' + data['query'], 'https://shared.example/']
        return schema(sources=[dict(id='', title='官方资料', url=url, level='L3', fetched_at='') for url in urls])

    async def cached(city, query):
        seen_at_query.append((query, list(indexed)))
        return []

    async def ingest(city, query, source):
        indexed.append(str(source.url))

    async def fetch(url):
        return '# 官方资料\n可追溯的完整正文 ' + url

    harness.search.search, harness.models.json = search, extract
    harness.kb.search, harness.kb.ingest, harness.fetch.fetch = cached, ingest, fetch
    state = {'spec': {'city':'杭州'}, 'outline': {'queries':['q1','q2','q3']}, 'warnings':[]}
    task = asyncio.create_task(harness.researcher({'id':'ordered'}, state, tmp_path))
    await asyncio.wait_for(both_started.wait(), 1)
    release.set()
    await task
    sources = state['sources']
    assert peak == 2
    assert [(s['id'], s['url']) for s in sources] == [('S1','https://one.example/q1'), ('S2','https://shared.example/'), ('S3','https://one.example/q2'), ('S4','https://one.example/q3')]
    assert seen_at_query[1][1] == ['https://one.example/q1', 'https://shared.example/']
    assert indexed == [s['url'] for s in sources]


async def test_research_failure_cancels_sibling_operations(tmp_path):
    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def search(query):
        if query == 'broken':
            await started.wait()
            raise ValueError('search failed')
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def cached(*args):
        return []

    harness.search.search, harness.kb.search = search, cached
    state = {'spec': {'city':'杭州'}, 'outline': {'queries':['broken','waiting']}, 'warnings':[]}
    with pytest.raises(ExceptionGroup):
        await harness.researcher({'id':'failed-search'}, state, tmp_path)
    assert cancelled.is_set() and 'sources' not in state


async def test_cover_overlaps_ordered_photo_checks(tmp_path, monkeypatch):
    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    document = Deliverable(prd_version='1.1', spec=TaskSpec(city='杭州', type='travel_guide'), pois=[POI(id='P1',name='甲馆'), POI(id='P2',name='乙馆')])
    cover_started, photo_started = asyncio.Event(), asyncio.Event()
    order = []

    async def cover(*args):
        cover_started.set()
        await photo_started.wait()
        return ImageRef(url='https://images.example/cover.jpg', author='AI', license='generated', generated=True, source_page='https://images.example/cover')

    async def obtain(city, poi, folder):
        photo_started.set()
        await cover_started.wait()
        order.append(poi.id)
        poi.photo_status = '暂无合规实拍图'

    harness.photos.obtain = obtain
    harness.photos.hashes = ['previous-job-hash']
    monkeypatch.setattr('backend.harness.graph.generated_image', cover)
    state = {'document':document.model_dump(mode='json')}
    await asyncio.wait_for(harness.illustrator({'id':'parallel-cover'}, state, tmp_path), 1)
    assert order == ['P1','P2'] and harness.photos.hashes == []
    assert len(state['document']['decorations']) == 1


@pytest.mark.parametrize("mode, expected_photos", [("fast", []), ("balanced", ["P1"])])
async def test_lighter_modes_avoid_generated_cover(tmp_path, monkeypatch, mode, expected_photos):
    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    document = Deliverable(
        prd_version="1.3", spec=TaskSpec(city="杭州", type="travel_guide"),
        pois=[POI(id="P1", name="甲馆")],
    )
    photos = []

    async def cover(*args):
        pytest.fail("lighter modes must not call image generation")

    async def obtain(city, poi, folder):
        photos.append(poi.id)

    harness.photos.obtain = obtain
    monkeypatch.setattr("backend.harness.graph.generated_image", cover)
    state = {"document": document.model_dump(mode="json")}

    await harness.illustrator({"id": "mode-test", "generation_mode": mode}, state, tmp_path)

    assert photos == expected_photos
    assert state["document"]["decorations"] == []
    assert (state["document"]["pois"][0]["photo_status"] == PHOTO_SKIPPED) == (mode == "fast")


def test_generation_mode_is_validated_and_inherited_by_revision(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.app.main import create_app

    app = create_app(Settings(_env_file=None, data_dir=tmp_path))

    async def run(job_id, node):
        pass

    monkeypatch.setattr(app.state.harness, "run", run)
    with TestClient(app) as client:
        assert client.post("/api/jobs", json={
            "prompt": "杭州攻略", "generation_mode": "other",
        }).status_code == 422
        response = client.post("/api/jobs", json={"prompt": "杭州攻略", "generation_mode": "fast"})
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        job = app.state.store.get(job_id)
        assert job["generation_mode"] == "fast"
        job["status"] = "completed"
        app.state.store.save(job)
        folder = tmp_path / "jobs" / job_id
        folder.mkdir(parents=True)
        (folder / "state.json").write_text("{}")

        response = client.post(f"/api/jobs/{job_id}/revise", json={"instruction": "修改主题颜色"})
        assert response.status_code == 202
        revision = client.get(f"/api/jobs/{response.json()['job_id']}").json()
        assert revision["generation_mode"] == "fast"


async def test_same_host_keeps_one_second_spacing(tmp_path, monkeypatch):
    import time

    async def public(*args, **kwargs):
        pass

    starts = []

    async def respond(request):
        if request.url.path == '/robots.txt':
            return httpx.Response(404)
        starts.append(time.monotonic())
        return httpx.Response(200, text='<html><body><article><p>' + '公开参观须知。' * 80 + '</p></article></body></html>')

    original = httpx.AsyncClient
    monkeypatch.setattr('backend.tools.fetch.public_url', public)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    provider = FetchProvider(Settings(_env_file=None), Store(tmp_path))
    await asyncio.gather(provider.fetch('https://one.example/a'), provider.fetch('https://one.example/b'))
    assert len(starts) == 2 and starts[1] - starts[0] >= .99


async def test_embedding_provider_restarted_indices_are_avoided_with_bounded_batches(tmp_path):
    models = Models(Settings(_env_file=None), Store(tmp_path))
    sizes = []

    async def provider(base, endpoint, key, body):
        texts=body['input']
        sizes.append(len(texts))
        # Observed service response: index restarts at zero every eight texts.
        return {'data':[{'index':i % 8,'embedding':[float(text.split('-')[1]),1.0]} for i,text in enumerate(texts)]}

    models.post=provider
    await models.embed(['text-2','text-9'])
    expected=[[float(i),1.0] for i in range(12)]
    assert await models.embed([f'text-{i}' for i in range(12)]) == expected
    assert sizes == [2,8,2]
    assert await models.embed([f'text-{i}' for i in range(12)]) == expected
    assert sizes == [2,8,2]


async def test_fetch_uses_declared_html_encoding_instead_of_lossy_text(tmp_path, monkeypatch):
    async def public(*args,**kwargs):
        pass

    text='博物馆免费开放，请遵守参观须知。'
    body=('<html><head><meta charset="gb2312"><title>博物馆参观指南</title></head><body><article><p>'+text*40+'</p></article></body></html>').encode('gb2312')

    async def respond(request):
        return httpx.Response(404) if request.url.path=='/robots.txt' else httpx.Response(200,content=body,headers={'content-type':'text/html'})

    original=httpx.AsyncClient
    monkeypatch.setattr('backend.tools.fetch.public_url',public)
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(respond),**kwargs))
    store=Store(tmp_path)
    store.put_cache(store.key('fetch','https://museum.example/visit'),'\ufffd'*100)
    result=await FetchProvider(Settings(_env_file=None),store).fetch('https://museum.example/visit')
    assert text in result and '博物馆参观指南' in result and '\ufffd' not in result


async def test_long_text_generation_has_more_time_without_extending_embedding_timeout():
    seen=[]

    async def respond(request):
        seen.append(request.extensions['timeout']['read'])
        return httpx.Response(200,json={'ok':True})

    models=Models(Settings(_env_file=None))
    models.client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        await models.post('https://models.example','/chat/completions','test-key',{})
        await models.post('https://models.example','/embeddings','test-key',{})
    finally:
        await models.aclose()
    assert seen==[180,90]


async def test_research_reuses_complete_page_cache_instead_of_only_vector_chunk(tmp_path):
    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    url = 'https://museum.example/visit'
    full = '# 参观须知\n' + '场馆历史介绍。' * 100 + '\n开放时间09:00–17:00，免费参观，需预约。'
    harness.store.put_cache(harness.store.key('fetch', url), full)

    async def cached(city, query):
        return [{'source_url': url, 'text': full[:500], 'source_level': 'L1', 'fetched_at': '2026-10-30T00:00:00+00:00'}]

    async def search(query):
        return url

    async def extract(instruction, data, schema):
        return schema(sources=[dict(id='', title='参观须知', url=url, level='L3', fetched_at='')])

    async def unexpected_fetch(url):
        pytest.fail('完整网页已在缓存，不应再次请求网络')

    harness.kb.search, harness.search.search = cached, search
    harness.models.json, harness.fetch.fetch = extract, unexpected_fetch
    state = {'spec': {'city': '包头'}, 'outline': {'queries': ['参观须知']}, 'warnings': []}
    await harness.researcher({'id': 'full-page-cache'}, state, tmp_path)
    assert state['sources'][0]['text'] == full
    assert '开放时间09:00–17:00' in state['sources'][0]['text']


async def test_fetch_accepts_same_host_robot_redirect_without_crossing_hosts(tmp_path, monkeypatch):
    checked, requested = [], []

    async def public(url, **kwargs):
        checked.append(url)

    async def respond(request):
        requested.append(str(request.url))
        if request.url.path == '/robots.txt':
            return httpx.Response(301, headers={'Location': '/404.html'})
        if request.url.path == '/404.html':
            return httpx.Response(200, text='<html><body>页面不存在</body></html>')
        return httpx.Response(200, text='<html><body><article><p>' + '官方参观公告，免费开放，请提前预约。' * 30 + '</p></article></body></html>')

    original = httpx.AsyncClient
    monkeypatch.setattr('backend.tools.fetch.public_url', public)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    provider = FetchProvider(Settings(_env_file=None), Store(tmp_path))
    assert '官方参观公告' in await provider.fetch('http://museum.example/visit')
    assert requested == ['http://museum.example/robots.txt', 'http://museum.example/404.html', 'http://museum.example/visit']
    assert 'http://museum.example/404.html' in checked


@pytest.mark.parametrize('location', ['http://other.example/robots.txt', '/robots.txt'])
async def test_fetch_rejects_cross_host_robots_and_looping_page_redirects(tmp_path, monkeypatch, location):
    requested = []

    async def public(url, **kwargs):
        pass

    async def respond(request):
        requested.append(str(request.url))
        return httpx.Response(301, headers={'Location': location})

    original = httpx.AsyncClient
    monkeypatch.setattr('backend.tools.fetch.public_url', public)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    provider = FetchProvider(Settings(_env_file=None), Store(tmp_path))
    with pytest.raises(ValueError, match='robots.txt' if location.startswith('http') else '网页'):
        await provider.fetch('http://museum.example/visit')
    if location.startswith('http'):
        assert requested == ['http://museum.example/robots.txt']
    else:
        assert len(requested) == 12
        assert requested[:6] == ['http://museum.example/robots.txt'] * 6
        assert requested[6] == 'http://museum.example/visit'


@pytest.mark.parametrize('failure', ['rate_limit', 'connection'])
async def test_research_resume_preserves_completed_queries_after_upstream_failure(tmp_path, failure):
    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    calls, extractions = [], []
    failed = True
    fail_second_query = asyncio.Event()

    async def search(query):
        calls.append(query)
        if failed and query == 'q2':
            await fail_second_query.wait()
            if failure == 'rate_limit':
                response = httpx.Response(429, request=httpx.Request('POST', 'https://search.example/mcp'))
                response.raise_for_status()
            raise ExceptionGroup('MCP transport', [httpx.ConnectError('connection failed')])
        return 'https://test.gov.cn/' + query

    async def cached(city, query):
        if failed and query == 'q2':
            fail_second_query.set()
        return []

    async def model(instruction, data, schema):
        extractions.append(data['query'])
        return schema(sources=[dict(id='', title='官方资料',
                                   url='https://test.gov.cn/' + data['query'],
                                   level='L3', fetched_at='')])

    async def fetch(url):
        return '官方公开资料正文：' + url

    async def ingest(*args):
        pass

    harness.search.search, harness.kb.search = search, cached
    harness.models.json, harness.fetch.fetch, harness.kb.ingest = model, fetch, ingest
    state = {'spec': {'city': '苏州'}, 'outline': {'queries': ['q1', 'q2']}, 'warnings': []}
    with pytest.raises(ExceptionGroup):
        await harness.researcher({'id': 'resume'}, state, tmp_path)
    failed = False
    await harness.researcher({'id': 'resume'}, state, tmp_path)
    assert calls.count('q1') == 1, '上游中断后不应重复请求已经完成的查询'
    assert extractions.count('q1') == 1
    assert [(s['id'], s['url']) for s in state['sources']] == [
        ('S1', 'https://test.gov.cn/q1'), ('S2', 'https://test.gov.cn/q2')]
    state['outline']['queries'] = ['q3']
    await harness.researcher({'id': 'resume'}, state, tmp_path)
    assert [s['url'] for s in state['sources']] == ['https://test.gov.cn/q3']


@pytest.mark.parametrize('cached_page', ['valid', 'blocked', 'missing'])
async def test_research_can_use_query_kb_evidence_during_search_outage(tmp_path, cached_page):
    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    url = 'https://test.gov.cn/visit'

    async def search(query):
        response = httpx.Response(429, request=httpx.Request('POST', 'https://search.example/mcp'))
        response.raise_for_status()

    async def cached(city, query):
        assert (city, query) == ('苏州', '场馆开放时间')
        if cached_page == 'missing':
            return []
        return [dict(source_url=url, source_level='L1', fetched_at='2026-11-01',
                     text='官方公告：场馆周二至周日开放。')]

    if cached_page == 'blocked':
        harness.store.put_cache(harness.store.key('fetch', url), '## 请完成拼图验证')
    harness.search.search, harness.kb.search = search, cached
    state = {'spec': {'city': '苏州'}, 'outline': {'queries': ['场馆开放时间']}, 'warnings': []}
    if cached_page == 'valid':
        await harness.researcher({'id': 'offline'}, state, tmp_path)
        assert [s['url'] for s in state['sources']] == [url]
        assert any('429' in w and '知识库' in w for w in state['warnings'])
    else:
        with pytest.raises(ExceptionGroup) as caught:
            await harness.researcher({'id': 'offline'}, state, tmp_path)
        assert isinstance(caught.value.exceptions[0], httpx.HTTPStatusError)
        assert 'sources' not in state
