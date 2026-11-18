# 知行 · 文旅 Agent Harness

将旅游攻略或城市文化研究整理成同源的 PPT、Word 与结构化 JSON。React 18 / TypeScript / Vite 前端，FastAPI / Python 3.12.11 后端，SQLite 保存任务与缓存，Milvus 保存向量资料。

**当前是可运行的首版，尚未达到 PRD 第 14 章的完整交付验收。** 三个模型必须接通后才能验证真实城市产物，当前不会用预置旅游资料或模拟成功替代真实研究。已实现内容与待验收项目见 [实施记录](docs/implementation.md)。

## 任务进度

任务详情展示当前具体操作、每阶段耗时、本次总耗时及可展开的时间记录。执行中每秒更新计时，并根据近期阶段耗时估算剩余时间和完成时刻；样本不足时使用明确标注的初始参考值，外部服务延迟会影响估算。失败、中断或完成后停止计时，重试单独记录本次执行耗时。历史任务缺失的计时显示“未记录”，不补造数据。

## 生成提速

已启用有上限的并行搜索、跨网站抓取、文本向量复用及封面与照片核验重叠执行，内容范围与质量检查保持不变。同站抓取保留一秒间隔。`RESEARCH_CONCURRENCY` 默认 2（可设 1），`FETCH_CONCURRENCY` 默认 3（可设 1–3）。实际收益与缓存和外部服务响应有关，详见 [提速实施记录](docs/performance-implementation.md)。

## 快速启动

需要 Python **3.12.11**、Node.js 22+、npm 和 Docker Compose。可选安装 LibreOffice、Poppler，以生成实际 PPT 页面预览，并检查 Word 能否转换为 PDF。

```bash
conda env create -f environment.yml
conda activate travel-harness
PYTHON_BIN="$(command -v python)" ./scripts/setup.sh
```

安装脚本创建独立 `.venv`、安装锁定依赖、构建前端，并下载官方思源黑体到被忽略的 `data/fonts/`。不会修改系统字体目录。`.env` 不存在时从 `.env.example` 创建。

在 `.env` 中设置：

```dotenv
LLM_API_KEY=你的本机网关密钥
SILICONFLOW_API_KEY=你的硅基流动密钥
```

不要将真实密钥写入代码或提交到仓库。API 地址和模型 ID 均可在 `.env` 中调整。

```bash
./scripts/start.sh
```

启动脚本先启动并等待本项目 Docker 服务，再启动后端与已构建的前端。打开 `http://127.0.0.1:8000`；API 文档在 `/docs`。默认仅绑定本机地址。

本次开发机器的 19530 已由其他项目使用，因此本地 `.env` 配置为 Milvus **19540**、健康端口 **9095**、Attu **8005**；默认示例仍保留 PRD 的 19530。不要将已有项目的数据目录挂到本项目。

可更改应用端口或单独启动 Web 服务：

```bash
PORT=8765 ./scripts/start.sh
SKIP_DOCKER=1 PORT=8765 ./scripts/start.sh
```

修改 `.env` 后重启应用，配置才会生效。

## 开发与检查

```bash
# 后端
.venv/bin/python -m uvicorn backend.app.main:app --reload --host 127.0.0.1 --port 8000

# 另一个终端：前端开发服务器，将 /api 代理到 8000
cd frontend
npm run dev
```

```bash
.venv/bin/ruff check backend scripts
.venv/bin/python -m pytest -q
MILVUS_TEST_URI=http://127.0.0.1:19540 .venv/bin/python -m pytest -q
cd frontend
npm run build
```

Milvus 集成测试创建随机命名的独立测试数据库，用合成向量验证入库、召回、城市过滤和维度保护，结束后删除自己的数据库。不会往正式知识库预置数据。模型服务质量不由这项测试代表。

## M0 服务探测

从项目根目录运行：

```bash
.venv/bin/python scripts/smoke.py
```

探测 Python、LLM 文本及 JSON、LLM 图片输入、文本与图片 Embedding 实际维度及建库、Reranker、MCP 工具发现、Nominatim、OSRM。报告保存到 `data/smoke.json`；真实 `tools/list` 结果写入 `docs/mcp_tools.md`。关键探测失败或缺密钥会返回非零退出码，不能视作全绿。

本机 MCP 配置默认读取 `~/.codex/config.toml` 的 `mcp_servers`。通过 `MCP_CONFIG_PATH`、`MCP_SEARCH_SERVER` 指定配置文件和搜索服务。应用先发现工具及输入 Schema，再选择工具并校验参数，不硬编码搜索工具名。当前已发现 Exa 同时提供搜索与网页抓取；抓取先检查目标 URL、robots 和速率，再优先走 MCP。MCP 连接错误最多重试三次；单页 MCP 抓取失败时尝试 HTTP 抓取，仍不可用的网页记录警告并跳过。搜索服务未提供抓取能力时使用 httpx + trafilatura。优先发现配置的 fetch 服务，再尝试搜索服务中的网页抓取工具，均不可用时使用经过 URL 检查的 HTTP 抓取。

若本机代理以 `198.18.0.0/15` 返回 Fake-IP DNS，可在 `.env` 显式设置 `FETCH_PROXY_FAKE_IP=true`。默认关闭；开启仅放行域名解析所得的该代理网段，字面 IP、本机地址、私网地址仍被拒绝。仅在已确认使用这种代理的环境开启。

本地 `fetch` MCP 自身仍可能拒绝 Fake-IP 解析结果；此时可将 `.env` 的 `MCP_FETCH_SERVER` 设为已配置且提供网页抓取工具的远程服务（如 `exa`），重启项目后生效。该设置不改变本地 Fetch MCP 或系统代理的安全策略。

## 使用流程

1. 输入“做一个上海的旅游攻略”或“做一些苏州的文旅介绍”，也可输入其他城市。
2. 可选填写人数、天数、预算、出行日期。缺失参数由 Router 抽取默认值并列为假设。
3. 查看节点进度、耗时及来源数。事件存入 SQLite，SSE 断线按事件编号续传，前端另有轮询同步。
4. 审阅通过后查看正文、实际 PPT 页面（安装转换工具时）、Leaflet 交互路线和来源，并下载两种文档。
5. 失败后重试当前节点；进程重启后未完成任务标为中断，可从检查点继续。
6. 修改会建立新版本，保留原文档。主题颜色仅重跑渲染；现有内容改写重跑 Writer、Reviewer、Assembler；景点或偏好变化从 Planner 开始；目的地变化重新路由。

无模型密钥时可以打开界面、查看历史和知识库，但生成会在调用模型时明确失败，不会返回虚构旅游内容。

## 真实性与边界

- 正文引用源编号；事实记录保留引文、核验日期与置信度。核验器检查来源存在、引文可回查、数字一致和有效期；中文排版空白先规范化，保留字词和数字校验。资料等级不足的敏感字段标为暂未核实。审阅节点区分事实与明确标注的编辑建议，只修正被指出的段落。两轮后仍无法核实的段落会被省略并记录，再审阅剩余全文；仍不通过或文化主题不足五个则停止发布。
- 票价、开放时间、预约与地址仅接受 L1 / L2，并保留原文证据。L1 包括政府域名、UNESCO 与已核对的场馆域名；自有域名登记在 `backend/tools/sources.py`，未知域名不会仅凭模型声明升为官方来源。L1 单一来源可以采信。
- 新任务采用 PRD v1.1 交付门禁：关键参观字段核实比例至少 75%，其余附官方核实渠道；内部日志、重复正文、未对应引用和不合格路线阻止发布。旧版本修改时会重新研究与核验。
- 未指定日期时按下一个周六、每天 09:00 排程；游玩、真实路网交通、等待及用餐合计不超过 10 小时。开放日、最后入场和停留时间参与排序；缩时或删点后仍失败则停止。未纳入景点列入备选。
- 步行使用独立的 `OSRM_FOOT_BASE_URL`，驾车路由仅作为明确标注的打车估算。Overpass 提供可构建的公交或地铁关系；无法取得线路时只列步行、打车参考，禁止驾车时间折算公交。
- 路线图使用 OSM 底图，瓦片不可用时尝试真实矢量数据；底图有效像素不足 30% 时停止。前端与导出使用同一 GeoJSON，路线沿真实 geometry，一日游不重复总览图。
- 景点图优先 Commons 授权实拍，记录作者、许可及页面，执行 Qwen 图文相似度、视觉模型与 phash 去重；取得不到合格照片时显示“暂无合规实拍图”。AI 仅作封面等装饰，全文至多三张。Magpie 生图需先配置可用模型。
- Word 使用实际 PDF 页码更新原生 TOC 域；PPT 采用地图加时间轴、景点与实用信息表格。发布前真实转换两种 Office 文件，检查完整性、越界、目录和正文清洁。转换工具不可用时阻止发布。
- 当前三城实测及外部服务限制见 [PRD v1.1 整改报告](docs/prd-v1.1-validation.md)。模型核验仍需人工抽查；授权实拍和公共交通资料不可用时会按上述规则降级。

## 本地数据与部署

```text
backend/app/         FastAPI、SQLite、异步任务、SSE
backend/harness/     Schema、节点编排、事实与路线规则
backend/tools/       模型、MCP、网页、地理编码、OSRM、MCP 生图
backend/kb/          URL 去重、分块、Embedding、Milvus、Rerank
backend/render/      同源文档、真实底图、Office 页面转换与门禁
backend/tests/       单元测试、API 测试、可选真实 Milvus 集成
frontend/            React 工作台
docker/              独立 Milvus / Attu、可选 OSRM
data/                本机任务、缓存、图片、检查点和产物（不进 Git）
```

Docker 使用独立的 `travel-harness` 项目名、网络和卷。Milvus 3.0.2 使用本地存储；etcd 3.5.33 独立启动，通过健康检查后才启动 Milvus，避免内嵌 etcd 初次选主时的启动竞态。etcd 沿用本项目卷中的原 `/var/lib/milvus/etcd` 数据，不对主机开放端口。Attu 3.0.0 以本地单用户模式运行，端口仅绑定本机。本项目容器可用以下命令停止，不删除数据：

```bash
docker compose --env-file .env -f docker/docker-compose.yml stop
```

默认 OSRM 指向公共服务，适用于冒烟与小规模开发。自建时，将目标区域 `.osm.pbf` 放入 `docker/osrm-data/`，用对应 OSRM 镜像依次运行 `osrm-extract -p /opt/car.lua`、`osrm-partition`、`osrm-customize`，将处理好的文件前缀设为 `region`，然后：

```bash
docker compose --env-file .env -f docker/docker-compose.yml --profile routing up -d
# .env 设置 OSRM_BASE_URL=http://127.0.0.1:5000，重启应用
```

没有地图数据时不启动 routing profile。此容器只提供驾车 profile。自建步行路由必须另用 foot.lua 处理地图并启动独立实例，将地址写入 OSRM_FOOT_BASE_URL；修改 URL 中的 profile 名称不会改变已加载的数据 profile。
# html-ppt 风格化生成

项目已接入 `third_party/html-ppt-skill`。启动后可通过首页选择预置风格、输入风格描述、选择 html-ppt 主题或上传 `.pptx/.potx` 模板。

常用接口：

- `GET /api/styles` 获取 36 个主题、15 套完整模板和预置风格。
- `POST /api/styles/compile` 将自然语言风格描述编译成 StyleSpec。
- `POST /api/styles/compare` 比较两套风格的主题、模板、动效、布局语法和母题差异。
- `POST /api/templates` 上传本地 PPTX/POTX 并提取页面槽位。

每个完成的文档任务包含 `travel.html`、`travel-html.zip`、`DESIGN.md`、`html-qa.json` 和 `travel.pptx`。HTML ZIP 适合继续修改主题、布局和动画，PPTX 用渲染页保证 PowerPoint 与 HTML 的视觉一致。
