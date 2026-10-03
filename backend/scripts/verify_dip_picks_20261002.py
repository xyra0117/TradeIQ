"""Independent raw-SQL checks for every selected event and the actual UI outcome helper."""
import json
import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app import _attach_preferred_outcomes

STAMP = '20261002'


def main():
    frame = pd.read_csv(ROOT / f'docs/dip-picks-events-{STAMP}.csv', dtype={'ts_code':str, 'trade_date':str})
    conn = sqlite3.connect((ROOT / 'backend/market_data.db').as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    calendar = [r[0] for r in conn.execute('SELECT DISTINCT cal_date FROM trading_dates_cache ORDER BY cal_date')]
    outcome_checks, threshold_checks = 0, 0
    for day, group in frame.groupby('trade_date'):
        rows = [{'ts_code':r.ts_code, 'close':r.close} for r in group.itertuples()]
        _attach_preferred_outcomes(rows, day, conn, key='validation')
        for index, row in enumerate(group.itertuples()):
            outcomes = rows[index]['validation']
            complete = True
            for n, item in enumerate(outcomes, 1):
                for suffix, key in [('high_pct','high_sum_pct'), ('close_pct','close_sum_pct')]:
                    expected, actual = getattr(row, f't{n}_{suffix}'), item[key]
                    assert (actual is None) == pd.isna(expected), (row.ts_code, day, n, suffix)
                    if actual is not None:
                        assert abs(actual - round(expected, 4)) < 1e-7
                complete &= item['high_sum_pct'] is not None
            assert complete == row.complete
            if complete:
                highs = [item['high_sum_pct'] for item in outcomes]
                assert (min(highs) > 0) == row.all_positive
                assert (max(highs) > 10) == row.touch10
                threshold_checks += 1
            outcome_checks += 1
    selection_checks = 0
    for row in frame.itertuples():
        position = calendar.index(row.trade_date)
        dates = calendar[position - 120:position + 1]
        bars = conn.execute('SELECT trade_date,open,high,low,close,change,volume FROM stock_daily '
                            'WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date',
                            (row.ts_code, dates[0], dates[-1])).fetchall()
        assert [bar['trade_date'] for bar in bars] == dates
        for index, bar in enumerate(bars):
            assert bar['low'] > 0 and bar['volume'] > 0
            assert bar['low'] <= min(bar['open'],bar['close']) and bar['high'] >= max(bar['open'],bar['close'])
            if index:
                assert abs((bar['close'] / bars[index - 1]['close'] - 1) * 100 - bar['change']) <= .15
        assert bars[-1]['change'] < 0
        ret120 = (bars[-1]['close'] / bars[0]['close'] - 1) * 100
        volume5 = bars[-1]['volume'] / (sum(bar['volume'] for bar in bars[-6:-1]) / 5)
        ma20 = sum(bar['close'] for bar in bars[-20:]) / 20
        amplitude20 = sum((bars[index]['high'] - bars[index]['low']) / bars[index - 1]['close'] * 100
                          for index in range(101,121)) / 20
        assert ret120 < 300 and volume5 < 1.2 and bars[-1]['close'] > ma20 and amplitude20 >= 5 - 1e-10
        for key, value in [('ret120',ret120), ('volume_ratio5',volume5), ('ma20',ma20), ('amplitude20',amplitude20)]:
            assert abs(getattr(row, key) - value) < 1e-7
        flows = {item['trade_date']:item['main_net_inflow'] for item in conn.execute(
            "SELECT trade_date,main_net_inflow FROM fund_flow WHERE source='eastmoney-push2' AND ts_code=? "
            'AND trade_date BETWEEN ? AND ?', (row.ts_code,dates[-20],row.trade_date))}
        assert all(day in flows for day in dates[-20:])
        sums = [math.fsum(flows[day] for day in dates[-n:]) for n in [3,5,10,20]]
        assert min(sums) > 0 and max(sums) > 50_000_000
        for n, total in zip([3,5,10,20],sums):
            assert abs(getattr(row, f'main_net_{n}d') - total) < .01
        selection_checks += 1
    source_coverage = {}
    for table, date in [('stock_daily','trade_date'), ('fund_flow','trade_date'), ('fund_flow_ths','trade_date'),
                        ('index_daily','date'), ('overview','date'), ('limitup','date'), ('market_fflow','trade_date'),
                        ('sector_fund_flow','trade_date'), ('stock_minline_bars','trade_date'),
                        ('index_minline_bars','trade_date'), ('market_notes','date'), ('theme_heat_daily','trade_date')]:
        source_coverage[table] = dict(zip(['rows','first','last','dates'],conn.execute(
            f'SELECT COUNT(*),MIN({date}),MAX({date}),COUNT(DISTINCT {date}) FROM {table}').fetchone()))
    source_coverage['eastmoney_only'] = dict(zip(['rows','first','last','dates'],conn.execute(
        "SELECT COUNT(*),MIN(trade_date),MAX(trade_date),COUNT(DISTINCT trade_date) FROM fund_flow WHERE source='eastmoney-push2'").fetchone()))
    summary = {'raw_sql_selection_checks':selection_checks, 'shared_interface_outcome_checks':outcome_checks,
               'rounded_interface_threshold_checks':threshold_checks,
               'query_only':conn.execute('PRAGMA query_only').fetchone()[0],
               'duplicate_stock_date_events':int(frame.duplicated(['ts_code','trade_date']).sum()),
               'complete_events':int(frame.complete.sum()), 'source_coverage':source_coverage}
    assert summary['duplicate_stock_date_events'] == 0
    conn.close()
    path = ROOT / f'docs/dip-picks-verification-{STAMP}.json'
    path.write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps({key:value for key,value in summary.items() if key!='source_coverage'},ensure_ascii=False))


if __name__ == '__main__':
    main()
