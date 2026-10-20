"""短线因子库：全部基于 <= T 日数据计算，无未来函数。

A股 T+5 短线的实证有效逻辑：
  - 量能：放量突破是主升浪启动信号，但过度放量（天量）要防见顶
  - 趋势：均线多头排列 + 股价站上均线
  - 资金：主力净流入（net 相对流通市值归一）
  - 相对强度：跑赢大盘/板块，强者恒强
  - 位置：避免追高（离 20 日高点太远），偏好刚突破
  - 波动：控制风险，剔除妖股
"""

import numpy as np


def _f(x):
    """安全除法，避免 inf 污染。"""
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.where(np.abs(x) > 1e-9, x, np.nan)
    return r


def sma(x, w):
    """简单移动平均，axis=1。前 w-1 列为 NaN。

    窗口 [j-w+1, j]，用 cumsum 差分：c[:, j] - c[:, j-w]，
    其中 c 是 pad 后的累积和（pad 索引 k 对应原始 j = k-1）。
    """
    x = np.asarray(x, dtype=np.float64)
    c = np.cumsum(np.nan_to_num(x, nan=0.0), axis=1)
    n = np.cumsum(np.isfinite(x).astype(np.float64), axis=1)
    cp = np.concatenate([np.zeros((x.shape[0], 1)), c], axis=1)
    np_ = np.concatenate([np.zeros((x.shape[0], 1)), n], axis=1)
    s = cp[:, w:] - cp[:, :-w]
    cnt = np_[:, w:] - np_[:, :-w]
    out = np.full_like(x, np.nan, dtype=np.float64)
    out[:, w - 1 :] = np.where(cnt >= w * 0.8, s / np.maximum(cnt, 1), np.nan)
    return out


def rolling_max(x, w):
    """滑动窗口最大值，axis=1。前 w-1 列为 NaN。"""
    pad = np.full((x.shape[0], w - 1), np.nan, dtype=x.dtype)
    xp = np.concatenate([pad, x], axis=1)
    win = np.lib.stride_tricks.sliding_window_view(xp, w, axis=1)
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            out = np.nanmax(win, axis=-1)
    return out.astype(x.dtype)


def rolling_min(x, w):
    """滑动窗口最小值，axis=1。前 w-1 列为 NaN。"""
    pad = np.full((x.shape[0], w - 1), np.nan, dtype=x.dtype)
    xp = np.concatenate([pad, x], axis=1)
    win = np.lib.stride_tricks.sliding_window_view(xp, w, axis=1)
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        out = np.nanmin(win, axis=-1)
    return out.astype(x.dtype)


def ret_n(C, n):
    """n 日收益率：C[t]/C[t-n] - 1。"""
    out = np.full_like(C, np.nan)
    out[:, n:] = C[:, n:] / C[:, :-n] - 1
    return out


class FactorSet:
    """一次性算好全部因子，之后每个信号日直接切片取值。"""

    def __init__(self, C, O, H, L, V, A, bench=None):
        self.C, self.O, self.H, self.L, self.V, self.A = C, O, H, L, V, A
        n_c, n_d = C.shape

        # ---- 均线 ----
        self.ma5 = sma(C, 5)
        self.ma10 = sma(C, 10)
        self.ma20 = sma(C, 20)
        self.ma60 = sma(C, 60)

        # ---- 动量 ----
        self.r5 = ret_n(C, 5)
        self.r10 = ret_n(C, 10)
        self.r20 = ret_n(C, 20)
        self.r60 = ret_n(C, 60)

        # ---- 量能 ----
        self.vma5 = sma(V, 5)
        self.vma20 = sma(V, 20)
        # 量比：今日量 / 20 日均量
        self.vr = _f(V / self.vma20)
        # 量能温和放大区间（1.2 ~ 3.0 最优，天量 >4 反而见顶）
        self.vr_mild = (self.vr > 1.2) & (self.vr < 3.5)

        # ---- 成交额（流动性）----
        # 注意：TuShare daily 的 amount 单位是【千元】，volume 单位是【手】
        # 这里统一换算成【元】，避免流动性门槛写错量级。
        self.amt_yuan = A * 1000.0
        self.amt20 = sma(self.amt_yuan, 20)
        self.turnover = self.amt_yuan  # 成交额（元）

        # ---- 位置（避免追高）----
        self.hh20 = rolling_max(H, 20)
        self.ll20 = rolling_min(L, 20)
        # 距 20 日高点距离：0 = 在高点，负值 = 离高点多远
        self.dist_high20 = _f(C / self.hh20 - 1)
        # 突破：今日收盘创 20 日新高（用前 20 日的最高，不含今日）
        prev_hh20 = np.full_like(C, np.nan)
        prev_hh20[:, 20:] = rolling_max(H, 20)[:, :-20]
        self.breakout20 = C > prev_hh20
        # 位置：0~1，越靠近高点越好，但 0.98 以上追高
        self.pos20 = _f((C - self.ll20) / _f(self.hh20 - self.ll20))

        # ---- 波动率 ----
        daily_ret = np.full_like(C, np.nan)
        daily_ret[:, 1:] = C[:, 1:] / C[:, :-1] - 1
        # 20 日滚动波动率（与 C 同列数，前 20 列为 NaN）
        self.vol20 = np.full_like(C, np.nan)
        dr = np.nan_to_num(daily_ret, nan=0.0)
        valid = np.isfinite(daily_ret).astype(np.float64)
        cp = np.cumsum(dr, axis=1)
        sp = np.cumsum(dr * dr, axis=1)
        vp = np.cumsum(valid, axis=1)
        # 窗口 daily_ret[:, j-19 : j+1]，两端的 cumsum 直接同宽切片
        s1 = cp[:, 19:] - np.concatenate(
            [np.zeros((C.shape[0], 1)), cp[:, : C.shape[1] - 20]], axis=1
        )
        s2 = sp[:, 19:] - np.concatenate(
            [np.zeros((C.shape[0], 1)), sp[:, : C.shape[1] - 20]], axis=1
        )
        nn = vp[:, 19:] - np.concatenate(
            [np.zeros((C.shape[0], 1)), vp[:, : C.shape[1] - 20]], axis=1
        )
        ok = nn >= 15
        var = np.where(
            ok,
            (s2 - s1 * s1 / np.maximum(nn, 1)) / np.maximum(nn - 1, 1),
            np.nan,
        )
        self.vol20[:, 20:] = (
            np.sqrt(np.maximum(var[:, 1:], 0)) * np.sqrt(243)
        ).astype(C.dtype)
        assert self.vol20.shape == C.shape, f"vol20 shape {self.vol20.shape} != {C.shape}"
        # 前 20 列 vol20 必须为 NaN（窗口不足）
        assert np.all(np.isnan(self.vol20[:, :20])), "vol20 warmup should be NaN"

        # ---- K 线形态 ----
        # 实体占比：(close-open)/(high-low)，大阳线 = 强
        self.body = _f((C - O) / _f(H - L))
        # 上影线：冲高回落 = 弱
        self.upper_shadow = _f((H - np.maximum(C, O)) / _f(H - L))
        # 收盘位置：(close-low)/(high-low)，接近 1 = 收在最高 = 强
        self.close_pos = _f((C - L) / _f(H - L))

        # ---- 趋势排列 ----
        self.bull_stack = (self.ma5 > self.ma10) & (self.ma10 > self.ma20)
        self.above_ma20 = C > self.ma20
        self.ma20_slope = np.full_like(C, np.nan)
        ma20_shift = np.full_like(C, np.nan)
        ma20_shift[:, 5:] = self.ma20[:, :-5]
        self.ma20_slope = _f(self.ma20 / ma20_shift - 1)
        self.ma5_up = np.full_like(C, np.nan)
        ma5_prev = np.full_like(C, np.nan)
        ma5_prev[:, 1:] = self.ma5[:, :-1]
        self.ma5_up = self.ma5 > ma5_prev

        # ---- 相对强度 ----
        self.bench = bench
        if bench is not None:
            self.rs5 = _f(self.r5 - (bench[5:] / bench[:-5] - 1))
            self.rs20 = _f(self.r20 - (bench[20:] / bench[:-20] - 1))

        # ---- 连涨天数（向量化）----
        up = np.zeros_like(C, dtype=np.int16)
        up[:, 1:] = (C[:, 1:] > C[:, :-1]).astype(np.int16)
        # 连续 1 的长度：cumsum 差分
        grp = np.cumsum((up == 0).astype(np.int32), axis=1)
        first = np.zeros_like(grp, dtype=bool)
        first[:, 0] = True
        first[:, 1:] = up[:, 1:] == 0
        gid = np.where(first, grp, 0)
        # 每个位置所属连续段的长度
        seg_len = np.zeros_like(grp)
        cnt = np.zeros(C.shape[0], dtype=np.int32)
        for j in range(1, C.shape[1]):
            same = (gid[:, j] == gid[:, j - 1]) & (grp[:, j] == grp[:, j - 1])
            cnt = np.where(same, cnt + 1, 1)
            seg_len[:, j] = cnt
        self.up_streak = (up * seg_len).astype(np.int16)

        # ---- 上一日收盘 ----
        prevC = np.full_like(C, np.nan)
        prevC[:, 1:] = C[:, :-1]
        self.prevC = prevC

        # ---- 形状自检：任何因子错位都会立刻暴露 ----
        for nm in (
            "ma5", "ma10", "ma20", "ma60", "vma5", "vma20", "amt20", "amt_yuan", "vr",
            "hh20", "ll20", "dist_high20", "pos20", "breakout20", "vol20",
            "r5", "r10", "r20", "r60", "body", "close_pos", "upper_shadow",
            "prevC", "ma20_slope",
        ):
            assert getattr(self, nm).shape == C.shape, (
                f"{nm} shape {getattr(self, nm).shape} != {C.shape}"
            )

    # ---------- 组合打分 ----------
    def score(self, j, cfg, fund_flow=None):
        """在信号日 j 计算每个股票的分数。"""
        c = cfg
        s = np.full(self.C.shape[0], np.nan)

        # 基础可交易性
        ok = np.isfinite(self.C[:, j]) & (self.C[:, j] > 0)
        # 流动性门槛
        ok &= np.nan_to_num(self.turnover[:, j], nan=0) > c["min_amount"]
        # 波动率上限（剔除妖股）
        ok &= np.nan_to_num(self.vol20[:, j], nan=9) < c["max_vol"]
        # 上市天数 >= 60（次新股波动大）
        ok &= self._listing_ok(c["min_listed_days"])
        # 不买 ST / 退市
        ok &= self._st_ok(c["exclude_st"])
        # 不买当天涨停（买不进）
        ok &= ~self._is_limit_up_today(j)

        # 各因子打分（截面 rank 归一，方向已在函数内处理）
        parts = []

        if c.get("w_breakout", 0) > 0:
            x = self.breakout20[:, j].astype(float)
            parts.append(c["w_breakout"] * _rank(x))

        if c.get("w_vr", 0) > 0:
            # 量比在 1.2~2.5 之间最优：score = -|log(vr/1.8)|
            x = -np.abs(np.log(np.nan_to_num(self.vr[:, j], nan=1.0) / 1.8))
            parts.append(c["w_vr"] * _rank(x))

        if c.get("w_r5", 0) > 0:
            parts.append(c["w_r5"] * _rank(self.r5[:, j]))

        if c.get("w_r20", 0) > 0:
            parts.append(c["w_r20"] * _rank(self.r20[:, j]))

        if c.get("w_r60", 0) > 0:
            parts.append(c["w_r60"] * _rank(self.r60[:, j]))

        if c.get("w_bull", 0) > 0:
            x = self.bull_stack[:, j].astype(float) * 2 + self.above_ma20[
                :, j
            ].astype(float)
            parts.append(c["w_bull"] * _rank(x))

        if c.get("w_maslp", 0) > 0:
            parts.append(c["w_maslp"] * _rank(self.ma20_slope[:, j]))

        if c.get("w_pos", 0) > 0:
            # 位置偏好 0.7~0.98：太低了没启动，太高追高
            x = -np.abs(self.pos20[:, j] - 0.88)
            parts.append(c["w_pos"] * _rank(x))

        if c.get("w_body", 0) > 0:
            parts.append(c["w_body"] * _rank(self.body[:, j]))

        if c.get("w_closepos", 0) > 0:
            parts.append(c["w_closepos"] * _rank(self.close_pos[:, j]))

        if c.get("w_shadow", 0) > 0:
            # 上影线惩罚
            parts.append(c["w_shadow"] * _rank(-self.upper_shadow[:, j]))

        if c.get("w_rs", 0) > 0 and hasattr(self, "rs20"):
            parts.append(c["w_rs"] * _rank(self.rs20[:, j]))

        if c.get("w_fflow", 0) > 0 and fund_flow is not None:
            parts.append(c["w_fflow"] * _rank(fund_flow))

        s = np.sum(parts, axis=0)
        s[~ok] = np.nan

        # 剔除极小市值 / 垃圾股
        if c.get("min_amount", 0) > 0:
            s[np.nan_to_num(self.turnover[:, j], nan=0) < c["min_amount"]] = np.nan
        return s

    # ---- 辅助 ----
    def _listing_ok(self, min_days):
        if not hasattr(self, "_listed_arr"):
            # 用数据首日近似：本地无完整上市历史，用上市日期字符串与 dates 比较
            pass
        return self._listed_mask

    def _st_ok(self, exclude_st):
        return self._st_mask if exclude_st else np.ones(self.C.shape[0], bool)

    def _is_limit_up_today(self, j):
        if j < 1:
            return np.zeros(self.C.shape[0], bool)
        return np.nan_to_num(self.C[:, j] / self.prevC[:, j] - 1, nan=0) > 0.095

    def set_universe(self, listed_mask, st_mask):
        self._listed_mask = listed_mask
        self._st_mask = st_mask


def _rank(x):
    """截面 percentile rank，NaN 保持 NaN。输出 -1..1。"""
    x = np.asarray(x, dtype=float)
    valid = np.isfinite(x)
    out = np.full_like(x, np.nan)
    nv = int(valid.sum())
    if nv < 2:
        return out
    xv = x[valid]
    order = xv.argsort(kind="mergesort")
    ranks = np.empty(nv)
    ranks[order] = np.arange(nv, dtype=float)
    # 处理并列：取平均秩
    _, inv, counts = np.unique(xv, return_inverse=True, return_counts=True)
    sums = np.zeros(len(counts))
    np.add.at(sums, inv, ranks)
    ranks = (sums / counts)[inv]
    out[valid] = (ranks / (nv - 1)) * 2 - 1
    return out
