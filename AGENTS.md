# TradeIQ

## 项目结构

```
TradeIQ/
├── dashboard/          # 前端页面（HTML + ECharts）
├── backend/            # Flask 后端
│   ├── app.py         # Flask 应用 + SQLite 数据库
│   └── requirements.txt
├── docs/              # 文档
└── AGENTS.md
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
- `GET /api/index/kline?ts_code=000001.SH` - 获取指定指数全部历史日 K（副图为成交额/亿元）
- `GET /api/index/minline?ts_code=000001.SH&date=YYYYMMDD` - 获取经过日期核验的指数分时；无对应历史数据时返回空结果和说明
- `POST /api/index/sync` - 从 TuShare 同步指数数据
- `GET /api/leaderboard` - 获取涨幅榜单
- `GET /api/overview` - 获取盘面概览
- `GET /api/limitup` - 获取涨停数据
- `GET /api/stats` - 数据统计

### 数据说明
- 存储在 `backend/market_data.db`（SQLite）
- TuShare 免费版有限制，部分高级功能需付费
- 涨幅榜需要自建爬虫或购买数据源

## 验证约定

### 条件优选

- `/api/conditional-picks/preferred` 继承条件选股新的参数和基础名单；新增计算只读现有表，不新增数据库结构或后台任务。
- 核心条件：入选日小单净额 < 0；最近 5 个本地交易日资金和成交额完整，主力累计净额 / 对应成交额 ≥ 5%。成交额 `stock_daily.amount` 单位千元，资金净额单位元。
- 辅助排序：成交量为前日 70%～80%、收盘价 ≤ MA60，各命中一项优先一级；同级按主力强度降序。MA60不足不加分，不把辅助项解释为收益预测。
- T+N 为前 N−1 日收盘涨跌幅算术和，加第 N 日最高涨幅；只使用对应交易日完整收盘日线，缺记录或参考价不一致不计算该期及后续。
- 市场背景使用入选日 `overview` 的上涨、下跌、平盘家数，并展示上证、深证、创业板、科创50分时；必须核验分时行情日期与入选日一致，缺失时明确提示，不用最新分时替代历史日期。
- 盘中若今天对应历史入选日的 T+1～T+5 任一列，条件选股、条件选股新、条件优选都需用当日实时价和截至当前最高价补齐该列；日涨跌幅以当日昨收为基准，累计口径仍从入选日连续计算，盘后切回 `stock_daily`。
- 策略说明在 `docs/conditional-preferred.md`；验证使用 `backend/test_conditional_preferred.py` 与 `dashboard/test-conditional-preferred.cjs`，再检查浏览器日期切换及个股弹窗。

### 手机只读版结构约定

- `backend/mobile_app.py`：独立 Flask 手机服务，只注册 `/api/mobile/` 查询与手机静态资源；不导入 `app.py`、初始化数据库或启动采集任务。
- `backend/mobile_data.py`：参数化只读查询，连接既有 `backend/market_data.db`，使用 SQLite URI `mode=ro` 与 `query_only`；不创建或迁移数据库。
- `backend/requirements-mobile.txt`：手机服务的最小依赖，安装到项目 `.venv`，不安装全局依赖。
- `dashboard/mobile/`：手机 HTML、CSS、JS、manifest、service worker 和图标；`vendor/` 仅存固定版本第三方浏览器库及许可证。不保存用户数据或构建缓存。
- `docs/mobile.md`：本地启动、Tailscale 私人连接、iPhone 安装及验收说明。
- 手机测试使用 `backend/test_mobile.py`、`backend/test_mobile_browser.py` 和 `dashboard/test-mobile.cjs`；浏览器测试临时文件和截图放系统临时目录，不进入项目。
- 手机资源升级时更新 service worker 缓存版本，只清理本应用的过期资源缓存；API 与个人数据不进入持久缓存。
- 手机权限由独立服务、数据库只读连接及 Tailscale 访问规则共同保证，不依赖隐藏按钮、User-Agent 或客户端传入的只读参数。

- 后端接口回归测试放在 `backend/test_*.py`，使用 Python `unittest`；测试使用内存数据库和模拟行情请求，不初始化或改写实际数据库。
- 指数弹窗接口验证：`python3 -m unittest discover -s backend -p 'test_index_charts.py'`。
- 前端逻辑测试放在 `dashboard/test-*.cjs`，使用 Node 内置断言和模拟 DOM；指数弹窗验证：`node dashboard/test-index-charts.cjs`。
- 前端修改需检查页面内 JavaScript 语法，并验证指数及个股弹窗的切换、关闭与日期范围。

## Agent skills

### Issue tracker

Issues live in GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Uses default label names: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
