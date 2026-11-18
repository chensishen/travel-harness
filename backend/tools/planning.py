"""Small, deterministic time-window scheduling with explicit transport fallbacks."""

import asyncio
import itertools
import math
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx

from backend.harness.quality import minutes
from backend.harness.schemas import DayPlan, ScheduleEntry, Segment


def clock(value):
    if value >= 1440:
        raise ValueError("行程不能跨越午夜")
    return f"{value // 60:02d}:{value % 60:02d}"


def scheduling_date(spec):
    if spec.start_date:
        return spec.start_date
    explicit = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", spec.season_or_dates)
    if explicit:
        spec.start_date = datetime(*map(int, explicit.groups())).date()
        return spec.start_date
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    value = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    spec.start_date = value
    spec.date_assumed = True
    spec.assumptions.append(
        f"未指定日期，按下一个周六 {value.isoformat()} 起安排；法定节假日及临时公告另行核对。"
    )
    return value


def schedule(stops, legs, visit_date, departure):
    if not stops:
        return None
    current = minutes(departure)
    timeline, meals = [], []

    def meal(label):
        nonlocal current
        timeline.append(
            ScheduleEntry(
                start=clock(current),
                end=clock(current + 45),
                kind="meal",
                place=label,
                note="就近餐饮区用餐，费用另见预算。",
            )
        )
        meals.append(f"{clock(current)}–{clock(current + 45)} {label}")
        current += 45

    for i, poi in enumerate(stops):
        if not poi.hours or visit_date.weekday() in poi.hours.closed_days:
            return None
        if i:
            segment = legs[i - 1]
            duration = max(1, math.ceil(segment.duration_min))
            if (
                11 * 60 <= current <= 13 * 60 + 30
                and not meals
                and stops[i - 1].dining
                and current + 45 + duration + poi.duration_min <= minutes(poi.hours.close)
                and minutes(stops[i - 1].hours.close) - current >= 60
            ):
                meal(
                    stops[i - 1].dining[0]["name"] + " · 午餐（含就近步行预留）"
                )
            timeline.append(
                ScheduleEntry(
                    start=clock(current),
                    end=clock(current + duration),
                    kind="travel",
                    place=f"{stops[i - 1].name} → {poi.name}",
                    transport=segment.mode,
                    note="；".join(segment.instructions),
                )
            )
            current += duration
            if (
                11 * 60 + 30 <= current <= 13 * 60 + 30
                and not meals
                and poi.dining
                and current + 45 + poi.duration_min <= minutes(poi.hours.close)
                and min(minutes(poi.hours.close), minutes(stops[i - 1].hours.close)) - current >= 60
            ):
                meal(poi.dining[0]["name"] + " · 午餐（含就近步行预留）")
        opening = minutes(poi.hours.open)
        if current < opening:
            timeline.append(
                ScheduleEntry(
                    start=clock(current),
                    end=clock(opening),
                    kind="wait",
                    place=poi.name,
                    note="等待开放",
                )
            )
            current = opening
        if (
            poi.hours.last_entry and current > minutes(poi.hours.last_entry)
        ) or current + poi.duration_min > minutes(poi.hours.close):
            return None
        timeline.append(
            ScheduleEntry(
                start=clock(current),
                end=clock(current + poi.duration_min),
                kind="visit",
                place=poi.name,
                poi_id=poi.id,
                note=poi.hours.note,
            )
        )
        current += poi.duration_min
    active = sum(minutes(e.end) - minutes(e.start) for e in timeline if e.kind != "wait")
    buffered = sum(minutes(e.end) - minutes(e.start) for e in timeline if e.kind == "wait")
    if active > 600 or buffered > 60:
        return None
    return timeline, meals, active


async def routed(geo, a, b, foot=True):
    config = geo.config
    base = config.osrm_foot_base_url if foot else config.osrm_base_url
    mode = "foot" if foot else "driving"
    key = geo.store.key("route-v11", f"{base}:{a.lng},{a.lat};{b.lng},{b.lat}")
    data = geo.store.cached(key, 30)
    if data is None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{base}/route/v1/{mode}/{a.lng},{a.lat};{b.lng},{b.lat}",
                params={"overview": "full", "geometries": "geojson"},
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != "Ok" or not payload.get("routes"):
                raise ValueError("路由服务未返回真实路径")
            data = payload["routes"][0]
            geo.store.put_cache(key, data)
    coordinates = data["geometry"]["coordinates"]
    if len(coordinates) < 3:
        raise ValueError("路由 geometry 不足，不能用直线替代")
    return Segment(
        from_poi=a.id,
        to_poi=b.id,
        from_name=a.name,
        to_name=b.name,
        mode="步行参考（估算）" if foot else "打车参考（估算，非公交）",
        duration_min=round(data["duration"] / 60 + (0 if foot else 8), 1),
        distance_m=data["distance"],
        polyline=coordinates,
        source="OSRM 独立步行路由" if foot else "OSRM 驾车路径，仅作打车参考",
        geometry_mode="foot" if foot else "car",
        route_verified=True,
        instructions=[
            f"{a.name} → {b.name}；{'步行' if foot else '打车'}时间为路网参考估算，不含排队。"
        ],
    )


async def plan(
    geo,
    spec,
    pois,
    sources,
    osm,
    folder,
    *,
    report=None,
    v13=False,
    provider_report=None,
    origin_hints=(),
):
    if v13:
        from backend.tools.planning_v13 import plan as plan_v13

        return await plan_v13(
            geo,
            spec,
            pois,
            sources,
            osm,
            folder,
            report=report,
            provider_report=provider_report,
            origin_hints=origin_hints,
        )
    first_date = scheduling_date(spec)
    located = []
    location_alternatives = [
        {"name": p.name, "reason": "未取得可用于排程的开放时间窗，未纳入主线。"}
        for p in pois
        if not p.hours
    ]
    for poi in [p for p in pois if p.hours]:
        if report:
            report("核对景点入口位置：" + poi.name)
        try:
            await geo.locate(spec.city, poi, sources)
        except httpx.HTTPError as error:
            location_alternatives.append(
                {
                    "name": poi.name,
                    "reason": "地图服务暂不可用，入口位置未能核实，未纳入主线；请通过官方渠道确认。",
                    "diagnostic": type(error).__name__,
                }
            )
        except ValueError as error:
            location_alternatives.append(
                {
                    "name": poi.name,
                    "reason": "目标入口位置无法唯一核对，未纳入主线；请通过官方渠道确认。",
                    "diagnostic": str(error),
                }
            )
        else:
            located.append(poi)
    pois = located
    if not pois:
        raise ValueError("没有通过位置核对的景点，不能生成路线")
    # Query the real public-transport relations once per plan, not once per permutation.
    transit, transit_error = {}, ""
    if len(pois) > 1 and "公共" in spec.transport_preference:
        if report:
            report("查询公共交通站点与线路关系")
        try:
            transit = await osm.transit(pois[0], pois[-1])
        except (httpx.HTTPError, ValueError):
            transit_error = (
                "公共交通站点或线路资料未取得；本行程仅给步行或打车参考，不表示公交方案。"
            )
    pairs = {}
    coordinates = ";".join(f"{p.lng},{p.lat}" for p in pois)
    # A foot matrix determines grouping/order, never a driving-time multiplier.
    matrix_key = geo.store.key("foot-table-v11", geo.config.osrm_foot_base_url + coordinates)
    if report:
        report("单景点行程，无景点间交通" if len(pois) == 1 else "获取真实步行时间与距离矩阵")
    # A single verified stop has no travel leg; the table API requires multiple coordinates.
    matrix = (
        {"durations": [[0]], "distances": [[0]]}
        if len(pois) == 1
        else geo.store.cached(matrix_key, 30)
    )
    if matrix is None:
        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.get(
                        f"{geo.config.osrm_foot_base_url}/table/v1/foot/{coordinates}",
                        params={"annotations": "duration,distance"},
                    )
                    response.raise_for_status()
                    matrix = response.json()
                    if matrix.get("code") != "Ok":
                        raise ValueError("步行距离矩阵不可用，不能验证路线排序")
                    geo.store.put_cache(matrix_key, matrix)
                break
            except httpx.TransportError:
                if attempt:
                    raise
                await asyncio.sleep(1)
    positions = {p.id: i for i, p in enumerate(pois)}

    async def leg(a, b):
        key = (a.id, b.id)
        if key not in pairs:
            if report:
                report(f"计算交通路径：{a.name} → {b.name}")
            walk_seconds = matrix["durations"][positions[a.id]][positions[b.id]]
            if walk_seconds is None:
                raise ValueError("候选景点间没有可用路网")
            pairs[key] = await routed(geo, a, b, foot=walk_seconds <= 40 * 60)
            if transit.get("elements"):
                public = await transit_segment(geo, a, b, transit)
                if public and public.duration_min < pairs[key].duration_min:
                    pairs[key] = public
        return pairs[key]

    remaining = list(pois)
    days = []
    alternatives = location_alternatives
    for number in range(1, spec.days + 1):
        date = first_date + timedelta(days=number - 1)
        available = [p for p in remaining if p.hours and date.weekday() not in p.hours.closed_days]
        target = (
            3
            if spec.days == 1
            else min(3, max(1, math.ceil(len(remaining) / (spec.days - number + 1))))
        )

        # At most six candidates are enumerated; a nearest-neighbour shortlist bounds runtime.
        def open_days_left(p):
            return sum(
                (first_date + timedelta(days=i)).weekday() not in p.hours.closed_days
                for i in range(number - 1, spec.days)
            )

        available = sorted(available, key=open_days_left)[:6]
        chosen = None
        for round_number in range(3):
            if report:
                report(
                    f"优化第 {number}/{spec.days} 天时间轴 · 第 {round_number + 1} 轮开放时间与时长检查"
                )
            count = min(target, len(available))
            candidates = []
            for order in itertools.permutations(available, count):
                costs = [
                    matrix["durations"][positions[a.id]][positions[b.id]]
                    for a, b in zip(order, order[1:])
                ]
                if any(c is None for c in costs):
                    continue
                candidates.append(
                    (sum(costs) + sum(open_days_left(p) for p in order) * 100000, order)
                )
            for _, order in sorted(candidates, key=lambda x: x[0])[:12]:
                legs = [await leg(a, b) for a, b in zip(order, order[1:])]
                if sum(s.distance_m > 5000 for s in legs) > 1:
                    continue
                result = schedule(order, legs, date, spec.departure_time)
                if result:
                    chosen = (order, legs, result)
                    break
            if chosen:
                break
            if round_number == 0:
                for p in available:
                    p.duration_min = min(p.duration_min, p.minimum_duration_min)
            else:
                target = max(1, target - 1)
        if not chosen:
            raise ValueError(f"第 {number} 天无法满足开放时间与 10 小时约束，请补充可用景点")
        order, legs, (timeline, meals, total) = chosen
        notes = [transit_error or "交通时长为参考估算；乘车线路仅在取得站点与线路关系时列出。"]
        days.append(
            DayPlan(
                day=number,
                date=date,
                theme=" → ".join(p.name for p in order),
                stops=list(order),
                segments=legs,
                timeline=timeline,
                total_minutes=total,
                meals=meals,
                notes=notes,
            )
        )
        used = {p.id for p in order}
        remaining = [p for p in remaining if p.id not in used]
    for p in remaining:
        alternatives.append(
            {"name": p.name, "reason": "为满足开放时间、少折返或每日 10 小时上限，未纳入主行程。"}
        )
    for day in days:
        if report:
            report(f"准备第 {day.day} 天真实地图底图")
        day.basemap = await osm.basemap([day], folder, f"day-{day.day}")
    if len(days) > 1:
        if report:
            report("准备多日行程总览底图")
        overview = await osm.basemap(days, folder, "overview")
    else:
        overview = days[0].basemap
    return days, alternatives, overview


def route_geometry(route):
    """Join only contiguous route ways; platform outlines are not tracks."""
    parts = route_geometry_parts(route)
    return parts[0] if len(parts) == 1 else []


def route_geometry_parts(route):
    """Keep real connected sections separately; never bridge a missing road."""
    from backend.tools.geo import distance

    geometry, parts, lines = [], [], []
    for member in route.get("members", []):
        if member.get("type") != "way" or member.get("role", "").startswith("platform"):
            continue
        line = [[p["lon"], p["lat"]] for p in member.get("geometry", [])]
        if not line:
            continue
        lines.append(line)
        if geometry:
            last = SimpleNamespace(lng=geometry[-1][0], lat=geometry[-1][1])
            distances = [
                distance(last, SimpleNamespace(lng=p[0], lat=p[1])) for p in [line[0], line[-1]]
            ]
            if min(distances) > 75:
                start = SimpleNamespace(lng=geometry[0][0], lat=geometry[0][1])
                if (
                    min(
                        distance(start, SimpleNamespace(lng=p[0], lat=p[1]))
                        for p in [line[0], line[-1]]
                    )
                    <= 75
                ):
                    geometry.reverse()
                    last = SimpleNamespace(lng=geometry[-1][0], lat=geometry[-1][1])
                    distances = [
                        distance(last, SimpleNamespace(lng=p[0], lat=p[1]))
                        for p in [line[0], line[-1]]
                    ]
                else:
                    parts.append(geometry)
                    geometry = []
            if geometry and distances[1] < distances[0]:
                line.reverse()
        geometry.extend(line)
    if geometry:
        parts.append(geometry)
    if len(parts) > 1:
        # Recover an unordered relation only when its actual way endpoints form one unique chain.
        adjacency = {}
        for i, line in enumerate(lines):
            for point in [line[0], line[-1]]:
                adjacency.setdefault(tuple(point), []).append(i)
        ends = [point for point, edges in adjacency.items() if len(edges) == 1]
        if len(ends) == 2 and all(len(edges) <= 2 for edges in adjacency.values()):
            current, used, joined = ends[0], set(), []
            while remaining := [i for i in adjacency[current] if i not in used]:
                i = remaining[0]
                used.add(i)
                line = lines[i] if tuple(lines[i][0]) == current else list(reversed(lines[i]))
                joined.extend(line[1:] if joined else line)
                current = tuple(line[-1])
            if len(used) == len(lines):
                return [joined]
    return parts


def route_section(parts, start, end):
    from backend.tools.geo import distance

    for geometry in parts:
        indexes = [min(range(len(geometry)), key=lambda i:distance(
            stop, SimpleNamespace(lng=geometry[i][0], lat=geometry[i][1]))) for stop in [start,end]]
        if indexes[0] == indexes[1] or any(distance(stop, SimpleNamespace(lng=geometry[i][0], lat=geometry[i][1])) > 150
                                          for stop,i in zip([start,end],indexes,strict=True)):
            continue
        line = geometry[min(indexes):max(indexes)+1]
        return list(reversed(line)) if indexes[0] > indexes[1] else line
    return []


def route_stops(route):
    """PTv2 permits ordered stop positions, platforms, or both for each service."""
    groups = [
        [m for m in route.get('members', [])
         if m.get('type') == 'node' and 'lat' in m
         and m.get('role') in {role, role + '_entry_only', role + '_exit_only'}]
        for role in ['stop', 'platform']
    ]
    return max(groups, key=len)


async def transit_segment(geo, a, b, data):
    from backend.tools.geo import distance

    names = {
        e["id"]: e.get("tags", {}).get("name", "")
        for e in data.get("elements", [])
        if e.get("type") == "node"
    }
    for route in data.get("elements", []):
        tags = route.get("tags", {})
        if tags.get("route") not in {"bus", "subway"} or not tags.get("ref"):
            continue
        stops = route_stops(route)
        if len(stops) < 2:
            continue
        nearest = []
        for point in [a, b]:
            candidate = min(
                stops, key=lambda s: distance(point, SimpleNamespace(lat=s["lat"], lng=s["lon"]))
            )
            if distance(point, SimpleNamespace(lat=candidate["lat"], lng=candidate["lon"])) > 1000:
                break
            nearest.append(candidate)
        if len(nearest) != 2 or nearest[0]["ref"] == nearest[1]["ref"]:
            continue
        count = abs(stops.index(nearest[1]) - stops.index(nearest[0]))
        if tags["route"] == "bus" and stops.index(nearest[0]) >= stops.index(nearest[1]):
            continue
        line = route_section(route_geometry_parts(route), *[
            SimpleNamespace(lat=stop['lat'],lng=stop['lon']) for stop in nearest])
        if len(line) < 3:
            continue
        stations = []
        for i, stop in enumerate(nearest):
            name = stop.get("tags", {}).get("name") or stop.get("name") or names.get(stop["ref"])
            if not name:
                break
            stations.append(
                SimpleNamespace(
                    id=f"station-{stop['ref']}",
                    name=name,
                    lng=line[0 if i == 0 else -1][0],
                    lat=line[0 if i == 0 else -1][1],
                )
            )
        if len(stations) != 2:
            continue
        if stations[0].name == stations[1].name:
            continue
        try:
            first, last = await routed(geo, a, stations[0]), await routed(geo, stations[1], b)
        except (httpx.HTTPError, ValueError):
            continue
        if max(first.duration_min, last.duration_min) > 15:
            continue
        rail_metres = sum(
            distance(SimpleNamespace(lng=x[0], lat=x[1]), SimpleNamespace(lng=y[0], lat=y[1]))
            for x, y in zip(line, line[1:])
        )
        total = (
            first.duration_min
            + last.duration_min
            + rail_metres / (geo.config.transit_rail_speed_kmh * 1000 / 60)
            + geo.config.transit_wait_min
            + count * geo.config.transit_stop_min
        )
        return Segment(
            from_poi=a.id,
            to_poi=b.id,
            from_name=a.name,
            to_name=b.name,
            mode=("地铁" if tags["route"] == "subway" else "公交") + tags["ref"] + "（估算）",
            duration_min=math.ceil(total * 1.2),
            distance_m=first.distance_m + last.distance_m + rail_metres,
            polyline=first.polyline + line + last.polyline,
            route_verified=True,
            geometry_mode=tags["route"],
            source="OpenStreetMap 站点与线路关系；时长按路网距离估算，不是实时班次",
            instructions=[
                f"步行至{stations[0].name}",
                f"乘{tags['ref']}至{stations[1].name}",
                f"步行至{b.name}",
            ],
            parts=[
                {"mode": "foot", "coordinates": first.polyline},
                {"mode": tags["route"], "coordinates": line},
                {"mode": "foot", "coordinates": last.polyline},
            ],
            transit={
                "mode": "public_transport",
                "confidence": "osm_graph",
                "total_min_range": [max(1, math.floor(total * 0.9)), math.ceil(total * 1.2)],
                "note": "OSM实际线路几何与OSRM步行路网；用时估算，非实时班次",
                "legs": [
                    {
                        "mode": "步行",
                        "from": a.name,
                        "to": stations[0].name,
                        "minutes": math.ceil(first.duration_min),
                    },
                    {
                        "mode": tags["route"],
                        "line": tags["ref"],
                        "from": stations[0].name,
                        "to": stations[1].name,
                        "stops": count,
                    },
                    {
                        "mode": "步行",
                        "from": stations[1].name,
                        "to": b.name,
                        "minutes": math.ceil(last.duration_min),
                    },
                ],
            },
        )
    return None
