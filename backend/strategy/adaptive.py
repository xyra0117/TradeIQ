"""第 3 轮：市场状态自适应策略。

核心洞察（来自前两轮失败的根因分析）：
  - 反向动量在【熊市/震荡】有效（市场 -24% 时超额 +44%）
  - 反向动量在【牛市】必然大幅跑输（市场 +62% 时超额 -53%）
  - 所以不能用单一逻辑，必须按市场状态切换

市场状态判定（只用历史数据，无未来函数）：
  - 用全市场等权指数的 20 日累计涨幅 / 60 日累计涨幅
  - 强牛：20日涨幅 > 8%  → 改用「强势跟随」逻辑（追强势）
  - 震荡：-8% ~ 8%      → 用「反向动量」逻辑
  - 弱市：< -8%         → 用「超跌反弹」逻辑（更激进的反向）

每种市场状态用各自历史上表现最好的因子组合，并分别验证。
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

# ---- 市场状态（严格只用 <= j 的数据）----
def _pct_change(s, w, size=None):
    """等价于 s[t]/s[t-w]-1，输出长度 size（默认与 s 等长），前 w 个为 NaN。"""
    size = size if size is not None else len(s)
    out = np.full(size, np.nan)
    m = min(len(s), size)
    if m > w:
        out[w:m] = s[w:m] / s[: m - w] - 1
    return out


mkt20 = _pct_change(mkt_eq, 20, n_d)
mkt60 = _pct_change(mkt_eq, 60, n_d)
# 状态标签：0=弱市 1=震荡 2=强牛
STATE = np.zeros(n_d, dtype=int)
STATE[(mkt20 >= -0.08) & (mkt20 <= 0.08)] = 1
STATE[mkt20 > 0.08] = 2

print("=== 市场状态分布 ===")
for s, nm in [(0, "弱市(20日跌>8%)"), (1, "震荡"), (2, "强牛(20日涨>8%)")]:
    days = [dates[j] for j in DAYS if STATE[j] == s]
    print(f"  {nm:<22} {len(days):>4} 天" + (f"  例: {days[0]} ~ {days[-1]}" if days else ""))

# ---- 因子 rank ----
FR = {}
RAW = {
    "r5": -_rank(fs.r5), "r10": -_rank(fs.r10), "r20": -_rank(fs.r20),
    "r60": -_rank(fs.r60), "vol": _rank(fs.vol20),
    "closepos": _rank(fs.close_pos), "dist_high": _rank(fs.dist_high20),
    "pos": -_rank(fs.pos20), "body": _rank(fs.body),
    "r5_fwd": _rank(fs.r5), "r20_fwd": _rank(fs.r20),   # 正向动量（牛市用）
    "slope_fwd": _rank(fs.ma20_slope), "vr_fwd": _rank(fs.vr),
}
for k, v in RAW.items():
    FR[k] = [v[:, j] for j in DAYS]

SEGS = [(60, 130), (130, 200), (200, 270), (270, n_d - 6)]


def build(w, amt, tn, j0, j1, state_mode="all"):
    """state_mode: all / adaptive / 0 / 1 / 2"""
    picks = {}
    for j in range(j0, j1):
        di = j - J0
        if state_mode == "adaptive":
            st = STATE[j]
        elif state_mode == "all":
            st = None
        else:
            st = int(state_mode)
        if st is not None and st != STATE[j]:
            continue
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st
        ok &= fs.amt_yuan[:, j] > amt
        idx = np.where(ok)[0]
        if len(idx) < tn:
            continue
        s = np.zeros(len(idx))
        for k, wk in w.items():
            if wk:
                s += wk * np.nan_to_num(FR[k][di][idx], nan=0.0)
        picks[j] = list(idx[np.argpartition(-s, tn - 1)[:tn]])
    return picks


def bt(w, amt, tn, j0, j1, state_mode="all"):
    picks = build(w, amt, tn, j0, j1, state_mode)
    if not picks:
        return None
    return simulate(dates, C, O, H, L, tradable, picks, hold=HOLD,
                    top_n=tn, lp=lp)


def seg_eval(w, amt, tn, state_mode="all"):
    excs, anns, ns = [], [], []
    for j0, j1 in SEGS:
        r = bt(w, amt, tn, j0, j1, state_mode)
        if r is None:
            continue
        mm = metrics(r["equity"], dates, r["trades"])
        b = (mkt_eq[j1 - 1] / mkt_eq[j0]) ** (243 / (j1 - j0)) - 1
        excs.append(mm["年化"] - b * 100)
        anns.append(mm["年化"])
        ns.append(mm["交易次数"])
    return excs, anns, ns


# ---- 先看单一逻辑在各状态的表现 ----
print("\n=== 单一逻辑 vs 市场状态（诊断）===")
TESTS = [
    ("纯反向(r20+r60)", {"r20": 1.0, "r60": 1.0}),
    ("纯顺势(r5+r20正向)", {"r5_fwd": 1.0, "r20_fwd": 1.0, "slope_fwd": 0.5}),
    ("反向+顺势混合", {"r20": 0.6, "r60": 0.6, "r5_fwd": 0.5, "slope_fwd": 0.5}),
]
for nm, w in TESTS:
    excs, anns, ns = seg_eval(w, 3e8, 5, "all")
    print(f"  {nm:<22} " + " ".join(f"{a:>6.1f}" for a in anns)
          + f"  | 正超额 {sum(1 for e in excs if e>0)}/4  均值{np.mean(excs):+.1f}")

print("\n=== 分状态独立搜索（在对应状态日里找最优因子）===")
best_per_state = {}
for st, stname in [(0, "弱市"), (1, "震荡"), (2, "强牛")]:
    rng = np.random.default_rng(100 + st)
    cands = []
    KEYS = list(RAW)
    for it in range(150):
        w = {k: float(rng.choice([0.0, 0.5, 1.0, 1.5])) for k in KEYS}
        if sum(w.values()) < 1.0:
            continue
        amt = float(rng.choice([3e8, 5e8, 1e9]))
        tn = int(rng.choice([3, 5, 8]))
        excs, anns, ns = seg_eval(w, amt, tn, str(st))
        if not excs:
            continue
        cands.append((np.mean(excs), np.mean(anns), w, amt, tn, excs, anns))
    cands.sort(key=lambda x: -x[0])
    if cands:
        best_per_state[st] = cands[0]
        print(f"  {stname}: 最优 均超额{cands[0][0]:+.1f}% 均年化{cands[0][1]:+.1f}% "
              f"topN={cands[0][4]} 各段{[f'{a:.0f}' for a in cands[0][5]]}")
        nz = " ".join(f"{k}={v:g}" for k, v in cands[0][2].items() if v)
        print(f"      {nz}")

# ---- 组合成自适应策略 ----
if len(best_per_state) == 3:
    print("\n=== 自适应策略（按市场状态切换）===")
    class Adaptive:
        def __init__(self, cfg):
            self.cfg = cfg

        def build(self, j0, j1):
            picks = {}
            for st, (amt, tn, w) in self.cfg.items():
                for j in range(j0, j1):
                    if STATE[j] != st:
                        continue
                    di = j - J0
                    ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st
                    ok &= fs.amt_yuan[:, j] > amt
                    idx = np.where(ok)[0]
                    if len(idx) < tn:
                        continue
                    s = np.zeros(len(idx))
                    for k, wk in w.items():
                        if wk:
                            s += wk * np.nan_to_num(FR[k][di][idx], nan=0.0)
                    picks.setdefault(j, []).extend(
                        list(idx[np.argpartition(-s, tn - 1)[:tn]]))
            return picks

        def bt(self, j0, j1):
            picks = self.build(j0, j1)
            if not picks:
                return None
            return simulate(dates, C, O, H, L, tradable, picks, hold=HOLD,
                            top_n=20, lp=lp)

    cfg = {st: (b[3], b[4], b[2]) for st, b in best_per_state.items()}
    ad = Adaptive(cfg)
    excs, anns = [], []
    print(f"{'区间':<24} {'市场':>8} {'策略':>8} {'超额':>9} {'回撤':>8} {'夏普':>6}")
    for j0, j1 in SEGS:
        r = ad.bt(j0, j1)
        mm = metrics(r["equity"], dates, r["trades"])
        b = (mkt_eq[j1 - 1] / mkt_eq[j0]) ** (243 / (j1 - j0)) - 1
        e = mm["年化"] - b * 100
        excs.append(e)
        print(f"{dates[j0]}~{dates[j1-1]:<10} {b*100:>7.2f}% {mm['年化']:>7.2f}% "
              f"{e:>8.2f}% {mm['最大回撤']:>7.2f}% {mm['夏普']:>6.2f}")
    print(f"  → {sum(1 for e in excs if e>0)}/4 段正超额，平均 {np.mean(excs):+.2f}%")

    r = ad.bt(J0, n_d - 6)
    mm = metrics(r["equity"], dates, r["trades"])
    print(f"\n  全样本：年化{mm['年化']:.2f}% 回撤{mm['最大回撤']:.2f}% "
          f"夏普{mm['夏普']:.2f} 胜率{mm['胜率']:.1f}% 交易{mm['交易次数']}笔")

    json.dump({str(k): {"amt": v[0], "tn": v[1],
                        "w": {kk: vv for kk, vv in v[2].items()}}
               for k, v in cfg.items()},
              open("_adaptive.json", "w"), ensure_ascii=False, indent=1)
    print("  已保存 _adaptive.json")
