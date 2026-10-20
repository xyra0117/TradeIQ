"""
TradeIQ 同步业务函数 — 给 sync_agent.py 独立 launchd agent 跑。

设计:
  - standalone, 不 import app (避免启动 Flask 副作用占 5555 端口)
  - DB_PATH / uploads / cookie 路径从 __file__ 算, 跟 app.py 同一目录
  - Flask 路由通过 `from sync_jobs import ...` 复用这些函数 (同一个 DB / 同一份状态文件)
  - OCR 的内层 helper (mmx 解析 / 切图 / 合并) 抽到 _ocr_helpers.py
"""
import os
import re
import json
import json as _json
import subprocess
import time
import sqlite3
import asyncio
import uuid as _uuid
from datetime import datetime
import requests as _requests
import traceback

# ---------- 路径常量 ----------
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BACKEND_DIR, 'market_data.db')
UPLOADS_DIR = os.path.join(BACKEND_DIR, 'uploads')
os.makedirs(UPLOADS_DIR, exist_ok=True)

# mmx OCR 命令 (硬编码在 app.py 多处; 抽到一处)
MMX_BIN = '/usr/local/bin/mmx'

# 韭研公社凭据 / cookie 文件
_JIYE_COOKIES_PATH = os.path.join(BACKEND_DIR, '.jiuye_cookies.json')
_JIYE_LOGIN_PATH = os.path.join(BACKEND_DIR, '.jiuye_login.json')

__all__ = [
    # OCR
    '_ocr_status_path', '_save_result', '_read_ocr_status', '_schedule_ocr_image_cleanup',
    'run_ocr_job', 'run_fetch_and_ocr',
    # 韭研拉图
    'fetch_jiuye_diagram', '_load_jiuye_creds',
    # EM 主力净流入
    'run_em_sync', '_save_em_fund_flow_rows',
    # sector summary
    'refresh_sector_summary_for_codes', 'rebuild_sector_summary',
    # 路径常量
    'DB_PATH', 'UPLOADS_DIR', 'MMX_BIN', 'BACKEND_DIR',
]


# ============================================================
# OCR 状态文件 helper
# ============================================================

def _ocr_status_path(job_id):
    return os.path.join(UPLOADS_DIR, f'_ocr_{job_id}.json')


def _save_result(result):
    try:
        with open(_ocr_status_path(result.get('job_id', 'unknown')), 'w', encoding='utf-8') as _f:
            _json.dump(result, _f, ensure_ascii=False)
    except Exception:
        pass


def _read_ocr_status(job_id):
    try:
        with open(_ocr_status_path(job_id), 'r', encoding='utf-8') as _f:
            return _json.load(_f)
    except Exception:
        return None


def _schedule_ocr_image_cleanup(filepath, delay_sec=0):
    """OCR 成功后立即删临时图片 (delay_sec=0). 前端 OCR modal 只显示识别结果不显示原图, 没并发读风险.
    status=error 时不删 (保留供重试). 启动一个 daemon 线程等 N 秒后 os.remove.
    """
    if not filepath or not os.path.exists(filepath):
        return
    import threading as _th
    def _del():
        import time as _t
        if delay_sec > 0:
            _t.sleep(delay_sec)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                print(f'[ocr-cleanup] 已删 {os.path.basename(filepath)} (job 跑完 +{delay_sec}s)', flush=True)
        except Exception as e:
            print(f'[ocr-cleanup] 删 {filepath} 失败: {e}', flush=True)
    _th.Thread(target=_del, daemon=True).start()


# ============================================================
# 韭研公社 — cookie 加载 / 自动登录 / cookie 回写
# ============================================================

def _load_jiuye_creds():
    """读 .jiuye_login.json，返回 {'phone':..., 'password':...} 或 None（文件不存在/无效）。
    凭据文件不应进 git（已在 .gitignore），权限建议 chmod 600。
    """
    if not os.path.exists(_JIYE_LOGIN_PATH):
        return None
    try:
        with open(_JIYE_LOGIN_PATH, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and d.get('phone') and d.get('password'):
            return {'phone': str(d['phone']).strip(), 'password': str(d['password'])}
    except Exception:
        pass
    return None


async def _jiuye_auto_login(context, page, creds):
    """用账号密码自动登录韭研。成功返回 True，失败返回 False。
    2026-08-20 改版: 独立的 /login 页面被撤掉 (302 跳首页), 登录改成首页点「登录注册」
    弹 modal, 弹窗里再切「账号密码登录」tab。旧流程找不到 tab 全部失败。
    调用前 page 应已在任一 jiuyangongshe.com 页面 (首页/action 页都有顶部「登录注册」按钮)。
    每个失败点都 print 原因, 排查不靠猜.
    """
    try:
        if 'jiuyangongshe.com' not in page.url:
            await page.goto('https://www.jiuyangongshe.com/',
                            wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(1500)
        login_entries = [c for c in await page.locator('text=登录注册').all() if await c.is_visible()]
        if not login_entries:
            cookies = await context.cookies('https://www.jiuyangongshe.com')
            if any(c.get('name') == 'SESSION' and c.get('value') for c in cookies):
                print('[jiuye-login] 已是登录态 (无登录注册按钮且 SESSION 在), 跳过', flush=True)
                return True
            print('[jiuye-login] 找不到「登录注册」按钮且 SESSION 不在', flush=True)
            return False
        _dialog_up = False
        for _try in range(3):
            await login_entries[0].click(timeout=5000)
            try:
                await page.wait_for_selector('.el-tabs__item:has-text("账号密码登录")',
                                             state='visible', timeout=6000)
                _dialog_up = True
                break
            except Exception:
                pass
        if not _dialog_up:
            print('[jiuye-login] 登录弹窗没弹出或弹窗里没有「账号密码登录」tab', flush=True)
            return False
        await page.locator('.el-tabs__item:has-text("账号密码登录")').first.click(timeout=5000)
        await page.wait_for_timeout(600)
        try:
            phone_inputs = await page.locator('input[placeholder="请输入手机号"]').all()
            if not phone_inputs:
                print('[jiuye-login] 找不到手机号输入框', flush=True)
                return False
            await phone_inputs[-1].fill(creds['phone'])
            await page.locator('input[type="password"]').first.fill(creds['password'])
        except Exception as e:
            print(f'[jiuye-login] 填账号密码失败: {e}', flush=True)
            return False
        try:
            login_btns = [b for b in await page.locator('button:has-text("登录")').all()
                          if await b.is_visible()]
            if not login_btns:
                print('[jiuye-login] 找不到登录按钮', flush=True)
                return False
            await login_btns[-1].click(timeout=5000)
        except Exception as e:
            print(f'[jiuye-login] 点登录按钮失败: {e}', flush=True)
            return False
        for _ in range(30):
            cookies = await context.cookies('https://www.jiuyangongshe.com')
            if any(c.get('name') == 'SESSION' and c.get('value') for c in cookies):
                await page.wait_for_timeout(500)
                print('[jiuye-login] 登录成功, SESSION 已刷新', flush=True)
                return True
            await page.wait_for_timeout(500)
        print('[jiuye-login] 登录后 15s 内未拿到 SESSION cookie (账号密码可能不对)', flush=True)
        return False
    except Exception as e:
        print(f'[jiuye-login] 登录流程异常: {e}', flush=True)
        return False


def _persist_jiuye_cookies(cookies_path, fresh_cookies):
    """把 Playwright context.cookies() 返回的最新 cookies 合并写回文件。
    以 (name, domain, path) 为唯一键覆盖；原文件中独有的 cookie 保留。
    原子写：tmp + rename，避免半写文件让下次启动直接挂掉。
    """
    try:
        with open(cookies_path, 'r', encoding='utf-8') as _f:
            old = json.load(_f)
        if not isinstance(old, list):
            old = []
    except Exception:
        old = []

    def _key(c):
        return (c.get('name', ''), c.get('domain', ''), c.get('path', '/'))

    merged = {_key(c): c for c in old}
    for c in fresh_cookies:
        if not isinstance(c, dict) or not c.get('name'):
            continue
        if c.get('expires') in (None, -1):
            c = dict(c); c['expires'] = 0
        merged[_key(c)] = c

    tmp_path = cookies_path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as _f:
        json.dump(list(merged.values()), _f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, cookies_path)


# ============================================================
# 韭研公社 — 拉涨停简图
# ============================================================

def fetch_jiuye_diagram(date_str, job_id=None):
    """从韭研公社拉取某日涨停简图，存到 uploads/，返回 (filepath, job_id, ymd) 或 raise。date_str 格式 YYYY-MM-DD。
    job_id 可选传入；不传则内部生成。传入是为了让 /api/limitup/fetch 异步流程能预先把 job_id 返回给前端轮询。
    """
    cookies_path = _JIYE_COOKIES_PATH
    creds = _load_jiuye_creds()
    if not os.path.exists(cookies_path):
        if not creds:
            raise RuntimeError(f'cookie 文件不存在: {cookies_path}，且 .jiuye_login.json 也没配，无法自动登录')
        cookie_list = []
    else:
        with open(cookies_path, 'r', encoding='utf-8') as f:
            cookie_list = json.load(f)
        if not isinstance(cookie_list, list):
            raise RuntimeError('cookie 文件格式错误：应为数组')

    os.makedirs(UPLOADS_DIR, exist_ok=True)
    job_id = job_id or _uuid.uuid4().hex[:8]
    ymd = date_str.replace('-', '')

    async def _fetch():
        from playwright.async_api import async_playwright
        img_url_holder = {'url': None, 'err': None}
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
                viewport={'width': 1440, 'height': 900},
            )
            await context.add_cookies(cookie_list)
            page = await context.new_page()
            # A. 拉图前先访问首页让阿里 WAF 派发新鲜的 acw_tc / cdn_sec_tc（这俩 24h 过期）
            try:
                await page.goto('https://www.jiuyangongshe.com/', wait_until='domcontentloaded', timeout=30000)
                await page.wait_for_timeout(800)
            except Exception:
                pass
            # D'. 拉图前直接走自动登录刷新 SESSION
            if creds:
                ok = await _jiuye_auto_login(context, page, creds)
                if not ok:
                    raise RuntimeError('韭研自动登录失败, 请检查 .jiuye_login.json 里的账号密码')

            async def on_resp(r):
                if 'diagram-url' in r.url and r.request.method == 'POST':
                    try:
                        body = await r.json()
                    except Exception:
                        body = await r.text()
                    err = body.get('errCode') if isinstance(body, dict) else None
                    if err in ('0', 0):
                        img_url_holder['url'] = body.get('data')
                    else:
                        img_url_holder['err'] = body
            page.on('response', on_resp)
            async def _open_action_page():
                try:
                    await page.goto(f'https://www.jiuyangongshe.com/action/{date_str}',
                                    wait_until='domcontentloaded', timeout=45000)
                except Exception as e:
                    raise RuntimeError(f'打开页面失败: {e}')
                try:
                    await page.wait_for_selector('[role="tab"], .el-tabs__item',
                                                 state='visible', timeout=20000)
                except Exception:
                    pass

            async def _login_dialog_up():
                """页面上只有登录弹窗的 tab (pane-phone/pane-accounts) = SESSION 过期被拦。"""
                try:
                    return await page.evaluate("""() => {
                        const tabs = Array.from(document.querySelectorAll('[role="tab"], .el-tabs__item'));
                        return tabs.some(t => /pane-phone|pane-accounts/.test(t.getAttribute('aria-controls') || ''));
                    }""")
                except Exception:
                    return False

            await _open_action_page()
            if await _login_dialog_up():
                print('[jiuye] 检测到登录弹窗 (SESSION 过期), 重新登录...', flush=True)
                if creds and await _jiuye_auto_login(context, page, creds):
                    await _open_action_page()
                else:
                    raise RuntimeError('韭研登录态已失效且自动登录失败, 请检查 .jiuye_login.json 账号密码')

            tab_clicked = False
            try:
                await page.locator('[role="tab"][aria-controls*="diagram"]').first.click(timeout=5000)
                tab_clicked = True
                print(f'[jiuye] tab 点中 (策略1: aria-controls=diagram)', flush=True)
            except Exception as e:
                print(f'[jiuye] tab 策略1 失败: {type(e).__name__}: {e}', flush=True)
            if not tab_clicked:
                try:
                    for _cand in await page.locator('text=涨停简图').all():
                        if await _cand.is_visible():
                            await _cand.click(timeout=5000)
                            tab_clicked = True
                            print(f'[jiuye] tab 点中 (策略2: text=涨停简图 visible)', flush=True)
                            break
                    if not tab_clicked:
                        print('[jiuye] tab 策略2 失败: 匹配到的元素均不可见 (hidFont 水印)', flush=True)
                except Exception as e:
                    print(f'[jiuye] tab 策略2 失败: {type(e).__name__}: {e}', flush=True)
            if not tab_clicked:
                try:
                    vue_result = await page.evaluate("""() => {
                        const app = document.querySelector('#app');
                        if (!app || !app.__vue__) return 'no vue';
                        const walk = (n) => {
                            if (n.activeName !== undefined) { n.activeName = 'diagram'; return true; }
                            if (n.$children) for (const c of n.$children) if (walk(c)) return true;
                            return false;
                        };
                        return walk(app.__vue__) ? 'toggled' : 'no activeName';
                    }""")
                    print(f'[jiuye] tab 策略3 (Vue store) 返回: {vue_result}', flush=True)
                except Exception as e:
                    print(f'[jiuye] tab 策略3 失败: {type(e).__name__}: {e}', flush=True)
            if not tab_clicked:
                try:
                    tabs_dump = await page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('[role="tab"], .el-tabs__item')).map(t => ({
                            tag: t.tagName,
                            text: (t.textContent || '').trim().slice(0, 30),
                            aria: t.getAttribute('aria-controls') || '',
                            cls: t.className || ''
                        }));
                    }""")
                    print(f'[jiuye] tab 三策略全失败, 页面所有 tab 元素: {tabs_dump}', flush=True)
                except Exception as e:
                    print(f'[jiuye] tab dump 也失败: {type(e).__name__}: {e}', flush=True)

            for _ in range(50):
                if img_url_holder['url'] or img_url_holder['err']:
                    break
                await page.wait_for_timeout(500)
            if not img_url_holder['url'] and not img_url_holder['err']:
                print('[jiuye] 25s 内未捕获响应, 重试点击 tab...', flush=True)
                try:
                    await page.locator('[role="tab"][aria-controls*="diagram"]').first.click(timeout=3000)
                except Exception:
                    try:
                        for _cand in await page.locator('text=涨停简图').all():
                            if await _cand.is_visible():
                                await _cand.click(timeout=3000)
                                break
                    except Exception as e:
                        print(f'[jiuye] tab 重试点击也失败: {type(e).__name__}: {e}', flush=True)
                for _ in range(16):
                    if img_url_holder['url'] or img_url_holder['err']:
                        break
                    await page.wait_for_timeout(500)
            try:
                fresh_cookies = await context.cookies('https://www.jiuyangongshe.com')
                if fresh_cookies:
                    _persist_jiuye_cookies(cookies_path, fresh_cookies)
            except Exception:
                pass
            await browser.close()
            if img_url_holder['err']:
                raise RuntimeError(f'韭研 API 错误: {img_url_holder["err"].get("msg")} (errCode={img_url_holder["err"].get("errCode")})')
            if not img_url_holder['url']:
                raise RuntimeError('未捕获到 diagram-url 响应（该日可能无涨停数据或 cookie 已失效）')
            return img_url_holder['url']

    img_url = None
    _last_err = None
    for _attempt in range(2):
        try:
            img_url = asyncio.run(_fetch())
            break
        except Exception as e:
            _last_err = e
            print(f'[jiuye] 第 {_attempt+1} 次抓取失败: {e}', flush=True)
    if img_url is None:
        raise _last_err
    img_resp = _requests.get(img_url, timeout=60)
    img_resp.raise_for_status()
    ext = '.png'
    base = img_url.split('?')[0].split('/')[-1]
    if '.' in base:
        e = '.' + base.rsplit('.', 1)[-1].lower()
        if 1 < len(e) <= 5:
            ext = e
    filename = f'jiuye_{ymd}_{job_id}{ext}'
    filepath = os.path.join(UPLOADS_DIR, filename)
    with open(filepath, 'wb') as f:
        f.write(img_resp.content)
    return filepath, job_id, ymd


# ============================================================
# 主力净流入 — DB 写入
# ============================================================

def _save_em_fund_flow_rows(rows, today, source='eastmoney-push2'):
    """把 detail.html JSONP 拿到的 rows (raw f-code dict) 写入 fund_flow 表.
    字段映射:
      f12 code, f14 name, f2 close, f3 change_pct,
      f62 main_net_inflow, f184 main_net_pct,
      f66 super_net, f69 super_pct,
      f72 big_net, f75 big_pct,
      f78 mid_net, f81 mid_pct,
      f84 small_net, f87 small_pct
    Returns: 写入条数.
    """
    import math
    def _f(v):
        if v is None or v == '-' or v == '':
            return None
        try:
            x = float(v)
            return 0.0 if math.isnan(x) else x
        except (ValueError, TypeError):
            return None

    def _to_ts_code(code: str) -> str:
        code = str(code).strip()
        if not code or len(code) != 6:
            return f'{code}.SH' if code else ''
        return f'{code}.SH' if code.startswith(('6', '9')) else f'{code}.SZ'

    conn = sqlite3.connect(DB_PATH, timeout=30)
    cur = conn.cursor()
    count = 0
    for r in rows:
        code = str(r.get('f12', '')).strip()
        if not code or len(code) != 6:
            continue
        ts_code = _to_ts_code(code)
        name = r.get('f14', '')
        close = _f(r.get('f2'))
        change_pct = _f(r.get('f3'))
        main_net_inflow = _f(r.get('f62')) or 0.0
        main_net_pct = _f(r.get('f184'))
        super_net = _f(r.get('f66'))
        super_pct = _f(r.get('f69'))
        big_net = _f(r.get('f72'))
        big_pct = _f(r.get('f75'))
        mid_net = _f(r.get('f78'))
        mid_pct = _f(r.get('f81'))
        small_net = _f(r.get('f84'))
        small_pct = _f(r.get('f87'))

        cur.execute('DELETE FROM fund_flow WHERE ts_code=? AND trade_date=?', (ts_code, today))
        cur.execute('''INSERT INTO fund_flow
            (trade_date, ts_code, code, name,
             main_net_inflow, main_net_pct,
             super_net, super_pct, big_net, big_pct,
             mid_net, mid_pct, small_net, small_pct, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (today, ts_code, code, name,
             main_net_inflow, main_net_pct,
             super_net, super_pct, big_net, big_pct,
             mid_net, mid_pct, small_net, small_pct, source))
        count += 1
    conn.commit()
    conn.close()
    return count


# ============================================================
# 主力净流入 — 状态文件 / run 入口
# ============================================================

def _write_em_state(state_file: str, state: dict):
    """写状态文件. macOS 上 os.replace 会因 xattr 偶发 EPERM, 直接 truncate 写更稳."""
    try:
        with open(state_file, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False, default=str)
    except Exception as e:
        print(f'[run_em_sync] ⚠ 写 state 失败: {e}', flush=True)


def run_em_sync(force: bool = False, wait_for_user: bool = False,
                state_file: str = '/tmp/tradeiq-flow-sync-state.json'):
    """主力净流入同步 — 直接同步执行 (不子进程). agent 起 daemon thread 调这个.

    Args:
        force: 跳过幂等守卫, 强制重抓
        wait_for_user: 第 1 页失败时弹窗等用户刷新
        state_file: 状态 JSON 文件路径

    跑完保留 finished_at 供前端展示"上次成功/失败".
    """
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
    _write_em_state(state_file, state)

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
        _write_em_state(state_file, state)

    try:
        from fetch_fund_flow_today import fetch_today_market
        rows, total, failed, actual_date = fetch_today_market(
            progress_callback=_cb,
            headless=False,
            verbose=True,
            skip_if_exists=not force,
            wait_for_user=bool(wait_for_user),
        )
    except Exception as e:
        err_msg = f'{type(e).__name__}: {str(e)[:200]}'
        state['errors'].append(('eastmoney-push2', err_msg))
        state['failed'] = max(state['failed'], 1)
        state['running'] = False
        state['finished_at'] = datetime.now().isoformat()
        state['elapsed_sec'] = round(time.time() - t0, 1)
        state['current_code'] = f'❌ {err_msg}'
        _write_em_state(state_file, state)
        print(f'[run_em_sync] 异常退出: {err_msg}', flush=True)
        traceback.print_exc()
        return

    # 幂等守卫命中 (rows 空 + is_already_synced)
    if not rows and state.get('is_already_synced'):
        state['running'] = False
        state['finished_at'] = datetime.now().isoformat()
        state['elapsed_sec'] = round(time.time() - t0, 1)
        _write_em_state(state_file, state)
        print(f'[run_em_sync] ⏭ 已有数据, 跳过 ({state["elapsed_sec"]}s)', flush=True)
        return

    if not rows:
        if not state['errors']:
            state['errors'].append(('eastmoney-push2', '抓取结果为空'))
        if not state['failed']:
            state['failed'] = 1
    else:
        try:
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
    _write_em_state(state_file, state)
    print(f'[run_em_sync] 完成 success={state["success"]} elapsed={state["elapsed_sec"]}s', flush=True)


# ============================================================
# OCR + 韭研拉图 wrapper (供 agent 直接调)
# ============================================================

def run_fetch_and_ocr(date_str, job_id, trade_date):
    """拉韭研图 + 调 run_ocr_job。任何阶段失败都写到 _ocr_<job_id>.json 让前端轮询能拿到."""
    import json as _json2
    status_path = _ocr_status_path(job_id)
    def _save_err(stage, msg):
        try:
            with open(status_path, 'w', encoding='utf-8') as _f:
                _json2.dump({'status': 'error', 'stage': stage,
                             'error': msg, 'job_id': job_id}, _f, ensure_ascii=False)
        except Exception:
            pass
    try:
        try:
            with open(status_path, 'w', encoding='utf-8') as _f:
                _json2.dump({'status': 'processing', 'stage': 'pulling_image'},
                            _f, ensure_ascii=False)
        except Exception:
            pass
        try:
            filepath, _, _ = fetch_jiuye_diagram(date_str, job_id=job_id)
        except Exception as e:
            _save_err('pulling_image', f'拉取韭研图失败: {e}')
            return
        try:
            with open(status_path, 'w', encoding='utf-8') as _f:
                _json2.dump({'status': 'processing', 'stage': 'fetched'},
                            _f, ensure_ascii=False)
        except Exception:
            pass
        run_ocr_job(filepath, job_id, trade_date)
    except Exception as e:
        _save_err('unexpected', f'子进程未处理异常: {type(e).__name__}: {e}')


# run_ocr_job + 内层 helper 由 _ocr_helpers.py 提供
# 这里用 try/except import 防 _ocr_helpers.py 缺失时报清楚
try:
    from _ocr_helpers import run_ocr_job  # noqa: E402
    __all__.append('run_ocr_job')
except ImportError as _e:
    raise ImportError(
        f'sync_jobs 加载失败: _ocr_helpers.py 缺失或语法错误 ({_e})'
    ) from _e

# ============================================================
# sector summary 增量刷新 (搬自 app.py:2132, 保持原 schema: ts_code/sectors_json/total_limitups/last_updated)
# ============================================================

def refresh_sector_summary_for_codes(ts_codes):
    """增量刷新指定 ts_code 列表的 sector 汇总.
    逻辑: GROUP BY (code, sector) → 标记 is_latest (跨 sector 取 MAX(date) 的 sector) → UPSERT 到 sectors_json.
    对已无 limitup 记录的 ts_code, 删掉 summary 行."""
    import sqlite3 as _sq
    if not ts_codes:
        return 0
    conn = _sq.connect(DB_PATH)
    conn.row_factory = _sq.Row
    placeholders = ','.join('?' * len(ts_codes))
    rows = conn.execute(
        f"""SELECT code AS ts_code, sector, MAX(date) AS latest_date, COUNT(*) AS cnt
            FROM limitup
            WHERE code IN ({placeholders}) AND sector IS NOT NULL AND sector != ''
            GROUP BY code, sector""",
        ts_codes
    ).fetchall()
    grouped = {}
    for r in rows:
        grouped.setdefault(r['ts_code'], []).append({
            'sector': r['sector'],
            'count': r['cnt'],
            'latest_date': r['latest_date'],
        })
    now_iso = datetime.now().isoformat(timespec='seconds')
    for code, sectors in grouped.items():
        max_date = max(s['latest_date'] for s in sectors)
        for s in sectors:
            s['is_latest'] = (s['latest_date'] == max_date)
        conn.execute(
            'INSERT OR REPLACE INTO stock_sector_summary (ts_code, sectors_json, total_limitups, last_updated) VALUES (?, ?, ?, ?)',
            (code, json.dumps(sectors, ensure_ascii=False), sum(s['count'] for s in sectors), now_iso)
        )
    # 已无涨停记录的股 → 删 summary 行 (避免显示过期数据)
    to_delete = [c for c in ts_codes if c not in grouped]
    if to_delete:
        del_placeholders = ','.join('?' * len(to_delete))
        conn.execute(f'DELETE FROM stock_sector_summary WHERE ts_code IN ({del_placeholders})', to_delete)
    conn.commit()
    conn.close()
    return len(grouped)


def rebuild_sector_summary():
    """全量重建 stock_sector_summary. 跑 init_db 时调."""
    import sqlite3 as _sq
    conn = _sq.connect(DB_PATH)
    conn.row_factory = _sq.Row
    rows = conn.execute(
        """SELECT DISTINCT code FROM limitup
           WHERE sector IS NOT NULL AND sector != ''"""
    ).fetchall()
    codes = [r['code'] for r in rows]
    conn.close()
    if not codes:
        return 0
    return refresh_sector_summary_for_codes(codes)
