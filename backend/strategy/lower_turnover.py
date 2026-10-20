"""最终尝试：降低换手率。

前面的诊断给出了明确结论：
  T+5 每日全仓换手 → 年换手 243 倍 → 年成本 87% → 数学上不可能盈利

所以要验证的是：**把换手降下来后，反向动量 alpha 能否覆盖成本？**

两条路线：
  A. 延长持有期（T+10 / T+15 / T+20），每日只开一部分仓
  B. 降低调仓频率（每 N 天换一次，其余时间持有）

同时检验「信号强度门槛」：只在信号特别极端时才建仓，其余时间空仓观望。
空仓也是一种策略——在无 alpha 时不亏钱。
"""

import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from engine import LIMIT_PCT, load_cache, metrics, simulate  # noqa: E402
from factors import FactorSet, _rank  # noqa: E402

d = load_cache("_cache.npz")
C, O, H, L, V, A = d["C"], d["O"], d["H"], d["L"], d["V"], d["A"]
dates, meta_mkt, meta_stt = d["dates"], d["meta_mkt"], d["meta_stt"]
n_c, n_d = C.shape

fs = FactorSet(C, O, H, L, V, A)
not_st = ~np.isin(meta_stt, ["st", "star", "delisting"])
fs.set_universe(np.ones(n_c, bool), not_st)
lp = np.array([LIMIT_PCT.get(m, 0.10) for m in meta_mkt])
tradable = np.isfinite(C) & np.isfinite(O) & (C > 0) & (O > 0)
mkt_ret = np.nan_to_num(np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0))
mkt_eq = np.cumprod(1 + mkt_ret)
J0 = 60
DAYS = list(range(J0, n_d))

# 反向动量分数
SCORE = {}
SCORE["r60"] = [_rank(-fs.r60[:, j]) for j in DAYS]
SCORE["r20"] = [_rank(-fs.r20[:, j]) for j in DAYS]
SCORE["r10"] = [_rank(-fs.r10[:, j]) for j in DAYS]
SCORE["vol"] = [_rank(fs.vol20[:, j]) for j in DAYS]
SCORE["closepos"] = [_rank(fs.close_pos[:, j]) for j in DAYS]

W_MIX = {"r60": 1.0, "r20": 0.8, "r10": 0.4, "vol": 0.3}


def build(tn, amt, j0, j1, rebal=1):
    """rebal: 每 rebal 天换一次仓。"""
    picks = {}
    for j in range(j0, j1, rebal):
        di = j - J0
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st & (fs.amt_yuan[:, j] > amt)
        idx = np.where(ok)[0]
        if len(idx) < tn:
            continue
        s = np.zeros(len(idx))
        for k, w in W_MIX.items():
            s += w * np.nan_to_num(SCORE[k][di][idx], nan=0.0)
        picks[j] = list(idx[np.argpartition(-s, tn - 1)[:tn]])
    return picks


def bt(tn, amt, j0, j1, rebal=1, hold=5):
    picks = build(tn, amt, j0, j1, rebal)
    if not picks:
        return None
    return simulate(dates, C, O, H, L, tradable, picks, hold=hold, top_n=tn, lp=lp)


print("=" * 76)
print("降低换手率实验")
print("=" * 76)

print("\n【实验1】调仓频率 + 持有期组合（top10, 成交额>3亿）")
print(f"{'调仓间隔':>8} {'持有期':>6} {'年化':>8} {'回撤':>8} {'夏普':>6} {'交易数':>7} {'换手估计':>9}")
print("-" * 66)
rows = []
for rebal in [1, 3, 5, 10, 20]:
    for hold in [5, 10, 20]:
        r = bt(10, 3e8, J0, n_d - 6, rebal=rebal, hold=hold)
        m = metrics(r["equity"], dates, r["trades"])
        # 换手估计：每年调仓次数 * 持仓数 / 持仓期
        days = n_d - 6 - J0
        rebal_per_year = 243 / rebal
        turnover = rebal_per_year * (10 / hold)
        rows.append((rebal, hold, m["年化"], m["最大回撤"], m["夏普"],
                     m["交易次数"], turnover))
        print(f"{rebal:>8} {hold:>6} {m['年化']:>7.2f}% {m['最大回撤']:>7.2f}% "
              f"{m['夏普']:>6.2f} {m['交易次数']:>7} {turnover:>8.0f}x")

best_row = max(rows, key=lambda x: x[2])
print(f"\n  最优: 调仓间隔={best_row[0]} 持有期={best_row[1]} 年化={best_row[2]:.2f}%")

print("\n【实验2】持股数 x 调仓间隔（持有期=10）")
print(f"{'topN':>5} {'调仓':>5} {'年化':>8} {'回撤':>8} {'夏普':>6}")
print("-" * 40)
for tn in [3, 5, 10, 20, 30]:
    for rebal in [5, 10]:
        r = bt(tn, 3e8, J0, n_d - 6, rebal=rebal, hold=10)
        m = metrics(r["equity"], dates, r["trades"])
        print(f"{tn:>5} {rebal:>5} {m['年化']:>7.2f}% {m['最大回撤']:>7.2f}% {m['夏普']:>6.2f}")

print("\n【实验3】信号门槛（只在极端信号时建仓，其余空仓）")
print(f"{'门槛分位':>9} {'建仓天数':>9} {'年化':>8} {'回撤':>8} {'夏普':>6} {'胜率':>6}")
print("-" * 50)
for thr in [90, 95, 97, 99]:
    picks = {}
    cnt = 0
    for j in range(J0, n_d - 6):
        di = j - J0
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st & (fs.amt_yuan[:, j] > 3e8)
        idx = np.where(ok)[0]
        if len(idx) < 10:
            continue
        s = np.zeros(len(idx))
        for k, w in W_MIX.items():
            s += w * np.nan_to_num(SCORE[k][di][idx], nan=0.0)
        s = (s - s.mean()) / (s.std() + 1e-9)
        # 只在有股票超过阈值分位时才建仓
        if (s > np.percentile(s, 100 - thr)).sum() >= 10:
            top = idx[np.argpartition(-s, 9)[:10]]
            picks[j] = list(top)
            cnt += 1
    r = simulate(dates, C, O, H, L, tradable, picks, hold=5, top_n=10, lp=lp)
    m = metrics(r["equity"], dates, r["trades"])
    print(f"{thr:>9} {cnt:>9} {m['年化']:>7.2f}% {m['最大回撤']:>7.2f}% "
          f"{m['夏普']:>6.2f} {m['胜率']:>5.1f}%")

print("\n【实验4】最优配置的分段样本外")
rebal, hold = best_row[0], best_row[1]
tn = 10
print(f"配置: topN={tn} 调仓间隔={rebal} 持有期={hold} 成交额>3e8")
print(f"{'区间':<24} {'市场':>8} {'策略':>8} {'超额':>9} {'回撤':>8} {'夏普':>6}")
print("-" * 70)
SEGS = [(60, 130), (130, 200), (200, 270), (270, n_d - 6)]
excs = []
for j0, j1 in SEGS:
    r = bt(tn, 3e8, j0, j1, rebal=rebal, hold=hold)
    m = metrics(r["equity"], dates, r["trades"])
    b = (mkt_eq[j1 - 1] / mkt_eq[j0]) ** (243 / (j1 - j0)) - 1
    e = m["年化"] - b * 100
    excs.append(e)
    print(f"{dates[j0]}~{dates[j1-1]:<10} {b*100:>7.2f}% {m['年化']:>7.2f}% "
          f"{e:>8.2f}% {m['最大回撤']:>7.2f}% {m['夏普']:>6.2f}")
print(f"  → {sum(1 for e in excs if e>0)}/4 段正超额，平均 {np.mean(excs):+.2f}%")
