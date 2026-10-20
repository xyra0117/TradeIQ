"""把回测需要的字段从 3.9G 的 market_data.db 抽成紧凑 npz 缓存。
只抽 2025-04-01 之后（数据最密集，368 个交易日）的深历史个股。
"""
import sqlite3
import time

import numpy as np

START = "20250401"
DB = "../market_data.db"

t0 = time.time()
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
cur = con.cursor()

# 只保留深历史个股：>=300 根日线
cur.execute(
    "SELECT ts_code, COUNT(*) n FROM stock_daily "
    "WHERE trade_date >= ? GROUP BY ts_code HAVING n >= 300",
    (START,),
)
codes = [r[0] for r in cur.fetchall()]
print(f"deep stocks: {len(codes)}")

rows_d = []
code_pos = {c: i for i, c in enumerate(codes)}
for i in range(0, len(codes), 400):
    chunk = codes[i : i + 400]
    ph = ",".join("?" * len(chunk))
    cur.execute(
        "SELECT ts_code, trade_date, open, high, low, close, volume, amount "
        f"FROM stock_daily WHERE trade_date >= ? AND ts_code IN ({ph})",
        (START, *chunk),
    )
    for r in cur.fetchall():
        rows_d.append((code_pos[r[0]], r[1], r[2], r[3], r[4], r[5], r[6], r[7]))
    print(f"  {i + len(chunk)}/{len(codes)}", flush=True)

date_set = sorted({r[1] for r in rows_d})
date_pos = {d: j for j, d in enumerate(date_set)}
n_c, n_d = len(codes), len(date_set)
print(f"matrix {n_c} codes x {n_d} dates = {n_c * n_d / 1e6:.1f}M cells")

O = np.full((n_c, n_d), np.nan, np.float32)
H = np.full((n_c, n_d), np.nan, np.float32)
L = np.full((n_c, n_d), np.nan, np.float32)
C = np.full((n_c, n_d), np.nan, np.float32)
V = np.full((n_c, n_d), np.nan, np.float32)
A = np.full((n_c, n_d), np.nan, np.float32)
for ci, d, o, h, l, c, v, a in rows_d:
    j = date_pos[d]
    O[ci, j] = o
    H[ci, j] = h
    L[ci, j] = l
    C[ci, j] = c
    V[ci, j] = v
    A[ci, j] = a

# 股票静态信息：名称、市场、上市日、状态
cur.execute("SELECT ts_code, name, listing_date, market, status FROM stock_basic")
meta = {}
for ts, nm, ld, mk, stt in cur.fetchall():
    i = code_pos.get(ts)
    if i is not None:
        meta[i] = (nm or "", ld or "", mk or "", stt or "")


def col(k):
    return np.array([meta.get(i, ("", "", "", ""))[k] for i in range(n_c)])


np.savez_compressed(
    "_cache.npz",
    codes=np.array(codes),
    dates=np.array(date_set),
    O=O, H=H, L=L, C=C, V=V, A=A,
    meta_code=col(0),
    meta_list=col(1),
    meta_mkt=col(2),
    meta_stt=col(3),
)
print(f"saved _cache.npz in {time.time() - t0:.0f}s")
