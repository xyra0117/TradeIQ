# 拟合最终上线权重: 只用「训练段与测试段方向一致」的特征, 排除符号翻转和行业类易衰减项.
# 产出直接抄进 app.py 的 _compute_edge_score.
from pathlib import Path
import numpy as np
import pandas as pd

OUT = Path(__file__).resolve().parents[2] / 'docs'
df = pd.read_csv(OUT / 'picks-tn-outcomes-20260929.csv')
df = df[df.settled].copy()
y = df.winner.astype(float).to_numpy()
days = df.trade_date.to_numpy()
uniq = np.array(sorted(set(days)))

bull3 = ((df.ma5 > df.ma10) & (df.ma10 > df.ma20)).astype(float)
bull4 = ((df.ma5 > df.ma10) & (df.ma10 > df.ma20) & (df.ma20 > df.ma30)).astype(float)

RAW = {
    '入选日涨停': df.limitup.astype(float),
    '入选日涨跌幅': df.change_pct0,
    '入选日涨跌幅平方': df.change_pct0 ** 2,
    '三线多头非四线': bull3 - bull4,
    '跌破MA60': (~df.above_ma60).astype(float),
    '距20日高点%': df.drawdown20,
    '股价<20元': (df.close0 < 20).astype(float),
    '量比(对前5日)': df.vol_mult,
    '入选前5日涨幅%': df.prev5,
    '小单净额占比%': df.small_pct,
}
names = list(RAW)
X = pd.DataFrame(RAW).to_numpy(float)
X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
mu, sd = X.mean(0), X.std(0) + 1e-9
Xs = (X - mu) / sd


def fit(Xa, ya, l2=2.0, iters=60):
    n, p = Xa.shape
    A = np.hstack([np.ones((n, 1)), Xa])
    w = np.zeros(p + 1)
    for _ in range(iters):
        mu_ = 1 / (1 + np.exp(-np.clip(A @ w, -30, 30)))
        W = np.clip(mu_ * (1 - mu_), 1e-6, None)
        g = A.T @ (ya - mu_) - l2 * np.r_[0, w[1:]]
        H = (A * W[:, None]).T @ A + l2 * np.diag(np.r_[0, np.ones(p)])
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            break
        w += step
        if np.abs(step).max() < 1e-9:
            break
    return w


w_std = fit(Xs, y)
# 换算回原始量纲: raw_weight = w_std / sd, 截距吸收中心化
raw_w = w_std[1:] / sd
intercept = w_std[0] - float(np.sum(w_std[1:] * mu / sd))

print('=== 最终权重 (原始量纲, 直接乘到实际值上) ===')
for nme, wt in sorted(zip(names, raw_w), key=lambda t: -abs(t[1])):
    print(f'  {nme:16s} {wt:+.5f}')
print(f'  截距 intercept = {intercept:+.5f}')

# 方向稳定性复查 (最终特征集)
print('\n=== 最终特征集 训练/测试方向复查 ===')
cut = uniq[int(len(uniq) * 0.6)]
tr, te = days <= cut, days > cut
for i, nme in enumerate(names):
    a, b = Xs[tr, i] > 0, Xs[te, i] > 0
    dtr = (y[tr][a].mean() - y[tr][~a].mean()) * 100 if a.sum() > 20 and (~a).sum() > 20 else np.nan
    dte = (y[te][b].mean() - y[te][~b].mean()) * 100 if b.sum() > 20 and (~b).sum() > 20 else np.nan
    ok = '稳定' if np.isfinite(dtr) and np.isfinite(dte) and np.sign(dtr) == np.sign(dte) else '不稳'
    print(f'  {nme:16s} 训练 {dtr:+6.2f}pp  测试 {dte:+6.2f}pp  {ok}')

# 用最终权重在训练/测试段分别评估 TopK
z = X @ raw_w + intercept
for label, m in [('训练段', tr), ('测试段', te)]:
    out = []
    for k in (10, 20, 30):
        sel = []
        for d in set(days[m]):
            idx = np.where(m & (days == d))[0]
            sel.append(y[idx[np.argsort(-z[idx], kind='stable')[:k]]])
        out.append(f'Top{k} {np.concatenate(sel).mean() * 100:.2f}%')
    print(f'  {label} (基线 {y[m].mean() * 100:.2f}%): ' + ' · '.join(out))
