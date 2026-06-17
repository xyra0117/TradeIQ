"""
拉取今日全市场个股主力净流入数据并导出 Excel。

【方案 A - JSONP】在 Playwright 浏览器 context 里用 JSONP 加载 push2 接口
为什么 JSONP:
  - 东财 push2 接口本身就是 JSONP 格式（cb=jQuery...({...})）
  - JSONP 通过 <script> 加载，不受 CORS 限制（fetch 被 CORS 拦）
  - 完全不需要 click 翻页，0 风控风险
  - 53 页 ≈ 2 分钟跑完

数据源: push2.eastmoney.com clist/get
排序:  fid=f62 (主力净流入金额降序)
每页:  pz=100 (东财上限)

字段:
  f62  主力净流入金额（元）- 主力 = 超大单+大单
  f267 主力净流入（另一口径，东财主力净额）
  f268 主力净流入占比 %
  f269/f270 超大单净流入金额/占比
  f271/f272 大单净流入金额/占比
  f273/f274 中单净流入金额/占比
  f275/f276 小单净流入金额/占比
  f12/f14 代码/名称, f2 最新价, f3 涨跌幅, f100 板块, f124 时间戳
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
from playwright.sync_api import sync_playwright


# ---------- 配置 ----------
# detail.html 默认 URL（fid=f62 按主力净流入金额排序，含"净额"口径分档）
PUSH2_URL_BASE = (
    "https://push2.eastmoney.com/api/qt/clist/get?"
    "fid=f62&po=1&pz=100&np=1&fltt=2&invt=2"
    "&ut=8dec03ba335b81bf4ebdf7b29ec27d15"
    "&fs=m%3A0%2Bt%3A6%2Bf%3A%212%2Cm%3A0%2Bt%3A13%2Bf%3A%212%2Cm%3A0%2Bt%3A80%2Bf%3A%212%2Cm%3A1%2Bt%3A2%2Bf%3A%212%2Cm%3A1%2Bt%3A23%2Bf%3A%212%2Cm%3A0%2Bt%3A7%2Bf%3A%212%2Cm%3A1%2Bt%3A3%2Bf%3A%212"
    "&fields=f12%2Cf14%2Cf2%2Cf3%2Cf62%2Cf184%2Cf66%2Cf69%2Cf72%2Cf75%2Cf78%2Cf81%2Cf84%2Cf87%2Cf204%2Cf205%2Cf124%2Cf1%2Cf13"
)

FIELD_MAP = [
    ("f12", "代码"),
    ("f14", "名称"),
    ("f2", "最新价"),
    ("f3", "涨跌幅"),
    ("f62", "主力净流入"),
    ("f184", "主力净流入占比"),
    ("f66", "超大单净额"),
    ("f69", "超大单净额占比"),
    ("f72", "大单净额"),
    ("f75", "大单净额占比"),
    ("f78", "中单净额"),
    ("f81", "中单净额占比"),
    ("f84", "小单净额"),
    ("f87", "小单净额占比"),
    ("f204", "主力净流入(口径2)"),
    ("f205", "主力净流入占比(口径2)"),
    ("f1", "市场"),
    ("f13", "市场代码"),
]

WAIT_MS_PER_PAGE = 1500  # 每页间隔（JSONP 加载完一般 <500ms）
PAGE_URL = "https://data.eastmoney.com/zjlx/detail.html"  # 正确页面：按金额排序+净额口径
OUTPUT_DIR = Path(__file__).parent.parent


def fetch_page_jsonp(page, pn: int) -> Optional[dict]:
    """在浏览器 context 里用 JSONP 加载第 pn 页数据。返回 data dict 或 None。"""
    js = """
    (pn) => {
        return new Promise((resolve, reject) => {
            const cb = 'jjsonp_' + Date.now() + '_' + pn;
            window[cb] = (data) => {
                try { delete window[cb]; } catch(e) { window[cb] = undefined; }
                if (script.parentNode) script.parentNode.removeChild(script);
                resolve({ok: true, data: data});
            };
            const script = document.createElement('script');
            const url = '%s' + '&pn=' + pn + '&cb=' + cb;
            script.src = url;
            script.onerror = (e) => {
                try { delete window[cb]; } catch(e2) {}
                if (script.parentNode) script.parentNode.removeChild(script);
                resolve({ok: false, err: 'script load failed'});
            };
            document.head.appendChild(script);
            setTimeout(() => {
                if (window[cb] !== undefined) {
                    try { delete window[cb]; } catch(e) {}
                    if (script.parentNode) script.parentNode.removeChild(script);
                    resolve({ok: false, err: 'timeout 15s'});
                }
            }, 15000);
        });
    }
    """ % PUSH2_URL_BASE
    result = page.evaluate(js, pn)
    if result and result.get("ok"):
        return result.get("data")
    return None


def fetch_today_market(progress_callback=None, headless: bool = False, verbose: bool = True) -> tuple:
    """JSONP 循环拉全市场数据 (lib API, 给 Flask 后台调用).

    Args:
        progress_callback: 可选回调, 签名 (stage: str, **kwargs).
            - stage="total", total=N
            - stage="page", pn=N, total_pages=N, rows_count=M, failed=[...]
            - stage="done", rows=M, total=N, failed=[...]
        headless: Playwright 启动模式
        verbose: 是否 print 到 stderr (后台调用设 False)

    Returns:
        (rows: list[dict], total: int, failed_pages: list[int])
        rows 是 detail.html 原始 f-code dict (未做中文/英文列名转换)
    """
    all_rows = []
    failed_pages = []
    total = 0
    total_pages = 0

    def _emit(stage, **kw):
        if progress_callback:
            try:
                progress_callback(stage, **kw)
            except Exception:
                pass

    _emit("start")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel="chrome")
        page = browser.new_page(viewport={"width": 1920, "height": 1080})

        if verbose:
            print(f"[{datetime.now():%H:%M:%S}] 打开页面建立 session...")
        page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        # 先拿第 1 页看 total
        if verbose:
            print("拉第 1 页...")
        data = fetch_page_jsonp(page, 1)
        if not data or not data.get("data"):
            if verbose:
                print("FAIL: 第 1 页拿不到数据", file=sys.stderr)
            _emit("error", message="第 1 页拿不到数据")
            browser.close()
            return [], 0, list(range(1, 54))

        total = data["data"].get("total", 0)
        diff = data["data"].get("diff") or []
        all_rows.extend(diff)
        if verbose:
            print(f"  total={total}, 第 1 页 {len(diff)} 行")
        _emit("total", total=total)

        if total == 0:
            browser.close()
            _emit("done", rows=0, total=0, failed=list(range(2, 54)))
            return [], 0, list(range(2, 54))

        # 计算总页数
        import math
        total_pages = math.ceil(total / 100)
        if verbose:
            print(f"  总页数: {total_pages}（{total} 只）")

        # 循环拉剩余页
        for pn in range(2, total_pages + 1):
            page.wait_for_timeout(WAIT_MS_PER_PAGE)
            data = fetch_page_jsonp(page, pn)
            if not data or not data.get("data") or not data["data"].get("diff"):
                failed_pages.append(pn)
                if verbose:
                    print(f"  pn={pn}: 无数据", file=sys.stderr)
                _emit("page", pn=pn, total_pages=total_pages, rows_count=len(all_rows), failed=failed_pages[:])
                continue
            all_rows.extend(data["data"]["diff"])
            if verbose and (pn % 5 == 0 or pn == total_pages):
                print(f"  pn={pn}/{total_pages}, 累计 {len(all_rows)} 行")
            _emit("page", pn=pn, total_pages=total_pages, rows_count=len(all_rows), failed=failed_pages[:])

        browser.close()

    if verbose:
        print(f"\n抓取完成: {len(all_rows)} 行")
        if failed_pages:
            print(f"⚠️ 失败页: {failed_pages[:20]}{'...' if len(failed_pages) > 20 else ''}")
    _emit("done", rows=len(all_rows), total=total, failed=failed_pages[:])
    return all_rows, total, failed_pages


# ===== 兼容旧名: fetch_all 仍然可用, 但走新函数 =====
def fetch_all(headless: bool = False) -> pd.DataFrame:
    """旧 CLI 入口, 内部用 fetch_today_market. 保留向后兼容."""
    rows, _, _ = fetch_today_market(progress_callback=None, headless=headless, verbose=True)
    return rows_to_df(rows)


def rows_to_df(rows: list) -> pd.DataFrame:
    decoded = []
    for r in rows:
        rec = {}
        for k, zh in FIELD_MAP:
            v = r.get(k, "-")
            if v == "-" or v == "":
                v = None
            rec[zh] = v
        decoded.append(rec)
    df = pd.DataFrame(decoded)
    # 数值化
    numeric_cols = [
        "最新价", "涨跌幅", "主力净流入", "主力净流入占比",
        "主力净流入(口径2)", "主力净流入占比(口径2)",
        "超大单净额", "超大单净额占比",
        "大单净额", "大单净额占比",
        "中单净额", "中单净额占比",
        "小单净额", "小单净额占比",
    ]
    for c in numeric_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    # 主力净流入降序
    df = df.sort_values("主力净流入", ascending=False, na_position="last").reset_index(drop=True)
    df.insert(0, "排名", range(1, len(df) + 1))
    return df


def export_excel(df: pd.DataFrame, out_path: Path, top: Optional[int] = None):
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_to_write = df.head(top) if top else df

    with pd.ExcelWriter(out_path, engine="openpyxl") as writer:
        df_to_write.to_excel(writer, sheet_name="主力净流入", index=False)
        notes = pd.DataFrame({
            "项": ["数据源", "接口", "排序字段", "抓取时间", "口径", "金额字段", "完整度"],
            "值": [
                "东方财富 push2.eastmoney.com",
                "clist/get (fid=f62 按主力净流入金额降序, pz=100)",
                "f62 = 主力净流入金额（主力=超大单+大单 净流入）",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "正数=主力净流入，负数=主力净流出；东财口径",
                "f62 主力净额、f267 主力净额(口径2)、f269 超大单、f271 大单、f273 中单、f275 小单（单位均为元）",
                f"本次抓到 {len(df)} 行（A 股共约 5300 只）",
            ],
        })
        notes.to_excel(writer, sheet_name="说明", index=False)

        ws = writer.sheets["主力净流入"]
        ws.freeze_panes = "A2"
        widths = {
            "排名": 6, "代码": 10, "名称": 12, "最新价": 10, "涨跌幅": 10,
            "主力净流入": 18, "主力净流入占比": 12,
            "主力净流入(口径2)": 18, "主力净流入占比(口径2)": 12,
            "超大单净额": 18, "超大单净额占比": 12,
            "大单净额": 18, "大单净额占比": 12,
            "中单净额": 18, "中单净额占比": 12,
            "小单净额": 18, "小单净额占比": 12,
            "市场": 8, "市场代码": 10,
        }
        for col_idx, header in enumerate(df_to_write.columns, start=1):
            ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = widths.get(header, 12)
        # 数值格式
        for col_idx, header in enumerate(df_to_write.columns, start=1):
            for row_idx in range(2, len(df_to_write) + 2):
                cell = ws.cell(row=row_idx, column=col_idx)
                if header in ["主力净流入", "主力净流入(口径2)", "超大单净额", "大单净额", "中单净额", "小单净额"]:
                    cell.number_format = '#,##0.00,,"万"'
                elif header in ["最新价", "主力净流入占比", "主力净流入占比(口径2)", "涨跌幅",
                                 "超大单净额占比", "大单净额占比", "中单净额占比", "小单净额占比"]:
                    cell.number_format = "0.00"
        # 涨跌幅/主力净流入 条件格式（红涨绿跌）
        from openpyxl.formatting.rule import CellIsRule
        from openpyxl.styles import PatternFill, Font
        red = PatternFill(start_color="FFE5E5", end_color="FFE5E5", fill_type="solid")
        green = PatternFill(start_color="E5F5E5", end_color="E5F5E5", fill_type="solid")
        for h in ["涨跌幅", "主力净流入", "超大单净额", "大单净额"]:
            if h in df_to_write.columns:
                col_idx = list(df_to_write.columns).index(h) + 1
                col_letter = ws.cell(row=1, column=col_idx).column_letter
                last_row = len(df_to_write) + 1
                ws.conditional_formatting.add(f"{col_letter}2:{col_letter}{last_row}",
                                               CellIsRule(operator="greaterThan", formula=["0"], fill=red))
                ws.conditional_formatting.add(f"{col_letter}2:{col_letter}{last_row}",
                                               CellIsRule(operator="lessThan", formula=["0"], fill=green))
        # 表头加粗
        for col_idx in range(1, len(df_to_write.columns) + 1):
            ws.cell(row=1, column=col_idx).font = Font(bold=True)
        # 说明 sheet 格式
        notes_ws = writer.sheets["说明"]
        notes_ws.column_dimensions["A"].width = 16
        notes_ws.column_dimensions["B"].width = 95
        for col_idx in range(1, 3):
            notes_ws.cell(row=1, column=col_idx).font = Font(bold=True)
        for row_idx in range(2, len(notes) + 2):
            notes_ws.cell(row=row_idx, column=1).font = Font(bold=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=None, help="只导出前 N 名")
    ap.add_argument("--out", type=str, default=None, help="输出 xlsx 路径")
    ap.add_argument("--headless", action="store_true", help="无头模式（默认有头）")
    args = ap.parse_args()

    today = datetime.now().strftime("%Y%m%d_%H%M")
    default_out = OUTPUT_DIR / f"主力净流入_{today}.xlsx"
    out_path = Path(args.out) if args.out else default_out

    print(f"[{datetime.now():%H:%M:%S}] 开始拉取（Playwright + JSONP, fid=f62 按金额排序）...")
    df = fetch_all(headless=args.headless)
    if df.empty:
        print("FAIL: 数据为空", file=sys.stderr)
        sys.exit(1)

    export_excel(df, out_path, top=args.top)
    print(f"\n✓ 已导出: {out_path}")
    print(f"  行数: {len(df)}, 字段: {list(df.columns)}")
    print("\n主力净流入 Top 5:")
    print(df[["排名", "代码", "名称", "最新价", "涨跌幅", "主力净流入", "主力净流入占比", "超大单净额", "大单净额"]].head(5).to_string(index=False))
    print("\n主力净流入 Bottom 5（资金流出最大）:")
    print(df[["排名", "代码", "名称", "最新价", "涨跌幅", "主力净流入", "主力净流入占比", "超大单净额", "大单净额"]].tail(5).to_string(index=False))


if __name__ == "__main__":
    main()