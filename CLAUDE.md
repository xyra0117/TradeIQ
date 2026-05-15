# TradeIQ

## 项目结构

```
TradeIQ/
├── dashboard/          # 前端页面（HTML + ECharts）
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

## Agent skills

### Issue tracker

Issues live in GitHub Issues. See `docs/agents/issue-tracker.md`.

### Triage labels

Uses default label names: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.