"""Regressions for missing official evidence in the Inner Mongolia runs."""

from types import SimpleNamespace

import pytest

from backend.harness.enrichment import enrich
from backend.harness.schemas import POI, Block, Deliverable, FactRecord, SourceRef, TaskSpec, now
from backend.tools.sources import normalize_facts


def test_newer_official_museum_address_replaces_outdated_directory():
    old = SourceRef(id='S19', title='呼和浩特市A级旅游景区名录', url='http://wlgd.huhhot.gov.cn/ggfw/lyqy/202307/t20230721_1560364.html', level='L1', fetched_at=now(), text='内蒙古博物院 地址：呼和浩特市新华大街27号')
    new = SourceRef(id='S21', title='呼和浩特市A级旅游景区名录', url='http://wlgd.huhhot.gov.cn/ggfw/lyqy/202511/t20251127_1953067.html', level='L1', fetched_at=old.fetched_at, text='内蒙古博物院 地址：呼和浩特市赛罕区新华东街76号')
    poi = POI(id='P1', name='内蒙古博物院', facts=[FactRecord(field='address', value='呼和浩特市新华大街27号', evidence='呼和浩特市新华大街27号', source_ids=['S19']), FactRecord(field='address', value='呼和浩特市赛罕区新华东街76号', evidence='呼和浩特市赛罕区新华东街76号', source_ids=['S21'])])
    normalize_facts(poi, [old, new])
    assert next(f for f in poi.facts if f.field == 'address').value == '呼和浩特市赛罕区新华东街76号'


async def test_existing_official_reference_does_not_lock_research_to_one_department():
    old = SourceRef(id='S1', title='包头测试馆介绍', url='https://dept.gov.cn/old', level='L1', fetched_at=now(), text='包头测试馆地址：文化路1号。')
    target = 'https://city.gov.cn/current-visit'
    queries, fetched = [], []
    text = '包头测试馆免费参观。开放时间09:00-17:00，周一闭馆。个人无需预约。地址：文化路1号。'

    async def search(query):
        queries.append(query)
        return target if 'site:gov.cn' in query else str(old.url)

    async def fetch(url):
        fetched.append(url)
        return text

    async def model(instruction, data, schema):
        if schema.__name__ == 'CandidateSources':
            url = target if target in data['results'] else str(old.url)
            return schema(sources=[dict(id='', title='包头测试馆参观须知', url=url, level='L3', fetched_at='')])
        if schema.__name__ == 'CandidateFacts':
            return schema(facts=[])
        source = next((s for s in data['sources'] if s['url'] == target), None)
        if not source:
            return schema(facts=[])
        facts = [FactRecord(field=field, value=value, evidence=value, source_ids=[source['id']]) for field,value in [('ticket','免费参观'),('hours','开放时间09:00-17:00，周一闭馆'),('booking','个人无需预约'),('address','文化路1号')]]
        return schema(facts=facts, introduction=Block(text='建议参观馆内陈列。', source_ids=[], kind='advice'))

    harness = SimpleNamespace(search=SimpleNamespace(search=search),fetch=SimpleNamespace(fetch=fetch),models=SimpleNamespace(json=model))
    poi = POI(id='P1', name='包头测试馆', facts=[FactRecord(field='address', value='文化路1号', evidence='文化路1号', source_ids=['S1'])])
    doc = Deliverable(spec=TaskSpec(type='travel_guide', city='包头', days=1), pois=[poi], sources=[old])
    await enrich(harness, doc)
    assert any('site:gov.cn' in query for query in queries)
    assert target in fetched
    assert {f.field for f in doc.pois[0].facts if f.source_ids} >= {'ticket','hours','booking','address'}


async def test_duplicate_links_do_not_consume_supplemental_fetch_budget():
    await _check_candidate_budget(duplicates=True)


async def test_official_page_is_read_before_lower_ranked_articles():
    await _check_candidate_budget(duplicates=False)


async def _check_candidate_budget(*, duplicates):
    sources = [SourceRef(id=f'S{i+1}', title='包头测试馆旧资料', url=f'https://dept.gov.cn/old-{i}', level='L1', fetched_at=now(), text='包头测试馆地址：文化路1号。') for i in range(3)]
    target = 'https://city.gov.cn/visit'
    leading = [str(s.url) for s in sources] if duplicates else [f'https://travel.example/article-{i}' for i in range(3)]
    candidates = [*leading, target]
    fetched = []

    async def search(query):
        return '\n'.join(candidates)

    async def fetch(url):
        fetched.append(url)
        return '包头测试馆免费参观。开放时间09:00-17:00，周一闭馆。个人无需预约。地址：文化路1号。' if url == target else '旅游新闻。'

    async def model(instruction, data, schema):
        if schema.__name__ == 'CandidateSources':
            return schema(sources=[dict(id='', title='包头测试馆参观须知', url=url, level='L3', fetched_at='') for url in candidates])
        if schema.__name__ == 'CandidateFacts':
            return schema(facts=[])
        source = next((s for s in data['sources'] if s['url'] == target), None)
        if source is None:
            return schema(facts=[])
        return schema(facts=[FactRecord(field=field, value=value, evidence=value, source_ids=[source['id']]) for field,value in [('ticket','免费参观'),('hours','开放时间09:00-17:00，周一闭馆'),('booking','个人无需预约'),('address','文化路1号')]])

    harness = SimpleNamespace(search=SimpleNamespace(search=search),fetch=SimpleNamespace(fetch=fetch),models=SimpleNamespace(json=model))
    poi = POI(id='P1', name='包头测试馆', facts=[FactRecord(field='address', value='文化路1号', evidence='文化路1号', source_ids=['S1'])])
    doc = Deliverable(spec=TaskSpec(type='travel_guide', city='包头', days=1), pois=[poi], sources=sources)
    await enrich(harness, doc)
    assert target in fetched
    assert fetched[0] == target
    # Each of the two bounded searches may read up to three previously unseen pages.
    assert len(fetched) <= 6 and len(fetched) == len(set(fetched))
    assert {f.field for f in doc.pois[0].facts if f.source_ids} >= {'ticket','hours','booking','address'}


def test_time_cards_with_explicit_open_close_labels_are_schedulable():
    from backend.tools.hours import parse_hours

    window = parse_hours('09:00 开馆；16:30 停止入馆；17:00 闭馆', ['S1'])
    assert window is not None
    assert (window.open, window.last_entry, window.close) == ('09:00','16:30','17:00')
    assert parse_hours('09:00；16:30；17:00', ['S1']) is None
    assert parse_hours('09:00–24:00', ['S1']) is None
    assert parse_hours('09:00 开馆；10:00 开馆；17:00 闭馆', ['S1']) is None


async def test_fetch_restores_time_card_numbers_missing_from_existing_cache(tmp_path, monkeypatch):
    import httpx

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.tools.fetch import FetchProvider

    url = 'https://museum.example/visit'
    store = Store(tmp_path)
    store.put_cache(store.key('fetch', url), '# 博物馆\n个人凭身份证进入。\n开馆\n停止入馆\n闭馆\n')
    requests = []

    async def public(url, **kwargs):
        pass

    async def respond(request):
        requests.append(request.url.path)
        if request.url.path == '/robots.txt':
            return httpx.Response(404)
        return httpx.Response(200, text='<html><head><title>博物馆</title></head><body><article><p>' + '博物馆展示本地历史文化。' * 30 + '</p></article><section><h5>参观预约</h5><div><a><span>09:00</span><p>开馆</p></a><a><span>16:30</span><p>停止入馆</p></a><a><span>17:00</span><p>闭馆</p></a></div></section></body></html>')

    class MCP:
        async def discover(self, *args):
            raise AssertionError('已知时间卡片丢失的缓存，应直接读取原始HTML恢复')

    original = httpx.AsyncClient
    monkeypatch.setattr('backend.tools.fetch.public_url', public)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    provider = FetchProvider(Settings(_env_file=None), store, MCP())
    text = await provider.fetch(url)
    assert all(value in text for value in ['09:00','16:30','17:00'])
    assert await provider.fetch(url) == text
    assert requests.count('/visit') == 1


def test_government_linked_museum_domain_is_official_without_trusting_lookalikes():
    from backend.tools.sources import source_level

    assert source_level('https://www.nmgbwy.cn/Home/Indexs') == 'L1'
    assert source_level('https://nmgbwy.cn.evil.example/visit') == 'L3'
    assert source_level('https://news.example/story-about-nmgbwy.cn') == 'L3'


async def test_mcp_search_preserves_site_filter_and_exact_name(tmp_path):
    from contextlib import asynccontextmanager

    from mcp.types import Tool

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.tools.mcp_search import MCPProvider, SearchProvider, ToolCall

    query = '包头 "包头博物馆" site:wlgd.baotou.gov.cn 参观须知'
    calls = []

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[Tool(name='web_search_exa', inputSchema={'type':'object','properties':{'query':{'type':'string'},'objective':{'type':'string'}},'required':['query','objective']})])

        async def call_tool(self, name, arguments):
            calls.append(arguments)
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text='官方检索结果')])

    @asynccontextmanager
    async def session(server):
        yield Session()

    async def choose(*args):
        return ToolCall(name='web_search_exa', arguments={'query':'包头博物馆普通泛化查询','objective':'查找官方参观信息'})

    provider = MCPProvider(Settings(_env_file=None), SimpleNamespace(json=choose))
    provider.session = session
    search = SearchProvider(provider, Store(tmp_path))
    assert await search.search(query) == '官方检索结果'
    assert calls[0]['query'] == query
    assert await search.search(query) == '官方检索结果'
    assert len(calls) == 1


async def test_fetch_follows_checked_same_host_https_upgrade(tmp_path, monkeypatch):
    import httpx

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.tools.fetch import FetchProvider

    checked = []

    async def public(url, **kwargs):
        checked.append(url)

    async def respond(request):
        if request.url.path == '/robots.txt':
            return httpx.Response(404)
        if request.url.scheme == 'http':
            return httpx.Response(308, headers={'Location':'https://museum.gov.cn/visit'})
        return httpx.Response(200, text='<html><body><article><p>' + '博物馆每周二至周日免费开放。' * 30 + '</p></article></body></html>')

    original = httpx.AsyncClient
    monkeypatch.setattr('backend.tools.fetch.public_url', public)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    text = await FetchProvider(Settings(_env_file=None), Store(tmp_path)).fetch('http://museum.gov.cn/visit')
    assert '免费开放' in text
    assert 'https://museum.gov.cn/visit' in checked


def test_source_tiers_keep_root_government_and_wikimedia_domains():
    from backend.tools.sources import source_level

    assert source_level('https://www.gov.cn/gongbao/content/2001/example.htm') == 'L1'
    assert source_level('https://www.wikidata.org/wiki/Q1') == 'L2'
    assert source_level('https://wikipedia.org/') == 'L2'
    assert source_level('https://www.gov.cn.evil.example/') == 'L3'
    assert source_level('https://wikipedia.org.evil.example/') == 'L3'


async def test_research_query_is_not_stored_as_a_poi_name(tmp_path):
    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.graph import Harness

    harness = Harness(Settings(_env_file=None), Store(tmp_path))
    query = '呼和浩特博物馆官方参观信息' * 9
    indexed = []

    async def search(query):
        return 'https://museum.gov.cn/visit'

    async def model(instruction, data, schema):
        return schema(sources=[dict(id='', title='参观信息', url='https://museum.gov.cn/visit', level='L3', fetched_at='')])

    async def recall(city, query):
        return []

    async def fetch(url):
        return '真实来源正文'

    async def ingest(city, poi, source):
        indexed.append(poi)
        assert poi == ''  # Research has not established a POI identity yet.

    harness.search.search, harness.models.json = search, model
    harness.kb.search, harness.kb.ingest, harness.fetch.fetch = recall, ingest, fetch
    await harness.researcher({'id':'long-query'}, {'spec':{'city':'呼和浩特'},'outline':{'queries':[query]},'warnings':[]}, tmp_path)
    assert indexed == ['']


def test_weekday_opening_variants_use_a_safe_common_window():
    from backend.tools.hours import parse_hours

    window = parse_hours('周二至周五9:00-17:00；周六、周日8:30-17:30；周一展厅闭馆（法定节假日除外）', ['S26'])
    assert window is not None
    assert (window.open, window.close, window.closed_days) == ('09:00', '17:00', [0])
    assert '共同开放时段' in window.note
    assert parse_hours('上午9:00-12:00，下午14:00-17:00', ['S1']) is None
    assert parse_hours('一馆9:00-17:00，二馆10:00-18:00', ['S1']) is None


def test_sasac_listed_operator_site_is_an_official_source():
    from backend.tools.sources import source_level

    assert source_level('http://www.norincogroup.com.cn/art/2026/5/19/art_85_569388.html') == 'L1'
    assert source_level('https://norincogroup.com.cn.evil.example/') == 'L3'


def test_explicit_morning_evening_labels_preserve_clock_values():
    from backend.tools.hours import parse_hours

    window = parse_hours('开馆时间：早9:00至晚17:00(16:30停止入馆）。', ['S4'])
    assert window is not None
    assert (window.open, window.close, window.last_entry) == ('09:00', '17:00', '16:30')


def test_office_hours_do_not_hide_explicit_exhibition_schedule():
    from backend.tools.hours import parse_hours

    text = '办公时间 | 办公区：周一到周五(法定节假日除外)每天8:30-12:00 14:30-17:30 展 厅：周二至周五 9:00-17:00，周一展厅闭馆 周六、周日 8:30-17:30（法定节假日除外）'
    window = parse_hours(text, ['S26'])
    assert window is not None
    assert (window.open, window.close, window.closed_days) == ('09:00', '17:00', [0])
    assert parse_hours('办公区：8:30-12:00 14:30-17:30', ['S26']) is None


async def test_new_evidence_checks_record_the_actual_verification_date():
    from datetime import date

    source = SourceRef(id='S1',title='测试馆参观须知',url='https://museum.gov.cn/visit',level='L1',fetched_at=now(),text='免费参观。09:00-17:00。个人无需预约。文化路1号。')
    facts = [FactRecord(field=field,value=value,evidence=value,source_ids=['S1'],verified_at='2020-01-01') for field,value in [('ticket','免费参观'),('hours','09:00-17:00'),('booking','个人无需预约'),('address','文化路1号')]]

    async def model(instruction,data,schema):
        return schema(facts=facts)

    harness = SimpleNamespace(models=SimpleNamespace(json=model))
    doc = Deliverable(spec=TaskSpec(type='travel_guide',city='测试城',days=1),pois=[POI(id='P1',name='测试馆',facts=facts)],sources=[source])
    await enrich(harness,doc,search_missing=False)
    assert all(f.verified_at == date.today() for f in doc.pois[0].facts)


async def test_bilingual_city_boundary_keeps_exact_city_and_polygon_checks(tmp_path, monkeypatch):
    import httpx

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.tools.geo import Geo

    boundary = {'class':'boundary','name':'包头市 ᠪᠤᠭᠤᠲᠤ ᠬᠣᠲᠠ','address':{'country_code':'cn'},'geojson':{'type':'Polygon','coordinates':[[[109,40],[110,40],[110,41],[109,41],[109,40]]]}}

    async def respond(request):
        return httpx.Response(200,json=[boundary])

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs: original(transport=httpx.MockTransport(respond),**kwargs))
    geo = Geo(Settings(_env_file=None),Store(tmp_path))
    point = {'lat':'40.6406746','lon':'109.8350087','address':{'country_code':'cn'}}
    assert await geo.within_city('包头',point)
    assert not await geo.within_city('呼和浩特',point)
    assert not await geo.within_city('包头',{**point,'lon':'111'})
    assert not await geo.within_city('包头',{**point,'address':{'country_code':'mn'}})


async def test_one_geocoder_outage_keeps_other_verified_stop_and_reports_exclusion(tmp_path):
    import httpx

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.schemas import OpeningHours
    from backend.tools.planning import plan

    async def locate(city,poi,sources):
        if poi.id == 'P2':
            raise httpx.ConnectError('地图连接中断')
        poi.lat,poi.lng,poi.location_verified=40.0,112.0,True

    async def basemap(*args):
        return {'test':'map'}

    pois=[POI(id=id,name=name,hours=OpeningHours(open='09:00',close='17:00',source_ids=['S1'])) for id,name in [('P1','已核实场馆'),('P2','连接中断场馆')]]
    geo=SimpleNamespace(config=Settings(_env_file=None),store=Store(tmp_path),locate=locate)
    days,alternatives,_=await plan(geo,TaskSpec(city='呼和浩特',type='travel_guide',days=1),pois,[],SimpleNamespace(basemap=basemap),tmp_path)
    assert [p.id for p in days[0].stops] == ['P1']
    assert len(alternatives)==1 and alternatives[0]['name']=='连接中断场馆'
    assert '地图服务' in alternatives[0]['reason']


async def test_culture_verifier_searches_official_visit_information(tmp_path, monkeypatch):
    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.graph import FactSet, Harness

    harness = Harness(Settings(_env_file=None, data_dir=tmp_path), Store(tmp_path))
    folder = tmp_path / 'jobs' / 'culture-official'
    folder.mkdir(parents=True)
    target = 'https://culture.gov.cn/visit'
    queries = []
    text = '苏州测试馆免费参观。开放时间09:00-17:00。个人无需预约。地址：文化路1号。'

    async def search(query):
        queries.append(query)
        return target

    async def fetch(url):
        assert url == target
        return text

    async def model(instruction, data, schema):
        if schema is FactSet:
            return schema(pois=[POI(id='P1', name='苏州测试馆')])
        if schema.__name__ == 'CandidateSources':
            return schema(sources=[SourceRef(id='', title='苏州测试馆参观须知', url=target,
                                            level='L3', fetched_at='')])
        if schema.__name__ == 'CandidateFacts':
            return schema(facts=[])
        official = next((s for s in data['sources'] if s['url'] == target), None)
        return schema(facts=[] if not official else [
            FactRecord(field=field, value=value, evidence=value, source_ids=[official['id']])
            for field, value in [('ticket', '免费参观'), ('hours', '09:00-17:00'),
                                 ('booking', '个人无需预约'), ('address', '文化路1号')]
        ])

    monkeypatch.setattr(harness.models, 'json', model)
    monkeypatch.setattr(harness.search, 'search', search)
    monkeypatch.setattr(harness.fetch, 'fetch', fetch)
    state = {'prd_version': '1.3', 'warnings': [],
             'spec': TaskSpec(type='culture_intro', city='苏州').model_dump(mode='json'),
             'outline': {'poi_names': ['苏州测试馆']}, 'sources': []}
    # An old no-search checkpoint must not suppress the newly enabled recovery.
    await enrich(harness, Deliverable(
        prd_version='1.3', spec=TaskSpec.model_validate(state['spec']),
        pois=[POI(id='P1', name='苏州测试馆')],
    ), folder, search_missing=False)
    await harness.verifier({'id': folder.name}, state, folder)
    assert queries, '文化介绍也必须补查官方参观信息，不能留到发布时才发现资料不足'
    poi = POI.model_validate(state['document']['pois'][0])
    assert {f.field for f in poi.facts if f.source_ids} >= {'ticket', 'hours', 'booking', 'address'}
    assert poi.verification_url == target


def test_official_fetch_budget_reaches_other_hosts_when_one_department_dominates():
    from backend.tools.sources import candidate_pages

    urls = [f'https://dept.gov.cn/page-{i}' for i in range(4)] + [
        'https://city.gov.cn/visit', 'https://culture.gov.cn/booking',
        'https://wikipedia.org/wiki/venue']
    candidates = [SourceRef(id='', title='参观资料', url=url, level='L3', fetched_at='')
                  for url in urls]
    selected = [url for _, url in candidate_pages(candidates, '\n'.join(urls), set(), 3)]
    assert selected == [urls[0], urls[4], urls[5]]
    assert [url for _, url in candidate_pages(candidates, '\n'.join(urls), set(), 6)][-1] == urls[3]


async def test_robots_redirect_limit_is_unavailable_but_actual_rules_still_apply(tmp_path, monkeypatch):
    import httpx
    import pytest

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.tools.fetch import FetchProvider

    requests = []
    mode = 'redirect'

    async def public(url, **kwargs):
        pass

    async def respond(request):
        requests.append(request.url.path)
        if request.url.path in {'/robots.txt', '/'}:
            if mode == 'redirect':
                return httpx.Response(302, headers={'Location': '/'})
            if mode == 'deny':
                return httpx.Response(200, text='User-agent: *\nDisallow: /')
            return httpx.Response(503)
        return httpx.Response(200, text='<html><body><article><p>' + '博物馆公开参观信息。' * 40 + '</p></article></body></html>')

    original = httpx.AsyncClient
    monkeypatch.setattr('backend.tools.fetch.public_url', public)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    config = Settings(_env_file=None)
    provider = FetchProvider(config, Store(tmp_path / 'redirect'))
    assert '博物馆公开参观信息' in await provider.fetch('https://museum.gov.cn/visit')
    assert requests[:6] == ['/robots.txt', '/', '/', '/', '/', '/']
    for mode in ['deny', 'server_error']:
        requests.clear()
        provider = FetchProvider(config, Store(tmp_path / mode))
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            await provider.fetch('https://museum.gov.cn/visit')
        assert '/visit' not in requests


async def test_missing_visit_fields_are_requested_in_official_search(tmp_path):
    from backend.harness.enrichment import enrich

    queries = []

    async def search(query):
        queries.append(query)
        return ''

    async def model(instruction, data, schema):
        return schema(sources=[]) if schema.__name__ == 'CandidateSources' else schema(facts=[])

    harness = SimpleNamespace(search=SimpleNamespace(search=search), models=SimpleNamespace(json=model))
    document = Deliverable(prd_version='1.3', spec=TaskSpec(type='culture_intro', city='苏州'),
                           pois=[POI(id='P1', name='拙政园')])
    await enrich(harness, document)
    official = next(q for q in queries if 'site:gov.cn' in q)
    assert all(field in official for field in ['门票', '开放时间', '预约', '地址'])


async def test_v13_visit_model_never_receives_ineligible_third_party_evidence():
    from backend.harness.enrichment import enrich

    source = SourceRef(id='S1', title='测试馆参观攻略', url='https://blog.example/visit',
                       level='L3', fetched_at=now(), text='测试馆票价99元，09:00开放。')

    async def model(instruction, data, schema):
        assert data['sources'] == [], 'L3不能作为参观核验依据，也不应占用核验输入预算'
        return schema(facts=[])

    harness = SimpleNamespace(models=SimpleNamespace(json=model))
    document = Deliverable(prd_version='1.3', spec=TaskSpec(type='culture_intro', city='苏州'),
                           pois=[POI(id='P1', name='测试馆')], sources=[source])
    await enrich(harness, document, search_missing=False)
    assert not any(f.source_ids for f in document.pois[0].facts)


@pytest.mark.parametrize('code', [408, -32000, -32602])
async def test_mcp_retries_session_failures_only(code, monkeypatch):
    from contextlib import asynccontextmanager

    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData
    from tenacity import wait_none

    from backend.app.config import Settings
    from backend.tools.mcp_search import MCPProvider

    provider = MCPProvider(Settings(_env_file=None), None)
    monkeypatch.setattr(provider.discover.retry, 'wait', wait_none())
    attempts = 0

    @asynccontextmanager
    async def session(server):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ExceptionGroup('MCP session', [McpError(ErrorData(code=code, message='test'))])
        async def list_tools():
            return SimpleNamespace(tools=[])
        yield SimpleNamespace(list_tools=list_tools)

    provider.session = session
    if code == -32602:
        with pytest.raises(ExceptionGroup):
            await provider.discover('test')
        assert attempts == 1
    else:
        assert await provider.discover('test') == []
        assert attempts == 2
