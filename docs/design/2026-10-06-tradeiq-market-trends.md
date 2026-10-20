# TradeIQ 简易版市场总览 · 两组趋势图UI

日期：2026-10-06。仅补UI和虚构数据交互，不接入行情接口，不改旧页面或后台。

[打开新版简易版市场总览](http://127.0.0.1:5556/index.html?page=index&view=overview&simple=1)

[直接定位两组趋势图](http://127.0.0.1:5556/index.html?page=index&view=overview&simple=1&focus=trends&preview=20261006.6)。图表位于指数卡下方；较窄预览窗口上下排列，第二图需继续向下滚动。HTML的本地样式和脚本统一增加版本参数，避免资源缓存混用。2026-10-06在Codex内置浏览器1047×664px窗口核对两图可见，预览服务返回正常；未将缓存推断为原先未显示的确定原因。

## 老版核对与新版布局

已核对旧前端 `dashboard/index.html` 中 `smPanel-overview`、`smTrendChart`、`smTrendChart2` 的图表组成、图例、坐标轴和区间操作。老版新增市场总览入口，两个趋势图位于统计与指数卡下方。新版沿用这一信息层级，简易版现在包含市场总览、盘面晴雨表、个股搜索、选股推荐四个入口，进入简易版时默认市场总览。

| 图表 | 指标 | 坐标轴 |
| --- | --- | --- |
| 指数与分层资金 | 上证指数、科创50、大盘超大单、大盘小单 | 指数右轴 / 点；资金左轴 / 亿元 |
| 市场广度与主力资金 | 上涨家数、下跌家数、成交额、主力净流入 | 家数右外轴 / 家；成交额右轴 / 亿元；主力资金左轴 / 亿元 |

旧版第二图提示文案把资金轴写成右轴，但实际图表配置为左轴。新版按真实坐标配置标明左轴，避免同屏提示相互矛盾。

页面和按钮使用已有灰阶；指数、成交额和分层资金用灰阶线型区分，涨跌家数保留低饱和红绿。资金轴包含虚线零轴，独立单位出现在轴标签和悬停提示中。桌面两图并排，较窄窗口纵向排列；指数卡与趋势绘图区保持紧凑，较矮窗口允许内容区滚动。

## 可操作的UI

- 每图独立切换近10/20/60日、全部；“全部”显示90个内置虚构工作日点位。
- 四条图例分别开关，提示只显示当前开启的指标；全部关闭时显示恢复说明。
- 悬停查看同日各项数值、单位和日期；资金负值保留零轴参照。
- 滚轮缩放、主图拖动平移、底部时间窗口拖动与两端调整；按评审取消独立的起点 / 终点滑杆，时间轴最新规格为16px高。选中区间复用规范的灰阶选中色和强调细边框，取消原主力净流入缩略曲线，短柄支持键盘调整。近10/20/60日与全部沿用既有`period-pills`时间范围分段组件。
- 图表可聚焦后使用左右键平移、加减键缩放；图例和区间按钮可通过键盘操作。
- 顶栏日期为截止日期，切换日期重置两图至近10日，避免旧缩放状态串到新日期。
- 加载、空结果、失败和部分缺失均可在顶部“设计预览”菜单查看；缺失示例在曲线上断开，不补成0。
- 浅色、深色及侧栏继续复用原工作台。完整版入口、晴雨表、个股搜索和选股推荐仍保留。
- UI组件展示页第14类“业务功能组件”增加两组趋势图及市场总览直达入口。

## 评审图

| 内容 | 浅色 | 深色 |
| --- | --- | --- |
| 市场总览完整画面 | [浅色](2026-10-06-tradeiq-market-trends-assets/market-overview-light.png) | [深色](2026-10-06-tradeiq-market-trends-assets/market-overview-dark.png) |
| 初版两组趋势图 | [浅色](2026-10-06-tradeiq-market-trends-assets/market-trends-light.png) | [深色](2026-10-06-tradeiq-market-trends-assets/market-trends-dark.png) |
| 初版UI组件中的示例 | [浅色](2026-10-06-tradeiq-market-trends-assets/trend-components-light.png) | [深色](2026-10-06-tradeiq-market-trends-assets/trend-components-dark.png) |
| v1.3精简时间轴工作台 | [浅色](2026-10-06-tradeiq-market-trends-assets/compact-timeline-light.png) | [深色](2026-10-06-tradeiq-market-trends-assets/compact-timeline-dark.png) |
| v1.4纯区间16px时间轴 | [浅色](2026-10-06-tradeiq-market-trends-assets/range-timeline-light.png) | [深色](2026-10-06-tradeiq-market-trends-assets/range-timeline-dark.png) |
| v1.5规范对齐后的工作台 | [浅色](2026-10-06-tradeiq-market-trends-assets/aligned-controls-light.png) | [深色](2026-10-06-tradeiq-market-trends-assets/aligned-controls-dark.png) |
| v1.6下划线导航Tab | [浅色](2026-10-06-tradeiq-market-trends-assets/simple-tabs-light.png) | [深色](2026-10-06-tradeiq-market-trends-assets/simple-tabs-dark.png) |

## 验证与边界

系统Google Chrome下初版完成73项断言。工作台在1440/1920/2560px × 双主题共6种布局、组件展示在三个宽度 × 浅色/深色/并排共9种布局通过；未出现页面横向溢出。覆盖两个独立区间、图例、单位、悬停、滚轮、主图平移、时间窗起点拖动、初版起止滑条、键盘、日期重置、五种数据状态和原有简易版入口。无JavaScript错误、资源失败或业务API请求。后续时间轴调整单独记录在验证摘要的`compact_timeline_followup`中：内置浏览器核对两端拖动、选区平移、键盘调整和两图独立区间，6种工作台布局及3种组件黑白对照布局均无横向溢出，时间轴均为20px高。

v1.4历史验证（选区配色已由v1.5替代）记录于`range_timeline_followup`：6种工作台布局与1280px组件黑白对照通过；轨道总高16px、没有SVG或缩略曲线，选区与轨道的颜色对比超过10:1。两端拖动、整体平移、键盘调整及两图独立区间通过。

v1.5规范对齐验证记录于`alignment_followup`：两张图复用`period-pills`分段控件，按钮选中面与时间轴选区均准确匹配`--selected`（浅色#E7E7E4、深色#2C2E30），不再使用主要按钮底色。6种工作台布局及组件黑白对照无横向溢出，标题与范围控件不重叠；四档区间的选中类和可访问状态一致，自定义区间清除快捷选中态。

v1.6简易版导航Tab验证记录于`simple_tabs_followup`：顶部四个入口复用39px高、13px文字、2px下划线的策略Tab视觉，选中项不再填充背景。浅色/深色 × 1440/1920/2560px六种布局无横向溢出或Tab与说明重叠。市场总览、盘面晴雨表、选股推荐切换后选中类与可访问状态一致，个股搜索打开及关闭均保留原页面选中态；完整/简易模式切换及原策略栏正常。组件展示页补充黑白对应样例，交互与工作台均无JavaScript错误。[导航组件黑白对照](2026-10-06-tradeiq-market-trends-assets/simple-tabs-components-comparison.png)。

四个前端脚本语法及差异格式检查通过。工作台与后台文件在本轮前后的摘要一致，保留了此前其他任务的修改。本轮没有启动行情后台、采集或写入数据库；预览为本地独立静态服务。

所有日期和值为视觉示例；真实交易日历、全历史行情、接口加载与请求竞态，以及单位换算均需在后续真实开发阶段核验。本轮不宣称这些真实能力已开发完成。入口与示例图表代码分别沿用 `workspace.js`、`pages.js`、`preview-charts.js`，未新增依赖。

[本轮验证摘要](2026-10-06-tradeiq-market-trends-assets/verification.json) · [素材与源文件摘要](2026-10-06-tradeiq-market-trends-assets/manifest.json)
