# PPT 与 Word 设计技能调研

调研日期：2026-10-28。目标：文旅内容图文并茂、层级清楚、版式有变化，交付可编辑的 PPTX 与 DOCX。

## 推荐组合

以下安装量是查询时 skills.sh 的显示值，热度仅作为筛选参考，不代表本项目实测质量。

| 技能 | 来源 | 显示安装量 | 本项目用途 |
| --- | --- | --- | --- |
| pptx | Anthropic 官方 anthropics/skills | 243.4K | 可编辑幻灯片；大图、分栏、卡片等版式；字体、间距及逐页视觉检查 |
| docx | Anthropic 官方 anthropics/skills | 208.1K | Word 标题、目录、图文、表格、页眉页脚；生成后转 PDF 检查 |
| theme-factory | Anthropic 官方 anthropics/skills | 95.9K | 统一颜色、字体和视觉主题；提供 10 个预设主题及自定义主题 |
| baoyu-slide-deck | JimLiu/baoyu-skills | 31.2K | 插画式演示、先大纲后逐页视觉设计，可借鉴水彩与历史主题 |

Anthropic 仓库查询时约 184.2K stars；宝玉仓库约 27.0K stars。已阅读官方技能说明和宝玉的 PPT 合成脚本；尚未安装这些外部技能，也未执行其脚本或重新生成文件。

推荐以 pptx + docx 为制作基础，theme-factory 协助统一风格。宝玉方案将每张幻灯片作为整页图片嵌入 PPT，文字不能作为独立文本框编辑；适合参考视觉方向或制作分享图集，不宜直接替代当前可编辑文档输出。

当前会话已提供 Presentations 与 Documents 技能，它们同样覆盖视觉设计和渲染检查。应先把这些流程落实到项目，避免重复安装后误以为后端会自动调用。

## 当前项目的问题及证据

- `backend/render/documents.py:233–235` 将全文按 110 字切片，每片一张 PPT，切片不遵循语义。实测已有上海文件 207 页、53.6 MB，苏州文件 188 页、49.8 MB。
- `sections()` 把待核验事项、景点正文、参观信息和每个来源都当作独立章节输出，正文、研究记录、参考资料缺少编辑层级。
- PPT 使用固定的标题、左侧正文、右侧图片布局，续页重复使用同一图片；空正文仍然生成页面。
- Word 把段落连续追加后再插入固定宽度图片，尚无面向文旅报告的章节开篇、图文节奏及逐页视觉验收。
- 后端 Assembler 直接调用 Python 渲染函数。安装到编码助手的技能不会自动成为后端模型的技能调用。

## 建议的落地顺序

1. 在现有结构化成果和已生成插画上先做苏州样稿，不重复执行搜索、事实核验或全部生图。
2. 为 PPT 单独编辑演示大纲：正文先按约 20–30 页预算编排；每页表达一个主题，长篇资料保留在 Word 或演讲者备注中。页数应随内容调整，不能靠缩小字号或硬截文本达到预算。
3. 使用有区别的页面类型：大图封面、城市概览、文化时间轴、章节开篇、景点图文页、路线地图、实用信息对照表、来源附录。正文避免连续重复同一版式。
4. Word 使用独立的报告结构：封面、可用目录、阅读导引、文化章节、景点及路线、实用信息、来源与待核验附录。配置标题层级、中文字体、图注、页眉页码、表格列宽和分页规则。
5. 复用 MCP 插画，按内容需要使用大图、局部裁切与组图。图片继续明确标注“AI 生成插画 · 非景点实拍”，不能通过生图伪造地图或历史事实。
6. 先展示代表性的封面、图文页、时间轴、地图和 Word 章节页，再推广到所有任务。保留现有文件，避免未验收样稿覆盖旧成果。

## 验收要求

- 正文按语义编排，无 110 字机械切页、空正文占页、半句断页或来源 URL 挤满正文的情况。
- PPT 主体有明确视觉中心，中文标题和正文可读；地图与时间轴标签不重叠，文字与图片不越界。
- Word 标题不孤悬，图片与图注相邻，表格跨页有重复表头，无截断、缺字和异常大面积空白。
- 来源、待核验标记、AI 图片声明完整保留；精简演示内容时不把未经核验的信息变成确定事实。
- PPT 与 Word 均实际渲染并检查页面图像，修复后重新渲染。仅检查文件能打开或 ZIP 完整性不足以验收排版。
- PPT 标题、正文、表格保持原生可编辑；不把全部页面转成图片来替代排版。

## 可选安装命令

这些命令供后续选定工具时使用，本次未执行：

```bash
npx skills add https://github.com/anthropics/skills --skill pptx
npx skills add https://github.com/anthropics/skills --skill docx
npx skills add https://github.com/anthropics/skills --skill theme-factory
# 仅需要图片式幻灯片时考虑：
npx skills add https://github.com/jimliu/baoyu-skills --skill baoyu-slide-deck
```

## 一手来源

- https://skills.sh/anthropics/skills/pptx
- https://github.com/anthropics/skills/blob/main/skills/pptx/SKILL.md
- https://skills.sh/anthropics/skills/docx
- https://github.com/anthropics/skills/blob/main/skills/docx/SKILL.md
- https://skills.sh/anthropics/skills/theme-factory
- https://github.com/anthropics/skills/blob/main/skills/theme-factory/SKILL.md
- https://github.com/anthropics/skills
- https://skills.sh/jimliu/baoyu-skills/baoyu-slide-deck
- https://github.com/JimLiu/baoyu-skills
- https://github.com/JimLiu/baoyu-skills/blob/main/skills/baoyu-slide-deck/SKILL.md
- https://github.com/JimLiu/baoyu-skills/blob/main/skills/baoyu-slide-deck/scripts/merge-to-pptx.ts

许可注意：Anthropic 仓库明确将文档技能标为 source-available，而非开源。若后续把技能代码或资源随产品分发，应先核对各目录 LICENSE.txt；本次仅调研和引用，未复制外部实现。
