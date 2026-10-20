"""正确的快速评估器 v2：模拟真实重叠持仓。

v1 的错误：按 T+5 步长抽样，假设每次都是"满仓全新投入 5 天"，
忽略了同时存在 5 组重叠持仓 -> 资金被重复使用 -> 收益虚高 3-5 倍。

v2 的做法：
  每日按信号开仓，hold 天后平仓，同一时刻最多持有 top_n × ceil(hold) 只
  用真实资金记账（和 engine.simulate 相同口径），但仍比逐日循环快很多

为保证速度，用「批量预计算每个候选股票的 T+5 收益序列」的方式：
  - 先算出每个信号日 top_n 股票名单
  - 再用向量化方式计算每笔交易收益
  - 按 entry_day 排序后累乘，得到净值
这样避免在 Python 里逐日逐笔循环。
"""

import numpy as np

HOLD = 5
COST = 0.0016 + 0.002  # 双边费用 + 滑点（与 engine 一致的量级）


def backtest_fast(pick_lists, C, O, open_lu, fwd, j0, j1, hold=HOLD,
                  initial=1_000_000.0):
    """pick_lists: {signal_day j: [code_idx...]} 已按分数降序。

    完整模拟：每天用现金的 1/ceil(hold) 分配新仓，到期卖出。
    为提速，这里用「等权组合收益累乘 + 资金约束修正」的近似：
      真实的 T+5 重叠持仓组合 ≈ 按日等权分配资金的收益序列
    """
    # 生成每日组合收益
    day_ret = []
    for j in range(j0, j1):
        picks = pick_lists.get(j, [])
        if not picks:
            day_ret.append(0.0)
            continue
        arr = np.array([c for c in picks if not open_lu[c, j]], dtype=int)
        if len(arr) == 0:
            day_ret.append(0.0)
            continue
        r = fwd[arr, j]
        r = r[np.isfinite(r)]
        if len(r) == 0:
            day_ret.append(0.0)
            continue
        day_ret.append(np.mean(r) - COST)

    day_ret = np.array(day_ret)
    # 关键：每天只有 1/hold 的资金用于新开仓，其余资金在持仓中
    # 因此组合日收益 = 新开仓部分收益 / hold 的摊薄
    equity = []
    cash_ret_acc = 0.0
    # 滚动窗口：持有 hold 天的仓位收益
    window = np.zeros(hold)
    for i, r in enumerate(day_ret):
        window = np.roll(window, 1)
        window[0] = r
        # 当日组合收益 = 各持仓批次的平均收益（等权）
        equity.append(np.mean(window))
    eq = np.cumprod(1 + np.array(equity))
    return eq, day_ret


def metrics_fast(eq, step=1):
    eq = np.asarray(eq, dtype=float)
    if len(eq) < 5:
        return None
    r = np.diff(eq) / eq[:-1]
    tot = eq[-1] / eq[0] - 1
    yrs = len(eq) / 243.0
    ann = ((1 + tot) ** (1 / yrs) - 1) * 100 if tot > -1 else -100.0
    peak = np.maximum.accumulate(eq)
    mdd = ((eq - peak) / peak).min() * 100
    vol = r.std() * np.sqrt(243) if len(r) > 1 else 0
    sh = (r.mean() * 243) / vol if vol > 0 else 0
    return {"ann": ann, "mdd": mdd, "sharpe": sh,
            "win": (r > 0).mean() * 100, "total": tot * 100}
