"""
run_ocr_job + 内层 helper — 抽到独立文件避免 sync_jobs.py 太大。
所有 DB / 状态文件路径 / mmx 路径都从 sync_jobs 拿, 不重复定义.
"""
import os
import re
import time
import json
import json as _json
import sqlite3
import subprocess
import threading as _th
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image as _Image, Image as _PILImage

import sync_jobs
from sync_jobs import (
    DB_PATH, MMX_BIN, _ocr_status_path, _save_result,
    _schedule_ocr_image_cleanup, UPLOADS_DIR,
)

__all__ = ['run_ocr_job']


def _parse_mmx_content(stdout_text):
    """从 mmx stdout 解析出 [{sector, stocks:[...]}, ...] 数组，失败返回 ([], 错误描述)"""
    try:
        outer = _json.loads(stdout_text.strip())
        inner = outer.get('content', '')
        inner = re.sub(r'^```json\s*', '', inner).strip()
        inner = re.sub(r'```\s*$', '', inner).strip()
        if inner.startswith('```'):
            inner = re.sub(r'^```[a-z]*\s*', '', inner).strip()
            inner = re.sub(r'```\s*$', '', inner).strip()
        if inner.startswith('['):
            return _json.loads(inner), ''
        if inner.startswith('"'):
            decoded = _json.loads(inner)
            if isinstance(decoded, str) and decoded.startswith('['):
                return _json.loads(decoded), ''
        m = re.search(r'\[\s*\{.*\}\s*\]', stdout_text.strip(), re.DOTALL)
        if m:
            return _json.loads(m.group()), ''
    except Exception as _pe:
        return [], f'{type(_pe).__name__}: {_pe}'
    return [], 'no array found'

# ============================================================
# 单图调 mmx 多次重试
# ============================================================

def _try_ocr_image(sp, prompt, max_attempts=2, timeout=240, attempt_label=''):
    """单图调 mmx 最多 max_attempts 次（瞬时错误重试）。返回 (data, err, last_stdout, last_stderr, attempts_used)."""
    data, err = [], ''
    last_out, last_err = '', ''
    attempts_used = 0
    for attempt in range(max_attempts):
        try:
            r = subprocess.run(
                [MMX_BIN, 'vision', 'describe', '--image', sp,
                 '--output', 'json', '--prompt', prompt],
                capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            err = f'timeout {timeout}s ({attempt_label}attempt {attempt})'
            time.sleep(2)
            continue
        except Exception as _e:
            err = f'{type(_e).__name__}: {_e} ({attempt_label}attempt {attempt})'
            time.sleep(2)
            continue
        try:
            debug_path = sp + f'.{attempt_label}attempt{attempt}.txt'
            with open(debug_path, 'w', encoding='utf-8') as _df:
                _df.write(f'rc={r.returncode}\nstderr:\n{r.stderr}\nstdout:\n{r.stdout}\n')
        except Exception:
            pass
        last_out, last_err = r.stdout, r.stderr
        if r.returncode == 0 and r.stdout.strip():
            data, err = _parse_mmx_content(r.stdout)
            attempts_used = attempt + 1
            if data:
                return data, '', last_out, last_err, attempts_used
        err = (r.stderr or 'empty stdout')[:200]
        attempts_used = attempt + 1
        time.sleep(2)
    return [], err, last_out, last_err, attempts_used

# ============================================================
# 切图策略
# ============================================================

def _slice_image_into(filepath):
    """按高度切成长图切片, 返回 (slice_paths, meta_dict). 图小时只切 1 块.

    切块规则 (user 2026-06-22 拍板):
      H ≤ 6000           → 1 块 (整张直送)
      6000 <  H <  7000  → 2 块
      7000 ≤ H <  9000  → 3 块
      9000 ≤ H < 12000  → 4 块
      12000 ≤ H < 16000 → 5 块
      H ≥ 16000         → 5 块 cap (防止长图越切越多)
    """
    img = _Image.open(filepath)
    W, H = img.size
    OVERLAP = 400
    if H <= 6000:
        N = 1
    elif H < 7000:
        N = 2
    elif H < 9000:
        N = 3
    elif H < 12000:
        N = 4
    else:
        N = 5
    if N == 1:
        slice_h = H
    else:
        slice_h = (H + (N - 1) * OVERLAP) // N
    slice_paths = []
    for i in range(N):
        y0 = max(0, i * (slice_h - OVERLAP)) if N > 1 else 0
        y1 = min(H, y0 + slice_h)
        if i == N - 1:
            y1 = H
        if N == 1:
            sp = filepath.rsplit('.', 1)[0] + '_ocr.jpg'
            img.convert('RGB').save(sp, 'JPEG', quality=80)
        else:
            sp = filepath.rsplit('.', 1)[0] + f'_slice{i}.jpg'
            img.crop((0, y0, W, y1)).convert('RGB').save(sp, 'JPEG', quality=75)
        slice_paths.append(sp)
    return slice_paths, {'slice_count': N, 'slice_height': slice_h, 'image_size': f'{W}x{H}'}

# ============================================================
# 跨切片合并
# ============================================================

def _merge_parsed_results(results):
    """results: [(idx, [{sector, stocks:[...]}, ...]), ...] 按 idx 升序.

    做两件事:
      1. 跨切片 sector="" 传播: 本切片首项 sector="" 用上一切片最后具体板块填充
      2. 按 code 去重, merge 字段 (同 code 取信息更全的版本)
    """
    all_parsed = []
    last_out, last_err = '', ''
    errors = []
    for idx, data in results:
        for sec_idx, item in enumerate(data):
            if not isinstance(item, dict):
                continue
            raw_sector = (item.get('sector') or item.get('sector_name') or '').strip()
            raw_sector = re.sub(r'\*\d+$', '', raw_sector)
            if not raw_sector and sec_idx == 0 and idx > 0:
                prev_data = None
                for prev_idx, prev_d in results:
                    if prev_idx == idx - 1:
                        prev_data = prev_d
                        break
                if prev_data:
                    for prev_item in reversed(prev_data):
                        if not isinstance(prev_item, dict):
                            continue
                        prev_sec = (prev_item.get('sector') or prev_item.get('sector_name') or '').strip()
                        prev_sec = re.sub(r'\*\d+$', '', prev_sec)
                        if prev_sec and prev_sec != '其他':
                            raw_sector = prev_sec
                            break
            sector_final = raw_sector if raw_sector else '其他'
            for stock in item.get('stocks', []) or []:
                if not isinstance(stock, dict):
                    continue
                s = dict(stock)
                s['sector'] = sector_final
                s['_slice'] = idx
                all_parsed.append(s)

    def _better(a, b):
        a = (a or '').strip(); b = (b or '').strip()
        if not a: return b
        if not b: return a
        return a if len(a) >= len(b) else b

    merged = {}
    for s in all_parsed:
        code = (s.get('code') or '').strip()
        if not code:
            continue
        if code not in merged:
            merged[code] = s
            continue
        cur = merged[code]
        for k in ('name', 'days', 'time', 'market_cap', 'turnover', 'keywords'):
            cur[k] = _better(cur.get(k), s.get(k))
        if (cur.get('sector') in ('其他', '', None)) and s.get('sector') not in ('其他', '', None):
            cur['sector'] = s.get('sector')

    return list(merged.values()), errors, last_out, last_err

# ============================================================
# 短字段 (4 fields) 兼容 helper
# ============================================================

def _flatten_short_data(data):
    """把 4 字段 prompt 输出 [{sector, stocks:[{code,name,days}]}] 扁平化成 [{sector, code, name, days}, ...]"""
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        sec = (item.get('sector') or item.get('sector_name') or '').strip()
        sec = re.sub(r'\*\d+$', '', sec)
        sec_final = sec if sec else '其他'
        for stock in item.get('stocks', []) or []:
            if not isinstance(stock, dict):
                continue
            s = dict(stock)
            s['sector'] = sec_final
            out.append(s)
    return out

def _apply_fields3(stocks, fields3_data):
    """按 code 把 3 字段 (time/market_cap/turnover/keywords) 填进 stocks.
    fields3_data 格式: [{code, time, market_cap, turnover, keywords}, ...]
    """
    if not fields3_data:
        return stocks
    by_code = {}
    for item in fields3_data:
        if not isinstance(item, dict):
            continue
        code = (item.get('code') or '').strip()
        if code:
            by_code[code] = item
    for s in stocks:
        code = (s.get('code') or '').strip()
        if not code or code not in by_code:
            continue
        f3 = by_code[code]
        for k in ('time', 'market_cap', 'turnover', 'keywords'):
            v = (f3.get(k) or '').strip()
            if v:
                s[k] = v
    return stocks

# ============================================================
# run_ocr_job 主函数
# ============================================================

# 7 字段 prompt
_PROMPT_7FIELDS = (
    '识别图中所有涨停股票。严格按 JSON 数组输出，不加任何解释、不带 markdown 代码块标记：\n'
    '[{"sector":"板块名","stocks":[{"code":"代码","name":"名称","days":"连板数","time":"封板时间","market_cap":"流通市值","turnover":"成交额","keywords":"关键词"}]}]\n'
    '要求：保留所有板块和股票不遗漏；板块名去掉 *N 后缀；缺失字段填空串；'
    '若某组股票顶部看不到板块标题（可能被裁剪），sector 留空字符串 ""，不要猜测；'
    '输出从 [ 开始 ] 结束。'
)

def run_ocr_job(filepath, job_id, trade_date):
    """模块级 OCR 任务：跑 mmx → 解析 → 写库 → 保留原图。供 /parse-image 和 /fetch 复用。
    每个 job 单独写到 uploads/_ocr_<job_id>.json, 父进程 poll 读它.
    """
    job_status_path = _ocr_status_path(job_id)

    def _save(stage, **extra):
        try:
            payload = {'status': 'processing', 'stage': stage}
            payload.update(extra)
            with open(job_status_path, 'w', encoding='utf-8') as _f:
                _json.dump(payload, _f, ensure_ascii=False)
        except Exception:
            pass

    try:
        _save('ocr_starting')

        # ---- 阶段 A: 原图直接 7 字段 mmx, 5min × 2 次重试 ----
        _save('ocr_full', phase='A', max_attempts=2, timeout=300)
        full_data, full_err, full_out, full_errstr, full_attempts = _try_ocr_image(
            filepath, _PROMPT_7FIELDS, max_attempts=2, timeout=300, attempt_label='full.')
        results_for_merge = []
        slice_paths = []
        date_image = filepath
        parsed_data = None
        if full_data:
            _save('ocr_full_ok')
            results_for_merge = [(-1, full_data)]
        else:
            # ---- 阶段 B: 切图 + 并发 + 合并 ----
            _save('ocr_full_failed', err=full_err, fallback='slice')
            slice_paths, slice_meta = _slice_image_into(filepath)
            _save('sliced', **slice_meta)
            N = len(slice_paths)

            results_by_idx = {}
            progress = {'done': 0}
            progress_lock = _th.Lock()

            def _ocr_slice(idx, sp):
                with progress_lock:
                    _save('ocr_slicing', done=progress['done'], total=N, current=idx)
                d, e, o, estr, slice_attempts = _try_ocr_image(sp, _PROMPT_7FIELDS, max_attempts=2,
                                                attempt_label=f'slice{idx}.')
                with progress_lock:
                    progress['done'] += 1
                    _save('ocr_slicing', done=progress['done'], total=N, current=idx)
                return idx, d, e, o, estr

            MAX_WORKERS = min(3, N)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futs = [ex.submit(_ocr_slice, i, sp) for i, sp in enumerate(slice_paths)]
                for f in as_completed(futs):
                    try:
                        idx, data, err, out, errstr = f.result()
                    except Exception:
                        continue
                    results_by_idx[idx] = (data, err)
                    if out: full_out = out
                    if errstr: full_errstr = errstr

            for idx in range(N):
                data, err = results_by_idx.get(idx, ([], 'missing'))
                if data:
                    results_for_merge.append((idx, data))
                else:
                    full_err = (full_err or '') + f' slice{idx}:{err}'
            date_image = slice_paths[0]

        if parsed_data is None and not results_for_merge:
            err_msg = '原图和切图都识别失败: ' + (full_err or 'unknown')
            _save_result({'status': 'error', 'stage': 'error', 'error': err_msg,
                          'mmx_stderr': (full_errstr or '')[:500],
                          'raw_stdout': (full_out or '')[:500],
                          'job_id': job_id})
            return

        if parsed_data is None:
            _save('merging', sources=[idx for idx, _ in results_for_merge],
                  total_raw=sum(len(d) for _, d in results_for_merge))
            parsed_data, _merge_errs, _, _ = _merge_parsed_results(results_for_merge)
        else:
            _save('merging_short_path', count=len(parsed_data))

        # ---- 日期识别: 用 date_image (原图成功用原图, 否则用第一块切片) ----
        date_prompt = (
            'What date is shown on this image? '
            'Output ONLY the date text visible in the title or header area, '
            'e.g. "05.15" or "5月15日". '
            'Do not add any explanation or additional text.'
        )
        date_r = None
        for _d_attempt in range(2):
            try:
                date_r = subprocess.run(
                    [MMX_BIN, 'vision', 'describe', '--image', date_image,
                     '--output', 'json', '--prompt', date_prompt],
                    capture_output=True, text=True, timeout=120
                )
                if date_r.returncode == 0 and date_r.stdout.strip():
                    break
            except subprocess.TimeoutExpired:
                time.sleep(2)
                continue
        parsed_date = trade_date
        if date_r is not None and date_r.returncode == 0:
            try:
                date_outer = _json.loads(date_r.stdout.strip())
                date_str = date_outer.get('content', '').strip()
                if date_str:
                    m = re.match(r'(\d{1,2})\.(\d{1,2})', date_str)
                    if m:
                        month, day = int(m.group(1)), int(m.group(2))
                        year = datetime.now().year
                        parsed_date = f'{year}{month:02d}{day:02d}'
                    else:
                        m2 = re.match(r'(\d{1,2})月(\d{1,2})日', date_str)
                        if m2:
                            month, day = int(m2.group(1)), int(m2.group(2))
                            year = datetime.now().year
                            parsed_date = f'{year}{month:02d}{day:02d}'
            except Exception:
                pass
        if not parsed_date:
            parsed_date = datetime.now().strftime('%Y%m%d')

        parse_error = full_err or ''
        raw_stdout = (full_out or '')[:800]
        mmx_stderr = (full_errstr or '')[:500] if (full_err or full_errstr) else ''

        _save('parsing', parse_error=parse_error, raw_stdout=raw_stdout,
              mmx_stderr=mmx_stderr, merged_count=len(parsed_data))
        boards_map = {}
        streak_stocks = []

        if parsed_data and any('stocks' in item for item in parsed_data):
            flat = []
            for item in parsed_data:
                if isinstance(item, dict) and 'stocks' in item:
                    sector_base = (item.get('sector') or item.get('sector_name') or '其他').strip()
                    sector_base = re.sub(r'\*\d+$', '', sector_base)
                    for stock in item.get('stocks', []):
                        stock_copy = dict(stock)
                        stock_copy['sector'] = sector_base
                        flat.append(stock_copy)
                elif isinstance(item, dict):
                    flat.append(item)
            parsed_data = flat

        for s in parsed_data:
            sector = (s.get('sector') or '其他').strip()
            sector = re.sub(r'\*\d+$', '', sector)
            if sector not in boards_map:
                boards_map[sector] = []
            boards_map[sector].append(s)
            days = s.get('days', '1') or '1'
            if days not in ('1', '首板', '1天'):
                streak_stocks.append(s)

        if not parsed_date:
            parsed_date = datetime.now().strftime('%Y%m%d')

        def to_float(v):
            try:
                if not v: return 0.0
                s = str(v).replace(',', '').replace('亿', '').replace('万', '')
                return float(s)
            except Exception:
                return 0.0

        def norm(v):
            if not v or v in ('1', '首板', '1天'):
                return '首板'
            return str(v)

        _save('writing_db')
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute('DELETE FROM limitup WHERE date=?', (parsed_date,))
        cnt = 0
        for s in streak_stocks:
            try:
                c.execute('''INSERT INTO limitup (date,code,name,marketCap,time,sector,volume,streak,keyword)
                    VALUES (?,?,?,?,?,?,?,?,?)''',
                    (parsed_date, s.get('code',''), s.get('name',''),
                     to_float(s.get('market_cap')),
                     s.get('time',''), s.get('sector',''),
                     to_float(s.get('turnover')), norm(s.get('days','')), s.get('keywords', '')))
                cnt += 1
            except Exception:
                pass
        for board_sector, stocks in boards_map.items():
            for s in stocks:
                try:
                    c.execute('''INSERT INTO limitup (date,code,name,marketCap,time,sector,volume,streak,keyword)
                        VALUES (?,?,?,?,?,?,?,?,?)''',
                        (parsed_date, s.get('code',''), s.get('name',''),
                         to_float(s.get('market_cap')),
                         s.get('time',''), board_sector,
                         to_float(s.get('turnover')), norm(s.get('days', '1')), s.get('keywords', '')))
                    cnt += 1
                except Exception:
                    pass
        conn.commit()
        conn.close()

        # 刷新涉及的 ts_code 的 sector summary
        try:
            _sum_conn = sqlite3.connect(DB_PATH)
            _sum_codes = [r[0] for r in _sum_conn.execute(
                'SELECT DISTINCT code FROM limitup WHERE date = ? AND code IS NOT NULL',
                (parsed_date,)
            ).fetchall()]
            _sum_conn.close()
            if _sum_codes:
                sync_jobs.refresh_sector_summary_for_codes(_sum_codes)
        except Exception as _e:
            print(f'[run_ocr_job] sector summary 刷新失败 (非致命): {_e}')

        _w, _h = _PILImage.open(filepath).size
        result = {
            'status': 'done', 'stage': 'done', 'count': cnt, 'date': parsed_date,
            'boards': list(boards_map.keys()), 'streak_count': len(streak_stocks),
            'image_path': filepath, 'job_id': job_id,
            'image_size': f'{_w}x{_h}',
            'slices': slice_paths,
            'attempts': full_attempts if parsed_data is not None else len(results_by_idx) if 'results_by_idx' in dir() else full_attempts,
            'mode': 'full' if parsed_data is not None else f'sliced_{N}' if 'N' in dir() else 'full',
        }
        if cnt == 0:
            result['parse_error'] = parse_error
            result['raw_stdout'] = raw_stdout
            result['mmx_stderr'] = mmx_stderr
        _save_result(result)
        # OCR 成功, 5 分钟后删临时图片
        _schedule_ocr_image_cleanup(filepath, delay_sec=0)
    except Exception as e:
        _save_result({'status': 'error', 'stage': 'error', 'error': str(e), 'job_id': job_id})
