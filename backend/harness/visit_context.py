"""Evidence-backed practical content and computed group budgets for v1.3."""

import asyncio
import re
import time
from datetime import date
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from backend.harness.reviewer import evidence_text, numeric_values
from backend.harness.schemas import PracticalItem, SourceRef, now
from backend.tools.geo import distance
from backend.tools.mcp_search import MCPToolError
from backend.tools.sources import source_level


class PracticalCandidate(BaseModel):
    category: str
    label: str
    value: str
    source_ids: list[str]
    evidence: str


class Hint(BaseModel):
    poi_id: str
    line: str
    station: str
    exit: str = ""
    walk_min: int | None = None
    source_id: str
    evidence: str


class Context(BaseModel):
    practical: list[PracticalCandidate] = Field(default_factory=list)
    transit: list[Hint] = Field(default_factory=list)


def monthly_normal(document):
    """Read a reported anomaly and expose the arithmetic, rather than reuse a forecast."""
    if not document.spec.start_date:
        return None
    month = document.spec.start_date.month
    for source in document.sources:
        if source.level not in {"L1", "L2"}:
            continue
        text = re.sub(r"\s+", "", source.text)
        match = re.search(
            rf"气温[：:]?{month}月(?:全市|我市)?平均气温(?:为)?(\d+(?:\.\d+)?)℃[，,]较常年同期偏(高|低)(\d+(?:\.\d+)?)℃",
            text,
        )
        if not match:
            continue
        from decimal import Decimal

        observed, direction, anomaly = match.groups()
        normal = Decimal(observed) + (Decimal(anomaly) if direction == "低" else -Decimal(anomaly))
        return PracticalItem(
            category="notice",
            label=f"{month}月常年气候与穿衣建议",
            source_ids=[source.id],
            value=f"根据官方月报的{month}月实况{observed}℃及较常年同期偏{direction}{anomaly}℃，推算常年同期月均温约{normal:g}℃（计算参考，非本次出行预报）。建议穿长袖并备薄外套，临行按官方预报调整。",
            basis="官方月报实况减去距平推算，穿衣为规划建议",
        )
    return None


async def dining_near(harness, poi, city=''):
    coordinates = f'{poi.lat:.5f},{poi.lng:.5f}'
    key = harness.store.key('dining-places-v2', coordinates)
    cached = harness.store.cached(key, 7)
    if cached is not None:
        return cached
    # Positive records already passed the distance check; empty city-agnostic searches need retry.
    previous = harness.store.cached(harness.store.key('dining-places-v1', coordinates), 7)
    if previous:
        harness.store.put_cache(key, previous)
        return previous
    try:
        rows = await osm_dining_near(harness, poi)
    except (ValueError, httpx.HTTPError) as error:
        from backend.harness.metrics import record_operation
        record_operation('OSM餐饮查询', 'map', error=error)
        rows = []
    if not rows:
        raw = await harness.search.search(
            f'Restaurants within 500 meters of {city} {poi.name} at latitude {poi.lat} longitude {poi.lng}. '
            'Named restaurants with coordinates and addresses. Exa Places.'
        )
        rows = places_dining(raw, poi)
    harness.store.put_cache(key, rows[:3])
    return rows[:3]


def places_dining(raw, poi):
    """Read coordinates from structured place records, never from generated prose."""
    rows = []
    for block in re.split(r'(?m)(?=^Title: )', raw):
        match = re.search(r'(?m)^URL:\s*(https://[^\s]+)', block)
        if not match:
            continue
        url = match[1]
        parsed = urlparse(url)
        if parsed.hostname != 'exa.ai' or not (parsed.path == '/library/places' or re.fullmatch(r'/library/place/[^/]+', parsed.path)):
            continue
        if parsed.path.startswith('/library/place/'):
            name = re.search(r'(?m)^Title:\s*([^\n]+)', block)
            sections = [(name[1].strip(), block)] if name else []
        else:
            sections = [(part.split('\n', 1)[0].strip(), part)
                        for part in re.split(r'(?m)^## ', block)[1:]]
        for name, section in sections:
            category = re.search(r'(?mi)^- (?:\*\*)?Categories:(?:\*\*)?\s*([^\n]+)', section)
            if not category or not re.search(r'restaurant|cafe|fast food', category[1], re.I):
                continue
            if re.search(r'\*\*Status:\*\*\s*(?:Permanently closed|Closed)', section, re.I):
                continue
            coords = re.search(r'(?m)^- Address:[^\n]*\((-?\d+\.\d+),\s*(-?\d+\.\d+)\)', section)
            if not coords:
                coords = re.search(r'\*\*Coordinates:\*\*\s*(-?\d+\.\d+),\s*(-?\d+\.\d+)', section)
            if not coords:
                continue
            lat, lng = map(float, coords.groups())
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                continue
            metres = distance(poi, SimpleNamespace(lat=lat, lng=lng))
            if metres > 500 or any(r['name'] == name for r in rows):
                continue
            rows.append({'name':name, 'lat':lat, 'lng':lng, 'distance_m':round(metres),
                         'kind':category[1].strip(), 'source':url, 'provider':'Exa Places',
                         'evidence':section.strip(), 'accessed_at':date.today().isoformat()})
    return sorted(rows, key=lambda r:r['distance_m'])[:3]


async def osm_dining_near(harness, poi):
    key = harness.store.key("dining-v13", f"{poi.lat:.5f},{poi.lng:.5f}")
    cached = harness.store.cached(key, 7)
    if cached is not None:
        return cached
    try:
        data = await harness.osm.overpass(
            f'[out:json][timeout:15];nwr(around:500,{poi.lat},{poi.lng})[amenity~"^(restaurant|cafe|fast_food)$"][name];out center tags 20;'
        )
        items = []
        for x in data.get("elements", []):
            point = x.get("center", x)
            tags = x.get("tags", {})
            if tags.get("name") and "lat" in point:
                items.append(
                    {
                        "name": tags["name"],
                        "lat": point["lat"],
                        "lon": point["lon"],
                        "type": tags.get("amenity", "restaurant"),
                        "osm_type": x["type"],
                        "osm_id": x["id"],
                    }
                )
    except (ValueError, httpx.HTTPError):
        # A bounded OSM search is a second transport for the same real place data.
        params = {
            "q": "[restaurant]",
            "format": "json",
            "bounded": 1,
            "viewbox": f"{poi.lng - 0.005},{poi.lat + 0.005},{poi.lng + 0.005},{poi.lat - 0.005}",
            "limit": 6,
        }
        async with harness.geo.lock:
            await asyncio.sleep(max(0, 1.1 - (time.monotonic() - harness.geo.last)))
            try:
                async with httpx.AsyncClient(
                    timeout=20, headers={"User-Agent": harness.config.http_user_agent}
                ) as c:
                    r = await c.get(harness.config.nominatim_base_url + "/search", params=params)
                    r.raise_for_status()
                    items = r.json()
            finally:
                harness.geo.last = time.monotonic()
    rows = []
    for x in items:
        if not x.get("name") or not x.get("osm_id"):
            continue
        metres = distance(poi, SimpleNamespace(lat=float(x["lat"]), lng=float(x["lon"])))
        if metres > 500:
            continue
        rows.append(
            {
                "name": x["name"],
                "distance_m": round(metres),
                "kind": x.get("type", "restaurant"),
                "lat": float(x["lat"]),
                "lng": float(x["lon"]),
                "source": f"https://www.openstreetmap.org/{x['osm_type']}/{x['osm_id']}",
                "accessed_at": date.today().isoformat(),
            }
        )
    rows.sort(key=lambda x: x["distance_m"])
    harness.store.put_cache(key, rows[:3])
    return rows[:3]


async def enrich_context(harness, document, folder):
    document.origin_hints = []
    for poi in document.pois:
        poi.official_transit = []
    document.practical = [
        p
        for p in document.practical
        if p.category != "food"
        and not (p.category == "notice" and ("气候" in p.label or "天气" in p.label))
    ]
    by_id = {s.id: s for s in document.sources}
    result = await harness.models.json(
        "从材料抽取有来源的实用信息，不写通用套话。practical只填写food（本地菜系/餐饮区）、climate（出行月份的常年气候及具体穿衣建议，不能用当前一日天气或往年的天气预报代替常年气候；不可把某年预测温度或冷空气次数用于今年）、arrival（高铁站或机场到市区的具体方式；城市到达不受当前市内起点限制，材料中有机场或高铁接驳就应抽取，不能因当前起点为火车站而忽略机场进城信息），每项evidence为连续原文，value不增加来源外数字、专名。无资料留空。transit抽取目标景点官方交通指南的line/station/exit/walk_min；只引用L1，未载明的出口或分钟留空，米不能填进walk_min。禁止猜线路站点。",
        {
            "city": document.spec.city,
            "month": document.spec.start_date.month if document.spec.start_date else None,
            "pois": [
                {"id": "ORIGIN", "name": document.spec.origin},
                *[{"id": p.id, "name": p.name} for p in document.pois],
            ],
            "sources": [
                s.model_dump(mode="json")
                for s in document.sources
                if not (
                    s.published_at
                    and document.spec.start_date
                    and s.published_at.year != document.spec.start_date.year
                    and re.search(r"预计|将有|预报|预测", s.title)
                )
            ],
        },
        Context,
    )
    from backend.app.store import save_json

    save_json(folder / "practical-candidates.json", result.model_dump(mode="json"))
    for item in result.practical:
        refs = [r for r in item.source_ids if r in by_id and by_id[r].level in {"L1", "L2"}]
        if item.category == "climate":
            refs = [
                r
                for r in refs
                if not (
                    by_id[r].published_at
                    and document.spec.start_date
                    and by_id[r].published_at.year != document.spec.start_date.year
                    and re.search(r"预计|将有|预报|预测|大致出现在", by_id[r].title + item.evidence)
                )
            ]
        if (
            not refs
            or not any(evidence_text(item.evidence) in evidence_text(by_id[r].text) for r in refs)
            or not numeric_values(item.value) <= numeric_values(item.evidence)
        ):
            continue
        category = {"food": "food", "climate": "notice", "arrival": "transport"}.get(item.category)
        if category:
            if item.category == "climate" and document.spec.start_date:
                period = next(
                    (
                        match
                        for r in refs
                        if (
                            match := re.search(
                                r"(20\d{2})\s*年\s*"
                                + str(document.spec.start_date.month)
                                + r"\s*月",
                                by_id[r].title + " " + by_id[r].text[:300],
                            )
                        )
                    ),
                    None,
                )
                if period and int(period[1]) != document.spec.start_date.year:
                    item.value = (
                        f"历史气候参考（{period[1]}年{document.spec.start_date.month}月实况，非本次出行预报）："
                        + item.value
                    )
            document.practical.append(
                PracticalItem(
                    category=category, label=item.label, value=item.value, source_ids=refs
                )
            )
    if not any(
        p.category == "notice" and ("气候" in p.label or "天气" in p.label)
        for p in document.practical
    ):
        if normal := monthly_normal(document):
            document.practical.append(normal)
    for hint in result.transit:
        source = by_id.get(hint.source_id)
        poi = next((p for p in document.pois if p.id == hint.poi_id), None)
        if (
            not source
            or source.level != "L1"
            or (not poi and hint.poi_id != "ORIGIN")
            or not hint.evidence
            or evidence_text(hint.evidence) not in evidence_text(source.text)
        ):
            continue
        if not all(value in hint.evidence for value in [hint.line, hint.station]):
            continue
        if hint.walk_min is not None and not re.search(
            rf"{hint.walk_min}\s*分(?:钟)?", hint.evidence
        ):
            hint.walk_min = None
        if hint.poi_id == "ORIGIN":
            document.origin_hints.append(hint.model_dump())
        else:
            poi.official_transit.append(hint.model_dump())
    for poi in document.pois:
        if poi.lat is None:
            try:
                await harness.geo.locate(document.spec.city, poi, document.sources)
            except (ValueError, httpx.HTTPError):
                continue
        try:
            poi.dining = await dining_near(harness, poi, document.spec.city)
        except (ValueError, httpx.HTTPError, MCPToolError) as error:
            harness.store.event(
                folder.name,
                node="Writer",
                status="running",
                message=poi.name + "餐饮点检索失败：" + type(error).__name__,
            )
        for place in poi.dining[:3]:
            existing = next((s for s in document.sources if str(s.url) == place["source"]), None)
            if not existing:
                existing = SourceRef(
                    id=f"S{max([int(s.id[1:]) for s in document.sources if re.fullmatch(r'S\d+', s.id)], default=0) + 1}",
                    title=place["name"] + " · " + place.get('provider', 'OpenStreetMap'),
                    url=place["source"],
                    level=source_level(place['source']),
                    fetched_at=now(),
                    text=place.get('evidence') or f"{place['name']}；类型{place['kind']}；坐标{place['lat']},{place['lng']}；位置来自OSM，非价格或营业承诺。",
                )
                document.sources.append(existing)
            elif place.get('evidence') and place['evidence'] not in existing.text:
                existing.text += '\n\n' + place['evidence']
            document.practical.append(
                PracticalItem(
                    category="food",
                    label=place["name"],
                    value=f"{place['name']} · {place['kind']}；距{poi.name}约{place['distance_m']}米（坐标距离估算）。营业与菜单请出发前核对，不提供无来源价格。",
                    source_ids=[existing.id],
                    basis=place.get('provider', 'OpenStreetMap') + '点位与坐标距离计算',
                )
            )
        poi.nationally_rated = any(
            poi.name in s.text
            and re.search(
                r"国家[一二三级]*级|国家[级]?AAAA[ A]?|国家[级]?4A|全国重点文物",
                s.text[max(0, s.text.find(poi.name) - 100) : s.text.find(poi.name) + 700],
            )
            for s in document.sources
            if s.level in {"L1", "L2"}
        )
    return document


def normalize_assumptions(document):
    spec = document.spec
    cleaned = []
    for text in spec.assumptions:
        if re.search(
            r"工作日|周[一二三四五六日天]|停车预约|个人参观预约|来源残|未指定.{0,8}日期|(?:起点|出发地|出发点).*(?:未指定|市区中心)|^出行日期按|^出发点为|^未指定出发地，以",
            text,
        ):
            continue
        if text not in cleaned:
            cleaned.append(text)
    date_note = (
        f"出行日期按{spec.start_date}起安排，日期及闭馆日以每日时间轴为准。"
        if spec.start_date
        else "出行日期待确定。"
    )
    origin_note = (
        f"出发点为{spec.origin}，首段交通从该处计算。"
        if spec.origin not in {"未指定", "", "市区中心位置"}
        else "出发点将在路线规划中按实际站点确定。"
    )
    spec.assumptions = list(dict.fromkeys([date_note, origin_note, *cleaned]))[:6]


def active_exhibitions(document):
    """Retain dated, venue-specific exclusive-exhibition notices from official sources."""
    notices = []
    seen = set()
    for poi in document.pois:
        visit = next((d.date for d in document.days if any(p.id == poi.id for p in d.stops)), None)
        visit = visit or document.spec.start_date or date.today()
        refs = {r for f in poi.facts for r in f.source_ids}
        for source in document.sources:
            if source.level != "L1" or source.id not in refs:
                continue
            for line in source.text.splitlines():
                if "将不再展出其他展览" not in line:
                    continue
                subject = line.split("将不再展出其他展览")[0]
                if not any(name in subject for name in [poi.name, *poi.aliases]):
                    continue
                title = re.search(r'[“《]([^”》]+)[”》].*?期间', line)
                period = re.search(r'(\d{4})[-./年](\d{1,2})[-./月](\d{1,2})日?\s*[-—–至]\s*(\d{4})[-./年](\d{1,2})[-./月](\d{1,2})日?', line)
                if not title or not period:
                    continue
                try:
                    start, end = date(*map(int, period.groups()[:3])), date(*map(int, period.groups()[3:]))
                except ValueError:
                    continue
                key = poi.id, title[1], start, end
                if start <= visit <= end and key not in seen:
                    seen.add(key)
                    notices.append(dict(poi_id=poi.id, title=title[1], text=line.strip(), source_id=source.id))
    return notices


def apply_exhibition_notices(document):
    by_id = {p.id: p for p in document.pois}
    for notice in active_exhibitions(document):
        item = PracticalItem(category="notice", label=by_id[notice["poi_id"]].name + " · 当期展览限制",
                             value=notice["text"], source_ids=[notice["source_id"]])
        if item not in document.practical:
            document.practical.append(item)


def adult_ticket_price(value, visit_date):
    """Read the full adult fare without mistaking calendar dates or concessions for prices."""
    if value.startswith("免费"):
        return 0.0
    seasons = list(re.finditer(
        r"(?:旺季|淡季)[（(]([^）)]+)[）)]\s*(?:成人)?(?:每位|每人)?\s*(\d+(?:\.\d+)?)\s*元",
        value,
    ))
    if seasons:
        if not visit_date or not re.search(r"成人|全票", value):
            return None
        current = visit_date.month, visit_date.day
        for season in seasons:
            for start_month, start_day, end_month, end_day in re.findall(
                r"(\d{1,2})月(\d{1,2})日至(\d{1,2})月(\d{1,2})日", season[1]
            ):
                start = int(start_month), int(start_day)
                end = int(end_month), int(end_day)
                if (start <= current <= end) if start <= end else (current >= start or current <= end):
                    return float(season[2])
        return None
    match = re.search(r"(?:成人(?:票|门票)?|全票)\s*(?:原价)?\s*[：:]?\s*(\d+(?:\.\d+)?)\s*元", value)
    if not match:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*元(?:/人)?[。.]?", value)
    return float(match[1]) if match else None


def group_budget(document):
    n = document.spec.travelers
    days = document.spec.days or 1
    document.practical = [p for p in document.practical if p.category != "budget"]
    known = 0.0
    unknown = []
    for poi in document.pois:
        fact = next((f for f in poi.facts if f.field == "ticket" and f.source_ids), None)
        if not fact:
            unknown.append(poi.name)
            continue
        price = adult_ticket_price(fact.value, document.spec.start_date)
        if price is None:
            unknown.append(poi.name)
        else:
            known += price * n
        document.practical.append(
            PracticalItem(
                category="budget",
                label=poi.name + " · 门票",
                value=fact.value.rstrip('。；;') + f"；按{n}人计算，优惠条件与特展另核。",
                source_ids=fact.source_ids,
                amount_range=[price * n, price * n] if price is not None else [],
            )
        )
    food = [60 * n * days, 120 * n * days]
    transit = [20 * n * days, 60 * n * days]
    document.practical.extend(
        [
            PracticalItem(
                category="budget",
                label=f"{n}人餐饮经验预留",
                value=f"{food[0]}–{food[1]}元，{days}天；仅为预算预留，非餐厅报价。",
                estimated=True,
                amount_range=food,
                basis="经验参考",
            ),
            PracticalItem(
                category="budget",
                label=f"{n}人市内交通经验预留",
                value=f"{transit[0]}–{transit[1]}元，{days}天；不含到达及离城费用，非实时票价。",
                estimated=True,
                amount_range=transit,
                basis="经验参考",
            ),
        ]
    )
    total = [known + food[0] + transit[0], known + food[1] + transit[1]]
    note = (
        ("未含" + "、".join(unknown) + "未核实门票；")
        if unknown
        else "收费特展、个人购物及往返城市费用另计；"
    )
    document.practical.append(
        PracticalItem(
            category="budget",
            label=f"{n}人{days}天合计（含经验参考）",
            value=f"{total[0]:g}–{total[1]:g}元；" + note + "经验项非有据报价。",
            estimated=True,
            amount_range=total,
            basis="已知门票＋餐饮、交通经验参考",
        )
    )
