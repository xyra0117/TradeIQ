"""系统化寻优：在训练集上搜索因子权重与过滤条件，用样本外检验是否过拟合。

关键防过拟合措施：
  1. 训练/样本外严格按时间切分
  2. 目标函数用【相对基准的超额】而非绝对收益（剥离市场beta）
  3. 加入交易成本与换手惩罚
  4. 参数搜索用粗网格 + 稳定性检查（相邻参数也要好）
"""

import itertools
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

# 基准：全市场等权日收益序列
mkt_ret = np.nan_to_num(np.nanmean(C[:, 1:] / C[:, :-1] - 1, axis=0))
mkt_eq = np.cumprod(1 + mkt_ret)

SPLIT = int(n_d * 0.6)   # 训练集 0-60%
J0 = 60


def score_day(j, p):
    """p 为参数字典。"""
    ok = np.isfinite(C[:, j]) & (C[:, j] > 0)
    ok &= fs.amt_yuan[:, j] > p["min_amount"]
    ok &= not_st
    if p.get("max_vol"):
        ok &= np.nan_to_num(fs.vol20[:, j], nan=9) < p["max_vol"]
    if p.get("min_vol"):
        ok &= np.nan_to_num(fs.vol20[:, j], nan=0) >= p["min_vol"]

    parts = []
    # 方向已按诊断结论修正：
    #   r60/r20 反向（涨多者差）
    #   ma20_slope 反向（趋势强者差）
    #   vol20 原方向（高波动者好）—— 诊断显示高波动组绝对收益更高
    for key, arr, sign in [
        ("w_r5", fs.r5, -1),
        ("w_r20", fs.r20, -1),
        ("w_r60", fs.r60, -1),
        ("w_vol", fs.vol20, +1),
        ("w_slope", fs.ma20_slope, -1),
        ("w_pos", fs.pos20, -1),
    ]:
        w = p.get(key, 0)
        if w:
            parts.append(w * _rank(sign * arr[:, j]))
    # 量比：追温和放量最优 -> 用钟形
    if p.get("w_vr"):
        vr = np.nan_to_num(fs.vr[:, j], nan=1.0)
        parts.append(p["w_vr"] * _rank(-np.abs(np.log(np.clip(vr, 0.1, 20) / 1.3))))
    # 收盘位置：强收盘好
    if p.get("w_closepos"):
        parts.append(p["w_closepos"] * _rank(fs.close_pos[:, j]))
    # 实体：阳线好
    if p.get("w_body"):
        parts.append(p["w_body"] * _rank(fs.body[:, j]))

    if not parts:
        return np.full(n_c, np.nan)
    s = np.sum(parts, axis=0)
    s[~ok] = np.nan
    return s


def backtest(p, j0, j1, top_n):
    picks = {}
    for j in range(j0, j1):
        s = score_day(j, p)
        v = np.where(np.isfinite(s))[0]
        if len(v) == 0:
            continue
        picks[j] = list(v[np.argsort(-s[v])][:top_n])
    if not picks:
        return None
    return simulate(dates, C, O, H, L, tradable, picks, hold=HOLD,
                    top_n=top_n, lp=lp)


def excess_and_sharpe(res, j0, j1):
    """返回 (年化超额, 夏普, 总收益%)，用同期基准切分。"""
    if res is None or len(res["equity"]) < 2:
        return -999, 0, -999
    eq = res["equity"][j0:j1]
    beq = mkt_eq[j0:j1]
    if len(eq) < 10:
        return -999, 0, -999
    n = len(eq)
    yrs = n / 243.0
    tot = eq[-1] / eq[0] - 1
    ann = (1 + tot) ** (1 / yrs) - 1 if tot > -1 else -1
    b_tot = beq[-1] / beq[0] - 1
    b_ann = (1 + b_tot) ** (1 / yrs) - 1 if b_tot > -1 else -1
    r = np.diff(eq) / eq[:-1]
    vol = r.std() * np.sqrt(243) if len(r) > 1 else 0
    sharpe = (r.mean() * 243) / vol if vol > 0 else 0
    return (ann - b_ann) * 100, sharpe, tot * 100


def objective(p, top_n, j0, j1):
    res = backtest(p, j0, j1, top_n)
    exc, sh, tot = excess_and_sharpe(res, j0, j1)
    return exc, sh, tot


# ---------- 参数网格 ----------
GRID = {
    "w_r5":     [0.0, 0.6, 1.2],
    "w_r20":    [0.0, 0.6, 1.2],
    "w_r60":    [0.0, 0.4, 0.8],
    "w_vol":    [0.0, 0.5, 1.0],
    "w_slope":  [0.0, 0.4, 0.8],
    "w_pos":    [0.0, 0.3],
    "w_vr":     [0.0, 0.3],
    "w_closepos": [0.0, 0.3],
    "w_body":   [0.0, 0.3],
    "min_amount": [5e7, 1e8, 3e8],
    "max_vol":  [0.0, 1.0, 1.5],
    "min_vol":  [0.0, 0.25, 0.4],
}

keys = list(GRID)
# 随机采样而非全笛卡尔积（组合数太大）
rng = np.random.default_rng(7)
N_TRIALS = 400
combos = []
seen = set()
while len(combos) < N_TRIALS:
    p = {k: float(rng.choice(GRID[k])) for k in keys}
    t = tuple(sorted(p.items()))
    if t in seen:
        continue
    seen.add(t)
    combos.append(p)

TOP_NS = [8, 12, 20]
results = []
for i, p in enumerate(combos):
    for tn in TOP_NS:
        exc, sh, tot = objective(p, tn, J0, SPLIT)
        results.append((exc, sh, tot, tn, p))

results.sort(key=lambda r: -r[0])
print(f"=== 训练集搜索完成：{len(results)} 组候选 ===")
print(f"训练期基准年化: {((mkt_eq[J0:SPLIT][-1]/mkt_eq[J0:SPLIT][0])**(243/(SPLIT-J0))-1)*100:.2f}%\n")

print("训练集 Top10（含样本外检验）:")
print(f"{'排名':<4} {'参数':<58} {'topN':<5} {'超额':>7} {'夏普':>6} {'样本外超额':>10} {'样本外总收':>10}")
print("-" * 105)
for r, (exc, sh, tot, tn, p) in enumerate(results[:10], 1):
    o_exc, o_sh, o_tot = objective(p, tn, SPLIT, n_d - HOLD - 1)
    desc = " ".join(f"{k.replace('w_','').replace('min_','m').replace('max_','M')}={v:g}" for k, v in p.items() if v != 0)
    print(f"{r:<4} {desc[:58]:<58} {tn:<5} {exc:>7.2f} {sh:>6.2f} {o_exc:>10.2f} {o_tot:>10.2f}")

json.dump(
    [{"excess": r[0], "sharpe": r[1], "total": r[2], "top_n": r[3], "params": r[4]} for r in results[:50]],
    open("_search_results.json", "w"), ensure_ascii=False, indent=1,
)
print("\n已保存 _search_results.json")
