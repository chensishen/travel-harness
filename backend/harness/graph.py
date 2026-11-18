import asyncio
import hashlib
import json
import re
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, create_model

from backend.app.store import save_json
from backend.harness.chronology import age_issues
from backend.harness.enrichment import enrich
from backend.harness.metrics import job_metrics, measure, set_metric_stage
from backend.harness.preparation import prepare
from backend.harness.progress import STAGES
from backend.harness.quality import require_quality
from backend.harness.reviewer import SENSITIVE, text_blocks, validate_facts
from backend.harness.schemas import (
    NODES,
    PHOTO_SKIPPED,
    POI,
    Block,
    CultureChapter,
    Deliverable,
    FactRecord,
    PracticalItem,
    SourceRef,
    TaskSpec,
    now,
)
from backend.kb.ingest import KnowledgeBase, normalize_url
from backend.render.documents import render
from backend.render.inspection import inspect_office
from backend.render.map_renderer import geojson
from backend.render.preview import available, render_previews
from backend.tools.fetch import FetchProvider, blocked_page, damaged_text, missing_time_cards
from backend.tools.geo import Geo
from backend.tools.images import generated_image
from backend.tools.llm import ConfigurationError, IncompleteModelOutput, Models
from backend.tools.mcp_search import MCPProvider, MCPToolError, SearchProvider, search_unavailable
from backend.tools.osm import OSM
from backend.tools.photos import Photos
from backend.tools.planning import plan as plan_itinerary
from backend.tools.sources import candidate_pages, page_title, source_level


class ResearchBacktrack(ValueError):
    pass


def trim_low_verification_culture_pois(document):
    """Keep culture-intro destinations that have enough visit evidence to publish."""
    if document.spec.type != "culture_intro":
        return []

    def score(poi):
        verified = {fact.field for fact in poi.facts if fact.source_ids}
        if poi.hours and poi.hours.source_ids:
            verified.add("hours")
        return len(verified & {"ticket", "hours", "booking", "address"})

    kept = [poi for poi in document.pois if score(poi) >= 3]
    removed = [poi for poi in document.pois if poi not in kept]
    if removed and len(kept) >= 2:
        document.alternatives.extend(
            {"name": poi.name, "reason": "票价、开放时间、预约或地址中可核实字段不足，未编入本次发布页面。"}
            for poi in removed
        )
        document.warnings.append(
            "部分目的地的参观信息核实不足，已从 PPT 页面中省略：" + "、".join(p.name for p in removed)
        )
        document.pois = kept
    return [poi.name for poi in removed] if removed and len(kept) >= 2 else []


def require_visit_coverage(state, document):
    from backend.harness.quality import verified_visit_fields

    minimum_stops = document.spec.days * 3
    counts = sorted(
        (len(verified_visit_fields(p, document.sources)) for p in document.pois), reverse=True
    )
    if len(counts) < minimum_stops or sum(counts[:minimum_stops]) >= minimum_stops * 3 * .75:
        return
    incomplete = [p.name for p in document.pois
                  if len(verified_visit_fields(p, document.sources)) < 3]
    if state.get('research_replenishment', 0) >= 2:
        raise ValueError('两轮补查后参观资料仍不足75%核实要求，尚未计算或发布不合格路线：' + '、'.join(incomplete))
    state['research_replenishment'] = state.get('research_replenishment', 0) + 1
    state.pop('reuse_verified_pois', None)
    state['outline']['queries'] = [
        f'{document.spec.city} "{name}" 官方 参观须知 门票 开放时间 散客预约'
        for name in incomplete
    ][:16]
    raise ResearchBacktrack('参观资料不足，先回退Researcher定向核验再计算路线：' + '、'.join(incomplete))


class Outline(BaseModel):
    title: str
    poi_names: list[str] = Field(max_length=56)
    themes: list[str] = Field(min_length=1, max_length=12)
    queries: list[str] = Field(min_length=1, max_length=16)


class SearchResults(BaseModel):
    sources: list[SourceRef] = Field(max_length=8)


class FactSet(BaseModel):
    pois: list[POI]


class Writing(BaseModel):
    pois: list[POI]
    chapters: list[CultureChapter]
    practical: list[PracticalItem] = Field(default_factory=list)


class GuideStopWriting(BaseModel):
    id: str
    name: str
    blocks: list[Block] = Field(default_factory=list)


class GuideWriting(BaseModel):
    pois: list[GuideStopWriting]


class CultureWriting(BaseModel):
    chapters: list[CultureChapter]
    practical: list[PracticalItem] = Field(default_factory=list)


class TextIssue(BaseModel):
    block_id: str
    reason: str


class TextReview(BaseModel):
    unsupported: list[TextIssue] = Field(description="仅实际无依据或与来源矛盾的段落；已被来源支持的段落不得列入。全部通过返回空数组。")
    warnings: list[str]


class BlockRepair(BaseModel):
    block_id: str
    block: Block | None


class BlockRepairs(BaseModel):
    repairs: list[BlockRepair]


class RevisionPlan(BaseModel):
    start_node: Literal["Router", "Planner", "Writer", "Illustrator", "Assembler"]
    spec: TaskSpec
    reason: str
    primary_color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


def climate_status(document):
    if any(p.category == "notice" and ("气候" in p.label or "天气" in p.label) and p.source_ids
           for p in document.practical):
        return "ok"
    return "assumed" if document.spec.date_assumed else "missing"


def safe_error(error, config):
    # Network exceptions may contain request URLs with credentials. Only expose structured diagnostics.
    if isinstance(error, BaseExceptionGroup):
        return "；".join(dict.fromkeys(safe_error(e, config) for e in error.exceptions))[:400]
    if isinstance(error, ConfigurationError):
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code == 429:
            request = error.request
            if (
                request.url.host == "mcp.exa.ai"
                and not request.headers.get("Authorization")
                and not request.headers.get("x-api-key")
                and not request.url.params.get("exaApiKey")
            ):
                return "Exa 匿名资料服务访问受限（HTTP 429），请配置个人 API key，或等待限额恢复后继续生成"
            try:
                body = error.response.text.casefold()
            except httpx.ResponseNotRead:
                body = ""
            if any(term in body for term in ('usage limit reached', 'insufficient_quota', 'quota exceeded', 'quota_exceeded', '额度')):
                return "外部服务额度已用尽（HTTP 429），请等待额度恢复或补充额度后继续生成"
            if any(term in body for term in ('使用量已超出', '频率限制', 'out of its allowance')):
                reset = re.search(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*(?:UTC\+8|UTC\+08:00)?', error.response.text)
                when = f"，将在 {reset.group(1)}（UTC+8）重置" if reset else ""
                return f"模型账户用量已超出限额（HTTP 429）{when}；可等待重置或切换其他模型后继续生成"
            return "外部服务请求过于频繁（HTTP 429），请稍后再试"
        return f"外部服务返回 HTTP {error.response.status_code}，请检查网络、权限与服务配置"
    if isinstance(error, httpx.TimeoutException):
        return "外部服务请求超时，可重试当前节点"
    if isinstance(error, httpx.TransportError):
        request = getattr(error, "request", None)
        host = request.url.host if request is not None else None
        where = f"（{host}）" if host else ""
        return f"无法连接外部服务{where}，请检查网络或服务配置后重试"
    if isinstance(error, ValidationError):
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or '$'} [{item['type']}]"
            for item in error.errors(include_input=False, include_context=False, include_url=False)[:4]
        )
        message = f"结构化数据校验失败：{error.title}，{details}"
        for secret in (config.llm_api_key, config.siliconflow_api_key):
            if value := secret.get_secret_value():
                message = message.replace(value, "[REDACTED]")
        return message[:400]
    if isinstance(error, (ValueError, MCPToolError)):
        message = str(error)
        for secret in (config.llm_api_key, config.siliconflow_api_key):
            if value := secret.get_secret_value():
                message = message.replace(value, "[REDACTED]")
        # Validation errors can echo input material; keep the details out of persisted public state.
        return (
            "结构化数据校验失败，请查看节点输入或重新生成"
            if "validation error" in message.lower()
            else message[:400]
        )
    return f"{type(error).__name__}：节点执行失败，请检查对应外部服务后重试"


def verifier_sources(sources, names, *, per_poi=6, max_chars=80000):
    """Keep each verification call small: only material naming this batch's POIs."""
    def matched(source, name):
        stem = name.split("（")[0]
        return stem in source.get("title", "") or stem in source.get("text", "")

    ordered = []
    seen = set()
    for name in names:
        candidates = [s for s in sources if matched(s, name)]
        candidates.sort(key=lambda s: {"L1": 0, "L2": 1}.get(s.get("level"), 2))
        for source in candidates[:per_poi]:
            if source["id"] in seen:
                continue
            seen.add(source["id"])
            ordered.append(source)
    if not ordered:
        ordered = sorted(sources, key=lambda s: {"L1": 0, "L2": 1}.get(s.get("level"), 2))
    kept, used = [], 0
    for source in ordered:
        text = source.get("text", "")[:8000]
        if used + len(text) > max_chars:
            continue
        used += len(text)
        kept.append({**source, "text": text})
    return kept


class Harness:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.models = Models(config, store)
        self.mcp = MCPProvider(config, self.models)
        self.search = SearchProvider(self.mcp, store)
        self.fetch = FetchProvider(config, store, self.mcp)
        self.kb = KnowledgeBase(config, self.models)
        self.geo = Geo(config, store, self.models)
        self.osm = OSM(config, store, self.mcp)
        self.photos = Photos(config, store, self.models)
        self.photos.mcp = self.mcp
        self.limit = asyncio.Semaphore(1)

    async def run(self, job_id: str, start="Router"):
        with job_metrics(self.store, job_id, start):
            await self._run(job_id, start)

    async def _run(self, job_id: str, start="Router"):
        async with self.limit:
            job = self.store.get(job_id)
            folder = self.store.root / "jobs" / job_id
            folder.mkdir(parents=True, exist_ok=True)
            state_path = folder / "state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            end = 3 if job.get("kind") == "kb" else len(NODES)
            job.update(status="running", error=None, artifacts={}, run_started_at=now(), run_finished_at=None,
                       planned_nodes=["Revision"] if start == "Revision" else NODES[NODES.index(start):end])
            self.store.save(job)
            if start == "Revision":
                self.store.event(job_id, node="Revision", status="running", phase_start=True,
                                 message=STAGES["Revision"][1], progress=0)
                revision_started = time.monotonic()
                try:
                    plan = await self.models.json(
                        "确定修改所需重跑的最早节点：目的地/任务类型改变用 Router；"
                        "天数、景点、亲子等偏好或新事实研究用 Planner；"
                        "仅现有材料的文字改写用 Writer；只换配图用 Illustrator；"
                        "仅主题颜色用 Assembler 并提供 primary_color。"
                        "其他不能仅靠现有材料完成的修改用 Planner。同时返回修改后的完整 spec，"
                        "未受修改影响的参数沿用原值。",
                        {
                            "spec": state.get("spec"),
                            "outline": state.get("outline"),
                            "instruction": job["revision_instruction"],
                        },
                        RevisionPlan,
                    )
                    start = plan.start_node
                    previous_spec = TaskSpec.model_validate(state["spec"])
                    if (plan.spec.city, plan.spec.type) != (previous_spec.city, previous_spec.type):
                        start = "Router"
                    elif any(
                        getattr(plan.spec, field) != getattr(previous_spec, field)
                        for field in (
                            "days",
                            "travelers",
                            "interests",
                            "budget_level",
                            "season_or_dates",
                            "origin",
                        )
                    ):
                        start = "Planner"
                    state["spec"] = plan.spec.model_dump(mode="json")
                    if state.get("prd_version") not in {"1.1","1.3"}:
                        state["prd_version"] = "1.3"
                        start = "Router" if start == "Router" else "Planner"
                    state["skip_nodes"] = (
                        ["RoutePlanner", "Illustrator"] if start == "Writer" else []
                    )
                    if plan.primary_color:
                        state["primary_color"] = plan.primary_color
                    save_json(state_path, state)
                    self.store.event(
                        job_id, node="Revision", status="completed", message=plan.reason, progress=0,
                        duration_s=round(time.monotonic()-revision_started, 2)
                    )
                    job["planned_nodes"] = ["Revision", *NODES[NODES.index(start):end]]
                    self.store.save(job)
                except asyncio.CancelledError:
                    job.update(status="interrupted", run_finished_at=now(), error="服务停止，重新启动后可从当前节点重试")
                    self.store.save(job)
                    self.store.event(job_id, node="Revision", status="interrupted", message=job["error"],
                                     duration_s=round(time.monotonic()-revision_started, 2), progress=0)
                    raise
                except Exception as error:
                    job.update(
                        status="failed", node="Revision", run_finished_at=now(), error=safe_error(error, self.config)
                    )
                    self.store.save(job)
                    self.store.event(
                        job_id, node="Revision", status="failed", message=job["error"], progress=0,
                        duration_s=round(time.monotonic()-revision_started, 2)
                    )
                    return
            job["reused_nodes"] = state.get("skip_nodes", [])
            self.store.save(job)
            index=NODES.index(start)
            while index < end:
                node = NODES[index]
                reuse_verification = node == 'Verifier' and state.pop('reuse_verified_pois', False)
                if node in state.get("skip_nodes", []) or reuse_verification:
                    if reuse_verification:
                        save_json(state_path, state)
                    self.store.event(
                        job_id,
                        node=node,
                        status="completed",
                        message="实用信息补查，沿用已核实的景点事实" if reuse_verification else "沿用上一版本结果",
                        progress=(index + 1) / len(NODES),
                        duration_s=0,
                    )
                    index+=1
                    continue
                job["node"] = node
                set_metric_stage(node)
                self.store.save(job)
                self.store.event(
                    job_id,
                    node=node,
                    status="running",
                    message=STAGES[node][1],
                    phase_start=True,
                    progress=index / len(NODES),
                )
                before = time.monotonic()
                try:
                    await getattr(self, node.lower())(job, state, folder)
                    save_json(state_path, state)
                    job["spec"] = state.get("spec")
                    job["outline"] = state.get("outline")
                    job["warnings"] = state.get("warnings", [])
                    self.store.save(job)
                    self.store.event(
                        job_id,
                        node=node,
                        status="completed",
                        message="已完成",
                        duration_s=round(time.monotonic() - before, 2),
                        source_count=len(state.get("sources", [])),
                        progress=(index + 1) / len(NODES),
                    )
                except ResearchBacktrack as error:
                    job['planned_nodes'] = [name for name in ['Revision', *NODES]
                                            if name in job.get('planned_nodes', []) or name in NODES[NODES.index('Researcher'):end]]
                    self.store.save(job)
                    self.store.event(job_id,node=node,status='retrying',message=str(error),duration_s=round(time.monotonic()-before,2),progress=2/len(NODES))
                    save_json(state_path,state)
                    index=NODES.index('Researcher')
                    continue
                except asyncio.CancelledError:
                    job.update(status="interrupted", run_finished_at=now(), error="服务停止，重新启动后可从当前节点重试")
                    self.store.save(job)
                    self.store.event(job_id, node=node, status="interrupted", message=job["error"],
                                     duration_s=round(time.monotonic()-before, 2), progress=index / len(NODES))
                    raise
                except Exception as error:
                    job.update(status="failed", run_finished_at=now(), error=safe_error(error, self.config))
                    self.store.save(job)
                    self.store.event(
                        job_id,
                        node=node,
                        status="failed",
                        message=job["error"],
                        duration_s=round(time.monotonic()-before, 2),
                        progress=index / len(NODES),
                    )
                    return
                index+=1
            job.update(status="completed", deliverable=state.get("document"), error=None, run_finished_at=now())
            self.store.save(job)
            self.store.event(
                job_id,
                node="Done",
                status="completed",
                message="资料已入库" if job.get("kind") == "kb" else "产物已生成",
                progress=1,
            )

    async def router(self, job, state, folder):
        spec = await self.models.json(
            "从用户需求抽取任务参数；识别单城市。攻略默认三天两晚。"
            "未指定城市时拒绝编造；将需用户补充目的地写入 assumptions，city 写 未指定。"
            "天数、预算、人数、偏好等缺失值用默认并在 assumptions 声明。修改要求优先于原 options。",
            {"prompt": job["prompt"], "options": job["options"]},
            TaskSpec,
        )
        if spec.city == "未指定":
            raise ValueError("未能从需求中识别目的地城市，请补充明确的城市名称后重试")
        options = job.get("options", {})
        explicit_date = None
        for date_input in (job.get("revision_instruction"), options.get("start_date"),
                           options.get("season_or_dates"), job["prompt"]):
            explicit_date = re.search(r'(20\d{2})[-年/]([01]?\d)[-月/]([0-3]?\d)', str(date_input or ""))
            if explicit_date:
                break
        if spec.type=='travel_guide' and explicit_date:
            from datetime import date
            spec.start_date=date(*map(int,explicit_date.groups()))
            spec.season_or_dates=spec.start_date.isoformat()
            spec.date_assumed=False
        if spec.type=='travel_guide' and not explicit_date:
            spec.start_date=None
            if re.search(r'20\d{2}[-年/][01]?\d[-月/][0-3]?\d',spec.season_or_dates):
                spec.season_or_dates='未指定'
        primary_color = state.get("primary_color")
        state.clear()
        if primary_color:
            state["primary_color"] = primary_color
        state["spec"] = spec.model_dump(mode="json")
        state["warnings"] = []
        state["prd_version"] = "1.3"

    async def planner(self, job, state, folder):
        state.pop("culture_research_targets", None)
        spec = TaskSpec.model_validate(state["spec"])
        schema = Outline
        limits = ""
        if spec.type == "travel_guide":
            maximum = spec.days * 3
            schema = create_model(
                "TravelOutline",
                __base__=Outline,
                poi_names=(list[str], Field(min_length=spec.days * 2, max_length=maximum)),
                queries=(list[str], Field(min_length=1, max_length=min(16, maximum))),
            )
            limits = (
                f"本次只有 {spec.days} 天，poi_names 总计最多 {maximum} 个，"
                "只选实际行程景点，不列备选池；检索问题合并同景点字段，避免重复。"
            )
        elif spec.type == "culture_intro":
            schema = create_model(
                "CultureOutline",
                __base__=Outline,
                poi_names=(list[str], Field(min_length=1, max_length=6)),
                queries=(list[str], Field(min_length=1, max_length=12)),
            )
            limits = "本次文旅介绍最多选择6个最具代表性的目的地，优先官方资料齐全的地点；其余地点放入备选。"
        outline = await self.models.json(
            "规划研究大纲，不输出事实。旅游攻略每天以3个景点为主；"
            "poi_names每项必须是单独景点、单独馆区或明确入口，不能用与、斜线、括号把多个地点合并；"
            "长堤、滨江步道、步行街等线状景观必须指定一个有正式名称的入口或观景点，不能用整段的代表坐标代替；无法明确则改选其他地点。"
            "优先官方资料齐全且交通相近的地点，缺少实用信息的景点放入备选而非主线。"
            "文旅介绍至少5个文化主题，覆盖历史脉络、文化渊源、价值、今日体验。"
            "queries 是官网、政府或维基百科检索问题，应覆盖票价、时间、预约、历史。" + limits,
            {
                "spec": state["spec"],
                "prompt": job["prompt"],
                "revision_instruction": job.get("revision_instruction"),
            },
            schema,
        )
        state["outline"] = outline.model_dump()
        if state.get("prd_version") == "1.3" and spec.type == "travel_guide":
            from backend.harness.candidates import discover
            from backend.tools.planning import scheduling_date
            scheduling_date(spec)
            state['spec']=spec.model_dump(mode='json')
            state['candidate_funnel'] = await discover(self, spec, state['outline'], folder)
            state['candidate_sources'] = state['candidate_funnel'].get('sources', [])
            state['outline']['poi_names'] = state['candidate_funnel']['shortlist']
            state['outline']['queries'] = [f'{spec.city} "{name}" 官网 参观须知 开放 预约 历史 特色展陈 交通指南' for name in state['outline']['poi_names']][:12]
            state['outline']['queries'] += [f'{spec.city} site:gov.cn 必游景点 文化 历史',f'{spec.city} site:gov.cn {spec.start_date.month}月 气候 平均气温',f'{spec.city} site:gov.cn 地方特色饮食 餐饮街区',f'{spec.city} site:gov.cn 机场 火车站 到市区 地铁 交通指南']
            self.store.event(job['id'],node='Planner',status='running',message=f"候选池{len(state['candidate_funnel']['pool'])}个，深度核验{len(state['outline']['poi_names'])}个",progress=1/len(NODES))

    async def researcher(self, job, state, folder):
        sources, seen, warnings = [], set(), state["warnings"]
        for raw_source in (state.get('sources',[]) if state.get('research_replenishment') or state.get('context_replenishment') or state.get('food_replenishment') else state.get('candidate_sources', [])):
            source=SourceRef.model_validate(raw_source)
            source.id=f'S{len(sources)+1}'
            sources.append(source)
            seen.add(normalize_url(str(source.url)))
        city = state["spec"]["city"]
        if state.get('prd_version')=='1.3' and state['spec']['type']=='travel_guide' and state['spec'].get('origin') in {'未指定','','市区中心位置'}:
            from backend.tools.planning_v13 import origin_point
            spec=TaskSpec.model_validate(state['spec'])
            await origin_point(self.geo,spec)
            state['spec']=spec.model_dump(mode='json')
        queries = state["outline"]["queries"]
        signature = hashlib.sha256(json.dumps(
            {"spec": state["spec"], "queries": queries,
             "sources": [s.model_dump(mode="json") for s in sources]},
            ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
        progress_path = folder / "research-progress.json"
        completed = 0
        if progress_path.exists():
            saved = json.loads(progress_path.read_text())
            if saved.get("signature") == signature:
                sources = [SourceRef.model_validate(s) for s in saved["sources"]]
                seen = {normalize_url(str(s.url)) for s in sources}
                completed = saved["completed"]
        search_limit = asyncio.Semaphore(self.config.research_concurrency)

        async def discover(query_index, query):
            async with search_limit:
                self.store.event(job["id"], node="Researcher", status="running", message=f"检索 {query_index}/{len(queries)}：{query}", progress=2/len(NODES))
                try:
                    raw = await self.search.search(query)
                except Exception as error:
                    if not search_unavailable(error):
                        raise
                    return error
                results = await self.models.json(
                    "仅从给定搜索返回结果提取真实网址和标题，禁止猜测或补全 URL。"
                    "为每条设置占位 id、level=L3、fetched_at=''、text=''；代码会重新核定来源。",
                    {"query": query, "results": raw[:35000]},
                    SearchResults,
                )
                return raw, results

        async with asyncio.TaskGroup() as group:
            pending = queries[completed:]
            searches = [group.create_task(discover(i, query))
                        for i, query in enumerate(pending, completed + 1)]
            for query, search_task in zip(pending, searches, strict=True):
                cached = await self.kb.search(city, query)
                cached_usable = False
                for hit in cached:
                    url = normalize_url(hit["source_url"])
                    if url in seen:
                        cached_usable = True
                        continue
                    text = self.store.cached(self.store.key("fetch", str(hit["source_url"])))
                    if text is None or damaged_text(text):
                        text = hit["text"]
                    elif missing_time_cards(text):
                        try:
                            text = await self.fetch.fetch(str(hit["source_url"]))
                        except (ValueError, httpx.HTTPError) as error:
                            warnings.append(f"时间卡片未能恢复（{type(error).__name__}），保留原文且不推断开放时间。")
                    if blocked_page(text):
                        warnings.append("旧资料返回验证页面，未用作事实依据。")
                        continue
                    if not text.strip():
                        continue
                    cached_usable = True
                    seen.add(url)
                    sources.append(
                        SourceRef(
                            id=f"S{len(sources) + 1}",
                            title=page_title(text, url),
                            url=url,
                            level=hit["source_level"],
                            fetched_at=hit["fetched_at"],
                            text=text,
                        )
                    )
                outcome = await search_task
                if isinstance(outcome, Exception):
                    if not cached_usable:
                        raise outcome
                    warnings.append(
                        f"外部搜索未完成（{safe_error(outcome, self.config)}），"
                        f"本查询使用知识库资料：{query}"
                    )
                    raw, candidates = "", []
                else:
                    raw, results = outcome
                    candidates = results.sources
                async def read(result, url):
                    try:
                        self.store.event(job["id"], node="Researcher", status="running", message=f"读取网页：{result.title}", progress=2/len(NODES))
                        text = await self.fetch.fetch(url)
                        return result, url, text, None
                    except (ValueError, httpx.HTTPError) as error:
                        return result, url, None, f"部分检索网页不可抓取（{type(error).__name__}），未用作事实依据。"

                async with asyncio.TaskGroup() as pages:
                    reads = [pages.create_task(read(result, url)) for result, url in candidate_pages(candidates, raw, seen, 5)]
                for task in reads:
                    result, url, text, warning = task.result()
                    if warning:
                        warnings.append(warning)
                    if text is None:
                        continue
                    seen.add(url)
                    source = SourceRef(
                        id=f"S{len(sources) + 1}",
                        title=page_title(text, url, result.title),
                        url=url,
                        level=source_level(url),
                        fetched_at=self.store.cache_created_at(self.store.key("fetch",url)) or now(),
                        text=text,
                    )
                    self.store.event(job["id"], node="Researcher", status="running", message=f"保存与索引资料：{source.title}", progress=2/len(NODES))
                    await self.kb.ingest(city, "", source)
                    sources.append(source)
                completed += 1
                save_json(progress_path, {
                    "signature": signature, "completed": completed,
                    "sources": [s.model_dump(mode="json") for s in sources],
                })
        if not sources:
            raise ValueError("未获得可回查的网页正文，不能继续生成；请检查搜索与抓取配置")
        state["sources"] = [s.model_dump(mode="json") for s in sources]
        (folder / "research.json").write_text(
            json.dumps(state["sources"], ensure_ascii=False, indent=2)
        )

    async def verifier(self, job, state, folder):
        names = state.get("outline", {}).get("poi_names") or []
        previous_pois = {}
        previous = None
        targets = set(state.get("culture_research_targets", []))
        if targets and state.get("document"):
            candidate = Deliverable.model_validate(state["document"])
            current_sources = {s["id"]: (s["url"], s["text"], s["level"]) for s in state["sources"]}
            if (
                candidate.spec == TaskSpec.model_validate(state["spec"])
                and [p.name for p in candidate.pois] == names
                and all(
                    current_sources.get(s.id) == (str(s.url), s.text, s.level)
                    for s in candidate.sources
                )
            ):
                previous = candidate
                previous_pois = {
                    p.name: p for p in previous.pois
                    if p.name not in targets and all(
                        (date.today() - f.verified_at).days <= 90
                        for f in p.facts if f.field in SENSITIVE
                    )
                }
        instruction = (
            "仅从提供的网页正文抽取候选 POI 事实。每项事实必须 source_ids 引用材料编号，"
            "只抽取明确属于spec.city和目标场馆的事实，不得套用其他城市的同名景点、其他馆区或入口。"
            "evidence 必须是原文中可精确找到的连续引文。票价必须保留优惠、季节和另收费限制。"
            "已找到来源的每条事实，value 与 evidence 都逐字复制同一个连续原文片段；"
            "不得合并其他位置的信息，不得改写或换算数字。一条只记录一个事实，可用多条记录。"
            "只返回 outline.poi_names 指定的景点，沿用原名称，不增加其他景点或拆分场馆。"
            "票价 ticket、时间 hours、预约 booking、地址 address 只可引用 L1/L2。"
            "无法核实的字段 value 必须为 暂未核实，请以官方渠道为准，source_ids=[]。"
            "历史与文化要标注时间、人物、事件、影响。不要生成图片、正文、坐标。"
            "每个景点至少包含 ticket/hours/booking/address 四字段。今日核验日期：" + now()[:10]
        )

        async def extract(batch, *, compact=False):
            outline = {**state.get("outline", {})}
            if batch:
                outline["poi_names"] = batch
            return await self.models.json(
                instruction + (
                    " 每个字段仅取最相关的短引文，文化亮点最多4条；只输出景点名称和 facts，省略其他字段。"
                    if compact else ""
                ),
                {
                    "spec": state["spec"],
                    "outline": outline,
                    "sources": verifier_sources(
                        state["sources"], batch, max_chars=16000 if compact else 80000
                    ),
                },
                FactSet,
            )

        async def extract_split(batch):
            # Reasoning models spend part of the completion budget on reasoning and can
            # return empty or truncated JSON. Halve the batch instead of failing the node.
            try:
                return (await extract(batch)).pois
            except ValueError as error:
                if len(batch) == 1:
                    if not isinstance(error, IncompleteModelOutput):
                        raise
                    return (await extract(batch, compact=True)).pois
                if not batch:
                    raise
                middle = len(batch) // 2
                return await extract_split(batch[:middle]) + await extract_split(batch[middle:])

        batches = [(i, names[i:i + 3]) for i in range(0, len(names), 3)] or [(0, names)]
        progress_path = folder / "verification-progress.json"
        signature = hashlib.sha256(json.dumps(
            {"spec": state["spec"], "names": names,
             "sources": [(s["id"], s["url"], s["level"], s["text"]) for s in state["sources"]]},
            ensure_ascii=False, sort_keys=True,
        ).encode()).hexdigest()
        done = {}
        if progress_path.exists():
            saved = json.loads(progress_path.read_text())
            if saved.get("signature") == signature:
                done = saved.get("pois", {})
        extracted = []
        if not names:
            extracted = list((await extract([])).pois)
        for batch_index, (offset, batch) in enumerate(batches, 1):
            if not batch:
                continue
            missing = [
                (offset + i, name) for i, name in enumerate(batch)
                if str(offset + i) not in done and name not in previous_pois
            ]
            if missing:
                if job.get("id"):
                    self.store.event(
                        job["id"], node="Verifier", status="running",
                        message=f"核验批次 {batch_index}/{len(batches)}：{'、'.join(name for _, name in missing)}",
                    )
                pois = await extract_split([name for _, name in missing])
                for (position, _), poi in zip(missing, pois, strict=True):
                    done[str(position)] = poi.model_dump(mode="json")
                save_json(progress_path, {"signature": signature, "pois": done})
            for i, name in enumerate(batch):
                extracted.append(
                    previous_pois[name] if name in previous_pois
                    else POI.model_validate(done[str(offset + i)])
                )
        facts = FactSet(pois=extracted)
        save_json(folder / "verification-candidates.json", facts.model_dump(mode="json"))
        source_levels = {source["id"]: source["level"] for source in state["sources"]}
        for index, poi in enumerate(facts.pois):
            if poi.name in previous_pois:
                continue
            poi.id = f"P{index + 1}"
            poi.lat = poi.lng = None
            poi.images = []
            poi.official_transit = []
            poi.dining = []
            poi.nationally_rated = False
            poi.blocks = []
            fields = {f.field for f in poi.facts}
            for field in sorted(SENSITIVE - fields):
                poi.facts.append(FactRecord(field=field, value="暂未核实，请以官方渠道为准"))
            for fact in poi.facts:
                fact.verified_at = __import__("datetime").date.today()
                if fact.field in SENSITIVE and any(
                    source_levels.get(source_id) == "L3" for source_id in fact.source_ids
                ):
                    fact.value = "暂未核实，请以官方渠道为准"
                    fact.source_ids = []
                    fact.evidence = ""
                    fact.confidence = 0
                    state["warnings"].append(
                        f"{poi.name} / {fact.field}：候选资料等级不足，未作为已核实事实发布"
                    )
                if fact.source_ids:
                    by_id = {s["id"]: s for s in state["sources"]}
                    import re

                    from backend.harness.reviewer import evidence_text
                    quote = evidence_text(fact.evidence)
                    valid = quote and any(r in by_id and quote in evidence_text(by_id[r]["text"]) for r in fact.source_ids)
                    if not valid or any(r not in by_id for r in fact.source_ids):
                        fact.value, fact.source_ids, fact.evidence = "暂未核实，请以官方渠道为准", [], ""
                        state["warnings"].append(f"{poi.name}/{fact.field} 候选引文无法回查，等待补充核验")
                    elif any(n not in re.findall(r"\d+(?:\.\d+)?", fact.evidence) for n in re.findall(r"\d+(?:\.\d+)?", fact.value)):
                        fact.value = fact.evidence
                        state["warnings"].append(f"{poi.name}/{fact.field} 候选扩写超出证据，字段改用引文并补充核验")
        document = Deliverable(
            spec=TaskSpec.model_validate(state["spec"]),
            pois=facts.pois,
            sources=[SourceRef.model_validate(s) for s in state["sources"]],
        )
        document.prd_version = state.get('prd_version', '1.0')
        if document.prd_version == '1.3' and state.get('candidate_funnel'):
            from backend.harness.candidates import key_name
            pool = [POI.model_validate(x) for x in state['candidate_funnel']['pool']]
            allowed={key_name(name) for name in state['outline']['poi_names']}
            document.pois=[p for p in document.pois if key_name(p.name) in allowed]
            for candidate in pool:
                if candidate.name not in state['outline']['poi_names']:
                    continue
                poi = next((p for p in document.pois if key_name(p.name)==key_name(candidate.name)), None)
                if poi is None:
                    poi = candidate.model_copy(deep=True)
                    poi.id = f'P{len(document.pois)+1}'
                    document.pois.append(poi)
                for field in ['lat','lng','location_source','location_verified','location_note','category','aliases','wikidata','osm_tags','planning_priority']:
                    setattr(poi,field,getattr(candidate,field))
                if candidate.official_url:
                    poi.official_url = candidate.official_url
            document.candidate_count = len(pool)
        validate_facts(document)
        if state.get("prd_version") in {"1.1", "1.3"}:
            if previous_pois:
                document.practical = [
                    item for item in previous.practical
                    if any(item.label == f"{name} · 参观提示" for name in previous_pois)
                ]
                document = await enrich(self, document, folder, verified_names=previous_pois.keys())
                if job.get("id"):
                    self.store.event(job["id"], node="Verifier", status="running",
                                     message=f"沿用 {len(previous_pois)} 个地点的核验结果")
            else:
                document = await enrich(self, document, folder)
        validate_facts(document)
        eligible = []
        for poi in document.pois:
            missing = sum([not poi.blocks, poi.hours is None, not any(f.field == "address" and f.source_ids for f in poi.facts)])
            if missing >= 2 and state.get("prd_version") != "1.3":
                document.alternatives.append({"name": poi.name, "reason": "简介、开放时间或地址资料不足，未纳入主线。"})
            else:
                eligible.append(poi)
        if state.get("prd_version") == "1.1":
            document.pois = eligible
        document.prd_version = state.get("prd_version", "1.0")
        state["sources"] = [s.model_dump(mode="json") for s in document.sources]
        state["document"] = document.model_dump(mode="json")
        state.pop("culture_research_targets", None)

    async def writer(self, job, state, folder):
        document = Deliverable.model_validate(state["document"])
        dedicated_context = document.prd_version == '1.3' and document.spec.type == 'travel_guide'
        if dedicated_context:
            present={s.id for s in document.sources}
            document.sources.extend(SourceRef.model_validate(s) for s in state.get('sources',[]) if s['id'] not in present)
            require_visit_coverage(state, document)
            from backend.harness.visit_context import apply_exhibition_notices
            apply_exhibition_notices(document)
        allowed_sources={s['id'] for s in state['sources'] if document.prd_version!='1.3' or s['level'] in {'L1','L2'}}
        fact_payload=[p.model_dump(mode='json') for p in document.pois]
        if document.prd_version=='1.3':
            fact_payload=[{'id':p.id,'name':p.name,'facts':[{**f.model_dump(mode='json'),'source_ids':[ref for ref in f.source_ids if ref in allowed_sources]} for f in p.facts if set(f.source_ids)&allowed_sources]} for p in document.pois]
        if job.get('revision_instruction'):
            for payload, poi in zip(fact_payload, document.pois, strict=True):
                payload['existing_blocks'] = [b.model_dump(mode='json') for b in poi.blocks]
        visit_notices = [
            p.model_dump(mode="json") for p in document.practical if p.category == "notice"
        ]
        instruction = (
            "写中文文旅正文，基于FactRecord简洁改写，不整段照抄或保留来源口语，不用失去指代的'它'开头。"
            "每个景点blocks至少4条不同的具体亮点，其中至少3条带L1/L2来源且属于展陈、馆藏、建筑、历史或文化价值；门票、开放、预约、地址、交通不计入这3条文化亮点，也不要在亮点重复参观信息。建议必须具体且有依据；每条一句、不超过80字；同一事实只写一次，文化内容不超过攻略正文的30%。"
            "馆藏总量或其他馆区的陈列不能套用到目标馆区；不足的亮点用具体游览建议，不用泛泛套话。"
            "必须按参观日期和目标馆区落实当期展览限制；独占专展期间不得把其他馆区常设展当作本次体验。"
            "提供existing_blocks时，按修改要求改写原稿，保留其有来源的不同事实与引用，不得缩减为重复事实或通用建议。"
            "visit_notices中的当期展览限制须写入对应景点亮点，同段保留展名、限制和该通知的source_ids。"
            "人物年龄必须与出生年、事件年份自洽；没有两个独立来源支持时不写具体年龄数字。"
            "正文不得出现本案例、样例、交付形式要求、MCP、内部编号、模型或校验过程。"
            "所有事实句必须引用 source_ids，数字、专名、年代必须在引用正文中有据。"
            "不得使用知识记忆补写。pois 只填写给定 id/name 与 blocks，其余字段保留默认。"
            "门票、开放时间、预约和地址仅可使用已核实的 FactRecord；"
            "标为暂未核实的字段不得从 L3 正文中补写。"
            "攻略 chapters 补充有依据的交通/住宿/美食/预算/注意事项；无依据不编写。"
            "文旅介绍至少5章，每章明确是什么、历史渊源、文化价值和今日体验，包含有依据的年代与事件。"
            "包含一章按年代展开的历史脉络。无法写有来源的内容时用空 blocks，不要伪造引用。"
            "block.kind=fact 表示可核查事实，必须有引用；"
            "今日体验中的主观游览建议单独成段，kind=advice，明确写建议，source_ids 可为空。"
            "建议中不得夹带无来源的年代、景点开放、票价、地址或历史断言。"
        )
        payload = {
            "spec": state["spec"],
            "revision_instruction": job.get("revision_instruction"),
            "outline": state["outline"],
            "facts": fact_payload,
            "visit_notices": visit_notices,
            "sources": [
                {**s, "text": s["text"][:16000]}
                for s in state["sources"] if s["id"] in allowed_sources
            ],
        }
        if document.prd_version == "1.3":
            writing = Writing(pois=[], chapters=[])
            notice_refs = {ref for notice in visit_notices for ref in notice["source_ids"]}
            guide_instruction = (
                "只写 facts 中的景点亮点，按给定 id、name 返回 blocks。用 FactRecord 和引用原文，"
                "每处写至少4条不重复的具体亮点，每条一句、不超过80字；"
                "至少3条应讲展陈、馆藏、建筑、历史或文化价值，且必须有L1/L2来源。"
                "门票、开放、预约、地址和交通不算亮点。证据不足时可写具体游览建议（kind=advice），"
                "不要在建议中加入事实；无可用内容时留空。事实用 kind=fact 并给 source_ids。"
                "有 visit_notices 时保留对应馆区的当期展名、限制和来源；不要套用其他馆区陈列。"
                "人物年龄缺少两个独立来源时省略。改稿时保留 existing_blocks 中有据且不重复的事实。"
                "不猜测、不写内部术语，直接返回 JSON。"
            )
            for offset in range(0, len(fact_payload), 2):
                batch = fact_payload[offset:offset + 2]
                cited = notice_refs | {
                    ref for poi in batch
                    for item in [*poi["facts"], *poi.get("existing_blocks", [])]
                    for ref in item["source_ids"]
                }
                result = await self.models.json(
                    guide_instruction,
                    {
                        **payload,
                        "outline": {"poi_names": [p["name"] for p in batch]},
                        "facts": batch,
                        "sources": [s for s in payload["sources"] if s["id"] in cited],
                    },
                    GuideWriting,
                )
                by_id = {poi.id: poi for poi in result.pois}
                writing.pois.extend(
                    POI.model_validate(by_id[p["id"]].model_dump())
                    for p in batch if p["id"] in by_id
                )
            if not dedicated_context:
                chapter_sources = verifier_sources(payload["sources"], [], max_chars=48000)
                chapters = await self.models.json(
                    "按大纲写至少5章中文文旅介绍，涵盖历史脉络、文化渊源、价值和今日体验。"
                    "至少一章按年代展开。每个事实段落引用提供的来源，数字、年代、人物必须可回查。"
                    "主观游览建议单独成段并标为 advice。无证据的内容省略。"
                    "每章聚焦2至3条有据的内容，避免重复景点亮点，不生成图片。",
                    {
                        "spec": payload["spec"],
                        "outline": payload["outline"],
                        "sources": chapter_sources,
                        "revision_instruction": payload["revision_instruction"],
                        "existing_chapters": [
                            c.model_dump(mode="json", exclude={"images"}) for c in document.chapters
                        ] if job.get("revision_instruction") else [],
                    },
                    CultureWriting,
                )
                writing.chapters = chapters.chapters
                writing.practical = chapters.practical
        else:
            writing = await self.models.json(instruction, payload, Writing)
        save_json(folder/'writing-candidates.json',writing.model_dump(mode='json'))
        by_id = {p.id: p for p in writing.pois}
        for poi in document.pois:
            if poi.id in by_id:
                poi.blocks = by_id[poi.id].blocks or poi.blocks
        if document.prd_version == '1.3' and document.spec.type == 'culture_intro':
            incomplete = [p.name for p in document.pois if
                          len(p.blocks) < 4 or sum(b.kind == 'fact' for b in p.blocks) < 3 or
                          sum([not any(b.kind == 'fact' for b in p.blocks), p.hours is None,
                               not any(f.field == 'address' and f.source_ids and f.value != '暂未核实，请以官方渠道为准' for f in p.facts)]) >= 2]
            if incomplete:
                if state.get('research_replenishment', 0) >= 2:
                    supported = [p for p in document.pois if p.name not in incomplete]
                    requested = any(
                        name in job.get('prompt', '')
                        for p in document.pois if p.name in incomplete
                        for name in [p.name, *p.aliases]
                    )
                    if not supported or requested or job.get('revision_instruction'):
                        raise ValueError('两轮补查后文化景点资料仍不足：' + '、'.join(incomplete))
                    document.alternatives.extend(
                        {'name':name, 'reason':'参观资料或文化亮点依据尚不完整，未编入详细参观专题。'}
                        for name in incomplete
                    )
                    document.pois = supported
                else:
                    state['research_replenishment'] = state.get('research_replenishment', 0) + 1
                    state['culture_research_targets'] = incomplete
                    state['document'] = document.model_dump(mode='json')
                    state['outline']['queries'] = [
                        f'{document.spec.city} {name} 官方 历史沿革 建筑 馆藏 文化价值 开放时间 地址'
                        for name in incomplete
                    ]
                    raise ResearchBacktrack('文化景点资料不足，回退Researcher定向补查：' + '、'.join(incomplete))
        updated = {p.id: p for p in document.pois}
        for day in document.days:
            day.stops = [updated[p.id] for p in day.stops]
        images = {c.title: c.images for c in document.chapters}
        document.chapters = [] if dedicated_context else writing.chapters
        if not dedicated_context:
            document.practical.extend(item for item in writing.practical if item not in document.practical)
        for chapter in document.chapters:
            chapter.images = images.get(chapter.title, [])
        if document.spec.type == "culture_intro" and len(document.chapters) < 5:
            raise ValueError("有据可查的文化主题不足五个，需要补充研究材料")
        if not any(p.blocks for p in document.pois) and not document.chapters:
            raise ValueError("没有可交付的有来源正文")
        validate_facts(document)
        if dedicated_context:
            from backend.harness.visit_context import enrich_context
            document=await enrich_context(self,document,folder)
            state['document'] = document.model_dump(mode='json')
            state['sources'] = [s.model_dump(mode='json') for s in document.sources]
            source_index={s.id:s for s in document.sources}
            if not any(p.category=='food' and any(r in source_index and source_index[r].level in {'L1','L2'} and 'openstreetmap.org' not in str(source_index[r].url) for r in p.source_ids) for p in document.practical):
                if state.get('food_replenishment',0)>=2:
                    raise ValueError('两轮补查后仍缺少L1或L2来源的本地特色饮食')
                state['food_replenishment']=state.get('food_replenishment',0)+1
                state['reuse_verified_pois'] = True
                state['outline']['queries']=[f'{document.spec.city} 本地特色饮食 菜系 小吃 site:gov.cn']
                raise ResearchBacktrack('缺少本地特色饮食资料，回退Researcher补查官方介绍')
            climate = climate_status(document)
            if climate == 'assumed':
                note = '未指定出行日期，当前按假设日期安排，未纳入具体月份气候概况；确定日期后请核实天气。'
                if note not in document.spec.assumptions:
                    document.spec.assumptions.append(note)
            elif climate == 'missing':
                if state.get('context_replenishment',0)>=2:
                    raise ValueError('两轮补查后仍缺少出行月份的有来源气候概况，不能复用往年天气预报')
                state['context_replenishment']=state.get('context_replenishment',0)+1
                state['reuse_verified_pois'] = True
                month=document.spec.start_date.month
                state['outline']['queries']=[f'{document.spec.city} {month}月 常年 月平均气温 气候概况 site:gov.cn',f'{document.spec.city} {month}月 多年平均 气候 site:cma.gov.cn']
                raise ResearchBacktrack('缺少月份气候资料，回退Researcher补查常年数据')
        if document.prd_version in {"1.1", "1.3"}:
            document = prepare(document)
        state["document"] = document.model_dump(mode="json")

    async def routeplanner(self, job, state, folder):
        document = Deliverable.model_validate(state["document"])
        if document.spec.type == "travel_guide":
            def report(message):
                self.store.event(job["id"], node="RoutePlanner", status="running", message=message, progress=5/len(NODES))
            if document.prd_version == '1.3':
                from backend.tools.planning_v13 import CandidateShortage, MealVenueShortage
                require_visit_coverage(state, document)
                try:
                    document.days, alternatives, document.overview_basemap = await plan_itinerary(self.geo,document.spec,document.pois,document.sources,self.osm,folder,report=report,v13=True,origin_hints=document.origin_hints,provider_report=lambda data:self.store.event(job['id'],node='RoutePlanner',status='running',kind='provider',provider=data,message='交通Provider：'+str(data)))
                    if sum(a.get('verified') is True for a in alternatives) < 3:
                        raise CandidateShortage('可执行替换景点不足3个，需要补充临近候选')
                except MealVenueShortage as error:
                    raise ValueError(str(error) + '；请补充景点500米内可定位的餐饮资料后重试，当前未发布不合格行程。') from error
                except CandidateShortage as error:
                    pool=state['candidate_funnel']['pool']
                    # Only located candidates can become a routable stop. Re-researching
                    # names without coordinates burns a full research round and still
                    # cannot satisfy the itinerary constraints.
                    additions=[p['name'] for p in pool
                               if p['name'] not in state['outline']['poi_names'] and p.get('lat') is not None][:document.spec.days*3]
                    if state.get('research_replenishment',0)>=2 or not additions:
                        located=[p for p in document.pois if p.lat is not None and p.lng is not None]
                        raise ValueError(
                            f'{error}；本次{len(document.pois)}个景点中可定位{len(located)}个，'
                            f'每天至少需要3个彼此40分钟内可达的景点。'
                            '请检查地图服务（Overpass/Nominatim）后重试当前节点'
                        ) from error
                    state['outline']['poi_names'].extend(additions)
                    state['outline']['queries']=[f'{document.spec.city} "{name}" site:gov.cn 开放 交通 历史 文化' for name in additions]
                    state['research_replenishment']=state.get('research_replenishment',0)+1
                    raise ResearchBacktrack('行程或可执行备选不足，回退Researcher补充：'+'、'.join(additions)) from error
            else:
                document.days, alternatives, document.overview_basemap = await plan_itinerary(self.geo, document.spec, document.pois, document.sources, self.osm, folder, report=report)
            if document.prd_version == '1.3':
                document.alternatives = alternatives
            else:
                document.alternatives.extend(alternatives)
            visited = {p.id for day in document.days for p in day.stops}
            document.pois = [p for p in document.pois if p.id in visited]
            if document.prd_version == '1.3':
                from backend.harness.candidates import update_funnel
                update_funnel(state,folder,document)
                self.store.event(job['id'],node='RoutePlanner',status='running',message='候选漏斗：'+str(state['candidate_funnel']['counts']))
                from collections import Counter
                segments=[s for d in document.days for s in ([d.arrival_segment] if d.arrival_segment else [])+d.segments]
                tiers=Counter(s.transit.get('confidence','missing') for s in segments)
                self.store.event(job['id'],node='RoutePlanner',status='running',kind='transport_tiers',tiers=dict(tiers),message='交通层级统计：'+str(dict(tiers)))
                from backend.harness.visit_context import group_budget, normalize_assumptions
                group_budget(document)
                normalize_assumptions(document)
                from backend.tools.transit import transport_label
                document.practical=[p for p in document.practical if not (p.category=='transport' and ' → ' in p.label)]
                for day in document.days:
                    for segment in ([day.arrival_segment] if day.arrival_segment else [])+day.segments:
                        first=next((p for p in [day.origin,*day.stops] if p and p.id==segment.from_poi),None)
                        last=next((p for p in day.stops if p.id==segment.to_poi),None)
                        if not first or not last:
                            continue
                        source=SourceRef(id=f'S{max([int(s.id[1:]) for s in document.sources if re.fullmatch(r"S\d+",s.id)],default=0)+1}',title=segment.from_name+'至'+segment.to_name+' · 路网与站序估算',url=f'https://www.openstreetmap.org/directions?route={first.lat},{first.lng};{last.lat},{last.lng}',level='L2',fetched_at=now(),text=segment.source+'；'+'；'.join(segment.instructions)+'；'+transport_label(segment))
                        document.sources.append(source)
                        document.practical.append(PracticalItem(category='transport',label=segment.from_name+' → '+segment.to_name,value=transport_label(segment)+'；'+segment.transit.get('note',''),source_ids=[*segment.transit.get('source_ids',[]),source.id],basis='地图关系与路网估算，非实时班次'))
                from types import SimpleNamespace
                for poi in document.pois:
                    try:
                        thumbnail=await self.osm.basemap([SimpleNamespace(stops=[poi],segments=[])],folder,'poi-'+poi.id,closeup=True)
                        poi.map_thumbnail=thumbnail['image']
                    except (httpx.HTTPError,ValueError) as error:
                        self.store.event(job['id'],node='RoutePlanner',status='running',message=poi.name+'近景地图不可用：'+type(error).__name__)
            from backend.harness.quality import route_failures
            failures = route_failures(document)
            if failures:
                raise ValueError("行程硬约束未通过：" + "；".join(failures))
            state["document"] = document.model_dump(mode="json")
            state["spec"] = document.spec.model_dump(mode="json")
        state["geojson"] = geojson(document)

    async def illustrator(self, job, state, folder):
        document = Deliverable.model_validate(state["document"])
        mode = job.get("generation_mode", "complete")
        if mode == "fast":
            for poi in document.pois:
                if not poi.images:
                    poi.photo_status = PHOTO_SKIPPED
            updated = {poi.id: poi for poi in document.pois}
            for day in document.days:
                day.stops = [updated[poi.id] for poi in day.stops]
            state["document"] = document.model_dump(mode="json")
            self.store.event(job["id"], node="Illustrator", status="running",
                             message="快速模式跳过配图服务，保留路线图和已有实拍")
            return
        if document.prd_version in {"1.1", "1.3"} or mode == "balanced":
            # Keep selection/deduplication ordered; only overlap the independent cover.
            self.photos.hashes = []

            async def cover():
                self.store.event(job["id"], node="Illustrator", status="running", message="正在生成装饰封面，等待生图服务返回", progress=6/len(NODES))
                with measure("生成装饰封面", "image"):
                    return await generated_image(self.mcp, document.spec.city, "城市风貌装饰封面，仅作艺术装饰，非景点实拍", folder, "illustration-cover")

            async with asyncio.TaskGroup() as group:
                cover_task = (
                    group.create_task(cover())
                    if mode == "complete" and not document.decorations
                    else None
                )
                for poi in document.pois:
                    if poi.images and all(not i.generated and i.relevance_verified and i.subject_verified and i.author and i.license and i.license_url and Path(i.local_path).is_file() for i in poi.images):
                        self.photos.hashes.extend(i.perceptual_hash for i in poi.images if i.perceptual_hash)
                        self.store.event(job['id'],node='Illustrator',status='running',message=poi.name+'：复用已核验授权实拍',cache_hit=True)
                        continue
                    self.store.event(job["id"], node="Illustrator", status="running", message=f"检索授权照片并核验主体：{poi.name}", progress=6/len(NODES))
                    await self.photos.obtain(document.spec.city, poi, folder)
                    state["document"] = document.model_dump(mode="json")
                    save_json(folder / "state.json", state)
                    self.store.event(job["id"], node="Illustrator", status="running", message=f"景点实拍核验：{poi.name} · {poi.photo_status}", progress=6/len(NODES))
            if cover_task:
                document.decorations = [cover_task.result()]
            for chapter in document.chapters:
                chapter.images = []
            updated = {p.id: p for p in document.pois}
            for day in document.days:
                day.stops = [updated[p.id] for p in day.stops]
            state["document"] = document.model_dump(mode="json")
            return
        targets = [(p, p.name) for p in document.pois]
        targets += [(c, c.title) for c in document.chapters]
        limit = asyncio.Semaphore(2)
        completed = 0

        async def illustrate(index, target, subject):
            nonlocal completed
            async with limit:
                image = await generated_image(
                    self.mcp, document.spec.city, subject, folder, f"illustration-{index}"
                )
                target.images = [image]
                completed += 1
                # Persist each completed image so retries do not regenerate or spend again.
                state["document"] = document.model_dump(mode="json")
                save_json(folder / "state.json", state)
                self.store.event(
                    job["id"],
                    node="Illustrator",
                    status="running",
                    message=f"AI 插画 {completed}/{len(targets)}：{subject}",
                    progress=6 / len(NODES),
                )

        results = await asyncio.gather(
            *(illustrate(i, target, subject) for i, (target, subject) in enumerate(targets)),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        previous_image_warnings = {
            document.spec.city + " " + subject + suffix
            for _, subject in targets
            for suffix in (
                " 未取得可核实授权与相关性的配图，保留说明占位。",
                " 配图只经过元数据文本核验，未完成视觉核验。",
            )
        }
        state["warnings"] = [w for w in state["warnings"] if w not in previous_image_warnings]
        updated = {p.id: p for p in document.pois}
        for day in document.days:
            day.stops = [updated[p.id] for p in day.stops]
        state["warnings"].append(
            "配图为 AI 生成的文化意象插画，非景点实拍，不作为史实或建筑细节依据。"
        )
        state["document"] = document.model_dump(mode="json")

    async def reviewer(self, job, state, folder):
        document = Deliverable.model_validate(state["document"])
        validate_facts(document)
        for attempt in range(4):
            self.store.event(job["id"], node="Reviewer", status="running", message=f"第 {attempt+1} 轮审阅：核对正文与来源，检查数字及未核实断言", progress=7/len(NODES))
            blocks = text_blocks(document)
            cited_ids = {ref for block in blocks.values() for ref in block.source_ids}
            cited_sources = [s for s in document.sources if s.id in cited_ids]
            payload = {
                "spec": state.get("spec", document.spec.model_dump(mode="json")),
                "blocks": {k: b.model_dump() for k, b in blocks.items()},
                "facts": [
                    {"name": p.name, "facts": [f.model_dump(mode="json") for f in p.facts]}
                    for p in document.pois
                ],
                "sources": [s.model_dump(mode="json") for s in cited_sources],
            }
            review_instruction = (
                "逐句检查全部 blocks。每项 unsupported 必须给出实际 block_id 和具体原因。"
                "fact 中的数字、年代、人物、历史和文化断言必须由引用来源支持。"
                "kind=advice 是明确标注的编辑建议，不要求建议原句出现在来源中；"
                "但建议夹带的客观断言同样必须核实。旧稿未标 kind 的纯主观建议可改为 advice。"
                "门票、开放时间、预约、地址不得引用 L3 或绕过未核实 FactRecord。"
                "只报告实际无依据或矛盾的断言，不因合理概述未逐字照抄而拒绝。"
                "已得到支持或无需修改的段落绝不能放入unsupported；全部通过时unsupported必须为空数组。"
                "检查人物出生年、任职年与年龄的算术一致性；矛盾或不足以核对的年龄数字必须删去，不照抄来源错误。"
                "称谓差异、表述风格、建议统一措辞列 warnings，不能列 unsupported。"
                "未核实参观信息列 warnings。AI 插画已标明非实拍，不要求其证明史实。"
            )

            async def audit(batch):
                refs = {ref for block in batch.values() for ref in block["source_ids"]}
                return await self.models.json(
                    review_instruction,
                    {
                        **payload,
                        "blocks": batch,
                        "sources": [s for s in payload["sources"] if s["id"] in refs],
                    },
                    TextReview,
                )

            if document.spec.type == "culture_intro" and len(blocks) > 8:
                review = TextReview(unsupported=[], warnings=[])
                entries = list(payload["blocks"].items())
                for offset in range(0, len(entries), 8):
                    batch_review = await audit(dict(entries[offset:offset + 8]))
                    review.unsupported.extend(batch_review.unsupported)
                    review.warnings.extend(batch_review.warnings)
            else:
                review = await audit(payload["blocks"])
            def contradictory(issues):
                return any(re.search(r"无需列|不应列|不列入|不作为\s*unsupported|均可支持|均可由|原文确实支持|所有断言均可", i.reason) for i in issues)
            if contradictory(review.unsupported):
                save_json(folder / f"review-{attempt+1}-inconsistent.json", review.model_dump())
                review = await self.models.json(
                    "上一轮审阅自相矛盾：把原文明确支持、且理由中写明不应列为unsupported的段落放入了unsupported。"
                    "请依据blocks与sources重新核验，只输出真正缺依据或矛盾的段落；有依据的概括保留，风格提示放warnings。"
                    "不能为了凑数报告问题。全部有依据时unsupported必须为[]。",
                    {**payload, "previous_review":review.model_dump()}, TextReview,
                )
            if document.prd_version in {"1.1", "1.3"}:
                for issue in await age_issues(self.models, blocks, cited_sources):
                    if issue["block_id"] not in {i.block_id for i in review.unsupported}:
                        review.unsupported.append(TextIssue.model_validate(issue))
            if any(issue.block_id not in blocks for issue in review.unsupported):
                raise ValueError("审阅器返回不存在的段落编号，不能安全改写")
            if contradictory(review.unsupported):
                raise ValueError("审阅结论自相矛盾，已阻止误删有来源正文；请重试审阅")
            save_json(folder / f"review-{attempt + 1}.json", review.model_dump())
            if not review.unsupported:
                break
            if attempt == 3:
                raise ValueError(
                    "修正与删减后仍有无法溯源的事实，已阻止发布："
                    + "；".join(i.reason for i in review.unsupported)[:250]
                )
            if attempt == 2:
                if len(review.unsupported) > max(3, len(blocks) // 4):
                    raise ValueError("审阅拟批量删除大量正文，已阻止自动出件，请补充研究")
                # Omit only the still-unsupported paragraphs, then audit the remaining text again.
                repairs = BlockRepairs(
                    repairs=[
                        BlockRepair(block_id=i.block_id, block=None) for i in review.unsupported
                    ]
                )
                state["warnings"].extend(
                    f"已省略无法核实的段落 {i.block_id}：{i.reason}" for i in review.unsupported
                )
            else:
                self.store.event(job["id"], node="Reviewer", status="running", message=f"正在修正 {len(review.unsupported)} 处需核对的正文", progress=7/len(NODES))
                repairs = await self.models.json(
                    "仅修正 issues 指出的段落，不改其他段落。每个 block_id 返回一个 repair。"
                    "保留有来源的事实，删除无依据的具体历史断言，不以模糊措辞包装错误。"
                    "原文自身存在朝代、年代或人物矛盾时，不照搬错误，删除矛盾断言。"
                    "纯游览建议改为 kind=advice 且明确写建议；不要夹带未核实事实。"
                    "如果整段没有可保留的依据且不是合理建议，block 返回 null 删除。",
                    {**payload, "blocks": {i.block_id: payload["blocks"][i.block_id] for i in review.unsupported}, "issues": [i.model_dump() for i in review.unsupported]},
                    BlockRepairs,
                )
            save_json(folder / f"repairs-{attempt + 1}.json", repairs.model_dump())
            pending = {i.block_id for i in review.unsupported}
            fixes = {r.block_id: r.block for r in repairs.repairs}
            if len(fixes) != len(repairs.repairs) or set(fixes) != pending:
                raise ValueError("修正范围与审阅问题不一致，不能安全修改正文")
            for poi in document.pois:
                poi.blocks = [fixes.get(f"poi:{poi.id}:{i}", b) for i, b in enumerate(poi.blocks)]
                poi.blocks = [b for b in poi.blocks if b is not None]
            for i, chapter in enumerate(document.chapters):
                chapter.blocks = [
                    fixes.get(f"chapter:{i}:{j}", b) for j, b in enumerate(chapter.blocks)
                ]
                chapter.blocks = [b for b in chapter.blocks if b is not None]
            if (
                document.spec.type == "culture_intro"
                and sum(bool(c.blocks) for c in document.chapters) < 5
            ):
                raise ValueError("修正后有来源的文化主题不足五个，需要补充研究")
            validate_facts(document)
            state["warnings"].extend(
                f"审阅修正 {i.block_id}：{i.reason}" for i in review.unsupported
            )
            state["document"] = document.model_dump(mode="json")
            save_json(folder / "state.json", state)
        state["warnings"].extend(review.warnings)
        document.warnings = list(dict.fromkeys(state["warnings"]))
        if document.prd_version in {"1.1", "1.3"}:
            document = prepare(document)
        state["document"] = document.model_dump(mode="json")

    async def assembler(self, job, state, folder):
        document = Deliverable.model_validate(state["document"])
        if document.prd_version in {"1.1", "1.3"}:
            document = prepare(document)
        trim_low_verification_culture_pois(document)
        state["document"] = document.model_dump(mode="json")
        require_quality(document, require_maps=False)
        if not available():
            raise ValueError("缺少 Office 页面转换工具，未发布未经转换检查的产物")
        staging = folder / "delivery-staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()
        self.store.event(job["id"], node="Assembler", status="running", message="排版PPT、Word与路线图", progress=8/len(NODES))
        result = await asyncio.to_thread(
            render,
            document,
            staging,
            state.get("primary_color", self.config.primary_color),
            html_ppt=True,
            style=job.get("style", {}),
        )
        require_quality(document)
        self.store.event(job["id"], node="Assembler", status="running", message="转换实际页面、更新Word目录页码并生成预览", progress=8/len(NODES))
        previews = await asyncio.to_thread(render_previews, staging)
        self.store.event(job["id"], node="Assembler", status="running", message="检查最终文件完整性、版面边界与正文清洁", progress=8/len(NODES))
        validation = await asyncio.to_thread(inspect_office, staging)
        rated = [p for p in document.pois if p.nationally_rated]
        photo_coverage = {
            "all": sum(bool(p.images) for p in document.pois)/len(document.pois) if document.pois else 0,
            "national_or_4a": sum(bool(p.images) for p in rated)/len(rated) if rated else None,
        }
        photo_targets_met = None if job.get("generation_mode") == "fast" else (
            photo_coverage["all"] >= .6 and (not rated or photo_coverage["national_or_4a"] >= .8)
        )
        save_json(folder / "quality-report.json", {"prd_version": document.prd_version, "passed": True, "scope":"Office完整性、目录页码、对象边界与正文清洁；完整验收另见对照报告", "photo_coverage":photo_coverage,"photo_targets_met":photo_targets_met, **validation})
        if photo_targets_met is not None:
            self.store.event(job['id'],node='Assembler',status='running',kind='photo_coverage',coverage=photo_coverage,qa_severity='info' if photo_targets_met else 'red',message='授权实拍覆盖率：'+str(photo_coverage)+('；达标' if photo_targets_met else '；未达验收目标，需要人工处理'))
        for path in folder.glob("slide-*.png"):
            path.unlink()
        for path in staging.iterdir():
            target = folder / path.name
            if path.is_dir():
                shutil.copytree(path, target, dirs_exist_ok=True)
            else:
                path.replace(target)
        for day in document.days:
            day.map_png = str(folder / f"day-{day.day}.png")
        save_json(folder / "deliverable.json", document.model_dump(mode="json"))
        job["previews"] = [f"/api/jobs/{job['id']}/files/{path.name}" for path in previews]
        state["document"] = document.model_dump(mode="json")
        state["geojson"] = geojson(document)
        job["artifacts"] = {
            key: f"/api/jobs/{job['id']}/files/{name}" for key, name in result.items()
        }
