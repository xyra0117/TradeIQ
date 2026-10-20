"""Read-only T+5 research. See docs/t5-research-20261008-protocol.md.

Stages: prepare -> search (development only) -> freeze -> evaluate (one holdout).
Temporary cached arrays stay in /tmp; never import the application or write SQLite.
"""
import argparse
import collections
import hashlib
import itertools
import json
import pickle
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PREFIX = 't5-research-20261008'
CACHE = Path('/tmp/tradeiq-t5-20261008-features.pkl')
ENHANCED_CACHE = Path('/tmp/tradeiq-t5-20261008-enhanced-features.pkl')
DOCS = ROOT / 'docs'
TRAIN_END, DEV_END, TEST_END = '20260331', '20260630', '20260930'


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.generic):
        return clean(value.item())
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def write_json(path, value):
    path.write_text(json.dumps(clean(value), ensure_ascii=False, indent=2), encoding='utf-8')


def open_db(path=ROOT / 'backend/market_data.db'):
    conn = sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    return conn


def shift(a, n=1):
    out = np.full_like(a, np.nan, dtype=np.float32)
    if n > 0:
        out[n:] = a[:-n]
    elif n < 0:
        out[:n] = a[-n:]
    else:
        out[:] = a
    return out


def rolling(a, n, kind='mean'):
    return getattr(pd.DataFrame(a).rolling(n, min_periods=n), kind)().to_numpy(np.float32).copy()


def limit_fraction(code, name, date):
    if code.endswith('.BJ'):
        return .30
    if code.startswith(('300', '301', '688', '689')):
        return .20
    if (not identity_known(name, code) or 'ST' in str(name).upper()) and date < '20260706':
        return .05
    return .10


def identity_known(name, code):
    value = str(name).strip()
    return bool(value) and value not in {code, code[:6], 'nan', 'None'} and not value.isdigit()


def limit_price(previous, fraction, up=True):
    # Decimal half-up equivalent for positive exchange prices, not bankers rounding.
    return np.floor(previous * (1 + fraction if up else 1 - fraction) * 100 + .500001) / 100


def prepare():
    with open_db() as conn:
        sql = "SELECT ts_code,trade_date,name,open,high,low,close,change,volume,amount FROM stock_daily WHERE trade_date>='20250401' ORDER BY trade_date,ts_code"
        daily = pd.read_sql_query(sql, conn)
        last = daily.trade_date.max()
        dates = pd.read_sql_query('SELECT cal_date FROM trading_dates_cache WHERE cal_date BETWEEN ? AND ? ORDER BY cal_date', conn, params=('20250401', last)).cal_date.tolist()
        codes = sorted(daily.ts_code.unique())
        a = {k: daily.pivot(index='trade_date', columns='ts_code', values=k).reindex(index=dates, columns=codes).to_numpy(np.float32)
             for k in ['open', 'high', 'low', 'close', 'change', 'volume', 'amount']}
        names = daily.pivot(index='trade_date', columns='ts_code', values='name').reindex(index=dates, columns=codes).fillna('').to_numpy()
        del daily
        close, prev = a['close'], shift(a['close'])
        reference_ok = np.abs((close / prev - 1) * 100 - a['change']) <= .15
        bar_ok = (close > 0) & (a['open'] > 0) & (a['high'] >= np.maximum(close, a['open'])) & (a['low'] <= np.minimum(close, a['open'])) & (a['low'] > 0) & (a['volume'] > 0) & (a['amount'] > 0)
        valid = reference_ok & bar_ok
        hist = {n: rolling(valid.astype(np.float32), n, 'sum') == n for n in [3, 5, 10, 14, 20, 60]}
        ma = {n: rolling(close, n) for n in [5, 10, 20, 60]}
        f = {'day': a['change'], 'amount': a['amount'] / 100000,
             'vr': a['volume'] / shift(rolling(a['volume'], 5)),
             'amp20': rolling((a['high'] - a['low']) / prev * 100, 20),
             'clv': (close - a['low']) / np.maximum(a['high'] - a['low'], .01),
             'bias5': (close / ma[5] - 1) * 100,
             'bias20': (close / ma[20] - 1) * 100,
             'bias60': (close / ma[60] - 1) * 100,
             'ma10': ma[10], 'ma20': ma[20], 'ma60': ma[60]}
        for n in [3, 5, 10, 20, 60]:
            f[f'ret{n}'] = ((close / shift(close, n) - 1) * 100)
            f[f'ret{n}'][~hist[n]] = np.nan
        for n in [20, 60]:
            high = shift(rolling(a['high'], n, 'max'))
            f[f'break{n}'] = (close / high - 1) * 100
            f[f'draw{n}'] = (close / rolling(a['high'], n, 'max') - 1) * 100
            f[f'break{n}'][~(hist[n] & (shift(hist[n].astype(np.float32)) == 1))] = np.nan
            f[f'draw{n}'][~hist[n]] = np.nan
        for n in [10, 20, 60]:
            h = hist[n] if n in hist else rolling(valid.astype(np.float32), n, 'sum') == n
            f[f'ma{n}'][~h] = np.nan
        gains, losses = np.maximum(a['change'], 0), np.maximum(-a['change'], 0)
        for n in [2, 6, 14]:
            g, loss = rolling(gains, n), rolling(losses, n)
            f[f'rsi{n}'] = 100 * g / np.maximum(g + loss, .00001)
        f['atr14'] = rolling(np.maximum(a['high'] - a['low'], np.maximum(np.abs(a['high'] - prev), np.abs(a['low'] - prev))) / prev * 100, 14)
        lim = np.zeros(close.shape, dtype=np.float32)
        for j, code in enumerate(codes):
            lim[:, j] = [limit_fraction(code, names[i, j], d) for i, d in enumerate(dates)]
        upper = limit_price(prev, lim)
        up = valid & (close >= upper - .001)
        streak = np.zeros(close.shape, dtype=np.float32)
        for i in range(len(dates)):
            streak[i] = np.where(up[i], (streak[i - 1] if i else 0) + 1, 0)
        f['streak'] = streak
        f['limit10'] = rolling(up.astype(np.float32), 10, 'sum')
        f['oneword'] = ((a['high'] - a['low']) < .005).astype(np.float32)
        flows = pd.read_sql_query("SELECT trade_date,ts_code,main_net_inflow,super_net,big_net,small_net,source FROM fund_flow WHERE source IN ('eastmoney-push2','eastmoney-daykline') AND trade_date>=?", conn, params=(dates[0],))
        assert not flows.duplicated(['trade_date', 'ts_code']).any(), 'ambiguous source duplicates'
        for key, col in [('main', 'main_net_inflow'), ('super', 'super_net'), ('big', 'big_net'), ('small', 'small_net')]:
            flow = flows.pivot(index='trade_date', columns='ts_code', values=col).reindex(index=dates, columns=codes).to_numpy(np.float32)
            f[key + '1'] = flow / (a['amount'] * 1000) * 100
            for n in [3, 5]:
                f[key + str(n)] = rolling(flow, n, 'sum') / (rolling(a['amount'], n, 'sum') * 1000) * 100
        index = pd.read_sql_query("SELECT date,name,close,change FROM index_daily WHERE REPLACE(date,'-','')>=?", conn, params=(dates[0],))
        index['date'] = index.date.str.replace('-', '')
        ix = index[index.name == '上证指数'].drop_duplicates('date').set_index('date').reindex(dates)
        market = {'index_close': ix.close.to_numpy(np.float32), 'index_day': ix.change.to_numpy(np.float32)}
        market['index_ret20'] = (market['index_close'] / shift(market['index_close'], 20) - 1) * 100
        market['breadth'] = np.sum((a['change'] > 0) & valid, axis=1) / np.maximum(valid.sum(axis=1), 1)
        f['rs20'] = f['ret20'] - market['index_ret20'][:, None]
        # Historical themes only from stock-specific disclosures already dated <= signal.
        lu = pd.read_sql_query('SELECT date,code,sector FROM limitup WHERE REPLACE(date,\'-\',\'\')>=?', conn, params=(dates[0],))
        lu['date'] = lu.date.str.replace('-', '')
        lu = lu[lu.date.isin(dates) & lu.sector.notna() & ~lu.sector.isin(['其他', '公告'])].drop_duplicates(['date', 'code', 'sector'])
        sectors = lu.groupby('date')
        last_sector, recent, date_map = {}, collections.deque(maxlen=5), {d: i for i, d in enumerate(dates)}
        code_map = {s[:6]: j for j, s in enumerate(codes)}
        heat = np.full(close.shape, np.nan, dtype=np.float32)
        observed = set(lu.date)
        for i, date in enumerate(dates):
            sub = sectors.get_group(date) if date in observed else None
            counts = collections.Counter() if sub is None else collections.Counter(sub.sector)
            if sub is not None:
                for r in sub.itertuples():
                    j = code_map.get(str(r.code).split('.')[0].zfill(6))
                    if j is not None:
                        last_sector[j] = (r.sector, i)
            recent.append((date, counts))
            if len(recent) == 5 and all(d in observed for d, _ in recent):
                total = sum((x for _, x in recent), collections.Counter())
                for j, (sector, seen) in last_sector.items():
                    if i - seen <= 20:
                        heat[i, j] = total[sector]
        f['heat5'] = heat
        # Market margin reports are not same-day decision inputs; lag by two sessions.
        margin = pd.read_sql_query('SELECT dim_date,rzye,rzjme FROM rzrq_history_summary', conn)
        margin['dim_date'] = margin.dim_date.str.replace('-', '')
        mg = margin.drop_duplicates('dim_date').set_index('dim_date').reindex(dates)
        market['margin5'] = shift((mg.rzye.to_numpy(np.float32) / shift(mg.rzye.to_numpy(np.float32), 5) - 1) * 100, 2)
        # Preserve unknown identities rather than use current stock_basic survivor list.
        identity_ok = np.array([[identity_known(s, codes[j]) and 'ST' not in s.upper() and '退' not in s for j, s in enumerate(row)] for row in names])
        base = hist[20] & shift(hist[20], 1).astype(bool) & identity_ok & (close >= 2) & (f['amount'] >= 1)
        data = {'dates': dates, 'codes': codes, 'names': names, 'a': a, 'f': f, 'market': market,
                'valid': valid, 'reference_ok': reference_ok, 'base': base, 'lim': lim,
                'metadata': {'daily_rows': sum(np.isfinite(close).ravel()), 'first': dates[0], 'last': dates[-1],
                             'stocks': len(codes), 'days': len(dates), 'query_only': conn.execute('PRAGMA query_only').fetchone()[0],
                             'fund_sources': flows.groupby('source').agg(rows=('ts_code', 'count'), first=('trade_date', 'min'), last=('trade_date', 'max')).reset_index().to_dict('records'),
                             'missing_name_bars': int((names == '').sum()), 'unverifiable_reference_bars': int((~reference_ok & np.isfinite(close) & np.isfinite(prev)).sum())}}
    with CACHE.open('wb') as file:
        pickle.dump(data, file, protocol=pickle.HIGHEST_PROTOCOL)
    print(json.dumps(clean(data['metadata']), ensure_ascii=False), flush=True)


def candidate_grid():
    configs = []
    grids = {
        'continuation': {'streak': [1, 2, 3], 'retmax': [40, 80]},
        'pullback': {'momentum': [15, 30], 'volmax': [.8, 1.2], 'ma': [10, 20]},
        'breakout': {'amp': [5, 7], 'window': [20, 60], 'volmin': [1.2, 1.8]},
        'reversal': {'drop': [-10, -20], 'bounce': [0, 3], 'volmin': [1, 1.5]},
        'oversold': {'rsi': [10, 20], 'drop3': [-5, -10]},
        'fund_momentum': {'momentum': [10, 20], 'flow': [3, 5], 'window': [3, 5]},
        'fund_pullback': {'momentum': [10, 20], 'flow': [1, 3], 'volmax': [.8, 1.2]},
    }
    for family, grid in grids.items():
        for vals in itertools.product(*grid.values()):
            for rank, k in itertools.product(['price', 'liquidity'], [1, 3, 5]):
                cfg = {'family': family, **dict(zip(grid, vals)), 'rank': rank, 'k': k, 'gapmax': 5,
                       'board': 'all', 'regime': 'all'}
                cfg['id'] = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]
                configs.append(cfg)
    return configs


def signals(data, cfg):
    f, a = data['f'], data['a']
    family = cfg['family']
    mask = data['base'].copy()
    if family == 'continuation':
        mask &= (f['streak'] == cfg['streak']) & (f['ret20'] <= cfg['retmax']) & (f['oneword'] == 0)
        score = f['ret20'] - f['bias5'] + f['clv'] * 10
    elif family == 'pullback':
        mask &= (f['ret20'] >= cfg['momentum']) & (f['ret3'] >= -10) & (f['ret3'] < 0) & (f['day'] <= 1) & (a['close'] > f[f'ma{cfg["ma"]}']) & (f['vr'] <= cfg['volmax'])
        score = -f['ret3'] + .2 * f['ret20'] - np.maximum(f['bias20'], 0) * .3
    elif family == 'breakout':
        mask &= (f['amp20'] >= cfg['amp']) & (f[f'break{cfg["window"]}'] > 0) & (f['vr'] >= cfg['volmin']) & (f['clv'] >= .7)
        score = f['rs20'] - f['bias5'] + f['clv'] * 10
    elif family == 'reversal':
        mask &= (f['ret10'] <= cfg['drop']) & (f['day'] >= cfg['bounce']) & (f['vr'] >= cfg['volmin']) & (f['clv'] >= .7)
        score = -f['ret10'] + f['clv'] * 10
    elif family == 'oversold':
        mask &= (f['rsi2'] <= cfg['rsi']) & (f['ret3'] <= cfg['drop3']) & (f['amp20'] >= 4)
        score = -f['ret3'] - .1 * f['rsi2']
    elif family == 'fund_momentum':
        mask &= (f['ret5'] >= cfg['momentum']) & (f[f'main{cfg["window"]}'] >= cfg['flow']) & (f['amp20'] >= 5) & (a['close'] > f['ma20'])
        score = f[f'main{cfg["window"]}'] + .2 * f['rs20'] - .2 * f['bias5']
    elif family == 'fund_pullback':
        mask &= (f['ret20'] >= cfg['momentum']) & (f['main5'] >= cfg['flow']) & (f['ret3'] >= -10) & (f['ret3'] < 0) & (f['vr'] <= cfg['volmax']) & (a['close'] > f['ma20'])
        score = f['main5'] - f['ret3'] + .1 * f['rs20']
    elif family == 'ridge':
        model = cfg['model']
        features = model_inputs(data, cfg['short_history'])
        score = np.full(data['base'].shape, model['intercept'], dtype=np.float32)
        for name, low, high, center, scale, weight in zip(model['features'], model['low'], model['high'], model['center'], model['scale'], model['weights']):
            values = np.clip(features[name], low, high)
            values = np.nan_to_num((values - center) / scale, nan=0, posinf=0, neginf=0)
            score += weight * values
        if cfg['short_history']:
            mask &= np.isfinite(f['main5'])
        mask &= score >= cfg.get('pred_min', -100)
    else:
        raise ValueError(family)
    if cfg['rank'] == 'liquidity':
        score = .5 * score + np.log1p(f['amount']) * 3 + f['clv'] * 5
    elif cfg['rank'] == 'flow':
        score = score + np.nan_to_num(f['main5'], nan=0) * 2
    if cfg.get('board') == '20pct':
        allowed = np.array([s.startswith(('300', '301', '688', '689')) for s in data['codes']])
        mask &= allowed[None, :]
    if cfg.get('extension') == 'industry_strength':
        mask &= f['industry_rs20'] > 0
        score = score + f['industry_rs20'] * .25
    elif cfg.get('extension') == 'sector_flow':
        mask &= f['sector_main_pct5'] > 0
        score = score + f['sector_main_pct5']
    elif cfg.get('extension') == 'market_flow':
        mask &= (data['market']['market_main5_pct'] > 0)[:, None]
    elif cfg.get('extension') == 'index_agree':
        mask &= (data['market']['index_up_count'] >= 3)[:, None]
    if cfg.get('regime') == 'positive':
        mask &= (data['market']['index_ret20'] > 0)[:, None]
    elif cfg.get('regime') == 'weak':
        mask &= (data['market']['breadth'] < .45)[:, None]
    elif cfg.get('regime') == 'margin':
        mask &= (data['market']['margin5'] > 0)[:, None]
    elif cfg.get('regime') == 'hot':
        mask &= f['heat5'] >= 10
    mask &= np.isfinite(score)
    return mask, score


def planned_outcome(data, signal, stock, end, gapmax=5, slippage=.001):
    """Select first; evaluate only afterward. Never prefilter by future label validity."""
    a, dates = data['a'], data['dates']
    entry, planned = signal + 1, signal + 5
    result = {'signal': dates[signal], 'code': data['codes'][stock], 'name': data['names'][signal, stock],
              'entry': dates[entry] if entry < len(dates) else None,
              'planned_exit': dates[planned] if planned < len(dates) else None,
              'signal_i': signal, 'stock_i': stock, 'entry_i': entry}
    if planned > end:
        return {**result, 'status': 'not_mature'}
    price, anchor = a['open'][entry, stock], a['close'][signal, stock]
    if not np.isfinite(price) or price <= 0:
        return {**result, 'status': 'no_open'}
    fraction = data['lim'][entry, stock]
    upper = limit_price(anchor, fraction)
    if price >= upper - .001:
        return {**result, 'status': 'open_at_limit_up'}
    if (price / anchor - 1) * 100 > gapmax:
        return {**result, 'status': 'gap_too_high'}
    # Filled before future flags are inspected. Unverifiable reference changes remain trades.
    bought = min(float(price * (1 + slippage)), float(upper))
    for exit_i in range(planned, min(planned + 21, end + 1)):
        px = a['close'][exit_i, stock]
        pprev = a['close'][exit_i - 1, stock]
        if not np.isfinite(px) or px <= 0 or not np.isfinite(pprev):
            continue
        lower = limit_price(pprev, data['lim'][exit_i, stock], up=False)
        if px <= lower + .001:
            continue
        sold = max(float(px * (1 - slippage)), float(lower))
        ref = data['reference_ok'][entry:exit_i + 1, stock]
        # A missing path or unknown identity cannot be declared a verified return.
        identity_unknown = any(not identity_known(name, data['codes'][stock]) for name in data['names'][entry:exit_i + 1, stock])
        ambiguous = bool((~ref).any()) or identity_unknown
        gross = (sold / bought - 1) * 100
        net = (sold * (1 - .0003 - .00001 - .0005) / (bought * (1 + .0003 + .00001)) - 1) * 100
        return {**result, 'status': 'reference_unknown' if ambiguous else 'executed', 'exit': dates[exit_i], 'exit_i': exit_i,
                'buy_price': bought, 'sell_price': sold, 'gross': gross, 'net': net if not ambiguous else None,
                'raw_net': net, 'delay': exit_i - planned, 'unknown_identity': identity_unknown,
                'unknown_reference': bool((~ref).any()),
                'peak': (np.nanmax(a['high'][entry:exit_i + 1, stock]) / bought - 1) * 100,
                'trough': (np.nanmin(a['low'][entry:exit_i + 1, stock]) / bought - 1) * 100}
    return {**result, 'status': 'exit_unresolved', 'buy_price': bought, 'exit_i': end + 1, 'net': None}


def period_bounds(data, start, end):
    dates = np.array(data['dates'])
    indices = np.flatnonzero((dates >= start) & (dates <= end))
    return int(indices[0]), int(indices[-1])


def event_backtest(data, cfg, start, end, slippage=.001):
    first, last = period_bounds(data, start, end)
    mask, score = signals(data, cfg)
    ledger, last_exit = [], {}
    for i in range(first, last - 4):
        eligible = np.flatnonzero(mask[i])
        eligible = np.array([j for j in eligible if last_exit.get(j, -1) < i + 1], dtype=int)
        top = eligible[np.argsort(-score[i, eligible], kind='stable')[:cfg['k']]]
        for j in top:
            row = planned_outcome(data, i, int(j), last, cfg['gapmax'], slippage)
            row['rank_score'] = float(score[i, j])
            row['selection_rank'] = int(np.flatnonzero(top == j)[0]) + 1
            ledger.append(row)
            if row['status'] in ['executed', 'reference_unknown', 'exit_unresolved']:
                last_exit[j] = row['exit_i']
    return ledger


def summarize(ledger, bootstrap=False, calendar=None):
    chosen = pd.DataFrame(ledger)
    if chosen.empty:
        return {'n': 0, 'mean': None, 'days': 0, 'selected': 0}
    verified = chosen[chosen.status == 'executed'].copy()
    unknown = chosen.status.isin(['reference_unknown', 'exit_unresolved'])
    result = {'selected': len(chosen), 'n': len(verified), 'days': verified.signal.nunique(),
              'unknown': int(unknown.sum()), 'statuses': chosen.status.value_counts().to_dict()}
    if verified.empty:
        return {**result, 'mean': None}
    net = verified.net.to_numpy(float)
    result.update(mean=float(net.mean()), median=float(np.median(net)), win=float((net > 0).mean() * 100),
                  hit10=float((net >= 10).mean() * 100), worst=float(net.min()), p10=float(np.quantile(net, .1)),
                  trimmed_top5=float(np.sort(net)[:max(1, int(len(net) * .95))].mean()),
                  stressed_unknown_mean=float((net.sum() - 30 * unknown.sum()) / (len(net) + unknown.sum())),
                  months=verified.groupby(verified.signal.str[:6]).net.agg(['count', 'mean', 'median']).reset_index().to_dict('records'))
    if bootstrap:
        sums = verified.groupby('signal').net.agg(['sum', 'count'])
        # Include intervening exchange dates, so sparse signals do not compress time blocks.
        if calendar is None:
            raise ValueError('Bootstrap requires the actual exchange calendar')
        cal = np.array(calendar)
        cal = cal[(cal >= verified.signal.min()) & (cal <= verified.signal.max())]
        sums = sums.reindex(cal, fill_value=0)
        samples, rng = [], np.random.default_rng(61008)
        for _ in range(2000):
            starts = rng.integers(0, max(1, len(sums) - 4), size=int(np.ceil(len(sums) / 5)))
            ids = np.concatenate([np.arange(s, min(s + 5, len(sums))) for s in starts])[:len(sums)]
            sample = sums.iloc[ids]
            if sample['count'].sum():
                samples.append(sample['sum'].sum() / sample['count'].sum())
        result['date_block_ci95'] = list(np.quantile(samples, [.025, .975]))
    return clean(result)


_DATA = None


def load_data():
    global _DATA
    if _DATA is None:
        with (ENHANCED_CACHE if ENHANCED_CACHE.exists() else CACHE).open('rb') as file:
            _DATA = pickle.load(file)
        names = _DATA['names']
        codes = np.array(_DATA['codes'])
        _DATA['base'] &= (names != codes[None, :]) & (names != np.array([s[:6] for s in codes])[None, :])
    return _DATA


def search():
    data = load_data()
    rows = []
    for count, cfg in enumerate(candidate_grid(), 1):
        short = cfg['family'].startswith('fund_')
        tr = summarize(event_backtest(data, cfg, '20260409' if short else '20250701', '20260529' if short else TRAIN_END))
        va = summarize(event_backtest(data, cfg, '20260601' if short else '20260401', DEV_END))
        enough = tr['n'] >= (60 if short else 150) and va['n'] >= (30 if short else 60) and va['days'] >= (10 if short else 20)
        # Unknown outcomes penalize ranking. Weight validation stability, not isolated maximum.
        value = min(tr.get('stressed_unknown_mean', -100), va.get('stressed_unknown_mean', -100)) if enough else -100
        rows.append({'config': cfg, 'train': tr, 'validation': va, 'eligible': enough, 'objective': value})
        if count % 30 == 0:
            print(f'development candidates {count}', flush=True)
    ranked = sorted(rows, key=lambda r: (-r['objective'], -(r['validation'].get('mean') or -100), r['config']['id']))
    write_json(DOCS / f'{PREFIX}-round1.json', {'holdout_read': False, 'candidates': len(rows), 'results': ranked})
    print(json.dumps(clean([{'config': r['config'], 'train': r['train'], 'validation': r['validation']} for r in ranked[:6]]), ensure_ascii=False), flush=True)


def refine():
    data = load_data()
    initial = json.loads((DOCS / f'{PREFIX}-round1.json').read_text())['results']
    seeds, seen = [], set()
    for r in initial:
        if r['eligible'] and r['config']['family'] not in seen:
            seeds.append(r['config'])
            seen.add(r['config']['family'])
    rows = []
    for seed in seeds:
        for gap, board, regime in itertools.product([0, 3, 5], ['all', '20pct'], ['all', 'positive', 'weak', 'margin', 'hot']):
            cfg = {**seed, 'gapmax': gap, 'board': board, 'regime': regime}
            cfg['id'] = hashlib.sha256(json.dumps({k: v for k, v in cfg.items() if k != 'id'}, sort_keys=True).encode()).hexdigest()[:12]
            short = cfg['family'].startswith('fund_')
            tr = summarize(event_backtest(data, cfg, '20260409' if short else '20250701', '20260529' if short else TRAIN_END))
            va = summarize(event_backtest(data, cfg, '20260601' if short else '20260401', DEV_END))
            enough = tr['n'] >= (60 if short else 150) and va['n'] >= (30 if short else 60) and va['days'] >= (10 if short else 20)
            objective = min(tr.get('stressed_unknown_mean', -100), va.get('stressed_unknown_mean', -100)) if enough else -100
            rows.append({'config': cfg, 'train': tr, 'validation': va, 'eligible': enough, 'objective': objective})
        print('refined family ' + seed['family'], flush=True)
    ranked = sorted(rows, key=lambda r: (-r['objective'], -(r['validation'].get('mean') or -100), r['config']['id']))
    write_json(DOCS / f'{PREFIX}-round2.json', {'holdout_read': False, 'candidates': len(rows), 'results': ranked})
    print(json.dumps(clean(ranked[:5]), ensure_ascii=False), flush=True)


def model_inputs(data, short_history=False):
    f = data['f']
    features = {k: f[k] for k in ['day', 'ret3', 'ret5', 'ret10', 'ret20', 'bias5', 'bias20', 'amp20', 'vr', 'rsi2', 'rsi14', 'clv', 'streak', 'rs20', 'atr14']}
    features['liquidity_log'] = np.log1p(f['amount'])
    features['reversal_squared'] = np.minimum(f['ret3'], 0) ** 2
    features['momentum_pullback'] = np.maximum(f['ret20'], 0) * np.minimum(f['ret3'], 0)
    features['day_squared'] = f['day'] ** 2
    if 'industry_rs20' in f:
        features['industry_rs20'] = f['industry_rs20']
    for name in ['breadth', 'index_ret20', 'index_up_count', 'market_main5_pct']:
        if name in data['market']:
            features[name] = np.broadcast_to(data['market'][name][:, None], data['base'].shape)
    if short_history:
        for name in ['main1', 'main3', 'main5', 'super5', 'small5', 'sector_main_pct5']:
            if name in f:
                features[name] = f[name]
    return features


def ridge_training_rows(data, short_history):
    """Vectorized labels use the same entry, delayed exit and identity rules."""
    first, end = period_bounds(data, '20260409' if short_history else '20250701', '20260529' if short_history else TRAIN_END)
    mask = data['base'].copy()
    mask[:first] = False
    mask[end - 4:] = False
    for lag in range(1, 6):
        mask &= shift(data['valid'].astype(np.float32), -lag) == 1
    if short_history:
        mask &= np.isfinite(data['f']['main5'])
    day, stock = np.where(mask)
    price = data['a']['open'][day + 1, stock]
    anchor = data['a']['close'][day, stock]
    executable = (price < limit_price(anchor, data['lim'][day + 1, stock]) - .001) & ((price / anchor - 1) * 100 <= 5)
    day, stock, price = day[executable], stock[executable], price[executable]
    buy = np.minimum(price * 1.001, limit_price(data['a']['close'][day, stock], data['lim'][day + 1, stock]))
    target = np.full(len(day), np.nan)
    exit_days = np.full(len(day), -1)
    known = np.ones(len(day), dtype=bool)
    codes = np.array(data['codes'])[stock]
    def identities(at, js, selected_codes):
        value = np.char.strip(data['names'][at, js].astype(str))
        return (value != '') & (value != 'nan') & (value != 'None') & (value != selected_codes) & (value != np.array([s[:6] for s in selected_codes])) & ~np.char.isdigit(value)
    for lag in range(1, 6):
        known &= identities(day + lag, stock, codes)
    for delay in range(21):
        ids = np.flatnonzero(np.isnan(target) & known & (day + 5 + delay <= end))
        if len(ids) == 0:
            break
        at, js = day[ids] + 5 + delay, stock[ids]
        if delay:
            valid_path = data['reference_ok'][at, js] & identities(at, js, codes[ids])
            known[ids] &= valid_path
        px, previous = data['a']['close'][at, js], data['a']['close'][at - 1, js]
        lower = limit_price(previous, data['lim'][at, js], up=False)
        okay = known[ids] & np.isfinite(px) & (px > 0) & np.isfinite(previous) & (px > lower + .001)
        accepted = ids[okay]
        sale = np.maximum(px[okay] * .999, lower[okay])
        target[accepted] = (sale * (1 - .00081) / (buy[accepted] * (1 + .00031)) - 1) * 100
        exit_days[accepted] = at[okay]
    accepted = np.isfinite(target)
    return day[accepted], stock[accepted], np.clip(target[accepted], -40, 40), exit_days[accepted]


def fit_ridge(data, short_history, regularization):
    """All transformations and labels are fitted inside the development interval."""
    day, stock, target, exit_days = ridge_training_rows(data, short_history)
    if not len(target):
        raise ValueError('No mature verified labels in the training interval')
    features = model_inputs(data, short_history)
    columns, low, high, center, scale = [], [], [], [], []
    for name, values in features.items():
        sample = values[day, stock].astype(np.float64)
        available = sample[np.isfinite(sample)]
        lo, hi = np.quantile(available, [.005, .995]) if len(available) else (-1, 1)
        sample = np.clip(sample, lo, hi)
        mid = np.nanmean(sample) if len(available) else 0
        std = max(1e-6, np.nanstd(sample)) if len(available) else 1
        columns.append(np.nan_to_num((sample - mid) / std, nan=0))
        low.append(lo); high.append(hi); center.append(mid); scale.append(std)
    x = np.column_stack(columns)
    # Equal day weights prevent a larger universe on one day from dominating the fit.
    count = np.bincount(day, minlength=len(data['dates']))
    weights = 1 / count[day]
    weights /= weights.sum()
    intercept = float(np.sum(weights * target))
    beta = np.linalg.solve(x.T @ (x * weights[:, None]) + regularization * np.eye(x.shape[1]), x.T @ (weights * (target - intercept)))
    return clean({'features': list(features), 'low': low, 'high': high, 'center': center, 'scale': scale,
                  'weights': beta.tolist(), 'intercept': intercept, 'regularization': regularization,
                  'train_examples': len(target), 'latest_label_date': data['dates'][int(exit_days.max())],
                  'target_clip_training_only': [-40, 40]})


def development_result(data, cfg):
    short = cfg.get('short_history', cfg['family'].startswith('fund_'))
    tr = summarize(event_backtest(data, cfg, '20260409' if short else '20250701', '20260529' if short else TRAIN_END))
    va = summarize(event_backtest(data, cfg, '20260601' if short else '20260401', DEV_END))
    enough = tr['n'] >= (60 if short else 150) and va['n'] >= (30 if short else 60) and va['days'] >= (10 if short else 20)
    objective = min(tr.get('stressed_unknown_mean', -100), va.get('stressed_unknown_mean', -100)) if enough else -100
    return {'config': cfg, 'train': tr, 'validation': va, 'eligible': enough, 'objective': objective}


def advanced():
    data = load_data()
    if not ENHANCED_CACHE.exists():
        raise RuntimeError('Run enhance first')
    initial = json.loads((DOCS / f'{PREFIX}-round1.json').read_text())['results']
    second = json.loads((DOCS / f'{PREFIX}-round2.json').read_text())['results']
    seeds, seen = [], set()
    for row in sorted(initial + second, key=lambda r: -r['objective']):
        family = row['config']['family']
        if row['eligible'] and family not in seen:
            seeds.append(row['config']); seen.add(family)
    rows = []
    for seed in seeds:
        for extension, rank in itertools.product(['industry_strength', 'sector_flow', 'market_flow', 'index_agree'], ['price', 'liquidity', 'flow']):
            cfg = {**seed, 'extension': extension, 'rank': rank}
            cfg['id'] = hashlib.sha256(json.dumps({k: v for k, v in cfg.items() if k != 'id'}, sort_keys=True).encode()).hexdigest()[:12]
            rows.append(development_result(data, cfg))
        print('information increment ' + seed['family'], flush=True)
    for short, l2 in itertools.product([False, True], [.01, .1, 1.]):
        model = fit_ridge(data, short, l2)
        print(f'ridge fitted short={short} l2={l2} rows={model["train_examples"]}', flush=True)
        for k, gap, threshold in itertools.product([3, 5], [0, 3], [-100, 5]):
            cfg = {'family': 'ridge', 'short_history': short, 'model': model, 'k': k, 'gapmax': gap,
                   'pred_min': threshold, 'rank': 'price', 'board': 'all', 'regime': 'all'}
            cfg['id'] = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]
            rows.append(development_result(data, cfg))
    ranked = sorted(rows, key=lambda r: (-r['objective'], -(r['validation'].get('mean') or -100), r['config']['id']))
    write_json(DOCS / f'{PREFIX}-round3.json', {'holdout_read': False, 'candidates': len(rows), 'results': ranked})
    print(json.dumps(clean([{'config': {k: v for k, v in r['config'].items() if k != 'model'}, 'train': r['train'], 'validation': r['validation'], 'eligible': r['eligible']} for r in ranked[:5]]), ensure_ascii=False), flush=True)


def freeze():
    r1 = json.loads((DOCS / f'{PREFIX}-round1.json').read_text())['results']
    r2 = json.loads((DOCS / f'{PREFIX}-round2.json').read_text())['results']
    r3 = json.loads((DOCS / f'{PREFIX}-round3.json').read_text())['results'] if (DOCS / f'{PREFIX}-round3.json').exists() else []
    unique = {r['config']['id']: r for r in r1 + r2 + r3 if r['eligible']}
    eligible = sorted(unique.values(), key=lambda r: (-r['objective'], -(r['validation'].get('mean') or -100), r['config']['id']))
    if not eligible:
        raise RuntimeError('No candidate met development sample sufficiency')
    capital_checks = []
    for row in eligible[:12]:
        cfg = row['config']
        short = cfg.get('short_history', cfg['family'].startswith('fund_'))
        _, _, tr = portfolio(load_data(), cfg, '20260409' if short else '20250701', '20260529' if short else TRAIN_END, bootstrap=False)
        _, _, va = portfolio(load_data(), cfg, '20260601' if short else '20260401', DEV_END, bootstrap=False)
        sufficient = tr['n'] >= (60 if short else 150) and va['n'] >= (30 if short else 60) and va['days'] >= (10 if short else 20)
        capital_checks.append({**row, 'capital_train': tr, 'capital_validation': va, 'capital_sufficient': sufficient,
                               'capital_objective': min(tr['stressed_unknown_mean'], va['stressed_unknown_mean']) if sufficient else -100})
    if not any(r['capital_sufficient'] for r in capital_checks):
        raise RuntimeError('Capital simulation has insufficient development trades')
    winner = max(capital_checks, key=lambda r: r['capital_objective'])
    # Freeze one challenger per family to diagnose performance, without post-test selection.
    challengers, seen = [], {winner['config']['family']}
    for row in eligible:
        if row['config']['family'] not in seen:
            challengers.append(row)
            seen.add(row['config']['family'])
    lock = {'holdout_read_before_freeze': False, 'selection': 'max minimum(train, validation) mean after unknown=-30 sensitivity',
            'target_net_mean': 10, 'primary': winner, 'challengers': challengers,
            'total_tried': len(r1) + len(r2) + len(r3), 'capital_checks': capital_checks, 'metadata': load_data()['metadata'],
            'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_json(DOCS / f'{PREFIX}-frozen.json', lock)
    print(json.dumps(clean(winner), ensure_ascii=False), flush=True)


def portfolio(data, cfg, start, end, slippage=.001, selector=None, bootstrap=True):
    first, last = period_bounds(data, start, end)
    mask, score = (selector or signals)(data, cfg)
    cash, previous_nav, positions = 1_000_000., 1_000_000., {}
    ledger, navs, skipped = [], [], collections.Counter()
    for day in range(first, last + 1):
        if first < day and day + 4 <= last:
            signal = day - 1
            eligible = np.flatnonzero(mask[signal])
            eligible = np.array([j for j in eligible if j not in positions], dtype=int)
            top = eligible[np.argsort(-score[signal, eligible], kind='stable')[:cfg['k']]]
            budget = min(.1, .2 / cfg['k']) * previous_nav
            for rank, stock in enumerate(top, 1):
                row = planned_outcome(data, signal, int(stock), last, cfg['gapmax'], slippage)
                if row['status'] not in ['executed', 'reference_unknown', 'exit_unresolved']:
                    skipped[row['status']] += 1
                    ledger.append({**row, 'selection_rank': rank, 'shares': 0, 'portfolio_status': 'no_fill'})
                    continue
                size = min(budget, cash, float(data['a']['amount'][signal, stock]) * 1000 * .001)
                lot = 200 if data['codes'][stock].startswith(('688', '689')) else 100
                shares = int(size / (row['buy_price'] * 1.00031) // lot) * lot
                cost = shares * row['buy_price']
                commission = max(5., cost * .0003)
                total = cost + commission + cost * .00001
                if shares == 0 or total > cash:
                    skipped['insufficient_cash_or_lot'] += 1
                    ledger.append({**row, 'selection_rank': rank, 'shares': 0, 'portfolio_status': 'no_cash'})
                    continue
                cash -= total
                entry_row = {**row, 'selection_rank': rank, 'shares': shares, 'invested': total, 'portfolio_status': 'held', 'last_mark': row['buy_price']}
                positions[int(stock)] = entry_row
                ledger.append(entry_row)
        for stock, row in list(positions.items()):
            mark = data['a']['close'][day, stock]
            if np.isfinite(mark) and mark > 0:
                row['last_mark'] = float(mark)
            if row['exit_i'] == day:
                sale = row['shares'] * row['sell_price']
                proceeds = sale - max(5., sale * .0003) - sale * (.00001 + .0005)
                cash += proceeds
                row['actual_net'] = (proceeds / row['invested'] - 1) * 100 if row['status'] == 'executed' else None
                row['raw_actual_net'] = (proceeds / row['invested'] - 1) * 100
                row['portfolio_status'] = 'closed'
                del positions[stock]
        holdings = sum(r['shares'] * r['last_mark'] for r in positions.values())
        previous_nav = cash + holdings
        navs.append({'date': data['dates'][day], 'nav': previous_nav, 'cash': cash, 'positions': len(positions), 'exposure': holdings / previous_nav})
    frame = pd.DataFrame(navs)
    peak = np.maximum.accumulate(np.r_[1_000_000., frame.nav.to_numpy()])[1:]
    verified = [dict(r, net=r['actual_net']) for r in ledger if r.get('portfolio_status') == 'closed' and r['status'] == 'executed']
    filled = [r for r in ledger if r.get('shares', 0) > 0]
    summary = summarize(verified, bootstrap=bootstrap, calendar=data['dates'])
    summary.update(portfolio_return=(previous_nav / 1_000_000 - 1) * 100,
                   max_drawdown=float(np.min(frame.nav / peak - 1) * 100), average_exposure=float(frame.exposure.mean() * 100),
                   filled=len(filled), unresolved=int(sum(r['status'] != 'executed' for r in filled)),
                   open_positions=len(positions), skips=dict(skipped), nav_kind='raw unadjusted price marks; unknown corporate actions disclosed')
    summary['stressed_unknown_mean'] = (sum(r['actual_net'] for r in verified) - 30 * summary['unresolved']) / max(1, len(filled))
    return ledger, frame, clean(summary)


def evaluate():
    lock_path = DOCS / f'{PREFIX}-frozen.json'
    lock = json.loads(lock_path.read_text())
    if (DOCS / f'{PREFIX}-results.json').exists():
        raise RuntimeError('Holdout already viewed; do not overwrite its independent-test record')
    if lock['source_sha256'] != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise RuntimeError('Research source changed after freeze; freeze again before first evaluation')
    data = load_data()
    cfg = lock['primary']['config']
    ledger, nav, result = portfolio(data, cfg, '20260701', TEST_END)
    event_summary = summarize(event_backtest(data, cfg, '20260701', TEST_END), bootstrap=True, calendar=data['dates'])
    conditions = {'net_mean_at_least10': (result.get('mean') or -100) >= 10,
                  'at_least100_trades': result['n'] >= 100, 'at_least30_signal_days': result['days'] >= 30,
                  'three_months': len(result.get('months', [])) >= 3,
                  'block_ci_lower_positive': result.get('date_block_ci95', [-100])[0] > 0,
                  'all_filled_trades_verifiable': result['unresolved'] == 0}
    stress = []
    for slip in [.003, .005]:
        _, _, s = portfolio(data, cfg, '20260701', TEST_END, slippage=slip)
        stress.append({'slippage_each_side': slip, 'result': s})
    baselines = {}
    for name in ['universe_liquidity', 'old_surge']:
        # Fixed benchmark selection before results, same execution/capital assumptions.
        def baseline_signals(d, config, which=name):
            f, a = d['f'], d['a']
            mask = d['base'].copy()
            if which == 'old_surge':
                mask &= (f['amp20'] >= 5) & (a['close'] > f['ma20']) & ((f['ret20'] > 15) | (f['break20'] > 0))
            return mask, f['amount']
        _, benchmark_nav, s = portfolio(data, {**cfg, 'k': 3, 'gapmax': 5}, '20260701', TEST_END, selector=baseline_signals)
        baselines[name] = s
        nav[name] = benchmark_nav.nav.to_numpy()
    challengers = []
    for row in lock['challengers']:
        _, _, s = portfolio(data, row['config'], '20260701', TEST_END)
        challengers.append({'config': row['config'], 'development': row, 'holdout': s, 'role': 'pre-frozen diagnostic, not primary replacement'})
    report = {'frozen_sha256': hashlib.sha256(lock_path.read_bytes()).hexdigest(), 'config': cfg,
              'holdout': result, 'event_unconstrained': event_summary, 'acceptance_checks': conditions,
              'passed_research_gate': all(conditions.values()), 'reference_drawdown20_met': result['max_drawdown'] >= -20,
              'stress': stress, 'baselines': baselines, 'challengers': challengers,
              'read_only': True, 'holdout_can_be_reused_as_independent_test': False}
    write_json(DOCS / f'{PREFIX}-results.json', report)
    pd.DataFrame(ledger).drop(columns=['last_mark'], errors='ignore').to_csv(DOCS / f'{PREFIX}-trades.csv', index=False, encoding='utf-8-sig')
    nav.to_csv(DOCS / f'{PREFIX}-nav.csv', index=False, encoding='utf-8-sig')
    print(json.dumps(clean(report), ensure_ascii=False), flush=True)


def enhance():
    """Add strictly dated raw features to a separate cache; no outcome labels.

    The original CACHE remains byte-for-byte unchanged. Incremental sources are
    four indices, complete market funding closes, PIT industry peers, and dated
    limit-up theme links to sector funding. Coverage reports stop at June.
    """
    import re

    def digest(path):
        h = hashlib.sha256()
        with path.open('rb') as fd:
            for block in iter(lambda: fd.read(1024 * 1024), b''):
                h.update(block)
        return h.hexdigest()

    def shift(a, n):
        out = np.full_like(a, np.nan, dtype=np.float32)
        out[n:] = a[:-n]
        return out

    def roll(a, n, kind='mean'):
        return getattr(pd.DataFrame(a).rolling(n, min_periods=n), kind)().to_numpy(np.float32)

    def compound(a, n):
        # a is percentage daily return; windows must contain n valid observations.
        with np.errstate(divide='ignore', invalid='ignore'):
            logs = np.log1p(np.where(a > -100, a, np.nan) / 100)
        return np.expm1(roll(logs, n, 'sum')) * 100

    def norm_date(value):
        s = str(value or '').replace('-', '')
        return s if re.fullmatch(r'\d{8}', s) else None

    def code6(value):
        s = str(value or '').split('.')[0].strip().zfill(6)
        return s if re.fullmatch(r'\d{6}', s) else None

    def coverage(a, dates, cutoff='20260630'):
        select = np.array(dates) <= cutoff
        z = a[select]
        valid = np.isfinite(z)
        byday = valid if z.ndim == 1 else valid.any(axis=1)
        ds = np.array(dates)[select]
        return {'finite_observations': int(valid.sum()), 'finite_days': int(byday.sum()),
                'first': str(ds[byday][0]) if byday.any() else None,
                'last': str(ds[byday][-1]) if byday.any() else None}

    def add_market_flow(conn, data, summary):
        dates = data['dates']; pos = {d: i for i, d in enumerate(dates)}
        n = len(dates)
        raw = {k: np.full(n, np.nan, np.float64) for k in ['main', 'super', 'small', 'amount']}
        excluded = collections.Counter()
        rows = conn.execute('SELECT trade_date,times,series,amount,last,pct,complete FROM market_fflow WHERE REPLACE(trade_date,\'-\',\'\') BETWEEN ? AND ? ORDER BY trade_date', (dates[0], dates[-1]))
        for day, times_s, series_s, amount, last_s, pct_s, complete in rows:
            d = norm_date(day)
            if d not in pos:
                excluded['non_calendar_date'] += 1; continue
            try:
                times, series, last, pct = map(json.loads, [times_s, series_s, last_s, pct_s])
                if complete != 1 or not times or times[-1] != '15:00':
                    excluded['incomplete_close'] += 1; continue
                if not np.isfinite(amount) or amount <= 0:
                    excluded['invalid_amount'] += 1; continue
                keys = ['main', 'super', 'big', 'mid', 'small']
                vals = {k: float(last[k]) for k in keys}
                if not all(np.isfinite(list(vals.values()))):
                    excluded['invalid_last'] += 1; continue
                if any(not isinstance(series[k], list) or len(series[k]) != len(times) or not np.isclose(float(series[k][-1]), vals[k], rtol=1e-6, atol=1) for k in keys):
                    excluded['series_last_mismatch'] += 1; continue
                if any(pct.get(k) is None or not np.isclose(float(pct[k]), vals[k] / amount * 100, rtol=.001, atol=.02) for k in keys):
                    excluded['pct_amount_mismatch'] += 1; continue
                if not np.isclose(vals['main'], vals['super'] + vals['big'], rtol=.001, atol=10):
                    excluded['main_components_mismatch'] += 1; continue
                i = pos[d]
                for k in ['main', 'super', 'small']:
                    raw[k][i] = vals[k]
                raw['amount'][i] = amount
            except (TypeError, ValueError, KeyError, IndexError):
                excluded['parse_failure'] += 1
        for k in ['main', 'super', 'small']:
            data['market'][f'fflow_{k}1'] = (raw[k] / raw['amount'] * 100).astype(np.float32)
            data['market'][f'fflow_{k}5'] = (roll(raw[k][:, None], 5, 'sum')[:, 0] / roll(raw['amount'][:, None], 5, 'sum')[:, 0] * 100).astype(np.float32)
        summary['market_fflow'] = {'excluded': dict(excluded),
            'source_note': 'Historical close snapshots are daily-only; amount may be inferred from rounded provider main-net percentages.',
            'date_note': 'Only exact stored trading dates present in the cache are used; no forward/backward date substitution.'}

    def add_indices(conn, data, summary):
        dates = data['dates']
        names = ['上证指数', '深证成指', '创业板指', '科创50']
        rows = pd.read_sql_query('SELECT date,name,close,change FROM index_daily WHERE REPLACE(date,\'-\',\'\') BETWEEN ? AND ?', conn, params=(dates[0], dates[-1]))
        rows['date'] = rows.date.map(norm_date)
        rows = rows[rows.name.isin(names)]
        dup = rows.duplicated(['date', 'name'], keep=False)
        excluded_dup = int(dup.sum())
        rows = rows[~dup]
        close = rows.pivot(index='date', columns='name', values='close').reindex(index=dates, columns=names).to_numpy(np.float32)
        change = rows.pivot(index='date', columns='name', values='change').reindex(index=dates, columns=names).to_numpy(np.float32)
        ok = (close > 0) & np.isfinite(change) & (np.abs((close / shift(close, 1) - 1) * 100 - change) <= .15)
        complete = ok.all(axis=1)
        upcount = np.where(complete, (change > 0).sum(axis=1), np.nan).astype(np.float32)
        h20 = roll(ok.astype(np.float32), 20, 'sum') == 20
        ma20 = roll(close, 20)
        trendcount = np.where(h20.all(axis=1), (close > ma20).sum(axis=1), np.nan).astype(np.float32)
        h5 = roll(ok.astype(np.float32), 5, 'sum') == 5
        ret5 = (close / shift(close, 5) - 1) * 100
        ret5[~h5] = np.nan
        growth = (ret5[:, 2] + ret5[:, 3]) / 2 - ret5[:, 0]
        growth[~h5.all(axis=1)] = np.nan
        data['market'].update({'index_up4': upcount, 'index_trend20_up4': trendcount,
                               'growth_relative5': growth.astype(np.float32),
                               'index_mean5': np.where(h5.all(axis=1), ret5.mean(axis=1), np.nan).astype(np.float32)})
        summary['four_indices'] = {'names': names, 'duplicate_rows_excluded': excluded_dup,
                                  'return_reference_tolerance_pp': .15,
                                  'rule': 'All four same-date records and complete historical windows are required.'}

    def add_industry(conn, data, summary):
        dates, codes = data['dates'], data['codes']
        ds = np.array(dates); cmap = {c: j for j, c in enumerate(codes)}
        rows = conn.execute("SELECT m.index_code,m.ts_code,m.in_date,m.out_date FROM sw_industry_member m JOIN sw_industry i ON i.index_code=m.index_code WHERE i.level='L1'").fetchall()
        groups = collections.defaultdict(list); skipped = collections.Counter()
        for group, code, start, end in rows:
            start = norm_date(start)
            end = norm_date(end) if end else '99999999'
            if start is None or end is None or start >= end:
                skipped['invalid_interval'] += 1; continue
            groups[group].append((code, start, end))
        shape = data['a']['close'].shape
        active_groups = np.zeros(shape, np.uint8)
        membership_by_group = {}
        denominators = {}
        for group, members in groups.items():
            members = list(set(members))
            active = np.zeros(shape, bool); all_n = np.zeros(len(dates), np.int32)
            for code, start, end in members:
                on = (ds >= start) & (ds < end)
                all_n += on
                j = cmap.get(code)
                if j is not None:
                    active[:, j] |= on
            active_groups += active
            membership_by_group[group] = active
            denominators[group] = all_n
        keys = ['industry_ret5', 'industry_ret20', 'industry_breadth', 'rs_industry20', 'industry_other_count']
        arrays = {k: np.full(shape, np.nan, np.float32) for k in keys}
        daily = data['a']['change']; valid = data['valid'] & np.isfinite(daily)
        coverage_daily = []
        for group, active in membership_by_group.items():
            good = active & valid
            count = good.sum(axis=1)
            total = np.sum(np.where(good, daily, 0), axis=1, dtype=np.float64)
            up = np.sum(good & (daily > 0), axis=1)
            all_n = denominators[group]
            group_ok = (count >= 10) & (count / np.maximum(all_n, 1) >= .8)
            # Benchmark removes the candidate itself on every past day when it belongs.
            other_count = count[:, None] - good
            other_sum = total[:, None] - np.where(good, daily, 0)
            benchmark = (other_sum / np.maximum(other_count, 1)).astype(np.float32)
            benchmark[~group_ok] = np.nan
            benchmark[other_count < 9] = np.nan
            ret5, ret20 = compound(benchmark, 5), compound(benchmark, 20)
            breadth = ((up[:, None] - (good & (daily > 0))) / np.maximum(other_count, 1)).astype(np.float32)
            breadth[~group_ok] = np.nan
            current = active & (active_groups == 1)
            for k, v in [('industry_ret5', ret5), ('industry_ret20', ret20),
                         ('industry_breadth', breadth), ('rs_industry20', data['f']['ret20'] - ret20),
                         ('industry_other_count', np.where(group_ok[:, None], other_count, np.nan))]:
                arrays[k][current] = v[current]
            cutoff = ds <= '20260630'
            coverage_daily.append({'group': group, 'usable_days_preholdout': int((group_ok & cutoff).sum()),
                                   'mean_members_preholdout': float(np.mean(count[cutoff]))})
        data['f'].update(arrays)
        summary['industry'] = {'method': 'Point-in-time L1 membership; daily equal-weight peer returns excluding the candidate; compounded over complete 5/20-session windows.',
                               'inclusion': 'in_date <= day < out_date; empty out_date means active. At least 10 valid members and >=80% coverage of all dated members, including members absent from cached stocks.',
                               'official_daily_table': 'Empty, so these are reconstructed peer benchmarks rather than official SW index returns.',
                               'excluded_intervals': dict(skipped), 'ambiguous_membership_observations_preholdout': int(((active_groups > 1) & (ds <= '20260630')[:, None]).sum()),
                               'groups': coverage_daily}

    def add_sector(conn, data, summary):
        dates, codes = data['dates'], data['codes']
        cmap = {c[:6]: j for j, c in enumerate(codes)}
        rows = pd.read_sql_query("SELECT trade_date,sector_type,sector_code,name,close,change_pct,main_net_pct,source FROM sector_fund_flow WHERE trade_date BETWEEN ? AND ? AND source IN ('eastmoney-push2','eastmoney-daykline')", conn, params=(dates[0], dates[-1]))
        rows['trade_date'] = rows.trade_date.map(norm_date)
        rows['key'] = rows.sector_type + ':' + rows.sector_code
        dup = rows.duplicated(['trade_date', 'key'], keep=False)
        duplicate_count = int(dup.sum()); rows = rows[~dup & rows.trade_date.isin(dates)]
        entity_keys = sorted(rows.key.unique())
        def matrix(col):
            return rows.pivot(index='trade_date', columns='key', values=col).reindex(index=dates, columns=entity_keys).to_numpy(np.float32)
        close, change, flow1 = map(matrix, ['close', 'change_pct', 'main_net_pct'])
        ref = np.abs((close / shift(close, 1) - 1) * 100 - change) <= .15
        ok = (close > 0) & np.isfinite(change) & ref
        ret5 = (close / shift(close, 5) - 1) * 100
        ret5[~(roll(ok.astype(np.float32), 5, 'sum') == 5)] = np.nan
        flow5 = roll(flow1, 5)  # Arithmetic mean of reported daily percentage, not cumulative money/turnover.
        epos = {k: i for i, k in enumerate(entity_keys)}
        named = collections.defaultdict(lambda: collections.defaultdict(set))
        for r in rows.itertuples():
            named[r.trade_date][str(r.name).strip()].add(epos[r.key])
        lu = conn.execute("SELECT REPLACE(date,'-',''),code,sector FROM limitup WHERE REPLACE(date,'-','') BETWEEN ? AND ? AND sector IS NOT NULL", (dates[0], dates[-1])).fetchall()
        perday = collections.defaultdict(set)
        for day, code, theme in lu:
            theme = str(theme).strip(); code = code6(code)
            if theme not in ('', '其他', '公告') and code in cmap:
                perday[day].add((cmap[code], theme))
        keys = ['sector_main1_max', 'sector_main1_mean', 'sector_main5_max', 'sector_main5_mean',
                'sector_ret5_max', 'sector_match_count', 'sector_positive_fraction']
        arrays = {k: np.full(data['a']['close'].shape, np.nan, np.float32) for k in keys}
        last_seen = collections.defaultdict(dict); conflicts = 0; matches = collections.Counter()
        for i, date in enumerate(dates):
            for j, theme in sorted(perday.get(date, ())):
                last_seen[j][theme] = i
            mapping = named.get(date, {})
            for j, themes in last_seen.items():
                entities = set()
                for theme, seen in themes.items():
                    if i - seen > 20:
                        continue
                    es = mapping.get(theme, ())
                    if len(es) == 1:
                        entities.update(es)
                        if date <= '20260630': matches[theme] += 1
                    elif len(es) > 1:
                        if date <= '20260630': conflicts += 1
                if not entities:
                    continue
                es = sorted(entities)
                arrays['sector_match_count'][i, j] = len(es)
                for values, maxkey, meankey in [(flow1[i, es], 'sector_main1_max', 'sector_main1_mean'),
                                              (flow5[i, es], 'sector_main5_max', 'sector_main5_mean')]:
                    finite = values[np.isfinite(values)]
                    if len(finite):
                        arrays[maxkey][i, j] = finite.max(); arrays[meankey][i, j] = finite.mean()
                v = ret5[i, es]; v = v[np.isfinite(v)]
                if len(v): arrays['sector_ret5_max'][i, j] = v.max()
                v = flow1[i, es]; v = v[np.isfinite(v)]
                if len(v): arrays['sector_positive_fraction'][i, j] = np.mean(v > 0)
        data['f'].update(arrays)
        summary['dated_sector_link'] = {'link': 'Only exact trimmed theme names observed for the stock on/before the signal and no more than 20 sessions old; requires a unique same-date sector entity. No current constituents or fuzzy aliases.',
                                        'source': 'Existing sector_fund_flow with compatible Eastmoney source tags.',
                                        'flow5_definition': 'Mean of five consecutive daily main_net_pct observations, not cumulative main-money share.',
                                        'duplicate_rows_excluded': duplicate_count, 'ambiguous_name_links_preholdout': conflicts,
                                        'matched_theme_counts_preholdout': dict(matches)}

    input_path = CACHE
    output_path = Path('/tmp/tradeiq-t5-20261008-enhanced-features.pkl')
    audit_path = DOCS / f'{PREFIX}-feature-audit.json'
    assert input_path.resolve() != output_path.resolve(), 'Original cache must not be overwritten.'
    original_hash = digest(input_path)
    with input_path.open('rb') as fd:
        data = pickle.load(fd)
    old_f, old_m = set(data['f']), set(data['market'])
    conn = open_db()
    summary = {'input': str(input_path), 'output': str(output_path), 'original_sha256': original_hash,
               'query_only': conn.execute('PRAGMA query_only').fetchone()[0],
               'holds_original_features': True, 'outcomes_accessed': False,
               'coverage_report_cutoff': '20260630'}
    try:
        for func in [add_market_flow, add_indices, add_industry, add_sector]:
            func(conn, data, summary)
    finally:
        conn.rollback()
        conn.close()
    data['f']['industry_rs20'] = data['f']['rs_industry20']
    data['f']['sector_main_pct5'] = data['f']['sector_main5_mean']
    data['market']['market_main5_pct'] = data['market']['fflow_main5']
    data['market']['index_up_count'] = data['market']['index_up4']
    summary['aliases'] = {'industry_rs20': 'rs_industry20',
                          'sector_main_pct5': 'sector_main5_mean',
                          'market_main5_pct': 'fflow_main5',
                          'index_up_count': 'index_up4'}
    summary['new_features'] = {k: coverage(v, data['dates']) for k, v in data['f'].items() if k not in old_f}
    summary['new_market_features'] = {k: coverage(v, data['dates']) for k, v in data['market'].items() if k not in old_m}
    assert digest(input_path) == original_hash, 'Original cache changed during enhancement.'
    summary['original_cache_unchanged'] = True
    data['metadata']['enhancement'] = summary
    with output_path.open('wb') as fd:
        pickle.dump(data, fd, protocol=pickle.HIGHEST_PROTOCOL)
    write_json(audit_path, summary)
    print(json.dumps({'output': str(output_path), 'audit': str(audit_path),
                      'feature_count': len(summary['new_features']),
                      'market_feature_count': len(summary['new_market_features'])}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('stage', choices=['prepare', 'enhance', 'search', 'refine', 'advanced', 'freeze', 'evaluate'])
    args = parser.parse_args()
    globals()[args.stage]()
