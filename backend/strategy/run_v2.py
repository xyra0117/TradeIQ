"""基于 IC 诊断结论重写的 T+5 策略 v2。

诊断结论（本地数据实证）：
  - A股 T+5 呈【反向动量】：r20/r60 IC ≈ -0.06，过去涨多者未来 5 日表现差
  - 低波动因子 IC ≈ -0.06，波动大者表现差
  - 量比 vr 在高位偏负，放量冲高后回落
  - 主力资金流 IC ≈ 0，无预测力（本地数据抓取偏晚），不作为主因子

策略 v2 核心逻辑：
  1. 中期趋势仍向上（ma20_slope > 0），确认不是下跌中继
  2. 短期处于回调（r5 小幅为负或温和正），避开追高
  3. 回调幅度适中（不是崩塌），dist_high20 在合理区间
  4. 低波动、低换手（抗跌特征）
  5. 剔除 ST / 低流动性 / 涨跌停边缘

严格样本内外分离：前 60% 训练，后 40% 样本外验证。
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


def build_score(j, cfg):
    """按 v2 逻辑在信号日 j 打分。"""
    ok = np.isfinite(C[:, j]) & (C[:, j] > 0)
    ok &= fs.amt_yuan[:, j] > cfg["min_amount"]
    ok &= np.nan_to_num(fs.vol20[:, j], nan=9) < cfg["max_vol"]
    ok &= not_st

    parts = []
    # 1) 趋势向上但不强：ma20 斜率为正 -> 排除下跌中继
    slope = fs.ma20_slope[:, j]
    trend_ok = np.nan_to_num(slope, nan=-9) > cfg["min_slope"]
    # 2) 短期回调：r5 反向
    parts.append(cfg["w_r5"] * _rank(-fs.r5[:, j]))
    # 3) 中期反向动量：r20/r60 反向
    parts.append(cfg["w_r20"] * _rank(-fs.r20[:, j]))
    parts.append(cfg["w_r60"] * _rank(-fs.r60[:, j]))
    # 4) 低波动
    parts.append(cfg["w_vol"] * _rank(-fs.vol20[:, j]))
    # 5) 位置：距 20 日高点适中（0.85~0.98 最佳，太低是弱势）
    dhi = fs.dist_high20[:, j]
    pos_score = -np.abs(np.nan_to_num(dhi, nan=-9) + 0.10)
    parts.append(cfg["w_pos"] * _rank(pos_score))
    # 6) 量能：不追天量，温和缩量回调最佳
    vr = np.nan_to_num(fs.vr[:, j], nan=1.0)
    vr_score = -np.abs(np.log(np.clip(vr, 0.1, 20) / 0.9))
    parts.append(cfg["w_vr"] * _rank(vr_score))
    # 7) 收盘位置：不追冲高回落
    parts.append(cfg["w_closepos"] * _rank(fs.close_pos[:, j]))

    s = np.sum(parts, axis=0)
    # 趋势过滤：只在 ma20 向上的股票里选
    s = np.where(trend_ok, s, np.nan)
    s[~ok] = np.nan
    return s


BASE = {
    "min_amount": 1e8,
    "max_vol": 0.9,
    "min_slope": -0.005,
    "w_r5": 1.0,
    "w_r20": 0.8,
    "w_r60": 0.5,
    "w_vol": 0.8,
    "w_pos": 0.6,
    "w_vr": 0.4,
    "w_closepos": 0.3,
}


def run(cfg, top_n=10, j0=60, j1=None, stop_loss=None, take_profit=None, hold=HOLD):
    j1 = j1 if j1 is not None else n_d - hold - 1
    picks = {}
    for j in range(j0, j1):
        s = build_score(j, cfg)
        valid = np.where(np.isfinite(s))[0]
        if len(valid) == 0:
            continue
        order = valid[np.argsort(-s[valid])]
        picks[j] = list(order[:top_n])
    tradable = np.isfinite(C) & np.isfinite(O) & (C > 0) & (O > 0)
    return simulate(
        dates, C, O, H, L, tradable, picks, hold=hold, top_n=top_n,
        lp=lp, stop_loss=stop_loss, take_profit=take_profit,
    )


def bench():
    mkt_ret = np.nan_to_num(np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0))
    return np.cumprod(1 + mkt_ret)


def report(tag, res):
    m = metrics(res["equity"], dates, res["trades"])
    print(
        f"  {tag:<22} 年化{m['年化']:>7.2f}%  总收益{m['总收益']:>7.2f}%  "
        f"回撤{m['最大回撤']:>6.2f}%  夏普{m['夏普']:>5.2f}  "
        f"胜率{m['胜率']:>5.1f}%  笔数{m['交易次数']:>4d}"
    )
    return m


split = int(n_d * 0.6)
print(f"数据 {n_c} 只 x {n_d} 日 | {dates[0]} ~ {dates[-1]}")
print(f"样本内 60%: {dates[60]} ~ {dates[split-1]}")
print(f"样本外 40%: {dates[split]} ~ {dates[-1]}")

be = bench()
yrs = len(be) / 243
print(f"\n基准(全市场等权): 年化 {((be[-1])**(1/yrs)-1)*100:.2f}%")

print("\n=== 策略 v2 全样本 ===")
res = run(BASE)
report("v2 基础", res)

print("\n=== 样本内 / 样本外 分离验证 ===")
res_in = run(BASE, j1=split)
res_out = run(BASE, j0=split)
report("样本内(60%)", res_in)
report("样本外(40%)", res_out)

print("\n=== 持股数敏感性 ===")
for tn in [5, 8, 10, 15, 20]:
    r = run(BASE, top_n=tn)
    m = metrics(r["equity"], dates, r["trades"])
    o = run(BASE, top_n=tn, j0=split)
    mo = metrics(o["equity"], dates, o["trades"])
    print(
        f"  top{tn:<3d} 全样本年化{m['年化']:>7.2f}% 回撤{m['最大回撤']:>6.2f}% "
        f"| 样本外年化{mo['年化']:>7.2f}% 胜率{mo['胜率']:>5.1f}%"
    )

print("\n=== 持有期敏感性 ===")
for hd in [3, 5, 8, 10]:
    res_h = run(BASE, hold=hd)
    m = metrics(res_h["equity"], dates, res_h["trades"])
    o = run(BASE, j0=split, hold=hd)
    mo = metrics(o["equity"], dates, o["trades"])
    print(
        f"  T+{hd:<2d} 全样本年化{m['年化']:>7.2f}% 回撤{m['最大回撤']:>6.2f}% "
        f"| 样本外年化{mo['年化']:>7.2f}%"
    )
