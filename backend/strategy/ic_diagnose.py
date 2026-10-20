"""单因子 IC 诊断：用数据确认每个因子对 T+5 收益的真实方向。

IC = 因子值 与 未来 T+5 收益 的截面相关系数（Spearman 近似，用 rank 相关）。
- IC > 0：该因子值越高，未来收益越好
- IC < 0：反向使用

同时按因子分 5 组看收益单调性（quantile return），验证线性关系是否成立。
"""

import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from engine import load_cache  # noqa: E402
from factors import FactorSet, _rank, sma  # noqa: E402

HOLD = 5

d = load_cache("_cache.npz")
C, O, H, L, V, A = d["C"], d["O"], d["H"], d["L"], d["V"], d["A"]
dates = d["dates"]
meta_stt = d["meta_stt"]
n_c, n_d = C.shape

fs = FactorSet(C, O, H, L, V, A)
not_st = ~np.isin(meta_stt, ["st", "star", "delisting"])
fs.set_universe(np.ones(n_c, bool), not_st)

# ---- 未来 T+5 收益（从 j+1 开盘买入，j+1+HOLD 开盘卖出）----
fwd = np.full_like(C, np.nan)
fwd[:, : n_d - HOLD - 1] = O[:, HOLD + 1 :] / O[:, 1 : n_d - HOLD] - 1

print(f"{'因子':<16} {'IC均值':>8} {'IC标准差':>9} {'ICIR':>7} {'正IC占比':>9} {'有效天':>7}")
print("-" * 66)

# 基础可交易池：流动性 + 非 ST + 非停牌
liq = fs.amt_yuan > 1e8

factors = {
    "r5": fs.r5,
    "r10": fs.r10,
    "r20": fs.r20,
    "r60": fs.r60,
    "vr(量比)": fs.vr,
    "vol20(波动)": fs.vol20,
    "pos20(位置)": fs.pos20,
    "dist_high20": fs.dist_high20,
    "ma20_slope": fs.ma20_slope,
    "body(实体)": fs.body,
    "close_pos(收盘位)": fs.close_pos,
    "upper_shadow": fs.upper_shadow,
    "r20/vol(风险调整动量)": fs.r20 / np.maximum(fs.vol20, 0.1),
    "amt20/amt60(量能扩张)": fs.amt20 / np.maximum(sma(fs.amt_yuan, 60), 1),
    "up_streak(连涨)": fs.up_streak.astype(float),
}

results = {}
for name, arr in factors.items():
    ics = []
    for j in range(60, n_d - HOLD - 1):
        f = arr[:, j].astype(float)
        r = fwd[:, j]
        m = liq[:, j] & np.isfinite(f) & np.isfinite(r) & not_st
        if m.sum() < 100:
            continue
        fv, rv = f[m], r[m]
        if np.nanstd(fv) < 1e-12:
            continue
        ic = np.corrcoef(_rank(fv), _rank(rv))[0, 1]
        if np.isfinite(ic):
            ics.append(ic)
    if not ics:
        print(f"{name:<16}  {'无有效样本':>8}")
        continue
    ics = np.array(ics)
    icir = ics.mean() / (ics.std() + 1e-12)
    results[name] = ics
    print(
        f"{name:<16} {ics.mean():>8.4f} {ics.std():>9.4f} {icir:>7.2f} "
        f"{(ics>0).mean()*100:>8.1f}% {len(ics):>7d}"
    )

# ---- 分组收益单调性（只对最有效的几个因子）----
print("\n=== 分 5 组收益单调性（T+5 平均净收益，粗略扣 0.2% 成本）===")
COST = 0.002
key_factors = sorted(results.items(), key=lambda kv: -abs(kv[1].mean()))[:6]
for name, _ in key_factors:
    arr = factors[name]
    buckets = [[] for _ in range(5)]
    for j in range(60, n_d - HOLD - 1, 3):  # 抽样加速
        f = arr[:, j].astype(float)
        r = fwd[:, j]
        m = liq[:, j] & np.isfinite(f) & np.isfinite(r) & not_st
        if m.sum() < 200:
            continue
        fv, rv = _rank(f[m]), r[m]
        order = np.argsort(fv)
        for b in range(5):
            seg = order[b * len(order) // 5 : (b + 1) * len(order) // 5]
            buckets[b].append(rv[seg].mean() * 100 - COST * 100)
    means = [np.mean(b) if b else np.nan for b in buckets]
    mono = "↑单调" if all(means[i] <= means[i + 1] for i in range(4)) else (
        "↓单调" if all(means[i] >= means[i + 1] for i in range(4)) else "混乱")
    print(f"  {name:<20} " + "  ".join(f"{m:>6.2f}%" for m in means) + f"   {mono}")
