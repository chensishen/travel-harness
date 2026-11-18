from datetime import date

from backend.harness.quality import quality_issues, route_failures
from backend.harness.schemas import POI, DayPlan, Deliverable, ScheduleEntry, TaskSpec


def test_route_gate_counts_waiting_and_meals_in_ten_hour_limit():
    document = Deliverable(
        spec=TaskSpec(type="travel_guide", city="杭州", days=1),
        days=[DayPlan(day=1, theme="湖畔", stops=[], date=date(2026, 10, 31), timeline=[
            ScheduleEntry(start="09:00", end="19:30", kind="wait", place="等候"),
        ])],
    )
    assert any("10 小时" in s for s in route_failures(document))


def test_publication_gate_rejects_legacy_sample_instead_of_only_warning():
    document = Deliverable(
        spec=TaskSpec(type="travel_guide", city="杭州", days=1),
        pois=[POI(id="P1", name="景点", images=[])],
    )
    issues = quality_issues(document)
    assert any("75%" in i for i in issues)
    assert any("时间轴" in i or "天数" in i for i in issues)
    assert any("预算" in i for i in issues)


def test_schedule_rejects_closed_weekday_and_last_entry():
    from backend.harness.schemas import OpeningHours
    from backend.tools.planning import schedule

    poi = POI(id="P1", name="博物馆", duration_min=60, hours=OpeningHours(open="09:00", close="17:00", last_entry="16:00", closed_days=[0]))
    assert schedule([poi], [], date(2026, 11, 2), "09:00") is None
    assert schedule([poi], [], date(2026, 10, 31), "16:30") is None
    result = schedule([poi], [], date(2026, 10, 31), "09:00")
    assert result is not None and result[2] <= 600
    # Chapter 15: a short visit must not be padded with lunch/dinner or long waits.
    assert not [e for e in result[0] if e.kind in {"meal", "wait"}]
    assert schedule([], [], date(2026, 10, 31), "09:00") is None


def test_time_format_normalization_keeps_numeric_evidence_but_not_changed_price():
    from backend.harness.reviewer import numeric_values
    assert numeric_values("09:00–17:00") <= numeric_values("9:00-17:00")
    assert not numeric_values("门票31元") <= numeric_values("门票30元")


def test_hours_parser_retains_afternoon_last_entry_and_monday_closure():
    from backend.tools.hours import parse_hours
    window = parse_hours("周二至周日上午9:00—下午5:00(下午4:30分停止入馆)，周一休馆。", ["S1"])
    assert window.open == "09:00" and window.close == "17:00"
    assert window.last_entry == "16:30" and window.closed_days == [0]
    assert parse_hours("请查询官网开放时间", ["S1"]) is None


def test_structured_hours_missing_fact_row_are_recovered_only_from_exact_source():
    from backend.harness.enrichment import VisitInformation, recover_opening_fact
    from backend.harness.schemas import OpeningHours, SourceRef
    source = SourceRef(id="S1", title="参观须知", url="https://test.gov.cn/visit", level="L1", fetched_at="2026-10-28", text="周二至周日上午9:00—下午5:00(下午4:30分停止入馆)，周一休馆。")
    info = VisitInformation(facts=[], hours=OpeningHours(open="09:00", close="17:00", last_entry="16:30", source_ids=["S1"]))
    recover_opening_fact(info, {"S1":source})
    assert info.facts[0].field == "hours" and info.facts[0].evidence == source.text
    assert info.hours.closed_days == [0]


def test_photo_license_and_phash_reject_unlicensed_and_near_duplicate(tmp_path):
    from PIL import Image, ImageDraw

    from backend.tools.photos import license_allowed, phash

    assert license_allowed("CC BY-SA 4.0")
    assert not license_allowed("CC BY-NC 4.0")
    assert not license_allowed("版权所有")
    image = Image.new("RGB", (200, 120), "white")
    ImageDraw.Draw(image).rectangle((20, 20, 120, 80), fill="green")
    image.save(tmp_path / "a.jpg", quality=95)
    image.save(tmp_path / "b.jpg", quality=88)
    assert (int(phash(tmp_path / "a.jpg"), 16) ^ int(phash(tmp_path / "b.jpg"), 16)).bit_count() < 8


def test_taxi_reference_does_not_trigger_public_transit_gate():
    from backend.harness.schemas import Segment
    document = Deliverable(spec=TaskSpec(type='travel_guide', city='杭州', days=1), days=[DayPlan(
        day=1, theme='湖畔', stops=[], date=date(2026, 10, 31),
        timeline=[ScheduleEntry(start='09:00', end='09:20', kind='travel', place='甲 → 乙')],
        segments=[Segment(from_poi='P1', to_poi='P2', from_name='甲', to_name='乙', mode='打车参考（估算，非公交）', duration_min=20, distance_m=3000,
                          source='OSRM', polyline=[[120,30],[120.01,30.01],[120.02,30.02]], route_verified=True, geometry_mode='driving')],
    )])
    assert not any('公交方案' in issue for issue in route_failures(document))
    document.days[0].segments[0].mode = '公交'
    assert any('公交方案' in issue for issue in route_failures(document))


def test_unknown_booking_cannot_count_as_verified_despite_parking_citation():
    from backend.harness.schemas import FactRecord, SourceRef
    from backend.tools.sources import normalize_facts
    source = SourceRef(id='S1', title='参观须知', url='https://test.gov.cn/visit', level='L1', fetched_at='2026-10-28', text='车辆停车需提前预约')
    poi = POI(id='P1', name='美术馆', facts=[FactRecord(field='booking', value='普通参观预约暂未核实；停车需预约', source_ids=['S1'], evidence=source.text)])
    normalize_facts(poi, [source])
    assert not poi.facts[0].source_ids and poi.facts[0].confidence == 0


def test_age_arithmetic_requires_consistent_years():
    from backend.harness.chronology import age_consistent
    assert age_consistent(1000, 1040, 40)
    assert age_consistent(1000, 1040, 41)
    assert not age_consistent(1000, 1040, 60)
    assert not age_consistent(None, 1040, 40)


def test_budget_drops_old_ticket_copy_after_verification_changes():
    from backend.harness.preparation import prepare
    from backend.harness.quality import UNKNOWN
    from backend.harness.schemas import FactRecord, PracticalItem
    document = Deliverable(spec=TaskSpec(type='travel_guide', city='杭州', days=1),
        pois=[POI(id='P1', name='美术馆', facts=[FactRecord(field='ticket',value=UNKNOWN)])],
        practical=[PracticalItem(category='budget',label='美术馆 · 门票',value='门票20元')])
    prepared = prepare(document)
    assert not any(i.label == '美术馆 · 门票' for i in prepared.practical)


def test_ticket_requires_price_or_free_admission_not_a_reservation_sentence():
    from backend.harness.schemas import FactRecord, SourceRef
    from backend.tools.sources import normalize_facts
    source = SourceRef(id='S1', title='参观须知', url='https://test.gov.cn/visit', level='L1', fetched_at='2026-10-28', text='请通过小程序预约参观票。')
    poi = POI(id='P1', name='展馆', facts=[FactRecord(field='ticket',value=source.text,source_ids=['S1'],evidence=source.text)])
    normalize_facts(poi,[source])
    assert not poi.facts[0].source_ids


def test_page_title_uses_article_heading_and_rejects_missing_title_marker():
    from backend.tools.sources import page_title
    assert page_title('# 西安大唐西市博物馆\nURL: https://example.com\n\n西安大唐西市博物馆\n大唐西市博物馆开放时间调整通告\n2026年10月10日','https://example.com') == '大唐西市博物馆开放时间调整通告'
    assert page_title('# (no title)\n内容','https://example.com','(no title)') == 'example.com'


async def test_replanned_subset_reuses_real_cached_neighbourhood_tiles(tmp_path, monkeypatch):
    import math
    from types import SimpleNamespace

    from PIL import Image, ImageDraw

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.tools.osm import OSM, mercator

    cache = tmp_path / 'map-tiles'
    cache.mkdir()
    zoom = 14
    west, north = mercator(121.49-.006, 31.24+.004, zoom)
    east, south = mercator(121.49+.006, 31.24-.004, zoom)
    for x in range(math.floor(west/256), math.floor(east/256)+1):
        for y in range(math.floor(north/256), math.floor(south/256)+1):
            tile = Image.new('RGB',(256,256),'white')
            ImageDraw.Draw(tile).rectangle((0,0,127,255),fill='green')
            tile.save(cache / f'{zoom}-{x}-{y}.png')

    def unexpected_network(*args, **kwargs):
        raise AssertionError('Cached real tiles should not require a network request')
    monkeypatch.setattr('backend.tools.osm.httpx.AsyncClient', unexpected_network)
    day = SimpleNamespace(stops=[SimpleNamespace(lng=121.49,lat=31.24)],segments=[])
    result = await OSM(Settings(_env_file=None,data_dir=tmp_path), Store(tmp_path)).basemap([day],tmp_path,'day-1')
    assert result['kind']=='raster' and result['coverage'] >= .30
    assert (tmp_path/'day-1-basemap.png').exists()


def test_prepare_removes_only_exact_equally_cited_practical_highlights():
    from backend.harness.preparation import prepare
    from backend.harness.schemas import Block, FactRecord, SourceRef

    ticket = '门票免费；部分收费临特展需另行购票，具体以官方公告为准。'
    source = SourceRef(id='S1',title='参观须知',url='https://museum.gov.cn/visit',level='L1',fetched_at='2026-10-30',text=ticket+'馆内收藏丝绸服饰。')
    poi = POI(id='P1',name='丝绸博物馆',facts=[FactRecord(field='ticket',value=ticket,evidence=ticket,source_ids=['S1'])],blocks=[Block(text='馆内收藏丝绸服饰。',source_ids=['S1']),Block(text=ticket,source_ids=['S1']),Block(text='建议先看服饰展厅。',kind='advice',source_ids=[])])
    document = Deliverable(spec=TaskSpec(type='travel_guide',city='杭州',days=1),pois=[poi],sources=[source],days=[DayPlan(day=1,theme='文化',stops=[poi.model_copy(deep=True)])])
    prepared = prepare(document)
    assert [b.text for b in prepared.pois[0].blocks] == ['馆内收藏丝绸服饰。','建议先看服饰展厅。']
    assert any(f.field=='ticket' and f.value==ticket for f in prepared.pois[0].facts)
    assert any(p.label.endswith(' · 门票') and p.value==ticket for p in prepared.practical)
    assert prepared.days[0].stops[0] is prepared.pois[0]
    snapshot=prepared.pois[0].model_dump(mode='json')
    assert prepare(prepared).pois[0].model_dump(mode='json')==snapshot


def test_prepare_keeps_highlight_when_it_is_the_only_factual_introduction():
    from backend.harness.preparation import prepare
    from backend.harness.schemas import Block, FactRecord, SourceRef

    text='免费开放。'
    source=SourceRef(id='S1',title='参观须知',url='https://museum.gov.cn/visit',level='L1',fetched_at='2026-10-30',text=text)
    poi=POI(id='P1',name='博物馆',facts=[FactRecord(field='ticket',value=text,evidence=text,source_ids=['S1'])],blocks=[Block(text=text,source_ids=['S1'])])
    document=prepare(Deliverable(spec=TaskSpec(type='travel_guide',city='杭州',days=1),pois=[poi],sources=[source]))
    assert document.pois[0].blocks[0].text==text


async def test_one_verified_stop_does_not_request_multi_point_route_matrix(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import httpx

    from backend.app.config import Settings
    from backend.app.store import Store
    from backend.harness.schemas import OpeningHours
    from backend.tools.planning import plan

    async def no_route(*args, **kwargs):
        raise AssertionError('single-stop itinerary must not call a multi-point route service')

    async def locate(city, poi, sources):
        poi.lat,poi.lng,poi.location_verified=40.0,112.0,True

    async def basemap(days,folder,name):
        return {'test':'location map'}

    monkeypatch.setattr(httpx.AsyncClient,'get',no_route)
    poi=POI(id='P1',name='已核实场馆',hours=OpeningHours(open='09:00',close='17:00',source_ids=['S1']))
    geo=SimpleNamespace(config=Settings(_env_file=None),store=Store(tmp_path),locate=locate)
    osm=SimpleNamespace(basemap=basemap)
    days,alternatives,overview=await plan(geo,TaskSpec(city='呼和浩特',type='travel_guide',days=1),[poi],[],osm,tmp_path)
    assert len(days)==1 and [p.id for p in days[0].stops]==['P1']
    assert days[0].segments==[] and days[0].total_minutes<=600
    assert not alternatives and overview=={'test':'location map'}


def test_prepare_removes_route_promises_for_excluded_stops():
    from backend.harness.preparation import prepare
    from backend.harness.schemas import PracticalItem

    document=Deliverable(spec=TaskSpec(city='呼和浩特',type='travel_guide',days=1,assumptions=['按五塔寺—大召寺安排。','出发日期待定。']),pois=[POI(id='P1',name='五塔寺')],alternatives=[{'name':'大召寺','reason':'开放时间未核实'}],practical=[PracticalItem(category='transport',label='一日动线',value='上午五塔寺，下午安排大召寺。',estimated=True),PracticalItem(category='food',label='用餐',value='在大召寺附近午餐。',estimated=True),PracticalItem(category='budget',label='餐饮预留',value='按个人预算安排。',estimated=True)])
    prepared=prepare(document)
    assert all('大召寺' not in p.value for p in prepared.practical)
    assert all('大召寺' not in s for s in prepared.spec.assumptions)
    assert '出发日期待定。' in prepared.spec.assumptions
    assert any(p.label=='餐饮预留' for p in prepared.practical)
    assert any(p.category=='food' and '五塔寺' in p.value for p in prepared.practical)


def test_free_ticket_must_keep_paid_special_exhibition_exception():
    from backend.harness.quality import quality_issues
    from backend.harness.schemas import (
        POI,
        Block,
        Deliverable,
        FactRecord,
        SourceRef,
        TaskSpec,
        now,
    )

    source = SourceRef(id='S1', title='博物馆公告', url='https://museum.gov.cn/notice', level='L1', fetched_at=now(), text='博物馆免费开放。临时展厅的该展览为收费展览。')
    poi = POI(id='P1', name='博物馆', blocks=[Block(text='博物馆免费开放。', source_ids=['S1'])], facts=[FactRecord(field='ticket', value='博物馆免费开放', evidence='博物馆免费开放。', source_ids=['S1'])])
    doc = Deliverable(spec=TaskSpec(type='culture_intro', city='测试城'), sources=[source], pois=[poi])
    issue = '博物馆免费票价说明遗漏收费展览例外'
    assert issue in quality_issues(doc)
    poi.facts[0].value = '普通参观免费；特展收费，价格以官方售票信息为准'
    assert issue not in quality_issues(doc)


def test_paid_exhibit_at_another_museum_does_not_change_free_ticket():
    from backend.harness.quality import quality_issues
    from backend.harness.schemas import (
        POI,
        Block,
        Deliverable,
        FactRecord,
        SourceRef,
        TaskSpec,
        now,
    )

    source = SourceRef(id='S1', title='城市场馆指南', url='https://city.gov.cn/guide', level='L1', fetched_at=now(), text='甲馆免费开放。乙馆特展收费。')
    poi = POI(id='P1', name='甲馆', blocks=[Block(text='甲馆免费开放。', source_ids=['S1'])], facts=[FactRecord(field='ticket', value='免费开放', evidence='甲馆免费开放。', source_ids=['S1'])])
    doc = Deliverable(spec=TaskSpec(type='culture_intro', city='测试城'), sources=[source], pois=[poi])
    assert '甲馆免费票价说明遗漏收费展览例外' not in quality_issues(doc)
