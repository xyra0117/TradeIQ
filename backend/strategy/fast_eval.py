"""快速组合评估器：向量化计算因子组合的 T+5 收益，用于大规模搜索。

与 engine.simulate 的差异：
  - 不逐日循环、不模拟资金账本，而是直接算「等权买入 N 只 -> 持有 5 天」的净收益
  - 近似成本 = 双边 0.16% + 滑点 0.2%（与 simulate 一致的量级）
  - 用于快速筛参；最终候选再用完整 simulate 复核

用途：把搜索从分钟级压到秒级，从而能跑上万组参数。
"""

import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from engine import LIMIT_PCT, load_cache  # noqa: E402
from factors import FactorSet, _rank  # noqa: E402

HOLD = 5
COST_ROUNDTRIP = 0.0016 + 0.002  # 手续费+印花税+过户费 + 滑点

d = load_cache("_cache.npz")
C, O, H, L, V, A = d["C"], d["O"], d["H"], d["L"], d["V"], d["A"]
dates, meta_mkt, meta_stt = d["dates"], d["meta_mkt"], d["meta_stt"]
n_c, n_d = C.shape

fs = FactorSet(C, O, H, L, V, A)
not_st = ~np.isin(meta_stt, ["st", "star", "delisting"])
lp = np.array([LIMIT_PCT.get(m, 0.10) for m in meta_mkt])

# 未来 T+5 收益：j+1 开盘买，j+1+HOLD 开盘卖
fwd = np.full((n_c, n_d), np.nan)
fwd[:, : n_d - HOLD - 1] = O[:, HOLD + 1 :] / O[:, 1 : n_d - HOLD] - 1

# 涨停买不进的标记（开盘涨停）
prevC = np.full_like(C, np.nan)
prevC[:, 1:] = C[:, :-1]
open_limit_up = (O >= prevC * (1 + lp[:, None]) - 0.005) & np.isfinite(O) & np.isfinite(prevC)

# 基准
mkt_ret = np.nan_to_num(np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0))
mkt_eq = np.cumprod(1 + mkt_ret)


def eval_scores(score_matrix, top_n, j0, j1, nonoverlap=True):
    """score_matrix: (n_c, n_d) 分數。返回绩效。"""
    step = HOLD if nonoverlap else 1
    rets = []
    for j in range(j0, j1, step):
        s = score_matrix[:, j]
        v = np.where(np.isfinite(s))[0]
        if len(v) < top_n:
            continue
        top = v[np.argpartition(-s[v], top_n - 1)[:top_n]]
        # 剔除开盘涨停（买不进）
        good = top[~open_limit_up[top, j]]
        if len(good) < max(3, top_n // 3):
            continue
        r = fwd[good, j]
        r = r[np.isfinite(r)]
        if len(r) == 0:
            continue
        rets.append(np.mean(r) - COST_ROUNDTRIP)

    if len(rets) < 5:
        return None
    rets = np.array(rets)
    eq = np.cumprod(1 + rets)
    n_per_year = 243 / step
    tot = eq[-1] - 1
    ann = (1 + tot) ** (n_per_year / len(rets)) - 1 if tot > -1 else -1
    peak = np.maximum.accumulate(eq)
    mdd = ((eq - peak) / peak).min()
    std = rets.std() * np.sqrt(n_per_year)
    sharpe = (rets.mean() * n_per_year) / std if std > 0 else 0
    return {
        "ann": ann * 100,
        "total": tot * 100,
        "mdd": mdd * 100,
        "sharpe": sharpe,
        "win": (rets > 0).mean() * 100,
        "n": len(rets),
    }


def bench_stats(j0, j1, step=HOLD):
    """同口径基准。"""
    rets = []
    for j in range(j0, j1, step):
        r = fwd[~open_limit_up[:, j], j]
        r = r[np.isfinite(r)]
        if len(r) == 0:
            continue
        rets.append(np.mean(r) - COST_ROUNDTRIP)
    rets = np.array(rets)
    eq = np.cumprod(1 + rets)
    n_per_year = 243 / step
    tot = eq[-1] - 1
    ann = (1 + tot) ** (n_per_year / len(rets)) - 1 if tot > -1 else -1
    return ann * 100
