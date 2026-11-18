# 新案例生成与复核

日期：2026-10-28。

| 案例 | 任务 ID | PPT | Word |
| --- | --- | ---: | ---: |
| 杭州一日游 | 4c71f75b-aab6-407e-ac37-08e87bff3d6b | 14 页 | 18 页 |
| 南京两日游 | 2bf0291a-61e3-4eba-9d1c-550991bb0b23 | 18 页 | 24 页 |
| 西安城市文化 | e080962c-a6be-4c2e-95e7-c28c1f1d6cf3 | 16 页 | 21 页 |

三个任务均已完成并更新应用中的文件与预览。本次重新检索资料、调用文本与向量服务、通过 Magpie MCP 生成插画，未复用其他城市的研究内容。

## 实际处理

- 恢复本项目已停止的 Milvus 容器；检查知识库、文本模型、Embedding 和搜索 MCP 的连接后继续任务。
- 杭州、南京的事实候选存在描述超出引文的问题。逐项核对连续引文后，将相关描述改用来源原文，再通过既有核验器。修正前后的候选保存在各任务的 `verification-candidates-before-evidence-review.json` 和 `evidence-review.json`。
- 南京地图返回区级名称，杭州包含复合地点。人工核对十个 OSM 对象，保留原始结果、对象链接和代表点说明；未把地图代表点标成已核实入口。记录位于 `data/qa/case-map-review.json`、`data/qa/case-map-decisions.json`。
- 西安审阅返回格式错误后从审阅节点重试，沿用已生成插画。
- 杭州自动审阅将有来源支持的段落误列为问题并删除。页面检查发现空主题页后，按现有原始来源恢复可逐字回查的西湖、苏堤、运河内容；缺少材料的主题改为明确标注的编辑建议或省略。南京省略一张无可保留正文的主题页。
- Word 改用景点间连续排版，避免每个景点强制另起页造成孤立续页；补充 `history_culture` 字段的中文标签。旧文件保存在任务的 `versions/` 目录。

## 验证

三份 PPT 转换为 PDF 和逐页 PNG，检查全部页面总览及代表页。三份 Word 使用 Documents 技能的 `render_docx.py` 转换，检查全部页面总览及放大页面。最终视觉检查文件保存在杭州、南京的 `reviewed-content/word-final/` 和西安的 `word-final/`。

逐项验证最终正文、事实值、来源 URL 和核验提示保留在 Word 与 PPT 备注中；所有保留主题都有正文，PPT 没有空主题占位文案，形状未越出页面。六个 Office 文件均通过 ZIP 完整性检查。

实际通过 HTTP 下载六个 Office 文件，核对下载字节与磁盘一致；三个任务的首页数据、首尾预览及健康接口均正常。

- `MILVUS_TEST_URI=http://127.0.0.1:19540 .venv/bin/python -m pytest backend/tests -q`：52 passed；1 条已有的 Starlette/httpx 弃用提示。
- `.venv/bin/ruff check backend/render`：通过。
- `.venv/bin/python -m compileall -q backend/render`：通过。

机器可读结果：`data/qa/new-cases-validation-20261028.json`。

这些是经过本次内容与页面复核的案例，不能据此宣称自动审阅误判或所有城市的地理编码问题已在通用流程中修复。路线、开放、预约及未完成交叉验证的信息继续保留对应标注；AI 插画不作为实景或史实证据。
