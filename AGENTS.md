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
