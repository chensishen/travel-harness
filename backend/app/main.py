import asyncio
import hashlib
import io
import json
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.app.config import Settings, settings
from backend.app.store import Store
from backend.harness.graph import Harness
from backend.harness.progress import progress_snapshot
from backend.harness.quality import visit_verification_ratio
from backend.harness.schemas import Deliverable, ImageRef, JobRequest, Revision, now


class IngestRequest(BaseModel):
    city: str = Field(min_length=1, max_length=80)
    poi: str = Field(default="", max_length=120)


class StyleCompileRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=500)
    preset: str | None = None
    theme: str | None = None
    template: str | None = None
    animation: str | None = None


def create_app(config: Settings | None = None):
    config = config or settings()
    store = Store(config.data_dir)
    harness = Harness(config, store)
    active: dict[str, asyncio.Task] = {}

    @asynccontextmanager
    async def lifespan(app):
        for job in store.jobs():
            if job["status"] in {"running", "queued"}:
                if job["status"] == "running":
                    job["run_finished_at"] = now()
                job.update(status="interrupted", error="上次服务中断，可从当前节点重试")
                store.save(job)
        yield
        for task in active.values():
            task.cancel()
        await asyncio.gather(*list(active.values()), return_exceptions=True)
        await harness.mcp.aclose()
        await harness.models.aclose()

    app = FastAPI(title="文旅 Agent Harness", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.harness = harness

    def get_job(job_id):
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        return job

    def schedule(job_id, node="Router"):
        if job_id in active and not active[job_id].done():
            raise HTTPException(409, "任务仍在执行")
        task = asyncio.create_task(harness.run(job_id, node))
        active[job_id] = task
        task.add_done_callback(lambda done: active.pop(job_id, None))

    def create_job(body, kind="document", parent=None, instruction=None):
        job_id = str(uuid.uuid4())
        job = {
            "id": job_id,
            "prompt": body.prompt,
            "options": body.options,
            "generation_mode": body.generation_mode,
            "style": body.style,
            "kind": kind,
            "created_at": now(),
            "status": "queued",
            "node": "Revision" if parent else "Router",
            "warnings": [],
            "artifacts": {},
            "error": None,
        }
        if parent:
            previous = store.root / "jobs" / parent["id"]
            folder = store.root / "jobs" / job_id
            folder.mkdir(parents=True, exist_ok=True)
            checkpoint = json.loads((previous / "state.json").read_text())
            for image in previous.iterdir():
                if image.suffix in {".png", ".jpg"}:
                    shutil.copy2(image, folder / image.name)

            # Local image and map references must follow the new version's directory.
            def relocate(value):
                if isinstance(value, dict):
                    return {key: relocate(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [relocate(item) for item in value]
                if isinstance(value, str) and value.startswith(str(previous) + "/"):
                    return str(folder) + value[len(str(previous)) :]
                return value

            (folder / "state.json").write_text(json.dumps(relocate(checkpoint), ensure_ascii=False))
            job.update(parent_id=parent["id"], revision_instruction=instruction)
            job.update(spec=parent.get("spec"), outline=parent.get("outline"))
        store.save(job)
        schedule(job_id, job["node"])
        return {"job_id": job_id}

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "version": "0.1.0",
            "configured": {
                "llm": bool(config.llm_api_key.get_secret_value()),
                "embedding": bool(config.siliconflow_api_key.get_secret_value()),
                "mcp": config.mcp_config_path.expanduser().exists(),
            },
            "models": {
                "llm": config.llm_model,
                "embedding": config.embed_model,
                "rerank": config.rerank_model,
                "vision": config.vision_model,
            },
            "message": "配置存在不代表外部服务已连通，请运行 scripts/smoke.py",
        }

    @app.get("/api/styles")
    async def styles():
        from backend.render.html_ppt import catalog

        return catalog()

    @app.post("/api/styles/compile")
    async def compile_style(body: StyleCompileRequest):
        from backend.render.html_ppt import resolve_style

        style = resolve_style(
            body.prompt,
            {
                key: value
                for key, value in {
                    "preset": body.preset,
                    "theme": body.theme,
                    "template": body.template,
                    "animation": body.animation,
                    "prompt": body.prompt,
                }.items()
                if value
            },
        )
        return {
            "style": style,
            "preview_plan": [
                {"intent": "cover", "layout": "asymmetric-cover"},
                {"intent": "overview", "layout": "structured-overview"},
                {"intent": "chapter", "layout": "image-text-split"},
            ],
        }

    @app.post("/api/styles/compare")
    async def compare_styles(body: dict):
        from backend.qa.diversity import compare_styles
        from backend.render.html_ppt import resolve_style

        first = resolve_style(body.get("prompt_a", ""), body.get("style_a", {}))
        second = resolve_style(body.get("prompt_b", ""), body.get("style_b", {}))
        return {"first": first, "second": second, "difference": compare_styles(first, second)}

    @app.get("/api/templates")
    async def list_templates():

        folder = config.data_dir / "templates"
        folder.mkdir(parents=True, exist_ok=True)
        results = []
        for path in sorted(folder.glob("*")):
            analysis_path = path.with_suffix(path.suffix + ".json")
            if analysis_path.is_file():
                results.append(json.loads(analysis_path.read_text()))
        return results

    @app.post("/api/templates", status_code=201)
    async def upload_template(file: UploadFile = File(...)):
        from backend.templates.analyzer import analyze_template

        if Path(file.filename or "").suffix.casefold() not in {".pptx", ".potx"}:
            raise HTTPException(422, "模板必须是 .pptx 或 .potx")
        folder = config.data_dir / "templates"
        folder.mkdir(parents=True, exist_ok=True)
        template_id = hashlib.sha256((file.filename or "template").encode() + uuid.uuid4().bytes).hexdigest()[:16]
        path = folder / f"{template_id}{Path(file.filename).suffix.lower()}"
        path.write_bytes(await file.read(40_000_000))
        try:
            analysis = analyze_template(path)
        except Exception as error:
            path.unlink(missing_ok=True)
            raise HTTPException(422, "无法解析 PPTX 模板：" + str(error)[:200]) from error
        analysis["id"] = template_id
        analysis["path"] = path.name
        (folder / f"{path.name}.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2))
        return analysis

    @app.get("/api/jobs")
    async def list_jobs():
        return [{k: v for k, v in job.items() if k not in {"deliverable"}} for job in store.jobs()]

    @app.post("/api/jobs", status_code=202)
    async def new_job(body: JobRequest):
        return create_job(body)

    @app.get("/api/jobs/{job_id}")
    async def job_detail(job_id: str):
        job = get_job(job_id)
        events = store.events(job_id)
        return {**job, "events": events, "progress":progress_snapshot(job, events, store.timing_samples())}

    @app.get("/api/jobs/{job_id}/events")
    async def events(
        job_id: str, request: Request, last_event_id: Annotated[str | None, Header()] = None
    ):
        get_job(job_id)
        try:
            after = max(0, int(last_event_id or "0"))
        except ValueError:
            raise HTTPException(400, "Last-Event-ID 必须是整数")

        async def stream():
            cursor = after
            while not await request.is_disconnected():
                batch = store.events(job_id, cursor)
                for event in batch:
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if get_job(job_id)["status"] in {"completed", "failed", "interrupted"}:
                    yield "event: end\ndata: {}\n\n"
                    break
                if not batch:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str):
        job = get_job(job_id)
        if job["status"] not in {"failed", "interrupted"}:
            raise HTTPException(409, "只有失败或中断任务可以重试")
        node = job.get("node", "Router")
        if node == "Assembler":
            state = json.loads((store.root / "jobs" / job_id / "state.json").read_text())
            document = Deliverable.model_validate(state["document"])
            if visit_verification_ratio(document) < 0.75:
                node = "Verifier"
        job.update(status="queued", error=None)
        store.save(job)
        schedule(job_id, node)
        return {"job_id": job_id}

    @app.post("/api/jobs/{job_id}/revise", status_code=202)
    async def revise(job_id: str, body: Revision):
        job = get_job(job_id)
        if job["status"] != "completed" or job["kind"] != "document":
            raise HTTPException(409, "请在文档任务完成后提交修改")
        result = create_job(
            JobRequest(
                prompt=job["prompt"] + "\n修改要求：" + body.instruction,
                options=job["options"],
                generation_mode=job.get("generation_mode", "complete"),
                style=job.get("style", {}),
            ),
            parent=job,
            instruction=body.instruction,
        )
        return result

    @app.get("/api/jobs/{job_id}/artifacts")
    async def artifacts(job_id: str):
        return get_job(job_id).get("artifacts", {})

    @app.get("/api/jobs/{job_id}/sources")
    async def sources(job_id: str):
        job = get_job(job_id)
        return [
            {k: v for k, v in s.items() if k != "text"}
            for s in (job.get("deliverable") or {}).get("sources", [])
        ]

    @app.get("/api/jobs/{job_id}/map")
    async def map_data(job_id: str):
        from backend.harness.schemas import Deliverable
        from backend.render.map_renderer import geojson

        job = get_job(job_id)
        return (
            geojson(Deliverable.model_validate(job["deliverable"]))
            if job.get("deliverable")
            else {"type": "FeatureCollection", "features": []}
        )

    @app.get("/api/jobs/{job_id}/files/{name}")
    async def files(job_id: str, name: str):
        job = get_job(job_id)
        if job["status"] != "completed":
            raise HTTPException(409, "任务尚未完成，不能下载未经审阅的文件")
        allowed = {
            "travel.pptx",
            "travel.docx",
            "travel.html",
            "travel-html.zip",
            "html-ppt-style.json",
            "DESIGN.md",
            "html-qa.json",
            "deliverable.json",
            "overview.png",
        }
        if name not in allowed and not (
            name.startswith(("day-", "image-", "illustration-", "photo-", "poi-", "slide-"))
            and Path(name).suffix in {".png", ".jpg"}
        ):
            raise HTTPException(404, "文件不存在")
        root = (store.root / "jobs" / job_id).resolve()
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            raise HTTPException(404, "文件不存在")
        return FileResponse(path, filename=name)

    @app.post("/api/jobs/{job_id}/pois/{poi_id}/images", status_code=202)
    async def upload_photo(
        job_id: str, poi_id: str, request: Request, file: UploadFile = File(...)
    ):
        from PIL import Image, UnidentifiedImageError

        from backend.app.store import save_json

        job = get_job(job_id)
        if job["status"] not in {"completed", "failed", "interrupted"}:
            raise HTTPException(409, "请等待当前任务结束后上传照片")
        folder = store.root / "jobs" / job_id
        state_path = folder / "state.json"
        if not state_path.is_file():
            raise HTTPException(409, "尚无可更新的文档")
        state = json.loads(state_path.read_text())
        if not isinstance(state.get('document'),dict):
            raise HTTPException(409,'尚无可更新的文档')
        document = Deliverable.model_validate(state["document"])
        poi = next((p for p in document.pois if p.id == poi_id), None)
        if not poi:
            raise HTTPException(404, "景点不存在")
        content = await file.read(12_000_001)
        if len(content) > 12_000_000:
            raise HTTPException(413, "照片不能超过12MB")
        name = "photo-user-" + hashlib.sha256(content).hexdigest()[:16] + ".jpg"
        try:
            image = Image.open(io.BytesIO(content))
            if image.width * image.height > 30_000_000 or max(image.size) < 1200:
                raise HTTPException(422, "实拍长边至少1200像素，总像素不超过3000万")
            image = image.convert("RGB")
            image.thumbnail((1920, 1920))
            image.save(folder / name, quality=90)
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise HTTPException(422, "无法读取照片，请上传JPEG、PNG或WebP") from error
        url = str(request.base_url).rstrip("/") + f"/api/jobs/{job_id}/files/{name}"
        uploaded = ImageRef(
            url=url,
            source_page=url,
            author="用户提供",
            license="用户拥有",
            license_url="用户声明拥有授权",
            local_path=str((folder / name).resolve()),
            relevance_verified=True,
            subject_verified=True,
        )
        poi.images = [uploaded, *[i for i in poi.images if i.local_path != uploaded.local_path]]
        poi.photo_status = "用户提供实拍"
        for day in document.days:
            day.stops = [poi if p.id == poi.id else p for p in day.stops]
        state["document"] = document.model_dump(mode="json")
        save_json(state_path, state)
        job.update(status="queued", node="Assembler", error=None)
        store.save(job)
        store.event(
            job_id,
            node="Assembler",
            status="running",
            message=poi.name + "收到用户实拍，仅重跑排版；授权为用户声明拥有",
        )
        schedule(job_id, "Assembler")
        return {"job_id": job_id, "poi_id": poi_id, "message": "照片已接收，正在重新排版"}

    @app.post("/api/kb/ingest", status_code=202)
    async def ingest(body: IngestRequest):
        return create_job(
            JobRequest(prompt=f"研究 {body.city} {body.poi} 的文旅资料", options={}), kind="kb"
        )

    @app.get("/api/kb/coverage")
    async def coverage(city: str = "上海"):
        try:
            return await asyncio.to_thread(harness.kb.coverage, city)
        except Exception:
            raise HTTPException(503, "Milvus 尚不可用，请检查容器和 MILVUS_URI；未返回虚假覆盖数据")

    frontend = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


app = create_app()
