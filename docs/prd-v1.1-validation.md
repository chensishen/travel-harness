# PRD v1.1 整改与实测报告

日期：2026-10-28。按用户提供的《杭州样例修改任务书》修改当前项目，未改动原任务书。

## 当前结果

三份新版文件通过当前自动交付门禁，服务已重启。回归复用已有真实研究检查点，并补采官网、浏览器实读和人工核对的官方资料；不是三次从空白开始的全自动成功率测试。

| 用例 | 主线景点 | 门票/开放/预约已核实 | 每日总时长 | 底图有效像素 | PPT/Word页数 |
| --- | ---: | --- | --- | --- | --- |
| 杭州 | 3 | 8/9（88.9%） | 8小时45分 | 84.3% | 12/10 |
| 上海 | 4 | 11/12（91.7%） | 8小时45分、8小时45分、8小时45分 | 92.3%、85.9%、87.9% | 17/13 |
| 西安 | 4 | 10/12（83.3%） | 不安排路线 | 不适用 | 16/11 |

## 使用的技能与工具

- 使用当前已提供的 find-skills、Presentations、Documents 技能，落实版式、原生目录、Office 转换与视觉检查；未安装或执行新的第三方技能。此前调研保存在 document-design-skills.md。
- MCP：配置中的 Exa 搜索、Fetch 抓取与 Magpie 装饰生图。Fetch 在当前代理环境拒绝 Fake-IP 解析，保留安全检查后使用 HTTP 抓取；未放宽全局 MCP 访问权限。动态官网用后台浏览器实读补证。
- 路线：独立 OSRM foot 服务、OSRM 驾车打车参考、Overpass、OSM 瓦片与真实矢量回退。前端与导出共用 GeoJSON；重规划子集可复用缓存中的真实邻域瓦片。
- 图片：Commons 授权元数据、phash、Qwen3-VL-Embedding-8B 与 Qwen3-VL-8B-Instruct；后两者已对真实 API 做负例测试，水彩装饰图均被拒绝作为景点实拍。

技术依据：[OSRM 官方说明](https://github.com/Project-OSRM/osrm-backend/wiki/Frequently-Asked-Questions)、[SiliconFlow Embeddings 文档](https://docs.siliconflow.cn/docs/api/embeddings-post)、[Commons Imageinfo API](https://www.mediawiki.org/wiki/API:Imageinfo)。

## 实施内容

- 新增发布门禁：时间、开放窗、最后入场、路线 geometry、字段证据、75%核实比例、照片/占位、引用、重复文案与内部词；失败不提供新产物下载。
- L1 官方单一来源可采信；登记已核对场馆域名，清理抓取标签，规范时间与地址；数字比较支持 9:00/09:00 及上午/下午；错误引文进行针对性补核。
- 每日含游玩、交通、用餐与等待；默认下个周六9点；最多六候选点排序，超时先缩时后删点；删点列入备选。
- 有真实底图、比例尺、署名与真实路径；一日仅一张主路线图。PPT 地图与时间轴同页，Word 使用时间表、原生 TOC 实际页码、分页控制。
- 景点页去除 AI 插画，保留明确实拍占位；AI 封面每份一张。交通预算改为表格，补充餐饮、交通公告、预算及读者核实清单。
- 网页同步展示完整时间轴、标准化开放时间、实拍占位、餐饮预算、备选与核实链接；内部审阅记录不混入新版读者预览。

## 仍需知晓的限制

- 本轮 Commons API 返回403、图片下载返回429；11个主线景点实际取得合规实拍0张，全部明确占位。图片模型负例通过，不代表已完成真实照片正例全链路验收。
- 未构建出可用的完整公交线路关系。需要交通的路段展示独立步行路径或打车估算；不提供虚构站名、换乘或公交班次。
- 上海由9个原候选缩为4个主线点，按2/1/1分配三天；空余时间标为自由安排。鲁迅纪念馆闭馆，另有开放信息或定位不足的项目列入备选。该产物是信息受限后的保守行程。
- 仍有未核实的敏感字段：浙江美术馆普通参观预约、上海博物馆人民广场馆具体票价、西安城墙开放时间及预约。已给官方渠道；餐饮/市内交通金额为个人预算参考，非商家报价。
- 模型审阅、节假日例外、临时闭馆及未知场馆域名仍需持续维护与人工抽查。自动门禁通过不代表所有历史叙述都已完成独立双源复核。

## 实际验证

- 后端测试：67项通过，包含真实 Milvus 独立数据库入库、召回、城市过滤与维度保护；4条已有/依赖弃用提示，无测试失败。
- Ruff 检查通过；TypeScript 与 Vite 生产构建通过。
- 三份 PPTX / DOCX 实际经 LibreOffice 转 PDF 并检查页码、完整性、边界和正文；逐页渲染检查中修正了文字挤压、表格孤行和稀疏强制分页。
- 服务健康正常；6个下载响应与本地最终文件 SHA-256 一致；三个地图接口返回正常。
- 原有已完成文件保留；新版以独立任务出现在历史记录。

## 文件

- 杭州：[PPT](/Users/admin/Documents/ChatGPT/agent项目3/data/jobs/35257983-409b-4933-bcd8-112e6007793e/travel.pptx) · [Word](/Users/admin/Documents/ChatGPT/agent项目3/data/jobs/35257983-409b-4933-bcd8-112e6007793e/travel.docx)
- 上海：[PPT](/Users/admin/Documents/ChatGPT/agent项目3/data/jobs/bc87a876-130b-4a85-90c2-4cca847943af/travel.pptx) · [Word](/Users/admin/Documents/ChatGPT/agent项目3/data/jobs/bc87a876-130b-4a85-90c2-4cca847943af/travel.docx)
- 西安：[PPT](/Users/admin/Documents/ChatGPT/agent项目3/data/jobs/fd26b94a-73cd-4263-9ace-fb13690f12a7/travel.pptx) · [Word](/Users/admin/Documents/ChatGPT/agent项目3/data/jobs/fd26b94a-73cd-4263-9ace-fb13690f12a7/travel.docx)

机器检查结果：data/qa/prd-v11-results.json；下载核对：data/qa/prd-v11-api-validation.json；图片负例：data/qa/v11-vl-negative-test.json。
