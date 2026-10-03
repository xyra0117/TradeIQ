"""条件优选批量截图 + 合并 PDF.

用法:
  python3 backend/build_preferred_pdf.py [START_DATE] [END_DATE] [VOLUME_RATIO_PCT]

默认 20260616 ~ 20260914, volume_ratio_pct=90.
"""
import io
import os
import sys
import time
import json
import urllib.request
import subprocess
from urllib.parse import urlencode

API = 'http://127.0.0.1:5555'
PREVIEW_DIR = os.path.join(os.path.dirname(__file__), 'previews')
OUT_DIR = os.path.join(os.path.dirname(__file__), '..', '文件')


def fetch_trading_dates(start, end):
    import sqlite3
    db_path = os.path.join(os.path.dirname(__file__), 'market_data.db')
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        'SELECT cal_date FROM trading_dates_cache WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date',
        (start, end)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def render_one(date, threshold_yi, volume_ratio_pct):
    """用 curl 而非 urllib/requests 触发端点; werkzeug + urllib 序列有兼容问题 (curl 测试稳定).
    返回 (path, err); err 以 'NO_HIT' 开头表示该日没有命中任何股票 (要求 PDF 不收录此页)."""
    body = json.dumps({'date': date, 'threshold_yi': threshold_yi,
                       'volume_ratio_pct': volume_ratio_pct})
    last_err = 'unknown'
    for attempt in range(3):
        try:
            result = subprocess.run(
                ['curl', '-s', '--noproxy', '*', '--max-time', '180',
                 '-X', 'POST', '-H', 'Content-Type: application/json',
                 '-H', 'Connection: close',
                 '--data-raw', body,
                 f'{API}/api/conditional-picks/preferred/preview-image'],
                capture_output=True, text=True, timeout=200)
            out = result.stdout.strip()
            try:
                data = json.loads(out)
            except Exception:
                last_err = f'curl 输出非 JSON (attempt {attempt+1}): {out[:200]} stderr={result.stderr[:120]}'
                time.sleep(2 + attempt * 3)
                continue
            if data.get('status') == 'success':
                if not data.get('count'):
                    return None, f'NO_HIT 该日没有满足全部优选条件的股票 (count=0)'
                return data['path'], None
            return None, data.get('note') or f'status={data.get("status")}'
        except subprocess.TimeoutExpired:
            last_err = f'curl 超时 attempt {attempt+1}'
            time.sleep(2 + attempt * 3)
    return None, last_err


def merge_pdf(png_paths, out_path):
    from PIL import Image
    images = []
    for p in png_paths:
        img = Image.open(p).convert('RGB')
        images.append(img)
    if not images:
        raise RuntimeError('没有可合并的 PNG')
    images[0].save(out_path, save_all=True, append_images=images[1:], format='PDF')
    return out_path


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else '20260616'
    end = sys.argv[2] if len(sys.argv) > 2 else '20260914'
    volume_ratio_pct = float(sys.argv[3]) if len(sys.argv) > 3 else 90
    threshold_yi = 3

    dates = fetch_trading_dates(start, end)
    print(f'交易日 {start}~{end}: {len(dates)} 个')
    os.makedirs(PREVIEW_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)

    successes, skipped, failed, no_hit = [], [], [], []
    for d in dates:
        path, err = render_one(d, threshold_yi, volume_ratio_pct)
        if err:
            if err.startswith('NO_HIT'):
                no_hit.append((d, err))
                print(f'  {d} NO_HIT (跳过, 该日没有满足全部优选条件的股票)')
            elif 'daily_ready' in err or 'not ready' in err.lower() or '当前不满足生成条件' in err:
                skipped.append((d, err))
                print(f'  {d} SKIP: {err}')
            else:
                failed.append((d, err))
                print(f'  {d} FAIL: {err}')
            continue
        successes.append((d, path))
        print(f'  {d} OK ({path and os.path.basename(path)}) -> 命中')
        time.sleep(0.4)  # 别把 playwright 压垮

    out_name = f'conditional_preferred_{start}_{end}_vr{int(volume_ratio_pct)}.pdf'
    out_path = os.path.join(OUT_DIR, out_name)
    pngs = [p for _, p in successes]
    merge_pdf(pngs, out_path)
    print(f'PDF: {out_path}  ({len(successes)} 页, no_hit={len(no_hit)}, '
          f'skip={len(skipped)}, fail={len(failed)})')


if __name__ == '__main__':
    main()