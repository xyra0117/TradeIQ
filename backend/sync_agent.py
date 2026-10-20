#!/usr/bin/env python3
"""
TradeIQ 同步 agent — 独立 launchd job, GUI session 借据由 launchd 注入.

跑一个 HTTP server 在 127.0.0.1:5556, Flask 用 HTTP 触发同步, agent 自己跑
Playwright / mmx, 写 state 文件. 完全脱离 Flask 父子进程链, 6h 借据问题不再复发.

Routes:
  GET  /health
  POST /sync/em       body {force, wait_for_user, state_file}
  POST /sync/ocr      body {date, job_id, trade_date}
  POST /sync/ocr-image body {filepath, job_id, trade_date}

启动时自检:
  1. sync_jobs 关键函数 import 成功
  2. 扫历史 state 文件, 卡死的 running/processing 标 done/error
"""
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 让 agent 能 import backend 目录下的模块
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BACKEND_DIR)
os.chdir(BACKEND_DIR)

# 强制 import sync_jobs 一次, 防漏抽依赖
try:
    import sync_jobs  # noqa: E402
    # 关键函数 assert
    for _fn in ('run_em_sync', 'run_sector_sync', 'run_ths_sync', 'run_fetch_and_ocr',
                'fetch_jiuye_diagram', 'run_ocr_job', '_save_em_fund_flow_rows', 'DB_PATH'):
        if not hasattr(sync_jobs, _fn):
            raise RuntimeError(f'sync_jobs 缺关键符号: {_fn}')
except Exception as _e:
    sys.stderr.write(f'[agent] 启动失败: sync_jobs 导入错误: {_e}\n{traceback.format_exc()}\n')
    raise

PORT = int(os.environ.get('TRADEIQ_AGENT_PORT', '5556'))
STARTED_AT = datetime.now().isoformat()
AGENT_PID = os.getpid()


def _write_log(msg):
    print(f'[agent][{datetime.now().strftime("%H:%M:%S")}] {msg}', flush=True)


def _threaded(fn):
    def runner(*a, **kw):
        t = threading.Thread(target=fn, args=a, kwargs=kw, daemon=True)
        t.start()
        return t
    return runner


@_threaded
def _run_em_sync(force, wait_for_user, state_file):
    try:
        sync_jobs.run_em_sync(force=force, wait_for_user=wait_for_user,
                              state_file=state_file)
    except Exception as e:
        _write_log(f'EM sync 异常: {type(e).__name__}: {e}')
        traceback.print_exc()


@_threaded
def _run_sector_sync(sector_type, force, state_file, wait_for_user=False):
    """板块资金流 (行业/概念) 同步 — 浏览器必须在 GUI session 里起, 所以跑在 agent 进程."""
    try:
        sync_jobs.run_sector_sync(sector_type=sector_type, force=force,
                                  state_file=state_file,
                                  wait_for_user=bool(wait_for_user))
    except Exception as e:
        _write_log(f'Sector sync 异常: {type(e).__name__}: {e}')
        traceback.print_exc()


@_threaded
def _run_ths_sync(force, state_file):
    """同花顺个股资金流同步 — 同理, 真 Chrome 必须在 GUI session 里起."""
    try:
        sync_jobs.run_ths_sync(force=force, state_file=state_file)
    except Exception as e:
        _write_log(f'THS sync 异常: {type(e).__name__}: {e}')
        traceback.print_exc()


@_threaded
def _run_ocr(date_str, job_id, trade_date):
    try:
        sync_jobs.run_fetch_and_ocr(date_str, job_id, trade_date)
    except Exception as e:
        _write_log(f'OCR 异常: {type(e).__name__}: {e}')
        traceback.print_exc()


@_threaded
def _run_ocr_image(filepath, job_id, trade_date):
    try:
        sync_jobs.run_ocr_job(filepath, job_id, trade_date)
    except Exception as e:
        _write_log(f'OCR-image 异常: {type(e).__name__}: {e}')
        traceback.print_exc()


def _startup_selfcheck():
    """扫历史 state 文件, 卡死的 running/processing 标 done/error.
    防止上次 agent 死掉时 state 永远卡 running=True 致前端转圈.
    """
    # 1. EM state
    em_state = '/tmp/tradeiq-flow-sync-state.json'
    if os.path.exists(em_state):
        try:
            with open(em_state, 'r', encoding='utf-8') as f:
                st = json.load(f)
            if st.get('running'):
                age_min = (time.time() - os.path.getmtime(em_state)) / 60
                if age_min > 30:
                    st['running'] = False
                    st['finished_at'] = datetime.now().isoformat()
                    st['current_code'] = f'⚠ 上次 agent 异常退出 (>30min 前卡死), 已重置'
                    st['errors'] = list(st.get('errors', [])) + [('agent-reset', 'selfcheck reset')]
                    with open(em_state, 'w', encoding='utf-8') as f:
                        json.dump(st, f, ensure_ascii=False, default=str)
                    _write_log(f'自检: 重置 EM state (卡死 {age_min:.0f}min)')
        except Exception as e:
            _write_log(f'自检: 读 EM state 失败: {e}')

    # 2. OCR state
    uploads_dir = sync_jobs.UPLOADS_DIR
    if os.path.isdir(uploads_dir):
        for fname in os.listdir(uploads_dir):
            if not fname.startswith('_ocr_') or not fname.endswith('.json'):
                continue
            path = os.path.join(uploads_dir, fname)
            try:
                age_min = (time.time() - os.path.getmtime(path)) / 60
                if age_min < 30:
                    continue
                with open(path, 'r', encoding='utf-8') as f:
                    st = json.load(f)
                if st.get('status') == 'processing':
                    st['status'] = 'error'
                    st['stage'] = 'stale_reset'
                    st['error'] = f'⚠ 上次 agent 异常退出, OCR 卡死 >30min, 已重置'
                    with open(path, 'w', encoding='utf-8') as f:
                        json.dump(st, f, ensure_ascii=False)
                    _write_log(f'自检: 重置 {fname} (卡死 {age_min:.0f}min)')
            except Exception as e:
                _write_log(f'自检: 读 {fname} 失败: {e}')


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f'[agent] {self.address_string()} - {fmt % args}\n')

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            return self._json(200, {
                'ok': True, 'pid': AGENT_PID, 'started_at': STARTED_AT,
                'port': PORT, 'uptime_sec': round(time.time() - _BOOT_TS, 1),
            })
        return self._json(404, {'error': 'not found'})

    def do_POST(self):
        ln = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(ln) if ln else b''
        try:
            data = json.loads(body) if body else {}
        except Exception:
            return self._json(400, {'error': 'bad json'})

        if self.path == '/sync/em':
            sf = data.get('state_file') or '/tmp/tradeiq-flow-sync-state.json'
            _run_em_sync(bool(data.get('force')),
                         bool(data.get('wait_for_user')),
                         sf)
            return self._json(202, {'job': 'em', 'state_file': sf,
                                     'pid': AGENT_PID})

        if self.path == '/sync/ths':
            sf = data.get('state_file') or '/tmp/tradeiq-ths-sync-state.json'
            _run_ths_sync(bool(data.get('force')), sf)
            return self._json(202, {'job': 'ths', 'state_file': sf,
                                     'pid': AGENT_PID})

        if self.path == '/sync/sector':
            st = (data.get('sector_type') or 'industry').strip()
            if st not in ('industry', 'concept'):
                return self._json(400, {'error': f'invalid sector_type: {st}'})
            sf = data.get('state_file') or f'/tmp/tradeiq-sector-sync-{st}.json'
            _run_sector_sync(st, bool(data.get('force')), sf,
                             bool(data.get('wait_for_user')))
            return self._json(202, {'job': 'sector', 'sector_type': st,
                                     'state_file': sf, 'pid': AGENT_PID})

        if self.path == '/sync/ocr':
            for k in ('date', 'job_id', 'trade_date'):
                if not data.get(k):
                    return self._json(400, {'error': f'missing {k}'})
            # 让 Flask 端先写完初始 status 落盘 (50ms 内)
            time.sleep(0.05)
            _run_ocr(data['date'], data['job_id'], data['trade_date'])
            return self._json(202, {'job': 'ocr', 'job_id': data['job_id'],
                                     'pid': AGENT_PID})

        if self.path == '/sync/ocr-image':
            for k in ('filepath', 'job_id', 'trade_date'):
                if not data.get(k):
                    return self._json(400, {'error': f'missing {k}'})
            if not os.path.exists(data['filepath']):
                return self._json(400, {'error': f'file not found: {data["filepath"]}'})
            time.sleep(0.05)
            _run_ocr_image(data['filepath'], data['job_id'], data['trade_date'])
            return self._json(202, {'job': 'ocr-image', 'job_id': data['job_id'],
                                     'pid': AGENT_PID})

        return self._json(404, {'error': 'not found'})


_BOOT_TS = time.time()

if __name__ == '__main__':
    _startup_selfcheck()
    _write_log(f'starting on 127.0.0.1:{PORT} pid={AGENT_PID} backend={BACKEND_DIR}')
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except OSError as e:
        _write_log(f'bind 失败 (port {PORT} 可能被占): {e}')
        sys.exit(1)
    srv.serve_forever()
