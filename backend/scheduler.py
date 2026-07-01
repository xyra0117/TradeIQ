"""APScheduler 调度框架 (A 股情绪分析系统)

设计原则:
- 阶段 0.4: 此文件只装框架, 不注册具体任务. 各阶段往 register_* 函数里塞任务.
- 单例 BackgroundScheduler, 启动在 app.py 末尾 if __name__ == '__main__'.
- 任务注册失败不阻塞启动, 仅 print 日志.

任务规划 (各阶段注册):
- 阶段 1 (情绪指数): register_sentiment_sampling - 交易时段 09:30-11:30 + 13:00-15:00 每 15 秒采样
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


# ============ 各阶段任务注册函数 (各阶段实现时填充) ============

def register_sentiment_sampling():
    """阶段 1: 交易时段每 15 秒采一次情绪指数.
    触发时间: 09:30-11:30 + 13:00-15:00 的每 15 秒 (秒 0/15/30/45).
    任务实际函数在 sentiment.py 里实现, 这里通过 lazy import 避免循环依赖."""
    from datetime import time as _time
    def _job():
        from sentiment import sample_intraday
        sample_intraday()
    sched = get_scheduler()
    # 两个时间区间, 每天 0/15/30/45 秒触发
    trigger = CronTrigger(
        hour='9-11,13-14',
        minute='*',
        second='0,15,30,45'
    )
    safe_register(_job, trigger, id='sentiment_sampling',
                  name='情绪指数盘中 15 秒采样 (09:30-11:30 + 13:00-15:00)')


def register_daily_review():
    """阶段 3: 每个交易日 15:30 自动生成盘后复盘报告."""
    trigger = CronTrigger(hour=15, minute=30)
    def _job():
        from review import generate_today
        generate_today()
    safe_register(_job, trigger, id='daily_review',
                  name='盘后复盘 15:30 自动生成')


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
