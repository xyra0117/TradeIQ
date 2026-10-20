"""基准回测：验证引擎正确性 + 给出初始成绩。"""

import sys

import numpy as np

sys.path.insert(0, ".")
from engine import load_cache, metrics, simulate  # noqa: E402
from factors import FactorSet  # noqa: E402

d = load_cache("_cache.npz")
dates = d["dates"]
C, O, H, L, V, A = d["C"], d["O"], d["H"], d["L"], d["V"], d["A"]
meta_list, meta_stt = d["meta_code"], d["meta_stt"]

print(f"universe: {C.shape[0]} codes x {C.shape[1]} days")
print(f"range: {dates[0]} ~ {dates[-1]}")

# 基准：全市场等权（每日平均收益）
mkt_ret = np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0)
mkt_ret = np.nan_to_num(mkt_ret)
mkt_eq = np.cumprod(1 + mkt_ret)
n = len(mkt_eq)
yrs = n / 243
print("\n=== 基准：全市场等权 ===")
print(f"  总收益 {((mkt_eq[-1]-1)*100):.2f}%  年化 {((mkt_eq[-1])**(1/yrs)-1)*100:.2f}%")
peak = np.maximum.accumulate(mkt_eq)
print(f"  最大回撤 {((mkt_eq/peak-1).min()*100):.2f}%")

# 基准：随机选股（对照）
fs = FactorSet(C, O, H, L, V, A)
# 上市天数掩码：数据首日即认为已上市足够久（本地回测窗口仅 368 天）
listed = np.ones(C.shape[0], bool)
st_mask = ~np.isin(meta_stt, ["st", "star", "delisting"])
fs.set_universe(listed, st_mask)

print("\n=== 对照：随机 10 只 等权持有 T+5 ===")
rng = np.random.default_rng(42)
n_d = C.shape[1]
rand_picks = {}
for j in range(60, n_d - 6):
    idx = rng.choice(C.shape[0], 10, replace=False)
    rand_picks[j] = list(idx)

tradable = np.isfinite(C) & np.isfinite(O) & (C > 0)
res = simulate(dates, C, O, H, L, tradable, rand_picks, hold=5, top_n=10)
m = metrics(res["equity"], dates, res["trades"])
print(
    f"  年化 {m['年化']:.2f}%  回撤 {m['最大回撤']:.2f}%  胜率 {m['胜率']:.1f}%  "
    f"笔数 {m['交易次数']}  夏普 {m['夏普']:.2f}"
)

# 策略 v1：纯放量突破 + 均线多头
print("\n=== 策略 v1: 突破 + 量能 + 均线多头 + 动量 ===")
cfg = {
    "w_breakout": 1.5,
    "w_vr": 1.0,
    "w_bull": 1.0,
    "w_maslp": 0.5,
    "w_r5": 0.8,
    "w_r20": 0.5,
    "w_pos": 0.5,
    "w_closepos": 0.3,
    "min_amount": 1e8,
    "max_vol": 1.2,
    "min_listed_days": 60,
    "exclude_st": True,
}
picks = {}
for j in range(60, n_d - 6):
    s = fs.score(j, cfg)
    ok = np.isfinite(s)
    idx = np.argsort(-np.nan_to_num(s, nan=-9e9))
    picks[j] = [i for i in idx[:30] if ok[i]][:10]

res2 = simulate(dates, C, O, H, L, tradable, picks, hold=5, top_n=10)
m2 = metrics(res2["equity"], dates, res2["trades"])
for k, v in m2.items():
    print(f"  {k}: {v:.2f}" if isinstance(v, float) else f"  {k}: {v}")
