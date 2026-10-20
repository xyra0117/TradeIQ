"""校准 fast_eval2 与完整 simulate 的一致性。

只有当两者在同一参数下给出接近的年化/回撤，才放心用 fast_eval2 搜索。
"""

import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from engine import LIMIT_PCT, load_cache, metrics, simulate  # noqa: E402
from factors import FactorSet, _rank  # noqa: E402
from fast_eval2 import backtest_fast, metrics_fast  # noqa: E402

HOLD = 5
d = load_cache("_cache.npz")
C, O, H, L, V, A = d["C"], d["O"], d["H"], d["L"], d["V"], d["A"]
dates, meta_mkt, meta_stt = d["dates"], d["meta_mkt"], d["meta_stt"]
n_c, n_d = C.shape

fs = FactorSet(C, O, H, L, V, A)
not_st = ~np.isin(meta_stt, ["st", "star", "delisting"])
fs.set_universe(np.ones(n_c, bool), not_st)
lp = np.array([LIMIT_PCT.get(m, 0.10) for m in meta_mkt])
tradable = np.isfinite(C) & np.isfinite(O) & (C > 0) & (O > 0)

fwd = np.full((n_c, n_d), np.nan)
fwd[:, : n_d - HOLD - 1] = O[:, HOLD + 1 :] / O[:, 1 : n_d - HOLD] - 1
prevC = np.full_like(C, np.nan)
prevC[:, 1:] = C[:, :-1]
open_lu = (O >= prevC * (1 + lp[:, None]) - 0.005) & np.isfinite(O) & np.isfinite(prevC)

SIGN = {"r5": -1, "r20": -1, "r60": -1, "vol": +1, "slope": -1,
        "pos": -1, "closepos": +1, "body": +1, "dist_high": +1, "r10": -1}
RAW = {"r5": fs.r5, "r20": fs.r20, "r60": fs.r60, "vol": fs.vol20,
       "slope": fs.ma20_slope, "pos": fs.pos20, "closepos": fs.close_pos,
       "body": fs.body, "dist_high": fs.dist_high20, "r10": fs.r10}

TEST_PARAMS = [
    ({"r5": 0.8, "r20": 0.8, "r60": 0.4, "vol": 1.3, "slope": 0.8, "pos": 1.3,
      "closepos": 0.4, "dist_high": 0.4, "vr_bell": 0.8}, 5e8, 0.45, 0, 5),
    ({"r5": 0.4, "r20": 1.3, "vol": 1.3, "pos": 0.8, "closepos": 0.4,
      "dist_high": 1.3, "vr_bell": 0.8}, 1e8, 0.0, 0, 5),
    ({"r60": 0.0, "r20": 0.0, "r5": 0.0, "vol": 0.0, "slope": 0.0, "pos": 0.0,
      "closepos": 0.0, "body": 0.0, "dist_high": 0.0, "r10": 0.0},
     1e8, 0.0, 0, 10),  # 全零权重 = 随机基准，用来验证两者是否都接近市场
]

SPLIT = int(n_d * 0.6)
J0 = 60
DAYS = list(range(J0, n_d))
RANK = {k: [_rank(SIGN[k] * RAW[k][:, j]) for j in DAYS] for k in SIGN}
VRB = []
for j in DAYS:
    vr = np.nan_to_num(fs.vr[:, j], nan=1.0)
    VRB.append(_rank(-np.abs(np.log(np.clip(vr, 0.1, 20) / 1.3))))


def build(w, min_amt, vlo, vhi, top_n, j0, j1):
    picks = {}
    for j in range(j0, j1):
        di = j - J0
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st
        ok &= fs.amt_yuan[:, j] > min_amt
        vv = np.nan_to_num(fs.vol20[:, j], nan=9)
        if vlo:
            ok &= vv >= vlo
        if vhi:
            ok &= vv < vhi
        idx = np.where(ok)[0]
        if len(idx) < top_n:
            continue
        s = np.zeros(len(idx))
        for k, wk in w.items():
            if k == "vr_bell" or not wk:
                continue
            s += wk * np.nan_to_num(RANK[k][di][idx], nan=0.0)
        if w.get("vr_bell"):
            s += w["vr_bell"] * np.nan_to_num(VRB[di][idx], nan=0.0)
        picks[j] = list(idx[np.argpartition(-s, top_n - 1)[:top_n]])
    return picks


print(f"{'#':<3} {'完整引擎年化':>12} {'fast2年化':>11} {'差异':>9} {'完整回撤':>10} {'fast2回撤':>10}")
print("-" * 62)
for i, (w, amt, vlo, vhi, tn) in enumerate(TEST_PARAMS, 1):
    j0, j1 = SPLIT, n_d - 6
    picks = build(w, amt, vlo, vhi, tn, j0, j1)
    r_full = simulate(dates, C, O, H, L, tradable, picks, hold=HOLD,
                      top_n=tn, lp=lp)
    m_full = metrics(r_full["equity"], dates, r_full["trades"])

    eq2, _ = backtest_fast(picks, C, O, open_lu, fwd, j0, j1, hold=HOLD)
    m2 = metrics_fast(eq2)

    print(f"{i:<3} {m_full['年化']:>12.2f} {m2['ann']:>11.2f} "
          f"{m2['ann']-m_full['年化']:>+9.2f} {m_full['最大回撤']:>10.2f} {m2['mdd']:>10.2f}")

print("\n注：fast2 是近似（假设各批持仓等权、无资金约束细节），")
print("    目标是【趋势一致、量级接近】，最终结论以完整引擎为准。")
