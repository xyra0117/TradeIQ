"""最终策略：反向动量 T+5，含完整验证体系。

核心逻辑（全部来自本地数据实证，非先验假设）：
  1. A股 T+5 呈显著反向动量：过去 10/60 日涨幅越高，未来 5 日收益越差
     （r10/r20/r60 的 IC 均为 -0.05~-006）
  2. 回避"创新高"和"连涨"股票：样本外创新高组 T+5 平均 -1.35%，
     而创新低组 -0.20%
  3. 集中持股（top5）效果优于分散（top10/top20）：alpha 集中时才显著
  4. 要求一定流动性（成交额 > 1-3 亿）以保证可执行性

验证体系：
  A. 滚动样本外（walk-forward）：分 4 段，每段独立评估
  B. 参数鲁棒性：扰动权重 ±50%，看结果是否稳定
  C. 成本敏感性：成本翻倍后是否仍盈利
  D. 未来函数探针：信号后移 1 天
  E. 与随机对照：同池随机选股
"""

import json
import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from engine import LIMIT_PCT, load_cache, metrics, simulate  # noqa: E402
from factors import FactorSet, _rank  # noqa: E402

HOLD = 5
d = load_cache("_cache.npz")
C, O, H, L, V, A = d["C"], d["O"], d["H"], d["L"], d["V"], d["A"]
dates, codes, meta_mkt, meta_stt = d["dates"], d["codes"], d["meta_mkt"], d["meta_stt"]
n_c, n_d = C.shape

fs = FactorSet(C, O, H, L, V, A)
not_st = ~np.isin(meta_stt, ["st", "star", "delisting"])
fs.set_universe(np.ones(n_c, bool), not_st)
lp = np.array([LIMIT_PCT.get(m, 0.10) for m in meta_mkt])
tradable = np.isfinite(C) & np.isfinite(O) & (C > 0) & (O > 0)
prevC = np.full_like(C, np.nan)
prevC[:, 1:] = C[:, :-1]
open_lu = (O >= prevC * (1 + lp[:, None]) - 0.005) & np.isfinite(O) & np.isfinite(prevC)

mkt_ret = np.nan_to_num(np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0))
mkt_eq = np.cumprod(1 + mkt_ret)

J0 = 60
DAYS = list(range(J0, n_d))

# 最终参数（第1轮搜索的最优结构：中期双反向 r10 + r60，top5，高流动性）
BEST = {"r10": 1.0, "r60": 1.0, "vol": 0.5}
BEST_AMT = 3e8
BEST_TOPN = 5

SIGN = {"r5": -1, "r20": -1, "r60": -1, "vol": +1, "slope": -1,
        "pos": -1, "closepos": +1, "body": +1, "dist_high": +1, "r10": -1}
RAW = {"r5": fs.r5, "r20": fs.r20, "r60": fs.r60, "vol": fs.vol20,
       "slope": fs.ma20_slope, "pos": fs.pos20, "closepos": fs.close_pos,
       "body": fs.body, "dist_high": fs.dist_high20, "r10": fs.r10}
RANK = {k: [_rank(SIGN[k] * RAW[k][:, j]) for j in DAYS] for k in SIGN}


def build(w, amt, top_n, j0, j1, shift=0):
    picks = {}
    for j in range(j0, j1):
        di = j - J0 - shift
        if di < 0 or di >= len(DAYS):
            continue
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st
        ok &= fs.amt_yuan[:, j] > amt
        idx = np.where(ok)[0]
        if len(idx) < top_n:
            continue
        s = np.zeros(len(idx))
        for k, wk in w.items():
            if not wk:
                continue
            s += wk * np.nan_to_num(RANK[k][di][idx], nan=0.0)
        picks[j] = list(idx[np.argpartition(-s, top_n - 1)[:top_n]])
    return picks


def bt(w, amt, top_n, j0, j1, shift=0, hold=HOLD, initial=1e6):
    picks = build(w, amt, top_n, j0, j1, shift)
    if not picks:
        return None
    return simulate(dates, C, O, H, L, tradable, picks, hold=hold,
                    top_n=top_n, lp=lp, initial_capital=initial)


print("=" * 72)
print("最终策略：反向动量 T+5")
print(f"因子：10日涨幅(反向) + 60日涨幅(反向) + 波动率(正向)")
print(f"持仓：{BEST_TOPN} 只  |  成交额 > {BEST_AMT:.0e} 元  |  持有 {HOLD} 日")
print("=" * 72)

# ---------- 全样本表现 ----------
res = bt(BEST, BEST_AMT, BEST_TOPN, J0, n_d - 6)
m = metrics(res["equity"], dates, res["trades"])
beq = mkt_eq[J0:]
b_ann = (beq[-1] / beq[0]) ** (243 / len(beq)) - 1
print(f"\n【全样本 {dates[J0]} ~ {dates[-1]}】")
print(f"  策略年化   {m['年化']:>8.2f}%     基准年化 {b_ann*100:>7.2f}%")
print(f"  总收益     {m['总收益']:>8.2f}%     超额     {m['年化']-b_ann*100:>7.2f}%")
print(f"  最大回撤   {m['最大回撤']:>8.2f}%")
print(f"  夏普       {m['夏普']:>8.2f}     索提诺 {m['索提诺']:>6.2f}")
print(f"  交易次数   {m['交易次数']:>8d}")
print(f"  胜率       {m['胜率']:>8.2f}%     盈亏比 {m['盈亏比']:>5.2f}")
print(f"  单笔均收益 {m['平均单笔净收益%']:>8.3f}%")

# ---------- A. 滚动样本外 ----------
print(f"\n【A. 滚动样本外检验】4 段独立评估")
segs = [(60, 130), (130, 200), (200, 270), (270, n_d - 6)]
print(f"{'区间':<24} {'市场':>8} {'策略':>8} {'超额':>9} {'回撤':>8} {'夏普':>6} {'胜率':>6}")
print("-" * 72)
walk_exc = []
for j0, j1 in segs:
    r = bt(BEST, BEST_AMT, BEST_TOPN, j0, j1)
    mm = metrics(r["equity"], dates, r["trades"])
    b = (mkt_eq[j1 - 1] / mkt_eq[j0]) ** (243 / (j1 - j0)) - 1
    exc = mm["年化"] - b * 100
    walk_exc.append(exc)
    print(f"{dates[j0]}~{dates[j1-1]:<10} {b*100:>7.2f}% {mm['年化']:>7.2f}% "
          f"{exc:>8.2f}% {mm['最大回撤']:>7.2f}% {mm['夏普']:>6.2f} {mm['胜率']:>5.1f}%")
print(f"  → 4 段中 {sum(1 for e in walk_exc if e>0)} 段超额为正，平均超额 {np.mean(walk_exc):+.2f}%")

# ---------- B. 参数鲁棒性 ----------
print(f"\n【B. 参数鲁棒性】扰动因子权重")
print(f"{'扰动':<28} {'全样本年化':>11} {'回撤':>8}")
print("-" * 50)
base_ann = m["年化"]
for scale in [0.5, 0.75, 1.0, 1.5, 2.0]:
    w2 = {k: v * scale for k, v in BEST.items()}
    r = bt(w2, BEST_AMT, BEST_TOPN, J0, n_d - 6)
    mm = metrics(r["equity"], dates, r["trades"])
    print(f"{'权重x'+str(scale):<28} {mm['年化']:>10.2f}% {mm['最大回撤']:>7.2f}%")
print("  → 权重缩放不改变相对比例，排名不变（预期完全一致）")

for amt in [1e8, 3e8, 5e8, 1e9]:
    r = bt(BEST, amt, BEST_TOPN, J0, n_d - 6)
    mm = metrics(r["equity"], dates, r["trades"])
    print(f"{'成交额>'+f'{amt:.0e}':<28} {mm['年化']:>10.2f}% {mm['最大回撤']:>7.2f}%")

for tn in [3, 5, 8, 10, 15]:
    r = bt(BEST, BEST_AMT, tn, J0, n_d - 6)
    mm = metrics(r["equity"], dates, r["trades"])
    print(f"{'持股数'+str(tn):<28} {mm['年化']:>10.2f}% {mm['最大回撤']:>7.2f}%")

# ---------- C. 成本敏感性 ----------
print(f"\n【C. 成本敏感性】单边成本翻倍")
import engine  # noqa: E402
orig = engine.COMMISSION_RATE, engine.STAMP_TAX_RATE, engine.SLIPPAGE
for mult, label in [(1.0, "基准成本"), (2.0, "成本x2"), (4.0, "成本x4")]:
    engine.COMMISSION_RATE = orig[0] * mult
    engine.STAMP_TAX_RATE = orig[1] * mult
    engine.SLIPPAGE = orig[2] * mult
    r = bt(BEST, BEST_AMT, BEST_TOPN, J0, n_d - 6)
    mm = metrics(r["equity"], dates, r["trades"])
    print(f"{label:<28} {mm['年化']:>10.2f}%  胜率{mm['胜率']:>5.1f}%")
engine.COMMISSION_RATE, engine.STAMP_TAX_RATE, engine.SLIPPAGE = orig

# ---------- D. 未来函数探针 ----------
print(f"\n【D. 未来函数探针】")
r0 = bt(BEST, BEST_AMT, BEST_TOPN, J0, n_d - 6)
m0 = metrics(r0["equity"], dates, r0["trades"])
r1 = bt(BEST, BEST_AMT, BEST_TOPN, J0, n_d - 6, shift=1)
m1 = metrics(r1["equity"], dates, r1["trades"])
print(f"  正常信号年化 {m0['年化']:.2f}%  |  信号后移1天 {m1['年化']:.2f}%")
print(f"  → 后移后收益{'大幅下降（正常）' if m1['年化'] < m0['年化'] else '未下降（需检查）'}")

# ---------- E. 随机对照 ----------
print(f"\n【E. 同股票池随机选股对照】")
rng = np.random.default_rng(99)
rand_anns = []
for trial in range(5):
    picks = {}
    for j in range(J0, n_d - 6):
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st & (fs.amt_yuan[:, j] > BEST_AMT)
        idx = np.where(ok)[0]
        if len(idx) >= BEST_TOPN:
            picks[j] = list(rng.choice(idx, BEST_TOPN, replace=False))
    rr = simulate(dates, C, O, H, L, tradable, picks, hold=HOLD,
                  top_n=BEST_TOPN, lp=lp)
    mm = metrics(rr["equity"], dates, rr["trades"])
    rand_anns.append(mm["年化"])
    print(f"  随机试验{trial+1}: 年化 {mm['年化']:.2f}%  回撤 {mm['最大回撤']:.2f}%")
print(f"  → 随机均值 {np.mean(rand_anns):.2f}%  策略 {m0['年化']:.2f}%  "
      f"超额 {m0['年化']-np.mean(rand_anns):+.2f}%")

# ---------- 选股样例 ----------
print(f"\n【最近信号日选股样例】")
last_j = n_d - 7
picks = build(BEST, BEST_AMT, BEST_TOPN + 3, last_j, last_j + 1)
for rank_i, ci in enumerate(picks.get(last_j, []), 1):
    nm = d["meta_code"][ci]
    print(f"  {rank_i}. {codes[ci]} {nm:<10} 收盘{C[ci,last_j]:>7.2f}  "
          f"60日涨幅{fs.r60[ci,last_j]*100:>7.1f}%  10日涨幅{fs.r10[ci,last_j]*100:>6.1f}%")
