"""Conservative parsing of a single explicit opening window, not guessed hours."""

import re

from pydantic import ValidationError

from backend.harness.schemas import OpeningHours


def parse_hours(text, source_ids):
    exhibition = re.search(r"办公区.*?展\s*厅\s*[:：]", text, re.S)
    if exhibition:
        text = text[exhibition.end():]

    def number(period, hour, minute):
        value = int(hour) + (12 if period in {"下午", "晚上", "晚"} and int(hour) < 12 else 0)
        return f"{value:02}:{minute}"

    pattern = r"(上午|下午|晚上|早上|早|晚)?\s*(\d{1,2})[:：](\d{2})\s*[-—–至~～]+\s*(上午|下午|晚上|早上|早|晚)?\s*(\d{1,2})[:：](\d{2})"
    matches = list(re.finditer(pattern, text))
    windows = [match.groups() for match in matches]
    note = ""
    if "全天" in text and not windows:
        opening, closing = "00:00", "23:59"
    elif len(windows) == 1:
        opening, closing = number(*windows[0][:3]), number(*windows[0][3:])
    elif not windows:
        cards = re.findall(r"(\d{1,2})[:：](\d{2})\s*(开馆|开园|闭馆|闭园)", text)
        opens = [(hour, minute) for hour, minute, label in cards if label in {"开馆", "开园"}]
        closes = [(hour, minute) for hour, minute, label in cards if label in {"闭馆", "闭园"}]
        if len(opens) != 1 or len(closes) != 1:
            return None
        opening, closing = number('', *opens[0]), number('', *closes[0])
    else:
        weekday = r"(?:周|星期)[一二三四五六日天](?:(?:、|和|及|与|至)?(?:周|星期)?[一二三四五六日天])*\s*$"
        if not all(re.search(weekday, text[:match.start()]) for match in matches):
            return None
        opening = max(number(*window[:3]) for window in windows)
        closing = min(number(*window[3:]) for window in windows)
        if opening >= closing:
            return None
        note = "按各星期开放安排的共同开放时段保守排程；完整时间见来源。"
    last = re.search(r"(上午|下午|晚上)?\s*(\d{1,2})[:：](\d{2})[^。；;]{0,5}(?:停止(?:售票及)?入|停止检票)", text)
    closed = set()
    for group in re.findall(r"((?:周|星期)[一二三四五六日天](?:(?:、|和|及|与|至)?(?:周|星期)?[一二三四五六日天])*)(?:及除夕)?(?:全天)?(?:展厅)?(?:闭馆|休馆|闭园)", text):
        days = [min("一二三四五六日天".index(x), 6) for x in re.findall(r"[一二三四五六日天]", group)]
        if "至" in group and len(days) == 2:
            closed.update(range(days[0], days[1]+1))
        else:
            closed.update(days)
    if "节假日" in text or "节日" in text:
        note += "法定节假日及临时调整以官方公告为准"
    try:
        return OpeningHours(open=opening, close=closing, last_entry=number(*last.groups()) if last else None, closed_days=sorted(closed), source_ids=source_ids, note=note)
    except ValidationError:
        return None


def fact_hours(fact, sources):
    parsed = parse_hours(fact.evidence, fact.source_ids)
    if not parsed:
        return None
    from backend.harness.reviewer import evidence_text
    quote = evidence_text(fact.evidence)
    for source in sources:
        if source.id not in fact.source_ids:
            continue
        text = evidence_text(source.text)
        start = text.find(quote)
        if start < 0:
            continue
        # Nearby closure notices are usable only when they do not introduce another time window.
        context = text[max(0, start-100):start+len(quote)+180]
        contextual = parse_hours(context, fact.source_ids)
        if contextual and (contextual.open, contextual.close) == (parsed.open, parsed.close):
            return contextual
    return parsed
