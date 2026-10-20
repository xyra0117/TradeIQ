"""
主力净流入同步的独立 runner, 通过 launchctl asuser 进入用户 GUI session 跑.

为什么需要这个:
  - 后端 Flask 进程跑在 launchd Aqua session, Playwright 直接 launch Chrome 会被 macOS 拒
  - 直接 subprocess 继承父进程 session, 同样问题
  - launchctl asuser $(id -u) /usr/bin/python3 xx.py 强制让子进程进入用户的 GUI session
  - 在 GUI session 里 Playwright + channel=chrome 才能起窗口

用法:
  launchctl asuser $(id -u) /usr/bin/python3 \\
      /path/to/_sync_market_runner.py \\
      --force 1 --wait-for-user 1 --state-file /tmp/tradeiq-flow-sync-state.json

状态文件: 每次 callback 都 atomically rewrite 整个 JSON, 父进程读 /api/flow/sync-status
          时直接 load 这个文件即可. 跑完保留 finished_at 供前端展示"上次成功/失败".

为什么不开 --headless: 反爬要求真人 Chrome 指纹 (JA3 + TLS), 跟 fetch_fund_flow_today.py 同步.
"""
import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime

# 让 fetch_fund_flow_today 能 import 它的依赖
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _write_state(state_file: str, state: dict):
    """写状态文件. macOS 上 os.replace 会因 xattr 偶发 EPERM, 直接 truncate 写更稳.
    前端轮询读到的可能是写一半的内容, 但 json.load 失败会用上次成功的 state, 不影响 UI."""
    try:
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f'[runner] ⚠ 写 state 失败: {e}', flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--force', type=int, default=0, help='跳过幂等守卫, 强制重抓')
    ap.add_argument('--wait-for-user', type=int, default=0, help='第 1 页失败时弹窗等用户刷新')
    ap.add_argument('--state-file', required=True, help='状态 JSON 文件路径')
    ap.add_argument('--skip-wait', type=int, default=0, help='wait_for_user 模式下, 等用户刷新的秒数 (默认 600)')
    args = ap.parse_args()

    state = {
        'running': True,
        'started_at': datetime.now().isoformat(),
        'finished_at': None,
        'total': 0, 'done': 0, 'success': 0, 'failed': 0,
        'errors': [],
        'current_code': '准备启动 Playwright...',
        'elapsed_sec': 0,
        'source': 'eastmoney-push2',
        'target_date': '',
        'is_already_synced': False,
        'is_holiday': False,
        'actual_date': '',
        'waiting_for_user': False,
        'wait_elapsed_sec': 0,
        'wait_remaining_sec': 0,
    }
    _write_state(args.state_file, state)

    t0 = time.time()
    today = datetime.now().strftime('%Y%m%d')
    state['target_date'] = today

    def _cb(stage, **kw):
        if stage == 'start':
            ad = kw.get('actual_date', today)
            if ad != today:
                state['is_holiday'] = True
                state['current_code'] = f'⏸ {today} 非交易日, 数据将归属到 {ad}'
            else:
                state['is_holiday'] = False
            state['actual_date'] = ad
        elif stage == 'total':
            state['total'] = kw.get('total', 0)
            state['current_code'] = f'总 {kw["total"]} 只, 开始翻页...'
        elif stage == 'page':
            pn = kw.get('pn', 0)
            total_pages = kw.get('total_pages', 0)
            rows_count = kw.get('rows_count', 0)
            state['done'] = rows_count
            state['failed'] = len(kw.get('failed', []))
            state['current_code'] = f'pn={pn}/{total_pages} 累计 {rows_count} 行'
        elif stage == 'done':
            state['done'] = kw.get('rows', 0)
            state['failed'] = len(kw.get('failed', []))
            ad = kw.get('actual_date', state.get('actual_date', today))
            state['current_code'] = (
                f'抓取完成 {kw["rows"]}/{kw["total"]} 行 (写入 {ad})'
                + (f' (失败 {len(kw["failed"])} 页)' if kw.get('failed') else '')
            )
        elif stage == 'already_synced':
            ad = kw.get('actual_date', today)
            cnt = kw.get('existing_count', 0)
            state['is_already_synced'] = True
            state['actual_date'] = ad
            state['current_code'] = f'✅ {ad} 已有数据 ({cnt} 行), 跳过抓取'
        elif stage == 'waiting_for_user':
            state['waiting_for_user'] = True
            state['wait_elapsed_sec'] = kw.get('elapsed_sec', 0)
            state['wait_remaining_sec'] = kw.get('remaining_sec', 0)
            state['current_code'] = (
                f'⏳ 浏览器已弹出, 请按 Cmd+R 刷新 (已等 {state["wait_elapsed_sec"]}s, 剩 {state["wait_remaining_sec"]}s)'
            )
        elif stage == 'error':
            state['errors'].append(('eastmoney-push2', kw.get('message', '')))
        state['elapsed_sec'] = round(time.time() - t0, 1)
        _write_state(args.state_file, state)

    try:
        from fetch_fund_flow_today import fetch_today_market
        rows, total, failed, actual_date = fetch_today_market(
            progress_callback=_cb,
            headless=False,
            verbose=True,
            skip_if_exists=not args.force,
            wait_for_user=bool(args.wait_for_user),
        )
    except Exception as e:
        err_msg = f'{type(e).__name__}: {str(e)[:200]}'
        state['errors'].append(('eastmoney-push2', err_msg))
        state['failed'] = max(state['failed'], 1)
        state['running'] = False
        state['finished_at'] = datetime.now().isoformat()
        state['elapsed_sec'] = round(time.time() - t0, 1)
        state['current_code'] = f'❌ {err_msg}'
        _write_state(args.state_file, state)
        print(f'[runner] 异常退出: {err_msg}', flush=True)
        traceback.print_exc()
        sys.exit(1)

    # 幂等守卫命中 (rows 空 + is_already_synced)
    if not rows and state.get('is_already_synced'):
        state['running'] = False
        state['finished_at'] = datetime.now().isoformat()
        state['elapsed_sec'] = round(time.time() - t0, 1)
        _write_state(args.state_file, state)
        print(f'[runner] ⏭ 已有数据, 跳过 ({state["elapsed_sec"]}s)', flush=True)
        return

    if not rows:
        if not state['errors']:
            state['errors'].append(('eastmoney-push2', '抓取结果为空'))
        if not state['failed']:
            state['failed'] = 1
    else:
        # 写入 DB (复用 app.py 里的 _save_em_fund_flow_rows)
        try:
            from app import _save_em_fund_flow_rows
            inserted = _save_em_fund_flow_rows(rows, actual_date, source='eastmoney-push2')
            state['success'] = inserted
            state['current_code'] = f'✅ {actual_date} 已写入 {inserted} 行'
        except Exception as e:
            err_msg = f'写入 DB: {type(e).__name__}: {str(e)[:200]}'
            state['errors'].append(('db.write', err_msg))
            state['failed'] = 1
            state['current_code'] = err_msg

    state['elapsed_sec'] = round(time.time() - t0, 1)
    state['running'] = False
    state['finished_at'] = datetime.now().isoformat()
    _write_state(args.state_file, state)
    print(f'[runner] 完成 success={state["success"]} elapsed={state["elapsed_sec"]}s', flush=True)


if __name__ == '__main__':
    main()
