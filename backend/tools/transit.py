"""Official directions, routable OSM station graphs, and explicitly labelled fallback."""

import asyncio
import heapq
import math
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx

from backend.harness.schemas import Segment
from backend.tools.geo import distance


def prefers_walking(preference):
    return any(term in preference for term in ("步行优先", "优先步行", "步行为主"))


class TransitProvider:
    def __init__(self, geo, osm, report=None):
        self.geo, self.osm, self.report = geo, osm, report
        self.transit_data = {}
        self.network_fetched_at = None
        self._graph_elements = None
        self._graph = None
        city = getattr(geo, "routing_city", "")
        self.network_key = geo.store.key("transit-city-v13", city) if city else ""
        saved = geo.store.cached(self.network_key, 7) if city else None
        if saved and datetime.fromisoformat(saved["fetched_at"]) > datetime.now(UTC) - timedelta(
            days=7
        ):
            self.transit_data = {(e["type"], e["id"]): e for e in saved["elements"]}
            self.network_fetched_at = saved["fetched_at"]

    def remember_network(self, data):
        self.transit_data.update(((e["type"], e["id"]), e) for e in data.get("elements", []))
        if self.network_key and data.get("_fetched_at"):
            self.network_fetched_at = min(
                filter(None, [self.network_fetched_at, data["_fetched_at"]])
            )
            self.geo.store.put_cache(
                self.network_key,
                {
                    "elements": list(self.transit_data.values()),
                    "fetched_at": self.network_fetched_at,
                },
            )

    def log(self, tier, **fields):
        if self.report:
            self.report({"provider": tier, **fields})

    async def get_route(self, a, b, depart_time="09:00"):
        preference = getattr(self.geo, "configured_preference", "公共交通")
        if prefers_walking(preference):
            from backend.tools.planning import routed

            try:
                walk = await routed(self.geo, a, b, True)
            except (httpx.HTTPError, ValueError) as error:
                self.log("fallback", status="walk_unavailable", error_type=type(error).__name__)
            else:
                if walk.duration_min <= 40:
                    return await self.fallback_route(a, b)
        providers = [OfficialHintProvider(self), OsmGraphProvider(self), FallbackProvider(self)]
        if "公共" not in preference:
            providers = providers[-1:]
        for provider in providers:
            try:
                result = await provider.get_route(a, b, depart_time)
            except (httpx.HTTPError, ValueError) as error:
                self.log(
                    provider.tier,
                    status="error",
                    error_type=type(error).__name__,
                    reason=str(error)[:300],
                )
                continue
            if result:
                self.log(provider.tier, status="hit")
                if provider.tier != "fallback":
                    from backend.tools.planning import routed

                    try:
                        walk = await routed(self.geo, a, b, True)
                    except (httpx.HTTPError, ValueError):
                        walk = None
                    if walk and walk.duration_min <= 15 and walk.duration_min < result.duration_min:
                        self.log(
                            provider.tier,
                            status="not_selected",
                            reason="两点间短途步行比进出站更快",
                        )
                        return await self.fallback_route(a, b)
                return result
            self.log(provider.tier, status="miss", reason=provider.miss_reason)
        raise ValueError("官方指引、站序图与路网兜底均未取得可信交通")

    async def fallback_route(self, a, b):
        from backend.tools.planning import routed

        try:
            walk = await routed(self.geo, a, b, True)
        except (httpx.HTTPError, ValueError):
            walk = None
        if walk and walk.duration_min <= 40:
            segment = walk
            low = round(walk.duration_min)
            high = math.ceil(walk.duration_min * 1.15)
            note = "OSRM步行路网估算，非班次时间"
        else:
            segment = await routed(self.geo, a, b, False)
            base = max(1, segment.duration_min - 8)
            low = math.ceil(base * self.geo.config.taxi_time_min_multiplier)
            high = math.ceil(base * self.geo.config.taxi_time_max_multiplier + 8)
            note = "驾车路网推算打车时间，非公交时间"
        segment.transit = {
            "mode": segment.geometry_mode,
            "legs": [{"from": a.name, "to": b.name, "mode": segment.mode}],
            "total_min_range": [low, high],
            "confidence": "fallback",
            "note": note,
        }
        common = next(
            (
                (x, y)
                for x in a.official_transit
                for y in b.official_transit
                if x["station"].removesuffix("站") == y["station"].removesuffix("站")
                and x.get("source_id")
                and y.get("source_id")
            ),
            None,
        )
        if common and segment.geometry_mode == "foot":
            segment.transit["same_station_catchment"] = {
                "station": common[0]["station"],
                "source_ids": list(dict.fromkeys([common[0]["source_id"], common[1]["source_id"]])),
            }
            segment.transit["source_ids"] = segment.transit["same_station_catchment"]["source_ids"]
            segment.transit["note"] += (
                "；两端官方到达指引共用" + common[0]["station"] + "，该段为同站区域内步行"
            )
        segment.duration_min = high
        segment.instructions = [f"{a.name} → {b.name}；{note}"]
        self.log("fallback", status="hit", reason=note)
        return segment

    def _build_graph(self, elements):
        from backend.tools.planning import route_geometry_parts, route_section, route_stops

        stops = {}
        adjacency = {}
        routes = []
        shapes = {}
        for relation in elements:
            tags = relation.get("tags", {})
            mode = tags.get("route")
            if mode not in {"subway", "light_rail", "tram", "bus"} or not (
                tags.get("ref") or tags.get("name")
            ):
                continue
            members = route_stops(relation)
            if len(members) < 2:
                continue
            line = tags.get("ref", tags.get("name"))
            route = []
            for m in members:
                name = m.get("tags", {}).get("name") or m.get("name")
                if not name:
                    name = next(
                        (
                            x.get("tags", {}).get("name")
                            for x in elements
                            if x.get("type") == "node" and x["id"] == m["ref"]
                        ),
                        None,
                    )
                if not name:
                    continue
                if route and stops[route[-1]].name == name:
                    continue
                sid = str(m["ref"])
                stops[sid] = SimpleNamespace(id=sid, name=name, lat=m["lat"], lng=m["lon"])
                route.append(sid)
            if len(route) < 2:
                continue
            routes.append((route, mode, line))
            geometry = route_geometry_parts(relation)
            for x, y in zip(route, route[1:]):
                shape = []
                if geometry:
                    shape = route_section(geometry, stops[x], stops[y])
                    if shape:
                        shapes[(x, y, mode, line)] = shape
                        if mode != "bus":
                            shapes[(y, x, mode, line)] = list(reversed(shape))
                cost = (
                    sum(
                        distance(
                            SimpleNamespace(lng=p[0], lat=p[1]), SimpleNamespace(lng=q[0], lat=q[1])
                        )
                        for p, q in zip(shape, shape[1:])
                    )
                    if shape
                    else distance(stops[x], stops[y])
                ) / (
                    self.geo.config.transit_rail_speed_kmh * 1000 / 60
                ) + self.geo.config.transit_stop_min
                adjacency.setdefault(x, []).append((y, cost, mode, line))
                if mode != "bus":
                    adjacency.setdefault(y, []).append((x, cost, mode, line))
        for x, sx in stops.items():
            for y, sy in stops.items():
                if x >= y:
                    continue
                metres = distance(sx, sy)
                if sx.name == sy.name or metres <= 150:
                    cost = metres / 70 + self.geo.config.transit_transfer_min
                    adjacency.setdefault(x, []).append((y, cost, "transfer", "换乘"))
                    adjacency.setdefault(y, []).append((x, cost, "transfer", "换乘"))
        return stops, adjacency, shapes

    def _shortest_path(self, stops, adjacency, first, last):
        best = None
        for source, walk_a in first:
            queue = [(walk_a.duration_min, source, "", [])]
            seen = {}
            while queue:
                cost, node, previous, path = heapq.heappop(queue)
                # Remaining rides and the exit walk cannot reduce the total cost.
                if best is not None and cost + self.geo.config.transit_wait_min >= best[0]:
                    break
                if cost >= seen.get((node, previous), float("inf")):
                    continue
                seen[(node, previous)] = cost
                for target, walk_b in last:
                    if (
                        node == target
                        and path
                        and stops[source].name != stops[target].name
                        and any(edge[2] != "transfer" for edge in path)
                    ):
                        total = cost + walk_b.duration_min + self.geo.config.transit_wait_min
                        if best is None or total < best[0]:
                            best = (total, walk_a, walk_b, path)
                for target, delta, mode, line in adjacency.get(node, []):
                    transfer = (
                        self.geo.config.transit_transfer_min
                        if previous and line != previous and mode != "transfer"
                        else 0
                    )
                    heapq.heappush(
                        queue,
                        (
                            cost + delta + transfer,
                            target,
                            ("" if mode == "transfer" else line),
                            path + [(node, target, mode, line)],
                        ),
                    )
        return best

    async def osm_route(self, a, b, data):
        from backend.tools.planning import routed

        elements = data.get("elements", [])
        if self._graph_elements != elements:
            graph = await asyncio.to_thread(self._build_graph, elements)
            self._graph_elements = list(elements)
            self._graph = graph
        stops, adjacency, shapes = self._graph
        if not stops:
            return None

        async def accessible(point, *, reverse=False):
            found = []
            ordered = sorted(stops.values(), key=lambda x: distance(point, x))
            names = list(dict.fromkeys(s.name for s in ordered if distance(point, s) <= 1600))[:3]
            for station in (s for s in ordered if s.name in names):
                if distance(point, station) > 1600:
                    continue
                try:
                    walk = (
                        await routed(self.geo, station, point, True)
                        if reverse
                        else await routed(self.geo, point, station, True)
                    )
                except (httpx.HTTPError, ValueError):
                    continue
                if walk.duration_min <= 15:
                    found.append((station.id, walk))
            return found

        first = await accessible(a)
        last = await accessible(b, reverse=True)
        if not first or not last:
            return None
        best = await asyncio.to_thread(self._shortest_path, stops, adjacency, first, last)
        if not best:
            return None
        total, walk_a, walk_b, path = best
        station_names = [stops[path[0][0]].name, stops[path[-1][1]].name]
        hints = [
            h
            for h in b.official_transit
            if any(
                h.get("station", "") == name and h.get("line") in {x[3] for x in path}
                for name in station_names
            )
        ]
        confidence = "official" if hints else "osm_graph"
        legs = [
            {
                "mode": "步行",
                "from": a.name,
                "to": station_names[0],
                "minutes": math.ceil(walk_a.duration_min),
            }
        ]
        for x, y, mode, line in path:
            if legs[-1].get("line") == line:
                legs[-1].update(to=stops[y].name, stops=legs[-1]["stops"] + 1)
            else:
                legs.append(
                    {
                        "mode": mode,
                        "line": line,
                        "from": stops[x].name,
                        "to": stops[y].name,
                        "stops": 1,
                    }
                )
        legs.append(
            {
                "mode": "步行",
                "from": station_names[1],
                "to": b.name,
                "minutes": math.ceil(walk_b.duration_min),
            }
        )
        instructions = [f"步行{legs[0]['minutes']}分钟至{station_names[0]}"]
        instructions.extend(
            f"{'换乘' if i else '乘'}{leg['line']} {leg['stops']}站至{leg['to']}"
            for i, leg in enumerate(legs[1:-1])
            if leg.get("line")
        )
        instructions.append(f"步行{legs[-1]['minutes']}分钟至{b.name}")
        # Station graph edges are an estimated schematic, never a claimed OSRM track.
        coords = (
            walk_a.polyline
            + [[stops[x].lng, stops[x].lat] for x, _, _, _ in path]
            + [[stops[path[-1][1]].lng, stops[path[-1][1]].lat]]
            + walk_b.polyline
        )
        parts = [{"mode": "foot", "coordinates": walk_a.polyline}]
        actual = True
        for x, y, mode, line in path:
            if mode == "transfer":
                if distance(stops[x], stops[y]) < 1:
                    continue
                try:
                    transfer = await routed(self.geo, stops[x], stops[y], True)
                except (httpx.HTTPError, ValueError):
                    actual = False
                    break
                parts.append({"mode": "foot", "coordinates": transfer.polyline})
            elif shape := shapes.get((x, y, mode, line)):
                parts.append({"mode": mode, "coordinates": shape})
            else:
                actual = False
                break
        if actual:
            parts.append({"mode": "foot", "coordinates": walk_b.polyline})
            coords = [p for part in parts for p in part["coordinates"]]
        return Segment(
            from_poi=a.id,
            to_poi=b.id,
            from_name=a.name,
            to_name=b.name,
            mode="公共交通（估算）",
            duration_min=math.ceil(total),
            distance_m=(
                sum(
                    sum(
                        distance(
                            SimpleNamespace(lng=p[0], lat=p[1]), SimpleNamespace(lng=q[0], lat=q[1])
                        )
                        for p, q in zip(part["coordinates"], part["coordinates"][1:])
                    )
                    for part in parts
                )
                if actual
                else walk_a.distance_m
                + walk_b.distance_m
                + sum(distance(stops[x], stops[y]) for x, y, _, _ in path)
            ),
            polyline=coords,
            source="OSM实际线路几何与OSRM步行、换乘地面路网；站内以指引为准"
            if actual
            else "OSM连续站序与OSRM步行；站间连线为线路示意，非实际轨迹",
            route_verified=True,
            geometry_mode=("bus" if all(edge[2] == "bus" for edge in path) else "subway")
            if actual
            else "transit_schematic",
            instructions=instructions,
            transit={
                "mode": "public_transport",
                "legs": legs,
                "total_min_range": [max(1, math.floor(total * 0.9)), math.ceil(total * 1.2)],
                "confidence": confidence,
                "source_ids": [h["source_id"] for h in hints if h.get("source_id")],
                "note": "OSM实际线路与步行、换乘地面路网估算，非实时班次；站内以指引为准"
                if actual
                else "时长估算，非实时班次；官方交通指引与OSM站名核对"
                if hints
                else "OSM站序图与步行路网估算，非实时班次",
            },
            parts=parts
            if actual
            else [
                {"mode": "foot", "coordinates": walk_a.polyline},
                {
                    "mode": "transit_schematic",
                    "coordinates": coords[len(walk_a.polyline) : -len(walk_b.polyline)],
                },
                {"mode": "foot", "coordinates": walk_b.polyline},
            ],
        )


def transport_label(segment):
    t = segment.transit
    span = t.get("total_min_range", [round(segment.duration_min)] * 2)
    stations = [leg for leg in t.get("legs", []) if leg.get("line")]
    stop_text = (
        "；".join(f"{leg['line']} {leg['from']}→{leg['to']}" for leg in stations)
        or f"{segment.from_name}→{segment.to_name}"
    )
    return f"{segment.mode} · {span[0]}–{span[-1]}分钟 · {stop_text} · {t.get('confidence', 'fallback')}"


class OfficialHintProvider:
    tier = "official"
    miss_reason = "未取得两端同线官方指引或站点未在OSM唯一匹配"

    def __init__(self, parent):
        self.parent = parent

    async def station(self, name):
        geo = self.parent.geo
        city = getattr(geo, "routing_city", "")
        key = geo.store.key("station-v13", city + name)
        cached = geo.store.cached(key, 7)
        if cached is not None:
            return SimpleNamespace(**cached)
        import asyncio
        import time

        async with geo.lock:
            await asyncio.sleep(max(0, 1.1 - (time.monotonic() - geo.last)))
            try:
                async with httpx.AsyncClient(
                    timeout=20, headers={"User-Agent": geo.config.http_user_agent}
                ) as c:
                    r = await c.get(
                        geo.config.nominatim_base_url + "/search",
                        params={
                            "q": city + " " + name,
                            "format": "json",
                            "limit": 5,
                            "addressdetails": 1,
                            "namedetails": 1,
                            "accept-language": "zh,en",
                        },
                    )
                    r.raise_for_status()
                    items = r.json()
            finally:
                geo.last = time.monotonic()
        matches = []
        for item in items:
            names = [item.get("name", ""), *(item.get("namedetails") or {}).values()]
            if item.get("class") not in {"railway", "public_transport"} or not any(
                x.removesuffix("站") == name.removesuffix("站") for x in names
            ):
                continue
            if not await geo.within_city(city, item):
                continue
            matches.append(item)
        if not matches:
            return None
        if (
            max(
                distance(
                    SimpleNamespace(lat=float(a["lat"]), lng=float(a["lon"])),
                    SimpleNamespace(lat=float(b["lat"]), lng=float(b["lon"])),
                )
                for a in matches
                for b in matches
            )
            > 200
        ):
            return None
        item = matches[0]
        value = {
            "id": "station-" + str(item["osm_id"]),
            "name": item["name"],
            "lat": float(item["lat"]),
            "lng": float(item["lon"]),
        }
        geo.store.put_cache(key, value)
        return SimpleNamespace(**value)

    async def get_route(self, a, b, depart_time):
        import re

        from backend.tools.planning import routed

        def line_key(value):
            value = re.sub(r"地铁|轨道交通|号线|线|\s", "", value)
            return value.translate(str.maketrans("一二三四五六七八九", "123456789"))

        for first in a.official_transit:
            for last in b.official_transit:
                if first["station"].removesuffix("站") == last["station"].removesuffix("站"):
                    continue
                if line_key(first["line"]) != line_key(last["line"]):
                    continue
                source = await self.station(first["station"])
                target = await self.station(last["station"])
                if not source or not target or source.id == target.id:
                    continue
                walk_a = await routed(self.parent.geo, a, source, True)
                walk_b = await routed(self.parent.geo, target, b, True)
                if max(walk_a.duration_min, walk_b.duration_min) > 15:
                    continue
                rail = distance(source, target)
                ride = (
                    rail
                    * self.parent.geo.config.detour_factor
                    / (self.parent.geo.config.transit_rail_speed_kmh * 1000 / 60)
                )
                total = (
                    walk_a.duration_min
                    + walk_b.duration_min
                    + ride
                    + self.parent.geo.config.transit_wait_min
                )
                legs = [
                    {
                        "mode": "步行",
                        "from": a.name,
                        "to": source.name,
                        "minutes": math.ceil(walk_a.duration_min),
                    },
                    {
                        "mode": "轨道",
                        "line": first["line"],
                        "from": source.name,
                        "to": target.name,
                        "stops": None,
                    },
                    {
                        "mode": "步行",
                        "from": target.name,
                        "to": b.name,
                        "minutes": math.ceil(walk_b.duration_min),
                    },
                ]
                instructions = [
                    f"步行{legs[0]['minutes']}分钟至{source.name}",
                    f"按官方交通指南乘{first['line']}至{target.name}；方向及站数现场核实",
                    f"步行{legs[-1]['minutes']}分钟至{b.name}",
                ]
                coords = (
                    walk_a.polyline
                    + [[source.lng, source.lat], [target.lng, target.lat]]
                    + walk_b.polyline
                )
                return Segment(
                    from_poi=a.id,
                    to_poi=b.id,
                    from_name=a.name,
                    to_name=b.name,
                    mode="公共交通（估算）",
                    duration_min=math.ceil(total * 1.2),
                    distance_m=walk_a.distance_m + rail + walk_b.distance_m,
                    polyline=coords,
                    source="两端官方交通指引与OSM站点核对，轨道连线仅为示意",
                    route_verified=True,
                    geometry_mode="transit_schematic",
                    instructions=instructions,
                    transit={
                        "mode": "public_transport",
                        "legs": legs,
                        "total_min_range": [math.floor(total * 0.9), math.ceil(total * 1.2)],
                        "confidence": "official",
                        "source_ids": list(dict.fromkeys([first["source_id"], last["source_id"]])),
                        "note": "线路与车站取自官方指引；用时为步行路网和站间距离估算，非时刻表，轨道连线仅为示意",
                    },
                    parts=[
                        {"mode": "foot", "coordinates": walk_a.polyline},
                        {
                            "mode": "transit_schematic",
                            "coordinates": [[source.lng, source.lat], [target.lng, target.lat]],
                        },
                        {"mode": "foot", "coordinates": walk_b.polyline},
                    ],
                )
        return None


class OsmGraphProvider:
    tier = "osm_graph"
    miss_reason = "未取得连续线路或15分钟内步行可达站点"

    def __init__(self, parent):
        self.parent = parent

    async def get_route(self, a, b, depart_time):
        from backend.tools.planning import transit_segment

        async def from_data(data):
            actual = await transit_segment(self.parent.geo, a, b, data)
            if actual and actual.duration_min <= 40:
                return actual
            graph = await self.parent.osm_route(a, b, data)
            if not actual or (
                graph
                and graph.route_verified
                and graph.geometry_mode in {"subway", "bus"}
                and graph.duration_min < actual.duration_min
            ):
                return graph
            return actual

        if self.parent.transit_data:
            try:
                cached = await from_data({"elements": list(self.parent.transit_data.values())})
            except (httpx.HTTPError, ValueError) as error:
                cached = None
                self.parent.log(
                    self.tier,
                    status="cache_miss",
                    error_type=type(error).__name__,
                    reason=str(error)[:300],
                )
            if (
                cached
                and cached.route_verified
                and cached.geometry_mode in {"subway", "bus"}
                and cached.duration_min <= 40
            ):
                self.parent.log(
                    self.tier, status="cache_hit", fetched_at=self.parent.network_fetched_at
                )
                return cached
        try:
            data = await self.parent.osm.transit(a, b)
        except (httpx.HTTPError, ValueError) as error:
            if not self.parent.transit_data:
                raise
            self.parent.log(
                self.tier,
                status="cached_network",
                error_type=type(error).__name__,
                reason=str(error)[:300],
            )
            data = {"elements": list(self.parent.transit_data.values())}
        else:
            self.parent.remember_network(data)
        return await from_data(data)


class FallbackProvider:
    tier = "fallback"
    miss_reason = "未取得OSRM步行或驾车路网"

    def __init__(self, parent):
        self.parent = parent

    async def get_route(self, a, b, depart_time):
        return await self.parent.fallback_route(a, b)


class ThirdPartyProvider:
    """Reserved interface: no credentials or service is enabled by default."""

    enabled = False

    async def get_route(self, a, b, depart_time):
        return None
