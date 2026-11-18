import { useEffect, useState } from "react";
import { Check, LoaderCircle, Sparkles, X } from "lucide-react";
import type { Job } from "./types";

function duration(seconds: number) {
  const value = Math.max(0, Math.floor(seconds));
  return value >= 3600 ? `${Math.floor(value / 3600)}时${Math.floor(value % 3600 / 60)}分`
    : value >= 60 ? `${Math.floor(value / 60)}分${value % 60}秒` : `${value}秒`;
}

export default function TaskProgress({ job, updatedAt }: { job: Job; updatedAt: number }) {
  const [clock, setClock] = useState(Date.now());
  const running = job.status === "running";
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [running]);
  const progress = job.progress;
  if (!progress) return <aside className="panel timeline">正在读取任务进度…</aside>;
  const delta = running ? Math.max(0, (clock - updatedAt) / 1000) : 0;
  const current = progress.stages.find(stage => stage.status === "running");
  const low = Math.max(0, (progress.remaining_low_s || 0) - delta);
  const high = Math.max(0, (progress.remaining_high_s || 0) - delta);
  return <aside className="panel timeline task-progress">
    <h2><Sparkles size={17} />任务进度</h2>
    <div className="progress-overview">
      <strong>{job.status === "queued" ? "排队等待执行" : running ? current?.label || "正在准备" : job.status === "completed" ? "任务已完成" : job.status === "failed" ? "任务已停止，等待重试" : "任务已中断"}</strong>
      {running && <p className="current-work" aria-live="polite">{current?.message}</p>}
      <div className="timing-metric"><span>{progress.elapsed_label}</span><b>{duration(progress.elapsed_s + delta)}</b></div>
      {running && <>
        <div className="timing-metric"><span>预计剩余</span><b>{high > 0 ? `约 ${Math.max(1, Math.ceil(low / 60)) === Math.max(1, Math.ceil(high / 60)) ? Math.max(1, Math.ceil(high / 60)) : `${Math.max(1, Math.ceil(low / 60))}–${Math.max(1, Math.ceil(high / 60))}`} 分钟` : "正在重新估算"}</b></div>
        {progress.estimated_finish_at && <p className="estimate-note">预计 {new Date(progress.estimated_finish_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })} 左右完成</p>}
        <p className="estimate-note">{progress.estimate_delayed ? "当前阶段已超过参考耗时，预计时间可能继续延长。" : `依据${progress.estimate_basis}，随实际进度更新。`}外部服务响应可能影响时间。</p>
      </>}
      {job.status === "queued" && <p className="estimate-note">前一任务结束后开始；开始执行后显示预计完成时间。</p>}
      <progress max={progress.total_stages || 1} value={progress.completed_stages} aria-label="已完成阶段" />
      <small>{progress.total_stages ? `已完成 ${progress.completed_stages}/${progress.total_stages} 个阶段` : "暂无阶段耗时记录"}</small>
    </div>
    <details className="progress-stages" open={running || job.status === "failed" || job.status === "interrupted" ? true : undefined}>
      <summary>查看阶段耗时与步骤</summary>
    {progress.stages.filter(stage => stage.status !== "skipped").map((stage, index) => {
      const records = job.events?.filter(event => event.node === stage.id) || [];
      return <details key={stage.id} className={`node ${stage.status}`} open={stage.status === "running" ? true : undefined}>
        <summary>
          <span className="node-marker">{stage.status === "completed" ? <Check size={13} /> : stage.status === "running" ? <LoaderCircle size={13} className="spin" /> : ["failed", "interrupted"].includes(stage.status) ? <X size={13} /> : index + 1}</span>
          <span>{stage.label}{stage.reused && <small>沿用已有结果</small>}</span>
          <span className="node-time">{stage.elapsed_s !== null ? duration(stage.elapsed_s + (stage.status === "running" ? delta : 0)) : stage.status === "pending" ? "待执行" : "未记录"}</span>
        </summary>
        <p>{stage.description}</p>
        {!!records.length && <ol className="stage-records">{records.map(record => <li key={record.seq}><time>{new Date(record.at).toLocaleTimeString("zh-CN")}</time><span>{record.message}</span>{record.duration_s !== undefined && <small>用时 {duration(record.duration_s)}</small>}</li>)}</ol>}
      </details>;
    })}
    </details>
  </aside>;
}
