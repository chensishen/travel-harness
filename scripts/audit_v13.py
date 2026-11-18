"""Inspect a saved real deliverable against the chapter 15 acceptance criteria."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.harness.quality import budget_issues, minutes, quality_issues, replacement_issues
from backend.harness.schemas import Deliverable
from backend.render.reader_content import basis


def audit(state):
    doc = state["document"]
    days = doc.get("days", [])
    funnel = state.get("candidate_funnel", {})
    candidates = funnel.get("pool", funnel.get("candidates", []))
    per_day = []
    for day in days:
        buffer = sum(
            minutes(e["end"]) - minutes(e["start"]) for e in day["timeline"] if e["kind"] == "wait"
        )
        per_day.append(
            {
                "day": day["day"],
                "stops": len(day["stops"]),
                "buffer_minutes": buffer,
                "has_first_leg": bool(day.get("arrival_segment")),
                "transport_confidence": [
                    s.get("transit", {}).get("confidence") for s in day.get("segments", [])
                ],
            }
        )
    typed = Deliverable.model_validate(doc)
    rated = [p for p in typed.pois if p.nationally_rated]

    def has_verified_photo(poi):
        return any(
            i.subject_verified
            and i.relevance_verified
            and not i.generated
            and i.author
            and i.license_url
            for i in poi.images
        )

    coverage = sum(has_verified_photo(p) for p in rated) / len(rated) if rated else None
    segments = [
        s
        for day in typed.days
        for s in ([day.arrival_segment] if day.arrival_segment else []) + day.segments
    ]
    sources = {s.id: s for s in typed.sources}

    def same_station(segment):
        context = segment.transit.get("same_station_catchment", {})
        refs = context.get("source_ids", [])
        name = context.get("station", "").removesuffix("站")
        return bool(
            name
            and refs
            and all(
                r in sources and sources[r].level == "L1" and name in sources[r].text for r in refs
            )
        )

    cross_station = [
        s
        for s in segments
        if s.geometry_mode != "foot" or (s.duration_min > 15 and not same_station(s))
    ]
    transit_share = (
        sum(s.transit.get("confidence") in {"official", "osm_graph"} for s in cross_station)
        / len(cross_station)
        if cross_station
        else None
    )
    checks = {
        "candidate_pool": len(candidates) >= max(20, len(days) * 6),
        "three_stops_each_day": bool(days) and all(x["stops"] >= 3 for x in per_day),
        "buffer_under_60": all(x["buffer_minutes"] <= 60 for x in per_day),
        "first_leg_each_day": bool(days) and all(x["has_first_leg"] for x in per_day),
        "dated_sources": all("published_at" in s for s in doc["sources"]),
        "transport_confidence": bool(segments)
        and all(
            s.transit.get("confidence") in {"official", "osm_graph", "fallback"} for s in segments
        ),
        "public_transport_share_70": "公共" not in typed.spec.transport_preference
        or transit_share is None
        or transit_share >= 0.7,
        "rail_geometry_actual": all(s.geometry_mode != "transit_schematic" for s in segments),
        "specific_origin": bool(days)
        and all(
            d.origin
            and d.origin.name not in {typed.spec.city, typed.spec.city + "市", "市区中心位置"}
            for d in typed.days
        ),
        "three_alternatives": not replacement_issues(typed),
        "rated_photo_coverage_80": coverage is None or coverage >= 0.8,
        "all_photo_coverage_60": bool(typed.pois)
        and sum(has_verified_photo(p) for p in typed.pois) / len(typed.pois) >= 0.6,
        "four_highlights_three_facts": all(
            len(p.blocks) >= 4
            and sum(b.kind == "fact" and bool(b.source_ids) for b in p.blocks) >= 3
            for p in typed.pois
        ),
        "assumptions_unique_max_six": len(typed.spec.assumptions) <= 6
        and len(typed.spec.assumptions) == len(set(typed.spec.assumptions)),
        "group_budget_total": not budget_issues(typed),
        "practical_basis": all(
            basis(typed, p.source_ids, estimated=p.estimated, note=p.basis) not in {"", "待补依据"}
            for p in typed.practical
        ),
        "monthly_climate": any(
            p.category == "notice" and ("气候" in p.label or "天气" in p.label) and p.source_ids
            for p in typed.practical
        ),
        "arrival_to_city": any(
            p.category == "transport"
            and "→" not in p.label
            and any(w in p.label for w in ["机场", "到达", "高铁", "火车站"])
            and p.source_ids
            for p in typed.practical
        ),
        "two_dining_points_or_areas": len(
            {p.label for p in typed.practical if p.category == "food" and p.source_ids}
        )
        >= 2,
        "two_checks_per_poi": 3 <= len(typed.reader_checks) <= 8
        and all(
            sum(
                any(t.get("poi") == p.name and t.get("url") for t in c.targets)
                for c in typed.reader_checks
            )
            >= 2
            for p in typed.pois
        ),
    }
    if typed.spec.type != "travel_guide":
        for key in (
            "candidate_pool",
            "three_stops_each_day",
            "buffer_under_60",
            "first_leg_each_day",
            "transport_confidence",
            "public_transport_share_70",
            "rail_geometry_actual",
            "specific_origin",
            "three_alternatives",
            "group_budget_total",
            "monthly_climate",
            "arrival_to_city",
            "two_dining_points_or_areas",
        ):
            checks.pop(key)
        checks["five_cultural_themes"] = len(typed.chapters) >= 5
    issues = quality_issues(typed, require_maps=True)
    checks["publication_quality"] = not issues
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "candidate_count": len(candidates),
        "days": per_day,
        "rated_photo_coverage": coverage,
        "public_transport_share": transit_share,
        "transit_share_denominator": "乘车段及大于15分钟的步行段；短途步行、两端L1官方到达指引共用同站的步行不计入跨站统计",
        "quality_issues": issues,
        "visual_qa": "需另行检查实际渲染页面；本脚本不证明无裁切或孤页",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = audit(json.loads((Path("data/jobs") / args.job_id / "state.json").read_text()))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["passed"] else 1)
