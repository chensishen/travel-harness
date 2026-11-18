"""Observable stage timing and explicitly approximate completion estimates."""

from datetime import UTC, datetime, timedelta
from statistics import median

from backend.harness.schemas import NODES

STAGES = {
    "Revision": ("分析修改", "识别修改范围，确定哪些阶段需要重新执行", 20),
    "Router": ("理解需求", "提取目的地、人数、日期与任务类型", 15),
    "Planner": ("规划大纲", "选择具体景点，整理研究主题与检索问题", 20),
    "Researcher": ("检索资料", "检索公开资料、读取网页并保存可追溯来源", 180),
    "Verifier": ("事实核验", "逐景点核对门票、开放时间、预约与地址", 150),
    "Writer": ("撰写内容", "根据已核实资料撰写正文、餐饮与预算建议", 50),
    "RoutePlanner": ("规划路线", "核对位置，计算交通并校验每日时间轴与底图", 80),
    "Illustrator": ("寻找配图", "检索授权照片、核验图像并准备装饰封面", 120),
    "Reviewer": ("质量审阅", "检查事实引用、文案和交付规则，修正问题", 90),
    "Assembler": ("生成文档", "排版PPT与Word，转换预览并检查最终文件", 20),
}


def timestamp(value):
    return datetime.fromisoformat(value) if value else None


def progress_snapshot(job, events, samples, *, current=None):
    current = current or datetime.now(UTC)
    names = NODES[:3] if job.get("kind") == "kb" else NODES
    if (job.get("spec") or {}).get("type") == "culture_intro":
        names = [name for name in names if name != "RoutePlanner"]
    if job.get("revision_instruction") or job.get("node") == "Revision":
        names = ["Revision", *names]
    planned = job.get("planned_nodes")
    if planned is None:
        planned = [name for name in names if any(e.get("node") == name for e in events)] if job["status"] == "completed" else names
    else:
        planned = [name for name in planned if name in names]
    run_start = timestamp(job.get("run_started_at"))
    stages = []
    expected = {}
    for name in names:
        observations = samples.get(name, [])
        expected[name] = median(observations) if observations else STAGES[name][2]
        if name in job.get("reused_nodes", []) or (name == "RoutePlanner" and (job.get("spec") or {}).get("type") == "culture_intro"):
            expected[name] = 0
        history = [e for e in events if e.get("node") == name]
        if run_start and name in planned:
            history = [e for e in history if timestamp(e["at"]) >= run_start]
        latest = history[-1] if history else None
        start = None
        status = None
        for event in history:
            if event["status"] == "running" and (event.get("phase_start") or status != "running"):
                start = timestamp(event["at"])
            status = event["status"]
        state = latest["status"] if latest else ("pending" if name in planned else "skipped")
        active = job["status"] == "running" and job.get("node") == name
        if state == "running" and not active:
            state = job["status"] if job["status"] in {"failed", "interrupted"} else "pending"
        elapsed = latest.get("duration_s") if latest else None
        if start and (active or (elapsed is None and state in {"failed", "interrupted"})):
            ended = current if active else timestamp(job.get("run_finished_at")) or timestamp(events[-1]["at"])
            elapsed = max(0, (ended - start).total_seconds())
        label, description, _ = STAGES[name]
        stages.append({"id":name,"label":label,"description":description,"status":state,
                       "elapsed_s":round(elapsed, 1) if elapsed is not None else None,
                       "started_at":start.isoformat() if start else None,
                       "message":latest.get("message", description) if latest else description,
                       "reused":(name not in planned and bool(latest)) or name in job.get("reused_nodes", []),
                       "estimated_s":round(expected[name]),"sample_count":len(observations)})
    recorded_run = run_start is not None
    end = timestamp(job.get("run_finished_at"))
    if not end and job["status"] not in {"queued", "running"} and events:
        end = timestamp(events[-1]["at"])
    elapsed = max(0, ((end or current) - run_start).total_seconds()) if run_start else 0
    if not recorded_run:
        elapsed = sum(s["elapsed_s"] or 0 for s in stages if s["status"] != "running")
    completed = [s for s in stages if s["id"] in planned and s["status"] == "completed"]
    remaining = 0
    delayed = False
    historical = False
    for stage in stages:
        if stage["id"] not in planned or stage["status"] in {"completed", "skipped"}:
            continue
        base = expected[stage["id"]]
        historical |= stage["sample_count"] > 0
        used = stage["elapsed_s"] or 0
        if stage["status"] == "running":
            delayed = base > 0 and used > base * 1.5
            # An overrun is uncertainty, never a false zero-second completion promise.
            remaining += max(base * .5, used * .5) if delayed else max(base * .25, base - used)
        else:
            remaining += base
    running = job["status"] == "running"
    low, high = round(remaining * .7), round(remaining * 1.7)
    return {"as_of":current.isoformat(),"stages":stages,"elapsed_s":round(elapsed, 1),
            "elapsed_label":"本次执行已用" if recorded_run else "已记录阶段耗时",
            "completed_stages":len(completed),"total_stages":len(planned),
            "remaining_low_s":low if running else None,"remaining_high_s":high if running else None,
            "estimated_finish_at":(current + timedelta(seconds=remaining)).isoformat() if running else None,
            "estimate_basis":"近期阶段耗时与初始参考值" if historical else "初始参考值（历史样本不足）",
            "estimate_delayed":delayed}
