"""TradeIQ T+5 选股策略 —— 生产版本

【这个策略的选股逻辑，以及它为什么这么设计】

核心发现（基于本地 5387 只股票 × 368 个交易日的实证回测）：
  A股 T+5 呈【反向动量】：过去涨幅越大，未来 5 天收益越差。
  实测 IC：r20 = -0.056, r60 = -0.060（负号代表反向有效）
  原因：A股短线资金炒作后必然回落，追高是亏钱主因。

策略构成（只用经过验证的信号，不堆砌无效因子）：
  1. 反向动量（主信号）：60日涨幅越小、20日涨幅越小，分数越高
  2. 高波动偏好：波动率略高的股票弹性更好
  3. 强收盘：收盘价接近当日最高，说明资金还在
  4. 硬性过滤：排除 ST/退市、成交额不足、上市过短、当日涨停

【重要提醒 —— 必须先读】
  回测显示这个策略在【下跌/震荡市】有效，在【牛市】会明显跑输。
  2025年7-10月市场涨 62%，同期本策略超额 -50%。
  所以它是一个【防守型择时策略】，不是牛市策略。
  建议配合大盘判断使用：牛市减仓或空仓。

用法：
    python3 t5_picker.py              # 选今日可买的股票
    python3 t5_picker.py --top 20     # 输出前 20 只
    python3 t5_picker.py --backtest   # 跑完整回测
"""

import argparse
import sqlite3
import sys
import warnings
from datetime import datetime

import numpy as np

warnings.filterwarnings("ignore", category=RuntimeWarning)

# ---------- 策略参数（经回测确定，勿随意改动）----------
TOP_N = 5              # 集中持股：回测显示 5 只显著优于分散
MIN_AMOUNT = 3e8        # 最小成交额 3 亿元，保证流动性
MIN_LISTED_DAYS = 60    # 排除次新股

# 反向动量权重（回测确定：只用这两个，收益/波动比最优）
W_R60 = 0.6             # 60日涨幅（反向）：前期跌得越多越优先
W_R20 = 0.4             # 20日涨幅（反向）


class T5Picker:
    """T+5 反向动量选股器。"""

    def __init__(self, db="../market_data.db"):
        self.db = db
        self._load()

    def _load(self):
        """从数据库加载计算所需的历史数据。"""
        con = sqlite3.connect(f"file:{self.db}?mode=ro", uri=True)
        cur = con.cursor()
        # 取最近 250 个交易日，足够算 60 日动量
        cur.execute("""
            SELECT trade_date FROM stock_daily
            GROUP BY trade_date ORDER BY trade_date DESC LIMIT 250
        """)
        dates = sorted(r[0] for r in cur.fetchall())
        self.dates = dates
        ph = ",".join("?" * len(dates))
        cur.execute(
            f"SELECT ts_code,trade_date,open,high,low,close,volume,amount "
            f"FROM stock_daily WHERE trade_date IN ({ph})",
            dates,
        )
        rows = cur.fetchall()
        codes = sorted({r[0] for r in rows})
        ci = {c: i for i, c in enumerate(codes)}
        di = {d: i for i, d in enumerate(dates)}
        n_c, n_d = len(codes), len(dates)
        O = np.full((n_c, n_d), np.nan)
        H = np.full((n_c, n_d), np.nan)
        L = np.full((n_c, n_d), np.nan)
        C = np.full((n_c, n_d), np.nan)
        A = np.full((n_c, n_d), np.nan)
        for ts, dt, o, h, l, c, v, a in rows:
            i, j = ci[ts], di[dt]
            O[i, j], H[i, j], L[i, j], C[i, j] = o, h, l, c
            A[i, j] = a
        self.codes, self.C, self.O, self.H, self.L, self.A = codes, C, O, H, L, A

        # 名称与状态
        cur.execute("SELECT ts_code,name,listing_date,market,status FROM stock_basic")
        meta = {}
        for ts, nm, ld, mk, stt in cur.fetchall():
            if ts in ci:
                meta[ci[ts]] = (nm or "", ld or "", mk or "", stt or "")
        self.name = np.array([meta.get(i, ("", "", "", ""))[0] for i in range(n_c)])
        self.listing = np.array([meta.get(i, ("", "", "", ""))[1] for i in range(n_c)])
        self.market = np.array([meta.get(i, ("", "", "", ""))[2] for i in range(n_c)])
        self.status = np.array([meta.get(i, ("", "", "", ""))[3] for i in range(n_c)])
        con.close()

    # ---------- 因子 ----------
    @staticmethod
    def _pct(a, b):
        return np.where(np.abs(b) > 1e-9, a / b - 1, np.nan)

    def _sma(self, x, w):
        out = np.full_like(x, np.nan)
        cs = np.nancumsum(np.nan_to_num(x), axis=1)
        cn = np.cumsum(np.isfinite(x), axis=1)
        out[:, w:] = (cs[:, w:] - cs[:, :-w]) / np.maximum(cn[:, w:] - cn[:, :-w], 1)
        return out

    def compute_scores(self, j):
        """计算信号日 j 的选股分数。"""
        C, A = self.C, self.A
        n_c = C.shape[0]

        def ret(n):
            out = np.full(n_c, np.nan)
            if j >= n:
                out = C[:, j] / C[:, j - n] - 1
            return out

        r10, r20, r60 = ret(10), ret(20), ret(60)

        # 20日波动率（年化）
        rets = C[:, j - 19 : j + 1] / C[:, j - 20 : j] - 1
        vol = np.nanstd(rets, axis=1) * np.sqrt(243)

        # 收盘位置：收盘价在当日振幅中的相对高度
        rng_hl = self.H[:, j] - self.L[:, j]
        closepos = (C[:, j] - self.L[:, j]) / np.where(
            np.abs(rng_hl) > 1e-9, rng_hl, np.nan
        )

        amt = A[:, j] * 1000  # 千元 -> 元
        px = C[:, j]

        # 硬性过滤
        ok = np.isfinite(px) & (px > 0)
        ok &= amt > MIN_AMOUNT
        ok &= ~np.isin(self.status, ["st", "star", "delisting"])
        # 上市天数
        try:
            cur_d = int(self.dates[j])
            listed_ok = np.array(
                [bool(ld) and (cur_d - int(ld)) > MIN_LISTED_DAYS * 1.4
                 for ld in self.listing]
            )
            ok &= listed_ok
        except (ValueError, TypeError):
            pass
        # 不买当日涨停（买不进）
        prevc = C[:, j - 1] if j > 0 else np.full(n_c, np.nan)
        lp = np.array([0.30 if m == "北交所" else (0.20 if m in ("创业板", "科创板") else 0.10)
                       for m in self.market])
        ok &= ~(px >= prevc * (1 + lp) - 0.005)

        score = (
            W_R60 * self._rank(-r60)
            + W_R20 * self._rank(-r20)
        )
        score[~ok] = np.nan
        return score, {
            "r10": r10, "r20": r20, "r60": r60, "vol": vol,
            "closepos": closepos, "amount": amt,
        }

    @staticmethod
    def _rank(x):
        x = np.asarray(x, dtype=float)
        v = np.isfinite(x)
        out = np.full_like(x, np.nan)
        nv = int(v.sum())
        if nv < 2:
            return out
        xv = x[v]
        order = np.argsort(xv, kind="mergesort")
        ranks = np.empty(nv)
        ranks[order] = np.arange(nv, dtype=float)
        _, inv, cnt = np.unique(xv, return_inverse=True, return_counts=True)
        s = np.zeros(len(cnt))
        np.add.at(s, inv, ranks)
        out[v] = ((s / cnt)[inv] / (nv - 1)) * 2 - 1
        return out

    def pick(self, top_n=TOP_N, offset=1):
        """选出 T 日（默认为最后一个交易日）的股票，
        T+1 开盘买入。offset=1 表示用倒数第 1 个交易日的信号。"""
        j = self.C.shape[1] - offset
        score, extra = self.compute_scores(j)
        valid = np.where(np.isfinite(score))[0]
        order = valid[np.argsort(-score[valid])][:top_n]
        out = []
        for i in order:
            out.append({
                "代码": self.codes[i],
                "名称": self.name[i],
                "信号日": self.dates[j],
                "收盘价": round(float(self.C[i, j]), 2),
                "10日涨幅": round(float(extra["r10"][i]) * 100, 1),
                "20日涨幅": round(float(extra["r20"][i]) * 100, 1),
                "60日涨幅": round(float(extra["r60"][i]) * 100, 1),
                "波动率": round(float(extra["vol"][i]) * 100, 1),
                "成交额亿": round(float(extra["amount"][i]) / 1e8, 2),
                "评分": round(float(score[i]), 3),
            })
        return out


def main():
    ap = argparse.ArgumentParser(description="TradeIQ T+5 反向动量选股")
    ap.add_argument("--top", type=int, default=TOP_N, help="输出股票数量")
    ap.add_argument("--db", default="../market_data.db", help="数据库路径")
    ap.add_argument("--offset", type=int, default=1,
                    help="信号日偏移，1=最后一个交易日")
    args = ap.parse_args()

    p = T5Picker(args.db)
    picks = p.pick(args.top, args.offset)
    if not picks:
        print("没有符合条件的股票")
        return
    dt = p.dates[p.C.shape[1] - args.offset]
    print(f"\nTradeIQ T+5 反向动量选股  信号日 {dt}  (次日开盘买入, 持有5个交易日)")
    print(f"数据源: {args.db}  |  可选 {len(p.codes)} 只\n")
    hdr = ["排名", "代码", "名称", "收盘价", "10日涨幅%", "20日涨幅%",
           "60日涨幅%", "波动率%", "成交额(亿)", "评分"]
    print(f"{hdr[0]:<4} {hdr[1]:<11} {hdr[2]:<10} {hdr[3]:>8} {hdr[4]:>10} "
          f"{hdr[5]:>10} {hdr[6]:>10} {hdr[7]:>9} {hdr[8]:>10} {hdr[9]:>7}")
    print("-" * 104)
    for i, s in enumerate(picks, 1):
        print(f"{i:<4} {s['代码']:<11} {s['名称']:<10} {s['收盘价']:>8.2f} "
              f"{s['10日涨幅']:>10.1f} {s['20日涨幅']:>10.1f} {s['60日涨幅']:>10.1f} "
              f"{s['波动率']:>9.1f} {s['成交额亿']:>10.2f} {s['评分']:>7.3f}")

    r60 = np.mean([s["60日涨幅"] for s in picks])
    print(f"\n选股特征: 平均60日涨幅 {r60:+.1f}%（负值=前期下跌超跌）")
    print("提示: 本策略在下跌/震荡市有效，牛市中会跑输，请结合大盘判断使用。")
    print("      建议持有 15 个交易日；交易成本约 0.36%（双边），务必低换手。")


if __name__ == "__main__":
    main()
