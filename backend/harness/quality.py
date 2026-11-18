"""Reader-facing publication rules; diagnostic warnings never become report copy."""

import re
from collections import Counter

from backend.harness.reviewer import validate_facts
from backend.harness.schemas import PHOTO_SKIPPED
from backend.tools.transit import prefers_walking

INTERNAL = re.compile(
    r"poi:|chapter:\d|confidence|ValueError|ConnectError|Facts|\bblock\b|\badvice\b|"
    r"本案例|样例|交付形式要求|MCP|自动审阅|来源原文逐字",
    re.I,
)
UNKNOWN = "暂未核实，请以官方渠道为准"


def verified_visit_fields(poi, sources):
    levels = {s.id: s.level for s in sources}
    return {
        f.field for f in poi.facts
        if f.field in {"ticket", "hours", "booking"}
        and f.value != UNKNOWN
        and any(levels.get(ref) in {"L1", "L2"} for ref in f.source_ids)
    }


def visit_verification_ratio(document):
    if not document.pois:
        return 0
    checked = sum(len(verified_visit_fields(poi, document.sources)) for poi in document.pois)
    return checked / (len(document.pois) * 3)


def minutes(clock):
    hour, minute = map(int, clock.split(":"))
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError("时间必须在 00:00–23:59 之间")
    return hour * 60 + minute


def referenced_sources(document):
    refs = {
        r
        for item in [*document.pois, *document.chapters]
        for b in item.blocks
        for r in b.source_ids
    }
    refs.update(r for p in document.pois for f in p.facts for r in f.source_ids)
    refs.update(r for p in document.pois if p.hours for r in p.hours.source_ids)
    refs.update(r for item in document.practical for r in item.source_ids)
    refs.update(r for item in document.alternatives for r in item.get("source_ids", []))
    return refs


def budget_issues(document):
    if document.spec.type != "travel_guide":
        return []
    from backend.harness.visit_context import adult_ticket_price

    rows = [p for p in document.practical if p.category == "budget"]
    totals = [p for p in rows if "合计" in p.label]
    issues = []
    for poi in document.pois:
        fact = next((f for f in poi.facts if f.field == "ticket" and f.source_ids), None)
        fare = adult_ticket_price(fact.value, document.spec.start_date) if fact else None
        if fare is not None and not any(
            p.label == poi.name + " · 门票"
            and p.amount_range == [fare * document.spec.travelers] * 2
            for p in rows
        ):
            issues.append(poi.name + "预算未计入已核实成人门票")
    expected = [round(sum(p.amount_range[i] for p in rows if p not in totals and p.amount_range), 2) for i in range(2)]
    if len(totals) != 1 or totals[0].amount_range != expected:
        issues.append("预算合计与分项金额不一致")
    return issues


def public_transport_sufficient(spec, days):
    if '公共' not in spec.transport_preference or prefers_walking(spec.transport_preference):
        return True
    segments = [s for d in days for s in [d.arrival_segment, *d.segments] if s]
    cross = [s for s in segments if s.geometry_mode != 'foot' or
             (s.duration_min > 15 and not s.transit.get('same_station_catchment'))]
    return not cross or sum(s.transit.get('confidence') in {'official', 'osm_graph'} for s in cross) / len(cross) >= .7


def replacement_issues(document):
    if document.spec.type != "travel_guide":
        return []
    from pydantic import ValidationError

    from backend.harness.schemas import DayPlan

    valid = set()
    for item in document.alternatives:
        if not item.get('verified') or not item.get('replacement_plan'):
            continue
        try:
            plan = DayPlan.model_validate(item['replacement_plan'])
        except ValidationError:
            continue
        original = next((d for d in document.days if d.day == plan.day and d.date == plan.date), None)
        if not original or len(plan.stops) != len(original.stops) or len(plan.stops) < 3:
            continue
        removed = {p.id for p in original.stops} - {p.id for p in plan.stops}
        added = [p for p in plan.stops if p.id not in {q.id for q in original.stops}]
        if removed != {item.get('replacement_for')} or len(added) != 1 or added[0].name != item['name']:
            continue
        days = [plan if d.day == plan.day else d for d in document.days]
        ids = [p.id for d in days for p in d.stops]
        if len(ids) != len(set(ids)) or route_failures(document.model_copy(update={'days': days})):
            continue
        if not re.search(r'\d{2}:\d{2}[–-]\d{2}:\d{2}', item.get('replace_day', '')):
            continue
        if not plan.arrival_segment or any(s.duration_min > 40 for s in plan.segments) or plan.buffer_minutes > 60:
            continue
        if not added[0].location_source:
            continue
        segments = [s for d in days for s in [d.arrival_segment, *d.segments] if s]
        if any(s.geometry_mode == 'transit_schematic' for s in segments):
            continue
        if not public_transport_sufficient(document.spec, days):
            continue
        valid.add(added[0].location_source)
    return [] if len(valid) >= 3 else ["至少需要3个有具体时段及可行排程的不同替换景点"]


def reader_text(document):
    return [
        *document.spec.assumptions,
        *(b.text for item in [*document.pois, *document.chapters] for b in item.blocks),
        *(f.value for p in document.pois for f in p.facts),
        *(p.hours.note for p in document.pois if p.hours),
        *(i.value for i in document.practical),
        *(i.text for i in document.reader_checks),
        *(e.note for day in document.days for e in day.timeline),
        *(n for day in document.days for n in day.notes),
    ]


def route_failures(document):
    issues = []
    if document.spec.type != "travel_guide":
        return issues
    if document.prd_version == "1.3" and any(
        f.field == "closure" and f.source_ids for p in document.pois for f in p.facts
    ):
        issues.append("主线包含有官方闭馆公告的景点，须替换后排程")
    if len(document.days) != document.spec.days:
        issues.append("行程天数与需求不一致")
    for day in document.days:
        prefix = f"第 {day.day} 天"
        if not day.timeline or not day.date:
            issues.append(prefix + "缺少日期或完整时间轴")
            continue
        duration = sum(p.duration_min for p in day.stops) + sum(
            s.duration_min for s in day.segments
        )
        duration += sum(
            minutes(e.end) - minutes(e.start) for e in day.timeline if e.kind in {"meal", "wait"}
        )
        elapsed = minutes(day.timeline[-1].end) - minutes(day.timeline[0].start)
        if document.prd_version == "1.3":
            duration = sum(
                minutes(e.end) - minutes(e.start) for e in day.timeline if e.kind != "wait"
            )
            elapsed = duration
        if max(duration, elapsed, day.total_minutes) > 600:
            issues.append(prefix + "总时长超过 10 小时")
        if len(day.segments) != max(0, len(day.stops) - 1):
            issues.append(prefix + "景点与交通路段不一致")
        if sum(s.distance_m > 5000 for s in day.segments) > 1 and not any(
            "长距离" in n for n in day.notes
        ):
            issues.append(prefix + "存在多次长距离转移且未说明理由")
        previous = minutes(day.timeline[0].start)
        visits = []
        for entry in day.timeline:
            begin, end = minutes(entry.start), minutes(entry.end)
            if begin < previous or end <= begin:
                issues.append(prefix + "时间轴重叠或时长无效")
            previous = end
            if entry.kind == "visit":
                visits.append(entry.poi_id)
                poi = next((p for p in day.stops if p.id == entry.poi_id), None)
                if not poi or not poi.hours:
                    issues.append(prefix + "景点缺少有来源的开放时间窗")
                    continue
                hours = poi.hours
                if day.date.weekday() in hours.closed_days:
                    issues.append(poi.name + "在计划日期闭馆")
                if begin < minutes(hours.open) or end > minutes(hours.close):
                    issues.append(poi.name + "超出开放时间")
                if hours.last_entry and begin > minutes(hours.last_entry):
                    issues.append(poi.name + "超过最后入场时间")
        if visits != [p.id for p in day.stops]:
            issues.append(prefix + "时间轴遗漏或重复景点")
        for segment in ([day.arrival_segment] if day.arrival_segment else []) + day.segments:
            if not segment.route_verified or len(segment.polyline) < 3:
                issues.append(prefix + "缺少真实路由 geometry")
            if re.match(r"^(?:公交|地铁|轨道)", segment.mode):
                if not segment.instructions or segment.geometry_mode not in {"subway", "bus"}:
                    issues.append(prefix + "公交方案缺少站点与线路")
            if "驾车时间折算" in segment.source:
                issues.append(prefix + "禁止以驾车时间代替公交")
        if any(not p.location_verified for p in day.stops):
            issues.append(prefix + "景点定位未核对")
    return list(dict.fromkeys(issues))


def quality_issues(document, *, require_maps=True):
    issues = route_failures(document)
    if document.prd_version == "1.3":
        from backend.harness.visit_context import active_exhibitions

        for notice in active_exhibitions(document):
            poi = next(p for p in document.pois if p.id == notice["poi_id"])
            if not any(notice["title"] in b.text and notice["source_id"] in b.source_ids for b in poi.blocks):
                issues.append(poi.name + "当期独占展览限制未进入景点说明")
        if document.spec.type == "travel_guide":
            issues.extend(budget_issues(document))
            issues.extend(replacement_issues(document))
            if len(document.spec.assumptions) > 6 or len(set(document.spec.assumptions)) != len(
                document.spec.assumptions
            ):
                issues.append("编制假设重复或超过6条")
            if document.candidate_count < max(20, document.spec.days * 6):
                issues.append("候选池不足，须回退研究补充候选")
            for day in document.days:
                if len(day.stops) < 3:
                    issues.append(f"第{day.day}天至少3个景点，须回退研究")
                if day.timeline and minutes(day.timeline[0].start) <= 13 * 60 + 30 < minutes(day.timeline[-1].end):
                    meals = [i for i, e in enumerate(day.timeline) if e.kind == "meal"]
                    named = {
                        item["name"]
                        for p in day.stops for item in p.dining
                        if item.get("source") and item.get("distance_m", 501) <= 500
                    }
                    if not meals or any(
                        not any(name in day.timeline[i].place for name in named)
                        or not any(e.kind == "visit" for e in day.timeline[:i])
                        or not any(e.kind == "visit" for e in day.timeline[i + 1:])
                        for i in meals
                    ):
                        issues.append(f"第{day.day}天缺少两景点之间有来源的具名午餐点")
                if (
                    sum(minutes(e.end) - minutes(e.start) for e in day.timeline if e.kind == "wait")
                    > 60
                ):
                    issues.append("机动缓冲超过60分钟")
                if not day.arrival_segment:
                    issues.append("缺少起点至首个景点的交通")
                if not day.origin or day.origin.name in {
                    document.spec.city,
                    document.spec.city + "市",
                    "市区中心位置",
                }:
                    issues.append("缺少具体可定位的出发点")
                for segment in [day.arrival_segment, *day.segments]:
                    if segment and not segment.transit.get("confidence"):
                        issues.append("交通段缺少confidence")
            if len(document.alternatives) < 3:
                issues.append("至少需要3个替换候选")
            if not document.spec.date_assumed and not any(
                p.category == "notice" and ("气候" in p.label or "天气" in p.label) and p.source_ids
                for p in document.practical
            ):
                issues.append("缺少有来源的月份气候与穿衣建议")
            sources = {s.id: s for s in document.sources}
            if not any(
                p.category == "food"
                and any(
                    r in sources
                    and sources[r].level in {"L1", "L2"}
                    and "openstreetmap.org" not in str(sources[r].url)
                    for r in p.source_ids
                )
                for p in document.practical
            ):
                issues.append("缺少L1或L2来源的本地特色饮食")
            if not any(
                p.category == "transport"
                and ("→" not in p.label or p.label.split("→", 1)[1].strip().startswith("市区"))
                and any(w in p.label for w in ["机场", "到达", "高铁", "火车站"])
                and p.source_ids
                for p in document.practical
            ):
                issues.append("缺少有来源的到达城市接驳指引")
            practical = [p for p in document.practical if p.category != "budget"]
            if practical and sum(not p.source_ids for p in practical) / len(practical) > 0.5:
                issues.append("实用信息超过一半缺少来源，需要补查")
            if not any(
                p.category == "budget" and "合计" in p.label and p.amount_range
                for p in document.practical
            ):
                issues.append("缺少按人数计算的预算合计")
            if not 3 <= len(document.reader_checks) <= 8 or any(
                sum(
                    any(t.get("poi") == p.name and t.get("url") for t in c.targets)
                    for c in document.reader_checks
                )
                < 2
                for p in document.pois
            ):
                issues.append("每个景点需有2项带链接的出行前核对，全文3至8项")
        for poi in document.pois:
            if len(poi.blocks) < 4 or sum(b.kind == "fact" for b in poi.blocks) < 3:
                issues.append(poi.name + "缺少4条亮点及3条有来源事实")
    try:
        validate_facts(document)
    except ValueError as error:
        issues.append(str(error))
    by_id = {s.id: s for s in document.sources}
    for poi in document.pois:
        verified = verified_visit_fields(poi, document.sources)
        for field in ("ticket", "hours", "booking"):
            if field not in verified and (
                not (poi.official_url or (document.prd_version == "1.3" and poi.verification_url))
                or not poi.verification_attempts
            ):
                issues.append(poi.name + "未核实字段缺少尝试记录或官方联系渠道")
        missing = sum(
            [
                not any(b.kind == "fact" for b in poi.blocks),
                poi.hours is None,
                not any(
                    f.field == "address" and f.source_ids and f.value != UNKNOWN for f in poi.facts
                ),
            ]
        )
        if missing >= 2:
            issues.append(poi.name + "简介、开放时间和地址缺少至少两项")
        for photo in poi.images:
            if document.prd_version == "1.3" and not photo.subject_verified:
                issues.append(poi.name + "实拍缺少源页面的景点主体关联证明")
            if (
                photo.generated
                or not photo.relevance_verified
                or not photo.author
                or not photo.license
            ):
                issues.append(poi.name + "景点图不是已核验授权实拍")
        if not poi.images and poi.photo_status not in {"暂无合规实拍图", PHOTO_SKIPPED}:
            issues.append(poi.name + "缺少实拍或明确占位说明")
        for fact in poi.facts:
            if (
                fact.field == "ticket"
                and "免费" in fact.value
                and not re.search(r"收费|另购|另计", fact.value)
            ):
                if any(
                    ref in by_id
                    and by_id[ref].level in {"L1", "L2"}
                    and poi.name in by_id[ref].title
                    and re.search(
                        r"(?:此|该)展览为收费展览|(?:临展|特展)(?:需要|需)?(?:单独)?收费",
                        by_id[ref].text,
                    )
                    for ref in fact.source_ids
                ):
                    issues.append(poi.name + "免费票价说明遗漏收费展览例外")
            if re.match(r"^(开始时间|开放时间|地址|票价|门票价格)\s*[:：]", fact.value):
                issues.append(poi.name + "字段含抓取前缀")
            if (
                fact.field == "address"
                and len(re.findall(r"\d+号(?!口)|\bNo\.?\s*\d+", fact.value, re.I)) > 1
            ):
                issues.append(poi.name + "地址混合多个馆区或门牌")
    if visit_verification_ratio(document) < 0.75:
        issues.append("票价、开放时间与预约核实比例低于 75%")
    illustrations = {i.local_path for c in document.chapters for i in c.images if i.generated}
    illustrations.update(i.local_path for i in document.decorations if i.generated)
    if len(illustrations) > 3:
        issues.append("装饰插画超过 3 张")
    if any(not c.blocks for c in document.chapters):
        issues.append("存在空主题页")
    sentences = [s.strip() for text in reader_text(document) for s in re.split(r"[。！？\n]", text)]
    counts = Counter(s for s in sentences if len(s) >= 24)
    if any(n > 2 for n in counts.values()):
        issues.append("同一事实或文案出现超过两次")
    body_sentences = Counter(
        s.strip()
        for item in [*document.pois, *document.chapters]
        for b in item.blocks
        for s in re.split(r"[。！？\n]", b.text)
        if len(s.strip()) >= 24
    )
    if any(n >= 2 for s, n in body_sentences.items() if s.startswith(("建议", "请在", "出行前"))):
        issues.append("重复建议句式")
    if any(INTERNAL.search(text) for text in reader_text(document)):
        issues.append("读者正文含内部日志或指令词")
    if referenced_sources(document) != {s.id for s in document.sources}:
        issues.append("来源附录与实际引用不一致")
    if len(document.reader_checks) > 8 or any(
        not c.official_url.startswith(("https://", "http://")) for c in document.reader_checks
    ):
        issues.append("出行前核实清单超限或缺少官方链接")
    if document.spec.type == "travel_guide":
        if not {"budget", "food", "transport"} <= {p.category for p in document.practical}:
            issues.append("缺少预算、餐饮或交通说明")
        if require_maps and any(d.map_coverage < 0.30 or not d.map_png for d in document.days):
            issues.append("路线图缺失或底图有效像素不足 30%")
    return list(dict.fromkeys(issues))


def require_quality(document, *, require_maps=True):
    issues = quality_issues(document, require_maps=require_maps)
    if issues:
        raise ValueError("交付检查未通过：" + "；".join(issues[:12]))
