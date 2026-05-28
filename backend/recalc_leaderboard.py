"""重新计算所有交易日的涨幅榜单"""
import sqlite3
import sys
import time
import warnings
from datetime import datetime, timedelta
warnings.filterwarnings('ignore')

sys.path.insert(0, '.')
from app import get_top_stocks, INTERVALS, DB_PATH

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
rows = c.execute('''
    SELECT DISTINCT trade_date FROM stock_daily
    ORDER BY trade_date DESC
''').fetchall()
all_dates = [r[0] for r in rows]
conn.close()

# 跳过最早的20天（20日榜需要至少21个交易日的历史数据）
all_dates = all_dates[:len(all_dates) - 20]
print(f'共 {len(all_dates)} 天需计算')

total = len(all_dates)
success = 0
fail = 0

for i, date in enumerate(all_dates):
    print(f'[{i+1}/{total}] {date}', end=' ', flush=True)
    for lb_type, days in INTERVALS:
        stocks = get_top_stocks(days, date, 10)
        if stocks is None:
            continue

        conn2 = sqlite3.connect(DB_PATH)
        c2 = conn2.cursor()
        c2.execute('DELETE FROM leaderboard WHERE date=? AND type=?', (date, lb_type))
        for rank, s in enumerate(stocks):
            c2.execute('''INSERT INTO leaderboard
                (date, type, rank, code, name, change, volume, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                (date, lb_type, rank+1, s['code'], s['name'],
                 s['change'], s.get('amount', 0), s.get('reason', '')))
        conn2.commit()
        conn2.close()

    print(f'✓ ({len(stocks) if "stocks" in dir() else 0})')
    success += 1
    if (i + 1) % 50 == 0:
        print(f'  -- 已完成 {i+1}/{total} --')
        time.sleep(1)

print(f'\n完成: 成功 {success} 天, 失败 {fail} 天')

# 验证
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
lb_dates = c.execute('SELECT COUNT(DISTINCT date) FROM leaderboard').fetchone()[0]
lb_records = c.execute('SELECT COUNT(*) FROM leaderboard').fetchone()[0]
min_d = c.execute('SELECT MIN(date) FROM leaderboard').fetchone()[0]
max_d = c.execute('SELECT MAX(date) FROM leaderboard').fetchone()[0]
print(f'leaderboard验证: {lb_records}条记录, {lb_dates}个交易日, 范围{min_d}~{max_d}')
conn.close()