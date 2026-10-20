"""大规模参数搜索（基于快速评估器）+ 严格的样本外验证。

搜索目标：训练期【超越同口径基准】的年化超额，而不是绝对收益。
这避免选出"只是在牛市里买得好的"策略。
"""

import json
import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from fast_eval import (  # noqa: E402
    C, O, n_c, n_d, dates, fs, not_st, eval_scores, bench_stats, fwd,
)
from factors import _rank  # noqa: E402

SPLIT = int(n_d * 0.6)
J0 = 60
J1_TRAIN = SPLIT
J1_TEST = n_d - 6

BENCH_TRAIN = bench_stats(J0, J1_TRAIN)
BENCH_TEST = bench_stats(SPLIT, J1_TEST)
print(f"训练期基准年化 {BENCH_TRAIN:.2f}%  |  样本外基准年化 {BENCH_TEST:.2f}%")

# 预计算各因子矩阵（避免重复计算 rank）
CACHE = {
    "r5": fs.r5, "r20": fs.r20, "r60": fs.r60,
    "vol": fs.vol20, "slope": fs.ma20_slope, "pos": fs.pos20,
    "vr": fs.vr, "closepos": fs.close_pos, "body": fs.body,
    "dist_high": fs.dist_high20, "r10": fs.r10,
}
# 方向已在诊断中确定：+1 = 原方向有效，-1 = 反向有效
SIGN = {
    "r5": -1, "r20": -1, "r60": -1, "vol": +1, "slope": -1, "pos": -1,
    "vr": 0, "closepos": +1, "body": +1, "dist_high": +1, "r10": -1,
}

# 预先算好每个因子的 rank 矩阵
print("预计算 rank 矩阵 ...")
RANK = {}
for k, arr in CACHE.items():
    if SIGN[k] == 0:
        continue
    R = np.full((n_c, n_d), np.nan)
    for j in range(J0, n_d):
        R[:, j] = _rank(SIGN[k] * arr[:, j])
    RANK[k] = R
    print(f"  {k} done")

# 量比的钟形分数也预先算好
vr_score = np.full((n_c, n_d), np.nan)
for j in range(J0, n_d):
    vr = np.nan_to_num(fs.vr[:, j], nan=1.0)
    vr_score[:, j] = _rank(-np.abs(np.log(np.clip(vr, 0.1, 20) / 1.3)))
print("  vr_bell done")

BASE_OK = np.isfinite(C) & (C > 0)
amt = fs.amt_yuan
vol = fs.vol20

FACTORS = ["r5", "r20", "r60", "vol", "slope", "pos", "closepos", "body", "dist_high", "r10"]
WEIGHTS = [0.0, 0.3, 0.6, 1.0, 1.5]
MIN_AMT = [5e7, 1e8, 2e8, 5e8]
VOL_LO = [0.0, 0.20, 0.30, 0.40, 0.50]
VOL_HI = [0.0, 0.8, 1.2, 2.0]
TOP_NS = [5, 8, 10, 15, 20]

rng = np.random.default_rng(2024)
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
best = []
print(f"\n随机搜索 {N} 组参数 ...")

for it in range(N):
    w = {k: float(rng.choice(WEIGHTS)) for k in FACTORS}
    w["vr_bell"] = float(rng.choice([0.0, 0.3, 0.6, 1.0]))
    min_amt = float(rng.choice(MIN_AMT))
    vlo = float(rng.choice(VOL_LO))
    vhi = float(rng.choice(VOL_HI))
    if vhi and vhi <= vlo:
        continue
    tn = int(rng.choice(TOP_NS))

    ok = BASE_OK & not_st[:, None] & (amt > min_amt)
    if vlo:
        ok &= np.nan_to_num(vol, nan=0) >= vlo
    if vhi:
        ok &= np.nan_to_num(vol, nan=9) < vhi

    total_w = sum(w.values())
    if total_w <= 0:
        continue

    S = np.zeros((n_c, n_d))
    for k in FACTORS:
        if w[k]:
            S += w[k] * np.nan_to_num(RANK[k], nan=0)
    if w["vr_bell"]:
        S += w["vr_bell"] * np.nan_to_num(vr_score, nan=0)
    S[~ok] = np.nan

    r = eval_scores(S, tn, J0, J1_TRAIN)
    if r is None:
        continue
    exc = r["ann"] - BENCH_TRAIN
    best.append({
        "exc": exc, "ann": r["ann"], "mdd": r["mdd"], "sharpe": r["sharpe"],
        "win": r["win"], "top_n": tn, "min_amt": min_amt, "vlo": vlo, "vhi": vhi,
        "w": w,
    })
    if (it + 1) % 500 == 0:
        b = max(best, key=lambda x: x["exc"])
        print(f"  {it+1}/{N}  当前最优超额 {b['exc']:.2f}%")

best.sort(key=lambda x: -x["exc"])
print(f"\n=== 训练集 Top15（含样本外验证）===")
print(f"{'#':<3} {'超额':>7} {'年化':>7} {'回撤':>7} {'夏普':>6} {'topN':>5} {'样本外超额':>10} {'样本外年化':>10} {'样本外回撤':>10}")
print("-" * 90)


def rebuild(entry):
    """用参数字典重建分数矩阵并评估任意区间。"""
    w = entry["w"]
    ok = BASE_OK & not_st[:, None] & (amt > entry["min_amt"])
    if entry["vlo"]:
        ok &= np.nan_to_num(vol, nan=0) >= entry["vlo"]
    if entry["vhi"]:
        ok &= np.nan_to_num(vol, nan=9) < entry["vhi"]
    S = np.zeros((n_c, n_d))
    for k in FACTORS:
        if w[k]:
            S += w[k] * np.nan_to_num(RANK[k], nan=0)
    if w["vr_bell"]:
        S += w["vr_bell"] * np.nan_to_num(vr_score, nan=0)
    S[~ok] = np.nan
    return S


top15 = []
for i, e in enumerate(best[:15], 1):
    S = rebuild(e)
    rt = eval_scores(S, e["top_n"], SPLIT, J1_TEST)
    if rt is None:
        continue
    o_exc = rt["ann"] - BENCH_TEST
    print(f"{i:<3} {e['exc']:>7.2f} {e['ann']:>7.2f} {e['mdd']:>7.2f} {e['sharpe']:>6.2f} "
          f"{e['top_n']:>5} {o_exc:>10.2f} {rt['ann']:>10.2f} {rt['mdd']:>10.2f}")
    e = dict(e)
    e["o_exc"] = o_exc
    e["o_ann"] = rt["ann"]
    e["o_mdd"] = rt["mdd"]
    e["o_sharpe"] = rt["sharpe"]
    top15.append(e)

json.dump(top15, open("_search_fast.json", "w"), ensure_ascii=False, indent=1)
print("\n已保存 _search_fast.json")
print(f"\n=== 样本外仍有超额的正向结果数: {sum(1 for e in top15 if e['o_exc']>0)}/{len(top15)} ===")
