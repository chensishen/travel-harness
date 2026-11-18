"""Validate and summarize the actual published files of a real full-pipeline task."""

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import settings  # noqa: E402
from backend.harness.quality import UNKNOWN, quality_issues  # noqa: E402
from backend.harness.schemas import NODES, Deliverable  # noqa: E402
from backend.render.inspection import inspect_office  # noqa: E402


def main(job_id, output_path=None):
    job = (
        httpx.get("http://127.0.0.1:8765/api/jobs/" + job_id, timeout=15).raise_for_status().json()
    )
    if job["status"] != "completed":
        raise ValueError(f"任务尚未成功完成：{job['status']} / {job.get('error')}")
    folder = settings().data_dir / "jobs" / job_id
    document = Deliverable.model_validate(job["deliverable"])
    issues = quality_issues(document)
    if issues:
        raise ValueError("实际产物质量检查失败：" + "；".join(issues))
    inspection = inspect_office(folder)
    levels = {s.id: s.level for s in document.sources}
    checked = sum(
        any(
            f.field == field
            and f.value != UNKNOWN
            and any(levels.get(ref) in {"L1", "L2"} for ref in f.source_ids)
            for f in poi.facts
        )
        for poi in document.pois
        for field in ["ticket", "hours", "booking"]
    )
    started = datetime.fromisoformat(job["run_started_at"])
    ended = datetime.fromisoformat(job["run_finished_at"])
    stage_events = [e for e in job["events"] if "duration_s" in e]
    first_started = min(
        datetime.fromisoformat(e["at"]) for e in job["events"] if e.get("phase_start")
    )
    failures = [e for e in stage_events if e["status"] in {"failed", "interrupted"}]
    completed_nodes = {e["node"] for e in stage_events if e["status"] == "completed"}
    report = {
        "job_id": job_id,
        "status": job["status"],
        "first_started_shanghai": first_started.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        "current_run_started_shanghai": started.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        "finished_shanghai": ended.astimezone(ZoneInfo("Asia/Shanghai")).isoformat(),
        "current_run_wall_clock_s": round((ended - started).total_seconds(), 2),
        "total_wall_clock_including_retry_pauses_s": round(
            (ended - first_started).total_seconds(), 2
        ),
        "recorded_execution_including_failed_stages_s": round(
            sum(e["duration_s"] for e in stage_events), 2
        ),
        "retry_count": len(failures),
        "failures": [
            {"stage": e["node"], "seconds": e["duration_s"], "reason": e["message"]}
            for e in failures
        ],
        "planned_stages_this_run": job.get("planned_nodes", []),
        "full_pipeline_in_this_run": set(NODES) <= set(job.get("planned_nodes", [])),
        "full_pipeline_across_attempts": set(NODES) <= completed_nodes,
        "stages": [
            {"stage": e["node"], "status": e["status"], "seconds": e["duration_s"]}
            for e in stage_events
        ],
        "external_operations": len([e for e in job["events"] if e.get("kind") == "operation"]),
        "cache_hits": len([e for e in job["events"] if e.get("cache_hit")]),
        "external_error_counts": dict(
            Counter(
                e["error_type"]
                for e in job["events"]
                if e.get("kind") == "operation"
                and e.get("error_type")
                and e["error_type"] != "CancelledError"
            )
        ),
        "quality_issues": issues,
        "source_count": len(document.sources),
        "poi_count": len(document.pois),
        "alternatives": document.alternatives,
        "sensitive_fields_checked": checked,
        "sensitive_fields_total": len(document.pois) * 3,
        "day_minutes": [d.total_minutes for d in document.days],
        "map_coverage": [d.map_coverage for d in document.days],
        "photos": sum(len(p.images) for p in document.pois),
        "photo_placeholders": sum(not p.images for p in document.pois),
        "decorations": len(document.decorations),
        **inspection,
        "word_pages": len(PdfReader(folder / "word-pdf/travel.pdf").pages),
        "downloads": {},
        "note": "分别记录本轮、累计阶段及包含重试间隔的实际耗时；沿用本机缓存，非旧版本成对测速。",
    }
    for name, url in job["artifacts"].items():
        response = httpx.get("http://127.0.0.1:8765" + url, timeout=20)
        response.raise_for_status()
        content = response.content
        local = (folder / url.rsplit("/", 1)[-1]).read_bytes()
        if content != local or content[:2] != b"PK":
            raise ValueError("下载内容与实际产物不一致")
        report["downloads"][name] = {
            "http_status": response.status_code,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    output = (
        Path(output_path)
        if output_path
        else settings().data_dir / "qa" / "performance-e2e-result.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    parser.add_argument("--output")
    args = parser.parse_args()
    main(args.job_id, args.output)
