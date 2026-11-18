"""Reapply corrected field normalization to recorded candidates; never invent evidence."""

import argparse
import asyncio
import json
import shutil

from backend.app.config import settings
from backend.app.store import Store, save_json
from backend.harness.enrichment import VisitInformation, recover_opening_fact
from backend.harness.graph import Harness
from backend.harness.reviewer import evidence_text, numeric_values, validate_facts
from backend.harness.schemas import Deliverable
from backend.tools.hours import fact_hours
from backend.tools.sources import normalize_facts, source_level


async def recover(job_id):
    config = settings()
    store = Store(config.data_dir)
    job = store.get(job_id)
    if not job or job["status"] not in {"failed", "interrupted", "completed"}:
        raise ValueError("只修复已结束或中断的任务，避免并发改写")
    folder = config.data_dir / "jobs" / job_id
    state = json.loads((folder / "state.json").read_text())
    progress = json.loads((folder / "enrichment-progress.json").read_text())
    document = Deliverable.model_validate(progress["document"])
    document.prd_version = "1.1"
    if state.get("document"):
        previous = Deliverable.model_validate(state["document"])
        document.decorations = previous.decorations
    backup = folder / "versions/before-hours-recovery"
    backup.mkdir(parents=True, exist_ok=True)
    for name in ["travel.pptx", "travel.docx", "deliverable.json", "state.json", "quality-report.json"]:
        if (folder / name).exists() and not (backup / name).exists():
            shutil.copy2(folder / name, backup / name)
    by_id = {s.id:s for s in document.sources}
    for source in document.sources:
        source.level = source_level(str(source.url))
    for poi in document.pois:
        candidates = folder / f"visit-candidates-{poi.id}.json"
        if candidates.exists():
            info = VisitInformation.model_validate_json(candidates.read_text())
            recover_opening_fact(info, by_id)
            for fact in info.facts:
                quote = evidence_text(fact.evidence)
                if fact.source_ids and quote and all(r in by_id for r in fact.source_ids) and any(quote in evidence_text(by_id[r].text) for r in fact.source_ids) and numeric_values(fact.value) <= numeric_values(fact.evidence):
                    poi.facts = [f for f in poi.facts if f.field != fact.field] + [fact]
        normalize_facts(poi, document.sources)
        hours = next((f for f in poi.facts if f.field == "hours" and f.source_ids), None)
        if hours:
            poi.hours = fact_hours(hours, document.sources)
    validate_facts(document)
    state.update(document=document.model_dump(mode="json"), sources=[s.model_dump(mode="json") for s in document.sources])
    save_json(folder / "state.json", state)
    job.update(status="queued", node="Writer", error=None)
    store.save(job)
    store.event(job_id, node="Verifier", status="completed", message="按已记录引文重新标准化时间字段；证据检查通过，重新排程与排版", progress=4/9)
    await Harness(config, store).run(job_id, "Writer")
    result = store.get(job_id)
    print(document.spec.city, result["status"], result["node"], result.get("error"), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("job_ids", nargs="+")
    args = parser.parse_args()
    for job_id in args.job_ids:
        await recover(job_id)


if __name__ == "__main__":
    asyncio.run(main())
