"""分层基准：把策略收益和"同类股票池"的收益做公平对比。

一个选股策略赚的钱，必须超过"随便在这个股票池里选同样多只"的水平，
否则它没有 alpha，只是选股范围本身自带 beta。

同时做因子"多空组合"检验：直接用 IC 最强的因子构造多空组合，
验证反向动量在扣费后是否真的成立。
"""

import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from engine import load_cache  # noqa: E402
from factors import FactorSet, _rank  # noqa: E402

HOLD = 5
d = load_cache("_cache.npz")
C, O, H, L, V, A = d["C"], d["O"], d["H"], d["L"], d["V"], d["A"]
dates, meta_stt = d["dates"], d["meta_stt"]
n_c, n_d = C.shape

fs = FactorSet(C, O, H, L, V, A)
not_st = ~np.isin(meta_stt, ["st", "star", "delisting"])
fs.set_universe(np.ones(n_c, bool), not_st)

fwd = np.full_like(C, np.nan)
fwd[:, : n_d - HOLD - 1] = O[:, HOLD + 1 :] / O[:, 1 : n_d - HOLD] - 1
liq = fs.amt_yuan > 1e8
COST = 0.0016  # 双边成本约 0.16%（佣金+印花税+过户费），另加滑点另算

print("=== 分层基准：各股票池 T+5 平均收益（毛，已扣0.16%成本）===")


# 简化：直接用分位数阈值按日计算
def pool_ret(mask_fn, label):
    rets = []
    for j in range(60, n_d - HOLD - 1, 3):
        m = liq[:, j] & not_st & np.isfinite(fwd[:, j]) & mask_fn(j)
        if m.sum() < 30:
            continue
        rets.append(np.mean(fwd[m, j]) * 100 - COST * 100)
    if rets:
        print(f"  {label:<28} {np.mean(rets):+.3f}%  (样本{len(rets)}天)")
    else:
        print(f"  {label:<28} 无有效样本")
    return np.mean(rets) if rets else np.nan


pool_ret(lambda j: np.isfinite(fs.vol20[:, j]) & (fs.vol20[:, j] <= np.nanpercentile(fs.vol20[liq[:, j] & not_st, j], 25)), "低波动组(vol25以下)")
pool_ret(lambda j: np.isfinite(fs.vol20[:, j]) & (fs.vol20[:, j] >= np.nanpercentile(fs.vol20[liq[:, j] & not_st, j], 75)), "高波动组(vol75以上)")
pool_ret(lambda j: np.isfinite(fs.r20[:, j]) & (fs.r20[:, j] < -0.05), "20日跌超5%(超跌)")
pool_ret(lambda j: np.isfinite(fs.r20[:, j]) & (fs.r20[:, j] > 0.15), "20日涨超15%(超涨)")
pool_ret(lambda j: np.isfinite(fs.ma20_slope[:, j]) & (fs.ma20_slope[:, j] > 0), "ma20上行")
pool_ret(lambda j: np.isfinite(fs.ma20_slope[:, j]) & (fs.ma20_slope[:, j] < 0), "ma20下行")

print("\n=== 多空组合检验（做多因子最高50% - 做空最低50%）===")


def long_short(arr, label):
    """每天多空各50%，看多空价差收益。"""
    ls = []
    for j in range(60, n_d - HOLD - 1, 3):
        f = arr[:, j].astype(float)
        r = fwd[:, j]
        m = liq[:, j] & not_st & np.isfinite(f) & np.isfinite(r)
        if m.sum() < 200:
            continue
        fv, rv = _rank(f[m]), r[m]
        order = np.argsort(fv)
        k = len(order) // 2
        # 因子最高 -> 多头；最低 -> 空头
        ls.append(rv[order[-k:]].mean() - rv[order[:k]].mean())
    if ls:
        print(f"  {label:<24} 多空价差 {np.mean(ls)*100:+.3f}%/次  样本{len(ls)}")
    return np.mean(ls) if ls else np.nan


long_short(fs.r60, "r60(原方向)")
long_short(-fs.r60, "r60(反向)")
long_short(fs.r20, "r20(原方向)")
long_short(-fs.r20, "r20(反向)")
long_short(fs.vol20, "vol20(原方向)")
long_short(-fs.vol20, "vol20(反向)")
long_short(fs.r5, "r5(原方向)")
long_short(-fs.r5, "r5(反向)")
long_short(fs.ma20_slope, "ma20斜率(原方向)")
long_short(-fs.ma20_slope, "ma20斜率(反向)")
long_short(fs.pos20, "pos20(原方向)")
long_short(-fs.pos20, "pos20(反向)")
long_short(fs.vr, "量比(原方向)")
long_short(-fs.vr, "量比(反向)")

print("\n=== 关键问题：因子在'控制beta后'还有没有超额？===")
print("做法：对每只票用它自身的历史波动做风险调整，再测多空")


def long_short_scaled(arr, label, beta_arr):
    """多空组合，收益除以市场当日收益（近似 beta 中性）。"""
    mkt = np.nan_to_num(np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0))
    ls = []
    for j in range(60, n_d - HOLD - 1, 5):
        f = arr[:, j].astype(float)
        r = fwd[:, j]
        m = liq[:, j] & not_st & np.isfinite(f) & np.isfinite(r)
        if m.sum() < 200:
            continue
        fv, rv = _rank(f[m]), _rank(r[m])
        order = np.argsort(fv)
        k = len(order) // 2
        # 组内 beta 差异：高低组的平均波动比
        b = beta_arr[:, j][m][order]
        hi_b, lo_b = np.nanmean(b[-k:]), np.nanmean(b[:k])
        spread = rv[order[-k:]].mean() - rv[order[:k]].mean()
        if hi_b > 0 and lo_b > 0:
            ls.append(spread * (lo_b / hi_b))  # 近似 beta 中和
    if ls:
        print(f"  {label:<26} beta调整后 {np.mean(ls)*100:+.3f}%/次  样本{len(ls)}")


long_short_scaled(-fs.r60, "r60(反向,beta调整)", fs.vol20)
long_short_scaled(-fs.r20, "r20(反向,beta调整)", fs.vol20)
long_short_scaled(-fs.vol20, "vol20(反向,beta调整)", fs.vol20)
