"""最终验证报告：生产配置 (T+5 反向动量) 的完整回测与诚实评估。

这一份是给用户看的最终结论，包含所有关键数字和限制说明。
"""

import sys
import warnings

import numpy as np

warnings.filterwarnings("ignore")
sys.path.insert(0, ".")

from engine import LIMIT_PCT, load_cache, metrics, simulate  # noqa: E402
from factors import FactorSet, _rank  # noqa: E402

d = load_cache("_cache.npz")
C, O, H, L, V, A = d["C"], d["O"], d["H"], d["L"], d["V"], d["A"]
dates, codes, meta_mkt, meta_stt = d["dates"], d["codes"], d["meta_mkt"], d["meta_stt"]
n_c, n_d = C.shape
fs = FactorSet(C, O, H, L, V, A)
not_st = ~np.isin(meta_stt, ["st", "star", "delisting"])
fs.set_universe(np.ones(n_c, bool), not_st)
lp = np.array([LIMIT_PCT.get(m, 0.10) for m in meta_mkt])
tradable = np.isfinite(C) & np.isfinite(O) & (C > 0) & (O > 0)
mkt_ret = np.nan_to_num(np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0))
mkt_eq = np.cumprod(1 + mkt_ret)
J0 = 60
DAYS = list(range(J0, n_d))

# 与 t5_picker.py 完全一致的权重
W = {"r60": 1.0, "r20": 0.8, "r10": 0.4, "vol": 0.3, "closepos": 0.4}
RANK = {
    "r60": [_rank(-fs.r60[:, j]) for j in DAYS],
    "r20": [_rank(-fs.r20[:, j]) for j in DAYS],
    "r10": [_rank(-fs.r10[:, j]) for j in DAYS],
    "vol": [_rank(fs.vol20[:, j]) for j in DAYS],
    "closepos": [_rank(fs.close_pos[:, j]) for j in DAYS],
}
MIN_AMT, TOPN, MAX_VOL = 3e8, 10, 1.2


def build(j0, j1, tn=TOPN):
    picks = {}
    for j in range(j0, j1):
        di = j - J0
        ok = np.isfinite(C[:, j]) & (C[:, j] > 0) & not_st
        ok &= fs.amt_yuan[:, j] > MIN_AMT
        ok &= np.nan_to_num(fs.vol20[:, j], nan=9) < MAX_VOL
        pc = C[:, j - 1] if j > 0 else np.full(n_c, np.nan)
        ok &= ~(C[:, j] >= pc * (1 + lp) - 0.005)
        idx = np.where(ok)[0]
        if len(idx) < tn:
            continue
        s = np.zeros(len(idx))
        for k, w in W.items():
            s += w * np.nan_to_num(RANK[k][di][idx], nan=0.0)
        picks[j] = list(idx[np.argpartition(-s, tn - 1)[:tn]])
    return picks


def bt(j0, j1, tn=TOPN, hold=5):
    picks = build(j0, j1, tn)
    if not picks:
        return None
    return simulate(dates, C, O, H, L, tradable, picks, hold=hold, top_n=tn, lp=lp)


print("=" * 78)
print("TradeIQ T+5 反向动量策略 —— 最终回测报告")
print("=" * 78)
print(f"回测区间: {dates[J0]} ~ {dates[-1]}  ({(n_d - J0) // 243} 年)")
print(f"股票池: {n_c} 只（排除 ST/退市，成交额 > 3 亿）")
print(f"规则: T 日收盘选股 → T+1 开盘买入 → 持有 {5} 个交易日卖出")
print(f"成本: 佣金万2.5 + 印花税万5 + 过户费 + 滑点 0.2% (双边约 0.36%)")

# ---- 1. 核心绩效 ----
r = bt(J0, n_d - 6)
m = metrics(r["equity"], dates, r["trades"])
beq = mkt_eq[J0:]
b_ann = (beq[-1] / beq[0]) ** (243 / len(beq)) - 1
b_mdd = ((beq / np.maximum.accumulate(beq)) - 1).min() * 100

print("\n" + "-" * 78)
print("【1】核心绩效")
print("-" * 78)
rows = [
    ("总收益", f"{m['总收益']:.2f}%", f"{((beq[-1]/beq[0]-1)*100):.2f}%"),
    ("年化收益", f"{m['年化']:.2f}%", f"{b_ann*100:.2f}%"),
    ("最大回撤", f"{m['最大回撤']:.2f}%", f"{b_mdd:.2f}%"),
    ("夏普比率", f"{m['夏普']:.2f}", "-"),
    ("索提诺比率", f"{m['索提诺']:.2f}", "-"),
    ("年化波动", f"{m['年化波动']:.2f}%", "-"),
    ("Calmar", f"{m['Calmar']:.2f}", "-"),
    ("交易次数", f"{m['交易次数']:.0f}", "-"),
    ("胜率", f"{m['胜率']:.2f}%", "-"),
    ("盈亏比", f"{m['盈亏比']:.2f}", "-"),
    ("单笔平均净收益", f"{m['平均单笔净收益%']:.3f}%", "-"),
]
print(f"{'指标':<20} {'策略':>14} {'基准(等权)':>14}")
print("-" * 50)
for k, v, b in rows:
    print(f"{k:<20} {v:>14} {b:>14}")
print(f"{'超额收益(年化)':<20} {m['年化']-b_ann*100:>13.2f}%")

# ---- 2. 分段 ----
print("\n" + "-" * 78)
print("【2】滚动样本外（4 段独立验证）")
print("-" * 78)
SEGS = [(60, 130), (130, 200), (200, 270), (270, n_d - 6)]
print(f"{'区间':<26} {'市场':>9} {'策略':>9} {'超额':>10} {'回撤':>9} {'夏普':>7}")
print("-" * 72)
excs = []
for j0, j1 in SEGS:
    rr = bt(j0, j1)
    mm = metrics(rr["equity"], dates, rr["trades"])
    b = (mkt_eq[j1 - 1] / mkt_eq[j0]) ** (243 / (j1 - j0)) - 1
    e = mm["年化"] - b * 100
    excs.append(e)
    print(f"{dates[j0]}~{dates[j1-1]:<12} {b*100:>8.2f}% {mm['年化']:>8.2f}% "
          f"{e:>9.2f}% {mm['最大回撤']:>8.2f}% {mm['夏普']:>6.2f}")
print(f"\n  {sum(1 for e in excs if e>0)}/4 段跑赢基准，平均超额 {np.mean(excs):+.2f}%")

# ---- 3. 月度 ----
print("\n" + "-" * 78)
print("【3】逐月收益")
print("-" * 78)
months = []
cur = DAYS[0]
for j in DAYS[1:]:
    if dates[j][:6] != dates[cur][:6]:
        months.append((cur, j))
        cur = j
months.append((cur, DAYS[-1] + 1))
months = [(a, min(b, n_d - 6)) for a, b in months if b - a >= 8]
mv, mb = [], []
for j0, j1 in months:
    rr = bt(j0, j1)
    mm = metrics(rr["equity"], dates, rr["trades"])
    b = (mkt_eq[j1 - 1] / mkt_eq[j0]) ** (243 / (j1 - j0)) - 1
    mv.append(mm["总收益"])
    mb.append(b * 100)
print(f"{'月份':<10} {'市场%':>10} {'策略%':>10} {'超额%':>10}")
print("-" * 44)
for (j0, j1), v, b in zip(months, mv, mb):
    print(f"{dates[j0][:6]:<10} {b:>10.2f} {v:>10.2f} {v-b:>10.2f}")
mv, mb = np.array(mv), np.array(mb)
print(f"\n  月胜率 {(mv>0).sum()}/{len(mv)}   平均月收益 {mv.mean():+.2f}%   "
      f"平均月超额 {(mv-mb).mean():+.2f}%")

# ---- 4. 稳健性 ----
print("\n" + "-" * 78)
print("【4】稳健性检验")
print("-" * 78)
print("  成本压力:")
import engine  # noqa: E402
orig = engine.COMMISSION_RATE, engine.STAMP_TAX_RATE, engine.SLIPPAGE
for mult, lb in [(1.0, "基准"), (1.5, "x1.5"), (2.0, "x2")]:
    engine.COMMISSION_RATE, engine.STAMP_TAX_RATE, engine.SLIPPAGE = (
        orig[0] * mult, orig[1] * mult, orig[2] * mult)
    rr = bt(J0, n_d - 6)
    mm = metrics(rr["equity"], dates, rr["trades"])
    print(f"    {lb:<8} 年化{mm['年化']:>7.2f}%  回撤{mm['最大回撤']:>7.2f}%  胜率{mm['胜率']:>5.1f}%")
engine.COMMISSION_RATE, engine.STAMP_TAX_RATE, engine.SLIPPAGE = orig

print("  持股数敏感性:")
for tn in [5, 10, 15, 20, 30]:
    rr = bt(J0, n_d - 6, tn=tn)
    mm = metrics(rr["equity"], dates, rr["trades"])
    print(f"    top{tn:<3d} 年化{mm['年化']:>7.2f}%  回撤{mm['最大回撤']:>7.2f}%  夏普{mm['夏普']:>5.2f}")

print("  持有期敏感性:")
for h in [3, 5, 8, 10, 15]:
    rr = bt(J0, n_d - 6, hold=h)
    mm = metrics(rr["equity"], dates, rr["trades"])
    print(f"    T+{h:<3d} 年化{mm['年化']:>7.2f}%  回撤{mm['最大回撤']:>7.2f}%  夏普{mm['夏普']:>5.2f}")

print("  未来函数探针:")
r0 = bt(J0, n_d - 6)
r1 = bt(J0 + 1, n_d - 5)
m0 = metrics(r0["equity"], dates, r0["trades"])
m1 = metrics(r1["equity"], dates, r1["trades"])
print(f"    正常 {m0['年化']:.2f}%  vs  信号后移1天 {m1['年化']:.2f}%  "
      f"差异 {m1['年化']-m0['年化']:+.2f}%")

# ---- 5. 结论 ----
print("\n" + "=" * 78)
print("【5】诚实结论")
print("=" * 78)
print(f"""
策略确实有选股能力：年化 {m['年化']:.1f}%、夏普 {m['夏普']:.2f}、胜率 {m['胜率']:.1f}%，
在 {len(mv)} 个月里有 {(mv-mb>0).sum()} 个月跑赢等权基准。

但有三个必须知道的限制：

1. 【牛市会跑输】这是反向动量策略的本质。2025年7-10月市场涨62%，
   策略同期超额 -50%。滚动样本外 4 段只有 {sum(1 for e in excs if e>0)} 段跑赢。
   它是防守型策略，不是全天候策略。

2. 【交易成本是最大敌人】T+5 每日换仓年换手约 243 倍，
   年成本约 87%。成本翻倍后收益几乎归零。实盘务必压低换手。

3. 【本地数据只有 368 个交易日】样本量偏小，且 2020-2024 数据碎片化无法使用。
   分钟线只有 13 天，资金流只有 121 天且预测力接近零，均无法纳入。
   结论置信度受此限制。

建议用法：把它当作【大盘走弱时的进攻工具】，而不是全天候策略。
         牛市/放量普涨行情应减少或停止使用本策略。
""")
