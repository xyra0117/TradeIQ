# 复权因子

## 是什么

`adj_factor` 表存 TuShare 的每日复权因子（`ts_code` + `trade_date` + `adj_factor`），是客观事实，跟选股口径无关，一天拉一次就不会变。

前复权价**不落库**，查询时现算：`qfq_close = close * f_t / f_latest`（f_t 是当天的因子，f_latest 是最新交易日的因子）。原始收盘价永远保留在 `stock_daily` 里，分红送转后只需要重算，不用重写历史行情。

目前**还没有任何功能在用这张表**——条件选股、箱体突破、涨幅统计用的都是不复权价。要接复权口径时，取数处按上面的公式现算即可。

## 数据现状

- 全量回填脚本：`backend/backfill_adj_factor.py`（从 2020-01-02 起，1462 个交易日，每个交易日约 5,570 只，覆盖率基本 100%）。
- 日常增量：`backend/adj_factor_sync.py`。
- 零星日期差 1～4 只（TuShare 当天返回就少几只，或本地有新股没上市因子），属正常。

## 拉取目标面板

顶栏「数据拉取情况」点开就是**拉取目标**面板，里面列了每个数据源在选定日期拉全了没有。复权因子是其中一项：

- 绿色「已拉全」：当天 ≥5000 行。
- 黄色「不完整」：有一部分。
- 灰色「无数据」：当天还没到拉取时间（当天盘后 17:00 TuShare 才陆续入库，当天 17:00 前没数据是正常的）。
- 红色「缺失」：该拉的没拉 —— 点这一项就能补，跟其它数据源一个入口（`POST /api/sync/run/adj_factor?date=YYYYMMDD`），可重复点，重复拉是覆盖写。

## 盘后自动拉

`register_adj_factor_after_close()` 注册两个时间点：**17:05 主跑 + 18:30 兜底**（TuShare 当天因子通常 17:00 后才陆续入库，晚了就补第二次）。每次跑会顺手把最近 6 个交易日里空着的日子补上（机器关机/断网漏掉的，下次自动补，不重复拉已存在的）。

调度线程里没有 Flask app context，所以这个任务直接调 `adj_factor_sync.sync_recent()`，不走 `current_app.test_client()`（走那条路会报 "Working outside of application context"）。TuShare token 由 `app.py` 传进来。

## 数据与接口

- 表：`adj_factor(ts_code, trade_date, adj_factor)`，主键 (ts_code, trade_date)，索引 idx_adj_factor_date。
- `POST /api/adj-factor/sync?date=YYYYMMDD` 拉某一天；`?recent=N` 补最近 N 个交易日里空着的日子。返回 `{status, date, rows, skipped, reason}`。非交易日、TuShare 还没入库都算 skipped，不算失败。
- 只入库本地 `stock_basic` 里有的股票，跟全量回填脚本同口径；因子必须是正数，NaN / 0 / None 跳过。
- TuShare 免费接口有每分钟调用上限，同一批多天之间留 0.4 秒间隔。

## 验证

2026-10-20：后端新增 17 项测试（只写本地股票、非法因子跳过、重复拉覆盖写、空日/接口报错算 skipped、接口契约、面板状态口径、调度任务注册与无 app context 也能跑、token 三级回退），自定义/低吸/追高/复权因子共 107 项通过。真实环境验收：通过「拉取目标」同一条接口真拉了 20261019，3.5 秒写入 5,577 行，面板状态从「缺失」变「已拉全」；手动触发盘后任务，今天（20261020）因子 TuShare 还没入库，返回补 1 天写 0 行、不报错。
