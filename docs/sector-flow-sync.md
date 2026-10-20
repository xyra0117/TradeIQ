# 需要真浏览器的同步任务（东财个股/板块资金流、同花顺资金流）

## 根因：浏览器不能在 Flask 进程里起

这几项同步都要用真浏览器抓页面（东财接口只认浏览器指纹，同花顺还必须真 Chrome 有界面 + 反检测标记）。而**Flask 进程内 launch Chrome 会被 macOS 直接拒掉**：

```
TargetClosedError: BrowserType.launch: Target page, context or browser has been closed
```

跟东财反爬无关，是进程环境问题：**有界面**的浏览器要 GUI session 凭据，Flask 起的浏览器拿不到 → 立刻 crash。（无头的 `headless=True` 不受影响，所以「生成预览图」那种渲染在 Flask 里能跑。）

所以凡是「有界面浏览器」的同步，一律走**独立 launchd agent**，Flask 只负责 trigger + 轮询状态文件。

## 架构

```
拉取目标面板点一下 / 盘后定时任务
        ↓ HTTP
Flask  POST /api/flow/{sector,ths}/sync
        ↓ HTTP (127.0.0.1:5556)
agent  sync_agent.py  →  POST /sync/{sector,ths}
        ↓ 线程
        Playwright (真浏览器) → fetch_*.run()
        ↓
写库 (fund_flow / sector_fund_flow / fund_flow_ths) + 状态文件
        ↓ Flask 轮询 state file
面板显示进度
```

- agent 进程：`backend/sync_agent.py`，独立跑，stdout 在 `backend/tradeiq-syncagent.log`。**改了 agent 的代码要重启它**（不热重载）。
- 状态文件：`/tmp/tradeiq-sector-sync-{industry,concept}.json`、`/tmp/tradeiq-ths-sync-state.json`、`/tmp/tradeiq-flow-sync-state.json`（个股）。
- 三个任务的分工：个股主力净流入 `/sync/em`、板块资金流 `/sync/sector`、同花顺个股资金流 `/sync/ths`。
- 盘后定时任务只 HTTP 触发本机 Flask，不 import app（避免在调度线程里重复加载整个模块）。

## 各通道

| 任务 | 接口 | 特点 |
|---|---|---|
| 东财个股主力净流入 | `push2 clist/get?fs=m:0+t:6` | 5,300 只左右 |
| 板块资金流（主） | `push2 clist/get?fs=m:90+t:2`（行业）/ `m:90+t:3`（概念） | 一次 100 行，5 页拿全 496/504 个板块，10~60 秒 |
| 板块资金流（兜底） | `push2his fflow/daykline/get?secid=90.BKxxxx` | 一个板块一次请求，慢（1000 个 ≈ 80 分钟）但抗盯防。clist 一行都没拿到时自动兜底（`backfill_sector_flow_daykline.py --date <当天> --skip-existing`，脚本自己写库） |
| 同花顺个股资金流 | `data.10jqka.com.cn` 翻页 | 105 页，约 5,200 只，100 秒 |

东财的 WAF 会按 IP 盯防，被盯上时 push2 / push2his 都 403 或连接重置（datacenter-web 和普通页面正常，说明只有行情 API 家族被封）。这是外部状态，代码只能做四件事：重试、换通道、换时段、把错误显示出来。

## 抗盯防的措施

1. **换 session 重试**（板块）：第 1 页拿不到 = 大概率被 IP 级冷却了。重新起浏览器、重新预热页面，退避 20/40 秒重试，最多 3 次（`SECTOR_SYNC_RETRIES` 可调）。
2. **单页重试**（板块）：翻页中途某页空了，重试 2 次（退避 5/10 秒）再记失败。
3. **盘后多时段兜底**：板块 15:35 / 15:36 / 16:30 / 16:31 / 20:00 / 20:01 六轮；同花顺 17:05 / 19:30 两轮。晚间那几轮专治下午被盯防的。
4. **换通道**：板块 clist 全军覆没时自动走 daykline。

## 幂等

- `skip_if_exists`：当天已有数据直接跳过，不重拉（`force=1` / 面板强制刷新除外）。同花顺是 ≥4000 行才算已有。
- 写库按 (trade_date, 类型, 代码) 先删后插，重复拉不会写重。

## 手动补

面板点「板块资金流·行业 / 概念」「资金流·同花顺」那几项即可（强制重抓覆盖）；或直接跑脚本：

```bash
cd backend
python3 fetch_fund_flow_sector.py --type industry --force            # 走 clist
python3 backfill_sector_flow_daykline.py --date 20261020 --skip-existing   # 走 daykline
python3 fetch_fund_flow_ths.py --force                              # 同花顺
```

## 验证

2026-10-20：把板块资金流和同花顺资金流都从「Flask 里起浏览器」改成「走 agent」。同花顺实测：触发后 100 秒翻完 105 页，写入 5,217 行，0 失败，面板「资金流·同花顺」变「已拉全」。板块实测：行业 496 行、概念 516 行（source=eastmoney-push2，即 clist 主通道）。当天中途东财 WAF 盯过这台机器的 IP（浏览器和 curl、直连和代理对 push2/push2his 全 403 / 连接重置），冷却过后重试即恢复。
