"""Run four real Inner Mongolia jobs and preserve per-city successes and failures."""

import argparse
import contextlib
import io
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.store import save_json  # noqa: E402
from scripts.validate_performance_job import main as validate  # noqa: E402

BASE = "http://127.0.0.1:8765"
ROOT = Path("data/qa/inner-mongolia")
MANIFEST = ROOT / "manifest.json"
CASES = [
    (
        "hohhot",
        "呼和浩特",
        "travel_guide",
        "制作呼和浩特一日城市旅游攻略，2人，偏好博物馆和历史文化，公共交通优先。只安排城区内可合理衔接的具体景点或明确馆区，核实参观日期、门票、开放时间、预约和地址，不把停车预约当作个人参观预约。",
    ),
    (
        "baotou",
        "包头",
        "travel_guide",
        "制作包头一日城市旅游攻略，2人，偏好工业文化、博物馆和城市休闲，公共交通优先。景点必须具体且交通可衔接，避免把市外远距离景点硬塞进一日行程，参观信息以可追溯来源为准。",
    ),
    (
        "ordos",
        "鄂尔多斯",
        "travel_guide",
        "制作鄂尔多斯一日旅游攻略，2人，以康巴什城区的博物馆、城市建筑与文化体验为主，公共交通优先。分别核实具体场馆和入口，不安排跨越多个远郊旗县的一日路线。",
    ),
    (
        "hulunbuir",
        "呼伦贝尔",
        "culture_intro",
        "制作呼伦贝尔城市文化介绍，至少五个有依据的文化主题，覆盖历史、地域文化、地方生活与今日体验。区分呼伦贝尔市、海拉尔区及下辖地区，不生成跨城一日路线；参观对象选择资料充分且有明确名称的场馆或景点，未知信息明确说明。",
    ),
]


def new_manifest():
    if MANIFEST.exists():
        raise ValueError("已有测试批次，请使用 --resume 保留既有结果")
    ROOT.mkdir(parents=True, exist_ok=True)
    manifest = {"scope": "四城市真实全流程测试，沿用现有本机缓存", "cases": []}
    save_json(MANIFEST, manifest)
    for slug, city, kind, prompt in CASES:
        options = {"travelers": 2, **({"days": 1} if kind == "travel_guide" else {})}
        response = httpx.post(
            BASE + "/api/jobs", json={"prompt": prompt, "options": options}, timeout=20
        )
        response.raise_for_status()
        manifest["cases"].append(
            {
                "slug": slug,
                "city": city,
                "expected_type": kind,
                "job_id": response.json()["job_id"],
                "retry_count": 0,
                "failures": [],
            }
        )
        save_json(MANIFEST, manifest)
    return manifest


def main(resume):
    manifest = json.loads(MANIFEST.read_text()) if resume else new_manifest()
    previous = {}
    while True:
        for item in manifest["cases"]:
            if item.get("validation") in {"passed", "failed"}:
                continue
            try:
                job = (
                    httpx.get(BASE + "/api/jobs/" + item["job_id"], timeout=20)
                    .raise_for_status()
                    .json()
                )
            except httpx.TransportError as error:
                print(
                    json.dumps({"city": item["city"], "poll_error": type(error).__name__}),
                    flush=True,
                )
                continue
            state = (job["status"], job["node"])
            item["last_status"], item["last_stage"] = state
            if previous.get(item["job_id"]) != state:
                print(
                    json.dumps(
                        {
                            "city": item["city"],
                            "job_id": item["job_id"],
                            "status": state[0],
                            "stage": state[1],
                            "current_elapsed_s": job.get("progress", {}).get("elapsed_s"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                previous[item["job_id"]] = state
            if job["status"] == "completed":
                try:
                    assert job["spec"]["city"].removesuffix("市") == item["city"], (
                        "任务城市发生偏移"
                    )
                    assert job["spec"]["type"] == item["expected_type"], "任务类型发生偏移"
                    if item["expected_type"] == "travel_guide":
                        assert job["spec"]["days"] == 1 and job["spec"]["travelers"] == 2, (
                            "行程参数未保留"
                        )
                    else:
                        assert len(job["deliverable"]["chapters"]) >= 5, "文化主题不足五章"
                    target = ROOT / item["slug"] / "result.json"
                    with contextlib.redirect_stdout(io.StringIO()):
                        report = validate(item["job_id"], target)
                    item.update(
                        validation="passed",
                        result_path=str(target),
                        ppt_pages=report["ppt_slides"],
                        word_pages=report["word_pages"],
                        elapsed_s=report["total_wall_clock_including_retry_pauses_s"],
                    )
                    print(
                        json.dumps(
                            {
                                "city": item["city"],
                                "validation": "passed",
                                "ppt_pages": item["ppt_pages"],
                                "word_pages": item["word_pages"],
                                "elapsed_s": item["elapsed_s"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                except Exception as error:
                    item.update(validation="failed", validation_error=str(error)[:350])
            elif job["status"] in {"failed", "interrupted"}:
                message = job.get("error") or ""
                item["failures"].append({"stage": job["node"], "error": message})
                retryable = job["status"] == "interrupted" or any(
                    token in message
                    for token in ["超时", "ConnectError", "连接", "HTTP 5", "HTTP 429"]
                )
                if retryable and item["retry_count"] < 1:
                    httpx.post(
                        BASE + "/api/jobs/" + item["job_id"] + "/retry", timeout=20
                    ).raise_for_status()
                    item["retry_count"] += 1
                    print(
                        json.dumps(
                            {
                                "city": item["city"],
                                "retry": item["retry_count"],
                                "stage": job["node"],
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                else:
                    item.update(validation="failed", validation_error=message)
            save_json(MANIFEST, manifest)
        if all(item.get("validation") in {"passed", "failed"} for item in manifest["cases"]):
            print(
                "BATCH_FINISHED",
                json.dumps(
                    {item["city"]: item["validation"] for item in manifest["cases"]},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            return
        time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--resume", action="store_true")
    main(parser.parse_args().resume)
