#!/bin/bash
# 从飞书多维表格获取A股指数行情 + 阶段涨幅榜单(5日/10日/20日)数据
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_TOKEN="VH8IbJOUxaqwcXsIWvGctjIFnAg"

echo "=== Fetching index data ==="
lark-cli base +record-list \
  --base-token "$BASE_TOKEN" \
  --table-id tbl575UrKmtfyxO2 \
  --view-id vewj7n3Y1G \
  --limit 200 \
  --as user 2>/dev/null | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
records = []
header_done = False
for line in lines:
    line = line.strip()
    if not line.startswith('|'): continue
    if '---' in line: continue
    if '_record_id' in line: header_done = True; continue
    if not header_done: continue
    if 'Meta:' in line: continue
    cols = [c.strip() for c in line.split('|')]
    if len(cols) < 7: continue
    try:
        records.append({
            'date': cols[2], 'name': cols[3],
            'close': float(cols[4]), 'change': float(cols[5]),
            'volume': float(cols[6])
        })
    except (ValueError, IndexError): continue
with open('$SCRIPT_DIR/data-index.json', 'w', encoding='utf-8') as f:
    json.dump(records, f, ensure_ascii=False, indent=2)
print(f'  Index: {len(records)} records')
"

echo "=== Fetching leaderboard data (5d/10d/20d) ==="
python3 -c "
import subprocess, json, re

BASE = '$BASE_TOKEN'
TABLE = 'tbl4EsJJ0hNQEheM'
VIEWS = {
    '5日':  'vewb73vUMT',
    '10日': 'vews5227Ol',
    '20日': 'vewXwFynSI',
}

all_records = []
for period, vid in VIEWS.items():
    result = subprocess.run(
        ['lark-cli', 'base', '+record-list',
         '--base-token', BASE, '--table-id', TABLE,
         '--view-id', vid, '--limit', '200', '--as', 'user'],
        capture_output=True, text=True
    )
    lines = result.stdout.strip().split('\n')
    header_done = False
    count = 0
    for line in lines:
        line = line.strip()
        if not line.startswith('|'): continue
        if '---' in line: continue
        if '_record_id' in line: header_done = True; continue
        if not header_done: continue
        if 'Meta:' in line: continue
        cols = [c.strip() for c in line.split('|')]
        if len(cols) < 10: continue
        rank_match = re.search(r'TOP\s*(\d+)', cols[4])
        rank = int(rank_match.group(1)) if rank_match else 0
        try:
            all_records.append({
                'date': cols[2], 'type': cols[3], 'rank': rank,
                'code': cols[5], 'name': cols[6],
                'change': float(cols[7]), 'volume': float(cols[8]),
                'reason': cols[9]
            })
            count += 1
        except (ValueError, IndexError): continue
    print(f'  {period}: {count} records')

with open('$SCRIPT_DIR/data-leaderboard.json', 'w', encoding='utf-8') as f:
    json.dump(all_records, f, ensure_ascii=False, indent=2)
print(f'  Total: {len(all_records)} records')
"

echo "=== Fetching market overview data ==="
lark-cli base +record-list \
  --base-token "$BASE_TOKEN" \
  --table-id tbl1OCE0YzUNKQwi \
  --view-id vew7ZuDG4O \
  --limit 200 \
  --as user 2>/dev/null | python3 -c "
import sys, json
lines = sys.stdin.read().strip().split('\n')
records = []
header_done = False
for line in lines:
    line = line.strip()
    if not line.startswith('|'): continue
    if '---' in line: continue
    if '_record_id' in line: header_done = True; continue
    if not header_done: continue
    if 'Meta:' in line: continue
    cols = [c.strip() for c in line.split('|')]
    if len(cols) < 19: continue
    try:
        records.append({
            'date': cols[2],
            'totalVolume': float(cols[3]),
            'volumeDiff': float(cols[4]),
            'netFlow': float(cols[5]),
            'upCount': int(float(cols[6])),
            'downCount': int(float(cols[7])),
            'flatCount': int(float(cols[8])),
            'limitUp': int(float(cols[9])),
            'up7': int(float(cols[10])),
            'up5_7': int(float(cols[11])),
            'up3_5': int(float(cols[12])),
            'up0_3': int(float(cols[13])),
            'down7': int(float(cols[14])),
            'down5_7': int(float(cols[15])),
            'down3_5': int(float(cols[16])),
            'down0_3': int(float(cols[17])),
            'limitDown': int(float(cols[18]))
        })
    except (ValueError, IndexError): continue
with open('$SCRIPT_DIR/data-overview.json', 'w', encoding='utf-8') as f:
    json.dump(records, f, ensure_ascii=False, indent=2)
print(f'  Overview: {len(records)} records')
"

echo "=== Fetching limit-up data ==="
lark-cli base +record-list \
  --base-token "$BASE_TOKEN" \
  --table-id tblnDthZrPaxuPnd \
  --view-id vew1HTAjiq \
  --limit 200 \
  --as user 2>/dev/null | python3 -c "
import sys, json, re
lines = sys.stdin.read().strip().split('\n')
records = []
header_done = False
for line in lines:
    line = line.strip()
    if not line.startswith('|'): continue
    if '---' in line: continue
    if '_record_id' in line: header_done = True; continue
    if not header_done: continue
    if 'Meta:' in line: continue
    cols = [c.strip() for c in line.split('|')]
    if len(cols) < 11: continue
    # 板块字段格式: [\"机器人\"]
    sector_match = re.search(r'\"(.+?)\"', cols[7])
    sector = sector_match.group(1) if sector_match else cols[7]
    try:
        records.append({
            'date': cols[2], 'code': cols[3], 'name': cols[4],
            'marketCap': float(cols[5]), 'time': cols[6],
            'sector': sector, 'volume': float(cols[8]),
            'streak': cols[9], 'keyword': cols[10]
        })
    except (ValueError, IndexError): continue
with open('$SCRIPT_DIR/data-limitup.json', 'w', encoding='utf-8') as f:
    json.dump(records, f, ensure_ascii=False, indent=2)
print(f'  Limit-up: {len(records)} records')
"

echo "Done. Files saved to $SCRIPT_DIR/"
