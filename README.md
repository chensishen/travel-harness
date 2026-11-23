# Travel Harness

Travel Harness 是知行文旅 Agent 的执行底座：把旅游攻略或城市文化研究整理成同源的
PPTX、DOCX、HTML PPT 与结构化 JSON。

前端使用 React 18、TypeScript 和 Vite；后端使用 FastAPI、Python 3.12.11、
SQLite 与 Milvus。任务由可恢复的多节点流程执行，关键事实必须经过来源核验，
Office 文件必须通过真实转换和版面检查后才能发布。

> **当前状态**：项目是可运行的首版，尚未完成 PRD 第 14 章的全部交付验收。
> 模型密钥是可运行生成链的前置条件；缺少密钥时界面、历史与知识库可打开，
> 但生成会明确失败，不会用预置内容伪造成功。

仓库地址：<https://github.com/chensishen/travel-harness>

## 总览

```mermaid
flowchart LR
    USER["用户需求"] --> UI["React 工作台"]
    UI -->|"创建 / 重试 / 修改任务"| API["FastAPI"]
    API --> DB[("SQLite<br/>任务、事件、检查点")]
    API --> HARNESS["Harness 调度器"]

    HARNESS --> FLOW["多节点内容流水线"]
    HARNESS <--> DB
    HARNESS --> LLM
    HARNESS --> MCP
    HARNESS --> KB
    HARNESS --> GEO

    subgraph PROVIDERS["能力层"]
        LLM["LLM / Embedding / Rerank"]
        MCP["MCP 搜索与网页抓取"]
        KB[("Milvus 知识库")]
        GEO["OSM / OSRM / Commons"]
    end

    FLOW --> GATE["事实、路线与交付门禁"]
    GATE -->|"通过"| OUTPUT["PPTX + DOCX + HTML PPT + JSON"]
    GATE -->|"资料不足"| FLOW
    OUTPUT --> UI
```

## 生成流程

旅游攻略与文旅介绍采用同一条内容生产链。知识库导入任务只执行前三个节点。

```mermaid
flowchart TD
    START["用户需求"] --> ROUTER["Router<br/>抽取城市、任务类型与约束"]
    ROUTER -->|"知识库任务"| KB_RESEARCH["Researcher<br/>抓取、清洗、分块、向量入库"]
    KB_RESEARCH --> KB_DONE["Done<br/>资料已入库"]

    ROUTER -->|"旅游攻略 / 文旅介绍"| PLANNER["Planner<br/>候选池、策略与大纲"]
    PLANNER --> RESEARCHER["Researcher<br/>搜索、抓取、索引与补查"]
    RESEARCHER --> VERIFIER["Verifier<br/>来源等级与关键事实核验"]
    VERIFIER --> WRITER["Writer<br/>生成有引用的正文"]
    WRITER --> ROUTE["RoutePlanner<br/>景点排程、交通与真实路线"]
    ROUTE --> ILLUSTRATOR["Illustrator<br/>授权实拍、封面与相似度检查"]
    ILLUSTRATOR --> REVIEWER["Reviewer<br/>逐段检查事实与引用"]
    REVIEWER --> ASSEMBLER["Assembler<br/>Office 排版、转换与版面校验"]
    ASSEMBLER --> DONE["Done<br/>发布交付文件"]

    VERIFIER -.->|"关键资料不足"| RESEARCHER
    ROUTE -.->|"路线或开放信息不足"| RESEARCHER
    REVIEWER -.->|"仍有未溯源事实"| REVIEWER
    REVIEWER -.->|"三轮后仍失败"| BLOCKED["阻止发布"]
    ASSEMBLER -.->|"转换或版面检查失败"| BLOCKED
```

各节点的职责：

| 节点 | 主要输出 | 失败或回退方式 |
| --- | --- | --- |
| `Router` | 城市、任务类型、天数、人数、预算、日期与偏好 | 城市不明确时拒绝编造并要求补充 |
| `Planner` | 候选池、内容策略、景点与章节大纲 | 偏好或目的地变化时从这里重跑 |
| `Researcher` | 来源、正文、分块、向量与缓存 | 接收定向补查请求 |
| `Verifier` | 来源等级、引文、日期、数字与字段核验 | 资料不足时回到 `Researcher` |
| `Writer` | 带来源编号的正文与实用信息 | 仅文字修改时可从这里重跑 |
| `RoutePlanner` | 每日顺序、真实路网、时间预算与 GeoJSON | 行程不可执行时回到 `Researcher` |
| `Illustrator` | 合规实拍、封面与图片来源记录 | 无合格图片时明确降级 |
| `Reviewer` | 正文审计、修正与删除记录 | 无法溯源的正文不能发布 |
| `Assembler` | PPTX、DOCX、HTML PPT、PDF 预览与 JSON | 转换或检查失败时阻止出件 |

## 任务状态与恢复

任务状态、事件编号和检查点保存在 SQLite。SSE 断线后可以按事件编号续传，
前端同时保留轮询兜底。

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running
    running --> completed
    running --> failed
    running --> interrupted
    failed --> queued: 重试当前节点
    interrupted --> queued: 重启后恢复
    completed --> [*]
```

修改任务会创建新版本并保留旧文件。`Revision` 先判断影响范围，再从最早受影响的节点重跑：

```mermaid
flowchart LR
    REV["修改要求"] --> CHECK{"影响范围"}
    CHECK -->|"目的地或任务类型"| R["Router"]
    CHECK -->|"天数、景点、偏好或新事实"| P["Planner"]
    CHECK -->|"仅现有正文改写"| W["Writer"]
    CHECK -->|"仅替换配图"| I["Illustrator"]
    CHECK -->|"仅主题颜色"| A["Assembler"]
    R --> RUN["继续执行后续节点"]
    P --> RUN
    W --> RUN
    I --> RUN
    A --> RUN
```

## 事实与交付门禁

```mermaid
flowchart LR
    SEARCH["搜索与候选资料"] --> FETCH["URL、robots、重定向与抓取检查"]
    FETCH --> LEVEL["来源分级<br/>L1 / L2 / L3"]
    LEVEL --> FACT["引文、数字、日期、开放时间与地址核验"]
    FACT --> PRD{"PRD v1.1 / v1.3<br/>关键参观字段 ≥ 75%"}
    PRD -->|"通过"| ROUTE["路线、时长、交通与午餐检查"]
    PRD -->|"未通过"| RETRY["定向补查或停止发布"]
    ROUTE --> OFFICE["真实 Office 转换、目录页码与版面检查"]
    OFFICE -->|"通过"| PUBLISH["发布"]
    OFFICE -->|"失败"| BLOCK["阻止发布"]
```

主要规则：

- 票价、开放时间、预约与地址只接受可回查的 L1 / L2 来源。
- 正文引用来源编号；审阅器检查引用是否存在、数字是否一致、事实是否过期。
- 三轮审阅仍无法核实的段落会被省略并记录；仍有阻断问题时不发布。
- 行程默认从下一个周六开始，每天 09:00 排程，活动、交通、等待与用餐合计不超过 10 小时。
- 步行与驾车使用不同的真实路网来源；驾车时间不能折算为公共交通时间。
- 景点图优先使用有作者、许可和页面记录的授权实拍；没有合格图片时明确降级。
- Word 目录使用真实 PDF 页码更新；PPTX、DOCX 与 HTML PPT 均需通过最终文件检查。

## 快速启动

```mermaid
flowchart LR
    ENV["Python 3.12.11<br/>Node.js 22+<br/>npm + Docker Compose"] --> CONDA["创建 Conda 环境"]
    CONDA --> SETUP["运行 scripts/setup.sh"]
    SETUP --> KEYS["在 .env 配置模型密钥"]
    KEYS --> START["运行 scripts/start.sh"]
    START --> DOCKER["启动 etcd、Milvus 与 Attu"]
    DOCKER --> APP["启动 FastAPI 与构建后的前端"]
    APP --> OPEN["打开 http://127.0.0.1:8000"]
```

```bash
conda env create -f environment.yml
conda activate travel-harness
PYTHON_BIN="$(command -v python)" ./scripts/setup.sh
```

安装脚本会创建 `.venv`、安装锁定依赖、构建前端，并下载思源黑体到被忽略的
`data/fonts/`。若 `.env` 不存在，会从 `.env.example` 创建。

在 `.env` 中至少配置：

```dotenv
LLM_API_KEY=你的本机网关密钥
SILICONFLOW_API_KEY=你的硅基流动密钥
```

不要提交真实密钥。模型地址、模型 ID、Milvus、MCP、OSRM 与并发参数均可在
`.env` 中调整。

```bash
./scripts/start.sh
```

默认地址为 `http://127.0.0.1:8000`，API 文档为 `/docs`。应用只绑定本机地址。

```bash
# 更换应用端口
PORT=8765 ./scripts/start.sh

# 已单独启动依赖时，只启动 Web 服务
SKIP_DOCKER=1 PORT=8765 ./scripts/start.sh
```

若本机 19530 已被其他项目占用，请在 `.env` 使用其他 Milvus、健康检查与 Attu 端口。
不要复用其他项目的数据库卷。

## 开发与检查

```bash
# 后端开发服务
.venv/bin/python -m uvicorn backend.app.main:app --reload \
  --host 127.0.0.1 --port 8000

# 另一个终端：前端开发服务，将 /api 代理到 8000
cd frontend
npm run dev
```

```bash
# 后端静态检查与测试
.venv/bin/ruff check backend scripts
.venv/bin/python -m pytest -q

# 可选：连接独立测试 Milvus
MILVUS_TEST_URI=http://127.0.0.1:19540 .venv/bin/python -m pytest -q

# 前端生产构建
cd frontend
npm run build
```

Milvus 集成测试使用随机命名的独立数据库和合成向量，结束后删除测试库，
不会向正式知识库预置数据。

## 服务探测

```bash
.venv/bin/python scripts/smoke.py
```

探测内容包括 Python、LLM 文本与 JSON、图片输入、文本与图片 Embedding、
Reranker、MCP 工具发现、Nominatim 与 OSRM。报告写入 `data/smoke.json`；
真实 `tools/list` 结果写入 `docs/mcp_tools.md`。

默认从 `~/.codex/config.toml` 读取 MCP 配置，也可通过 `MCP_CONFIG_PATH`、
`MCP_SEARCH_SERVER` 和 `MCP_FETCH_SERVER` 覆盖。

## 目录结构

```text
backend/app/         FastAPI、SQLite、异步任务、SSE
backend/harness/     Schema、节点编排、事实与路线规则
backend/tools/       LLM、MCP、网页、地理编码、OSRM、图片
backend/kb/          URL 去重、分块、Embedding、Milvus、Rerank
backend/render/      同源文档、真实底图、Office 转换与质量检查
backend/tests/       单元测试、API 测试与可选 Milvus 集成
frontend/            React 工作台
docker/              独立 Milvus、Attu 与可选 OSRM
docs/                实施、验收、性能与问题修复记录
scripts/             安装、启动、冒烟、回归与基准脚本
third_party/         HTML PPT 生成依赖
data/                本机运行数据、缓存与产物（不进入 Git）
```

## 文档入口

- [实施记录](docs/implementation.md)
- [PRD v1.1 整改报告](docs/prd-v1.1-validation.md)
- [性能实施记录](docs/performance-implementation.md)
- [HTML PPT 实施记录](docs/html-ppt-implementation.md)
- [完整功能浏览器验收](docs/browser-full-function-verification.md)
- [MCP 工具清单](docs/mcp_tools.md)

## 数据与安全边界

- `.env`、`data/`、`output/`、`backups/`、虚拟环境、依赖目录、构建产物和缓存不进入 Git。
- 本地运行数据只保存在被忽略目录中，不会随仓库发布。
- MCP 抓取前检查 URL、robots、同站速率与重定向；内网地址默认拒绝。
- `FETCH_PROXY_FAKE_IP` 默认关闭，只在确认代理使用 `198.18.0.0/15` Fake-IP 时开启。
- 外部模型、搜索、地图和图片服务不可用时，系统明确失败或降级，不伪造真实资料。
- 模型生成与自动核验不能替代人工事实复核；当前城市样例和剩余限制见验收文档。
