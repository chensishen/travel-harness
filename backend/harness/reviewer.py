import re
from datetime import date
from decimal import Decimal
from urllib.parse import urlparse

from backend.harness.schemas import Deliverable

SENSITIVE = {"ticket", "hours", "booking", "address"}


def numeric_values(text):
    values = {Decimal(value) for value in re.findall(r"\d+(?:\.\d+)?", text)}
    for hour in re.findall(r"(?:下午|晚上)\s*(\d{1,2})[:：]\d{2}", text):
        if int(hour) < 12:
            values.add(Decimal(int(hour) + 12))
    return values


def evidence_text(text: str):
    # Ignore Chinese layout whitespace while preserving spaces between Latin words/numbers.
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"(?<=[^\x00-\x7f]) | (?=[^\x00-\x7f])", "", text)


def validate_facts(document: Deliverable):
    sources = {s.id: s for s in document.sources}
    if len(sources) != len(document.sources):
        raise ValueError("来源编号重复，无法唯一回查")
    for poi in document.pois:
        for fact in poi.facts:
            if not fact.source_ids:
                if fact.value != "暂未核实，请以官方渠道为准":
                    raise ValueError(f"{poi.name} 的 {fact.field} 没有来源")
                continue
            if any(s not in sources for s in fact.source_ids):
                raise ValueError("事实引用了不存在的来源")
            if fact.field in SENSITIVE and any(sources[s].level == "L3" for s in fact.source_ids):
                raise ValueError("票价、时间、预约和地址不能使用 L3 来源")
            if not (quote := evidence_text(fact.evidence)) or not any(
                quote in evidence_text(sources[s].text) for s in fact.source_ids
            ):
                raise ValueError(f"{poi.name} 的 {fact.field} 证据无法在原文中回查")
            if not numeric_values(fact.value) <= numeric_values(fact.evidence):
                raise ValueError(f"{poi.name} 的 {fact.field} 数值与引文不符")
            if fact.verified_at > date.today():
                raise ValueError("事实核验日期不能在未来")
            if fact.field in SENSITIVE and (date.today() - fact.verified_at).days > 90:
                raise ValueError("敏感事实超过 90 天有效期，需要重新核验")
    blocks = [b for p in document.pois for b in p.blocks]
    blocks += [b for c in document.chapters for b in c.blocks]
    for block in blocks:
        if (block.kind == "fact" and not block.source_ids) or any(
            s not in sources for s in block.source_ids
        ):
            raise ValueError("正文存在无效或缺失的来源编号")
        if document.prd_version == "1.3" and block.kind == "fact":
            hosts = {urlparse(str(sources[r].url)).hostname for r in block.source_ids}
            if not any(sources[r].level in {"L1", "L2"} for r in block.source_ids) and len(hosts) < 2:
                raise ValueError("L3 单一来源事实不得进入正文，须回查L1或L2")


def text_blocks(document):
    blocks = {f"poi:{p.id}:{i}": b for p in document.pois for i, b in enumerate(p.blocks)}
    blocks.update(
        {
            f"chapter:{i}:{j}": b
            for i, c in enumerate(document.chapters)
            for j, b in enumerate(c.blocks)
        }
    )
    return blocks


def route_issues(document: Deliverable):
    issues = []
    for day in document.days:
        large = any(p.duration_min >= 180 for p in day.stops)
        lower, upper = (2, 3) if large else (3, 4)
        if not lower <= len(day.stops) <= upper:
            issues.append(f"第 {day.day} 天景点数量不满足每日 3–4 个（大型景点 2–3 个）要求")
        if (
            sum(p.duration_min for p in day.stops) + sum(s.duration_min for s in day.segments) + 120
            > 600
        ):
            issues.append(f"第 {day.day} 天游玩、交通与用餐超过 10 小时")
        if any(s.duration_min > 40 for s in day.segments):
            issues.append(f"第 {day.day} 天存在相邻景点交通超过 40 分钟的路段")
        if any(s.estimated for s in day.segments):
            issues.append(f"第 {day.day} 天公共交通为估算，不能视为已核验的公交路线")
        issues.extend(day.notes[1:2])
    return list(dict.fromkeys(issues))
