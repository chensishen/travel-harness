import asyncio
import itertools
import math
import re
import time

import httpx
from pydantic import BaseModel, Field

from backend.harness.reviewer import evidence_text
from backend.harness.schemas import DayPlan, Segment


class PlaceAlias(BaseModel):
    name: str
    source_id: str
    evidence: str


class PlaceAliases(BaseModel):
    aliases: list[PlaceAlias] = Field(max_length=3)
    addresses: list["PlaceAddress"] = Field(default_factory=list, max_length=2)


class PlaceAddress(BaseModel):
    street: str
    house_number: str = Field(pattern=r"^\d+$")
    source_id: str
    evidence: str


def distance(a, b):
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat, dlng = lat2 - lat1, math.radians(b.lng - a.lng)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


class Geo:
    def __init__(self, config, store, models=None):
        self.config, self.store = config, store
        self.models = models
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def within_city(self, city, item):
        key = self.store.key("geo-city-polygon", city)
        boundary = self.store.cached(key, 365)
        if boundary is None:
            async with httpx.AsyncClient(timeout=25) as client:
                response = await client.get(
                    self.config.nominatim_base_url + "/search",
                    params={
                        "q": city,
                        "format": "json",
                        "limit": 3,
                        "polygon_geojson": 1,
                        "polygon_threshold": 0.0001,
                        "addressdetails": 1,
                    },
                    headers={"User-Agent": self.config.http_user_agent},
                )
                response.raise_for_status()
                candidates = response.json()
            boundary = next(
                (
                    x
                    for x in candidates
                    if x.get("class") == "boundary"
                    and re.split(r"\s+", x.get("name", ""))[0].removesuffix("市")
                    == city.removesuffix("市")
                    and x.get("geojson", {}).get("type") in {"Polygon", "MultiPolygon"}
                ),
                None,
            )
            if boundary is None:
                return False
            self.store.put_cache(key, boundary)
        if item.get("address", {}).get("country_code") != boundary.get("address", {}).get(
            "country_code"
        ):
            return False
        point = (float(item["lon"]), float(item["lat"]))

        def ring_contains(ring):
            inside = False
            for a, b in zip(ring, ring[1:] + ring[:1]):
                if (a[1] > point[1]) != (b[1] > point[1]) and point[0] < (b[0] - a[0]) * (
                    point[1] - a[1]
                ) / (b[1] - a[1]) + a[0]:
                    inside = not inside
            return inside

        geometry = boundary["geojson"]
        polygons = (
            [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        )
        return any(
            ring_contains(p[0]) and not any(ring_contains(hole) for hole in p[1:]) for p in polygons
        )

    async def search(self, city, name, house_number=""):
        key = self.store.key("geo-v2", city + name + (":" + house_number if house_number else ""))
        result = self.store.cached(key, 365)
        if result is None:
            async with self.lock:
                await asyncio.sleep(max(0, 1.1 - (time.monotonic() - self.last)))
                try:
                    async with httpx.AsyncClient(timeout=25) as client:
                        response = await client.get(
                            self.config.nominatim_base_url + "/search",
                            params={
                                **(
                                    {"street": house_number + " " + name, "city": city}
                                    if house_number
                                    else {"q": city + " " + name}
                                ),
                                "format": "json",
                                "limit": 5,
                                "addressdetails": 1,
                                "namedetails": 1,
                            },
                            headers={"User-Agent": self.config.http_user_agent},
                        )
                        response.raise_for_status()
                        result = response.json()
                        if result:
                            self.store.put_cache(key, result)
                finally:
                    self.last = time.monotonic()
        matches = []
        for item in result:
            if item.get("class") in {"railway", "public_transport"} or item.get("type") in {
                "bus_stop",
                "platform",
                "station",
                "tram_stop",
            }:
                continue
            address = item.get("address", {})
            if not any(city in address.get(k, "") for k in ("city", "state", "town", "county")):
                names = [item.get("name", ""), *(item.get("namedetails") or {}).values()]
                if not (
                    house_number or any(evidence_text(name) == evidence_text(n) for n in names)
                ):
                    continue
                if not await self.within_city(city, item):
                    continue
            names = [item.get("name", ""), *(item.get("namedetails") or {}).values()]
            if house_number:
                if item.get("class") not in {"building", "tourism", "historic", "landuse", "place"}:
                    continue
                if address.get("house_number", "").removesuffix(
                    "号"
                ) == house_number and evidence_text(address.get("road", "")) == evidence_text(name):
                    matches.append(item)
            elif any(evidence_text(name) == evidence_text(n) for n in names):
                matches.append(item)
        primary = [m for m in matches if evidence_text(m.get("name", "")) == evidence_text(name)]
        if primary:
            matches = primary
        landmarks = [m for m in matches if m.get("class") in {"tourism", "historic", "leisure"}]
        if landmarks:
            matches = landmarks
        # Distinct OSM objects can identify the same building; never merge nearby landmarks.
        unique = {(m["lat"], m["lon"]): m for m in matches}
        if len(unique) > 1 and all(
            m.get("class") == "highway" and m.get("name") == name and m.get("boundingbox")
            for m in matches
        ):
            boxes = [list(map(float, m["boundingbox"])) for m in matches]
            connected = {0}
            while True:
                adjacent = {
                    j
                    for i in connected
                    for j, b in enumerate(boxes)
                    if max(boxes[i][0], b[0]) <= min(boxes[i][1], b[1]) + 0.00001
                    and max(boxes[i][2], b[2]) <= min(boxes[i][3], b[3]) + 0.00001
                }
                if adjacent <= connected:
                    break
                connected |= adjacent
            if len(connected) == len(matches):
                lat = sum(float(m["lat"]) for m in matches) / len(matches)
                lng = sum(float(m["lon"]) for m in matches) / len(matches)
                return min(
                    matches,
                    key=lambda m: (float(m["lat"]) - lat) ** 2 + (float(m["lon"]) - lng) ** 2,
                )
        return next(iter(unique.values())) if len(unique) == 1 else None

    async def locate(self, city, poi, sources=()):
        key = self.store.key("geo-resolved", city + poi.name)
        resolved = self.store.cached(key, 365)
        result = resolved["result"] if resolved else await self.search(city, poi.name)
        matched_name = resolved["matched_name"] if resolved else poi.name
        short = re.sub(r"(?:历史文化名街|文化名人街|历史文化街区)$", "", poi.name)
        if result is None and short and short != poi.name and any(short in s.text for s in sources):
            result = await self.search(city, short)
            if result:
                matched_name = short
        if result is None and poi.name.startswith(city) and len(poi.name) > len(city):
            short = poi.name[len(city) :].removeprefix("市")
            result = await self.search(city, short)
            if result:
                matched_name = short
        if result is None and self.models and sources:
            aliases = await self.models.json(
                "从提供的来源中提取目标场馆的正式简称或同一实体别名，按匹配把握排序。"
                "不得把旧址、新馆、附近景点或车站混为一谈。name 必须逐字出现在 evidence 中，"
                "evidence 必须为 source_id 对应正文的连续引文。不能确认则返回空 aliases。不要坐标。"
                "另从该景点的 L1/L2 来源提取 addresses，street 仅街道名称、house_number 仅数字，"
                "最多两个明确门牌地址，evidence 同样必须为包含街名门牌号的连续引文。",
                {
                    "city": city,
                    "target": poi.name,
                    "sources": [
                        {"id": s.id, "level": s.level, "text": s.text[:12000]} for s in sources
                    ],
                },
                PlaceAliases,
            )
            by_id = {s.id: s for s in sources}
            for alias in aliases.aliases:
                source = by_id.get(alias.source_id)
                quote = evidence_text(alias.evidence)
                if (
                    not source
                    or not quote
                    or quote not in evidence_text(source.text)
                    or not evidence_text(alias.name)
                    or evidence_text(alias.name) not in quote
                ):
                    continue
                result = await self.search(city, alias.name)
                if result:
                    matched_name = alias.name
                    break
            if result is None:
                for address in aliases.addresses:
                    source = by_id.get(address.source_id)
                    quote = evidence_text(address.evidence)
                    if (
                        not source
                        or source.level == "L3"
                        or not quote
                        or quote not in evidence_text(source.text)
                        or evidence_text(address.street) not in quote
                        or address.house_number not in re.findall(r"\d+", quote)
                    ):
                        continue
                    result = await self.search(city, address.street, address.house_number)
                    if result:
                        matched_name = f"来源地址：{address.street}{address.house_number}号"
                        break
        if result is None:
            raise ValueError(f"{poi.name} 无法唯一地理编码，不能生成可信路线")
        self.store.put_cache(key, {"result": result, "matched_name": matched_name})
        poi.lat, poi.lng = float(result["lat"]), float(result["lon"])
        poi.location_verified = bool(result.get("osm_type") and result.get("osm_id"))
        if result.get("osm_type") and result.get("osm_id"):
            poi.location_source = (
                f"https://www.openstreetmap.org/{result['osm_type']}/{result['osm_id']}"
            )
        poi.location_note = ""
        if matched_name != poi.name:
            poi.location_note = f"{poi.name} 按“{matched_name}”定位，出行前请复核场馆入口。"
        if result.get("class") == "highway":
            poi.location_note += " 街道坐标为地图路段代表点，非指定入口。"

    async def segment(self, a, b):
        key = self.store.key("route", f"{a.lng},{a.lat};{b.lng},{b.lat}")
        data = self.store.cached(key, 30)
        estimated_fallback = False
        if data is None:
            try:
                async with httpx.AsyncClient(timeout=25) as client:
                    response = await client.get(
                        self.config.osrm_base_url
                        + f"/route/v1/driving/{a.lng},{a.lat};{b.lng},{b.lat}",
                        params={"overview": "full", "geometries": "geojson"},
                    )
                    response.raise_for_status()
                    body = response.json()
                    if body.get("code") != "Ok" or not body.get("routes"):
                        raise ValueError("OSRM 无可用路线")
                    route = body["routes"][0]
                    data = {
                        "duration": route["duration"],
                        "distance": route["distance"],
                        "coordinates": route["geometry"]["coordinates"],
                    }
                    self.store.put_cache(key, data)
            except (httpx.HTTPError, ValueError):
                estimated_fallback = True
                meters = distance(a, b) * self.config.detour_factor
                data = {
                    "duration": meters / (25000 / 3600),
                    "distance": meters,
                    "coordinates": [[a.lng, a.lat], [b.lng, b.lat]],
                }
        minutes = (
            data["duration"] / 60 * self.config.transit_multiplier + self.config.transit_wait_min
        )
        return Segment(
            from_poi=a.id,
            to_poi=b.id,
            duration_min=round(minutes, 1),
            distance_m=round(data["distance"]),
            polyline=data["coordinates"],
            source="直线距离 × 绕行系数（估算）"
            if estimated_fallback
            else "OSRM 驾车时间折算（估算）",
        )

    async def plan(self, spec, pois, sources=()):
        for poi in pois:
            await self.locate(spec.city, poi, sources)
        remaining = list(pois)
        days = []
        for number in range(1, spec.days + 1):
            count = min(4, max(0, math.ceil(len(remaining) / (spec.days - number + 1))))
            if count == 0:
                days.append(
                    DayPlan(
                        day=number,
                        theme="自由活动与离城",
                        stops=[],
                        notes=["缺少已核实景点，留作自由安排"],
                    )
                )
                continue
            anchor = min(remaining, key=lambda p: (p.lng, p.lat))
            group = sorted(remaining, key=lambda p: distance(anchor, p))[:count]
            order = min(
                itertools.permutations(group),
                key=lambda ps: sum(distance(a, b) for a, b in zip(ps, ps[1:])),
            )
            segments = [await self.segment(a, b) for a, b in zip(order, order[1:])]
            notes = [
                "建议停留时间为规划估算；公共交通时间为估算，出发前核对实际班次。",
                "尚未交叉核验景点坐标与开放时段，路线须人工复核。",
            ]
            if number == 1:
                notes.append("首日预留抵达与入住时间，请根据实际到达时刻调整。")
            if number == spec.days:
                notes.append("末日预留离城交通时间，请根据返程班次调整。")
            days.append(
                DayPlan(
                    day=number,
                    theme=" · ".join(p.name for p in order),
                    stops=list(order),
                    segments=segments,
                    notes=notes,
                )
            )
            remaining = [p for p in remaining if p not in group]
        return days
