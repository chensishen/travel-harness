import { lazy, Suspense, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import { persist } from "zustand/middleware";
import {
  ArrowDownToLine,
  ArrowLeft,
  ArrowRight,
  BookOpen,
  Check,
  ChevronDown,
  CircleAlert,
  Compass,
  Copy,
  Database,
  FileText,
  History,
  Layers3,
  LoaderCircle,
  MapPin,
  Plus,
  RotateCcw,
  Send,
  Settings2,
  Sparkles,
} from "lucide-react";
import { api } from "./api";
import TaskProgress from "./TaskProgress";
import type { Block, GenerationMode, Job, Source, StyleChoice } from "./types";

const RouteMap = lazy(() => import("./RouteMap"));
type Health = {
  configured: { llm: boolean };
  models?: { llm: string };
};
type Page = "home" | "history" | "kb" | "job";
type Draft = { prompt: string; options?: Job["options"]; mode?: GenerationMode; style?: StyleChoice };
const generationModes = {
  fast: { label: "快速", description: "跳过自动配图，保留路线图和事实核验。" },
  balanced: { label: "均衡", description: "查找并核验授权实拍图，省去 AI 装饰封面。" },
  complete: { label: "完整", description: "查找并核验授权实拍图，另生成 AI 装饰封面。" },
} satisfies Record<GenerationMode, { label: string; description: string }>;
const fallbackStyleCatalog = {
  presets: [
    { id: "shanghai-modern", label: "海派摩登", description: "暖色杂志感与非对称图文" },
    { id: "official-formal", label: "官方庄重", description: "克制、对称、条目清晰" },
    { id: "modern-minimal", label: "现代简约", description: "网格、留白与清晰层级" },
    { id: "magazine-editorial", label: "杂志编辑", description: "衬线标题与章节叙事" },
    { id: "tech-dark", label: "科技深色", description: "深色背景与数据入口" },
  ],
  themes: ["editorial-serif", "magazine-bold", "swiss-grid", "corporate-clean", "tokyo-night"],
  templates: ["xhs-white-editorial", "weekly-report", "dir-key-nav-minimal", "graphify-dark-graph", "pitch-deck"],
};
const themeLabels: Record<string, string> = {
  "academic-paper": "学术论文", "arctic-cool": "极地冷调", "aurora": "极光", "bauhaus": "包豪斯",
  "blueprint": "蓝图工程", "catppuccin-latte": "柔和拿铁", "catppuccin-mocha": "柔和摩卡",
  "corporate-clean": "商务清爽", "cyberpunk-neon": "赛博霓虹", "dracula": "德古拉暗紫",
  "editorial-serif": "编辑衬线", "engineering-whiteprint": "工程白图", "glassmorphism": "玻璃拟态",
  "gruvbox-dark": "复古暗色", "japanese-minimal": "日式极简", "magazine-bold": "杂志醒目",
  "memphis-pop": "孟菲斯撞色", "midcentury": "世纪中叶", "minimal-white": "极简留白",
  "neo-brutalism": "新粗野主义", "news-broadcast": "新闻播报", "nord": "北境蓝灰",
  "pitch-deck-vc": "投资路演", "rainbow-gradient": "彩虹渐变", "retro-tv": "复古电视",
  "rose-pine": "玫瑰松木", "sharp-mono": "锐利黑白", "soft-pastel": "柔和粉彩",
  "solarized-light": "日蚀浅色", "sunset-warm": "日落暖调", "swiss-grid": "瑞士网格",
  "terminal-green": "终端绿", "tokyo-night": "东京夜色", "vaporwave": "蒸汽波",
  "xiaohongshu-white": "小红书留白", "y2k-chrome": "Y2K 镀铬",
};
const templateLabels: Record<string, string> = {
  "course-module": "课程模块", "dir-key-nav-minimal": "目录导航极简", "graphify-dark-graph": "深色关系图",
  "hermes-cyber-terminal": "赫尔墨斯赛博终端", "knowledge-arch-blueprint": "知识档案蓝图",
  "obsidian-claude-gradient": "黑曜石渐变", "pitch-deck": "项目路演", "presenter-mode-reveal": "演讲者揭示",
  "product-launch": "产品发布", "tech-sharing": "技术分享", "testing-safety-alert": "测试安全提醒",
  "weekly-report": "周报", "xhs-pastel-card": "小红书粉彩卡片", "xhs-post": "小红书图文",
  "xhs-white-editorial": "小红书白色编辑",
};
const useNavigation = create(
  persist<{
    page: Page;
    selected: string | null;
    navigate: (page: Page, selected?: string) => void;
  }>(
    (set) => ({
      page: "home",
      selected: null,
      navigate: (page, selected) => set({ page, selected: selected ?? null }),
    }),
    { name: "travel-harness-navigation" },
  ),
);
const statusNames: Record<string, string> = {
  completed: "已完成",
  failed: "生成失败",
  running: "生成中",
  queued: "排队中",
  interrupted: "已中断",
};
const examples = [
  {
    city: "上海",
    type: "城市漫游",
    text: "做一个上海 3 日游攻略，偏好历史建筑和本地美食",
  },
  {
    city: "苏州",
    type: "文化探寻",
    text: "做一些苏州的文旅介绍，介绍园林、昆曲和传统工艺",
  },
  {
    city: "西安",
    type: "历史人文",
    text: "做一个西安 3 日游攻略，偏好博物馆与历史文化",
  },
];

function Status({ status }: { status: string }) {
  return (
    <span className={`status ${status}`}>
      <i />
      {statusNames[status] || status}
    </span>
  );
}

function App() {
  const { page, selected, navigate } = useNavigation();
  const [seed, setSeed] = useState<Draft>({ prompt: "" });
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => api<Health>("/health"),
    refetchInterval: 15000,
  });
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api<Job[]>("/jobs"),
    refetchInterval: 5000,
  });
  const configured = health.data?.configured.llm;
  const modelStatus = health.isError ? "后端未连接"
    : health.isPending ? "正在读取模型配置"
    : configured ? "模型密钥已配置" : "待配置模型密钥";
  const textModel = health.isError ? undefined : health.data?.models?.llm;
  function copyJob(job: Job) {
    const spec = job.spec;
    const options = spec
      ? {
          ...job.options,
          days: spec.type === "culture_intro" ? undefined : spec.days ?? job.options?.days,
          travelers: spec.travelers ?? job.options?.travelers,
          budget_level: spec.budget_level ?? job.options?.budget_level,
          season_or_dates: spec.season_or_dates ?? job.options?.season_or_dates,
        }
      : job.options;
    setSeed({ prompt: job.prompt, options, mode: job.generation_mode ?? "complete", style: job.style });
    navigate("home");
  }
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <button
          className="brand"
          onClick={() => navigate("home")}
          aria-label="知行首页"
        >
          <span className="brand-icon">
            <Compass size={25} />
          </span>
          <span>
            知行<small>文旅文档助手</small>
          </span>
        </button>
        <nav aria-label="主导航">
          <button
            className={page === "home" ? "active" : ""}
            onClick={() => { setSeed({ prompt: "" }); navigate("home"); }}
          >
            <Layers3 size={18} />
            新建文档
          </button>
          <button
            className={page === "history" ? "active" : ""}
            onClick={() => navigate("history")}
          >
            <History size={18} />
            历史任务<span className="nav-count">{jobs.data?.length ?? 0}</span>
          </button>
          <button
            className={page === "kb" ? "active" : ""}
            onClick={() => navigate("kb")}
          >
            <Database size={18} />
            资料库
          </button>
        </nav>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div>
            <span>工作空间</span>
            <span className="slash">/</span>
            <strong>
              {page === "home"
                ? "新建文档"
                : page === "history"
                  ? "历史任务"
                  : page === "kb"
                    ? "资料库"
                    : "任务详情"}
            </strong>
          </div>
          <span
            className={`service-state ${configured && !health.isError ? "ready" : ""}`}
            title={textModel ? `${textModel} · ${modelStatus}` : undefined}
          >
            <i aria-hidden="true" />
            <span className="model-name">{textModel || modelStatus}</span>
          </span>
        </header>
        <main>
          {page === "home" && (
            <Home
              seed={seed}
              onCreated={(id) => navigate("job", id)}
              jobs={jobs.data ?? []}
              onOpen={(id) => navigate("job", id)}
            />
          )}
          {page === "history" && (
            <>
              <PageTitle
                eyebrow=""
                title="历史任务"
                description="查看生成结果，或复制需求创建新文档。"
              />
              {jobs.isError ? (
                <div className="notice error">{jobs.error.message}</div>
              ) : jobs.isPending ? (
                <Loading />
              ) : (
                <JobList
                  jobs={jobs.data}
                  onOpen={(id) => navigate("job", id)}
                  onCopy={copyJob}
                />
              )}
            </>
          )}
          {page === "kb" && (
            <KnowledgeBase onCreated={(id) => navigate("job", id)} />
          )}
          {page === "job" && selected && (
            <JobDetail
              key={selected}
              id={selected}
              onOpen={(id) => navigate("job", id)}
              onCopy={copyJob}
            />
          )}
        </main>
        <footer className="page-footer">
          <span>知行 · 让每一段旅程有据可循</span>
          <span>
            本地运行 <i /> 来源可溯 <i /> 同源文档
          </span>
        </footer>
      </div>
    </div>
  );
}

function PageTitle({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description: string;
}) {
  return (
    <div className="page-title">
      {eyebrow && <div className="eyebrow">{eyebrow}</div>}
      <h1>{title}</h1>
      <p>{description}</p>
    </div>
  );
}

function Home({
  seed,
  onCreated,
  jobs,
  onOpen,
}: {
  seed: Draft;
  onCreated: (id: string) => void;
  jobs: Job[];
  onOpen: (id: string) => void;
}) {
  const [prompt, setPrompt] = useState(seed.prompt);
  const [mode, setMode] = useState<GenerationMode>(seed.mode ?? "balanced");
  const [documentType, setDocumentType] = useState<"travel" | "culture">("travel");
  const [archiveView, setArchiveView] = useState<"route" | "source">("route");
  const [advanced, setAdvanced] = useState(false);
  const [optionsChanged, setOptionsChanged] = useState(false);
  const [days, setDays] = useState("3");
  const [travelers, setTravelers] = useState("1");
  const [budget, setBudget] = useState("适中");
  const [date, setDate] = useState("");
  const [pace, setPace] = useState("松弛");
  const [transport, setTransport] = useState("公共交通");
  const [dietary, setDietary] = useState("不限制");
  const [focus, setFocus] = useState("城市肌理");
  const [stylePreset, setStylePreset] = useState(seed.style?.preset ?? "");
  const [stylePrompt, setStylePrompt] = useState(seed.style?.prompt ?? "");
  const [theme, setTheme] = useState(seed.style?.theme ?? "");
  const [template, setTemplate] = useState(seed.style?.template ?? "");
  const [templateId, setTemplateId] = useState(seed.style?.template_id ?? "");
  const styles = useQuery({
    queryKey: ["styles"],
    queryFn: () => api<{ presets: { id: string; label: string; description: string }[]; themes: string[]; templates: string[] }>("/styles"),
    staleTime: 300000,
  });
  const styleCatalog = styles.data ?? fallbackStyleCatalog;
  useEffect(() => {
    setPrompt(seed.prompt);
    setDocumentType(seed.prompt.startsWith("文旅介绍：") ? "culture" : "travel");
    setMode(seed.mode ?? "balanced");
    setAdvanced(!!seed.options && Object.keys(seed.options).length > 0);
    setOptionsChanged(!!seed.options && Object.keys(seed.options).length > 0);
    setDays(String(seed.options?.days ?? 3));
    setTravelers(String(seed.options?.travelers ?? 1));
    setBudget(seed.options?.budget_level ?? "适中");
    const savedDate = seed.options?.season_or_dates ?? "";
    setDate(/^\d{4}-\d{2}-\d{2}$/.test(savedDate) ? savedDate : "");
    setPace(seed.options?.pace ?? "松弛");
    setTransport(seed.options?.transport ?? "公共交通");
    setDietary(seed.options?.dietary ?? "不限制");
    setFocus(seed.options?.focus ?? "城市肌理");
    setStylePreset(seed.style?.preset ?? "");
    setStylePrompt(seed.style?.prompt ?? "");
    setTheme(seed.style?.theme ?? "");
    setTemplate(seed.style?.template ?? "");
    setTemplateId(seed.style?.template_id ?? "");
  }, [seed]);
  const uploadTemplate = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData();
      form.append("file", file);
      return api<{ id: string }>("/templates", { method: "POST", body: form });
    },
    onSuccess: (result) => setTemplateId(result.id),
  });
  const invalidTravelers = (advanced || optionsChanged) &&
    (!Number.isInteger(Number(travelers)) || Number(travelers) < 1 || Number(travelers) > 50);
  const createJob = useMutation({
    mutationFn: () =>
      api<{ job_id: string }>("/jobs", {
        method: "POST",
        body: JSON.stringify({
          prompt: documentType === "culture" ? `文旅介绍：${prompt}` : prompt,
          generation_mode: mode,
            style: {
              ...(stylePreset ? { preset: stylePreset } : {}),
              ...(stylePrompt.trim() ? { prompt: stylePrompt.trim() } : {}),
              ...(theme ? { theme } : {}),
              ...(template ? { template } : {}),
              ...(templateId ? { template_id: templateId } : {}),
            },
          options: advanced || optionsChanged
            ? {
                days: Number(days),
                travelers: Number(travelers),
                budget_level: budget,
                season_or_dates: date || "未指定",
                pace,
                transport,
                dietary,
                focus,
              }
            : {},
        }),
      }),
    onSuccess: (data) => onCreated(data.job_id),
  });
  return (
    <>
      <section className="workspace-hero">
        <div className="hero-copy">
          <div className="hero-kicker"><span className="kicker-dot" />知行工作台 <span className="kicker-line" /> 研究 · 规划 · 交付</div>
          <h1>把目的地，<br /><em>写成一段可出发的路。</em></h1>
          <p>无论是规划一段旅程，还是介绍一座城市，知行都会检索公开资料、核验关键事实，<br />再整理成可以分享和继续修改的文旅文档。</p>
          <div className="hero-proof" aria-label="知行工作方式">
            <span><strong>01</strong> 可追溯来源</span>
            <span><strong>02</strong> 路线可核验</span>
            <span><strong>03</strong> 同源交付</span>
          </div>
        </div>
        <div className={`hero-visual archive-${archiveView}`}>
          <div className="archive-paper">
            <span className="archive-stamp">ZN / 026</span>
            <span className="archive-index">CITY<br />{archiveView === "route" ? "ROUTE" : "SOURCE"}</span>
            <div className="archive-title">{archiveView === "route" ? "SHANGHAI" : "CITY NOTES"}</div>
            <div className="archive-rule" />
            <div className="archive-meta">{archiveView === "route" ? <><span>沿江而行</span><span>旧城 · 新味</span><span>3 DAYS / 2 PERS</span></> : <><span>公共资料</span><span>事实核验</span><span>12 SOURCES / 04 THEMES</span></>}</div>
            {archiveView === "route" ? <div className="archive-map"><i /><i /><i /><b /><b /></div> : <div className="archive-index-list"><span>01&nbsp; 城市肌理与步行尺度</span><span>02&nbsp; 一顿本地早餐的去处</span><span>03&nbsp; 旧建筑的开放时间</span><span>04&nbsp; 一条不赶路的江岸线</span></div>}
            <span className="archive-note">{archiveView === "route" ? <>记录一座城<br />从街角开始</> : <>先把事实理清<br />再决定怎么出发</>}</span>
            <span className="archive-coord">31°14′N&nbsp;&nbsp;121°28′E</span>
          </div>
          <button className={`archive-tab tab-red ${archiveView === "route" ? "selected" : ""}`} onClick={() => setArchiveView("route")}>TRAVEL</button>
          <button className={`archive-tab tab-blue ${archiveView === "source" ? "selected" : ""}`} onClick={() => setArchiveView("source")}>RESEARCH</button>
        </div>
      </section>
      <section className="composer panel">
        <div className="composer-title">
          <span className="section-icon">
            <Sparkles size={19} />
          </span>
          <h2>你想做什么文档？</h2>
        </div>
        <div className="document-intent" role="radiogroup" aria-label="文档类型">
          <button type="button" role="radio" aria-checked={documentType === "travel"} className={documentType === "travel" ? "active" : ""} onClick={() => setDocumentType("travel")}>
            <span className="intent-mark">↗</span><span><strong>旅行攻略</strong><small>按天规划路线、时间与交通</small></span>
          </button>
          <button type="button" role="radio" aria-checked={documentType === "culture"} className={documentType === "culture" ? "active" : ""} onClick={() => setDocumentType("culture")}>
            <span className="intent-mark">▤</span><span><strong>文旅介绍</strong><small>讲清城市、文化与在地故事</small></span>
          </button>
        </div>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (prompt.trim().length >= 2 && !invalidTravelers) createJob.mutate();
          }}
        >
          <label className="sr-only" htmlFor="prompt">
            你的文旅需求
          </label>
          <textarea
            id="prompt"
            value={prompt}
            onChange={(event) => setPrompt(event.target.value)}
            maxLength={2000}
            placeholder="例如：杭州 3 日游，2 人，喜欢博物馆和本地美食；或介绍苏州园林与昆曲。"
            rows={3}
          />
          <div className="generation-mode" aria-labelledby="generation-mode-label">
            <span id="generation-mode-label" className="mode-label">生成模式</span>
            <div className="mode-switcher" role="radiogroup" aria-label="生成模式">
              {Object.entries(generationModes).map(([value, item]) => (
                <button
                  type="button"
                  key={value}
                  role="radio"
                  aria-checked={mode === value}
                  className={mode === value ? "active" : ""}
                  onClick={() => setMode(value as GenerationMode)}
                >
                  <strong>{item.label}</strong><small>{value === "fast" ? "轻装" : value === "balanced" ? "推荐" : "全套"}</small>
                </button>
              ))}
            </div>
            <span id="generation-mode-description" className="mode-description">{generationModes[mode].description}</span>
          </div>
          <div className="style-picker">
            <label htmlFor="style-preset">PPT 风格</label>
            <select id="style-preset" value={stylePreset} onChange={(event) => setStylePreset(event.target.value)}>
              <option value="">根据描述自动选择</option>
              {styleCatalog.presets.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select>
            <input
              value={stylePrompt}
              onChange={(event) => setStylePrompt(event.target.value)}
              placeholder="例如：上海海派摩登，外滩与石库门，图文交错"
              aria-label="自定义 PPT 风格描述"
            />
            <details>
              <summary>高级：主题与完整模板</summary>
              <div className="style-advanced-grid">
                <select aria-label="html-ppt 主题" value={theme} onChange={(event) => setTheme(event.target.value)}>
                  <option value="">自动主题</option>
                  {styleCatalog.themes.map((item) => <option key={item} value={item}>{themeLabels[item] ?? item}</option>)}
                </select>
                <select aria-label="html-ppt 完整模板" value={template} onChange={(event) => setTemplate(event.target.value)}>
                  <option value="">自动模板</option>
                  {styleCatalog.templates.map((item) => <option key={item} value={item}>{templateLabels[item] ?? item}</option>)}
                </select>
                <label className="template-upload">
                  上传 PPTX 模板
                  <input type="file" accept=".pptx,.potx" onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) uploadTemplate.mutate(file);
                  }} />
                  {templateId && <small>已解析模板：{templateId}</small>}
                </label>
              </div>
            </details>
          </div>
          {advanced && (
            <div className="advanced-grid">
              <label>
                行程天数
                <select value={days} onChange={(e) => { setDays(e.target.value); setOptionsChanged(true); }}>
                  {Array.from({ length: 14 }, (_, i) => (
                    <option key={i} value={i + 1}>
                      {i + 1} 天
                    </option>
                  ))}
                </select>
              </label>
              <label>
                出行人数
                <input
                  type="number"
                  min="1"
                  max="50"
                  value={travelers}
                  onChange={(e) => { setTravelers(e.target.value); setOptionsChanged(true); }}
                  required
                />
              </label>
              <label>
                预算偏好
                <select
                  value={budget}
                  onChange={(e) => { setBudget(e.target.value); setOptionsChanged(true); }}
                >
                  <option>经济</option>
                  <option>适中</option>
                  <option>舒适</option>
                </select>
              </label>
              <label>
                出行日期
                <input
                  type="date"
                  value={date}
                  onChange={(e) => { setDate(e.target.value); setOptionsChanged(true); }}
                />
              </label>
              <label>
                出行节奏
                <select value={pace} onChange={(e) => { setPace(e.target.value); setOptionsChanged(true); }}>
                  <option>松弛</option>
                  <option>均衡</option>
                  <option>紧凑</option>
                </select>
              </label>
              <label>
                交通偏好
                <select value={transport} onChange={(e) => { setTransport(e.target.value); setOptionsChanged(true); }}>
                  <option>公共交通</option>
                  <option>步行优先</option>
                  <option>打车为主</option>
                </select>
              </label>
              <label>
                饮食偏好
                <select value={dietary} onChange={(e) => { setDietary(e.target.value); setOptionsChanged(true); }}>
                  <option>不限制</option>
                  <option>本地小吃</option>
                  <option>素食友好</option>
                  <option>清真友好</option>
                </select>
              </label>
              <label>
                内容重点
                <select value={focus} onChange={(e) => { setFocus(e.target.value); setOptionsChanged(true); }}>
                  <option>城市肌理</option>
                  <option>历史建筑</option>
                  <option>本地美食</option>
                  <option>亲子体验</option>
                  <option>拍照路线</option>
                </select>
              </label>
            </div>
          )}
          <div className="composer-actions">
            <button
              type="button"
              className="text-button"
              onClick={() => setAdvanced(!advanced)}
              aria-expanded={advanced}
            >
              <Settings2 size={15} />
              天数、人数等设置
              <ChevronDown size={14} className={advanced ? "rotated" : ""} />
            </button>
            <span className="composer-hint">未填的信息会在文档中注明默认安排</span>
            <button
              className="primary"
              disabled={prompt.trim().length < 2 || invalidTravelers || createJob.isPending}
            >
              {createJob.isPending ? (
                <LoaderCircle className="spin" size={17} />
              ) : (
                <Sparkles size={17} />
              )}
              生成文档
              <ArrowRight size={16} />
            </button>
          </div>
        </form>
        {invalidTravelers && <div className="notice error" role="alert">出行人数需为1–50之间的整数，请展开设置修改。</div>}
        {createJob.isError && (
          <div className="notice error" role="alert">
            {createJob.error.message}
          </div>
        )}
      </section>
      <section className="quick-examples" aria-label="需求示例">
        <span className="quick-label">从一个方向开始</span>
        {examples.map(example => <button key={example.city} onClick={() => {
          setPrompt(example.text);
          document.getElementById("prompt")?.focus();
        }}><Plus size={14} />{example.city}{example.type === "文化探寻" ? "文化介绍" : "3日攻略"}</button>)}
      </section>
      {jobs.length > 0 && (
        <section className="recent">
          <div className="section-heading">
            <h2>最近任务</h2>
            <button
              className="text-button"
              onClick={() => useNavigation.getState().navigate("history")}
            >
              查看全部
              <ArrowRight size={15} />
            </button>
          </div>
          <JobList jobs={jobs.slice(0, 3)} onOpen={onOpen} />
        </section>
      )}
    </>
  );
}

function JobList({
  jobs,
  onOpen,
  onCopy,
}: {
  jobs: Job[];
  onOpen: (id: string) => void;
  onCopy?: (job: Job) => void;
}) {
  if (!jobs.length)
    return (
      <div className="empty panel">
        <Compass size={34} />
        <h3>你的第一段旅程，正待开启</h3>
        <p>在新建文档输入目的地，研究记录会保存在这里。</p>
      </div>
    );
  return (
    <div className="job-list panel">
      {jobs.map((job) => (
        <div className="job-row" key={job.id}>
          <span className="job-icon">
            {job.kind === "kb" ? (
              <Database size={21} />
            ) : (
              <FileText size={21} />
            )}
          </span>
          <button className="job-link" onClick={() => onOpen(job.id)}>
            <strong>
              {job.spec?.city
                ? `${job.spec.city} · ${job.spec.type === "culture_intro" ? "文旅介绍" : "旅行攻略"}`
                : job.prompt}
            </strong>
            <small>
              {new Date(job.created_at).toLocaleString("zh-CN")} ·{" "}
              {job.parent_id ? "修改版本" : job.kind === "kb" ? "资料采集" : `${job.spec?.type === "culture_intro" ? "文化介绍" : `${job.spec?.days || "—"} 天`} · ${job.spec?.travelers || "—"} 人`}
            </small>
            {job.status === "failed" && job.error && (
              <small className="job-error" title={job.error}>{job.error}</small>
            )}
          </button>
          <Status status={job.status} />
          {onCopy && (
            <button
              className="icon-button"
              title="复制为新任务"
              aria-label="复制为新任务"
              onClick={() => onCopy(job)}
            >
              <Copy size={16} />
            </button>
          )}
          <button
            className="icon-button"
            aria-label="打开任务"
            onClick={() => onOpen(job.id)}
          >
            <ArrowRight size={18} />
          </button>
        </div>
      ))}
    </div>
  );
}

function JobDetail({
  id,
  onOpen,
  onCopy,
}: {
  id: string;
  onOpen: (id: string) => void;
  onCopy: (job: Job) => void;
}) {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState("preview");
  const [instruction, setInstruction] = useState("");
  const [streamError, setStreamError] = useState(false);
  const query = useQuery({
    queryKey: ["job", id],
    queryFn: () => api<Job>(`/jobs/${id}`),
    refetchInterval: 5000,
  });
  const job = query.data;
  useEffect(() => {
    if (!job || !["running", "queued"].includes(job.status)) return;
    const source = new EventSource(`/api/jobs/${id}/events`);
    source.onmessage = () => {
      setStreamError(false);
      void queryClient.invalidateQueries({ queryKey: ["job", id] });
    };
    source.addEventListener("end", () => {
      source.close();
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    });
    source.onerror = () => setStreamError(true);
    return () => source.close();
  }, [id, job?.status, queryClient]);
  const retry = useMutation({
    mutationFn: () => api(`/jobs/${id}/retry`, { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["job", id] }),
  });
  const revise = useMutation({
    mutationFn: () =>
      api<{ job_id: string }>(`/jobs/${id}/revise`, {
        method: "POST",
        body: JSON.stringify({ instruction }),
      }),
    onSuccess: (data) => onOpen(data.job_id),
  });
  if (query.isPending) return <Loading />;
  if (query.isError || !job)
    return (
      <div className="notice error">{query.error?.message || "任务不存在"}</div>
    );
  return (
    <>
      <button
        className="text-button back"
        onClick={() => useNavigation.getState().navigate("history")}
      >
        <ArrowLeft size={15} />
        所有任务
      </button>
      <div className="detail-heading">
        <div>
          <h1>{job.outline?.title || job.prompt}</h1>
          <p>
            {job.spec
              ? [
                  job.spec.city,
                  job.spec.days ? `${job.spec.days} 天` : null,
                  job.spec.type === "culture_intro" ? "文旅介绍" : "旅游攻略",
                  job.spec.travelers ? `${job.spec.travelers} 人` : null,
                  job.spec.budget_level || null,
                ]
                  .filter(Boolean)
                  .join(" · ")
              : "正在理解你的创作需求"}
            {job.kind === "document" && ` · ${generationModes[job.generation_mode ?? "complete"].label}模式`}
            {job.kind === "document" && job.spec && (
              <button className="text-button spec-edit" onClick={() => onCopy(job)}>
                修改需求
              </button>
            )}
          </p>
          {job.parent_id && (
            <div className="revision-meta">
              <span className="badge">修改版本</span>
              {job.revision_instruction && <span>修改要求：{job.revision_instruction}</span>}
              <button
                className="text-button"
                onClick={() => {
                  if (job.parent_id) onOpen(job.parent_id);
                }}
              >
                查看原版本
              </button>
            </div>
          )}
        </div>
        <div className="detail-actions">
          <Status status={job.status} />
          {job.status === "completed" && Object.entries(job.artifacts).map(([type, url]) =>
            <a key={type} className={type === "pptx" ? "primary" : "download-secondary"} href={url} download>
              <ArrowDownToLine size={16} />下载 {type === "pptx" ? "PPT" : type === "docx" ? "Word" : type.toUpperCase()}
            </a>)}
        </div>
      </div>
      {job.error && (
        <div className="notice error">
          <CircleAlert size={18} />
          <span>{job.error}</span>
          <button
            className="text-button"
            disabled={retry.isPending}
            onClick={() => retry.mutate()}
          >
            <RotateCcw size={15} />
            继续生成
          </button>
        </div>
      )}
      {retry.isError && (
        <div className="notice error">{retry.error.message}</div>
      )}
      {streamError && (
        <div className="notice">
          实时连接暂时中断，正在通过定时刷新同步进度。
        </div>
      )}
      <div className="task-layout">
        <TaskProgress job={job} updatedAt={query.dataUpdatedAt} />
        <section className="panel output">
          <div className="tabs" role="tablist">
            {[
              ["preview", "内容预览"],
              ["map", "交互路线"],
              ["sources", "参考来源"],
              ["downloads", "下载文档"],
            ].map(([key, label]) => (
              <button
                key={key}
                role="tab"
                aria-selected={tab === key}
                className={tab === key ? "active" : ""}
                onClick={() => setTab(key)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="output-body">
            {!job.deliverable ? (
              <div className="working-preview">
                <span className="large-icon">
                  {["failed", "interrupted"].includes(job.status) ? (
                    <CircleAlert size={32} />
                  ) : (
                    <BookOpen size={32} />
                  )}
                </span>
                <h2>
                  {["failed", "interrupted"].includes(job.status)
                    ? "等待解决后继续研究"
                    : job.status === "completed"
                      ? "知识库更新完成"
                      : job.kind === "kb" ? "正在采集资料" : "正在生成文档"}
                </h2>
                <p>
                  {["failed", "interrupted"].includes(job.status)
                    ? "已完成的步骤已保存。解决提示的问题后，可继续生成。"
                    : job.status === "completed"
                      ? "本次采集的网页资料已进入知识库。"
                      : job.kind === "kb" ? "正在检索公开网页并保存资料来源。" : "检索、核验、创作。内容通过审阅后将在这里显示。"}
                </p>
                {job.outline && (
                  <div className="outline-preview">
                    <span>研究大纲</span>
                    {job.outline.themes.map((theme) => (
                      <div key={theme}>
                        <Check size={14} />
                        {theme}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            ) : tab === "preview" ? (
              <Preview job={job} />
            ) : tab === "map" ? (
              <Suspense fallback={<Loading />}>
                <RouteMap jobId={id} />
              </Suspense>
            ) : tab === "sources" ? (
              <Sources sources={job.deliverable.sources} />
            ) : (
              <Downloads job={job} />
            )}
          </div>
        </section>
      </div>
      {job.status === "completed" && job.kind === "document" && (
        <form
          className="panel revision"
          onSubmit={(event) => {
            event.preventDefault();
            revise.mutate();
          }}
        >
          <label htmlFor="revision">
            <Sparkles size={18} />
            修改这份文档
          </label>
          <div>
            <input
              id="revision"
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
              maxLength={2000}
              placeholder="例如：第二天换成适合亲子的路线……"
              required
              minLength={2}
            />
            <button className="primary" disabled={revise.isPending}>
              <Send size={16} />
              生成修改版
            </button>
          </div>
          <small>修改会创建一个新版本并复用资料缓存，原始产物保留。</small>
          {revise.isError && (
            <p className="error-text">{revise.error.message}</p>
          )}
        </form>
      )}
    </>
  );
}

function Blocks({ blocks }: { blocks: Block[] }) {
  return (
    <>
      {blocks.map((block, i) => (
        <p key={i}>
          {block.kind === "advice" && <strong>编辑建议：</strong>}
          {block.text}
          <sup>{block.source_ids.map((id) => `[${id}]`).join(" ")}</sup>
        </p>
      ))}
    </>
  );
}

function PhotoUpload({ jobId, poiId }: { jobId: string; poiId: string }) {
  const queryClient = useQueryClient();
  const upload = useMutation({
    mutationFn: async (file: File) => {
      const body = new FormData();
      body.append("file", file);
      const response = await fetch(`/api/jobs/${jobId}/pois/${poiId}/images`, { method: "POST", body });
      const result = await response.json();
      if (!response.ok) throw new Error(result.detail || "上传失败");
      return result;
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["job", jobId] });
      void queryClient.invalidateQueries({ queryKey: ["jobs"] });
    },
  });
  return <div className="photo-upload">
    <label>上传实拍图（用户提供并拥有授权，长边至少1200px，≤12MB）
      <input type="file" accept="image/jpeg,image/png,image/webp" disabled={upload.isPending}
        onChange={e => { const file = e.target.files?.[0]; if (file) upload.mutate(file); e.target.value = ""; }} />
    </label>
    {upload.isPending && <small>正在上传照片…</small>}
    {upload.isSuccess && <small>照片已接收，正在重新排版。</small>}
    {upload.isError && <p className="error-text">{upload.error.message}</p>}
  </div>;
}

function Preview({ job }: { job: Job }) {
  const document = job.deliverable!;
  return (
    <article className="document-preview">
      <h1>{job.outline?.title || job.prompt}</h1>
      {!!job.previews?.length && (
        <details className="slide-previews">
          <summary>
            <Layers3 size={16} />
            查看实际 PPT 页面 · {job.previews.length} 页
          </summary>
          {job.previews.map((url, index) => (
            <figure key={url}>
              <img src={url} loading="lazy" alt={`PPT 第 ${index + 1} 页`} />
              <figcaption>第 {index + 1} 页</figcaption>
            </figure>
          ))}
        </details>
      )}
      {!!document.spec.assumptions.length && (
        <div className="assumptions">
          <strong>编制假设</strong>
          {document.spec.assumptions.map((a, i) => (
            <p key={i}>{a}</p>
          ))}
        </div>
      )}
      {!["1.1", "1.3"].includes(document.prd_version || "") && !!document.warnings.length && (
        <details className="notice">
          <summary>审阅报告 · {document.warnings.length} 项待复核</summary>
          {document.warnings.map((warning, i) => (
            <p key={i}>{warning}</p>
          ))}
        </details>
      )}
      {document.days.map((day) => (
        <section key={day.day}>
          <h2>
            第 {day.day} 天 {day.date && `· ${day.date}`}
          </h2>
          <img
            className="preview-map"
            src={`/api/jobs/${job.id}/files/day-${day.day}.png`}
            alt={`第 ${day.day} 天路线图`}
          />
          <p>{day.stops.map((p) => p.name).join(" → ")}</p>
          {!!day.timeline?.length && <>
            <p>全日 {Math.floor((day.total_minutes || 0) / 60)} 小时 {(day.total_minutes || 0) % 60} 分，含交通、用餐与等候。</p>
            <table>
              <thead><tr><th>时间</th><th>地点</th><th>交通</th><th>备注</th></tr></thead>
              <tbody>{day.timeline.map((entry, i) => <tr key={i}>
                <td>{entry.start}–{entry.end}</td><td>{entry.place}</td><td>{entry.transport || "—"}</td><td>{entry.note}</td>
              </tr>)}</tbody>
            </table>
          </>}
          {!day.timeline?.length && day.meals.map((meal) => (
            <small key={meal}>{meal}　</small>
          ))}
        </section>
      ))}
      {document.pois.map((poi) => (
        <section key={poi.id}>
          <h2>
            <MapPin size={19} />
            {poi.name}
          </h2>
          {poi.images[0] && (
            <figure>
              <img
                src={`/api/jobs/${job.id}/files/${poi.images[0].local_path.split("/").pop()}`}
                alt={poi.name}
              />
              <figcaption>
                {poi.images[0].caption && `${poi.images[0].caption} · `}{poi.images[0].author} · {poi.images[0].license}
              </figcaption>
            </figure>
          )}
          {!poi.images.length && poi.photo_status && <div className="photo-placeholder">
            <strong>{poi.name} · {poi.photo_status}</strong>
            {poi.map_thumbnail && <img alt={`${poi.name}近景地图`} src={`/api/jobs/${job.id}/files/${poi.map_thumbnail.split("/").pop()}`} />}
            <PhotoUpload jobId={job.id} poiId={poi.id} />
          </div>}
          <Blocks blocks={poi.blocks} />
          <table>
            <tbody>
              {poi.facts.map((fact, i) => (
                <tr key={i}>
                  <th>
                    {(
                      {
                        ticket: "票价",
                        hours: "开放时间",
                        booking: "预约",
                        address: "地址",
                        history: "历史",
                        culture: "文化",
                        intro: "介绍",
                      } as Record<string, string>
                    )[fact.field] || fact.field}
                  </th>
                  <td>
                    {fact.field === "hours" && poi.hours
                      ? `${poi.hours.open}–${poi.hours.close}${poi.hours.last_entry ? `；${poi.hours.last_entry}停止入场` : ""}${poi.hours.closed_days.length ? `；${poi.hours.closed_days.map(day => `周${"一二三四五六日"[day]}闭馆`).join("、")}` : ""}${poi.hours.note ? `；${poi.hours.note}` : ""}`
                      : fact.value}
                    {fact.freshness_note && <small>{fact.freshness_note}</small>}
                    <small>
                      来源发布 {fact.published_at || "未载明"} / 访问 {fact.verified_at} {fact.source_ids.join(", ")}
                    </small>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
      {document.chapters.map((chapter, i) => (
        <section key={i}>
          <h2>{chapter.title}</h2>
          <span className="subtle">{chapter.period}</span>
          {chapter.images?.[0] && (
            <figure>
              <img
                src={`/api/jobs/${job.id}/files/${chapter.images[0].local_path.split("/").pop()}`}
                alt={chapter.title}
              />
              <figcaption>
                {chapter.images[0].author} · {chapter.images[0].license}
              </figcaption>
            </figure>
          )}
          <Blocks blocks={chapter.blocks} />
        </section>
      ))}
      {[['food', '餐饮安排'], ['budget', '预算'], ['transport', '到达与市内交通'], ['notice', '注意事项']].map(([category, title]) => {
        const items = document.practical?.filter(item => item.category === category) || [];
        return !!items.length && <section key={category}><h2>{title}</h2><table>
          <thead><tr><th>项目</th><th>安排与参考</th></tr></thead>
          <tbody>{items.map((item, i) => <tr key={i}><th>{item.label}</th><td>{item.value}<small>{item.estimated && '参考，待核实 '}{item.source_ids.map(id => `[${id}]`).join(' ')}</small></td></tr>)}</tbody>
        </table></section>;
      })}
      {!!document.alternatives?.length && <section><h2>备选与未纳入景点</h2><ul>{document.alternatives.map((item, i) => <li key={i}><strong>{item.name}：</strong>{item.reason}{item.replace_day && `；时段：${item.replace_day}`}</li>)}</ul></section>}
      {!!document.reader_checks?.length && <section><h2>出行前须自行核实</h2><ul>{document.reader_checks.map((item, i) => <li key={i}>{item.poi}：{item.text} {item.targets?.length ? item.targets.map(target => <a key={target.poi} href={target.url} target="_blank" rel="noreferrer">{target.poi}官方渠道 </a>) : <a href={item.official_url} target="_blank" rel="noreferrer">官方渠道</a>}</li>)}</ul></section>}
      <div className="document-end">— 价格与开放时间以官方渠道为准 —</div>
    </article>
  );
}

function Sources({ sources }: { sources: Source[] }) {
  return (
    <div className="sources">
      <h2>参考来源</h2>
      <p className="subtle">
        共 {sources.length} 个来源。L1 官方 · L2 百科与地图数据 · L3 平台参考
      </p>
      {sources.map((source) => (
        <a
          key={source.id}
          href={source.url}
          target="_blank"
          rel="noreferrer"
          className="source-card"
        >
          <span className="source-number">{source.id}</span>
          <div>
            <strong>{source.title}</strong>
            <small>{source.url}</small>
            <span>来源发布 {source.published_at || "未载明"} / 访问 {source.fetched_at.slice(0, 10)}</span>
          </div>
          <span className="source-level">{source.level}</span>
          <ArrowRight size={15} />
        </a>
      ))}
    </div>
  );
}

function Downloads({ job }: { job: Job }) {
  if (job.status !== "completed") return <div className="empty">任务完成并通过审阅后可下载文档。</div>;
  const artifacts = { json: `/api/jobs/${job.id}/files/deliverable.json`, ...job.artifacts };
  return (
    <div className="downloads">
      <h2>把研究成果带走</h2>
      <p className="subtle">
        PPT 与 Word 由同一份结构化内容生成，来源与审阅说明一并保留。
      </p>
      {[
        ["pptx", "演示文稿", "16:9 · 适用于分享与汇报"],
        ["docx", "研究文档", "完整正文 · 适用于阅读与打印"],
        ["json", "结构化数据", "包含事实、来源与审阅结果"],
      ].map(
        ([key, title, note]) =>
          artifacts[key as keyof typeof artifacts] && (
            <a
              className="download-card"
              key={key}
              href={artifacts[key as keyof typeof artifacts]}
              download
            >
              <span className={`file-badge ${key}`}>{key.toUpperCase()}</span>
              <div>
                <h3>{title}</h3>
                <p>{note}</p>
              </div>
              <ArrowDownToLine size={21} />
            </a>
          ),
      )}
    </div>
  );
}

function KnowledgeBase({ onCreated }: { onCreated: (id: string) => void }) {
  const [city, setCity] = useState("上海");
  const [lookup, setLookup] = useState("上海");
  const [cityError, setCityError] = useState("");
  const coverage = useQuery({
    queryKey: ["coverage", lookup],
    queryFn: () =>
      api<{ chunks: number; sources: string[] }>(
        `/kb/coverage?city=${encodeURIComponent(lookup)}`,
      ),
    retry: false,
  });
  const ingest = useMutation({
    mutationFn: () =>
      api<{ job_id: string }>("/kb/ingest", {
        method: "POST",
        body: JSON.stringify({ city }),
      }),
    onSuccess: (data) => onCreated(data.job_id),
  });
  return (
    <>
      <PageTitle
        eyebrow=""
        title="资料库"
        description="从公开网页采集资料，保留来源与访问日期。时效性内容需要定期重新核验。"
      />
      <div className="panel kb-panel">
        <h2>
          <Database size={20} />
          城市资料
        </h2>
        <form
          className="kb-search"
          noValidate
          onSubmit={(event) => {
            event.preventDefault();
            const value = city.trim();
            if (!value) {
              setCityError("请输入城市名称");
              return;
            }
            setCityError("");
            setLookup(value);
          }}
        >
          <label className="sr-only" htmlFor="city">
            城市名称
          </label>
          <input
            id="city"
            value={city}
            onChange={(e) => {
              setCity(e.target.value);
              if (cityError) setCityError("");
            }}
            maxLength={80}
            required
            placeholder="输入城市名称"
          />
          <button className="secondary">查看资料</button>
          <button
            className="primary"
            type="button"
            disabled={!city.trim() || ingest.isPending}
            onClick={() => ingest.mutate()}
          >
            <Plus size={16} />
            采集更新
          </button>
        </form>
        {cityError && <div className="notice error" role="alert">{cityError}</div>}
        {coverage.isPending ? (
          <Loading />
        ) : coverage.isError ? (
          <div className="notice error">{coverage.error.message}</div>
        ) : (
          <>
            <div className="coverage-grid">
              <div>
                <span>研究城市</span>
                <strong>{lookup}</strong>
              </div>
              <div>
                <span>已保存资料片段</span>
                <strong>{coverage.data.chunks}</strong>
              </div>
              <div>
                <span>独立来源</span>
                <strong>{coverage.data.sources.length}</strong>
              </div>
            </div>
            {coverage.data.sources.length ? (
              coverage.data.sources.map((url) => (
                <a
                  className="kb-source"
                  href={url}
                  key={url}
                  target="_blank"
                  rel="noreferrer"
                >
                  {url}
                  <ArrowRight size={14} />
                </a>
              ))
            ) : (
              <div className="empty">
                该城市还没有采集记录，可以开始第一次研究。
              </div>
            )}
          </>
        )}
        {ingest.isError && (
          <div className="notice error">{ingest.error.message}</div>
        )}
        <p className="subtle">
          资料保存在本机，门票、开放时间等信息请在出行前再次核实。
        </p>
      </div>
    </>
  );
}

function Loading() {
  return (
    <div className="loading">
      <LoaderCircle size={22} className="spin" />
      正在加载……
    </div>
  );
}

export default App;
