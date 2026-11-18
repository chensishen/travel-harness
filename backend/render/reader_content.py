from urllib.parse import urlparse

from backend.harness.quality import UNKNOWN

LABELS = {"ticket": "门票", "hours": "开放时间", "booking": "预约", "address": "地址"}


def fact_rows(poi, sources):
    rows = []
    levels = {s.id: s.level for s in sources}
    for field, label in LABELS.items():
        fact = next((f for f in poi.facts if f.field == field), None)
        known = fact and fact.source_ids and fact.value != UNKNOWN
        value = fact.value if known else "待核实，请查阅官方参观渠道"
        if field == "hours" and poi.hours:
            hours = poi.hours
            value = f"{hours.open}–{hours.close}"
            if hours.last_entry:
                value += f"；{hours.last_entry} 停止入场"
            if hours.closed_days:
                value += "；" + "、".join(
                    "周" + "一二三四五六日"[d] + "闭馆" for d in hours.closed_days
                )
            if hours.note:
                value += "；" + hours.note
        if known and fact.freshness_note:
            value += "；" + fact.freshness_note
        evidence = (
            (
                "发布 "
                + (str(fact.published_at) if fact.published_at else "未载明")
                + " / 访问 "
                + str(fact.verified_at)
                + " / "
                + "、".join(f"{r} · {levels.get(r, '')}" for r in fact.source_ids)
            )
            if known
            else "未核实"
        )
        rows.append([label, value, evidence])
    return rows


def photo_caption(poi, image):
    if image.author == "用户提供":
        return poi.name + " · 用户提供（用户声明拥有授权）"
    site = urlparse(str(image.source_page)).hostname or "图片来源"
    return f"{image.caption or poi.name} · 摄影：{image.author} · 授权：{image.license} · {site}"


def citation(ids):
    return " [" + "、".join(dict.fromkeys(ids)) + "]" if ids else ""


def checks(document):
    result = [(c.poi + "：" + c.text, c.official_url) for c in document.reader_checks]
    if document.prd_version == "1.3":
        return result[:8]
    for poi in document.pois:
        if len(result) >= 5:
            break
        if poi.official_url and not any(text.startswith(poi.name + "：") for text, _ in result):
            result.append((poi.name + "：出发前确认预约状态与临时开放公告。", poi.official_url))
    return result[:5]


def basis(document, ids, estimated=False, note=""):
    if estimated:
        return note or "经验参考，非有据报价"
    by_id = {s.id: s for s in document.sources}
    parts = []
    for ref in ids:
        s = by_id.get(ref)
        if s:
            parts.append(
                f"{ref} · {s.level} · 发布 "
                + (s.published_at.strftime("%Y-%m") if s.published_at else "未载明")
            )
    return "；".join(parts) or note or "待补依据"


def brief_value(label, value):
    if len(value) <= 42:
        return value
    if label == "门票" and value.startswith("免费"):
        return (
            "免费；含收费项目，完整规则见Word"
            if any(x in value for x in ["收费", "另购", "另计"])
            else "免费；适用范围与例外见Word"
        )
    if label == "预约" and ("官网" in value or "官方网站" in value) and "公众号" in value:
        return "官网或公众号预约；免预约例外见Word及备注"
    return value[:26].rstrip("，；。") + "…（完整说明见Word及备注）"
