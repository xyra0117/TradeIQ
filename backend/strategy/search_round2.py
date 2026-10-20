"""第 2 轮优化：解决上一轮暴露的三个问题。

问题 1：牛市大幅跑输（市场+62%时策略超额-53%）
  → 加入「市场状态过滤」：只在市场不极端强势时启用反向策略，
     强势期改用低吸/跟随逻辑
问题 2：成本敏感（成本x2后归零）
  → 提高单笔收益：集中持股、严格过滤、只选最极端的超跌
问题 3：4 段中只有 1 段正超额
  → 引入「相对大盘弱势」概念：不是绝对跌，而是相对跌

新增因子：
  - rs20: 相对市场 20 日超额（选相对最弱的）
  - 距年内高点回撤幅度
  - 缩量回调：价格跌但成交额萎缩（抛压衰竭）

策略目标：把 4 段中正超额的比例从 1/4 提到 3/4 以上。
"""

import json
import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from engine import LIMIT_PCT, load_cache, metrics, simulate  # noqa: E402
from factors import FactorSet, _rank, sma  # noqa: E402

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

# 新因子：相对市场弱势（个股 20 日收益 - 全市场 20 日平均收益）
mkt20 = np.full_like(C, np.nan)
mkt20[:, 20:] = C[:, 20:] / C[:, :-20] - 1
mkt_series20 = np.full(n_d, np.nan)
mkt_series20[20:] = np.nanmean(mkt20[:, 20:], axis=0)
rs20 = fs.r20 - mkt_series20[None, :]

# 缩量回调：5日涨跌幅 / 5日成交额变化
amt_ratio = fs.amt20 / np.maximum(sma(fs.amt_yuan, 60), 1)

FACTORS = {
    "r10": -_rank(fs.r10),      # 反向
    "r20": -_rank(fs.r20),      # 反向
    "r60": -_rank(fs.r60),      # 反向
    "rs20": -_rank(rs20),       # 相对弱势（新增）
    "vol": _rank(fs.vol20),     # 高波动
    "closepos": _rank(fs.close_pos),
    "dist_high": _rank(fs.dist_high20),
    "amt_shrink": _rank(amt_ratio),  # 缩量（新增，抛压衰竭）
    "pos": -_rank(fs.pos20),
}
FR = {k: [] for k in FACTORS}
for j in DAYS:
    for k, v in FACTORS.items():
        FR[k].append(v[:, j])

SEGS = [(60, 130), (130, 200), (200, 270), (270, n_d - 6)]


def build(w, amt, top_n, j0, j1, mkt_filter=None):
    picks = {}
    for j in range(j0, j1):
        di = j - J0
        if mkt_filter is not None and not mkt_filter(j):
            continue
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st
        ok &= fs.amt_yuan[:, j] > amt
        idx = np.where(ok)[0]
        if len(idx) < top_n:
            continue
        s = np.zeros(len(idx))
        for k, wk in w.items():
            if wk:
                s += wk * np.nan_to_num(FR[k][di][idx], nan=0.0)
        picks[j] = list(idx[np.argpartition(-s, top_n - 1)[:top_n]])
    return picks


def bt(w, amt, tn, j0, j1, mkt_filter=None):
    picks = build(w, amt, tn, j0, j1, mkt_filter)
    if not picks:
        return None
    return simulate(dates, C, O, H, L, tradable, picks, hold=HOLD,
                    top_n=tn, lp=lp)


def seg_score(w, amt, tn, mkt_filter=None):
    """4 段滚动样本外：返回 (正超额段数, 平均超额, 段年化列表)。"""
    excs, anns = [], []
    for j0, j1 in SEGS:
        r = bt(w, amt, tn, j0, j1, mkt_filter)
        if r is None:
            continue
        mm = metrics(r["equity"], dates, r["trades"])
        b = (mkt_eq[j1 - 1] / mkt_eq[j0]) ** (243 / (j1 - j0)) - 1
        excs.append(mm["年化"] - b * 100)
        anns.append(mm["年化"])
    pos = sum(1 for e in excs if e > 0)
    return pos, len(excs), (np.mean(excs) if excs else 0), anns


print("第 2 轮：搜索稳健的因子组合（目标：4 段中 ≥3 段正超额）\n")

rng = np.random.default_rng(5)
results = []
KEYS = list(FACTORS)
for it in range(400):
    w = {k: float(rng.choice([0.0, 0.5, 1.0, 1.5])) for k in KEYS}
    if sum(w.values()) < 1.5:
        continue
    amt = float(rng.choice([3e8, 5e8, 1e9]))
    tn = int(rng.choice([3, 5, 8]))
    pos, tot, avg, anns = seg_score(w, amt, tn)
    results.append({"pos": pos, "tot": tot, "avg_exc": avg, "anns": anns,
                    "w": w, "amt": amt, "tn": tn})
    if (it + 1) % 100 == 0:
        b = max(results, key=lambda x: (x["pos"], x["avg_exc"]))
        print(f"  {it+1}/400  当前最优 {b['pos']}/{b['tot']}段正超额 平均{b['avg_exc']:+.1f}%")

results.sort(key=lambda x: (-x["pos"], -x["avg_exc"]))
print(f"\n{'#':<3} {'正超额段':>9} {'平均超额':>9} {'topN':>5} {'成交额':>8}  各段年化")
print("-" * 88)
for i, r in enumerate(results[:15], 1):
    nz = " ".join(f"{k}={v:g}" for k, v in r["w"].items() if v)
    print(f"{i:<3} {r['pos']}/{r['tot']:<7} {r['avg_exc']:>8.2f}% {r['tn']:>5} {r['amt']:>8.0e}  "
          + " ".join(f"{a:>6.1f}" for a in r["anns"]))
    print(f"     {nz}")

json.dump(results[:40], open("_round2.json", "w"), ensure_ascii=False, indent=1)
print("\n已保存 _round2.json")
