# Positions 表数据陷阱与维护指南

> 适用对象: 维护 / 持仓 Tab 的开发者
> 触发日期: 2026-06-11 (rebuild_positions.py 重建, 修复 604 只持仓)

## 1. 三表关系

```
trades (流水)          positions (持仓)         position_reconcile_log (对账日志)
  applied=1/0/-1         closed_at IS NULL
                         closed_at IS NOT NULL  ← 已清仓
```

- **trades**: 每次成交 1 行, `applied=1` 表示已应用到持仓
- **positions**: 当前持有(closed_at NULL)或已清仓(closed_at NOT NULL)
- **position_reconcile_log**: 每次 reconcile 发现 drift 时写一行

## 2. 数据来源

- `trades` 的 buy 累加 = 建仓/加仓
- `trades` 的 sell 按 FIFO 扣减 = 清仓/减仓
- 每次 `_apply_one_trade` 调 `_upsert_position`:
  - 买: 找现存未清仓 position, 找到则合并(加权平均 cost, 累加 shares); 找不到则 INSERT 新建
  - 卖: FIFO 扣现存 position(可能关闭)

## 3. 已知陷阱(2026-06-11 踩坑)

### 3.1 历史 trade 没建仓,后来新加仓只显示最新一笔

**症状**: `trades` 表里有 N 笔 buy 累计 X 股, `positions` 表只有 1 条, shares=100, cost=最后一笔价格

**根因**:
1. 早期某次 apply 阶段, 某些老 trade 走完 `_apply_one_trade` 但**没正确建仓** (可能 FIFO 错配 / sell 卖超 / 老 bug)
2. 这些 trade 被标记 `applied=1`, 系统以为成功了
3. 后续新加仓时 `_upsert_position` 查 `closed_at IS NULL` 找不到 → 走"新增"分支, **只 INSERT 最新一笔** (100 股, 最后一笔价格)
4. 累计 buy/sell 历史完全跟 position 断开

**触发条件**:
- 中间有"手动 close position"(绕过 sell trade)
- 早期 apply 时有 trade 的 `trade_date` 为空 (排序错乱, 跳过)
- FIFO 卖超时 `applied=1` 仍被打标, 仓位"消失"

### 3.2 `_reconcile_position` 治不了历史脏数据

`apply_trades` 调 `_reconcile_position` 跑 self-heal, 但只看 **latest closed_at 之后的活跃窗口**。
**已关仓位**(closed_at NOT NULL) 的 FIFO 错误不会重新对账。

这意味着:
- ✅ **新增** drift 会被自愈
- ❌ **历史** drift 不会被发现

## 4. 如何避免

### 4.1 日常操作 (用户)

1. **清仓一定用"导入成交"导入 sell 记录**, 不要在持仓页手动改 `shares=0` 或 `closed_at`
2. **改 position 后**: 跑一次 apply (`/api/trades/apply`), 让 trades 跟 position 重新对账
3. **每周跑一次健康检查**:
   ```bash
   cd backend
   python3 rebuild_positions.py --dry-run
   ```
   看差异, 差异大就 `--apply --yes`

### 4.2 代码 (开发者)

1. **新增** `/api/positions/health` 端点: 跑完整 FIFO 重算所有股票, 跟 positions 对比, 返回 drift 列表 (供前端展示)
2. **前端**: "持仓" Tab 顶部加红色告警条, 当 health 检查有 drift 时显示
3. **改 `_reconcile_position`**: 关闭 "只看活跃窗口" 的限制, 对每只股票算完整 FIFO (类似 `rebuild_positions.py`)

## 5. 重建工具使用

### 5.1 dry-run (只查不修)

```bash
python3 rebuild_positions.py --dry-run
```

输出: 604+ 只会变更, 显示新旧对比。**不动 DB**。

### 5.2 apply (清空 + 重建)

```bash
python3 rebuild_positions.py --apply --yes
```

操作:
1. 备份 `positions` 表到 `positions_backup_YYYYMMDD_HHMMSS.json`
2. 清空 `positions` 表
3. 从 `trades` 按 FIFO 完整重算, 写入新 rows

**风险**: 丢失 `position_type` (建仓/加仓/清仓 区分) 字段, 重建后全部标记 `建仓`。如果用户依赖该字段, 需要后续手工区分。

### 5.3 字段重算口径

- **shares** = `SUM(buy) - SUM(sell)`
- **cost_price** = 加权平均: `SUM(price * shares) / SUM(shares)` (买入口径, 不扣 sell)
- **realized_pnl** = 累计: `(sell_price - avg_cost) * sold_shares - sell_fees`
- **original_shares** = 当前 shares (重建后无法区分"加仓前底仓" vs "加仓部分")
- **buy_date** = 最早一笔 buy 的 trade_date

## 6. 备份与回滚

重建前会备份到 `backend/positions_backup_YYYYMMDD_HHMMSS.json`。

**回滚**:
```python
import json, sqlite3
c = sqlite3.connect('market_data.db')
cur = c.cursor()
cur.execute('DELETE FROM positions')
for r in json.load(open('backend/positions_backup_YYYYMMDD_HHMMSS.json')):
    cols = ','.join(r.keys())
    placeholders = ','.join('?' * len(r))
    cur.execute(f'INSERT INTO positions ({cols}) VALUES ({placeholders})', list(r.values()))
c.commit()
```

## 7. 已知数据偏差

rebuild 重建后, 仍可能有**残余**的"信息丢失":
- `position_type` 字段: 全部标 `建仓` (无法区分)
- `note` 字段: 重建后会清空 (trades 表的 trade_no / 价格 写在 positions.note 但重建版不复制)
- `first_buy_amount` / `add_count`: 重建版写入 (`add_count = buy_n - 1`), 但**历史 add_count 可能不同** (用户已经 rebuild 过, 改不了)

如果用户对这些字段敏感, 建议:
- 跑 rebuild 前先 JSON dump 整个 `positions` 表
- 重建后再做差异对比
