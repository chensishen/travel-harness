"""Source provenance and field normalization, separate from model prose."""

import re
from datetime import date, datetime
from urllib.parse import urlparse

from backend.harness.quality import UNKNOWN
from backend.harness.reviewer import evidence_text
from backend.kb.ingest import normalize_url

# Curated official ownership; arbitrary .com hosts must never be promoted by a model.
OFFICIAL_HOSTS = {
    "zjmuseum.com.cn", "shanghaimuseum.net", "shanghaimuseum.org", "njmuseum.com",
    "njmuseum.cn", "liuchaomuseum.com", "zschina.org.cn",
    "shaanximuseum.com", "bmy.com.cn", "dtxsmuseum.com", "suzhoumuseum.com",
    "panmen.com.cn", "yugarden.com.cn", "sxhm.com", "shanghaimuseum.cn", "shhkjrm.com", "supec.org.cn",
    "whc.unesco.org", "unesco.org",
    "zjam.org.cn", "chinasilkmuseum.com",
    "nmgbwy.cn",  # 内蒙古自治区文旅厅直属单位导航指向的博物院官网。
    "ordosbwg.org.cn",  # 鄂尔多斯市文旅局 http://wlj.ordos.gov.cn/ 直接链接的博物院官网。
    "norincogroup.com.cn",  # 国家政务服务平台央企名录列明的兵器工业集团官网。
    "meet-in-shanghai.net",  # 上海市文旅推广网 /en/about/及页脚列明上海市文化和旅游局。
    "zgyd1921.com",  # 上海市政府英文馆舍介绍列明的中共一大纪念馆官网。
    "orientalpearltower.com",  # 上海市政府英文东方明珠介绍列明的场馆官网。
}


def source_level(url):
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if host == "gov.cn" or host.endswith((".gov.cn", ".gov")) or any(host == root or host.endswith("." + root) for root in OFFICIAL_HOSTS):
        return "L1"
    if any(host == root or host.endswith("." + root) for root in ("wikipedia.org", "wikimedia.org", "wikidata.org", "openstreetmap.org")):
        return "L2"
    return "L3"


def candidate_pages(candidates, raw, seen, limit):
    """Spend the fetch budget on distinct, traceable pages, official sources first."""
    available = {}
    for candidate in candidates:
        url = normalize_url(str(candidate.url))
        if url in seen or (url not in raw and str(candidate.url) not in raw):
            continue
        available.setdefault(url, candidate)
    ranked, host_counts = [], {}
    for url, candidate in available.items():
        level = {"L1": 0, "L2": 1, "L3": 2}[source_level(url)]
        host = (level, urlparse(url).hostname)
        count = host_counts.get(host, 0)
        host_counts[host] = count + 1
        ranked.append((level, count, candidate, url))
    ranked.sort(key=lambda item: item[:2])
    return [(candidate, url) for _, _, candidate, url in ranked[:limit]]


def publication_date(source):
    """Recognize the dated article path used by government publishing systems."""
    explicit = re.search(r"(?:发布日期|发布时间|发文时间|信息时间|Published(?:\s+Time)?)\s*[:：]\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", source.text[:5000], re.I)
    if explicit:
        try:
            return date(*map(int, explicit.groups()))
        except ValueError:
            return None
    match = re.search(r"/20\d{4}/t(20\d{6})[_./]", urlparse(str(source.url)).path)
    if match:
        try:
            return datetime.strptime(match[1], "%Y%m%d").date()
        except ValueError:
            pass
    return None


def page_title(text, url, proposed=""):
    for line in text.splitlines()[:20]:
        line = line.strip().lstrip("# ")
        if 8 <= len(line) <= 70 and re.search(r"(?:通告|公告|参观攻略|参观须知|购票指南)$", line) and not re.match(r"URL:|Published:", line):
            return line
    heading = re.search(r"^#{1,2}\s+([^\n]+)", text, re.M)
    if heading and heading[1].strip().lower() not in {"(no title)", "no title"}:
        return heading[1].strip()[:200]
    title = proposed.strip()
    if title and title.lower() not in {"(no title)", "no title"} and not any(w in title for w in ("官网 门票", "开放时间 预约", "维基百科 历史")):
        return title[:200]
    return urlparse(url).hostname or "参考资料"


def clean_value(text):
    return re.sub(r"^(?:开始时间|开放时间|景点地址|地址|门票价格|票价|门票|预约方式)\s*[:：]\s*", "", text.strip())


def normalize_facts(poi, sources):
    for source in sources:
        source.published_at = publication_date(source)
    levels = {s.id: s.level for s in sources}
    by_id = {s.id: s for s in sources}
    chosen = {}
    for fact in poi.facts:
        fact.value = clean_value(fact.value)
        if fact.field == "ticket" and re.fullmatch(r"0\s*元[。.]?", fact.value):
            fact.value = "免费"
        if re.search(r"暂未核实|待核实|尚未核实|未确认", fact.value):
            fact.value, fact.source_ids, fact.evidence, fact.confidence = UNKNOWN, [], "", 0
        if fact.field == "ticket" and not re.search(r"免费|免票|\d+(?:\.\d+)?\s*元|[¥￥]\s*\d", fact.value):
            fact.value, fact.source_ids, fact.evidence, fact.confidence = UNKNOWN, [], "", 0
        if fact.field in {"ticket", "hours", "booking", "address"}:
            quote = evidence_text(fact.evidence)
            fact.source_ids = [r for r in fact.source_ids if r in by_id and levels[r] in {"L1", "L2"} and "openstreetmap.org" not in (urlparse(str(by_id[r].url)).hostname or "") and quote and quote in evidence_text(by_id[r].text)]
            dated = [by_id[r].published_at for r in fact.source_ids if by_id[r].published_at]
            fact.published_at = max(dated) if dated else None
            fact.freshness_note = ""
            if fact.published_at and (date.today() - fact.published_at).days > 365:
                recent = any(by_id[r].published_at and (date.today()-by_id[r].published_at).days <= 365 for r in fact.source_ids)
                if not recent:
                    fact.freshness_note = f"来源发布于 {fact.published_at:%Y-%m}，出行前请核实"
            elif fact.source_ids and not fact.published_at:
                fact.freshness_note = "来源发布日期未载明，出行前请核实"
        rank = max(({"L1": 3, "L2": 2, "L3": 1}.get(levels.get(s), 0) for s in fact.source_ids), default=0)
        if fact.field in {"ticket", "hours", "booking", "address"}:
            if rank < 2:
                fact.value, fact.source_ids, fact.evidence = UNKNOWN, [], ""
                fact.confidence = 0
            elif rank == 3:
                fact.confidence = max(fact.confidence, 0.9)
                fact.note = "官方来源，已标注核验日期"
        previous = chosen.get(fact.field)
        newer = False
        if previous is not None and rank == previous[0] and fact.field in {"ticket", "hours", "booking", "address"}:
            current_dates = [day for ref in fact.source_ids if ref in by_id and (day := publication_date(by_id[ref]))]
            previous_dates = [day for ref in previous[1].source_ids if ref in by_id and (day := publication_date(by_id[ref]))]
            newer = bool(current_dates and previous_dates and max(current_dates) > max(previous_dates))
        if previous is None or rank > previous[0] or newer or (previous[1].value == UNKNOWN and fact.value != UNKNOWN):
            chosen[fact.field] = (rank, fact)
    poi.facts = [item[1] for item in chosen.values()]
    def own_site(url):
        host=(urlparse(url).hostname or '').removeprefix('www.')
        # The official tourism portal is authoritative, but is not a venue-owned site.
        return any((host==root or host.endswith('.'+root)) and root != 'meet-in-shanghai.net' for root in OFFICIAL_HOSTS)
    official=next((s for s in sources if s.level=='L1' and own_site(str(s.url)) and any(s.id in f.source_ids for f in poi.facts)),None)
    if official:
        poi.official_url=str(official.url)
    elif not own_site(poi.official_url):
        poi.official_url=''
    evidence_sources=[s for s in sources if s.level=='L1' and any(s.id in f.source_ids for f in poi.facts) and not re.search(r'名录|名单|列表',s.title)]
    if evidence_sources:
        poi.verification_url=str(max(evidence_sources,key=lambda s:s.published_at or date.min).url)
    return poi
