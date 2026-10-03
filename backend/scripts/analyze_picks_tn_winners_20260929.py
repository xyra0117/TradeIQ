# 一次性只读研究: 系统推荐 T+1~T+5 累计最高涨幅「全正且峰值 >5%」样本的共性.
# 口径与 dashboard 条件优选 / 系统推荐新增的 T+N 列完全一致 (high_sum_pct = 前 N-1 日
# 收盘涨跌幅相加 + 第 N 日最高涨幅). 特征一律只用入选日当天及之前可得的数据, 无未来函数.
# 产出: docs/picks-tn-winners-analysis-20260929.md + 同名 .csv
import json
import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs'
THRESH = 5.0          # 峰值门槛 %
MIN_SUM = 0.0         # 5 格全部要 > 0
LOOKBACK = 60         # 入选日前取多少个交易日算前期特征

c = sqlite3.connect(f'file:{ROOT}/backend/market_data.db?mode=ro', uri=True)
c.execute('PRAGMA query_only=ON')
q = lambda s, *a: pd.read_sql_query(s, c, params=tuple(a[0]) if len(a) == 1 else a)

picks = q('SELECT * FROM stock_picks ORDER BY trade_date, score DESC')
picks['reasons'] = picks['reasons_json'].fillna('').map(
    lambda s: json.loads(s) if s else [])
cal = q("SELECT cal_date FROM trading_dates_cache ORDER BY cal_date").cal_date.tolist()
print(f'picks={len(picks)} dates={picks.trade_date.nunique()} '
      f'range={picks.trade_date.min()}~{picks.trade_date.max()}', flush=True)

# 只取入选股票 + 前后窗口的日线, 避免 225 万行全量进内存
codes = sorted(picks.ts_code.unique())
CACHE = Path(tempfile.gettempdir()) / 'tradeiq_picks_tn_daily.pkl'
if CACHE.exists():
    daily = pd.read_pickle(CACHE)
    print(f'daily rows={len(daily)} codes={daily.ts_code.nunique()} (cache)', flush=True)
else:
    ph = ','.join('?' * len(codes))
    daily = q(f'SELECT ts_code,trade_date,open,high,low,close,change,volume,amount '
              f'FROM stock_daily WHERE ts_code IN ({ph})', codes)
    daily.to_pickle(CACHE)
    print(f'daily rows={len(daily)} codes={len(codes)}', flush=True)

by_code = {k: g.sort_values('trade_date').reset_index(drop=True)
           for k, g in daily.groupby('ts_code', sort=False)}
idx_by_code = {k: {d: i for i, d in enumerate(g.trade_date)}
               for k, g in by_code.items()}
pos = {d: i for i, d in enumerate(cal)}


def bar(code, day, field):
    """取 code 在 day 当天(或之前最近一天)的字段."""
    g = by_code.get(code)
    if g is None:
        return np.nan
    i = idx_by_code[code].get(day)
    if i is None:
        j = g.index[g.trade_date <= day]
        if not len(j):
            return np.nan
        i = int(j[-1])
    return g.at[i, field]


def outcomes(code, day, n=5):
    """T+1..T+n 的 high_sum_pct / close_sum_pct, 口径同 _attach_preferred_outcomes.
    任一天数据缺失/不完整/参考价对不上 → 该格 reason 非 None (不计入 settled)."""
    g = by_code.get(code)
    if g is None:
        return [None] * n
    p = pos.get(day)
    if p is None:
        return [None] * n
    days = cal[p + 1: p + 1 + n]
    if len(days) < n:
        return [None] * n
    m = idx_by_code[code]
    base = m.get(day)
    if base is None or not np.isfinite(g.at[base, 'close']) or g.at[base, 'close'] <= 0:
        return [None] * n
    prev = float(g.at[base, 'close'])
    cum = 0.0
    out = []
    for d in days:
        i = m.get(d)
        if i is None:
            out.append(None)
            continue
        o, h, cl = g.at[i, 'open'], g.at[i, 'high'], g.at[i, 'close']
        ch = g.at[i, 'change']
        if not all(np.isfinite(v) and v > 0 for v in (o, h, cl)) or h < max(o, cl) - 1e-3:
            out.append(None)
            continue
        change = (cl / prev - 1) * 100
        if not np.isfinite(ch) or abs(change - ch) > 0.15:
            out.append(None)
            continue
        out.append({'date': d, 'high': cum + (h / prev - 1) * 100, 'close': cum + change})
        cum += change
        prev = cl
    return out


rows = []
for r in picks.itertuples(index=False):
    oc = outcomes(r.ts_code, r.trade_date)
    row = {
        'trade_date': r.trade_date, 'ts_code': r.ts_code, 'name': r.name,
        'score': r.score, 'signal_type': r.signal_type, 'close0': r.close,
        'change_pct0': r.change_pct, 'ma5': r.ma5, 'ma10': r.ma10,
        'ma20': r.ma20, 'ma30': r.ma30,
        'ma5_slope': r.ma5_slope, 'ma20_slope': r.ma20_slope,
        'main_net_today': r.main_net_today, 'main_net_3d': r.main_net_3d,
        'main_net_5d': r.main_net_5d, 'main_net_10d': r.main_net_10d,
        'main_net_20d': r.main_net_20d, 'main_net_pct_today': r.main_net_pct_today,
        'super_net': r.super_net, 'big_net': r.big_net,
        'mid_net': r.mid_net, 'small_net': r.small_net,
        'super_pct': r.super_pct, 'big_pct': r.big_pct,
        'mid_pct': r.mid_pct, 'small_pct': r.small_pct,
        'reasons': r.reasons,
    }
    for k in range(1, 6):
        o = oc[k - 1]
        row[f'h{k}'] = o['high'] if o else np.nan
        row[f'c{k}'] = o['close'] if o else np.nan
    rows.append(row)
df = pd.DataFrame(rows)

H = df[[f'h{k}' for k in range(1, 6)]].values
df['settled'] = ~np.isnan(H).any(axis=1)
df['peak'] = H.max(axis=1)
df['peak_n'] = pd.Series(np.where(df.settled, H.argmax(axis=1) + 1, np.nan)).astype('Int64')
df['all_pos'] = df.settled & (H > MIN_SUM).all(axis=1)
df['winner'] = df.all_pos & (df.peak > THRESH)
df['final5'] = df[[f'c{k}' for k in range(1, 6)]].values.sum(axis=1)
print(f'settled={df.settled.sum()} all_pos={df.all_pos.sum()} winner={df.winner.sum()}', flush=True)

# ── 入选当时可得特征 (全部无未来函数) ──
prev_close, prev5, prev20, vol5, amt5, hi20, ma60 = [], [], [], [], [], [], []
for r in df.itertuples(index=False):
    g = by_code.get(r.ts_code)
    i = idx_by_code[r.ts_code].get(r.trade_date)
    if g is None or i is None:
        prev_close.append(np.nan); prev5.append(np.nan); prev20.append(np.nan)
        vol5.append(np.nan); amt5.append(np.nan); hi20.append(np.nan); ma60.append(np.nan)
        continue
    j = i - 1
    p1 = float(g.at[j, 'close']) if j >= 0 else np.nan
    prev_close.append(p1)
    lo = max(0, i - 5)
    prev5.append((g.at[i, 'close'] / g.at[lo, 'close'] - 1) * 100 if g.at[lo, 'close'] else np.nan)
    lo20 = max(0, i - 20)
    prev20.append((g.at[i, 'close'] / g.at[lo20, 'close'] - 1) * 100 if g.at[lo20, 'close'] else np.nan)
    vol5.append(g['volume'].iloc[i] / g['volume'].iloc[lo:i].mean() if g['volume'].iloc[lo:i].mean() else np.nan)
    amt5.append(g['amount'].iloc[lo:i].sum() if i > lo else np.nan)
    hi20.append(g['high'].iloc[max(0, i - 20):i + 1].max())
    closes = g['close'].iloc[max(0, i - 59):i].dropna()
    ma60.append(closes.mean() if len(closes) >= 40 else np.nan)
df['prev_close'] = prev_close
df['prev5'] = prev5
df['prev20'] = prev20
df['vol_mult'] = vol5
df['amt5'] = amt5
df['hi20'] = hi20
df['ma60'] = ma60

df['bull_stack'] = (df.ma5 > df.ma10) & (df.ma10 > df.ma20) & (df.ma20 > df.ma30)
df['above_ma60'] = df.ma60.notna() & (df.close0 > df.ma60)
# 注意: stock_daily.amount 单位是千元, fund_flow/stock_picks 的净额单位是元, 主力强度要 ×1000 换算
df['strength5'] = df.main_net_5d / (df.amt5 * 1e3) * 100
df['strength_today'] = df.main_net_today / (df.amt5 / 5 * 1e3) * 100
df['small_ratio'] = df.small_net / df.main_net_today.replace(0, np.nan) * 100
df['drawdown20'] = (df.close0 / df.hi20 - 1) * 100
df['limitup'] = df.change_pct0 >= 9.8
df['dropday'] = df.change_pct0 < 0
df['price_bucket'] = pd.cut(df.close0, [0, 20, 50, 100, np.inf],
                            labels=['<20', '20-50', '50-100', '>=100'])
df['chg_bucket'] = pd.cut(df.change_pct0, [-np.inf, 0, 2, 5, 8, 9.8, np.inf],
                          labels=['<0%', '0-2%', '2-5%', '5-8%', '8-9.8%', '涨停'])
# stock_basic 的行业/市值字段全库为空, 用申万行业 + 成交额(体量代理) 代替
df['amt5_yi'] = df.amt5 / 1e5          # 近5日成交额, 亿元
df['amt_bucket'] = pd.cut(df.amt5_yi, [0, 5, 20, 50, 100, np.inf],
                          labels=['<5亿', '5-20亿', '20-50亿', '50-100亿', '>=100亿'])
df['market'] = df.ts_code.str[-2:]
df['score_pct'] = df.groupby('trade_date')['score'].rank(pct=True)
df['score_bucket'] = pd.cut(df.score_pct, [0, .2, .4, .6, .8, 1.0],
                            labels=['评分后20%', '20-40%', '40-60%', '60-80%', '评分前20%'])

basic = q('SELECT ts_code,listing_date FROM stock_basic')
sw = q("SELECT m.ts_code, i.industry_name FROM sw_industry_member m "
       "JOIN sw_industry i ON i.index_code=m.index_code "
       "WHERE i.level='L1' AND m.ts_code IN (SELECT ts_code FROM stock_basic)")
sw_map = dict(zip(sw.ts_code, sw.industry_name))
df = df.merge(basic, on='ts_code', how='left')
df['industry_name'] = [sw_map.get(t, '—') for t in df.ts_code]
sector = q('SELECT ts_code,sectors_json FROM stock_sector_summary')
smap = {r.ts_code: sorted(json.loads(r.sectors_json),
                          key=lambda s: (not s.get('is_latest'), -s.get('count', 0)))
        for r in sector.itertuples(index=False)}
df['sectors'] = [smap.get(t, []) for t in df.ts_code]
df['primary_sector'] = [s[0]['sector'] if s else '—' for s in df.sectors]
df['n_sectors'] = [len(s) for s in df.sectors]

# 排名 (按当日评分)
df['day_rank'] = df.groupby('trade_date')['score'].rank(ascending=False, method='first').astype(int)
df['rank_decile'] = pd.cut(df.day_rank, [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100],
                           labels=[f'{i * 10 + 1}-{i * 10 + 10}' for i in range(10)])

base = df[df.settled]           # 可统计总体
win = df[df.winner]             # 目标样本
hit = base.winner.mean() * 100
print(f'base={len(base)} win={len(win)} hit={hit:.2f}%', flush=True)

# 大盘基准: 同期上证 / 沪深300 的 5 日累计涨幅, 用来判断命中率是不是被行情 beta 抬起来的
bench = q("SELECT date, close FROM index_daily WHERE name='上证指数' ORDER BY date")
bench = bench.drop_duplicates('date').set_index('date').close.sort_index()
bench_ret = bench.pct_change(5) * 100
bench_df = pd.DataFrame({'cal_date': bench.index, 'sh_close': bench.values,
                         'mkt_r5': bench_ret.values})
base = base.merge(bench_df, left_on='trade_date', right_on='cal_date', how='left').drop(columns=['cal_date'])
win = win.merge(bench_df, left_on='trade_date', right_on='cal_date', how='left').drop(columns=['cal_date'])
mkt5 = bench_ret.reindex(sorted(base.trade_date.unique())).dropna()
print(f'同期上证 5 日累计涨幅: 均值 {mkt5.mean():.2f}% · 中位数 {mkt5.median():.2f}% · '
      f'为正比例 {(mkt5 > 0).mean() * 100:.1f}%', flush=True)


def bucket_table(col, bins=None, label=''):
    g = base.groupby(col, observed=True).winner.agg(['size', 'sum'])
    g['hit'] = g['sum'] / g['size'] * 100
    g = g.rename(columns={'size': '样本数', 'sum': '命中数'})
    return g.sort_values('hit', ascending=False)


def stat_table(cols):
    out = []
    for col in cols:
        a = pd.to_numeric(base[col], errors='coerce')
        b = pd.to_numeric(win[col], errors='coerce')
        out.append({
            '特征': col, '全体均值': round(a.mean(), 3), '命中组均值': round(b.mean(), 3),
            '差值': round(b.mean() - a.mean(), 3),
            '全体中位数': round(a.median(), 3), '命中组中位数': round(b.median(), 3),
            '全体>0占比%': round((a > 0).mean() * 100, 1),
            '命中组>0占比%': round((b > 0).mean() * 100, 1),
        })
    return pd.DataFrame(out)


# ── 命中理由标签 (把理由里的具体数字抹掉, 只保留规则名) ──
import re
from collections import Counter

NUM = re.compile(r'-?\d+(?:\.\d+)?')


def norm_tag(t):
    t = NUM.sub('N', t)
    return re.sub(r'\s+', ' ', t).strip()


all_tags, win_tags = Counter(), Counter()
for tags, settled, is_win in zip(df.reasons, df.settled, df.winner):
    if not settled:
        continue                  # 分母只用可统计样本, 才能和总体命中率直接比
    for t in set(norm_tag(x) for x in tags):
        all_tags[t] += 1
        if is_win:
            win_tags[t] += 1
tag_df = pd.DataFrame([
    {'标签': t, '出现次数': n, '其中命中': win_tags.get(t, 0),
     '命中率%': round(win_tags.get(t, 0) / n * 100, 2)}
    for t, n in all_tags.items() if n >= 30
]).sort_values(['命中率%', '出现次数'], ascending=False)

peak_dist = win.peak_n.value_counts().sort_index()
peak_bucket = pd.cut(win.peak, [5, 10, 15, 20, 30, 50, np.inf],
                     labels=['5-10%', '10-15%', '15-20%', '20-30%', '30-50%', '>=50%'],
                     include_lowest=True).value_counts().sort_index()

summary = {
    '入选总次数': int(len(df)), '入选日数': int(df.trade_date.nunique()),
    '可统计(5格齐全)': int(len(base)),
    '5格全正': int(base.all_pos.sum()),
    '5格全正且峰值>5%': int(len(win)),
    '全正占比%': round(base.all_pos.mean() * 100, 2),
    '命中率(对可统计)%': round(hit, 2),
    '命中组独立股票数': int(win.ts_code.nunique()),
    '命中组峰值中位数%': round(win.peak.median(), 2),
    '命中组T+5收盘累计中位数%': round(win.final5.median(), 2),
    '可统计组T+5收盘累计中位数%': round(base.final5.median(), 2),
    '同期上证5日累计涨幅均值%': round(mkt5.mean(), 2),
    '同期上证5日为正比例%': round((mkt5 > 0).mean() * 100, 1),
}
print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)

# ── 落盘 ──
OUT.mkdir(exist_ok=True)
df.to_csv(OUT / 'picks-tn-outcomes-20260929.csv', index=False,
          encoding='utf-8-sig')
lines = ['# 系统推荐 T+1~T+5 累计最高涨幅「全正且峰值 >5%」样本共性分析',
         '', f'生成时间 2026-09-29 · 数据源 backend/market_data.db (只读)',
         '', '## 一、总体口径与结果', '',
         'T+N 口径与 dashboard「系统推荐 / 条件优选」表格中的 T+N 列完全一致:',
         '前 N−1 个交易日收盘涨跌幅**相加** + 第 N 日 (最高价 / 前收 − 1)。',
         '同一只股票多次入选按独立样本计。特征只用入选日当天及之前可得的数据。', '',
         '| 指标 | 数值 |', '| --- | --- |']
lines += [f'| {k} | {v} |' for k, v in summary.items()]


def md(df_, title, note=''):
    s = ['', f'### {title}', '']
    if note:
        s += [note, '']
    cols = list(df_.columns)
    s += ['| ' + ' | '.join(str(x) for x in cols) + ' |',
          '| ' + ' | '.join('---' for _ in cols) + ' |']
    for r in df_.itertuples(index=False):
        vals = []
        for v in r:
            if isinstance(v, float):
                vals.append(f'{v:.2f}')
            elif pd.isna(v):
                vals.append('')
            else:
                vals.append(str(v))
        s.append('| ' + ' | '.join(vals) + ' |')
    return s


lines += md(pd.Series(peak_dist).rename('次数').reset_index()
            .rename(columns={'index': '峰值出现在'}), '二、峰值出现在第几天')
lines += md(peak_bucket.rename('命中数').reset_index().rename(
    columns={'index': '峰值区间'}), '三、峰值分布')
lines += md(stat_table([
    'score', 'score_pct', 'change_pct0', 'close0', 'prev5', 'prev20',
    'ma5_slope', 'ma20_slope', 'main_net_today', 'main_net_pct_today',
    'main_net_3d', 'main_net_5d', 'main_net_10d', 'main_net_20d',
    'strength5', 'strength_today', 'super_pct', 'big_pct', 'mid_pct',
    'small_pct', 'small_ratio', 'vol_mult', 'drawdown20',
]).rename(columns={'score': '评分', 'score_pct': '当日评分分位', 'change_pct0': '入选日涨幅%',
                    'close0': '入选日收盘价', 'prev5': '入选前5日涨幅%', 'prev20': '入选前20日涨幅%',
                    'ma5_slope': 'MA5斜率', 'ma20_slope': 'MA20斜率',
                    'main_net_today': '当日主力净额(亿)', 'main_net_pct_today': '当日主力净占比%',
                    'main_net_3d': '3日主力(亿)', 'main_net_5d': '5日主力(亿)',
                    'main_net_10d': '10日主力(亿)', 'main_net_20d': '20日主力(亿)',
                    'strength5': '5日主力强度%', 'strength_today': '当日主力强度%',
                    'super_pct': '超大单占比%', 'big_pct': '大单占比%',
                    'mid_pct': '中单占比%', 'small_pct': '小单占比%',
                    'small_ratio': '小单/主力%', 'vol_mult': '量比(对前5日)',
                    'drawdown20': '距20日高点%'}),
    '四、连续型特征对比 (全体可统计样本 vs 命中组)')

for col, title in [('score_bucket', '五、按当日评分分位'),
                   ('rank_decile', '六、按当日评分排名十分位'),
                   ('signal_type', '七、按信号类型'),
                   ('price_bucket', '八、按股价档'),
                   ('amt_bucket', '九、按近5日日均成交额 (体量代理)'),
                   ('market', '十、按市场')]:
    lines += md(bucket_table(col).reset_index().rename(columns={col: '分组'}), title,
                f'总体命中率 {hit:.2f}% · 样本量见「样本数」列')

def bool_block(mask, up, down):
    return pd.DataFrame([
        {'分组': up, '样本数': int(mask.sum()),
         '命中数': int(base.loc[mask, 'winner'].sum()),
         '命中率%': round(base.loc[mask, 'winner'].mean() * 100, 2)},
        {'分组': down, '样本数': int((~mask).sum()),
         '命中数': int(base.loc[~mask, 'winner'].sum()),
         '命中率%': round(base.loc[~mask, 'winner'].mean() * 100, 2)},
    ])


lines += md(pd.concat([
    bool_block(base.bull_stack, '多头排列 MA5>MA10>MA20>MA30', '非多头排列'),
    bool_block(base.above_ma60, '入选日站上 MA60', '入选日跌破 MA60'),
    bool_block(base.limitup, '入选日涨停', '入选日非涨停'),
    bool_block(base.dropday, '入选日下跌', '入选日上涨'),
    bool_block(base.ma5_slope > 45, 'MA5 陡峭向上 >45°', 'MA5 斜率 ≤45°'),
], ignore_index=True), '十一、形态类特征命中率', f'总体命中率 {hit:.2f}%')

lines += md(bucket_table('chg_bucket').reset_index().rename(
    columns={'chg_bucket': '入选日涨跌幅'}), '十二、命中 vs 入选日涨跌幅',
    f'总体命中率 {hit:.2f}% · 涨停股 (n={int(base.limitup.sum())}) '
    f'命中率 {base.loc[base.limitup, "winner"].mean() * 100:.2f}%, '
    f'其余 {base.loc[~base.limitup, "winner"].mean() * 100:.2f}%')

# 按入选日拆: 命中是否集中在少数几天 (若是, 说明是行情 beta 而非选股能力)
by_day = (base.groupby('trade_date')
          .agg(样本数=('winner', 'size'), 命中数=('winner', 'sum'),
               当日命中率=('winner', lambda s: round(s.mean() * 100, 2)),
               上证当日=('sh_close', 'first')))
lines += md(by_day.reset_index().sort_values('当日命中率', ascending=False).head(12),
            '十三、命中率最高的 12 个入选日',
            f'全期平均 {hit:.2f}% · 最低的 12 天平均 '
            f'{by_day["当日命中率"].nsmallest(12).mean():.2f}% · '
            f'若头部集中度过高说明是行情驱动')

lines += md(tag_df.head(28), '十四、命中理由规则命中率 (出现≥30次)',
            f'总体命中率 {hit:.2f}% · 数字已抹掉只保留规则名 · '
            f'分母 = 该规则出现的可统计样本数')

ind = bucket_table('industry_name')
lines += md(ind[ind['样本数'] >= 8].head(25).reset_index().rename(
    columns={'industry_name': '申万一级行业'}), '十五、行业命中率 (样本≥8)')
sec = bucket_table('primary_sector')
lines += md(sec[sec['样本数'] >= 5].head(25).reset_index().rename(
    columns={'primary_sector': '主力板块'}), '十六、板块命中率 (样本≥5)')

top = (win.sort_values('peak', ascending=False)
       .head(25)[['trade_date', 'ts_code', 'name', 'industry_name', 'primary_sector', 'score',
                  'change_pct0', 'prev5', 'strength5', 'small_ratio',
                  'h1', 'h2', 'h3', 'h4', 'h5', 'peak', 'peak_n', 'final5']])
lines += md(top, '十七、命中组峰值 Top25')

# 同票复现: 同一只股票多次入选, 第一次入选就命中的比例
first = df.sort_values('trade_date').groupby('ts_code').head(1)
rep = (df.groupby('ts_code').winner.agg(['size', 'sum'])
       .query('size >= 3'))
lines += md(pd.DataFrame([
    {'指标': '入选≥3次的股票数', '数值': int(len(rep))},
    {'指标': '其中至少命中过一次的股票数', '数值': int((rep['sum'] > 0).sum())},
    {'指标': '其中至少命中过一次的占比%', '数值': round((rep['sum'] > 0).mean() * 100, 2)},
    {'指标': '复现股贡献的命中次数占全部命中%', '数值': round(rep['sum'].sum() / int(win.shape[0]) * 100, 2)},
    {'指标': '单只股票最大入选次数', '数值': int(rep['size'].max())},
    {'指标': '首次入选即命中的股票数', '数值': int(first.winner.sum())},
    {'指标': '首次入选命中率% (对比全期 33.38%)', '数值': round(first.winner.mean() * 100, 2)},
]), '十八、复现性 (同票多次入选)')

(OUT / 'picks-tn-winners-analysis-20260929.md').write_text(
    '\n'.join(lines) + '\n', encoding='utf-8')
tag_df.to_csv(OUT / 'picks-tn-tag-hitrate-20260929.csv', index=False, encoding='utf-8-sig')
print('written', OUT / 'picks-tn-winners-analysis-20260929.md', flush=True)
