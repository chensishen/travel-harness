# PPT 风格与模板生成方案

## 目标

本方案满足三项要求：

1. 同一份文旅内容可以生成多种视觉风格。
2. 用户可以用自然语言描述目标风格，系统据此生成 PPT。
3. 用户可以上传 PPT 模板，系统按模板生成可编辑的 PPT。

示例需求包括：

- “做一个上海的文旅介绍。”系统使用上海城市视觉风格。
- “做一个官方风格的上海文旅介绍。”系统使用正式、稳重的政务汇报风格，同时保留上海城市特征。
- “保留内容和来源，改成海派杂志风。”系统只重跑设计和排版，不重新研究资料。

## 总体原则

大语言模型负责理解需求和生成结构化设计规范，确定性代码负责布局和 PPTX 输出。模型不直接生成整套 PPT 绘制代码。

```text
用户需求
  ↓
内容研究与来源核验
  ↓
Content IR
  ↓
Style Intent Parser
  ↓
Style Resolver
  ↓
Design IR
  ↓
Layout Planner
  ↓
自由设计渲染器或模板渲染器
  ↓
LibreOffice 转换与页面截图
  ↓
结构、版面、来源和视觉质量检查
```

## 风格识别

系统解析四类信息：

- 城市或地域风格
- 受众和使用场景
- 用户明确描述的视觉风格
- 内容类型和页面用途

风格优先级如下：

```text
用户明确风格 > 受众与用途 > 城市风格 > 内容类型 > 默认风格
```

“上海文旅介绍”默认采用海派、现代、开放、城市文化方向。

“官方风格的上海文旅介绍”使用正式、克制、清晰的官方版式。上海特征通过城市图片、章节主题、辅助色和内容组织体现。

## 城市风格档案

系统维护可版本化的城市风格档案，例如：

```json
{
  "shanghai": {
    "keywords": ["海派", "开放", "现代", "近代城市", "国际化"],
    "palette": ["1F4E79", "B34A36", "F4F1EA", "263238"],
    "motifs": ["石库门", "黄浦江", "城市天际线", "近代建筑"],
    "image_policy": "城市地标、历史建筑、街区生活",
    "layout_bias": ["editorial", "timeline", "city_map"],
    "avoid": ["过度传统园林风", "纯江南水墨风"]
  }
}
```

城市档案只提供文化和视觉方向，不虚构官方品牌规范。涉及官方标识、政府标准色或机构字体时，必须使用用户提供的品牌资料或模板。

## 用途风格档案

系统同时维护用途风格档案，例如：

```json
{
  "official": {
    "palette": ["123B63", "C62828", "FFFFFF"],
    "density": "balanced",
    "title_style": "formal",
    "source_display": "visible",
    "decorative_level": "low",
    "layout_bias": ["report", "timeline", "data_summary"]
  }
}
```

可内置的用途风格包括：

- 官方汇报
- 商务提案
- 文旅宣传
- 杂志编辑
- 学术研究
- 年轻社交媒体
- 深色高端
- 新中式文化

## 设计中间表示 Design IR

自然语言经过文本生成模型后，必须转换为受 Pydantic Schema 约束的结构化对象：

```json
{
  "style_name": "shanghai_official",
  "palette": {
    "primary": "123B63",
    "accent": "C62828",
    "background": "FFFFFF",
    "text": "263238",
    "muted": "68737A"
  },
  "fonts": {
    "heading": "Source Han Serif SC",
    "body": "Source Han Sans SC"
  },
  "density": "balanced",
  "image_ratio": "medium",
  "cover_layout": "formal_city_cover",
  "section_layout": "report_section",
  "poi_layout": "image_fact_card",
  "timeline_layout": "vertical_timeline",
  "table_style": "official_rows",
  "source_display": "footer_and_notes",
  "decorative_level": "low",
  "city_features": ["上海城市天际线", "近代建筑", "黄浦江"]
}
```

Schema 校验需要限制颜色格式、字体回退、布局名称、图片比例和页面密度。模型输出无效时使用同一风格的安全默认值，不重新研究内容。

## 内容中间表示 Content IR

研究和核验结果先转成与视觉无关的内容结构：

```json
{
  "deck_type": "culture_intro",
  "title": "上海城市文化",
  "audience": "政府文旅汇报",
  "chapters": [],
  "poi_cards": [],
  "timeline": [],
  "maps": [],
  "sources": []
}
```

同一份 Content IR 可以输出不同风格的 PPT、Word 或模板版本。换风格不改变事实、来源和核验状态。

## 布局规划

新增 Layout Planner。它只输出页面计划，不直接画 PPT：

```json
{
  "slides": [
    {
      "role": "cover",
      "layout": "formal_city_cover",
      "content_ids": ["title", "hero_image"]
    },
    {
      "role": "chapter",
      "layout": "report_section",
      "content_ids": ["chapter_1"]
    },
    {
      "role": "poi",
      "layout": "image_fact_card",
      "content_ids": ["P1"]
    }
  ]
}
```

布局规划负责页面数量、页面类型、内容容量、分页、图片比例和模板布局选择。渲染器只执行布局计划。

## 两个渲染后端

### 自由设计渲染器

使用 TypeScript 和 PptxGenJS。适合根据 Design IR 生成新风格、主题、母版、图表和页面布局。当前 Python Harness 继续负责研究、核验、图片和 Word。

调用方式：

```text
Python Harness → POST /render/freeform → Node Slide Renderer → travel.pptx
```

### 模板渲染器

使用 pptx-automizer 处理用户上传的 PPT 模板。模板渲染器负责保留母版、布局、Logo、页脚和主题，并按 Layout Plan 选择模板布局。

简单的文字和图片占位符模板可以兼容 pyptx-templar。复杂母版和布局导入使用 pptx-automizer。

## 用户模板流程

1. 用户上传 `.pptx`。
2. 系统读取母版、布局、占位符、字体、主题色和页面截图。
3. 系统展示模板布局预览。
4. 用户确认每个布局的用途，例如封面、章节、景点、时间线和来源页。
5. 系统保存模板清单。
6. Layout Planner 根据页面语义选择模板布局。
7. 模板渲染器填充文字、图片、表格和图表。
8. 转换为 PDF 和页面截图后执行质量检查。

模板清单示例：

```json
{
  "template_id": "template_001",
  "source_file": "government-tourism.pptx",
  "layouts": {
    "cover": {"master": 1, "layout": 2},
    "section": {"master": 1, "layout": 4},
    "poi_detail": {"master": 1, "layout": 7},
    "timeline": {"master": 1, "layout": 8},
    "sources": {"master": 1, "layout": 9}
  }
}
```

首次使用允许用户确认布局用途。系统不强行猜测复杂模板，确认后的映射可以重复使用。

## 后端节点调整

当前流程增加以下节点：

```text
Router
Planner
Researcher
Verifier
Writer
RoutePlanner
StyleIntentParser
StyleResolver
DesignPlanner
LayoutPlanner
Illustrator
Reviewer
Renderer
Assembler
```

只修改风格时运行：

```text
StyleIntentParser → StyleResolver → DesignPlanner → LayoutPlanner → Renderer → Assembler
```

只更换模板时运行：

```text
TemplateAnalyzer → LayoutPlanner → Renderer → Assembler
```

只修改事实内容时才重新进入研究和核验流程。

## 前端能力

首页增加三个区域：

- 风格预设
- 风格描述
- 上传 PPT 模板

生成前显示三页预览：

- 封面
- 章节页
- 内容页

预览中显示识别结果：

- 城市风格
- 用途风格
- 配色
- 字体
- 页面密度
- 图片比例
- 主要视觉元素

支持以下局部操作：

- 只换风格
- 只换模板
- 只换图片
- 只重做某一页
- 保持内容和来源不变

## 质量控制

### 结构检查

- PPTX 可以打开
- 页面数量正确
- 所有模板字段填充
- 图片文件存在
- 字体有安全回退
- 来源页存在

### 几何检查

- 文本不越界
- 图片不越界
- 表格不超出页面
- 标题不重叠
- 页码不重复
- 来源不被裁切

### 内容检查

- 正文来自已核验材料
- 来源与段落对应
- 图片与景点匹配
- 换风格不改变事实
- 模板填充不丢失内容

### 视觉检查

- LibreOffice 转 PDF
- 导出页面截图
- 检查页面空白、溢出、重叠和字体异常
- 只对问题页面重新布局

## 实施阶段

### 阶段一，建立中间表示

- 建立 Content IR
- 建立 Design IR
- 建立 Layout Plan
- 让现有风格先通过新接口生成
- 保留现有视觉结果

### 阶段二，实现自然语言风格

- 增加 Style Intent Parser
- 增加城市风格档案
- 增加用途风格档案
- 实现 8 套预设
- 实现三页设计预览
- 实现只换风格

### 阶段三，实现自由设计渲染

- 增加 Node/PptxGenJS 渲染服务
- 将颜色、字体、页面类型和布局参数化
- 支持单页重排
- 保留 Python 渲染器作为回退路径

### 阶段四，实现模板生成

- 上传和解析 PPTX
- 展示模板布局预览
- 保存模板清单
- 接入 pptx-automizer
- 支持文字、图片、表格、图表和来源填充

### 阶段五，局部重试和缓存

- 缓存 Content IR
- 缓存 Design IR
- 缓存模板分析结果
- 缓存单页渲染结果
- 失败时只重跑问题页面
- 保存最终 PPT、设计 JSON、布局 JSON 和模板清单

## 技术选型

| 能力 | 技术 |
|---|---|
| 资料研究和核验 | 现有 Python Harness |
| 内容结构 | Pydantic Content IR |
| 风格理解 | 当前文本生成模型 |
| 模板视觉分析 | 视觉模型，可选 |
| 自由设计 PPT | TypeScript + PptxGenJS |
| 复杂模板填充 | pptx-automizer |
| 简单占位符模板 | pyptx-templar |
| Word | 现有 Python 渲染器 |
| 页面转换 | LibreOffice |
| 质量检查 | PPTX 结构检查 + 页面截图检查 |

## 验收标准

- “上海文旅介绍”和“杭州文旅介绍”具有明显不同的城市视觉方向。
- “上海风”和“官方风格的上海”具有明显不同的版式和装饰密度。
- 用户明确指定的风格优先于默认城市风格。
- 风格变化不改变已核验事实、来源和正文。
- 模板生成结果保留母版、Logo、页脚和主要字体。
- 模板缺少某类布局时使用安全备用布局。
- PPTX、DOCX 和页面预览均通过结构与版面检查。
- 只换风格时不重新执行完整研究流程。
