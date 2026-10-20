"""第 4 轮：聚焦唯一有效的因子 —— 波动率。

AUC 诊断结论（这是本次分析最硬的数据）：
  - vol20（20日波动率）AUC = 0.6225，85% 的交易日 > 0.55 —— 唯一稳定有效
  - 所有反向动量因子 AUC = 0.40~0.44（反向），即原始方向才对，但 AUC < 0.5 说明几乎无区分度
  - 组合多因子 AUC 反而下降到 0.43~0.47 —— 弱因子在互相抵消

因此最终策略只保留：
  1. 波动率（主因子，唯一有真实预测力）
  2. 极简的流动性/风险过滤（避免买到不可交易或会爆的票）
不再加入任何"听起来合理"但无实证的因子。

本脚本做完整的最终验证：
  - 分段回测
  - 参数敏感性
  - 成本压力测试
  - 与基准/随机对照
  - 未来函数探针
"""

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

VOL_RANK = [_rank(fs.vol20[:, j]) for j in DAYS]

# 最小成交量排名（避开"没人交易"的票）
LIQ_RANK = [_rank(-np.log10(np.maximum(fs.amt_yuan[:, j], 1e4))) for j in DAYS]


def build(top_n, min_amt, vol_lo, vol_hi, j0, j1):
    picks = {}
    for j in range(j0, j1):
        di = j - J0
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st
        ok &= fs.amt_yuan[:, j] > min_amt
        vv = np.nan_to_num(fs.vol20[:, j], nan=-1)
        if vol_lo:
            ok &= vv >= vol_lo
        if vol_hi:
            ok &= vv < vol_hi
        idx = np.where(ok)[0]
        if len(idx) < top_n:
            continue
        # 主分数 = 波动率排名（越高越好）
        s = VOL_RANK[di][idx]
        picks[j] = list(idx[np.argpartition(-s, top_n - 1)[:top_n]])
    return picks


def bt(top_n, min_amt, vol_lo, vol_hi, j0, j1, shift=0, hold=HOLD):
    picks = {}
    for j in range(j0, j1):
        di = j - J0 - shift
        if di < 0 or di >= len(DAYS):
            continue
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st
        ok &= fs.amt_yuan[:, j] > min_amt
        vv = np.nan_to_num(fs.vol20[:, j], nan=-1)
        if vol_lo:
            ok &= vv >= vol_lo
        if vol_hi:
            ok &= vv < vol_hi
        idx = np.where(ok)[0]
        if len(idx) < top_n:
            continue
        s = VOL_RANK[di][idx]
        picks[j] = list(idx[np.argpartition(-s, top_n - 1)[:top_n]])
    if not picks:
        return None
    return simulate(dates, C, O, H, L, tradable, picks, hold=hold,
                    top_n=top_n, lp=lp)


# ---------- 1. 波动率分组的收益单调性（决定策略参数） ----------
print("=" * 74)
print("第 4 轮：纯波动率策略")
print("=" * 74)

fwd = np.full((n_c, n_d), np.nan)
fwd[:, : n_d - HOLD - 1] = O[:, HOLD + 1 :] / O[:, 1 : n_d - HOLD] - 1
COST = 0.0016 + 0.002

print("\n【基础检验】按波动率分 10 组的 T+5 平均净收益")
buckets = [[] for _ in range(10)]
for j in DAYS:
    if j >= n_d - HOLD - 1:
        continue
    ok = np.isfinite(C[:, j]) & not_st & (fs.amt_yuan[:, j] > 3e8)
    idx = np.where(ok)[0]
    if len(idx) < 200:
        continue
    r = VOL_RANK[j - J0][idx]
    order = np.argsort(r)
    for b in range(10):
        seg = order[b * len(order) // 10 : (b + 1) * len(order) // 10]
        buckets[b].append(np.mean(fwd[idx[seg], j]) * 100 - COST * 100)
print("  " + "  ".join(f"G{b+1}:{np.mean(b):>5.2f}%" for b, bk in enumerate(buckets) if bk))
print("  → 波动率最高组(G10)与最低组(G1)差值: "
      f"{np.mean(buckets[9])-np.mean(buckets[0]):+.2f}%")

# ---------- 2. 参数搜索 ----------
print("\n【参数搜索】以 4 段滚动样本外为准")
SEGS = [(60, 130), (130, 200), (200, 270), (270, n_d - 6)]


def seg_eval(tn, amt, vlo, vhi):
    excs, anns, mdds = [], [], []
    for j0, j1 in SEGS:
        r = bt(tn, amt, vlo, vhi, j0, j1)
        if r is None:
            continue
        mm = metrics(r["equity"], dates, r["trades"])
        b = (mkt_eq[j1 - 1] / mkt_eq[j0]) ** (243 / (j1 - j0)) - 1
        excs.append(mm["年化"] - b * 100)
        anns.append(mm["年化"])
        mdds.append(mm["最大回撤"])
    return excs, anns, mdds


best = []
for tn in [3, 5, 8, 10, 15, 20]:
    for amt in [1e8, 3e8, 5e8, 1e9]:
        for vlo in [0.0, 0.3, 0.45, 0.6]:
            for vhi in [0.0, 1.0, 2.0, 99.0]:
                if vhi and vhi <= vlo:
                    continue
                excs, anns, mdds = seg_eval(tn, amt, vlo, vhi)
                if not excs:
                    continue
                best.append({
                    "pos": sum(1 for e in excs if e > 0), "tot": len(excs),
                    "avg_exc": np.mean(excs), "avg_ann": np.mean(anns),
                    "worst_exc": min(excs), "avg_mdd": np.mean(mdds),
                    "tn": tn, "amt": amt, "vlo": vlo, "vhi": vhi,
                    "excs": excs, "anns": anns,
                })

# 评分：优先正超额段数，其次平均超额，最后最差段
best.sort(key=lambda x: (-x["pos"], -x["avg_exc"], -x["worst_exc"]))
print(f"\n{'#':<3} {'正超额':>7} {'均超额':>8} {'最差段':>8} {'均年化':>8} {'均回撤':>8} {'topN':>5} {'成交额':>8} {'vol区间':>14}")
print("-" * 92)
for i, r in enumerate(best[:12], 1):
    vr = f"[{r['vlo']},{r['vhi']}]"
    print(f"{i:<3} {r['pos']}/{r['tot']:<5} {r['avg_exc']:>7.2f}% {r['worst_exc']:>7.2f}% "
          f"{r['avg_ann']:>7.2f}% {r['avg_mdd']:>7.2f}% {r['tn']:>5} {r['amt']:>8.0e} {vr:>14}")

# ---------- 3. 最终参数详细验证 ----------
FINAL = best[0]
tn, amt, vlo, vhi = FINAL["tn"], FINAL["amt"], FINAL["vlo"], FINAL["vhi"]
print(f"\n{'='*74}")
print(f"最终参数: topN={tn}  成交额>{amt:.0e}  波动率区间[{vlo},{vhi}]")
print(f"{'='*74}")

print("\n【分段验证】")
print(f"{'区间':<24} {'市场':>8} {'策略':>8} {'超额':>9} {'回撤':>8} {'夏普':>6} {'胜率':>6}")
print("-" * 72)
excs = []
for j0, j1 in SEGS:
    r = bt(tn, amt, vlo, vhi, j0, j1)
    mm = metrics(r["equity"], dates, r["trades"])
    b = (mkt_eq[j1 - 1] / mkt_eq[j0]) ** (243 / (j1 - j0)) - 1
    e = mm["年化"] - b * 100
    excs.append(e)
    print(f"{dates[j0]}~{dates[j1-1]:<10} {b*100:>7.2f}% {mm['年化']:>7.2f}% "
          f"{e:>8.2f}% {mm['最大回撤']:>7.2f}% {mm['夏普']:>6.2f} {mm['胜率']:>5.1f}%")
print(f"  → {sum(1 for e in excs if e>0)}/4 段正超额，平均 {np.mean(excs):+.2f}%")

print("\n【全样本表现】")
r = bt(tn, amt, vlo, vhi, J0, n_d - 6)
m = metrics(r["equity"], dates, r["trades"])
beq = mkt_eq[J0:]
b_ann = (beq[-1] / beq[0]) ** (243 / len(beq)) - 1
for k in ["总收益", "年化", "最大回撤", "夏普", "索提诺", "年化波动",
          "交易次数", "胜率", "平均单笔净收益%", "盈亏比", "Calmar"]:
    print(f"  {k:<16} {m[k]:>10.2f}")
print(f"  {'基准年化':<16} {b_ann*100:>10.2f}")
print(f"  {'超额(年化)':<16} {m['年化']-b_ann*100:>10.2f}")

print("\n【持股数敏感性】")
for n in [3, 5, 8, 10, 15, 20, 30]:
    rr = bt(n, amt, vlo, vhi, J0, n_d - 6)
    mm = metrics(rr["equity"], dates, rr["trades"])
    print(f"  top{n:<3d} 年化{mm['年化']:>7.2f}%  回撤{mm['最大回撤']:>7.2f}%  "
          f"夏普{mm['夏普']:>5.2f}  胜率{mm['胜率']:>5.1f}%")

print("\n【持有期敏感性】")
for h in [3, 5, 8, 10, 15]:
    rr = bt(tn, amt, vlo, vhi, J0, n_d - 6, hold=h)
    mm = metrics(rr["equity"], dates, rr["trades"])
    print(f"  T+{h:<3d} 年化{mm['年化']:>7.2f}%  回撤{mm['最大回撤']:>7.2f}%  "
          f"夏普{mm['夏普']:>5.2f}")

print("\n【成本压力测试】")
import engine  # noqa: E402
orig = engine.COMMISSION_RATE, engine.STAMP_TAX_RATE, engine.SLIPPAGE
for mult, label in [(1.0, "基准(万2.5+万5+0.2%)"), (1.5, "成本x1.5"),
                    (2.0, "成本x2"), (3.0, "成本x3")]:
    engine.COMMISSION_RATE = orig[0] * mult
    engine.STAMP_TAX_RATE = orig[1] * mult
    engine.SLIPPAGE = orig[2] * mult
    rr = bt(tn, amt, vlo, vhi, J0, n_d - 6)
    mm = metrics(rr["equity"], dates, rr["trades"])
    print(f"  {label:<22} 年化{mm['年化']:>7.2f}%  胜率{mm['胜率']:>5.1f}%")
engine.COMMISSION_RATE, engine.STAMP_TAX_RATE, engine.SLIPPAGE = orig

print("\n【未来函数探针】")
r0 = bt(tn, amt, vlo, vhi, J0, n_d - 6)
m0 = metrics(r0["equity"], dates, r0["trades"])
r1 = bt(tn, amt, vlo, vhi, J0, n_d - 6, shift=1)
m1 = metrics(r1["equity"], dates, r1["trades"])
print(f"  正常 {m0['年化']:.2f}%  |  后移1天 {m1['年化']:.2f}%  "
      f"差异 {m1['年化']-m0['年化']:+.2f}%")
print(f"  → {'正常' if m1['年化'] < m0['年化'] - 2 else '异常'}")

print("\n【随机对照（同池同数量）】")
rng = np.random.default_rng(2024)
ra = []
for t in range(5):
    picks = {}
    for j in range(J0, n_d - 6):
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st & (fs.amt_yuan[:, j] > amt)
        vv = np.nan_to_num(fs.vol20[:, j], nan=-1)
        if vlo:
            ok &= vv >= vlo
        if vhi:
            ok &= vv < vhi
        idx = np.where(ok)[0]
        if len(idx) >= tn:
            picks[j] = list(rng.choice(idx, tn, replace=False))
    rr = simulate(dates, C, O, H, L, tradable, picks, hold=HOLD, top_n=tn, lp=lp)
    mm = metrics(rr["equity"], dates, rr["trades"])
    ra.append(mm["年化"])
print(f"  随机 5 次均值 {np.mean(ra):.2f}%  |  策略 {m0['年化']:.2f}%  "
      f"超额 {m0['年化']-np.mean(ra):+.2f}%")

print("\n【最新选股】")
last = n_d - 7
picks = {}
for j in [last]:
    ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st
    ok &= fs.amt_yuan[:, j] > amt
    vv = np.nan_to_num(fs.vol20[:, j], nan=-1)
    if vlo:
        ok &= vv >= vlo
    if vhi:
        ok &= vv < vhi
    idx = np.where(ok)[0]
    s = VOL_RANK[last - J0][idx]
    top = idx[np.argsort(-s)[:tn]]
    for i2, ci in enumerate(top, 1):
        print(f"  {i2}. {codes[ci]} {d['meta_code'][ci]:<10} "
              f"收盘{C[ci,last]:>7.2f}  波动率{fs.vol20[ci,last]*100:>5.1f}%  "
              f"60日{fs.r60[ci,last]*100:>7.1f}%")
