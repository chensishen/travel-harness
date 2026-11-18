"""Normalize reader material after evidence extraction, keeping diagnostics in job state."""

import re

from backend.harness.quality import INTERNAL, UNKNOWN, referenced_sources
from backend.harness.schemas import PracticalItem, ReaderCheck
from backend.tools.sources import normalize_facts, page_title, source_level


def prepare(document):
    from backend.harness.visit_context import apply_exhibition_notices

    apply_exhibition_notices(document)
    document.prd_version = "1.3" if document.prd_version == "1.3" else "1.1"
    for source in document.sources:
        source.level = source_level(str(source.url))
        source.title = page_title(source.text, str(source.url), source.title)
    document.spec.assumptions = [x for x in document.spec.assumptions if not INTERNAL.search(x)]
    document.chapters = [c for c in document.chapters if c.blocks]
    document.reader_checks = []
    for poi in document.pois:
        normalize_facts(poi, document.sources)
        if document.prd_version == "1.3":
            if document.spec.type == "travel_guide":
                poi.blocks = poi.blocks[:4]
            primary = {s.id for s in document.sources if s.level in {"L1", "L2"}}
            poi.nationally_rated = poi.nationally_rated or any(
                b.kind == "fact"
                and set(b.source_ids) & primary
                and re.search(r"全国重点文物保护|世界文化遗产|国家[一二三]级|国家[级]?(?:[45]A|A{4,5})", b.text)
                for b in poi.blocks
            )
        for fact in poi.facts:
            if fact.field == "address":
                arrival = re.search(r"[（(]((?:地铁|公交|交通)[^）)]+)[）)]", fact.value)
                if arrival:
                    item = PracticalItem(
                        category="transport",
                        label=poi.name + " · 到达",
                        value=arrival[1],
                        source_ids=fact.source_ids,
                    )
                    if item not in document.practical:
                        document.practical.append(item)
                    fact.value = fact.value.replace(arrival[0], "").strip()
        poi.facts = [f for f in poi.facts if f.field in {"ticket", "hours", "booking", "address"}]
        # Practical facts already have a dedicated table and budget row. Remove
        # exact, equally cited copies from highlights, keeping distinct facts.
        practical_facts = {}
        for fact in poi.facts:
            if fact.source_ids and fact.value != UNKNOWN:
                practical_facts.setdefault(fact.value.strip("。；; \n"), set()).update(
                    fact.source_ids
                )

        def repeats_practical(block):
            refs = practical_facts.get(block.text.strip("。；; \n"))
            return (
                block.kind == "fact" and refs and block.source_ids and set(block.source_ids) <= refs
            )

        if any(b.kind == "fact" and not repeats_practical(b) for b in poi.blocks):
            poi.blocks = [b for b in poi.blocks if not repeats_practical(b)]
        missing = [
            name
            for field, name in [("ticket", "门票"), ("hours", "开放时间"), ("booking", "预约")]
            if not any(f.field == field and f.source_ids and f.value != UNKNOWN for f in poi.facts)
        ]
        if missing and poi.official_url:
            document.reader_checks.append(
                ReaderCheck(
                    poi=poi.name,
                    text=f"出发前核实{'、'.join(missing)}。"
                    + (f"咨询：{poi.contact}" if poi.contact else ""),
                    official_url=poi.official_url,
                )
            )
    document.reader_checks = document.reader_checks[:8]
    seen_notes = set()
    for day in document.days:
        notes = []
        for note in day.notes:
            if note not in seen_notes:
                notes.append(note)
                seen_notes.add(note)
        day.notes = notes
    if document.spec.type == "travel_guide":
        # Culture material belongs on each destination page; guide chapters are practical.
        document.chapters = [
            c
            for c in document.chapters
            if any(
                k in c.title
                for k in ["交通", "餐饮", "美食", "住宿", "注意", "预算", "天气", "预约"]
            )
        ]
        document.practical = [
            p for p in document.practical if p.category in {"budget", "food", "transport", "notice"}
        ]
        for chapter in document.chapters:
            category = (
                "food"
                if any(x in chapter.title for x in ["餐饮", "美食"])
                else "transport"
                if "交通" in chapter.title
                else "notice"
            )
            document.practical.extend(
                PracticalItem(
                    category=category,
                    label=chapter.title,
                    value=b.text,
                    source_ids=b.source_ids,
                    estimated=b.kind == "advice",
                )
                for b in chapter.blocks
            )
        document.chapters = []
        excluded = {item["name"] for item in document.alternatives if item.get("name")} - {
            p.name for p in document.pois
        }
        document.practical = [
            item
            for item in document.practical
            if not any(name in item.label or name in item.value for name in excluded)
        ]
        document.spec.assumptions = [
            text for text in document.spec.assumptions if not any(name in text for name in excluded)
        ]
        categories = {p.category for p in document.practical}
        if document.prd_version != "1.3" and "food" not in categories:
            areas = "、".join(p.name for p in document.pois[:3])
            document.practical.append(
                PracticalItem(
                    category="food",
                    label="用餐安排",
                    value=f"优先在{areas}当日路线沿途选择餐饮区；按时间轴就近用餐，出发前确认店铺营业与个人饮食需求。",
                    estimated=True,
                )
            )
        if document.prd_version != "1.3" and "transport" not in categories:
            document.practical.append(
                PracticalItem(
                    category="transport",
                    label="到达与市内交通",
                    value="到达站尚未指定；市内按路线页列出的步行或乘车方式执行。未取得公交线路时，不提供未经核对的线路或站名。",
                    estimated=True,
                )
            )
        document.practical = [
            i
            for i in document.practical
            if not (i.category == "budget" and i.label.endswith(" · 门票"))
        ]
        for poi in document.pois:
            fact = next(
                (
                    f
                    for f in poi.facts
                    if f.field == "ticket" and f.source_ids and f.value != UNKNOWN
                ),
                None,
            )
            if fact:
                document.practical.append(
                    PracticalItem(
                        category="budget",
                        label=poi.name + " · 门票",
                        value=fact.value,
                        source_ids=fact.source_ids,
                    )
                )
        if document.prd_version != "1.3" and "budget" not in categories:
            document.practical.extend(
                [
                    PracticalItem(
                        category="budget",
                        label="餐饮预算预留",
                        value="每人每天预留 60–120 元（个人预算参考，待核实；并非餐厅报价）。",
                        estimated=True,
                    ),
                    PracticalItem(
                        category="budget",
                        label="市内交通预算预留",
                        value="每人每天预留 20–60 元（个人预算参考，待核实；打车、到达及离城费用另计）。",
                        estimated=True,
                    ),
                ]
            )
        if document.prd_version != "1.3" and "notice" not in categories:
            document.practical.append(
                PracticalItem(
                    category="notice",
                    label="天气与排队",
                    value="出发当天查看官方天气预报与景区临时公告；雨天缩短室外步行，按预约时段到达，给安检排队留出弹性时间。",
                    estimated=True,
                )
            )
    if document.prd_version == "1.3":
        from backend.harness.visit_context import group_budget, normalize_assumptions

        if document.spec.type == "travel_guide":
            normalize_assumptions(document)
        from datetime import date

        by_id = {s.id: s for s in document.sources}
        for poi in document.pois:
            for fact in poi.facts:
                visits = [
                    date.fromisoformat(by_id[r].fetched_at[:10])
                    for r in fact.source_ids
                    if r in by_id
                ]
                if visits:
                    fact.verified_at = max(visits)
        if document.spec.type == "travel_guide":
            group_budget(document)
            if document.days:
                names = list(
                    dict.fromkeys(
                        p.dining[i]["name"]
                        for i in range(3)
                        for p in document.pois
                        if len(p.dining) > i
                    )
                )[: document.spec.days * 3]
                osm_food = {s.id for s in document.sources if "openstreetmap.org/" in str(s.url)}
                document.practical = [
                    item
                    for item in document.practical
                    if item.category != "food"
                    or not (set(item.source_ids) & osm_food)
                    or item.label in names
                ]
                pairs = {
                    s.from_name + " → " + s.to_name
                    for day in document.days
                    for s in ([day.arrival_segment] if day.arrival_segment else []) + day.segments
                }
                document.practical = [
                    item
                    for item in document.practical
                    if item.category != "transport"
                    or " → " not in item.label
                    or item.label in pairs
                ]
        document.practical = [
            item for item in document.practical if item.category == "budget" or item.source_ids
        ]
        unique = {}
        for item in document.practical:
            unique.setdefault((item.category, item.label, item.value), item)
        document.practical = list(unique.values())
        document.reader_checks = []
        import math

        group_size = max(1, math.ceil(len(document.pois) / 4))
        for offset in range(0, len(document.pois), group_size):
            group = document.pois[offset : offset + group_size]
            targets = [
                {"poi": p.name, "url": p.official_url or p.verification_url}
                for p in group
                if p.official_url or p.verification_url
            ]
            if not targets:
                continue
            names = "、".join(p.name for p in group)
            for rule in [
                "核对预约渠道、普通观众要求及免预约条件。",
                "核对当日开放、末次入场与临时交通调整。",
            ]:
                document.reader_checks.append(
                    ReaderCheck(
                        poi=names, text=rule, official_url=targets[0]["url"], targets=targets
                    )
                )
        document.reader_checks = document.reader_checks[:8]
    notices, practical = {}, []
    for item in document.practical:
        if item.category == "注意事项":
            item.category = "notice"
        key = (item.value, item.estimated, item.basis, tuple(item.amount_range))
        if item.category == "notice" and key in notices:
            previous = notices[key]
            if item.label != previous.label:
                previous.label += "；" + item.label
            previous.source_ids = list(dict.fromkeys([*previous.source_ids, *item.source_ids]))
        else:
            practical.append(item)
            if item.category == "notice":
                notices[key] = item
    document.practical = practical
    # Original warnings and uncited research stay in state/debug JSON, never reader appendices.
    updated = {p.id: p for p in document.pois}
    for day in document.days:
        day.stops = [updated[p.id] for p in day.stops]
    used = referenced_sources(document)
    if document.prd_version != "1.3" or document.spec.type != "travel_guide" or document.days:
        document.sources = [s for s in document.sources if s.id in used]
    return document
