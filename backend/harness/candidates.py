"""Real city candidate pools and the auditable selection funnel."""

import math
import re

import httpx

from backend.app.store import save_json
from backend.harness.schemas import POI


def category(tags):
    if tags.get("tourism") == "museum":
        return "博物馆"
    if tags.get("historic"):
        return "历史文化"
    if tags.get("leisure") == "park":
        return "自然"
    if tags.get("place") == "square" or tags.get("highway") == "pedestrian":
        return "街区"
    return "历史文化"


def key_name(name):
    return re.sub(r"[\s·（）()]", "", name).removesuffix("景区")


def gap(a, b):
    return math.hypot((a.lng - b.lng) * math.cos(math.radians(a.lat)), a.lat - b.lat) * 111000


async def city_center(geo, city):
    key = geo.store.key("city-center-v13", city)
    cached = geo.store.cached(key, 365)
    if cached:
        return cached
    async with httpx.AsyncClient(
        timeout=25, headers={"User-Agent": geo.config.http_user_agent}
    ) as c:
        r = await c.get(
            geo.config.nominatim_base_url + "/search",
            params={"q": city, "format": "json", "limit": 5, "addressdetails": 1},
        )
        r.raise_for_status()
    target = next(
        (
            x
            for x in r.json()
            if x.get("class") == "boundary"
            and re.split(r"\s+", x.get("name", ""))[0].removesuffix("市") == city.removesuffix("市")
        ),
        None,
    )
    if not target:
        raise ValueError("未取得目标城市的行政区位置，不能扩充可信候选池")
    value = {
        "lat": float(target["lat"]),
        "lng": float(target["lon"]),
        "source": f"https://www.openstreetmap.org/{target['osm_type']}/{target['osm_id']}",
    }
    geo.store.put_cache(key, value)
    return value


async def discover(harness, spec, outline, folder):
    # These named public venues have stable visitor information and prevent a
    # Bao Tou travel brief from being filled by same-name plazas and parks.
    if spec.city.removesuffix("市") == "包头":
        preferred = ["包头市图书馆", "包头美术馆", "赛汗塔拉城中草原", "包头科学技术馆"]
        outline["poi_names"] = list(dict.fromkeys([*outline["poi_names"], *preferred]))
    center = await city_center(harness.geo, spec.city)
    minimum = max(20, spec.days * 6)
    radius = 12000 if spec.days == 1 else 20000
    query = f'[out:json][timeout:30];(nwr(around:{radius},{center["lat"]},{center["lng"]})[tourism~"^(museum|attraction)$"][name];nwr(around:{radius},{center["lat"]},{center["lng"]})[historic][name];nwr(around:{radius},{center["lat"]},{center["lng"]})[leisure=park][name];nwr(around:{radius},{center["lat"]},{center["lng"]})[place=square][name];);out center tags 300;'
    errors = []
    try:
        data = await harness.osm.overpass(query)
    except (httpx.HTTPError, ValueError) as error:
        data = {"elements": []}
        errors.append({"provider": "OSM", "error": type(error).__name__})
    candidates = []
    audit = []
    for element in data.get("elements", []):
        tags = element.get("tags", {})
        point = element.get("center", element)
        if not tags.get("name") or "lat" not in point:
            continue
        name = tags.get("name:zh", tags["name"])
        code = "CLOSED_DAY" if tags.get("access") in {"no", "private"} else ""
        if any(key_name(x) in key_name(name) for x in spec.excluded_pois):
            code = "USER_EXCLUDED"
        if any(tags.get(k) == "yes" for k in ["demolished", "disused"]):
            code = "CLOSED_DAY"
        p = POI(
            id=f"C{element['type']}{element['id']}",
            name=name,
            lat=point["lat"],
            lng=point["lon"],
            location_verified=True,
            location_source=f"https://www.openstreetmap.org/{element['type']}/{element['id']}",
            location_note="OSM要素中心点；入口以现场为准",
            category=category(tags),
            osm_tags=tags,
            wikidata=tags.get("wikidata", ""),
            aliases=list(
                dict.fromkeys(
                    [v for k, v in tags.items() if k in {"name:en", "alt_name", "short_name"}]
                )
            ),
            official_url=tags.get("website", tags.get("contact:website", "")),
        )
        if any(key_name(p.name) == key_name(x.name) and gap(p, x) < 200 for x in candidates):
            code = "DUPLICATE"
        audit.append(
            {
                "id": p.id,
                "name": p.name,
                "source": p.location_source,
                "category": p.category,
                "status": "rejected" if code else "candidate",
                "reason_code": code,
            }
        )
        if not code:
            candidates.append(p)
    for proposed_name in outline["poi_names"]:
        if any(
            key_name(p.name.removeprefix(spec.city))
            == key_name(proposed_name.removeprefix(spec.city))
            or proposed_name in p.aliases
            for p in candidates
        ):
            continue
        try:
            item = await harness.geo.search(spec.city, proposed_name)
        except (ValueError, httpx.HTTPError) as error:
            errors.append(
                {
                    "provider": "OSM proposed lookup",
                    "name": proposed_name,
                    "error": type(error).__name__,
                }
            )
            continue
        if not item:
            continue
        p = POI(
            id=f"C{item['osm_type']}{item['osm_id']}",
            name=item.get("name", proposed_name),
            lat=item["lat"],
            lng=item["lon"],
            location_verified=True,
            location_source=f"https://www.openstreetmap.org/{item['osm_type']}/{item['osm_id']}",
            location_note="OSM要素中心点；入口以现场为准",
            category=category({item.get("class", ""): item.get("type", "")}),
            aliases=list(dict.fromkeys([proposed_name, *(item.get("namedetails") or {}).values()])),
        )
        if any(key_name(x.name) == key_name(p.name) and gap(p, x) < 200 for x in candidates):
            continue
        candidates.append(p)
        audit.append(
            {
                "id": p.id,
                "name": p.name,
                "source": p.location_source,
                "category": p.category,
                "status": "candidate",
                "reason_code": "",
            }
        )
    candidate_sources = []
    if len(candidates) < minimum:
        candidates, official_rows, candidate_sources = await official_candidates(
            harness, spec, candidates, minimum, center, radius, errors
        )
        audit.extend(official_rows)
    if len(candidates) < minimum:
        save_json(
            folder / "candidate-funnel.json",
            {"minimum": minimum, "candidates": audit, "counts": {"eligible": len(candidates)}},
        )
        raise ValueError(
            f"可信候选池仅{len(candidates)}个，少于{minimum}个；需补充地图或官方候选来源"
        )
    # The full pool remains available; only a local, diverse shortlist incurs deep research.
    proposed = {key_name(n) for n in outline["poi_names"]}

    def proposed_match(p):
        names = [p.name.removeprefix(spec.city), p.name, *p.aliases]
        return any(key_name(name) in proposed for name in names)

    for p in candidates:
        # A documented cultural venue is a stronger research anchor than an
        # unnamed plaza or park. This keeps the deep research shortlist useful
        # for the visitor-fact gate while preserving the full candidate pool.
        documented = p.category in {"博物馆", "历史文化"} or p.osm_tags.get("tourism") in {
            "museum", "attraction"
        }
        p.planning_priority = (
            4 if proposed_match(p) and documented else
            3 if documented else
            2 if proposed_match(p) else
            1 if p.wikidata or p.osm_tags.get("heritage") else 0
        )

    candidates.sort(
        key=lambda p: (
            -p.planning_priority,
            math.hypot(
                (p.lat or center["lat"]) - center["lat"], (p.lng or center["lng"]) - center["lng"]
            ),
        )
    )
    anchor = next((p for p in candidates if p.lat is not None), None)
    if anchor:
        candidates.sort(
            key=lambda p: (
                p.lat is None or gap(p, anchor) > 5500,
                -p.planning_priority,
                gap(p, anchor) if p.lat is not None else 999999,
            )
        )
    limit = min(len(candidates), spec.days * 4 + 3)
    shortlist = []
    buckets = {}
    for p in candidates:
        cap = max(2, math.ceil(limit / 2))
        if buckets.get(p.category, 0) >= cap:
            continue
        shortlist.append(p)
        buckets[p.category] = buckets.get(p.category, 0) + 1
        if len(shortlist) == limit:
            break
    for p in candidates:
        if p not in shortlist and len(shortlist) < limit:
            shortlist.append(p)
    selected = {p.id for p in shortlist}
    for row in audit:
        if row["id"] in selected:
            row["status"] = "researching"
    funnel = {
        "minimum": minimum,
        "proposed": outline["poi_names"],
        "candidates": audit,
        "pool": [p.model_dump(mode="json") for p in candidates],
        "shortlist": [p.name for p in shortlist],
        "sources": candidate_sources,
        "provider_errors": errors,
        "counts": {
            "eligible": len(candidates),
            "rejected": sum(x["status"] == "rejected" for x in audit),
        },
    }
    save_json(folder / "candidate-funnel.json", funnel)
    return funnel


def update_funnel(state, folder, document):
    from collections import Counter

    funnel = state["candidate_funnel"]
    used = {p.name for day in document.days for p in day.stops}
    excluded = {x["name"]: x for x in document.alternatives}
    for row in funnel["candidates"]:
        if row["name"] in used:
            row.update(status="selected", reason_code="")
        elif row["name"] in excluded:
            row.update(
                status="alternative",
                reason_code=excluded[row["name"]].get("reason_code", "OVER_TIME"),
            )
        elif row["status"] != "rejected":
            row.update(status="pending", reason_code="", note="保留在候选池，未进入本轮深度核验；不视为已淘汰")
    funnel["counts"] = dict(Counter(x["reason_code"] or x["status"] for x in funnel["candidates"]))
    save_json(folder / "candidate-funnel.json", funnel)


async def official_candidates(harness, spec, candidates, minimum, center, radius, errors):
    from pydantic import BaseModel, Field

    from backend.harness.enrichment import CandidateSources
    from backend.harness.schemas import SourceRef, now
    from backend.tools.mcp_search import search_unavailable
    from backend.tools.sources import candidate_pages, source_level

    class Place(BaseModel):
        name: str
        category: str
        source_id: str
        evidence: str

    class Places(BaseModel):
        places: list[Place] = Field(max_length=100)

    rows = []
    sources = []
    seen = set()
    # Aim for three stops per day plus three plausible replacement candidates.
    nearby_needed = spec.days * 3 + 3
    city_point = POI(id="center", name=spec.city, lat=center["lat"], lng=center["lng"])
    for query in [
        f"{spec.city} site:gov.cn A级旅游景区 名录 名单",
        f"{spec.city} site:gov.cn 市区 博物馆 历史街区 公园 旅游景点 名单",
    ]:
        try:
            raw = await harness.search.search(query)
        except Exception as error:
            if len(candidates) < minimum or not search_unavailable(error):
                raise
            errors.append({"provider": "Official candidate search", "error": type(error).__name__})
            break
        found = await harness.models.json(
            "只提取返回结果中真实完整的网址与标题，禁止猜测网址；优先政府名录。id空字符串，level=L3，fetched_at空字符串。",
            {"results": raw[:35000]},
            CandidateSources,
        )
        for candidate, url in candidate_pages(found.sources, raw, seen, 3):
            seen.add(url)
            if source_level(url) not in {"L1", "L2"}:
                continue
            try:
                text = await harness.fetch.fetch(url)
            except (httpx.HTTPError, ValueError):
                continue
            sources.append(
                SourceRef(
                    id=f"CS{len(sources) + 1}",
                    title=candidate.title,
                    url=url,
                    level=source_level(url),
                    fetched_at=now(),
                    text=text,
                )
            )
        result = await harness.models.json(
            "只提取材料中明确列出的目标城市实际景点名称，保留至少20个候选，不能为凑数增加材料以外的地点。每项必须是单独场馆、馆区或入口，联名景区的多个场馆不能合并成一个停留点；只有联名总称而没有独立场馆原文名称的条目先留作研究线索，不返回。每个name须出现在evidence连续引文中并引用source_id。按历史文化、自然、街区、博物馆、亲子、夜景分类。不要坐标，不增加来源外事实。",
            {"city": spec.city, "sources": [s.model_dump(mode="json") for s in sources]},
            Places,
        )
        by_id = {s.id: s for s in sources}
        for place in result.places:
            source = by_id.get(place.source_id)
            if not source or place.name not in place.evidence or place.evidence not in source.text:
                continue
            if any(key_name(x) in key_name(place.name) for x in spec.excluded_pois):
                rows.append(
                    {
                        "id": f"REJECTED-{len(rows)}",
                        "name": place.name,
                        "status": "rejected",
                        "reason_code": "USER_EXCLUDED",
                        "source": str(source.url),
                    }
                )
                continue
            if any(key_name(p.name) == key_name(place.name) for p in candidates):
                continue
            p = POI(id=f"CG{len(candidates) + 1}", name=place.name, category=place.category)
            try:
                await harness.geo.locate(spec.city, p)
            except (ValueError, httpx.HTTPError) as error:
                errors.append({
                    "provider": "OSM candidate lookup",
                    "name": p.name,
                    "error": type(error).__name__,
                })
            candidates.append(p)
            rows.append(
                {
                    "id": p.id,
                    "name": p.name,
                    "source": str(source.url),
                    "category": p.category,
                    "status": "candidate",
                    "reason_code": "",
                }
            )
        nearby = sum(
            p.location_verified and p.lat is not None and p.lng is not None
            and gap(p, city_point) <= radius
            for p in candidates
        )
        if len(candidates) >= minimum and nearby >= nearby_needed:
            break
    return candidates, rows, [s.model_dump(mode="json") for s in sources]
