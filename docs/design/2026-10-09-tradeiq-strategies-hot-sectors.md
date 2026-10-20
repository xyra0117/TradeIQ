# TradeIQ 低吸、追高、自定义选股与热门板块 UI评审稿

日期：2026-10-09。范围为独立黑白视觉与交互稿，复用已确认的工作台框架和设计规范v1.7。数据均为内置虚构示例；不开发真实策略、不改老版页面、不替换现有入口。

## 老版核对与本轮页面

已读取当前`dashboard/index.html`、`dashboard/custom-picks.js`、`docs/custom-picks.md`及`docs/hot-sectors.md`，并在运行中的老版查看四项入口与界面。

| 老版变化 | 新版UI稿 |
| --- | --- |
| 低吸新增10项独立开关、参数、单项数量 | 可收起条件面板，比较/区间输入，开启数量与独立标签 |
| 追高替代低吸2.0，11项条件 | 同一条件组件，独立设置；不保留旧2.0的三个附加条件 |
| 两策略的市场温度、同日热门榜、诊断 | 紧凑市场摘要与T+日期，展开指数与热门表；诊断名单与应用组合 |
| 自定义分组、点选/公式/中文、模板 | 组间与组内关系、条目编辑和开关、三种编辑入口、草稿核对、撤销 |
| 自定义数量、条件影响、股票核验 | 独立数量、诊断折叠区、核验弹窗、分页名单和T+1～T+5 |
| 板块合集与自选管理被热门板块替换 | 保留一级入口位置；三个状态摘要、近5日榜、近10日热度矩阵 |
| 热门板块龙头及昨日跟踪 | 龙一/二/三依据、缺数据/不足三只、昨日龙一断板仍显示 |
| 板块走势、当日股、历史拆解 | 板块详情页与原股票详情抽屉，返回时保留搜索和筛选 |

## 布局与交互方向

低吸与追高：来源 → 策略 → 条件设置 → 市场温度/同日热门榜 → 条件诊断 → 全宽结果表。条件设置可以收起，收起后保留已开启数量。低吸参数先编辑、再应用；开关和追高条件彼此隔离。单项数量标签明确说明“独立核验，不等于组合入选”。结果T列同时呈现第N日最高累计与收盘累计，后续日期不足或缺记录显示“—”。

自定义初稿：组间关系与新增入口在顶部；条件组是主要编辑区，右侧给出有限示例和数量解释。该布局按用户反馈改为下方v2；初稿素材继续保留。中文先生成可核对的草稿，未识别内容持续显示并阻止加入；移除条目可以撤销。没有开启条件时不展示名单。只演示固定模板与局部编辑，不运行用户公式或调用中文解析服务。

热门板块：优先看持续活跃、新近热点和已经降温；灰阶矩阵按天比较数量，近5日窗口边界明确。累计次数与去重股票数分开。下方再看龙头；点击板块进入走势/明细，点击股票打开右侧详情。缺数据与当天0只分别表达。

彩色热度调整（2026-10-09）：按用户反馈，上述灰阶矩阵改为浅黄、金黄、橙色、暖红的数量色阶。0仍为中性色，缺记录仍为虚线与“—”；保留0 / 1–2 / 3–5 / 6–9 / 10+五档、数值、图例和近5日边界，数据与排序不变。浅深主题使用分别适配的填充与数字色，复用同一组`--heat-*`变量并同步视觉组件展示。正式图片沿用本素材目录，使用`hot-heat-color-*`前缀；验证和摘要保存为`hot-heat-color-verification.json` / `hot-heat-color-manifest.json`，不覆盖历史素材。

[彩色热度浅色稿](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-heat-color-1920-light.jpg) · [深色稿](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-heat-color-1920-dark.jpg) · [打开预览](http://127.0.0.1:5555/dashboard/redesign/index.html?page=sectors&date=20260922&preview=20261009.7&view=recent)。该轮资源版本为`20261009.7`，规范为v1.9。六种宽度/主题布局无页面横向溢出；五档数字对填充色的实测最低对比度为4.67:1，矩阵数字与修改前一致。缺记录仍显示虚线“—”，零值独立显示；视觉组件展示的浅深色与工作台一致，浏览器无脚本错误。旧入口、后端及热门板块计算文件与本轮起始摘要一致。

沿用24px主区边距、208/64px侧栏、64px顶栏、13px表格、8px容器圆角、39px下划线Tab、本地Remix Icon及既有浅深色变量。宽表仅内部横向滚动，规则说明按需展开。

冷暖热度调整（2026-10-09）：用户进一步明确希望从冷色到暖色区分。当前矩阵、图例和视觉组件改为蓝色（1–2只）→ 青色（3–5只）→ 橙色（6–9只）→ 暖红（10只以上）；0为中性色，缺记录为虚线“—”。沿用原分档、数字、数据、排序和布局。浅深主题分别适配，规范更新为v1.10，资源版本为`20261009.8`。正式评审图及验证摘要使用`hot-heat-cool-warm-*`前缀，保存到既有素材目录；旧稿继续保留。

[冷暖热度浅色稿](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-heat-cool-warm-1920-light.jpg) · [深色稿](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-heat-cool-warm-1920-dark.jpg) · [打开当前预览](http://127.0.0.1:5555/dashboard/redesign/index.html?page=sectors&date=20260922&preview=20261009.8&view=recent)。1440 / 1920 / 2560px双主题检查通过，无页面横向溢出；五档数字最低对比度为4.67:1，70个矩阵示例值与修改前一致。组件展示的浅深色阶与工作台相同，页面无脚本错误，受影响JavaScript语法检查通过；旧入口、后端及热门板块计算文件与本轮起始摘要一致。记录见`hot-heat-cool-warm-verification.json`，正式素材摘要见`hot-heat-cool-warm-manifest.json`。

## 评审与交付

独立入口：`/dashboard/redesign/index.html`。策略参数使用`strategy=dip`、`chase`、`custom`；热门板块使用`page=sectors`。本轮资源版本统一为`20261009.4`。

正式黑白图、关键弹窗图、`verification.json`及`manifest.json`保存在同名素材目录；交互核对与三种桌面宽度验证完成后在本页追加结果。UI确认后再进行真实接口开发和验收，旧版继续保留。

## 正式评审图

以下为1920×1200px主稿；所有价格、数量、名单及图形均为虚构示例。条件展开用于评审内容，收起后用于比较名单空间。右上角主题按钮可直接切换黑白模式。

| 页面 | 浅色 | 深色 |
| --- | --- | --- |
| 低吸 · 10项条件与参数 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/dip-light.jpg) | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/dip-dark.jpg) |
| 低吸 · 收起条件后的名单 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/dip-results-light.jpg) | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/dip-results-dark.jpg) |
| 追高 · 11项条件 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/chase-light.jpg) | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/chase-dark.jpg) |
| 追高 · 收起条件后的名单 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/chase-results-light.jpg) | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/chase-results-dark.jpg) |
| 自定义 · 分组与组合结果 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-light.jpg) | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-dark.jpg) |
| 热门板块 · 摘要与热度矩阵 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-sectors-light.jpg) | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-sectors-dark.jpg) |
| 热门板块 · 龙头与昨日跟踪 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-leaders-light.jpg) | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-leaders-dark.jpg) |
| 热门板块 · 走势与拆解详情 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-detail-light.jpg) | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-detail-dark.jpg) |

关键交互图：[点选指标](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-editor-visual-light.jpg)、[公式编辑](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-editor-formula-light.jpg)、[中文草稿核对](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-editor-chinese-light.jpg)、[未识别内容](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-editor-blocked-light.jpg)、[诊断试算名单](2026-10-09-tradeiq-strategies-hot-sectors-assets/diagnostic-preview-light.jpg)、[筛选覆盖](2026-10-09-tradeiq-strategies-hot-sectors-assets/strategy-coverage-light.jpg)、[排名依据](2026-10-09-tradeiq-strategies-hot-sectors-assets/hot-leader-basis-light.jpg)、[组件黑白对照](2026-10-09-tradeiq-strategies-hot-sectors-assets/components-comparison.jpg)。

本轮保留低吸/追高的同日上次与本次数量比较。点选编辑器覆盖指标、计算方式、周期、偏移、固定数值/另一个指标/真假比较；公式提供口径目录及校验反馈样例；中文仅演示固定三项原句，未识别全文保留且不能加入。真实解析、合法性校验、独立计数、筛选结果及诊断仍待后续开发接入。

## 打开预览

已有本地老服务运行时，直接使用以下独立静态路由，不替换老版根入口：

- [低吸选股](http://127.0.0.1:5555/dashboard/redesign/index.html?page=picks&source=em&strategy=dip&date=20260922&preview=20261009.4)
- [追高选股](http://127.0.0.1:5555/dashboard/redesign/index.html?page=picks&source=em&strategy=chase&date=20260922&preview=20261009.4)
- [自定义选股](http://127.0.0.1:5555/dashboard/redesign/index.html?page=picks&source=em&strategy=custom&date=20260922&preview=20261009.4)
- [热门板块](http://127.0.0.1:5555/dashboard/redesign/index.html?page=sectors&date=20260922&preview=20261009.4)
- [UI视觉组件](http://127.0.0.1:5555/dashboard/redesign/components.html?section=business&theme=comparison)

独立静态服务备选地址：`http://127.0.0.1:5567/index.html`。仅绑定本机，根目录为`dashboard/redesign/`；本次没有修改后端路由。原`5556`端口已有手机服务，未占用或替换它。

```bash
python3 -m http.server 5567 --bind 127.0.0.1 --directory dashboard/redesign
```

## 本轮UI验证

- 四页 × 两主题 × 三种宽度，共24种布局；实际浏览器宽度与目标一致。页面无整体横向溢出，所测条件/参数/操作控件无重叠，宽表在内部滚动。
- 低吸10项、追高11项；当前入口不再显示低吸2.0、板块合集或旧自选板块管理。开关、恢复、参数边界提示、同日数量比较、市场日期、诊断预览与应用组合已检查。
- 自定义组间关系、条件组新增、点选保存、公式编辑、模板、关闭与刷新持久化、移除撤销、中文完整草稿与未识别阻止、结果分页及股票核验已检查。演示记录已恢复为默认三项。
- 热门板块搜索、状态筛选、排序、排名依据、板块详情及保留搜索的返回已检查。缺少窗口记录时热度状态/累计不作完整判断，走势对应位置留空。
- 1440px详情采用覆盖模式，1920px可并排；切换股票、放大、关闭均已检查。关闭后搜索与表格滚动位置保留；固定表头和股票列已实测。
- 四页的加载中、空结果、部分缺失与读取失败状态已检查；空组合显示诊断，试算不改变开关，应用才修改此稿设置。组件展示新增条件开关、独立标签、自定义组、灰阶热度单元格，并沿用原主题。
- `workspace.js`、`pages.js`、`components.js`、`preview-charts.js`通过Node语法检查；浏览器未发现JavaScript错误。旧版入口及相关业务文件使用本轮开始时的摘要核对，不用历史Git差异判定本轮改动。

验证细节见素材目录`verification.json`和`manifest.json`。本轮验证仅确认视觉与演示交互，不代表真实API、策略收益或中文解析已完成。待你评审UI后，再进入真实功能开发与数据核对。

## 自定义选股v2布局（2026-10-09）

根据“布局太乱”的反馈整理为全宽条件配置和全宽结果两区。新增入口集中在条件标题栏，模板移入弹窗；条件行按开关、名称、独立数量和操作对齐。公式默认隐藏，配置可以收起；多个组有明确且/或连接，并限制配置区高度。结果区集中搜索、刷新、列设置、条件影响与股票核验。

点选编辑器按判断指标、比较关系、参照值组织，移除常驻说明侧栏；名称、目标组、模式、错误和保存动作保持清晰。所有原有演示功能与新版独立设置保留，业务文件不修改。正式图片沿用本素材目录，使用`custom-v2-*`前缀，与原稿并存；该轮验证与摘要单独保存为`custom-v2-verification.json` / `custom-v2-manifest.json`。

[打开自定义选股v2](http://127.0.0.1:5555/dashboard/redesign/index.html?page=picks&source=em&strategy=custom&date=20260922&preview=20261009.6)。工作台与组件页资源版本统一为`20261009.6`，视觉规范同步到v1.8。

| v2评审内容 | 图片 |
| --- | --- |
| 全宽条件与结果 · 浅色 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-v2-1920-light.jpg) |
| 全宽条件与结果 · 深色 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-v2-1920-dark.jpg) |
| 收起配置，优先比较结果 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-v2-collapsed-light.jpg) |
| 三列点选编辑器 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-v2-editor-light.jpg) |
| 公式编辑与校验入口 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-v2-formula-light.jpg) |
| 中文完整草稿核对 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-v2-chinese-light.jpg) |
| 模板按需打开 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-v2-templates-light.jpg) |
| 多组条件及关系 · 深色 | [查看](2026-10-09-tradeiq-strategies-hot-sectors-assets/custom-v2-multiple-dark.jpg) |

v2验证：六种主题/宽度组合均无整体横向溢出，测量的操作控件未重叠；条件行44px，结果表格13px。配置收起保留关系与开启数量，多组内部高度限制320px。模板添加、组间且或、撤销、条件保存、开关重开持久化、公式显示、搜索排序分页、条件影响、股票核验与详情关闭已实测。点选错误提示、公式模式、中文三项草稿和未识别阻止均可正常展示。加载/空结果/缺失/失败四种状态仍保留；空结果自动打开条件影响。视觉组件页的条件行同步到v2，脚本语法检查通过，所测页面无浏览器脚本错误。六个受保护旧前端/业务文件与本轮起始摘要一致。

该轮仅核对视觉及虚构交互；实际计数、公式筛选和接口接入仍待UI确认后开发。
