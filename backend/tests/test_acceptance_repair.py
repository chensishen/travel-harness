from datetime import date

import pytest

from backend.harness.schemas import POI, DayPlan, Deliverable, FactRecord, TaskSpec
from backend.harness.visit_context import group_budget


def budget_document(month=11):
    return Deliverable(spec=TaskSpec(type='travel_guide', city='上海', days=3, travelers=2,
                                    start_date=date(2026, month, 7)), pois=[
        POI(id='P1', name='豫园', facts=[FactRecord(field='ticket', source_ids=['S1'],
            value='成人票旺季（每年4月1日至6月30日，9月1日至11月30日）每位40元，淡季（每年7月1日至8月31日，12月1日至3月31日）每位30元')]),
        POI(id='P2', name='宋庆龄故居', facts=[FactRecord(field='ticket', source_ids=['S2'], value='成人票原价20元；出发前核对当日票价。')]),
        POI(id='P3', name='犹太难民纪念馆', facts=[FactRecord(field='ticket', source_ids=['S3'], value='成人票20元，学生票10元，老年票15元。')]),
    ])


@pytest.mark.parametrize(('month', 'expected'), [(11, [640, 1240]), (8, [620, 1220])])
def test_budget_counts_current_season_and_original_adult_price(month, expected):
    doc = budget_document(month)
    group_budget(doc)
    assert doc.practical[-1].amount_range == expected
    assert '豫园' not in doc.practical[-1].value
    assert doc.practical[0].amount_range == ([80, 80] if month == 11 else [60, 60])


def test_saved_audit_requires_maps_even_when_paths_are_empty(monkeypatch):
    import scripts.audit_v13 as audit_module

    called = []
    def inspect(doc, *, require_maps):
        called.append(require_maps)
        return []
    monkeypatch.setattr(audit_module, 'quality_issues', inspect)
    doc = Deliverable(spec=TaskSpec(type='travel_guide', city='上海', days=1),
                      days=[DayPlan(day=1, theme='城市', stops=[])])
    audit_module.audit({'document': doc.model_dump(mode='json')})
    assert called == [True]


def test_exclusive_exhibition_notice_is_dated_and_must_reach_poi_copy():
    from backend.harness.quality import quality_issues
    from backend.harness.schemas import Block, SourceRef
    from backend.harness.visit_context import active_exhibitions, apply_exhibition_notices

    source = SourceRef(id='S1', title='人民广场馆参观须知', url='https://www.shanghaimuseum.net/visit',
                       level='L1', fetched_at='2026-11-03', text='“美洲古代文明大展”期间（2026.7.9-2027.11.14）上海博物馆人民广场馆将不再展出其他展览，常设展可前往东馆。')
    poi = POI(id='P1', name='上海博物馆', facts=[FactRecord(field='booking', value='需预约购票',
               evidence='需预约购票', source_ids=['S1'])])
    doc = Deliverable(spec=TaskSpec(type='travel_guide', city='上海', days=1, start_date=date(2026, 11, 8)),
                      prd_version='1.3', pois=[poi], sources=[source])
    assert active_exhibitions(doc)[0]['title'] == '美洲古代文明大展'
    assert any('当期独占展览' in x for x in quality_issues(doc, require_maps=False))
    apply_exhibition_notices(doc)
    poi.blocks = [Block(text='本次参观美洲古代文明大展；其余常设展在东馆。', source_ids=['S1'])]
    assert not any('当期独占展览' in x for x in quality_issues(doc, require_maps=False))
    assert doc.practical[0].source_ids == ['S1']
    doc.spec.start_date = date(2028, 1, 1)
    assert not active_exhibitions(doc)


def test_budget_gate_rejects_a_total_that_omits_known_fares():
    from backend.harness.quality import budget_issues

    doc = budget_document()
    group_budget(doc)
    assert not budget_issues(doc)
    doc.practical[-1].amount_range = [520, 1120]
    assert '预算合计与分项金额不一致' in budget_issues(doc)
    doc.practical[0].amount_range = []
    assert any('豫园' in x for x in budget_issues(doc))


def test_museum_branch_check_rejects_east_wikidata_photo_for_people_square():
    from backend.tools.photos import wrong_branch

    poi = POI(id='P9', name='上海博物馆', official_url='https://www.shanghaimuseum.net/mu/frontend/pg/service/visit-west')
    assert wrong_branch({'title': 'File:Shanghai East Museum - 54127959213.jpg',
                         'subject_evidence': 'wikidata_p18'}, poi)
    assert not wrong_branch({'title': 'File:Shanghai Museum exterior 3.jpg'}, poi)


def test_collection_photo_caption_retains_exhibition_scope_and_license():
    from backend.harness.schemas import ImageRef
    from backend.render.reader_content import photo_caption

    image = ImageRef(url='https://example.org/a.jpg', author='Gisling', license='CC BY 3.0',
                     source_page='https://commons.wikimedia.org/a',
                     caption='馆藏汝窑青瓷资料图，非本次专展展品')
    caption = photo_caption(POI(id='P9', name='上海博物馆人民广场馆'), image)
    assert '非本次专展展品' in caption and 'Gisling' in caption and 'CC BY 3.0' in caption


async def test_replacement_proofs_reschedule_and_reject_generic_or_schematic_routes():
    from backend.harness.quality import replacement_issues
    from backend.harness.schemas import OpeningHours, Segment
    from backend.tools.planning import schedule
    from backend.tools.planning_v13 import verified_replacements

    def poi(i):
        return POI(id=str(i), name='景点' + str(i), lat=31 + i / 10000, lng=121,
                   location_verified=True, location_source=f'https://www.openstreetmap.org/node/{i}',
                   duration_min=30, hours=OpeningHours(open='09:00', close='17:00'))

    async def route(a, b):
        return Segment(from_poi=a.id, to_poi=b.id, from_name=a.name, to_name=b.name,
                       distance_m=100, duration_min=5, source='fixture foot geometry',
                       route_verified=True, geometry_mode='foot', mode='步行',
                       polyline=[[a.lng, a.lat], [121.00001, 31.00001], [b.lng, b.lat]],
                       transit={'confidence': 'fallback'})

    origin, stops = poi(10), [poi(i) for i in range(1, 4)]
    legs = [await route(a, b) for a, b in zip(stops, stops[1:], strict=False)]
    timeline, meals, active = schedule(stops, legs, date(2026, 11, 7), '09:05')
    day = DayPlan(day=1, date=date(2026, 11, 7), theme='城市', origin=origin, stops=stops,
                  segments=legs, arrival_segment=await route(origin, stops[0]),
                  timeline=timeline, meals=meals, total_minutes=active + 5)
    spec = TaskSpec(type='travel_guide', city='测试', days=1, start_date=day.date)
    candidates = [poi(i) for i in range(4, 7)]
    alternatives = await verified_replacements(spec, [day], candidates, route)
    doc = Deliverable(spec=spec, prd_version='1.3', days=[day], pois=stops, alternatives=alternatives)
    assert len(alternatives) == 3 and not replacement_issues(doc)
    assert all('2026-11-07' in a['replace_day'] and '替换' in a['replace_day'] for a in alternatives)
    alternatives[0]['replacement_plan']['segments'][0]['geometry_mode'] = 'transit_schematic'
    assert replacement_issues(doc)
    doc.alternatives = [{'name': p.name, 'replace_day': '任一天同类时段'} for p in candidates]
    assert replacement_issues(doc)


async def test_replacement_planner_skips_closed_candidates_before_routing():
    from backend.harness.schemas import OpeningHours, Segment
    from backend.tools.planning_v13 import verified_replacements

    stops = [POI(id=str(i), name=str(i), lat=31, lng=121, location_verified=True,
                 location_source=f'osm:{i}') for i in range(3)]
    day = DayPlan(day=1, date=date(2026, 11, 7), theme='城市', stops=stops, origin=stops[0])
    closed = POI(id='closed', name='闭馆', lat=31, lng=121, location_verified=True,
                 location_source='osm:closed', hours=OpeningHours(open='09:00', close='17:00', closed_days=[5]))
    calls = []
    async def route(a, b):
        calls.append((a.id, b.id))
        return Segment(from_poi=a.id, to_poi=b.id, distance_m=500, duration_min=45,
                       source='test', polyline=[[121, 31]] * 3, route_verified=True, geometry_mode='foot')
    assert not await verified_replacements(TaskSpec(type='travel_guide', city='城'), [day], [closed], route)
    assert not calls


async def test_writer_receives_current_branch_exhibition_notice_before_model_call(tmp_path):
    from types import SimpleNamespace

    from backend.harness.graph import Harness
    from backend.harness.schemas import SourceRef

    source = SourceRef(id='S1', title='参观须知', url='https://www.shanghaimuseum.net/visit',
                       level='L1', fetched_at='2026-11-03',
                       text='“美洲文明大展”期间（2026.7.9-2027.11.14）上海博物馆人民广场馆将不再展出其他展览。')
    doc = Deliverable(spec=TaskSpec(type='travel_guide', city='上海', days=1, start_date=date(2026, 11, 8)),
                      prd_version='1.3', sources=[source], pois=[POI(id='P9', name='上海博物馆',
                      facts=[FactRecord(field='hours', value='9-17', source_ids=['S1'])])])
    payloads = []
    class Captured(Exception):
        pass
    async def capture(prompt, payload, schema):
        payloads.append(payload)
        raise Captured
    harness = object.__new__(Harness)
    harness.models = SimpleNamespace(json=capture)
    with pytest.raises(Captured):
        await harness.writer({}, {'document': doc.model_dump(mode='json'), 'sources': [source.model_dump(mode='json')],
                                  'spec': doc.spec.model_dump(mode='json'), 'outline': {}}, tmp_path)
    assert '美洲文明大展' in payloads[0]['visit_notices'][0]['value']
    assert payloads[0]['visit_notices'][0]['source_ids'] == ['S1']


def test_shared_venue_notices_merge_without_losing_scope_or_sources():
    from backend.harness.preparation import prepare
    from backend.harness.quality import quality_issues
    from backend.harness.schemas import PracticalItem, SourceRef

    text = '除老弱病残专用非机动车外，其他车辆未经许可禁止入园。'
    names = ['拙政园', '留园', '狮子林']
    document = Deliverable(prd_version='1.3', spec=TaskSpec(type='culture_intro', city='苏州'),
        practical=[PracticalItem(category='注意事项' if i == 2 else 'notice', label=name+' · 参观提示', value=text,
                                source_ids=[f'S{i}']) for i,name in enumerate(names,1)],
        sources=[SourceRef(id=f'S{i}', title=name+'参观须知', url=f'https://garden.gov.cn/{i}',
                           level='L1', fetched_at='2026-11-07', text=text)
                 for i,name in enumerate(names,1)])
    prepared = prepare(document)
    assert len(prepared.practical) == 1
    notice = prepared.practical[0]
    assert all(name in notice.label for name in names)
    assert notice.source_ids == ['S1', 'S2', 'S3']
    assert '同一事实或文案出现超过两次' not in quality_issues(prepared, require_maps=False)
    snapshot = [item.model_dump() for item in prepared.practical]
    assert [item.model_dump() for item in prepare(prepared).practical] == snapshot


async def test_travel_writer_sends_only_sources_needed_by_facts_notices_and_revision(tmp_path):
    from types import SimpleNamespace

    from backend.harness.graph import Harness
    from backend.harness.schemas import Block, PracticalItem, SourceRef

    refs = [
        SourceRef(id=f'S{i}', title=f'资料{i}', url=f'https://museum.gov.cn/{i}',
                  level='L1', fetched_at='2026-11-03', text=f'原文{i}')
        for i in range(1, 5)
    ]
    doc = Deliverable(
        spec=TaskSpec(type='travel_guide', city='上海', days=1),
        prd_version='1.3', sources=refs,
        pois=[POI(id='P1', name='示例博物馆',
                  facts=[FactRecord(field='history', value='原文1', source_ids=['S1'])],
                  blocks=[Block(text='既有正文', source_ids=['S3'])])],
        practical=[PracticalItem(category='notice', label='开放提示', value='原文2', source_ids=['S2'])],
    )

    class Captured(Exception):
        pass

    async def capture(prompt, payload, schema):
        assert [s['id'] for s in payload['sources']] == ['S1', 'S2', 'S3']
        raise Captured

    harness = object.__new__(Harness)
    harness.models = SimpleNamespace(json=capture)
    with pytest.raises(Captured):
        await harness.writer(
            {'revision_instruction': '精简正文'},
            {'document': doc.model_dump(mode='json'), 'spec': doc.spec.model_dump(mode='json'),
             'sources': [s.model_dump(mode='json') for s in refs], 'outline': {}},
            tmp_path,
        )


async def test_travel_writer_batches_pois_and_limits_each_batch_sources(tmp_path):
    from types import SimpleNamespace

    from backend.harness.graph import Harness
    from backend.harness.schemas import SourceRef

    refs = [
        SourceRef(id=f'S{i}', title=f'资料{i}', url=f'https://museum.gov.cn/{i}',
                  level='L1', fetched_at='2026-11-03', text=f'原文{i}')
        for i in range(1, 6)
    ]
    doc = Deliverable(
        spec=TaskSpec(type='travel_guide', city='上海', days=2), prd_version='1.3',
        sources=refs, pois=[
            POI(id=f'P{i}', name=f'场馆{i}',
                facts=[FactRecord(field='history', value=f'原文{i}', source_ids=[f'S{i}'])])
            for i in range(1, 6)
        ],
    )
    batches = []

    async def model(prompt, payload, schema):
        ids = [p['id'] for p in payload['facts']]
        assert len(ids) <= 2
        assert set(schema.model_json_schema()['properties']) == {'pois'}
        assert '文旅介绍至少5章' not in prompt
        assert [s['id'] for s in payload['sources']] == [p.replace('P', 'S') for p in ids]
        batches.append(ids)
        return schema(pois=[{'id': p['id'], 'name': p['name']} for p in payload['facts']],
                      chapters=[])

    harness = object.__new__(Harness)
    harness.models = SimpleNamespace(json=model)
    with pytest.raises(ValueError, match='没有可交付的有来源正文'):
        await harness.writer(
            {},
            {'document': doc.model_dump(mode='json'), 'spec': doc.spec.model_dump(mode='json'),
             'sources': [s.model_dump(mode='json') for s in refs], 'outline': {}},
            tmp_path,
        )
    assert batches == [['P1', 'P2'], ['P3', 'P4'], ['P5']]
    import json

    combined = json.loads((tmp_path / 'writing-candidates.json').read_text())
    assert [p['id'] for p in combined['pois']] == [p.id for p in doc.pois]
