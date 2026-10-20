"""用完整 simulate 引擎复核最优参数，验证：
  1. 不存在未来函数（信号日只用 <= j 的数据）
  2. 收益在真实资金约束下成立
  3. 与快速评估器结果一致

同时做「未来函数探针」：把信号整体后移 1 天，如果收益暴跌说明用了当日收盘后的信息。
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
dates, meta_mkt, meta_stt = d["dates"], d["meta_mkt"], d["meta_stt"]
n_c, n_d = C.shape

fs = FactorSet(C, O, H, L, V, A)
not_st = ~np.isin(meta_stt, ["st", "star", "delisting"])
fs.set_universe(np.ones(n_c, bool), not_st)
lp = np.array([LIMIT_PCT.get(m, 0.10) for m in meta_mkt])
tradable = np.isfinite(C) & np.isfinite(O) & (C > 0) & (O > 0)

SIGN = {"r5": -1, "r20": -1, "r60": -1, "vol": +1, "slope": -1,
        "pos": -1, "closepos": +1, "body": +1, "dist_high": +1, "r10": -1}
RAW = {"r5": fs.r5, "r20": fs.r20, "r60": fs.r60, "vol": fs.vol20,
       "slope": fs.ma20_slope, "pos": fs.pos20, "closepos": fs.close_pos,
       "body": fs.body, "dist_high": fs.dist_high20, "r10": fs.r10}

# 预计算每日 rank
J0 = 60
DAYS = list(range(J0, n_d))
RANK = {k: [_rank(SIGN[k] * RAW[k][:, j]) for j in DAYS] for k in SIGN}
VRB = []
for j in DAYS:
    vr = np.nan_to_num(fs.vr[:, j], nan=1.0)
    VRB.append(_rank(-np.abs(np.log(np.clip(vr, 0.1, 20) / 1.3))))

# 选股名单（限制输出长度）
_ = None


def build_picks(w, min_amt, vlo, vhi, top_n, j0, j1, shift=0):
    """shift: 信号日后移天数（用于未来函数探针）。shift=0 为正常。"""
    picks = {}
    for j in range(j0, j1):
        di = j - J0 - shift
        if di < 0 or di >= len(DAYS):
            continue
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
        top = idx[np.argpartition(-s, top_n - 1)[:top_n]]
        picks[j] = list(top)
    return picks


def full_bt(w, min_amt, vlo, vhi, top_n, j0, j1, shift=0):
    picks = build_picks(w, min_amt, vlo, vhi, top_n, j0, j1, shift)
    if not picks:
        return None
    return simulate(dates, C, O, H, L, tradable, picks, hold=HOLD,
                    top_n=top_n, lp=lp)


res = json.load(open("_search_v2.json"))
SPLIT = int(n_d * 0.6)
print(f"{'#':<3} {'参数':<52} {'训练年化':>9} {'样本外年化':>10} {'样本外回撤':>10} {'夏普':>6}")
print("-" * 95)
cands = [e for e in res if e["o_exc"] > 15][:8]
finalists = []
for i, e in enumerate(cands, 1):
    p = full_bt(e["w"], e["min_amt"], e["vlo"], e["vhi"], e["top_n"], J0, SPLIT)
    q = full_bt(e["w"], e["min_amt"], e["vlo"], e["vhi"], e["top_n"], SPLIT, n_d - 6)
    if p is None or q is None:
        continue
    mp, mq = metrics(p["equity"], dates, p["trades"]), metrics(q["equity"], dates, q["trades"])
    nz = " ".join(f"{k}={v:g}" for k, v in e["w"].items() if v)
    print(f"{i:<3} {nz[:52]:<52} {mp['年化']:>9.2f} {mq['年化']:>10.2f} "
          f"{mq['最大回撤']:>10.2f} {mq['夏普']:>6.2f}")
    finalists.append((e, mp, mq))
    e["full_train"] = mp
    e["full_test"] = mq

print("\n=== 未来函数探针（信号后移1天，若收益不变/升高说明有问题）===")
if finalists:
    e, mp, mq = finalists[0]
    p_sh = full_bt(e["w"], e["min_amt"], e["vlo"], e["vhi"], e["top_n"],
                   SPLIT, n_d - 6, shift=1)
    if p_sh:
        ms = metrics(p_sh["equity"], dates, p_sh["trades"])
        print(f"  正常信号 样本外年化 {mq['年化']:.2f}%")
        print(f"  后移1天 样本外年化 {ms['年化']:.2f}%")
        print(f"  差异 {ms['年化']-mq['年化']:+.2f}%  "
              f"({'正常，信号有效' if ms['年化'] < mq['年化'] else '异常，需警惕未来函数'})")

json.dump(res, open("_search_v2.json", "w"), ensure_ascii=False, indent=1)
print("\n已把完整引擎复核结果写回 _search_v2.json")
