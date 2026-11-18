# html-ppt 风格化 PPT 实施记录

## 已实现

- 将 `third_party/html-ppt-skill` 固定到仓库，保留 36 个主题、15 个完整 deck 模板、36 个单页布局和动画运行时。
- 新增 `backend/render/html_ppt.py`，将现有 Deliverable 映射为 HTML deck，按提示词自动选择风格，并支持显式传入预设、主题、模板和动画。
- 支持海派摩登、官方庄重、现代简约、杂志编辑、科技深色、商务汇报六个预设。海派和官方预设使用不同的封面与内容结构。
- Assembler 同时产出 `travel.html`、`travel-html.zip`、逐页 PNG 和 `travel.pptx`。HTML 与 ZIP 用于继续编辑主题、布局和动效，PPTX 使用渲染页保证打开效果一致。
- 新增 `GET /api/styles`，前端增加风格预设、自由描述、主题和完整模板选择器。
- 新增 `POST /api/styles/compile`、`POST /api/styles/compare`，返回 StyleSpec、DESIGN.md 内容和风格差异度。
- 新增 `POST /api/templates`、`GET /api/templates`，解析本地 PPTX/POTX 的页面尺寸、文本槽位和图片槽位。
- 每次 HTML 生成都会输出 `DESIGN.md` 和 `html-qa.json`，检查页面数量、资源路径和占位符。
- Chrome 截图采用最多两页并行、失败重试两次和每页独立超时，降低长 deck 生成失败率。

## 交付取舍

`html-ppt-skill` 本身是静态 HTML/CSS/JS 技能，没有 HTML 到原生 PPTX 形状的转换器。当前 PPTX 采用渲染页嵌入，HTML 源文件和 ZIP 保留可编辑的主题、布局、动效与备注。若后续必须让 PPTX 内的文字和形状逐对象可编辑，需要在此适配器后增加 DOM 测量到 DrawingML 的导出器。

## 验证

- 后端：`277 passed, 1 skipped`
- 前端：`npm run build` 通过
- html-ppt 目录：36 个主题、15 个完整模板
- 实际样例：22 页 HTML、22 张 1920×1080 PNG、PPTX Office 校验通过、HTML ZIP 校验通过
- 风格差异度：海派摩登 vs 官方庄重，5/5 个维度通过
