> 最新收尾及限制见[剩余任务收尾报告](remaining-task-closeout.md)：197项测试通过，MCP连接池已加载；外部地图数据与网络可用性仍有限制。

# MCP 服务检查（2026-11-04）

> 后续进展：原鄂尔多斯任务已完成并实际下载19页PPT、12页Word；后续发现并修复事实丢失、官方域名分级和文化候选及导出说明问题。最新结果见[浏览器复测报告](workbuddy-browser-retest.md)末尾。实拍覆盖仍不足；本报告中的核验中状态为历史记录。

本次检查配置、真实握手、工具发现、实际搜索与网页抓取。没有输出密钥或修改全局 MCP 配置。

| 服务 | 工具发现 | 实际调用 |
| --- | --- | --- |
| exa | 成功，21.73秒；提供 web_search_exa、web_fetch_exa | 当前会话搜索和官方页面抓取成功；项目调用经现有重试成功，29.47秒；不代表连接稳定 |
| fetch | 成功，5.45秒；提供 fetch | 抓取 example.com 失败，明确拒绝代理 DNS 返回的198.18.*非公网地址 |
| magpie-image | 成功，0.03秒；提供 generate_image、generate_video、generation_result | 本轮仅验证服务与工具发现，没有重新付费生成图片；上轮已实际生成封面 |

## 故障与处理

1. 本地Fetch MCP的私网保护与当前Fake-IP DNS不兼容。即使应用已有`FETCH_PROXY_FAKE_IP=true`，也不会改变独立Fetch MCP的保护规则。本次将项目`.env`的`MCP_FETCH_SERVER=fetch`改为`MCP_FETCH_SERVER=exa`，使用已发现的远程网页抓取能力。保留robots、URL和私网地址检查。未开启`--allow-private-ips`。
2. Exa直连偶发TLS阶段`httpx.ConnectError`，外层表现为`BrokenResourceError`。同一查询在项目现有重试下也可成功。显式使用本机7897代理的三次实际MCP调用分别11.22、8.65、15.64秒失败，因此没有把该代理配置写入项目，也没有降低TLS校验。
3. 修改后使用全新临时Store、实际FetchProvider读取example.com，通过，选择的抓取服务为exa，66.63秒。当前会话另外实际读取鄂尔多斯市文旅局博物院官方页面成功。该检查验证了抓取能力，不能声称外部链路已完全恢复。

服务已在无活动任务时正常重启。配置检查确认模型仍为`workbuddy/deepseek-v4.1-flash`，搜索exa、抓取exa、生图magpie-image；应用健康接口HTTP200。随后从用户Chrome恢复鄂尔多斯原任务的Researcher检查点，进行业务链路复测。

本轮仅改项目环境配置及说明，未修改后端代码，未重复执行上轮183项后端测试。真实调用证据保存在：

- `output/playwright/full-function/mcp-diagnostics.json`
- `output/playwright/full-function/mcp-fetch-retest.json`
- `output/playwright/full-function/mcp-proxy-comparison.json`


业务复测最终结果：鄂尔多斯任务推进到第12/13项查询后，Researcher于167.05秒因“MCP工具暂时不可用”失败。取得的搜索缓存和知识库资料保留，未发布不合格文件。不能将本次检查记为全部服务稳定或整个生成链路通过。证据：`mcp-business-retest.json`、`mcp-business-retest.png`。


## 第二次检查与恢复

- 相同查询、固定工具参数，排除模型选参：一次真实搜索成功（17.27秒），下一次在TLS建连阶段出现ConnectError（5.99秒），没有到达工具业务层。
- Node客户端三次MCP初始化为成功/ECONNRESET/成功（8.789/10.012/6.237秒），说明故障不局限于Python客户端。Python默认TLS和TLS1.2随后均可成功；没有更改TLS版本、证书校验、系统代理或引入客户端桥接依赖。
- 当前会话Exa MCP四次搜索均成功。已将四条原样真实结果写入应用相同查询的搜索缓存，补齐康巴什区成吉思汗广场、乌兰木伦湖广场、鄂尔多斯市文化艺术中心、恩格贝生态示范区。仅恢复搜索结果，没有填造FactRecord、伪造核验结果或直接发布文档。记录见`mcp-search-recovery.json`。
- 已在用户Chrome点击继续生成，从原Researcher检查点恢复。该措施用于完成当前案例；应用以后直连Exa仍可能受当前网络波动影响，不能据此宣称全局网络已修复。


第二次恢复的阶段结果：Researcher已真实完成，耗时463.82秒（约7分44秒），保留114条来源；任务现进入Verifier。浏览器确认生成中、已完成1/7阶段，核验仍在执行，未将资料条数当作已核实事实条数。证据：`mcp-recovery-progress.json`、`mcp-recovery-verifying.png`。本轮没有修改后端代码或重启服务。


## 2026-11-04 连接复用修复与实测

HTTP MCP 会话现在按端点及认证头复用 httpx 连接池，避免每次初始化重新建连；认证头变更时使用独立客户端，服务退出时关闭客户端。保留证书校验、原有有限重试和错误链，没有修改系统代理或降低 TLS 要求。新增真实 SDK 会话测试覆盖连接复用、认证隔离与关闭。

连续三次真实 Exa MCP 初始化及搜索均成功，耗时 12.11、8.52、7.98 秒，共用一个客户端；证据 `output/playwright/full-function/mcp-pool-live-check.json`。这是应用侧缓解措施，不能证明外部网络不再波动。

2026-11-04 最新完整后端测试 **192 passed，4 项已有弃用警告**；Ruff 通过。Commons 默认英文说明导致中文场馆漏配的修复也包含在该测试结果中。
