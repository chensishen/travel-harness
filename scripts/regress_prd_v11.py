"""Run three real city cases from saved research, with fresh practical-information checks."""

import argparse
import asyncio
import json
import uuid

from backend.app.config import settings
from backend.app.store import Store, save_json
from backend.harness.graph import Harness
from backend.harness.schemas import TaskSpec, now

CASES = [
    ("杭州一日游", "4c71f75b-aab6-407e-ac37-08e87bff3d6b", TaskSpec(type="travel_guide", city="杭州", days=1), ["浙江省博物馆孤山馆区", "浙江美术馆", "中国丝绸博物馆"]),
    ("上海三日游", "40541499-8ea6-40af-ac95-97dba8383d08", TaskSpec(type="travel_guide", city="上海", days=3, interests=["公共交通", "历史文化"]), ["上海博物馆", "上海市规划展示馆", "外滩", "豫园", "中共一大会址纪念馆", "田子坊", "上海鲁迅纪念馆", "多伦路文化名人街", "上海犹太难民纪念馆"]),
    ("西安文旅介绍", "e080962c-a6be-4c2e-95e7-c28c1f1d6cf3", TaskSpec(type="culture_intro", city="西安"), ["秦始皇帝陵博物院", "西安城墙", "陕西历史博物馆", "大唐西市博物馆"]),
]


async def main():
    config = settings()
    store = Store(config.data_dir)
    manifest = []
    manifest_path = config.data_dir / "qa/prd-v11-regression.json"
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.resume:
        manifest = json.loads(manifest_path.read_text())
    for label, parent, spec, names in CASES:
        if args.resume:
            break
        job_id = str(uuid.uuid4())
        previous = json.loads((config.data_dir / "jobs" / parent / "state.json").read_text())
        folder = config.data_dir / "jobs" / job_id
        folder.mkdir(parents=True)
        state = {"prd_version": "1.1", "spec": spec.model_dump(mode="json"), "outline": {"title": label, "poi_names": names, "themes": previous["outline"]["themes"], "queries": []}, "sources": previous["sources"], "warnings": []}
        save_json(folder / "state.json", state)
        job = {"id":job_id,"prompt":f"{label}，按整改后的实用结构输出，偏好公共交通。", "options":{}, "kind":"document", "status":"queued", "node":"Verifier", "created_at":now(), "error":None, "warnings":[], "artifacts":{}, "spec":state["spec"], "outline":state["outline"], "research_parent":parent}
        store.save(job)
        manifest.append({"label":label,"job_id":job_id})
    save_json(manifest_path, manifest)
    for item in manifest:
        print(item["label"], item["job_id"], "开始", flush=True)
        harness = Harness(config, store)
        previous_job = store.get(item["job_id"])
        if previous_job["status"] == "completed":
            continue
        await harness.run(item["job_id"], previous_job.get("node", "Verifier"))
        job = store.get(item["job_id"])
        print(item["label"], job["status"], job["node"], job.get("error"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
