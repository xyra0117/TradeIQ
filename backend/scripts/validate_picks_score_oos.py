# 样本外验证: 系统推荐「评分」重排到底能不能提高 T+1~T+5 命中率.
# 目的: 防止拿同一批数据既发现规律又验证规律. 时间切分 + walk-forward, 特征权重只在
# 训练段拟合, 测试段完全不参与. 评估指标 = 每日 TopK 的命中率 (实际用法是"看前 20 只").
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs'
SEED = 42
rng = np.random.default_rng(SEED)

df = pd.read_csv(OUT / 'picks-tn-outcomes-20260929.csv')
df = df[df.settled].copy()
df['y'] = df.winner.astype(float)
print(f'可统计样本 {len(df)} · {df.trade_date.nunique()} 个入选日 · '
      f'基线命中率 {df.y.mean() * 100:.2f}%', flush=True)

# ── 候选特征 (全部入选时可得, 无未来函数) ──
df['bull3'] = ((df.ma5 > df.ma10) & (df.ma10 > df.ma20)).astype(float)
df['bull4'] = ((df.ma5 > df.ma10) & (df.ma10 > df.ma20) & (df.ma20 > df.ma30)).astype(float)
df['sector_good'] = df.industry_name.isin(['电子', '有色金属', '轻工制造', '传媒']).astype(float)
df['sector_bad'] = df.industry_name.isin(
    ['交通运输', '国防军工', '房地产', '建筑材料', '钢铁']).astype(float)

FEATS = {
    '入选日涨停': df.limitup.astype(float),
    '入选日涨跌幅': df.change_pct0 / 10,
    '入选日涨跌幅^2': (df.change_pct0 / 10) ** 2,          # 捕捉 U 型
    'MA5陡峭>45°': (df.ma5_slope > 45).astype(float),
    '三线多头(非四线)': df.bull3 - df.bull4,
    '跌破MA60': (~df.above_ma60).astype(float),
    '股价20~100元': ((df.close0 >= 20) & (df.close0 < 100)).astype(float),
    '股价<20元': (df.close0 < 20).astype(float),
    '5日均额>=5亿': (df.amt5_yi >= 5).astype(float),
    '好行业': df.sector_good,
    '差行业': df.sector_bad,
    '入选前5日涨幅': df.prev5 / 10,
    '入选前20日涨幅': df.prev20 / 10,
    '当日主力强度': df.strength_today / 10,
    '超大单占比': df.super_pct / 5,
    '小单净额占比': df.small_pct / 5,
    '量比': df.vol_mult,
    '距20日高点': df.drawdown20 / 10,
}
names = list(FEATS)
X = pd.DataFrame(FEATS).to_numpy(float)
X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
X = (X - X.mean(0)) / (X.std(0) + 1e-9)
y = df.y.to_numpy()
days = df.trade_date.to_numpy()
uniq_days = np.array(sorted(set(days)))
print('特征:', ', '.join(names), flush=True)


# ── 极简逻辑回归 (L2 牛顿法) ──
def fit_logreg(Xa, ya, l2=1.0, iters=40):
    n, p = Xa.shape
    A = np.hstack([np.ones((n, 1)), Xa])
    w = np.zeros(p + 1)
    for _ in range(iters):
        z = np.clip(A @ w, -30, 30)
        mu = 1 / (1 + np.exp(-z))
        W = np.clip(mu * (1 - mu), 1e-6, None)
        g = A.T @ (ya - mu) - l2 * np.r_[0, w[1:]]
        H = (A * W[:, None]).T @ A + l2 * np.diag(np.r_[0, np.ones(p)])
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        w = w + step
        if np.abs(step).max() < 1e-8:
            break
    return w


def predict(Xa, w):
    A = np.hstack([np.ones((len(Xa), 1)), Xa])
    z = np.clip(A @ w, -30, 30)
    return 1 / (1 + np.exp(-z))


def topk_hit(score_by_day, y, day_arr, ks=(10, 20, 30, 50)):
    """每日按 score 降序取前 K, 返回 {K: 命中率}"""
    out = {}
    for k in ks:
        picks = []
        for d in uniq_days:
            m = np.where(day_arr == d)[0]
            top = m[np.argsort(-score_by_day[m], kind='stable')[:k]]
            picks.append(y[top])
        out[k] = float(np.concatenate(picks).mean() * 100)
    return out


# ── 1. 简单时间切分 (前 60% 训练 / 后 40% 测试) ──
cut = uniq_days[int(len(uniq_days) * 0.6)]
tr, te = days <= cut, days > cut
w = fit_logreg(X[tr], y[tr])
s_new = predict(X, w)
s_cur = df.score.to_numpy(float)
res = {'切分点': cut, '训练样本': int(tr.sum()), '测试样本': int(te.sum()),
       '测试段天数': int(len(set(days[te]))),
       '测试段基线%': round(y[te].mean() * 100, 2),
       '测试段当前评分Top20%': round(topk_hit(s_cur[te], y[te], days[te])[20], 2),
       '测试段新模型Top20%': round(topk_hit(s_new[te], y[te], days[te])[20], 2)}
print('\n[1] 简单时间切分', flush=True)
for k, v in res.items():
    print(f'  {k}: {v}', flush=True)

print('\n  训练段拟合的权重 (标准化后):', flush=True)
for nme, wt in sorted(zip(names, w[1:]), key=lambda t: -abs(t[1])):
    print(f'    {nme:18s} {wt:+.3f}', flush=True)

# ── 2. Walk-forward: 每 5 个交易日重训一次, 只看当天的前瞻表现 ──
print('\n[2] Walk-forward 逐日重训 (每 5 个交易日一次)', flush=True)
wf_new, wf_cur, wf_base = [], [], []
for i in range(15, len(uniq_days)):
    tr_days = set(uniq_days[max(0, i - 30):i])
    m_tr = np.isin(days, list(tr_days))
    m_te = days == uniq_days[i]
    if m_tr.sum() < 300 or m_te.sum() == 0:
        continue
    w = fit_logreg(X[m_tr], y[m_tr])
    p = predict(X[m_te], w)
    idx = np.where(m_te)[0]
    wf_new.append((idx, p))
    wf_cur.append((idx, s_cur[idx]))
    wf_base.append(y[idx])

ks = (10, 20, 30)
print(f'  {"K":>4} {"当前评分":>10} {"新模型":>10} {"随机基准":>10}', flush=True)
wf_tbl = {}
for k in ks:
    cn, nn, bn = [], [], []
    for i, (idx, p) in enumerate(wf_new):
        cn.append(y[idx[np.argsort(-s_cur[idx], kind='stable')[:k]]])
        nn.append(y[idx[np.argsort(-p, kind='stable')[:k]]])
        bn.append(np.random.default_rng(1000 + i).choice(
            y[idx], size=min(k, len(idx)), replace=False))
    c, n_, b = (np.concatenate(cn).mean() * 100, np.concatenate(nn).mean() * 100,
                np.concatenate(bn).mean() * 100)
    wf_tbl[k] = (c, n_, b)
    print(f'  {k:>4} {c:>9.2f}% {n_:>9.2f}% {b:>9.2f}%', flush=True)

# ── 3. Bootstrap: 每天重采样, 看 Top20 提升是否显著 ──
print('\n[3] Bootstrap (按入选日重采样 2000 次)', flush=True)
for k in (20, 30):
    dc, dn = [], []
    for idx, p in wf_new:
        dc.append(y[idx[np.argsort(-s_cur[idx], kind='stable')[:k]]].mean())
        dn.append(y[idx[np.argsort(-p, kind='stable')[:k]]].mean())
    dc, dn = np.array(dc), np.array(dn)
    diff = dc - dn
    bs = np.array([rng.choice(diff, len(diff), replace=True).mean() for _ in range(2000)])
    lo, hi = np.percentile(bs, [2.5, 97.5])
    print(f'  Top{k}: 当前评分 {dc.mean() * 100:.2f}% vs 新模型 {dn.mean() * 100:.2f}% · '
          f'差值均值 {(dn.mean() - dc.mean()) * 100:+.2f}pp · 95%CI [{lo * 100:+.2f}, {hi * 100:+.2f}]pp · '
          f'P(新模型更优)={(bs > 0).mean() * 100:.1f}%', flush=True)

# ── 4. 单特征边际命中率 (训练段/测试段各算一次, 看方向是否稳定) ──
print('\n[4] 单特征边际命中率: 训练段 vs 测试段 (方向一致性)', flush=True)
rows = []
for i, nme in enumerate(names):
    hi_ = df.loc[X[:, i] > 0, 'y'].mean() * 100
    lo_ = df.loc[X[:, i] <= 0, 'y'].mean() * 100
    a = X[tr, i] > 0
    b = X[te, i] > 0
    htr = y[tr][a].mean() * 100 if a.sum() > 20 else np.nan
    ltr = y[tr][~a].mean() * 100 if (~a).sum() > 20 else np.nan
    hte = y[te][b].mean() * 100 if b.sum() > 20 else np.nan
    lte = y[te][~b].mean() * 100 if (~b).sum() > 20 else np.nan
    rows.append({'特征': nme, '全体命中(有)': round(hi_, 2), '全体命中(无)': round(lo_, 2),
                 '全期差pp': round(hi_ - lo_, 2),
                 '训练段差pp': round(htr - ltr, 2) if np.isfinite(htr) and np.isfinite(ltr) else None,
                 '测试段差pp': round(hte - lte, 2) if np.isfinite(hte) and np.isfinite(lte) else None})
stab = pd.DataFrame(rows).sort_values('全期差pp', ascending=False)
print(stab.to_string(index=False), flush=True)
stab.to_csv(OUT / 'picks-tn-oos-feature-stability-20260929.csv', index=False,
            encoding='utf-8-sig')

pd.DataFrame([{'K': k, '当前评分%': round(v[0], 2), '新模型%': round(v[1], 2),
               '随机基准%': round(v[2], 2)} for k, v in wf_tbl.items()]
             ).to_csv(OUT / 'picks-tn-oos-topk-20260929.csv', index=False, encoding='utf-8-sig')
print('\nwritten docs/picks-tn-oos-*.csv', flush=True)
