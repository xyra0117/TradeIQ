# TradeIQ

## 项目结构

```
TradeIQ/
├── dashboard/          # 前端页面（HTML + ECharts）
│   └── vendor/        # 本地化的前端依赖（chart.js/echarts/flatpickr），不走 CDN
├── backend/            # Flask 后端
│   ├── app.py         # Flask 应用 + SQLite 数据库
│   └── requirements.txt
├── docs/              # 文档
└── CLAUDE.md
```

## 后端（Flask + SQLite + TuShare）

### 启动后端
```bash
cd backend
pip install -r requirements.txt
export TUSHARE_TOKEN=你的token  # 或直接写在代码里
python app.py
```

### API 接口
- `GET /api/index/daily` - 获取指数日线数据
- `POST /api/index/sync` - 从 TuShare 同步指数数据
- `GET /api/leaderboard` - 获取涨幅榜单
- `GET /api/overview` - 获取盘面概览
- `GET /api/limitup` - 获取涨停数据
- `GET /api/stats` - 数据统计

### 数据说明
- 存储在 `backend/market_data.db`（SQLite）
- TuShare 免费版有限制，部分高级功能需付费
- 涨幅榜需要自建爬虫或购买数据源

### 前端依赖
- 不引用外部 CDN（jsdelivr 等）。本机代理节点连不上 jsdelivr，CDN 一挂页面就是「图表全空 + flatpickr is not defined」
- 所有图表库放 `dashboard/vendor/`，页面用 `/dashboard/vendor/xxx` 引用
- Google Fonts 走代理正常，可继续用 `@import`

## 同花顺概念板块

数据表（`market_data.db`）：`ths_concept`（板块）/ `ths_concept_stock`（成分股）
- 来源：`q.10jqka.com.cn/gn/`（361 个概念板块）→ 详情页成分股涨跌排行榜
- 板块类型用 `--type` 切换，`board_type` 列记录类型：

| type | 页面 | 板块数 | 代码形态 |
|---|---|---|---|
| concept | `q.10jqka.com.cn/gn/` | 361 | 数字 30xxxx |
| thshy | `q.10jqka.com.cn/thshy/` | 90 | 数字 881xxx |
| zjhhy | `q.10jqka.com.cn/zjhhy/` | 19 | 字母 A~Z |
| dy | `q.10jqka.com.cn/dy/` | 33 | 数字 882xxx |

**当前数据状态（2026-10-30）**
- 已抓到：concept 361 个 / 67180 条成分股 + 71 个概念的图谱；zjhhy 19 个 / 5572 条
- **没抓到**：thshy（同花顺行业 90 个）只有板块名和代码，成分股是空的——抓的过程中被同花顺按 IP 封了
- 备份：`backend/ths_concepts.db` 存着 concept 的完整副本（不入库）
- 续抓（解禁后）：`python3 backend/retry_ths_crawl.py --type thshy`

- 脚本：
  - `backend/fetch_ths_concepts.py` — 抓板块列表 + 成分股，匿名只能拿到涨跌幅前 50 只
  - `backend/fetch_ths_concepts_full.py` — 浏览器会话版，可翻完全部页；弹窗登录后自动继续
  - `backend/merge_ths_concepts.py` — 独立库 `ths_concepts.db` 合并进 `market_data.db`
  - `backend/watch_ths_crawl.py` — 实时盯梢抓取进度
  - `backend/retry_ths_crawl.py` — 同花顺按 IP 限流被封时，等解禁自动续抓

```bash
python3 backend/fetch_ths_concepts_full.py --type thshy --db backend/market_data.db
```

### 概念图谱（产业链一览）
- 数据表：`ths_concept_graph`，单表三层，`level` 区分：1=概念本身、2=产业链子板块、3=个股
- 脚本：`backend/fetch_ths_concept_graph.py --write`（只抓每个概念详情页第 1 页，361 个请求约 5 分钟）
- 只有约四分之一的概念有图谱（THS 只给定义了产业链的概念做）
- 子板块属于第几条产业链看 class：`label-m down` = 下排（产业链2），无 down = 上排（产业链1）

## 财联社板块

数据表（`market_data.db`）：`cls_plate`（板块）/ `cls_plate_stock`（成分股）
- 接口在 `x-quote.cls.cn`，**不需要签名、不限 IP**（比同花顺干净得多）
- 板块类型：`industry` 54 个 / `concept` 398 个 / `area` 32 个（去重后的真实数量）
- 脚本：`backend/fetch_cls_plates.py --type concept --write`
- **列表要按 `secu_code` 去重**：列表按涨跌幅排序，翻页时排序会变，同一板块会在多页重复出现

| 接口 | 用途 |
|---|---|
| `/web_quote/plate/plate_list?type=&way=change&page=1&rever=1` | 板块列表，每页 30，`is_all=1` 到底 |
| `/web_quote/plate/info?secu_code=clsXXXXX` | 板块详情：涨跌幅/周月季年/主力资金/涨停家数/板块简介 |
| `/web_quote/plate/stocks?secu_code=clsXXXXX` | 成分股，**一次返回全部**（page 参数无效），数量 = 涨+跌+平家数 |
| `/web_quote/plate/industry?secu_code=clsXXXXX` | 相关板块，多数为空 |

- 板块代码形如 `cls80481`；成分股代码 `sh688073` → 存成 `688073.SH`
- 涨跌幅是小数（0.0234 = 2.34%）

### 同花顺抓取注意事项
- 页面是 GBK 编码
- ajax 翻页端点（`…/ajax/1/…`）有 WAF，直接 401；普通分页 URL `…/detail/code/{code}/page/{n}/` 能过
- **匿名访问第 6 页起 302 跳登录页**（真实 Chrome、无痕 session 都一样），要全量成分股必须先在真浏览器里登录一次
- 抓取必须绕开系统代理（Clash），走代理会挂住；脚本里用 `ProxyHandler({})` 直连
- 同花顺概念成分股每天会变，定期用 `--resume` 重抓

## Agent skills

### Issue tracker

Issues live in GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Uses default label names: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.