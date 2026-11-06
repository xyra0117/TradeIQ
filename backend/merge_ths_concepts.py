"""把 backend/ths_concepts.db 里的同花顺概念数据合并进 market_data.db。

用 ATTACH + INSERT ... SELECT, 不重建表, 同名板块按覆盖写。
跑法:
  python3 merge_ths_concepts.py            # 合并
  python3 merge_ths_concepts.py --dry-run  # 只看数量
"""
import argparse
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_DB = os.path.join(HERE, 'market_data.db')
SRC_DB = os.path.join(HERE, 'ths_concepts.db')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default=SRC_DB)
    ap.add_argument('--db', default=MAIN_DB)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print(f'源库不存在: {args.src}')
        return 1

    conn = sqlite3.connect(args.db, timeout=60)   # 后端 Flask 进程占着写锁, 等它放锁
    conn.execute("ATTACH DATABASE ? AS src", (args.src,))
    src_concepts = conn.execute('SELECT COUNT(*) FROM src.ths_concept').fetchone()[0]
    src_stocks = conn.execute('SELECT COUNT(*) FROM src.ths_concept_stock').fetchone()[0]
    print(f'源库: {src_concepts} 个板块, {src_stocks} 条成分股')
    if args.dry_run:
        conn.close()
        return 0

    conn.execute("CREATE TABLE IF NOT EXISTS ths_concept ("
                 "concept_code TEXT PRIMARY KEY, name TEXT NOT NULL,"
                 " board_type TEXT DEFAULT 'concept', stock_count INTEGER DEFAULT 0,"
                 " page_count INTEGER DEFAULT 0, updated_at TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS ths_concept_stock ("
                 "concept_code TEXT NOT NULL, ts_code TEXT NOT NULL, name TEXT,"
                 " board_type TEXT DEFAULT 'concept',"
                 " close REAL, change_pct REAL, change_amt REAL, speed REAL, turnover_pct REAL,"
                 " volume_ratio REAL, amplitude REAL, amount REAL, float_share REAL,"
                 " float_mktcap REAL, pe REAL, updated_at TEXT,"
                 " PRIMARY KEY (concept_code, ts_code))")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ths_concept_stock_code ON ths_concept_stock(ts_code)')

    conn.execute('INSERT OR REPLACE INTO ths_concept '
                 '(concept_code, name, board_type, stock_count, page_count, updated_at) '
                 'SELECT concept_code, name, COALESCE(board_type,\'concept\'), stock_count, '
                 'page_count, updated_at FROM src.ths_concept')
    conn.execute('INSERT OR REPLACE INTO ths_concept_stock '
                 '(concept_code, ts_code, name, board_type, close, change_pct, change_amt, speed,'
                 ' turnover_pct, volume_ratio, amplitude, amount, float_share, float_mktcap, pe,'
                 ' updated_at) '
                 'SELECT concept_code, ts_code, name, COALESCE(board_type,\'concept\'), close, '
                 'change_pct, change_amt, speed, turnover_pct, volume_ratio, amplitude, amount, '
                 'float_share, float_mktcap, pe, updated_at FROM src.ths_concept_stock')
    conn.commit()

    main_concepts = conn.execute('SELECT COUNT(*) FROM ths_concept').fetchone()[0]
    main_stocks = conn.execute('SELECT COUNT(*) FROM ths_concept_stock').fetchone()[0]
    conn.execute('DETACH DATABASE src')
    conn.close()
    print(f'合并完成: market_data.db 现有 {main_concepts} 个板块, {main_stocks} 条成分股')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
