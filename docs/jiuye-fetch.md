# 韭研公社涨停简图自动拉取

## 功能

"涨停拆解明细"页加了一个"📡 拉取韭研"按钮。点击后输入日期（YYYY-MM-DD），后端会：

1. 用 Playwright 真实浏览器打开 `https://www.jiuyangongshe.com/action/{date}`
2. 浏览器自动加载 cookie，点击"涨停简图" tab
3. 前端 JS 发出 `POST /api/v1/action/diagram-url` 请求，浏览器拦截响应
4. 拿到 `data` 字段的 CDN 图 URL，下载到 `backend/uploads/jiuye_YYYYMMDD_xxx.png`
5. 触发现有 `run_ocr_job` 流程，mmx 识别 → 解析 → 写入 `limitup` 表

全程异步，前端轮询 `/api/limitup/parse-status/<job_id>` 显示进度（约 3-4 分钟）。

## 为什么用 Playwright 不用 requests

直接用 `requests` 调 API 会被服务端拒绝（errCode=1 "登录失效" / errCode=9 "token无效"）。
真实浏览器执行 JS 时服务端会放行 —— sessionToken 是在浏览器内 JS 上下文里动态注入的，
不能简单从 cookie 文件里读出来复用。

## 文件清单

| 文件 | 作用 |
|------|------|
| `backend/.jiuye_cookies.json` | 从浏览器 F12 导出的 cookies（不入库） |
| `backend/app.py` 新增 `fetch_jiuye_diagram()` | 启动浏览器 → 拦截 API → 下载图 |
| `backend/app.py` 新增 `/api/limitup/fetch` | 端点：调 fetch_jiuye_diagram → 启 OCR |
| `backend/app.py` 抽出 `run_ocr_job()` | 模块级函数，/parse-image 和 /fetch 共用 |
| `dashboard/index.html` 新增 `setupLuFetch()` + `fetchAndParseLu()` | 拉取按钮 + 轮询逻辑 |

## 重新导出 cookie（cookie 失效时）

当后端返回 `errCode=1 登录失效` 或 `errCode=9 token无效` 时：

1. 浏览器登录 `https://www.jiuyangongshe.com`
2. F12 → Application → Cookies → `https://www.jiuyangongshe.com`
3. 全选所有 cookie → 复制
4. 转成 JSON 数组格式写入 `backend/.jiuye_cookies.json`：

```json
[
  {"name": "time", "value": "1", "domain": ".jiuyangongshe.com", "path": "/"},
  {"name": "admin", "value": "...", "domain": ".jiuyangongshe.com", "path": "/"},
  {"name": "SESSION", "value": "...", "domain": ".jiuyangongshe.com", "path": "/"},
  ... 其他 cookie
]
```

注意：
- 至少要保留 `admin`、`SESSION`（含 sessionToken 的那个）
- `domain` 用 `.jiuyangongshe.com`（带点）以覆盖子域
- 写入后**不需要重启后端**，下次 /fetch 拉取时自动读最新文件

## 使用限制

- 韭研公社对请求频率有反爬，**两次拉取间隔建议 ≥ 5 秒**
- 单次拉取耗时 30-60 秒（Playwright 启动 + 页面加载 + 等前端 JS 响应 + 下载图）
- OCR 部分再加 3-4 分钟（mmx vision describe）
- Playwright 启动时占用 ~100MB 内存；高并发场景需自行加锁

## 安全提醒

- `backend/.jiuye_cookies.json` 包含你的个人账号凭证，**严禁提交到 git**
- `.gitignore` 已添加 `backend/.jiuye_cookies.json` 保护
- 本地数据库 `backend/market_data.db` 同样不入库（含你的持仓数据）
