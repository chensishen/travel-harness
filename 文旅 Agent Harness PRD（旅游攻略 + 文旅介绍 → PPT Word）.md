# 文旅 Agent Harness · 产品需求文档（PRD）

版本 v1.0 ｜ 日期 2026-10-27 ｜ 读者：开发 Agent / 工程师

## 0. 给执行 Agent 的阅读说明

- 本文即任务书。按「第 15 章 里程碑」顺序实施，每个里程碑完成后对照「第 14 章 验收标准」自检。
- 【必须】为硬性要求；【建议】可按实际情况调整；【待确认】需先向用户提问再动手。
- 所有 API Key 只通过环境变量读取，用户会在对话中提供；禁止写入代码、仓库、日志或产物。
- 核心原则：**事实可溯源、路线可行、产物可交付**。宁可标注「暂未核实」，也不编造。

## 1. 产品概述

**一句话**：用户输入一句自然语言需求，系统自动检索、核验、规划、配图，输出可直接使用的 PPT 与 Word。

| 任务类型               | 示例输入       | 核心要求                            |
| ------------------ | ---------- | ------------------------------- |
| 旅游攻略 travel_guide  | 做一个上海的旅游攻略 | 图文并茂；真实合理；路线规划合理且有路线图；票价与景点介绍真实 |
| 文旅介绍 culture_intro | 做一些苏州的文旅介绍 | 图文并茂；真实合理；详细讲清历史渊源与文化价值         |

- **输出**：每个任务同时产出 .pptx 与 .docx，内容同源（同一份结构化 JSON 渲染成两种格式）。
- **目标用户**：个人出行者、做分享或汇报的文旅从业者、学生。
- **非目标（v1）**：订票支付、实时拥挤度、多城市联程（v1 为单城市，可含周边一日游）。

## 2. 本机环境与技术选型（来自「本机配置.md」）

- **容器**：Docker 运行 Milvus（standalone）与 Attu（可视化检查）；提供 docker-compose.yml，Milvus 端口 19530；OSRM 作为可选服务加入同一 compose。
- **Python**：conda 环境，Python 3.12.11（原文写作「py12.11」，按 3.12.11 理解）【待确认】。
- **Embedding**：Qwen/Qwen3-VL-Embedding-8B（多模态向量模型），base_url 为 <https://api.siliconflow.cn/v1，调用> /embeddings。以文本入库为主；是否支持图片输入需 M0 实测，支持则用于图文相关性初筛。向量维度以启动时探测到的实际返回维度为准，据此建 Milvus 集合。
- **Reranker**：Qwen/Qwen3-Reranker-8B，base_url 为 <https://api.siliconflow.cn/v1，调用> /rerank。
- **大语言模型**：workbuddy-ai/deepseek-v4.1-flash，base_url 为 <http://127.0.0.1:3425/v1（本机> OpenAI 兼容网关）。是否支持图片输入需 M0 实测。若该模型不支持图片输入，则「图文相关性校验」降级为基于图片元数据与标题的文本校验。
- **环境变量**：LLM_API_KEY（本机网关）、SILICONFLOW_API_KEY（embedding 与 reranker 共用）；base_url 与模型名同样走环境变量（LLM_BASE_URL、LLM_MODEL、EMBED_BASE_URL、EMBED_MODEL、RERANK_MODEL）。Key 的具体值由用户在本机设置，不得写入本文档、代码或仓库；地图使用开源方案、联网搜索使用本机 MCP 服务，均不需要额外 Key；提供 .env.example，变量名可按用户实际调整。
- **知识库数据**：全部来自网络检索与抓取（见第 6 章），不使用预置数据。
- **前端**：React（第 11 章）。**后端**：因 conda/Python 生态，采用 FastAPI。

## 3. 用户故事与交互范围

1. 用户输入一句话 → 系统识别任务类型（travel_guide / culture_intro），抽取参数：城市、天数、人数、偏好（亲子、历史、美食、摄影）、预算、出行日期或季节、出发地。
2. 缺失信息时**不阻塞**：使用默认值并在产物首页「编制假设」中声明（攻略默认 3 天 2 晚；文旅介绍无天数）。
3. 前端可展示并允许修改解析出的参数；可选在「大纲」阶段人工确认（默认自动通过）。
4. 生成过程实时展示节点进度；完成后可在线预览，并下载 PPT、Word。
5. 用户可对产物提出修改（如「第二天换成亲子路线」），系统仅重跑受影响节点。

## 4. 系统架构

```
React SPA（Vite + TypeScript）
   │  REST + SSE
FastAPI ── Job Manager（异步任务 / 事件流）
   │
Agent Harness（状态机编排）
 ├─ Router → Planner → Researcher → Verifier → Writer
 ├─ RoutePlanner → Illustrator → Reviewer → Assembler
 ├─ Tools: mcp_search / mcp_fetch / geo+routing(OSM) / image_fetch / llm / embed / rerank
 └─ Stores: Milvus（向量+元数据）· SQLite（任务/缓存）· 本地文件（图片/产物）
Renderers: python-pptx / python-docx（同源 JSON）
```

- 编排【建议】用 LangGraph 或自写轻量状态机；每个节点的输入输出都是 Pydantic 模型，可重放、可单独测试。
- 每次任务生成唯一 job_id，所有中间产物（检索结果、FactRecord、图片、路线）落盘，便于复现与排错。

## 5. Agent 工作流（节点定义）

1. **Router**：意图识别与参数抽取，输出 TaskSpec。
2. **Planner**：生成大纲与检索问题清单。攻略：每日主题与候选 POI；介绍：按历史分期与文化主题分章。
3. **Researcher**：先查 Milvus；若某 POI 缺少「票价 / 开放时间 / 简介 / 历史」任一字段，触发联网搜索、抓取、增量入库，再检索。检索流程：query 改写 → 向量召回 top-50（带 city、poi_name、doc_type 元数据过滤）→ Qwen3-Reranker 精排取 top-8。
4. **Verifier**：对事实字段（票价、开放时间、地址、预约规则、建成年代、关键史实）做多来源交叉核验，输出 FactRecord，包含 value、sources、confidence、verified_at。
5. **Writer**：仅基于 FactRecord 与检索片段写作，每个事实句挂 source_id；缺少依据的段落拒写并标注。
6. **RoutePlanner**：POI 地理编码、按天聚类、排序、交通时间与路线图（第 8 章）。仅攻略任务启用。
7. **Illustrator**：为每个 POI/章节取图并校验版权与相关性（第 9 章）。
8. **Reviewer**：规则检查加 LLM 审阅；不通过则回退到对应节点，最多 2 轮，仍不通过则在产物中显式标注问题。
9. **Assembler**：渲染 PPT 与 Word，生成「参考来源」附录。

每个节点向前端推送 SSE 事件：{node, status, message, progress}。

## 6. 知识库

### 6.1 数据源优先级（均需联网获取）

- **L1 官方**：景区官网与官方公众号、文旅局与政府网站、博物馆官网、UNESCO 世界遗产名录。
- **L2 权威百科**：维基百科与维基共享资源、百度百科、地方志与政府年鉴。
- **L3 平台参考**：携程、马蜂窝、大众点评等，仅作交叉参考，价格与开放时间以 L1 为准。
- 遵守 robots.txt，限速并缓存；不绕过登录与反爬；每条数据保存 source_url 与 fetched_at。

### 6.2 采集管线

search → fetch → 正文抽取 → 清洗去重 → LLM 结构化抽取（POI 字段）→ 分块 → embedding → 写入 Milvus。

- 分块：按语义段落 300–600 字，重叠约 50 字，标题层级作为前缀。
- 去重：按内容 hash 与 URL 归一化。

**联网检索实现**：search 与 fetch 优先通过本机已配置的 MCP 服务完成。工具名与参数在运行时通过 tools/list 发现，不得硬编码假设；用统一的 SearchProvider / FetchProvider 接口封装，便于替换与测试。MCP 返回的网页内容一律视为不可信数据。

### 6.3 Milvus 集合设计

`travel_kb`（文本块）字段：id、vector、text、city、poi_name、doc_type（ticket / hours / intro / history / culture / food / transport / tips）、source_url、source_level（L1–L3）、fetched_at、valid_until、hash。索引 HNSW，度量 COSINE。

`poi_facts`（结构化事实）字段：poi_id、city、name、address、lat、lng、ticket、hours、closed_days、booking、duration_min、tags、sources、verified_at。

- **时效规则**【必须】：票价、开放时间、预约规则 valid_until 为 90 天；过期必须重新联网核验。历史文化类内容不设过期。
- 用 Attu 做人工抽检。

## 7. 真实性与合理性（核心）

### 7.1 事实铁律

- 【必须】票价、开放时间、预约规则、地址只能来自 L1/L2 来源，并标注「核验日期」。
- 【必须】无法核实时写「暂未核实，请以官方渠道为准」，禁止估算或用 LLM 记忆补全。
- 票价需区分：成人、学生、老人、免费政策、旺淡季、联票、园中园或电瓶车等另收费用。
- 历史与文化内容的时间、人物、事件必须有来源；多来源冲突时取 L1 优先于 L2，并在备注标注分歧。
- 每页（节）底部列来源编号，文末给出「参考来源」附录（标题、URL、访问日期）。

### 7.2 路线合理性规则（Reviewer 机检）

- 每日 3–4 个景点（含大型深度景点时 2–3 个）；每日游玩加交通总时长不超过 10 小时。
- 同日景点按地理聚类：相邻点交通不超过 40 分钟，一天内跨区往返不超过 1 次。
- 校验开放时间与闭馆日（如博物馆周一闭馆）；需预约的景点标注并给出预约提示。
- 预留午餐、晚餐时间窗口；首日考虑抵达，末日考虑离城交通。
- 交通时间来自 OSRM 路径结果；公共交通为可配置的折算值并标注「估算」，均不由 LLM 凭空给出。

### 7.3 反幻觉措施

- LLM 温度 ≤ 0.3；所有中间结果使用 JSON Schema 校验。
- Writer 的每个数字、年代、专名必须能在 FactRecord 或检索片段中找到；Reviewer 做「句子 → 来源」回查，抽检比例不低于 30%，任何未通过项必须修正或删除。

## 8. 路线规划与路线图

### 8.1 规划流程

POI 地理编码（结果缓存）→ 按天聚类 → 日内小规模排序（≤5 点，暴力或 OR-Tools）→ 相邻点调用 OSRM（步行/驾车，取 duration 与 distance），公共交通按 8.3 折算→ 按 7.2 规则校验 → 不通过则调整分组或剔除景点后重排。

### 8.2 路线图产物

- **总览图**：全行程各天不同颜色，景点带序号，标注住宿区域。
- **每日路线图**：序号标记、折线、交通方式图例、每段耗时与距离。
- 输出 PNG（不小于 1600×1000）供 PPT 与 Word 使用；前端交互版用 Leaflet 渲染同一份 GeoJSON，保证图文一致。

### 8.3 地图方案（不使用高德，全开源替代）

- **地理编码**：以 Nominatim（OSM）为主，用 Wikidata / 维基百科坐标及景区官网地址交叉校验；限速 ≤1 次/秒，设置 User-Agent，结果落盘缓存。两个来源坐标相差 >500 m 时标记为待复核。
- **坐标系**：全程 WGS84，底图也用 OSM，无需 GCJ-02 转换；不得混用高德或百度坐标。
- **路径规划**：【建议】用 Docker 自建 OSRM（步行、驾车两个 profile，使用目标城市的 OSM 数据，如 Geofabrik 或 BBBike 的城市提取），避免公共演示服务的限流与不稳定；M0 冒烟阶段可先用 OSRM 公共服务。
- **公共交通**：开源方案没有可靠的公交/地铁路径规划。v1 做法：耗时 = 驾车耗时 × 系数 + 候车换乘分钟数（两个参数默认值可配置，须在 README 说明），并从 OSM 提取最近地铁站作为参考；产物中统一标注「公共交通时间为估算」。
- **底图与渲染**：OSM 标准瓦片，遵守瓦片使用政策（限速、本地缓存、标注 © OpenStreetMap contributors）；用 Playwright 无头渲染前端同一份 Leaflet 页面并截图为 PNG，保证与前端一致；备选 staticmap 库。

### 8.4 降级与诚实标注

OSRM 不可用时退化为直线距离 × 绕行系数估算。凡是估算得到的交通时间，必须在产物与 Reviewer 报告中标注「估算」，不得与实测值混写。

## 9. 配图

- **来源优先级**：Wikimedia Commons（CC 授权）→ 官方开放素材 → 官方宣传图（注明出处）；禁用来源与授权不明的图片。
- 记录 image_meta：url、author、license、source_page；每张图的图注含出处。
- **多模态校验**：用 deepseek-flash 判断图片与 POI 是否相符、是否清晰、有无水印遮挡；不合格则换图，最多 3 次。
- 每个 POI 至少 1 张，重点景点 2–3 张；找不到合适图片时使用地图截图或占位并标注，**禁止用 AI 生成图冒充实景**。
- 图片本地缓存，长边压缩到 ≤1920px。

## 10. 产物规格

### 10.1 通用

- 中文；PPT 为 16:9；统一主题（主色可配置，字体 思源黑体，微软雅黑作为回退）。
- PPT 每页正文不超过约 120 字，图文比例均衡；Word 为正式排版，含目录、页眉页脚、图注、表格。

### 10.2 旅游攻略

**PPT 页结构**

1. 封面
2. 编制说明与假设（编制日期、核验日期、天数、预算、偏好）
3. 行程总览（总路线图 + 每日主题）
4. 出行基础信息（最佳季节、到达交通、市内交通、住宿区域建议）
5. 每日：路线图页 + 时间轴页
6. 景点详情页：大图、简介、亮点、门票表（含优惠政策）、开放时间、预约方式、建议时长、到达方式
7. 美食与特色体验
8. 预算表
9. 注意事项与应急信息（天气、预约、官方咨询电话）
10. 参考来源附录

**Word 结构**：封面 → 目录 → 编制说明 → 总览与总路线图 → 分日章节（日路线图、行程表、景点小节）→ 美食 → 预算 → 小贴士 → 参考来源。

### 10.3 文旅介绍

**PPT / Word 结构**（PPT 约 15–25 页）：封面 → 城市名片（位置、气质、一句话定位）→ 历史脉络（时间轴图，按时期分段，标关键事件）→ 文化价值专题（遗产、非遗、建筑、文学艺术、人物等，每项按「是什么 → 渊源 → 价值 → 今日如何体验」展开）→ 代表景点或遗址专题（每个 1–2 页：历史渊源、形制特色、文化价值、保护现状、参观信息概要）→ 文化体验推荐 → 结语 → 参考文献。

**内容深度要求**：每个主题必须讲清时间、人物、事件、影响；价值论述须有出处；避免空泛形容词堆砌。

### 10.4 实现

- python-pptx + python-docx，先产出结构化 JSON，再分别渲染。
- 【建议】渲染后用 LibreOffice 转 PDF 并转图片做版面自检：文字溢出自动缩字号或拆页，图片不得变形。

## 11. React 前端

**技术栈**：React 18 + TypeScript + Vite；Tailwind + shadcn/ui；TanStack Query；Zustand；react-leaflet；SSE 使用 EventSource 或 fetch-event-source。

**页面**

1. **首页**：一句话输入框 + 示例 chips（如「上海 3 日游攻略」「苏州文旅介绍」）+ 高级参数折叠面板（天数、人数、偏好、预算、日期）。
2. **任务页**：左侧 Agent 步骤时间线（节点、状态、耗时、可展开日志、来源数）；右侧实时预览，大纲逐步被填充。
3. **结果页**：Tab 切换「预览 / 交互路线图 / 来源 / 下载」。PPT 预览用服务端转出的页面缩略图，Word 用 HTML 预览；提供 PPT 与 Word 下载按钮。
4. **历史任务**：列表、重新打开、复制为新任务。
5. **知识库（轻量）**：查看城市覆盖度、触发更新，深度检查跳转 Attu。

**体验要求**：失败节点可单独重试；移动端可用；加载、空态、错误态齐全；修改指令（revise）只重跑受影响节点。

## 12. 后端 API 与数据模型

**API**

- POST /api/jobs：{prompt, options?} → {job_id}
- GET /api/jobs/{id}/events：SSE 事件流
- GET /api/jobs/{id}：状态、TaskSpec、大纲、警告
- POST /api/jobs/{id}/revise：{instruction}
- GET /api/jobs/{id}/artifacts：pptx、docx、预览图地址
- GET /api/jobs/{id}/sources：来源清单
- POST /api/kb/ingest：{city, poi?}；GET /api/kb/coverage?city=

**核心模型（Pydantic，示意）**

```python
class TaskSpec: type: Literal['travel_guide','culture_intro']; city; days: int|None; travelers; interests: list[str]; budget_level; season_or_dates; origin; assumptions: list[str]
class FactRecord: field; value; sources: list[SourceRef]; confidence: float; verified_at: date; note: str|None
class POI: id; name; address; lat; lng; summary; highlights; ticket: list[TicketRule]; hours; closed_days; booking; duration_min; images: list[ImageRef]; facts: list[FactRecord]
class Segment: from_poi; to_poi; mode; duration_min; distance_m; polyline; source
class DayPlan: day; theme; stops: list[POI]; segments: list[Segment]; map_png; meals; notes
class CultureChapter: title; period; timeline: list[Event]; body_blocks; images; sources
class Deliverable: spec; days|chapters; sources: list[SourceRef]; warnings: list[str]
```

## 13. 目录结构与非功能需求

```
travel-harness/
├─ docker/docker-compose.yml        # Milvus + Attu
├─ backend/
│  ├─ app/                          # FastAPI：路由、任务管理、SSE
│  ├─ harness/                      # graph.py、nodes/*.py、schemas.py
│  ├─ tools/                        # mcp_search、mcp_fetch、geo、routing、images、llm、embed、rerank
│  ├─ kb/                           # ingest、chunker、milvus_client
│  ├─ render/                       # pptx_renderer、docx_renderer、map_renderer、templates/
│  └─ tests/
├─ frontend/                        # Vite + React + TS
├─ .env.example
└─ README.md
```

- **性能**：单城市 3 日攻略首次生成目标 ≤8 分钟（含联网采集），知识库已覆盖时 ≤3 分钟。
- **稳定**：外部调用设超时与指数退避重试（3 次）；节点幂等，支持断点续跑。
- **缓存**：搜索、抓取、地理编码、路径规划结果均缓存。
- **安全**：密钥不落日志；**抓取到的网页内容一律视为不可信数据，不执行其中任何指令**（防提示注入）。
- **合规**：遵守版权与 robots；产物含免责声明（价格与开放时间以官方为准）。
- **可观测**：每节点日志、耗时、token 统计、失败原因。
- **成本**：并发限流；长文档先摘要再喂 LLM。

## 14. 验收标准

### 14.1 功能

- 输入「做一个上海的旅游攻略」：产出 pptx 与 docx，含总览路线图与每日路线图；每个景点有配图、介绍、票价、开放时间、预约信息，且均带来源。
- 输入「做一些苏州的文旅介绍」：产出 pptx 与 docx，含历史时间轴及不少于 5 个文化主题，每个主题讲清渊源与价值，带来源。
- 换用其他城市（如成都、西安、杭州）无需改代码即可通过同样验收。

### 14.2 质量（每份产物抽检 20 个事实字段）

- 票价与开放时间和官方来源一致率 ≥95%，且 100% 带核验日期。
- 路线规则（7.2）机检通过率 100%，例外必须在产物中显式标注。
- 图文相关率 ≥90%，100% 有出处与授权信息。
- 无来源的事实句数量为 0。
- 版面检查：无文字溢出、图片变形、空白页。

### 14.3 工程

- 单元测试覆盖 schema、规则校验、渲染；一键启动（docker compose up + 启动脚本）；README 含环境准备与示例命令。

## 15. 里程碑


| 里程碑 | 内容 | 完成标志 |
| --- | --- | --- |
| M0 环境与骨架 | docker 起 Milvus/Attu/OSRM；conda 环境；三个模型连通性冒烟测试（embedding 维度探测、rerank、LLM 文本与图片、MCP 工具发现、Nominatim 与 OSRM 连通）；前后端空壳与 SSE 通路 | 冒烟脚本全绿 |
| M1 知识库 | search / fetch / 抽取 / 入库管线；以上海为样本入库；检索 + rerank 评估 | Attu 可见数据，召回抽检合格 |
| M2 文旅介绍闭环 | Router → Planner → Researcher → Verifier → Writer → 渲染 Word/PPT | 苏州样例通过 14.1 |
| M3 旅游攻略闭环 | 地理编码、路线规划、路线图、规则机检、票价核验 | 上海样例通过 14.1 |
| M4 配图与版式 | 图片获取、校验、出处；版面自检；主题模板 | 通过版面检查 |
| M5 前端完整 | 参数面板、进度时间线、交互路线图、预览与下载、历史 | 端到端可用 |
| M6 评测与加固 | 多城市回归、抽检报告、性能与错误处理、文档 | 通过 14.2 与 14.3 |

每个里程碑交付：可运行代码 + 测试 + 简短变更说明。

## 16. 待确认事项与默认假设

1. **Python 版本**：默认 3.12.11。
2. **deepseek-flash**：模型 ID 与 base\_url 已确定（见第 2 章）；M0 实测是否支持图片输入与结构化 JSON 输出，不支持则按第 2 章降级。
3. **联网搜索服务**：使用本机已配置的 MCP 服务。Agent 在 M0 读取本机 MCP 配置，列出可用工具与参数并记录到 docs/mcp\_tools.md；若该服务不含网页抓取，则用 httpx + trafilatura 补充。找不到配置或工具时再向用户询问，不得自行猜测。
4. **地图服务**：已确定不使用高德，采用 OSM 开源方案（Nominatim + OSRM + Leaflet），公共交通时间为折算估算，见 8.3、8.4。
5. **目标城市与出发地**：默认无出发地，交通部分只写到达方式概览。
6. **视觉风格**：默认简洁明亮，主色可配置。
7. **部署形态**：默认仅本机运行，无登录与多用户。
8. **图片版权口径**：默认仅使用 CC 授权或官方开放来源，用途为个人学习与分享。
