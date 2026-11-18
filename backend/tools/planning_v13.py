"""Chapter 15 route constraints with explicit daytime assumptions and first legs."""

import hashlib
import itertools
import json
import math
import re
from datetime import UTC, datetime, timedelta

import httpx

from backend.app.store import save_json
from backend.harness.quality import public_transport_sufficient
from backend.harness.schemas import POI, DayPlan, OpeningHours, ScheduleEntry
from backend.tools.geo import distance
from backend.tools.planning import clock, schedule, scheduling_date
from backend.tools.transit import TransitProvider, transport_label


class CandidateShortage(ValueError):
    pass


class MealVenueShortage(CandidateShortage):
    pass


async def verified_replacements(spec, days, candidates, route, report=None):
    """Replace one stop and rerun the whole day's actual routes and time windows."""
    selected = {p.location_source for d in days for p in d.stops if p.location_source}
    available = [p for p in candidates if p.location_verified and p.lat is not None and p.lng is not None
                 and p.location_source not in selected and p.osm_tags.get('access') not in {'private', 'no'}
                 and p.osm_tags.get('opening_hours') != 'closed'
                 and not any(f.field == 'closure' and f.source_ids for f in p.facts)]
    available.sort(key=lambda p: min(distance(p, q) for d in days for q in d.stops))
    food = {item['source']: item for d in days for p in d.stops for item in p.dining
            if item.get('source') and item.get('lat') is not None and item.get('lng') is not None}
    replacements = []
    seen = set()
    for original in available[:12]:
        if original.location_source in seen:
            continue
        candidate = original.model_copy(deep=True)
        if not candidate.hours:
            from backend.tools.hours import parse_hours
            raw_hours = candidate.osm_tags.get('opening_hours', '')
            candidate.hours = parse_hours(raw_hours, [])
            if candidate.hours:
                candidate.hours.note = '地图开放时段参考；出发前核对临时调整及入口'
                week = re.fullmatch(r'(Mo|Tu|We|Th|Fr|Sa|Su)-(Mo|Tu|We|Th|Fr|Sa|Su)\s+\d{2}:\d{2}-\d{2}:\d{2}', raw_hours)
                if week:
                    weekdays = ['Mo', 'Tu', 'We', 'Th', 'Fr', 'Sa', 'Su']
                    a, b = weekdays.index(week[1]), weekdays.index(week[2])
                    allowed = set(range(a, b + 1)) if a <= b else set(range(a, 7)) | set(range(b + 1))
                    candidate.hours.closed_days = [i for i in range(7) if i not in allowed]
            else:
                candidate.hours = OpeningHours(open='09:00', close='17:00', note='按日间时段规划；开放与入口待出行前核实')
        candidate.dining = []
        from types import SimpleNamespace
        for item in food.values():
            metres = distance(candidate, SimpleNamespace(lat=item['lat'], lng=item['lng']))
            if metres <= 500:
                candidate.dining.append({**item, 'distance_m': round(metres)})
        slots = sorted(((d, i) for d in days for i in range(len(d.stops))),
                       key=lambda slot: distance(candidate, slot[0].stops[slot[1]]))[:3]
        if report:
            report('验证替换景点：' + candidate.name)
        for day, index in slots:
            if not day.origin or day.date.weekday() in candidate.hours.closed_days:
                continue
            order = list(day.stops)
            replaced = order[index]
            order[index] = candidate
            try:
                first = day.arrival_segment if index != 0 else await route(day.origin, order[0])
                legs = [
                    old if (old.from_poi, old.to_poi) == (a.id, b.id) else await route(a, b)
                    for old, a, b in zip(day.segments, order, order[1:], strict=False)
                ]
            except (httpx.HTTPError, ValueError):
                continue
            if not first or any(s.duration_min > 40 for s in legs):
                continue
            if any(not s.route_verified or len(s.polyline) < 3 or s.geometry_mode == 'transit_schematic' for s in [first, *legs]):
                continue
            if sum(s.distance_m > 5000 for s in legs) > 1:
                continue
            depart = clock(int(spec.departure_time[:2]) * 60 + int(spec.departure_time[3:]) + math.ceil(first.duration_min))
            result = schedule(order, legs, day.date, depart)
            if not result:
                continue
            timeline, meals, active = result
            total = active + math.ceil(first.duration_min)
            if total > 600 or (spec.departure_time <= '13:30' < timeline[-1].end and not meals):
                continue
            timeline.insert(0, ScheduleEntry(start=spec.departure_time, end=depart, kind='travel',
                                             place=day.origin.name + ' → ' + order[0].name,
                                             transport=transport_label(first), note=first.transit.get('note', '路网估算')))
            for entry, segment in zip([e for e in timeline if e.kind == 'travel'], [first, *legs], strict=True):
                entry.transport = transport_label(segment)
            proof = day.model_copy(update={'stops': order, 'segments': legs, 'arrival_segment': first,
                                           'timeline': timeline, 'meals': meals, 'total_minutes': total,
                                           'buffer_minutes': sum((int(e.end[:2])*60+int(e.end[3:]))-(int(e.start[:2])*60+int(e.start[3:])) for e in timeline if e.kind == 'wait'),
                                           'basemap': {}, 'map_png': '', 'map_coverage': 0})
            variant = [proof if d.day == day.day else d for d in days]
            if not public_transport_sufficient(spec, variant):
                continue
            visit = next(e for e in timeline if e.kind == 'visit' and e.poi_id == candidate.id)
            replacements.append({'name': candidate.name, 'verified': True, 'reason_code': '', 'replacement_for': replaced.id,
                                 'reason': f'替换{replaced.name}；已按交通与开放时段重排，当日活动{total}分钟。' + candidate.hours.note,
                                 'replace_day': f'第{day.day}天（{day.date}）{visit.start}–{visit.end}，替换{replaced.name}',
                                 'replacement_plan': proof.model_dump(mode='json'),
                                 'source_ids': list(dict.fromkeys(r for f in candidate.facts for r in f.source_ids))})
            seen.add(candidate.location_source)
            break
        if len(replacements) == 3:
            break
    return replacements


async def origin_point(geo, spec):
    explicit = spec.origin not in {"未指定", "", "市区中心位置", spec.city, spec.city + "市"}
    queries = (
        [spec.origin]
        if explicit
        else [
            spec.city + "火车站",
            spec.city + "站",
            spec.city + "人民政府",
            spec.city + "中心广场",
        ]
    )
    for query in queries:
        key = geo.store.key("origin-v13", spec.city + query)
        values = geo.store.cached(key, 365)
        if values is None:
            async with httpx.AsyncClient(
                timeout=25, headers={"User-Agent": geo.config.http_user_agent}
            ) as c:
                r = await c.get(
                    geo.config.nominatim_base_url + "/search",
                    params={
                        "q": query if spec.city in query else spec.city + " " + query,
                        "format": "json",
                        "limit": 3,
                        "addressdetails": 1,
                    },
                )
                r.raise_for_status()
                values = r.json()
            if values:
                geo.store.put_cache(key, values)
        for item in values:
            if not item.get("osm_id") or not item.get("name"):
                continue
            if item.get("class") == "boundary" or item.get("type") in {"administrative", "city"}:
                continue
            if not await geo.within_city(spec.city, item):
                continue
            result = POI(
                id="ORIGIN",
                name=(
                    item["name"] + "站"
                    if item.get("class") == "railway"
                    and item.get("type") == "station"
                    and item["name"] == spec.city
                    else item["name"]
                ),
                lat=item["lat"],
                lng=item["lon"],
                location_verified=True,
                location_source=f"https://www.openstreetmap.org/{item['osm_type']}/{item['osm_id']}",
            )
            if not explicit:
                spec.assumptions.append(
                    f"未指定出发地，以{result.name}为默认起点；首段交通从此处计算。"
                )
            spec.origin = result.name
            geo.store.put_cache(geo.store.key("origin-v13", spec.city + result.name), [item])
            return result
    raise ValueError("没有取得可定位的具体出发点，不能遗漏首段交通")


async def plan(
    geo, spec, pois, sources, osm, folder, *, report=None, provider_report=None, origin_hints=()
):
    date = scheduling_date(spec)
    alternatives = []
    located = []
    source_index = {s.id: s for s in sources}
    for poi in pois:
        closure = next(
            (
                f
                for f in poi.facts
                if f.field == "closure"
                and f.source_ids
                and all(
                    r in source_index and source_index[r].level in {"L1", "L2"}
                    for r in f.source_ids
                )
            ),
            None,
        )
        if (
            closure
            or poi.osm_tags.get("access") in {"private", "no"}
            or poi.osm_tags.get("opening_hours") == "closed"
        ):
            alternatives.append(
                {
                    "name": poi.name,
                    "reason": closure.value + "（" + "、".join(closure.source_ids) + "）"
                    if closure
                    else "地图标注未对公众开放。",
                    "source_ids": closure.source_ids if closure else [],
                    "reason_code": "CLOSED_DAY",
                    "replace_day": "不可替换：闭馆或未对公众开放",
                }
            )
            continue
        if not poi.location_verified or poi.lat is None:
            try:
                await geo.locate(spec.city, poi, sources)
            except (ValueError, httpx.HTTPError) as error:
                alternatives.append(
                    {
                        "name": poi.name,
                        "reason": "未取得可核对坐标。",
                        "reason_code": "MISSING_FACT",
                        "diagnostic": type(error).__name__,
                        "replace_day": "不可替换：定位尚未核实",
                    }
                )
                continue
        if not poi.hours:
            poi.hours = OpeningHours(
                open="09:00",
                close="17:00",
                note="按日间时段排程，开放时间待核实；入口以现场为准",
                source_ids=[],
            )

        def identity(place):
            from backend.harness.candidates import key_name

            return {
                key_name(name.removeprefix(spec.city).removeprefix("市"))
                for name in [place.name, *place.aliases]
            }

        if any(
            distance(poi, other) < 200
            and (
                bool(identity(poi) & identity(other))
                or bool(poi.location_source and poi.location_source == other.location_source)
            )
            for other in located
        ):
            alternatives.append(
                {
                    "name": poi.name,
                    "reason": "与已核对景点为同一实体，合并重复候选。",
                    "reason_code": "DUPLICATE",
                    "replace_day": "无需替换，已合并同一景点",
                }
            )
            continue
        located.append(poi)
    if len(located) < spec.days * 3:
        raise CandidateShortage(f"可排程候选仅{len(located)}个，每天至少3个；回退Researcher补候选")
    departure_min = int(spec.departure_time[:2]) * 60 + int(spec.departure_time[3:])
    # Each day needs three distinct stops and at least two one-minute travel entries.
    if departure_min <= 810 < departure_min + sum(sorted(p.duration_min for p in located)[:3]) + 2:
        dining_stops = sum(bool(p.dining) for p in located)
        if dining_stops < spec.days:
            raise MealVenueShortage(f"{spec.days}天行程必跨午间，但仅{dining_stops}处候选有附近具名餐饮资料，需要补充餐饮点")
    origin = await origin_point(geo, spec)
    origin.official_transit = list(origin_hints)
    geo.routing_city = spec.city
    primary = next((p for p in located if p.category == "博物馆"), located[0])
    primary.duration_min = max(120, primary.duration_min)
    if getattr(spec, "date_assumed", False):
        while date.weekday() in primary.hours.closed_days:
            date += timedelta(days=1)
        spec.start_date = date
    provider = TransitProvider(geo, osm, provider_report)
    geo.configured_preference = spec.transport_preference
    pairs = {}

    async def route(a, b):
        key = (a.id, b.id)
        if key not in pairs:
            if report:
                report("计算到达指引：" + a.name + " → " + b.name)
            pairs[key] = await provider.get_route(a, b, spec.departure_time)
        return pairs[key]

    async def search_days(number, remaining, previous=()):
        if number > spec.days:
            return ([], remaining) if public_transport_sufficient(spec, previous) else None
        visit_date = date + timedelta(days=number - 1)
        available = [p for p in remaining if visit_date.weekday() not in p.hours.closed_days]
        available.sort(key=lambda p: distance(origin, p))
        options = available[: min(8, len(available))]
        for count in [3, 4]:
            if len(options) < count:
                continue
            ranked = sorted(
                itertools.permutations(options, count),
                key=lambda order: (
                    -sum(
                        (date + timedelta(days=later - 1)).weekday() in p.hours.closed_days
                        for p in order
                        for later in range(number + 1, spec.days + 1)
                    ),
                    distance(origin, order[0])
                    + sum(distance(a, b) for a, b in zip(order, order[1:]))
                    - sum(p.planning_priority for p in order) * 10000
                    - sum(
                        sum(
                            f.field in {"ticket", "hours", "booking"} and bool(f.source_ids)
                            for f in p.facts
                        )
                        for p in order
                    )
                    * 10000,
                ),
            )
            orders = ranked[:8]
            # Eight permutations can cover just one stop set; also try distinct nearby sets.
            groups = {frozenset(p.id for p in order) for order in orders}
            for order in ranked:
                if len(groups) >= 8:
                    break
                group = frozenset(p.id for p in order)
                if group not in groups:
                    groups.add(group)
                    orders.append(order)
            for order in orders:
                # Travel only adds time: this order must cross lunchtime and has no meal venue.
                if (departure_min <= 810 <= departure_min + sum(p.duration_min for p in order)
                        and not any(p.dining for p in order)):
                    continue
                # Preserve enough open candidates for every remaining date before routing.
                after = [p for p in remaining if p.id not in {x.id for x in order}]
                if any(
                    sum(
                        (date + timedelta(days=later - 1)).weekday() not in p.hours.closed_days
                        for p in after
                    )
                    < 3
                    for later in range(number + 1, spec.days + 1)
                ):
                    continue
                legs = []
                try:
                    first = await route(origin, order[0])
                    for a, b in zip(order, order[1:]):
                        legs.append(await route(a, b))
                except (httpx.HTTPError, ValueError):
                    continue
                if any(s.duration_min > 40 for s in legs):
                    continue
                if sum(s.distance_m > 5000 for s in legs) > 1:
                    continue
                depart = clock(
                    int(spec.departure_time[:2]) * 60
                    + int(spec.departure_time[3:])
                    + math.ceil(first.duration_min)
                )
                result = schedule(order, legs, visit_date, depart)
                if not result:
                    continue
                timeline, meals, total = result
                if spec.departure_time <= '13:30' < timeline[-1].end and not meals:
                    continue
                total += math.ceil(first.duration_min)
                if total > 600:
                    continue
                entry = ScheduleEntry(
                    start=spec.departure_time,
                    end=depart,
                    kind="travel",
                    place=origin.name + " → " + order[0].name,
                    transport=transport_label(first),
                    note=first.transit["note"],
                )
                timeline.insert(0, entry)
                for item, segment in zip(
                    [x for x in timeline if x.kind == "travel"], [first, *legs], strict=True
                ):
                    item.transport = transport_label(segment)
                day = DayPlan(
                    day=number,
                    date=visit_date,
                    theme=" → ".join(p.name for p in order),
                    stops=list(order),
                    segments=legs,
                    arrival_segment=first,
                    origin=origin,
                    timeline=timeline,
                    meals=meals,
                    total_minutes=total,
                    buffer_minutes=sum(
                        (int(e.end[:2]) * 60 + int(e.end[3:]))
                        - (int(e.start[:2]) * 60 + int(e.start[3:]))
                        for e in timeline
                        if e.kind == "wait"
                    ),
                    notes=[
                        "全日时长仅含游玩、交通和用餐；机动缓冲另列。",
                        "交通时间均为估算；站序示意与OSRM步行路径分别标注。",
                    ],
                )
                future = await search_days(number + 1, after, (*previous, day))
                if future is None:
                    continue
                return [day, *future[0]], future[1]
        return None

    # Map downloads can fail after a costly valid schedule has been found.
    # Reuse only the same inputs and recent actual route results on retry.
    checkpoint = folder / "route-plan-v13.json"
    inputs = {
        "spec": spec.model_dump(mode="json", exclude={"assumptions"}),
        "pois": [p.model_dump(mode="json") for p in located],
        "sources": [s.model_dump(mode="json") for s in sources],
        "origin": origin.model_dump(mode="json"),
    }
    signature = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    cached = json.loads(checkpoint.read_text()) if checkpoint.exists() else None
    reused = cached and cached["signature"] == signature and (
        datetime.fromisoformat(cached["created_at"]) > datetime.now(UTC) - timedelta(days=7)
    )
    if reused:
        days = [DayPlan.model_validate(d) for d in cached["days"]]
        reused = public_transport_sufficient(spec, days) and all(
            d.meals or not (d.timeline and spec.departure_time <= '13:30' < d.timeline[-1].end) for d in days)
    if reused:
        selected = {p.id for d in days for p in d.stops}
        found = days, [p for p in located if p.id not in selected]
        if report:
            report("复用已完成的路线排程，继续获取真实底图")
    else:
        found = await search_days(1, located)
    if found is None:
        raise CandidateShortage(
            "所有候选组合均不能满足每天至少3站、开放日、段间40分钟及跨午间具名午餐约束，需要补同区候选和餐饮资料"
        )
    days, remaining = found
    save_json(checkpoint, {
        "signature": signature,
        "created_at": cached["created_at"] if reused else datetime.now(UTC).isoformat(),
        "days": [d.model_dump(mode="json") for d in days],
    })
    replacements = await verified_replacements(spec, days, remaining, route, report)
    alternatives = replacements + alternatives
    for p in remaining:
        if any(a['name'] == p.name for a in replacements):
            continue
        alternatives.append(
            {
                "name": p.name,
                "reason": "候选景点，需核实开放与交通后替换。",
                "reason_code": "",
                "replace_day": "未验证，不作为可执行替换方案",
            }
        )
    for day in days:
        day.basemap = await osm.basemap([day], folder, f"day-{day.day}")
    overview = await osm.basemap(days, folder, "overview") if len(days) > 1 else days[0].basemap
    return days, alternatives, overview
