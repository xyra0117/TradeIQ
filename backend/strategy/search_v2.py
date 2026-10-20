"""高性能参数搜索。

性能优化要点：
  1. 不再构造 (n_c, n_d) 全矩阵，而是按信号日 j 逐日切片计算，
     每天只处理【当日可交易池】（通常 1000-3000 只），而非全部 5387 只
  2. 因子 rank 矩阵预计算一次并常驻（已算好）
  3. 用 argpartition 代替 argsort 取 top_n

预期比全矩阵版本快 20-50 倍。
"""

import json
import sys
import time
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from engine import load_cache  # noqa: E402
from factors import FactorSet, _rank  # noqa: E402

HOLD = 5
COST = 0.0016 + 0.002  # 双边成本 + 滑点

d = load_cache("_cache.npz")
C, O, H, L, V, A = d["C"], d["O"], d["H"], d["L"], d["V"], d["A"]
dates, meta_mkt, meta_stt = d["dates"], d["meta_mkt"], d["meta_stt"]
n_c, n_d = C.shape

fs = FactorSet(C, O, H, L, V, A)
not_st = ~np.isin(meta_stt, ["st", "star", "delisting"])
lp = np.array([0.10 if m in ("主板", "") else (0.20 if m in ("创业板", "科创板") else 0.30)
               for m in meta_mkt])

fwd = np.full((n_c, n_d), np.nan)
fwd[:, : n_d - HOLD - 1] = O[:, HOLD + 1 :] / O[:, 1 : n_d - HOLD] - 1
prevC = np.full_like(C, np.nan)
prevC[:, 1:] = C[:, :-1]
open_lu = (O >= prevC * (1 + lp[:, None]) - 0.005) & np.isfinite(O) & np.isfinite(prevC)

SPLIT = int(n_d * 0.6)
J0 = 60
SIGN = {"r5": -1, "r20": -1, "r60": -1, "vol": +1, "slope": -1,
        "pos": -1, "closepos": +1, "body": +1, "dist_high": +1, "r10": -1}
RAW = {"r5": fs.r5, "r20": fs.r20, "r60": fs.r60, "vol": fs.vol20,
       "slope": fs.ma20_slope, "pos": fs.pos20, "closepos": fs.close_pos,
       "body": fs.body, "dist_high": fs.dist_high20, "r10": fs.r10}

print("预计算 rank 切片 ...")
t0 = time.time()
# 只在 J0 之后的列上算，且只存每日一维数组到 list（惰性访问）
DAYS = list(range(J0, n_d))
RANK_DAY = {k: [] for k in SIGN}
VRB_DAY = []
for j in DAYS:
    for k in SIGN:
        RANK_DAY[k].append(_rank(SIGN[k] * RAW[k][:, j]))
    vr = np.nan_to_num(fs.vr[:, j], nan=1.0)
    VRB_DAY.append(_rank(-np.abs(np.log(np.clip(vr, 0.1, 20) / 1.3))))
print(f"  {time.time()-t0:.1f}s")

# 每日可交易基础掩码
BASE = [np.isfinite(C[:, j]) & (C[:, j] > 0) for j in DAYS]
AMT = [fs.amt_yuan[:, j] for j in DAYS]
VOL = [np.nan_to_num(fs.vol20[:, j], nan=9) for j in DAYS]
NOTST = not_st

mkt_ret = np.nan_to_num(np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0))
mkt_eq = np.cumprod(1 + mkt_ret)


def bench_ann(j0, j1, step=HOLD):
    rets = []
    for j in range(j0, j1, step):
        r = fwd[~open_lu[:, j], j]
        r = r[np.isfinite(r)]
        if len(r):
            rets.append(np.mean(r) - COST)
    if not rets:
        return 0.0
    eq = np.cumprod(1 + np.array(rets))
    tot = eq[-1] - 1
    npy = 243 / step
    return ((1 + tot) ** (npy / len(rets)) - 1) * 100 if tot > -1 else -100.0


BENCH_TRAIN = bench_ann(J0, SPLIT)
BENCH_TEST = bench_ann(SPLIT, n_d - 6)
print(f"基准: 训练期 {BENCH_TRAIN:.2f}%  样本外 {BENCH_TEST:.2f}%")


def evaluate(w, min_amt, vlo, vhi, top_n, di0, di1, step=HOLD):
    """di 为 DAYS 索引区间。"""
    rets = []
    for di in range(di0, di1, step):
        j = DAYS[di]
        ok = BASE[di] & NOTST & (AMT[di] > min_amt)
        if vlo:
            ok &= VOL[di] >= vlo
        if vhi:
            ok &= VOL[di] < vhi
        idx = np.where(ok)[0]
        if len(idx) < top_n:
            continue
        s = np.zeros(len(idx))
        for k, wk in w.items():
            if k == "vr_bell" or not wk:
                continue
            s += wk * np.nan_to_num(RANK_DAY[k][di][idx], nan=0.0)
        if w.get("vr_bell"):
            s += w["vr_bell"] * np.nan_to_num(VRB_DAY[di][idx], nan=0.0)
        top = idx[np.argpartition(-s, top_n - 1)[:top_n]]
        top = top[~open_lu[top, j]]
        if len(top) < max(3, top_n // 3):
            continue
        r = fwd[top, j]
        r = r[np.isfinite(r)]
        if len(r):
            rets.append(np.mean(r) - COST)
    if len(rets) < 4:
        return None
    rets = np.array(rets)
    eq = np.cumprod(1 + rets)
    tot = eq[-1] - 1
    npy = 243 / step
    ann = ((1 + tot) ** (npy / len(rets)) - 1) * 100 if tot > -1 else -100.0
    peak = np.maximum.accumulate(eq)
    mdd = ((eq - peak) / peak).min() * 100
    std = rets.std() * np.sqrt(npy)
    sh = (rets.mean() * npy) / std if std > 0 else 0
    return {"ann": ann, "mdd": mdd, "sharpe": sh, "win": (rets > 0).mean() * 100,
            "n": len(rets)}


FACTORS = list(SIGN)
WEIGHTS = [0.0, 0.4, 0.8, 1.3]
DI_TRAIN0 = 0
DI_TRAIN1 = SPLIT - J0
DI_TEST0 = SPLIT - J0
DI_TEST1 = n_d - 6 - J0

N = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
rng = np.random.default_rng(11)
best = []
t0 = time.time()
for it in range(N):
    w = {k: float(rng.choice(WEIGHTS)) for k in FACTORS}
    w["vr_bell"] = float(rng.choice([0.0, 0.4, 0.8]))
    if sum(w.values()) == 0:
        continue
    min_amt = float(rng.choice([5e7, 1e8, 2e8, 5e8]))
    vlo = float(rng.choice([0.0, 0.2, 0.3, 0.45]))
    vhi = float(rng.choice([0.0, 0.8, 1.3, 9.9]))
    if vhi and vhi <= vlo:
        continue
    tn = int(rng.choice([5, 8, 10, 15, 20, 30]))

    r = evaluate(w, min_amt, vlo, vhi, tn, DI_TRAIN0, DI_TRAIN1)
    if r is None:
        continue
    best.append({"exc": r["ann"] - BENCH_TRAIN, "train": r, "top_n": tn,
                 "min_amt": min_amt, "vlo": vlo, "vhi": vhi, "w": w})
    if (it + 1) % 250 == 0:
        b = max(best, key=lambda x: x["exc"])
        el = time.time() - t0
        print(f"  {it+1}/{N}  最优超额{b['exc']:.2f}%  {el:.0f}s")

best.sort(key=lambda x: -x["exc"])
print(f"\n搜索完成 {len(best)} 组，耗时 {time.time()-t0:.0f}s\n")
print(f"{'#':<3} {'超额':>7} {'年化':>7} {'回撤':>7} {'夏普':>6} {'topN':>5} {'样本外超额':>10} {'样本外年化':>10} {'样本外回撤':>10} {'胜率':>6}")
print("-" * 95)
out = []
for i, e in enumerate(best[:20], 1):
    rt = evaluate(e["w"], e["min_amt"], e["vlo"], e["vhi"], e["top_n"],
                  DI_TEST0, DI_TEST1)
    if rt is None:
        continue
    o_exc = rt["ann"] - BENCH_TEST
    t = e["train"]
    print(f"{i:<3} {e['exc']:>7.2f} {t['ann']:>7.2f} {t['mdd']:>7.2f} {t['sharpe']:>6.2f} "
          f"{e['top_n']:>5} {o_exc:>10.2f} {rt['ann']:>10.2f} {rt['mdd']:>10.2f} {rt['win']:>6.1f}")
    e2 = dict(e)
    e2["test"] = rt
    e2["o_exc"] = o_exc
    out.append(e2)

json.dump(out, open("_search_v2.json", "w"), ensure_ascii=False, indent=1)
pos = sum(1 for e in out if e["o_exc"] > 0)
print(f"\n样本外正超额: {pos}/{len(out)}")
print("已保存 _search_v2.json")
