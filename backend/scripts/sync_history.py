"""补全 20250515 ~ 20251104 的日线数据"""
import tushare as ts
import sqlite3
import time
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings('ignore')

TUSHARE_TOKEN = '1905eff139f559e9caa61c7363ac18e6217a33032e9e434812db1e34'
DB_PATH = 'market_data.db'

ts.set_token(TUSHARE_TOKEN)
pro = ts.pro_api()

def get_name_map(codes):
    """批量获取股票名称"""
    name_map = {}
    batch_size = 100
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        try:
            df_basic = pro.stock_basic(ts_code=','.join(batch))
            for _, r in df_basic.iterrows():
                name_map[r['ts_code']] = r['name']
        except:
            for code in batch:
                try:
                    df_b = pro.stock_basic(ts_code=code)
                    name_map[code] = df_b.iloc[0]['name'] if len(df_b) > 0 else ''
                except:
                    name_map[code] = ''
    return name_map

def sync_date(trade_date):
    """同步单日所有股票数据"""
    print(f'  同步 {trade_date} ...', end='', flush=True)
    try:
        df = pro.daily(trade_date=trade_date)
        if len(df) == 0:
            print(' 无数据')
            return 0

        codes = df['ts_code'].unique().tolist()
        name_map = get_name_map(codes)

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        count = 0
        for _, row in df.iterrows():
            try:
                name = name_map.get(row['ts_code'], '')
                c.execute('''INSERT OR IGNORE INTO stock_daily
                    (ts_code, name, trade_date, open, high, low, close, change, volume, amount)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (row['ts_code'], name, trade_date,
                     row['open'], row['high'], row['low'], row['close'],
                     row['pct_chg'], row['vol'], row['amount']))
                count += 1
            except Exception as e:
                continue
        conn.commit()
        conn.close()
        print(f' {count} 条')
        return count
    except Exception as e:
        print(f' 错误: {e}')
        return -1

def main():
    target_start = '20250515'
    target_end = '20251104'

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    existing = set(r[0] for r in c.execute('SELECT DISTINCT trade_date FROM stock_daily').fetchall())
    conn.close()

    df_cal = pro.trade_cal(exchange='SSE', start_date=target_start, end_date=target_end)
    trading_dates = sorted(df_cal[df_cal['is_open'] == 1]['cal_date'].tolist(), reverse=True)
    to_sync = [d for d in trading_dates if d not in existing]

    print(f'需要同步: {len(to_sync)} 天\n')

    success = 0
    fail = 0
    for i, date in enumerate(to_sync):
        print(f'[{i+1}/{len(to_sync)}]', end=' ')
        ret = sync_date(date)
        if ret > 0:
            success += 1
        else:
            fail += 1
        # 每 200 条休息一下，避免触发限流
        if (i + 1) % 200 == 0:
            print('  --- 休息 3 秒 ---')
            time.sleep(3)
        else:
            time.sleep(0.1)

    print(f'\n完成: 成功 {success} 天, 失败 {fail} 天')

    # 验证结果
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    min_date = c.execute('SELECT MIN(trade_date) FROM stock_daily').fetchone()[0]
    max_date = c.execute('SELECT MAX(trade_date) FROM stock_daily').fetchone()[0]
    cnt = c.execute('SELECT COUNT(DISTINCT trade_date) FROM stock_daily').fetchone()[0]
    print(f'现在数据库范围: {min_date} ~ {max_date}, 共 {cnt} 个交易日')

if __name__ == '__main__':
    main()