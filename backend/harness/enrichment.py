"""Targeted official-source completion before writing or route scheduling."""

import hashlib
import json
import re
from datetime import date
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from backend.app.store import save_json
from backend.harness.quality import UNKNOWN
from backend.harness.reviewer import evidence_text, numeric_values
from backend.harness.schemas import (
    Block,
    Deliverable,
    FactRecord,
    OpeningHours,
    PracticalItem,
    SourceRef,
    now,
)
from backend.kb.ingest import normalize_url
from backend.tools.hours import fact_hours, parse_hours
from backend.tools.llm import IncompleteModelOutput
from backend.tools.mcp_search import MCPToolError, transport_failure
from backend.tools.sources import (
    candidate_pages,
    clean_value,
    normalize_facts,
    page_title,
    source_level,
)


class CandidateSources(BaseModel):
    sources: list[SourceRef] = Field(max_length=6)


class CandidateFacts(BaseModel):
    facts: list[FactRecord]


class VisitInformation(BaseModel):
    facts: list[FactRecord]
    hours: OpeningHours | None = None
    contact: str = ""
    introduction: Block | None = None
    practical: list[FactRecord] = Field(default_factory=list)


def public_space_city_matches(source, poi, city):
    # Public squares and parks commonly share names across cities.
    if not poi.name.endswith(("广场", "公园")):
        return True
    target = re.sub(r"[\W_]+", "", city).removesuffix("市")
    context = re.sub(r"[\W_]+", "", source.title + source.text)
    return target in context


def source_matches_poi(source, poi, city):
    if not public_space_city_matches(source, poi, city):
        return False
    def compact(value):
        return re.sub(r"[\W_]+", "", value.casefold())

    text = compact(source.text)
    for name in [poi.name, *poi.aliases]:
        key = compact(name)
        short = key.removeprefix(compact(city))
        if key in text or (len(short) >= 3 and short in text):
            return True
        if len(short) > 4 and short.endswith("门"):
            site, gate = short[:-3], short[-3:]
            if (
                compact(city) + site in compact(source.title + " " + source.text[:1000])
                and gate in text
            ):
                return True
    return False


def scoped_source_text(source, poi, city, peers=()):
    if not public_space_city_matches(source, poi, city):
        return ""
    def compact(value):
        return re.sub(r"[\W_]+", "", value.casefold())

    title = compact(source.title)
    keys = [compact(name) for name in [poi.name, *poi.aliases]]
    if not any(k in title for k in keys) and any(
        p.id != poi.id and compact(p.name) in title for p in peers
    ):
        return ""
    sections = re.split(r"(?=^[^\n]*📍)", source.text, flags=re.M)
    if len(sections) > 2:
        return "\n\n".join(
            section
            for section in sections
            if section.strip() and any(k in compact(section.strip().splitlines()[0]) for k in keys)
        )
    if not re.search(
        r"\|[^\n]*(?:博物馆名称|场馆名称|景区名称|景点名称|单位名称)[^\n]*\|", source.text
    ):
        return source.text
    keys.extend(k.removeprefix(compact(city)) for k in keys.copy())
    return "\n\n".join(
        line
        for line in source.text.splitlines()
        if line.lstrip().startswith("|")
        and any(compact(cell) in keys for cell in line.split("|") if cell.strip())
    )


def supported_visit_fact(fact, source, poi, city, peers=()):
    text = scoped_source_text(source, poi, city, peers)
    if (
        not text
        or not evidence_text(fact.evidence)
        or evidence_text(fact.evidence) not in evidence_text(text)
    ):
        return False
    if fact.field == "address" and re.fullmatch(
        r"(?:[\u4e00-\u9fff]{1,10}(?:省|市|区|县|自治区|特别行政区)){1,5}", fact.value.strip()
    ):
        return False
    if fact.field == "ticket" and re.search(r"曾有|被指|未通过审批|未批先涨|历史争议", fact.value):
        return False
    if (
        fact.field == "booking"
        and re.search(r"团队|团体", fact.evidence)
        and not re.search(r"个人|普通观众|所有游客|所有观众|散客", fact.evidence)
    ):
        return False
    return True


def recover_opening_fact(info, sources):
    """A structured hours object does not excuse omitting its traceable fact row."""
    if (
        any(f.field == "hours" and f.source_ids and f.value != UNKNOWN for f in info.facts)
        or not info.hours
    ):
        return
    for ref in info.hours.source_ids:
        source = sources.get(ref)
        if not source or source.level not in {"L1", "L2"}:
            continue
        for line in source.text.splitlines():
            parsed = parse_hours(line, [ref])
            if not parsed or parsed.open != info.hours.open or parsed.close != info.hours.close:
                continue
            value = f"{parsed.open}–{parsed.close}"
            if parsed.last_entry:
                value += f"；{parsed.last_entry}停止入场"
            if parsed.closed_days:
                value += "；" + "、".join(
                    "周" + "一二三四五六日"[d] + "闭馆" for d in parsed.closed_days
                )
            info.facts.append(
                FactRecord(
                    field="hours", value=value, source_ids=[ref], evidence=line, confidence=0.9
                )
            )
            info.hours = parsed
            return


async def enrich(harness, document, folder=None, *, search_missing=True, verified_names=()):
    visit_fields = {"ticket", "hours", "booking", "address"}
    incoming_pois = {p.name: p for p in document.pois}
    signature = hashlib.sha256(
        json.dumps(
            {
                "policy": "source-recovery-v13-primary-visit-fields",
                "search_missing": search_missing,
                "spec": document.spec.model_dump(mode="json"),
                "pois": [p.name for p in document.pois],
                "sources": [(str(s.url), source_level(str(s.url)), s.text) for s in document.sources],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
    ).hexdigest()
    completed = list(verified_names)
    progress_path = folder / "enrichment-progress.json" if folder else None
    if progress_path and progress_path.exists():
        saved = json.loads(progress_path.read_text())
        if saved.get("signature") == signature:
            document = Deliverable.model_validate(saved["document"])
            completed = saved["completed"]
            for poi in document.pois:
                present = {f.field for f in poi.facts}
                poi.facts.extend(
                    f.model_copy(deep=True) for f in incoming_pois[poi.name].facts
                    if f.field not in visit_fields and f.field not in present
                )
    for source in document.sources:
        source.level = source_level(str(source.url))
        source.title = page_title(source.text, str(source.url), source.title)
    existing_sources = {s.id: s for s in document.sources}
    for poi in document.pois:
        if document.prd_version == "1.3":
            foreign = {s.id for s in document.sources
                       if not public_space_city_matches(s, poi, document.spec.city)}
            invalid = [f for f in poi.facts if foreign.intersection(f.source_ids) or (
                f.field == "booking" and f.source_ids and not any(
                    ref in existing_sources and supported_visit_fact(
                        f, existing_sources[ref], poi, document.spec.city, document.pois
                    ) for ref in f.source_ids
                )
            )]
            if invalid or any(
                foreign.intersection(b.source_ids) for b in poi.blocks
            ):
                poi.facts = [f for f in poi.facts if f not in invalid]
                poi.blocks = [b for b in poi.blocks if not foreign.intersection(b.source_ids)]
                if poi.name in completed:
                    completed.remove(poi.name)
        if poi.name in completed:
            continue
        if folder:
            harness.store.event(
                folder.name,
                node="Verifier",
                status="running",
                message=f"核验 {len(completed) + 1}/{len(document.pois)}：{poi.name} 的参观信息",
                progress=3 / 9,
            )
        normalize_facts(poi, document.sources)
        incomplete = any(
            not any(f.field == field and f.source_ids and f.value != UNKNOWN for f in poi.facts)
            for field in ["ticket", "hours", "booking", "address"]
        )
        stale = any(
            f.source_ids and (f.freshness_note or f.published_at is None)
            for f in poi.facts
            if f.field in {"ticket", "hours", "booking"}
        )
        if (incomplete or (document.prd_version == "1.3" and stale)) and search_missing:
            site = urlparse(poi.official_url).hostname
            queries = [
                f'{document.spec.city} "{poi.name}" {"site:" + site if site else "官网"} 参观须知 门票 开放时间 预约'
            ]
            queries.append(
                f'{document.spec.city} "{poi.name}" site:gov.cn 门票 地址 开放时间 个人预约 临时闭馆 修缮 恢复开放 最新公告'
            )
            for query in queries:
                if folder:
                    harness.store.event(
                        folder.name,
                        node="Verifier",
                        status="running",
                        message=f"补查官方渠道：{poi.name}",
                        progress=3 / 9,
                    )
                poi.verification_attempts.append(query)
                try:
                    raw = await harness.search.search(query)
                    candidates = await harness.models.json(
                        "只提取搜索结果中的真实标题及完整网址，禁止猜测；source字段level=L3、id=''、fetched_at=''、text=''。优先目标场馆官网与政府参观公告。",
                        {"results": raw[:30000]},
                        CandidateSources,
                    )
                    existing = {normalize_url(str(s.url)) for s in document.sources}
                    for candidate, url in candidate_pages(candidates.sources, raw, existing, 3):
                        try:
                            text = await harness.fetch.fetch(url)
                        except (httpx.HTTPError, ValueError):
                            continue
                        source = SourceRef(
                            id=f"S{max([int(s.id[1:]) for s in document.sources if re.fullmatch(r'S\d+', s.id)], default=0) + 1}",
                            title=page_title(text, url, candidate.title),
                            url=url,
                            level=source_level(url),
                            fetched_at=(
                                harness.store.cache_created_at(harness.store.key("fetch", url))
                                or now()
                            )
                            if hasattr(harness, "store")
                            else now(),
                            text=text,
                        )
                        document.sources.append(source)
                        existing.add(url)
                except Exception as error:
                    if not (
                        transport_failure(error)
                        or isinstance(error, (httpx.HTTPError, ValueError, MCPToolError))
                    ):
                        raise
                    poi.verification_attempts.append("渠道不可用：" + type(error).__name__)
        relevant = [
            s
            for s in document.sources
            if s.id in {r for f in poi.facts for r in f.source_ids}
            or source_matches_poi(s, poi, document.spec.city)
        ]
        if not relevant:
            relevant = document.sources
        if document.prd_version == "1.3":
            relevant = [
                s.model_copy(update={"text": text})
                for s in relevant
                if s.level in {"L1", "L2"}
                and (text := scoped_source_text(s, poi, document.spec.city, document.pois))
            ]
        own_host = (urlparse(poi.official_url).hostname or "").removeprefix("www.")
        relevant = sorted(
            relevant,
            key=lambda s: (
                bool(own_host)
                and (urlparse(str(s.url)).hostname or "").removeprefix("www.") == own_host,
                s.level == "L1",
                s.published_at or date.min,
                s.fetched_at,
            ),
            reverse=True,
        )
        short = poi.name.removeprefix(document.spec.city)
        if len(short) > 4 and short.endswith("门"):
            site, gate = short[:-3], short[-3:]
            if (
                any(
                    site in s.title and document.spec.city in s.title and gate in s.text
                    for s in relevant
                )
                and gate not in poi.aliases
            ):
                poi.aliases.append(gate)
        instruction = (
            "核验目标场馆参观资料，仅使用提供的来源。不同馆区与入口严格分开。"
            "只使用明确对应目标城市和场馆的事实；同名异地景点不得互用。来源冲突时优先较新的目标馆区公告，抓取日期不等于发布日期。"
            "facts包含ticket/hours/booking/address及intro；value简短清晰，去掉标签前缀，evidence为来源中连续原文。"
            "若官方公告在visit_date仍明确闭馆、停业或修缮暂停开放，另返回field=closure的事实并注明有效期；未给结束时间且未查到恢复公告时保守处理。已结束的固定日期闭馆不能返回closure。不得以常规开放时间覆盖临时闭馆公告。"
            "资料表格已按目标实体裁切，只能引用该实体行，不能拼接独立片段。地址必须到路名门牌或明确入口名，只有省市不能作为已核实地址。"
            "预约说明须保留普通观众的预约渠道及免预约例外，不得只摘录特殊人群政策；引文应覆盖这两部分。"
            "只适用于团队讲解或团队快速通道的政策不能作为普通观众预约规则。寺院入寺与寺内塔楼登塔分开；若仅查到寺院时段，value必须标明入寺时段，登塔时间另核。"
            "票价保留优惠与季节限制；官方L1单一来源可采信。无证据value写'暂未核实，请以官方渠道为准'，source_ids=[]。"
            "同一公告同时提到免费开放和收费展览时，ticket.value必须保留收费展览例外；不得把已结束的延时开放写成当前安排。"
            "hours仅在有来源开放时间时填写：open/close/last_entry为HH:MM；closed_days周一=0至周日=6。全天写00:00–23:59。"
            "不得猜开放时间；注明周闭馆、法定节假日例外和阶段性公告的适用日期，不能把临时延长或取消闭馆日的安排写成永久政策；source_ids必须有效。"
            "按visit_date判断适用时段，当前日期内生效的场馆自有临时公告优先常规名录；hours先列该日期适用时间，ticket先列该日期适用价格并保留季节、优惠与另收费限制。"
            "contact仅写来源中的官方电话。introduction写一条有来源的简洁景点介绍，改写，不复制整段。"
            "practical提取与目标馆区相关的公交到达、停车预约、限行、限流、免费接驳等实用公告；"
            "每项FactRecord的field为transport或notice，value为一句简洁改写，evidence为连续原文。没有则空列表。"
            "以现行参观须知及最新官方调整为准；不得把过时疫情限流公告当作今天的预约规则。"
            "不得补充来源以外的数字，不得将售票口、车站或其他馆区地址代替本目标。"
        )
        payload = {
            "city": document.spec.city,
            "poi": poi.name,
            "visit_date": (document.spec.start_date or date.today()).isoformat(),
            "sources": [
                {**s.model_dump(mode="json"), "text": s.text[:14000]} for s in relevant[:12]
            ],
        }
        try:
            info = await harness.models.json(instruction, payload, VisitInformation)
        except IncompleteModelOutput:
            try:
                info = await harness.models.json(
                    instruction + "每个字段最多一条，只保留必要的连续引文；实用公告最多两条。"
                    "每条事实只引用最直接的来源，省略非必要的可选字段，返回紧凑 JSON。",
                    {**payload, "sources": [
                        {**source, "text": source["text"][:3000]} for source in payload["sources"][:5]
                    ]},
                    VisitInformation,
                )
            except IncompleteModelOutput:
                if document.spec.type != "culture_intro":
                    raise
                poi.verification_attempts.append("补核输出不完整，保留已有核实事实，缺项交由定向核验")
                info = VisitInformation(facts=[
                    fact for fact in poi.facts if fact.field in visit_fields and fact.source_ids
                ])
        if folder:
            save_json(folder / f"visit-candidates-{poi.id}.json", info.model_dump(mode="json"))
        by_id = {s.id: s for s in document.sources}
        recover_opening_fact(info, by_id)
        accepted = []
        for fact in info.facts:
            if not fact.source_ids:
                fact.value, fact.evidence = UNKNOWN, ""
                accepted.append(fact)
                continue
            quote = evidence_text(fact.evidence)
            if not quote or not any(
                r in by_id and quote in evidence_text(by_id[r].text) for r in fact.source_ids
            ):
                continue
            if any(r not in by_id for r in fact.source_ids):
                continue
            if document.prd_version == "1.3":
                fact.source_ids = [
                    r
                    for r in fact.source_ids
                    if supported_visit_fact(fact, by_id[r], poi, document.spec.city, document.pois)
                ]
                if not fact.source_ids:
                    poi.verification_attempts.append(
                        fact.field + "：引文不属于目标场馆或不是当前可用参观信息，未采信"
                    )
                    continue
            fact.value = clean_value(fact.value)
            if not numeric_values(fact.value) <= numeric_values(fact.evidence):
                continue
            fact.verified_at = date.today()
            accepted.append(fact)
        retained = [f for f in poi.facts if f.field not in visit_fields
                    and f.field not in {item.field for item in accepted}]
        if document.prd_version == "1.3":
            poi.facts = accepted + retained + [
                FactRecord(field=field, value=UNKNOWN)
                for field in ["ticket", "hours", "booking", "address"]
                if not any(f.field == field for f in accepted)
            ]
        elif accepted:
            poi.facts = accepted + retained
        normalize_facts(poi, document.sources)
        await repair_missing_facts(harness, document, poi, relevant, folder)
        hours_fact = next((f for f in poi.facts if f.field == "hours" and f.source_ids), None)
        # The model's weekday indices and mixed-campus notes are not authoritative.
        poi.hours = fact_hours(hours_fact, document.sources) if hours_fact else None
        if info.contact and any(info.contact in s.text for s in relevant):
            poi.contact = info.contact
        if (
            info.introduction
            and info.introduction.source_ids
            and all(r in by_id for r in info.introduction.source_ids)
        ):
            if document.prd_version != "1.3" or any(
                by_id[r].level in {"L1", "L2"} for r in info.introduction.source_ids
            ):
                if document.prd_version != "1.3" or not poi.blocks:
                    poi.blocks = [info.introduction]
            else:
                poi.verification_attempts.append(
                    "L3单一来源简介仅作研究线索，待L1/L2资料支持后再写正文"
                )
        apply_practical(document, poi, info.practical)
        if not poi.verification_attempts:
            poi.verification_attempts.append("已读取官方参观资料并逐字段核验")
        completed.append(poi.name)
        if progress_path:
            save_json(
                progress_path,
                {
                    "signature": signature,
                    "completed": completed,
                    "document": document.model_dump(mode="json"),
                },
            )
            harness.store.event(
                folder.name,
                node="Verifier",
                status="running",
                message=f"参观信息核验 {len(completed)}/{len(document.pois)}：{poi.name}",
                progress=3 / 9,
            )
    return document


async def repair_missing_facts(harness, document, poi, sources, folder):
    """Retry rejected rows against the already collected official text, without inventing gaps."""
    by_id = {s.id: s for s in sources if s.level in {"L1", "L2"}}
    missing = [
        field
        for field in ["ticket", "hours", "booking", "address"]
        if not any(f.field == field and f.source_ids and f.value != UNKNOWN for f in poi.facts)
    ]
    if not missing or not by_id:
        return
    if folder:
        harness.store.event(
            folder.name,
            node="Verifier",
            status="running",
            message=f"再次核对 {poi.name} 的缺失字段与原文引文",
            progress=3 / 9,
        )
    # This is a targeted follow-up. The caller ranks official, recent sources first.
    excerpts = [
        {**s.model_dump(mode="json"), "text": s.text[:2500]}
        for s in list(by_id.values())[:8]
    ]
    result = await harness.models.json(
        "补核指定景点的缺失字段。只返回missing中的字段；每项evidence必须逐字复制同一个来源中连续的一段，"
        "严禁跨行抽取半句后拼接，不得自行添加分号。value仅概括该段明确内容，不能扩展。"
        "找不到就不返回该字段。hours必须返回一项FactRecord，不能另写hours对象。"
        "不同馆区严格分开；地址仅选一个游客入口。优先现行规则，区分票价、售票时间和闭馆时间。"
        "只接受目标实体的表格行；地址不能只有省市，缺门牌可用官方明确入口名。不得把历史争议票价当现行票价；按visit_date保留适用季节或临时有效期。"
        "避免把团体讲解预约、停车预约写成个人参观预约。",
        {
            "poi": poi.name,
            "visit_date": (document.spec.start_date or date.today()).isoformat(),
            "missing": missing,
            "sources": excerpts,
        },
        CandidateFacts,
    )
    if folder:
        save_json(folder / f"visit-repair-{poi.id}.json", result.model_dump(mode="json"))
    for fact in result.facts:
        quote = evidence_text(fact.evidence)
        refs = [
            r
            for r in fact.source_ids
            if r in by_id
            and quote
            and quote in evidence_text(by_id[r].text)
            and (
                document.prd_version != "1.3"
                or supported_visit_fact(fact, by_id[r], poi, document.spec.city, document.pois)
            )
        ]
        if fact.field not in missing or not refs:
            continue
        fact.source_ids = refs
        if not numeric_values(fact.value) <= numeric_values(fact.evidence):
            fact.value = clean_value(fact.evidence)
        fact.verified_at = date.today()
        poi.facts = [f for f in poi.facts if f.field != fact.field] + [fact]
    normalize_facts(poi, document.sources)


def apply_practical(document, poi, notices):
    by_id = {s.id: s for s in document.sources}
    for notice in notices:
        refs = notice.source_ids
        quote = evidence_text(notice.evidence)
        if notice.field not in {"transport", "notice"} or not refs or not quote:
            continue
        if not all(
            r in by_id and by_id[r].level in {"L1", "L2"} and quote in evidence_text(by_id[r].text)
            for r in refs
        ):
            continue
        if not numeric_values(notice.value) <= numeric_values(notice.evidence):
            continue
        item = PracticalItem(
            category=notice.field,
            label=poi.name + " · 参观提示",
            value=notice.value,
            source_ids=refs,
        )
        if item not in document.practical:
            document.practical.append(item)
