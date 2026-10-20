"""在线设备统计。

独立的小 SQLite 文件 (visitors.db), 不碰 market_data.db, 避免在 4GB 的行情库上
抢写锁、也不让这张会频繁更新的表把库文件撑大。

设备识别: 前端在 localStorage 里生成一个 device id, 通过 X-Device-Id 头带上,
拿不到时回退到 IP + User-Agent 哈希。用 device id 是为了让同一台设备换网络
(家里 WiFi -> 4G, IP 变了) 仍然算同一台。

上报和查询合并在同一个 GET /api/1: 前端每 30 秒轮询一次, 每次请求顺手续一次心跳。
没有用 before_request 全局挂钩, 这样只有真正打开页面的设备才被计入,
后台脚本 / 爬虫 / 静态资源请求都不会污染数字。
"""

import hashlib
import os
import re
import sqlite3
import time
from datetime import datetime

from flask import jsonify, request

DB_FILENAME = 'visitors.db'
ONLINE_WINDOW_SEC = 180     # 超过这么久没上报就算离线 (前端 30s 心跳, 留 6 次容错)
PRUNE_AFTER_SEC = 30 * 86400  # 只保留最近 30 天的设备记录
PRUNE_INTERVAL_SEC = 3600   # 清理不用每次请求都做, 一小时一次就够

_last_prune = 0.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    device_key TEXT PRIMARY KEY,
    ip         TEXT,
    ua         TEXT,
    label      TEXT,
    is_mobile  INTEGER DEFAULT 0,
    is_remote  INTEGER DEFAULT 0,
    first_seen REAL,
    last_seen  REAL
);
CREATE INDEX IF NOT EXISTS idx_visitors_last_seen ON visitors(last_seen);
"""


# ============ User-Agent 识别 ============
# 只为把 "一串看不懂的英文" 变成 "iPhone · Safari", 方便明细里认机器。
_UA_RULES = [
    # (匹配关键字, 显示名, 是否移动端)
    ('iphone',            'iPhone',  True),
    ('ipad',              'iPad',    True),
    ('android',           'Android', True),
    ('mobile',            '手机浏览器', True),
    ('macintosh',          'Mac',     False),
    ('windows nt',         'Windows', False),
    ('cros',               'Chromebook', False),
    ('linux',              'Linux',   False),
]

_BROWSER_RULES = [
    ('edg/',    'Edge'),
    ('opr/',    'Opera'),
    ('chrome/', 'Chrome'),
    ('firefox/', 'Firefox'),
    ('safari/', 'Safari'),
]


def describe_ua(ua):
    """把 User-Agent 变成 (设备名, 浏览器名, 是否移动端)。认不出就如实返回 Unknown。"""
    if not ua:
        return '未知设备', '未知浏览器', False
    low = ua.lower()
    device, mobile = '未知设备', False
    for key, name, is_mobile in _UA_RULES:
        if key in low:
            device, mobile = name, is_mobile
            break
    browser = '未知浏览器'
    for key, name in _BROWSER_RULES:
        if key in low:
            browser = name
            break
    # Windows/Android 下 Edge 会带 Edg/ 和 Chrome/，上面的顺序已经保证先认 Edg
    return device, browser, mobile


def _is_remote_ip(ip):
    """内网地址算本机, 其余 (100.x / 公网) 算远程访问。

    Flask 拿到的是直连来源 IP; 走 Tailscale 进来的会是 100.64.0.0/10 段,
    这正是需要标出来的"远程设备"。
    """
    if not ip or ip.startswith('127.'):
        return False
    m = re.match(r'^(\d+)\.(\d+)\.', ip)
    if not m:
        return False
    a, b = int(m.group(1)), int(m.group(2))
    if a == 10:
        return False
    if a == 192 and b == 168:
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    return True


# ============ 数据库 ============

def _db_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), DB_FILENAME)


def _connect(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA journal_mode=WAL')   # 读写不互相阻塞
        conn.execute('PRAGMA synchronous=NORMAL')
    except sqlite3.Error:
        pass
    return conn


def init_db(path=None):
    conn = _connect(path or _db_path())
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _device_key(ip, ua, header_id):
    """优先用前端带来的 device id, 回退到 IP+UA 哈希。"""
    if header_id:
        clean = re.sub(r'[^A-Za-z0-9_\-]', '', header_id)[:64]
        if len(clean) >= 8:
            return 'd_' + clean
    raw = f'{ip}|{ua}'.encode('utf-8', 'ignore')
    return 'h_' + hashlib.sha1(raw).hexdigest()


def _touch(conn, key, ip, ua, label, mobile, remote, now):
    conn.execute(
        """
        INSERT INTO visitors (device_key, ip, ua, label, is_mobile, is_remote, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(device_key) DO UPDATE SET
            ip = excluded.ip,
            ua = excluded.ua,
            label = excluded.label,
            is_mobile = excluded.is_mobile,
            is_remote = excluded.is_remote,
            last_seen = excluded.last_seen
        """,
        (key, ip, ua, label, int(mobile), int(remote), now, now),
    )


def _maybe_prune(conn, now):
    """删掉一个月没出现过的设备, 防止表无限增长。频率限制在一小时一次。"""
    global _last_prune
    if now - _last_prune < PRUNE_INTERVAL_SEC:
        return
    _last_prune = now
    conn.execute('DELETE FROM visitors WHERE last_seen < ?', (now - PRUNE_AFTER_SEC,))


def _humanize_ago(seconds):
    if seconds < 60:
        return '刚刚'
    if seconds < 3600:
        return f'{int(seconds // 60)} 分钟前'
    if seconds < 86400:
        return f'{int(seconds // 3600)} 小时前'
    return f'{int(seconds // 86400)} 天前'


def register_visitor_stats(app, db_path=None):
    """把 /api/online 挂到传入的 Flask app 上。

    桌面端 (5555) 和手机端 (5556) 都要注册: 两个服务面对的是不同的客户端,
    只在一边记的话就统计不到另一边的设备。两边写同一个 visitors.db, 所以
    谁看到的数字都是全量的。手机端仍然对行情库严格只读, 这里写的是独立访客表。
    """
    path = db_path or _db_path()
    init_db(path)

    @app.get('/api/online')
    def online():
        try:
            return jsonify(_collect(path))
        except sqlite3.Error as e:
            # 统计功能坏了不该拖垮页面, 返回 0 而不是 500
            app.logger.warning('[visitor_stats] 统计失败: %s', e)
            return jsonify({'ok': False, 'online': 0, 'today': 0, 'total': 0,
                            'devices': [], 'window_sec': ONLINE_WINDOW_SEC})

    return app


def _collect(path):
    now = time.time()
    ip = request.remote_addr or ''
    ua = request.headers.get('User-Agent', '')
    header_id = request.headers.get('X-Device-Id', '')
    device, browser, mobile = describe_ua(ua)
    label = f'{device} · {browser}'

    key = _device_key(ip, ua, header_id)
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA)          # 幂等, 防止有人删了库文件
        _touch(conn, key, ip, ua, label, mobile, _is_remote_ip(ip), now)
        _maybe_prune(conn, now)
        conn.commit()

        rows = conn.execute(
            """
            SELECT device_key, ip, label, is_mobile, is_remote, first_seen, last_seen
            FROM visitors ORDER BY last_seen DESC LIMIT 100
            """
        ).fetchall()
    finally:
        conn.close()

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today, online_keys = set(), set()
    devices = []
    for r in rows:
        seen = r['last_seen'] or 0
        if seen >= today_start:
            today.add(r['device_key'])
        if now - seen <= ONLINE_WINDOW_SEC:
            online_keys.add(r['device_key'])
        devices.append({
            'key': r['device_key'],
            'label': r['label'] or '未知设备',
            'ip': r['ip'] or '',
            'remote': bool(r['is_remote']),
            'mobile': bool(r['is_mobile']),
            'online': now - seen <= ONLINE_WINDOW_SEC,
            'today': seen >= today_start,
            'last_seen': seen,
            'ago': _humanize_ago(max(0, now - seen)),
        })

    return {
        'ok': True,
        'online': len(online_keys),
        'today': len(today),
        'total': len(rows),
        'devices': devices,
        'window_sec': ONLINE_WINDOW_SEC,
        'server_time': int(now),
    }