"""T+5 选股策略回测引擎。

严格模拟 A 股交易规则：
  1. T 日收盘后选股，T+1 日开盘买入（不用未来数据）
  2. 持有 5 个交易日，T+5 日卖出
  3. 涨停不能买（开盘涨停封板）、跌停不能卖
  4. 停牌不能交易
  5. 手续费：佣金万 2.5 双边（最低 5 元）+ 卖出印花税万 5 + 过户费
  6. 滑点：按开盘价不利方向偏移

所有信号计算只用 <= T 日的数据，杜绝前视偏差。
"""

import numpy as np

# ---- A 股交易成本参数 ----
COMMISSION_RATE = 0.00025   # 佣金 万2.5
COMMISSION_MIN = 5.0        # 最低 5 元
STAMP_TAX_RATE = 0.0005     # 印花税 万5（卖出单边，2023-08 后）
TRANSFER_FEE = 0.00001      # 过户费 十万分之一（双边）
SLIPPAGE = 0.002            # 滑点 0.2%

# 各板块涨跌停限制
LIMIT_PCT = {
    "主板": 0.10,
    "创业板": 0.20,
    "科创板": 0.20,
    "北交所": 0.30,
}

# 涨跌停判定容差（浮点误差 + 最小变动价位）
LIMIT_TOL = 0.005


def load_cache(path="_cache.npz"):
    d = np.load(path, allow_pickle=False)
    return {k: d[k] for k in d.files}


def limit_pct_matrix(meta_mkt):
    """按市场给每只票每日涨跌停比例。"""
    n_c = len(meta_mkt)
    n_d = 1  # 占位，运行时广播
    lp = np.zeros(n_c)
    for i, m in enumerate(meta_mkt):
        lp[i] = LIMIT_PCT.get(m, 0.10)
    return lp


def is_limit_up(open_p, prev_close, lp, tol=LIMIT_TOL):
    """开盘价 >= 涨停价（考虑四舍五入容差）。"""
    return (open_p >= prev_close * (1 + lp) - tol) & np.isfinite(open_p) & np.isfinite(prev_close)


def is_limit_down(open_p, prev_close, lp, tol=LIMIT_TOL):
    return (open_p <= prev_close * (1 - lp) + tol) & np.isfinite(open_p) & np.isfinite(prev_close)


def trade_cost(buy_price, sell_price, shares):
    """双边成本：佣金（买+卖）+ 印花税（卖）+ 过户费 + 滑点。"""
    buy_amt = buy_price * shares
    sell_amt = sell_price * shares
    comm = max(buy_amt * COMMISSION_RATE, COMMISSION_MIN) + max(
        sell_amt * COMMISSION_RATE, COMMISSION_MIN
    )
    stamp = sell_amt * STAMP_TAX_RATE
    transfer = (buy_amt + sell_amt) * TRANSFER_FEE
    return comm + stamp + transfer


def simulate(dates, C, O, H, L, mask_tradable, picks, hold=5, top_n=10,
             lp=None, initial_capital=1_000_000.0, stop_loss=None, take_profit=None):
    """回测主循环（真实资金 + 逐日盯市）。

    交易规则：
      - 信号日 T 收盘选股，T+1 开盘买入
      - 持有 hold 个交易日后，于开盘卖出
      - 开盘涨停买不进；跌停/停牌卖不出则顺延
      - 佣金万 2.5 双边（最低 5 元）+ 印花税万 5（卖出）+ 过户费 + 0.2% 滑点

    参数
    ----
    picks : dict  {信号日 index: [code_index 按分数降序]}
    stop_loss / take_profit : 盘中触发则当日收盘价了结（None 表示只用固定持有期）
    """
    n_c, n_d = C.shape
    lp = lp if lp is not None else np.full(n_c, 0.10)
    prevC = np.full_like(C, np.nan)
    prevC[:, 1:] = C[:, :-1]

    trades = []
    cash = float(initial_capital)
    # 持仓：code_idx -> dict(shares, buy_price, buy_day, entry_fee)
    holdings = {}
    # 到期日：exit_day -> [code_idx]
    exit_schedule = {}
    equity_curve = []

    def sellable(code_idx, j):
        """当日是否可卖：非停牌、开盘非跌停、价格有效。"""
        if not mask_tradable[code_idx, j]:
            return False
        op = O[code_idx, j]
        if not np.isfinite(op) or op <= 0:
            return False
        pc = prevC[code_idx, j]
        if np.isfinite(pc) and pc > 0 and op <= pc * (1 - lp[code_idx]) + LIMIT_TOL:
            return False
        return True

    def close_position(code_idx, j, sell_price, reason):
        nonlocal cash
        h = holdings.pop(code_idx, None)
        if h is None:
            return
        shares, buy_price, entry_fee, buy_day = (
            h["shares"], h["buy_price"], h["entry_fee"], h["buy_day"])
        sell_amt = sell_price * shares
        comm = max(sell_amt * COMMISSION_RATE, COMMISSION_MIN)
        stamp = sell_amt * STAMP_TAX_RATE
        transfer = sell_amt * TRANSFER_FEE
        cash += sell_amt - comm - stamp - transfer
        cost_amt = buy_price * shares
        trades.append({
            "code": code_idx,
            "entry_day": buy_day,
            "exit_day": j,
            "buy_price": buy_price,
            "sell_price": sell_price,
            "cost": cost_amt + entry_fee,
            "ret": (sell_price / buy_price - 1) * 100,
            "net_ret": (sell_amt - comm - stamp - transfer - cost_amt - entry_fee)
            / cost_amt
            * 100,
            "pnl": sell_amt - comm - stamp - transfer - cost_amt - entry_fee,
            "reason": reason,
        })

    for j in range(n_d):
        # 1) 到期卖出
        for code_idx in exit_schedule.pop(j, []):
            if code_idx not in holdings:
                continue
            op = O[code_idx, j]
            if not np.isfinite(op) or op <= 0:
                # 极端情况：退市/长期停牌，顺延
                exit_schedule.setdefault(j + 1, []).append(code_idx)
                continue
            if sellable(code_idx, j):
                close_position(code_idx, j, op * (1 - SLIPPAGE), "hold")
            else:
                # 跌停卖不出，顺延一天
                exit_schedule.setdefault(j + 1, []).append(code_idx)

        # 2) 止盈止损（用当日最高/最低触发，成交价取触发价与开盘价的不利侧）
        if stop_loss is not None or take_profit is not None:
            for code_idx in list(holdings.keys()):
                h = holdings[code_idx]
                bp = h["buy_price"]
                hi, lo = H[code_idx, j], L[code_idx, j]
                if not (np.isfinite(hi) and np.isfinite(lo)):
                    continue
                if stop_loss is not None and lo <= bp * (1 - stop_loss):
                    px = min(lo, O[code_idx, j]) if np.isfinite(O[code_idx, j]) else lo
                    close_position(code_idx, j, px * (1 - SLIPPAGE), "stop")
                elif take_profit is not None and hi >= bp * (1 + take_profit):
                    px = max(lo, O[code_idx, j]) if np.isfinite(O[code_idx, j]) else hi
                    close_position(code_idx, j, px * (1 - SLIPPAGE), "take")

        # 3) 买入新仓（信号日 = j-1）
        if j > 0 and (j - 1) in picks:
            cands = [c for c in picks[j - 1][:top_n] if c not in holdings]
            tradable_cands = []
            for code_idx in cands:
                if not mask_tradable[code_idx, j]:
                    continue
                op = O[code_idx, j]
                pc = prevC[code_idx, j]
                if not np.isfinite(op) or op <= 0:
                    continue
                # 开盘涨停买不进
                if np.isfinite(pc) and pc > 0 and op >= pc * (1 + lp[code_idx]) - LIMIT_TOL:
                    continue
                tradable_cands.append(code_idx)
            if tradable_cands:
                alloc = cash / len(tradable_cands)
                for code_idx in tradable_cands:
                    exec_price = O[code_idx, j] * (1 + SLIPPAGE)
                    shares = alloc / exec_price
                    buy_amt = exec_price * shares
                    comm = max(buy_amt * COMMISSION_RATE, COMMISSION_MIN)
                    transfer = buy_amt * TRANSFER_FEE
                    if comm + transfer > cash:
                        continue
                    cash -= buy_amt + comm + transfer
                    holdings[code_idx] = {
                        "shares": shares,
                        "buy_price": exec_price,
                        "entry_fee": comm + transfer,
                        "buy_day": j,
                    }
                    exit_schedule.setdefault(j + hold, []).append(code_idx)

        # 4) 盯市
        mv = 0.0
        for code_idx, h in holdings.items():
            px = C[code_idx, j]
            if not np.isfinite(px) or px <= 0:
                px = O[code_idx, j] if np.isfinite(O[code_idx, j]) else h["buy_price"]
            mv += px * h["shares"]
        equity_curve.append(cash + mv)

    # 期末强平
    last = n_d - 1
    for code_idx in list(holdings.keys()):
        px = C[code_idx, last]
        if not np.isfinite(px) or px <= 0:
            px = holdings[code_idx]["buy_price"]
        close_position(code_idx, last, px * (1 - SLIPPAGE), "eod")

    return {"trades": trades, "equity": np.array(equity_curve), "dates": dates,
            "final_equity": equity_curve[-1] if equity_curve else initial_capital}


def metrics(equity, dates, trades):
    """计算绩效指标。"""
    eq = np.array(equity, dtype=float)
    if len(eq) < 2:
        return {}
    rets = np.diff(eq) / eq[:-1]
    rets = np.nan_to_num(rets)
    total_ret = eq[-1] / eq[0] - 1
    n_days = len(eq)
    years = n_days / 243.0
    ann = (1 + total_ret) ** (1 / years) - 1 if total_ret > -1 else -1

    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / peak
    mdd = dd.min()

    vol = rets.std() * np.sqrt(243) if len(rets) > 1 else 0
    sharpe = (rets.mean() * 243) / vol if vol > 0 else 0
    downside = rets[rets < 0]
    sortino = (rets.mean() * 243) / (downside.std() * np.sqrt(243)) if len(downside) > 1 and downside.std() > 0 else 0

    net = [t["net_ret"] for t in trades]
    win_rate = float(np.mean([r > 0 for r in net])) if net else 0
    avg_win = float(np.mean([r for r in net if r > 0])) if any(r > 0 for r in net) else 0
    avg_loss = float(np.mean([r for r in net if r <= 0])) if any(r <= 0 for r in net) else 0
    gains = sum(r for r in net if r > 0)
    losses = abs(sum(r for r in net if r < 0))
    profit_factor = gains / losses if losses > 0 else float("inf")

    return {
        "总收益": total_ret * 100,
        "年化": ann * 100,
        "最大回撤": mdd * 100,
        "夏普": sharpe,
        "索提诺": sortino,
        "年化波动": vol * 100,
        "交易次数": len(trades),
        "胜率": win_rate * 100,
        "平均单笔净收益%": float(np.mean(net)) if net else 0,
        "平均盈利%": avg_win,
        "平均亏损%": avg_loss,
        "盈亏比": profit_factor,
        "Calmar": (ann * 100 / abs(mdd * 100)) if mdd < 0 else 0,
    }


def bench_buy_hold(C, idx=0, start=0, end=None):
    """基准：等权持有全市场（或单只）到期末。"""
    end = end if end is not None else C.shape[1]
    # 用全市场每日平均收益模拟等权指数
    rets = np.nanmean(C[:, start:end] / C[:, start - 1 : end - 1] - 1, axis=0)
    eq = np.cumprod(1 + np.nan_to_num(rets))
    return eq
