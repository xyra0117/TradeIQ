"""导入中泰证券交割单 xls (实际是 tab 分隔的 csv, GB18030 编码).

字段映射 (列顺序见表头):
  成交日期 / 证券代码(=前要去掉) / 证券名称 / 买卖标志 / 成交价格 / 成交数量 / 成交金额 /
  手续费 / 印花税 / 过户费 / 成交时间 / 委托编号 / 成交编号 / 备注(业务名称) / 交易所名称

买/卖 → direction='buy'/'sell', 其他(股息税/红利/送股/证券组合费/...) → 用备注翻译成
direction: dividend/tax/bonus/fee/transfer_out 等

代码归一化: 6 位 → xxxxxx.SH/SZ/BJ (按首位数字判定)
"""
import csv
import io
import os
import sys
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'market_data.db')

# 业务名称(列23) → direction
DIR_MAP = {
    '证券买入': 'buy',
    '证券卖出': 'sell',
    '股息税': 'tax', '股息红利税补缴': 'tax',
    '红利': 'dividend', '股息入账': 'dividend',
    '送股': 'bonus', '红股入账': 'bonus',
    '证券组合费': 'fee', '港股通组合费收取': 'fee',
    '清算冻结': 'fee', '交收资金冻结': 'fee', '交收资金冻结取消': 'fee',
    '托管转入': 'transfer_in', '结算非交易转入(对冲)': 'transfer_in',
    '托管转出': 'transfer_out', '结算非交易转出(对冲)': 'transfer_out',
    '缴申购款': 'fee', '还申购款': 'fee',
    '缴中签款': 'fee', '中签通知': 'fee',
    '调账转出': 'transfer_out',
    '市值申购中签': 'fee', '市值申购中签扣款': 'fee',
    '市值申购中签扣款回冲': 'fee', '新股申购确认缴款': 'fee', '新股入账': 'bonus',
}


def norm_code(raw):
    """='601869' → 601869.SH (Excel 单元格公式格式带 =)"""
    raw = raw.strip().lstrip('=').strip().strip('"')
    if not raw.isdigit() or len(raw) != 6:
        return None
    if raw[0] == '6' or raw.startswith('900'):
        return f'{raw}.SH'
    if raw[0] in '489':
        return f'{raw}.BJ'
    return f'{raw}.SZ'


def main(path):
    with open(path, 'rb') as f:
        text = f.read().decode('gb18030', errors='replace')
    rows = list(csv.reader(io.StringIO(text), delimiter='\t'))
    data = [r for r in rows[1:] if len(r) >= 24 and r[0].strip()]
    print(f'表头 {len(rows[0])} 列, 数据 {len(data)} 行')

    # 业务名称列在 idx 23
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    inserted = skip_dup = skip_unknown = 0
    skipped_examples = []
    for r in data:
        ts_code = norm_code(r[1])
        if not ts_code:
            skip_unknown += 1
            continue
        biz = r[23].strip()
        direction = DIR_MAP.get(biz)
        if not direction:
            skip_unknown += 1
            if len(skipped_examples) < 5:
                skipped_examples.append(f'  跳过 {biz}: {r[:3]}')
            continue
        trade_date = r[0].strip()
        if len(trade_date) == 8 and trade_date.isdigit():
            trade_date_fmt = f'{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}'
        else:
            trade_date_fmt = trade_date

        trade_time = r[15].strip() or None
        applied = 1 if direction in ('buy', 'sell') else -1

        try:
            c.execute('''INSERT OR IGNORE INTO trades
                (trade_no, ts_code, name, direction, price, shares, amount,
                 trade_date, trade_time, note, applied)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (r[29].strip() or None,
                 ts_code,
                 r[2].strip(),
                 direction,
                 float(r[4] or 0),
                 float(r[5] or 0),
                 float(r[6] or 0),
                 trade_date_fmt,
                 trade_time,
                 biz,
                 applied))
            if c.rowcount > 0:
                inserted += 1
            else:
                skip_dup += 1
        except Exception as e:
            print(f'  insert err {ts_code} {trade_date_fmt}: {e}')

    conn.commit()
    conn.close()
    print(f'插入了 {inserted} 条, 重复 {skip_dup} 条, 未识别 {skip_unknown} 条')
    for s in skipped_examples:
        print(s)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else '/Users/xyra/Downloads/交割单1.xls')
