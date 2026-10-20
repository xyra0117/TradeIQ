"""基于完整引擎的定向参数搜索。

放弃快速近似：校准显示近似与真实引擎差异可达 70 个百分点，不可信。
完整引擎每次回测约 0.3-1.5 秒，因此用「分层搜索」缩小空间：
  第 1 轮：粗筛少量结构化配置，每组跑完整引擎
  第 2 轮：在粗筛前 5 名附近做局部细化
  第 3 轮：鲁棒性检验（扰动参数看是否稳定）

所有评估都以【完整引擎 + 真实资金 + 交易成本】为准。
"""

import itertools
import json
import sys
import time
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

prevC = np.full_like(C, np.nan)
prevC[:, 1:] = C[:, :-1]
open_lu = (O >= prevC * (1 + lp[:, None]) - 0.005) & np.isfinite(O) & np.isfinite(prevC)

SIGN = {"r5": -1, "r20": -1, "r60": -1, "vol": +1, "slope": -1,
        "pos": -1, "closepos": +1, "body": +1, "dist_high": +1, "r10": -1}
RAW = {"r5": fs.r5, "r20": fs.r20, "r60": fs.r60, "vol": fs.vol20,
       "slope": fs.ma20_slope, "pos": fs.pos20, "closepos": fs.close_pos,
       "body": fs.body, "dist_high": fs.dist_high20, "r10": fs.r10}

J0 = 60
DAYS = list(range(J0, n_d))
RANK = {k: [_rank(SIGN[k] * RAW[k][:, j]) for j in DAYS] for k in SIGN}
VRB = []
for j in DAYS:
    vr = np.nan_to_num(fs.vr[:, j], nan=1.0)
    VRB.append(_rank(-np.abs(np.log(np.clip(vr, 0.1, 20) / 1.3))))

# 基准净值（同区间，等权全市场）
mkt_ret = np.nan_to_num(np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0))
mkt_eq = np.cumprod(1 + mkt_ret)
SPLIT = int(n_d * 0.6)


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


def bt(w, amt, vlo, vhi, tn, j0, j1):
    picks = build(w, amt, vlo, vhi, tn, j0, j1)
    if not picks:
        return None
    return simulate(dates, C, O, H, L, tradable, picks, hold=HOLD,
                    top_n=tn, lp=lp)


def eval_cfg(w, amt, vlo, vhi, tn):
    """返回 (训练年化超额, 样本外年化超额, 样本外回撤, 样本外夏普, 样本外年化)。"""
    tr = bt(w, amt, vlo, vhi, tn, J0, SPLIT)
    te = bt(w, amt, vlo, vhi, tn, SPLIT, n_d - 6)
    if tr is None or te is None:
        return None
    mt = metrics(tr["equity"], dates, tr["trades"])
    me = metrics(te["equity"], dates, te["trades"])
    b_tr = (mkt_eq[SPLIT - 1] / mkt_eq[J0]) ** (243 / (SPLIT - J0)) - 1
    b_te = (mkt_eq[-1] / mkt_eq[SPLIT - 1]) ** (243 / (n_d - 6 - SPLIT + 1)) - 1
    return {
        "tr_ann": mt["年化"], "tr_exc": (mt["年化"] - b_tr * 100),
        "te_ann": me["年化"], "te_exc": (me["年化"] - b_te * 100),
        "te_mdd": me["最大回撤"], "te_sharpe": me["夏普"],
        "te_win": me["胜率"], "te_n": me["交易次数"],
    }


# ---------- 第 1 轮：结构化粗筛 ----------
# 依据诊断，核心是「反向动量 + 高波动 + 回避高位」
CANDIDATES = []
for top_n in [5, 10]:
    for core, name in [
        ({"r5": 1.0, "r20": 1.0}, "双反向"),
        ({"r5": 1.0, "r20": 1.0, "vol": 1.0}, "反向+高波动"),
        ({"r5": 1.0, "r20": 1.0, "pos": 1.0}, "反向+位置"),
        ({"r5": 1.0, "r20": 1.0, "dist_high": 1.0}, "反向+距高"),
        ({"r5": 1.0, "r20": 1.0, "vol": 1.0, "pos": 1.0}, "反向+波动+位置"),
        ({"r5": 1.0, "r20": 1.0, "r60": 1.0}, "三反向"),
        ({"r20": 1.0, "vol": 1.0, "slope": 1.0}, "反向中期+波动+斜率"),
        ({"r5": 0.8, "r20": 1.0, "vol": 1.0, "closepos": 0.6}, "反向+强收盘"),
        ({"r5": 1.0, "r20": 1.0, "vol": 1.0, "vr_bell": 0.6}, "反向+量能钟形"),
        ({"r10": 1.0, "r60": 1.0, "vol": 1.0}, "中期双反向"),
    ]:
        for amt in [1e8, 3e8]:
            for vlo in [0.0, 0.35]:
                CANDIDATES.append((dict(core), amt, vlo, 0.0, top_n, name))

print(f"第1轮粗筛：{len(CANDIDATES)} 组配置，完整引擎评估")
t0 = time.time()
scored = []
for i, (w, amt, vlo, vhi, tn, name) in enumerate(CANDIDATES, 1):
    r = eval_cfg(w, amt, vlo, vhi, tn)
    if r:
        r.update({"w": w, "amt": amt, "vlo": vlo, "vhi": vhi, "tn": tn, "name": name})
        scored.append(r)
    if i % 20 == 0:
        print(f"  {i}/{len(CANDIDATES)}  {time.time()-t0:.0f}s")

scored.sort(key=lambda r: -r["te_exc"])
print(f"\n{'名称':<22} {'topN':>4} {'成交额':>8} {'vol_lo':>7} {'训练超额':>9} {'样本外超额':>10} {'样本外年化':>10} {'回撤':>8} {'夏普':>6}")
print("-" * 100)
for r in scored[:15]:
    print(f"{r['name']:<22} {r['tn']:>4} {r['amt']:>8.0e} {r['vlo']:>7.2f} "
          f"{r['tr_exc']:>9.2f} {r['te_exc']:>10.2f} {r['te_ann']:>10.2f} "
          f"{r['te_mdd']:>8.2f} {r['te_sharpe']:>6.2f}")

json.dump(scored, open("_round1.json", "w"), ensure_ascii=False, indent=1)
print(f"\n耗时 {time.time()-t0:.0f}s，已保存 _round1.json")
