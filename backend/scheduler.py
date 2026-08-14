"""APScheduler 调度框架 (A 股)

设计原则:
- 此文件只装框架, 不注册具体任务. 各功能往 register_* 函数里塞任务.
- 单例 BackgroundScheduler, 启动在 app.py 末尾 if __name__ == '__main__'.
- 任务注册失败不阻塞启动, 仅 print 日志.

任务规划 (各功能注册):
- 阶段 3 (盘后复盘): register_daily_review - 每个交易日 15:30 自动生成报告
- 阶段 5 (推送): register_push_jobs - 09:25/11:35/15:05/15:30 定时推送
"""
import sys
import traceback
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

_scheduler = None


def get_scheduler():
    """获取全局单例调度器, 不存在则创建 (未启动)."""
    global _scheduler
    if _scheduler is None:
        _scheduler = BackgroundScheduler(
            daemon=True,
            job_defaults={
                'coalesce': True,        # 堆积的任务合并成一次
                'max_instances': 1,      # 同一任务同时只跑 1 个实例
                'misfire_grace_time': 60,  # 错过的任务 60s 内补跑
            }
        )
    return _scheduler


def safe_register(func, trigger, id, name=None, replace_existing=True, **kwargs):
    """注册任务的安全包装: 失败不抛, 仅 print 日志."""
    sched = get_scheduler()
    job_name = name or id
    try:
        sched.add_job(func, trigger, id=id, replace_existing=replace_existing, **kwargs)
        print(f'[scheduler] 注册任务: {job_name} (trigger={trigger})')
        return True
    except Exception as e:
        print(f'[scheduler] 注册失败 {job_name}: {e}')
        traceback.print_exc()
        return False


# ============ 各功能任务注册函数 ============

def register_daily_review():
    """阶段 3: 每个交易日 15:30 自动生成盘后复盘报告."""
    trigger = CronTrigger(hour=15, minute=30)
    def _job():
        from review import generate_today
        generate_today()
    safe_register(_job, trigger, id='daily_review',
                  name='盘后复盘 15:30 自动生成')


def register_picks_resync_after_close():
    """盘中模式下, 14:30~15:30 sync 写到 stock_picks 表的 100 条是基于盘中实时价.
    A 股收盘 15:00 后, TuShare 当天数据一般在 15:30~16:00 入库 stock_daily.
    此任务每天 16:00 重新计算当日 stock_picks, 用 TuShare 真实收盘价覆盖盘中那批.
    若 TuShare 当天还没入库 (极端情况), 重算可能返 0 条; 用户手动 sync 一次即可."""
    trigger = CronTrigger(hour=16, minute=0)
    def _job():
        from datetime import datetime
        today = datetime.now().strftime('%Y%m%d')
        try:
            from app import _compute_picks, _write_stock_picks_for_date
            picks = _compute_picks(trade_date=today)
            if not picks:
                print(f'[picks_resync] {today} 重算无数据 (TuShare 可能还没入库)')
                return
            # 写入 stock_picks 表 (先删后插, 收盘口径覆盖盘中口径; 逻辑在 app.py 共用)
            _write_stock_picks_for_date(today, picks)
            print(f'[picks_resync] {today} 收盘后自动重算完成: {len(picks)} 只 (覆盖盘中口径)')
        except Exception as e:
            print(f'[picks_resync] {today} 重算失败: {e}')
            import traceback; traceback.print_exc()
    safe_register(_job, trigger, id='picks_resync_after_close',
                  name='收盘后重算 stock_picks (16:00, 覆盖盘中口径)')


def register_conditional_combined_snapshot():
    """每天 17:00 自动算一次当日条件选股 + 组合选股, 写入 snapshot 表.
    盘中切换 tab 会实时算 (不入库), 17:00 兜底确保历史日期有数据可查.
    与 picks_resync_after_close 错开 1 小时, 等收盘后行情 / 资金流 / 系统推荐都稳定再算."""
    trigger = CronTrigger(hour=17, minute=0)
    def _job():
        from datetime import datetime
        today = datetime.now().strftime('%Y%m%d')
        try:
            from flask import current_app
            with current_app.test_client() as c:
                r1 = c.get(f'/api/conditional-picks?date={today}')
                r2 = c.get(f'/api/combined-picks?date={today}')
                d1 = r1.get_json()
                d2 = r2.get_json()
                print(f'[picks_snapshot] {today} 条件选股入库: count={d1.get("count")}, '
                      f'组合选股: count={d2.get("count")}', flush=True)
        except Exception as e:
            print(f'[picks_snapshot] {today} 快照入库失败: {e}')
            import traceback; traceback.print_exc()
    safe_register(_job, trigger, id='picks_snapshot_after_close',
                  name='盘后入库条件/组合选股 (17:00, 历史查询复用)')


def register_stock_daily_backfill():
    """盘后自动把 stock_daily 入库.
    TuShare 16:00 后 stock_daily 一般入库完成; 15:35 抢跑同步当日,
    若 TuShare 还未入库则静默返 0 不报错, 16:30 再补一次兜底(防 TuShare 延迟).
    每周末最多补 7 天 (回溯 7 日), 防止某次重启后断档."""
    triggers = [
        (15, 35, 'stock_daily_after_close'),
        (16, 30, 'stock_daily_late_sync'),
    ]
    for hh, mm, jid in triggers:
        trigger = CronTrigger(hour=hh, minute=mm)
        def _job(_jid=jid):
            from datetime import datetime, timedelta
            from flask import current_app
            # 回溯 7 日, 简单稳定: 即使当天入库失败, 下次可补; 非交易日入库返 0, 无害
            today = datetime.now().strftime('%Y%m%d')
            with current_app.test_client() as c:
                # 用 sync_stock_daily_api: 不传 date, 服务端自己找最新一个交易日
                # (注意该路由只注册 POST, 用 GET 会 405 静默失败)
                r = c.post('/api/stock/sync')
                data = r.get_json() or {}
                print(f'[{_jid}] {today} sync_stock_daily -> {data}', flush=True)
        safe_register(_job, trigger, id=f'{jid}',
                      name=f'盘后同步 stock_daily ({hh:02d}:{mm:02d})')


def register_push_jobs():
    """阶段 5: 5 个推送时机 (09:25/盘中/11:35/15:05/15:30).
    盘中实时由预警规则触发, 走 feishu.py.push_alert() 不走 cron."""
    sched = get_scheduler()
    triggers = [
        ('09:25', 'morning_brief',  '早盘速览 09:25'),
        ('11:35', 'noon_brief',     '午间简评 11:35'),
        ('15:05', 'close_brief',    '收盘速报 15:05'),
        ('15:30', 'full_review',    '完整复盘 15:30'),
    ]
    for hm, jid, name in triggers:
        h, m = hm.split(':')
        trigger = CronTrigger(hour=int(h), minute=int(m))
        def _job(_jid=jid):
            from feishu import push_scheduled
            push_scheduled(_jid)
        safe_register(_job, trigger, id=f'push_{jid}', name=f'飞书推送 - {name}')


def start_scheduler():
    """启动调度器 (在 app.py 启动时调用)."""
    sched = get_scheduler()
    if not sched.running:
        sched.start()
        print(f'[scheduler] 启动, 已注册 {len(sched.get_jobs())} 个任务')
    else:
        print(f'[scheduler] 已在运行, {len(sched.get_jobs())} 个任务')


def shutdown_scheduler():
    """关闭调度器 (测试用)."""
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        print('[scheduler] 已关闭')
    _scheduler = None
