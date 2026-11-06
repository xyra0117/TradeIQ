"""Bounded formula interpreter and local-data screens. No eval, writes or providers."""
import ast
import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime
from pathlib import Path
from threading import Lock

from dip_picks import _daily_context, _limit_up_price_for
from intraday_picks import picks_connection
from surge_picks import (_bar_valid, _reference_valid, _number, _flow_where,
                         flow_source_for_range, attach_sector_history)

FIELDS = {
    'open': ('开盘价', '元'), 'high': ('最高价', '元'), 'low': ('最低价', '元'),
    'close': ('收盘价', '元'), 'change': ('当日涨跌幅', '%'),
    'volume': ('成交量', '本地日线单位'), 'amount': ('成交额', '亿元'),
    'main_net': ('主力净额', '亿元'), 'super_net': ('超大单净额', '亿元'),
    'big_net': ('大单净额', '亿元'), 'mid_net': ('中单净额', '亿元'),
    'small_net': ('小单净额', '亿元'), 'amplitude': ('日振幅', '%'),
    'limit_up': ('收盘涨停', '真假'), 'up_count': ('上涨家数', '家'),
    'down_count': ('下跌家数', '家'), 'flat_count': ('平盘家数', '家'),
    'sh_change': ('上证涨跌幅', '%'),
}
FUNDS = {'main_net': 'main_net_inflow', 'super_net': 'super_net', 'big_net': 'big_net',
         'mid_net': 'mid_net', 'small_net': 'small_net'}
GLOBAL_FIELDS = {'up_count', 'down_count', 'flat_count', 'sh_change'}
WINDOWS = {'SUM', 'AVG', 'MAX', 'MIN', 'COUNT', 'ALL', 'ANY'}
FUNCTION_HELP = {
    'SUM(x,n[,offset])': '最近n个交易日合计', 'AVG(x,n[,offset])': '最近n日平均',
    'MAX(x,n[,offset])': '最近n日最大值', 'MIN(x,n[,offset])': '最近n日最小值',
    'REF(x,n)': 'n个交易日前的值', 'MA(n)': '包含当日的n日收盘均线',
    'RET(n)': '相对n个交易日前收盘的累计涨幅，单位%',
    'COUNT(条件,n[,offset])': '最近n日满足条件的天数',
    'ALL(条件,n[,offset])': '最近n日每天都满足', 'ANY(条件,n[,offset])': '最近n日至少一天满足',
    'CROSS(a,b)': '当前a>b且前一交易日a≤b', 'ABS(x)': '绝对值',
}
EXAMPLES = [
    ['收盘站上20日线', 'close > MA(20)'],
    ['缩量至前5日均量的1.2倍以下', 'volume / AVG(volume, 5, 1) < 1.2'],
    ['近10日主力流入至少7天', 'COUNT(main_net > 0, 10) >= 7'],
    ['连续3日缩量', 'ALL(volume < REF(volume, 1), 3)'],
    ['突破此前20日最高价', 'close > MAX(high, 20, 1)'],
    ['5日主力强度超过8%', 'SUM(main_net, 5) / SUM(amount, 5) * 100 > 8'],
    ['均线多头排列', 'close > MA(5) > MA(10) > MA(20) > MA(60)'],
]
_scan_lock = Lock()
_config_lock = Lock()


def _read_saved_config(path):
    """读后端保存的条件配置。文件不存在/坏了当没有 (None), 不让同步把界面卡住."""
    try:
        with open(path, 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('config'), dict):
        return None
    return data


def _write_saved_config(path, payload):
    """原子写: 先写临时文件再 replace, 避免写到一半被另一台电脑读走."""
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _tri(values, mode):
    if mode == 'all':
        return False if False in values else None if None in values else True
    return True if True in values else None if None in values else False


def _compare(a, b, op):
    if a is None or b is None:
        return None
    equal = a == b if type(a) is bool else math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
    return (a > b and not equal if isinstance(op, ast.Gt) else a > b or equal if isinstance(op, ast.GtE) else
            a < b and not equal if isinstance(op, ast.Lt) else a < b or equal if isinstance(op, ast.LtE) else
            equal if isinstance(op, ast.Eq) else not equal)


class Formula:
    def __init__(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 1000:
            raise ValueError('公式不能为空，且不能超过1000个字符')
        text = text.strip().replace('≥', '>=').replace('≤', '<=').replace('≠', '!=')
        text = text.replace('&&', ' and ').replace('||', ' or ')
        text = re.sub(r'\b(AND|OR|NOT)\b', lambda m: m[0].lower(), text)
        try:
            self.tree = ast.parse(text, mode='eval').body
        except (SyntaxError, RecursionError):
            raise ValueError('公式语法不完整，请核对括号、比较符和函数参数') from None
        if len(list(ast.walk(self.tree))) > 160:
            raise ValueError('单条公式太长，请拆成多项条件')
        self.fields = set()
        kind, self.lookback = self._check(self.tree, 0)
        if kind != 'bool':
            raise ValueError('公式需要一个判断结果，例如 close > MA(20)，不能只有数值')
        if self.lookback > 250:
            raise ValueError('公式最远只能引用250个交易日前的数据')
        self.text = ast.unparse(self.tree)
        self.explanation = self._describe(self.tree)

    @staticmethod
    def _integer(node, minimum=0):
        if not isinstance(node, ast.Constant) or type(node.value) is not int or not minimum <= node.value <= 250:
            raise ValueError(f'周期和偏移需使用{minimum}至250的整数；不能引用未来')
        return node.value

    def _check(self, node, depth):
        if depth > 14:
            raise ValueError('公式嵌套过深，请拆成多项条件')
        check = lambda x: self._check(x, depth + 1)
        if isinstance(node, ast.Constant):
            if type(node.value) is bool:
                return 'bool', 0
            if type(node.value) in (int, float) and math.isfinite(node.value) and abs(node.value) <= 1e12:
                return 'num', 0
            raise ValueError('公式只接受有限数值和真假常量')
        if isinstance(node, ast.Name):
            if node.id not in FIELDS:
                raise ValueError(f'不支持字段“{node.id}”，请查看可用指标')
            self.fields.add(node.id)
            return ('bool' if node.id == 'limit_up' else 'num'), int(node.id in ('change', 'amplitude', 'limit_up'))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.Not, ast.USub, ast.UAdd)):
            kind, back = check(node.operand)
            expected = 'bool' if isinstance(node.op, ast.Not) else 'num'
            if kind != expected:
                raise ValueError('not用于条件，正负号用于数值')
            return kind, back
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, b = check(node.left), check(node.right)
            if a[0] != 'num' or b[0] != 'num':
                raise ValueError('加减乘除只能用于数值')
            return 'num', max(a[1], b[1])
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            parts = [check(n) for n in node.values]
            if any(k != 'bool' for k, _ in parts):
                raise ValueError('and / or两边必须是条件')
            return 'bool', max(b for _, b in parts)
        if isinstance(node, ast.Compare):
            if not all(isinstance(o, (ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq)) for o in node.ops):
                raise ValueError('仅支持 >、>=、<、<=、==、!= 比较')
            parts = [check(n) for n in [node.left, *node.comparators]]
            if len({k for k, _ in parts}) != 1:
                raise ValueError('不能把真假条件与数值比较')
            return 'bool', max(b for _, b in parts)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            fun, args = node.func.id, node.args
            if fun in WINDOWS and len(args) in (2, 3):
                kind, back = check(args[0])
                expected = 'bool' if fun in ('COUNT', 'ALL', 'ANY') else 'num'
                if kind != expected:
                    raise ValueError(f'{fun}第一项必须是' + ('条件' if expected == 'bool' else '数值'))
                n = self._integer(args[1], 1)
                off = self._integer(args[2]) if len(args) == 3 else 0
                return ('bool' if fun in ('ALL', 'ANY') else 'num'), back + n - 1 + off
            if fun == 'REF' and len(args) == 2:
                kind, back = check(args[0])
                return kind, back + self._integer(args[1])
            if fun in ('MA', 'RET') and len(args) == 1:
                n = self._integer(args[0], 1)
                self.fields.add('close')
                return 'num', n - 1 if fun == 'MA' else n
            if fun == 'ABS' and len(args) == 1:
                kind, back = check(args[0])
                if kind != 'num':
                    raise ValueError('ABS需要数值')
                return kind, back
            if fun == 'CROSS' and len(args) == 2:
                parts = [check(n) for n in args]
                if any(k != 'num' for k, _ in parts):
                    raise ValueError('CROSS需要两个数值指标')
                return 'bool', max(b for _, b in parts) + 1
            raise ValueError(f'函数“{fun}”或参数不支持，请查看公式说明')
        raise ValueError('公式仅支持指标、数值、比较、四则运算和列明的函数')

    def _describe(self, node):
        d = self._describe
        if isinstance(node, ast.Name):
            label, unit = FIELDS[node.id]
            return label + (f'（{unit}）' if unit in ('亿元', '%') else '')
        if isinstance(node, ast.Constant):
            return str(node.value) if type(node.value) is not bool else '是' if node.value else '否'
        if isinstance(node, ast.UnaryOp):
            return ('不满足 ' if isinstance(node.op, ast.Not) else '-' if isinstance(node.op, ast.USub) else '+') + '(' + d(node.operand) + ')'
        if isinstance(node, ast.BinOp):
            signs = {ast.Add: '+', ast.Sub: '−', ast.Mult: '×', ast.Div: '÷'}
            return f'({d(node.left)} {signs[type(node.op)]} {d(node.right)})'
        if isinstance(node, ast.Compare):
            signs = {ast.Gt: '>', ast.GtE: '≥', ast.Lt: '<', ast.LtE: '≤', ast.Eq: '=', ast.NotEq: '≠'}
            return d(node.left) + ''.join(f' {signs[type(op)]} {d(n)}' for op, n in zip(node.ops, node.comparators))
        if isinstance(node, ast.BoolOp):
            return (' 且 ' if isinstance(node.op, ast.And) else ' 或 ').join('(' + d(n) + ')' for n in node.values)
        fun, a = node.func.id, node.args
        if fun in WINDOWS:
            off = a[2].value if len(a) == 3 else 0
            period = f'最近{a[1].value}个交易日' if not off else f'此前{a[1].value}个交易日（不含当日）' if off == 1 else f'截至{off}个交易日前的{a[1].value}日'
            actions = {'SUM': '合计', 'AVG': '平均', 'MAX': '最大值', 'MIN': '最小值', 'COUNT': '符合天数', 'ALL': '每天均满足', 'ANY': '至少一天满足'}
            return f'{period} [{d(a[0])}] 的{actions[fun]}'
        if fun == 'REF':
            return f'上个交易日的 [{d(a[0])}]' if a[1].value == 1 else f'{a[1].value}个交易日前的 [{d(a[0])}]'
        if fun == 'MA':
            return f'{a[0].value}日收盘均线（含当日）'
        if fun == 'RET':
            return f'{a[0].value}个交易日累计涨幅（%）'
        if fun == 'CROSS':
            return f'[{d(a[0])}] 上穿 [{d(a[1])}]（前日≤，当日>）'
        return f'[{d(a[0])}] 的绝对值'


class Evaluator:
    def __init__(self, bars, flows, markets, dates):
        self.bars, self.flows, self.markets, self.dates = bars, flows, markets, dates
        self.cache = {}
        self.valid_suffix = 0
        previous = None
        for day in reversed(dates):
            row = bars.get(day)
            if not row or not _bar_valid(row) or (previous and not _reference_valid(previous, row)):
                break
            self.valid_suffix += 1
            previous = row

    def field(self, key, offset):
        if offset >= len(self.dates):
            return None
        day = self.dates[-1-offset]
        if key in GLOBAL_FIELDS:
            value = self.markets.get(day, {}).get(key)
            return value if _number(value) and (key == 'sh_change' or value >= 0) else None
        if key in FUNDS:
            value = self.flows.get(day, {}).get(key)
            return value / 1e8 if _number(value) else None
        if offset >= self.valid_suffix:
            return None
        row = self.bars[day]
        if key in ('change', 'amplitude', 'limit_up'):
            if offset + 1 >= self.valid_suffix:
                return None
            prev = self.bars[self.dates[-2-offset]]['close']
            if key == 'amplitude':
                return (row['high'] - row['low']) / prev * 100
            if key == 'limit_up':
                limit = _limit_up_price_for(row['ts_code'], row['name'], prev, day)
                return row['close'] >= limit[0] if limit else None
        value = row.get(key)
        if not _number(value):
            return None
        return value / 1e5 if key == 'amount' and value > 0 else None if key == 'amount' else value

    def eval(self, node, offset=0):
        key = (node, offset)
        if key not in self.cache:
            value = self._eval(node, offset)
            self.cache[key] = None if type(value) in (float, int) and not math.isfinite(value) else value
        return self.cache[key]

    def _eval(self, node, offset):
        ev = lambda n, o=offset: self.eval(n, o)
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return self.field(node.id, offset)
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            return None if v is None else not v if isinstance(node.op, ast.Not) else -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BoolOp):
            return _tri([ev(n) for n in node.values], 'all' if isinstance(node.op, ast.And) else 'any')
        if isinstance(node, ast.Compare):
            values = [ev(n) for n in [node.left, *node.comparators]]
            parts = []
            for a, b, op in zip(values, values[1:], node.ops):
                parts.append(_compare(a, b, op))
            return _tri(parts, 'all')
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if a is None or b is None or (isinstance(node.op, ast.Div) and b == 0):
                return None
            value = a + b if isinstance(node.op, ast.Add) else a - b if isinstance(node.op, ast.Sub) else a * b if isinstance(node.op, ast.Mult) else a / b
            return value if math.isfinite(value) else None
        fun, a = node.func.id, node.args
        if fun in WINDOWS:
            n, off = a[1].value, a[2].value if len(a) == 3 else 0
            values = [ev(a[0], offset + off + i) for i in range(n)]
            # A temporal predicate needs its entire window, even if one day decides AND/OR.
            if None in values:
                return None
            if fun == 'ALL':
                return all(values)
            if fun == 'ANY':
                return any(values)
            if fun == 'COUNT':
                return sum(values)
            return sum(values) if fun == 'SUM' else sum(values) / n if fun == 'AVG' else max(values) if fun == 'MAX' else min(values)
        if fun == 'REF':
            return ev(a[0], offset + a[1].value)
        if fun == 'ABS':
            v = ev(a[0])
            return abs(v) if v is not None else None
        if fun == 'MA':
            values = [self.field('close', offset + i) for i in range(a[0].value)]
            return None if None in values else sum(values) / len(values)
        if fun == 'RET':
            current, previous = self.field('close', offset), self.field('close', offset + a[0].value)
            return (current / previous - 1) * 100 if current is not None and previous else None
        values = [ev(a[0]), ev(a[1]), ev(a[0], offset+1), ev(a[1], offset+1)]
        return None if None in values else _compare(values[0], values[1], ast.Gt()) and _compare(values[2], values[3], ast.LtE())

    def detail(self, formula):
        value = self.eval(formula.tree)
        node = formula.tree
        detail = {'state': 'missing' if value is None else 'matched' if value else 'unmet'}
        if isinstance(node, ast.Compare):
            detail['values'] = [self.eval(n) for n in [node.left, *node.comparators]]
        return detail


def validate_config(config):
    if not isinstance(config, dict) or config.get('mode', 'all') not in ('all', 'any'):
        raise ValueError('组间关系只能是全部满足或任一满足')
    groups = config.get('groups', [])
    if not isinstance(groups, list) or len(groups) > 6:
        raise ValueError('最多创建6个条件组')
    ids, compiled, clean = set(), {}, []
    for group in groups:
        if not isinstance(group, dict) or group.get('mode', 'all') not in ('all', 'any'):
            raise ValueError('条件组格式不正确')
        name = group.get('name', '条件组')
        if not isinstance(name, str) or len(name) > 60:
            raise ValueError('组名不能超过60个字符')
        rules = group.get('rules', [])
        if not isinstance(rules, list):
            raise ValueError('条件列表格式不正确')
        parsed = []
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError('条件格式不正确')
            rid, label = rule.get('id'), rule.get('name', '自定义条件')
            if not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', rid) or rid in ids:
                raise ValueError('条件标识无效或重复')
            if not isinstance(label, str) or not label.strip() or len(label) > 100:
                raise ValueError('条件名称不能为空，且不能超过100个字符')
            if type(rule.get('enabled', True)) is not bool:
                raise ValueError('条件开关必须为真假值')
            ids.add(rid)
            compiled[rid] = Formula(rule.get('formula'))
            parsed.append(dict(id=rid, name=label.strip(), enabled=rule.get('enabled', True),
                               formula=compiled[rid].text, explanation=compiled[rid].explanation))
        clean.append(dict(name=name, mode=group.get('mode', 'all'), rules=parsed))
    if len(ids) > 20:
        raise ValueError('最多添加20项条件')
    return dict(mode=config.get('mode', 'all'), groups=clean), compiled


def combine(config, states, remove=None):
    values = []
    for group in config['groups']:
        rules = [r for r in group['rules'] if r['enabled'] and r['id'] != remove]
        if rules:
            values.append(_tri([states[r['id']] for r in rules], group['mode']))
    return _tri(values, config['mode']) if values else True


def screen_custom(conn, date, config, now=None, offset=0, limit=50, query='', inspect='', all_results=False):
    """选股主流程。all_results=True 时 results 返回全部命中 (不分页), 只给预览图这类
    "一次拿全量"的调用方用; 分页接口行为不变。"""
    config, compiled = validate_config(config)
    try:
        offset, limit = int(offset), int(limit)
    except (ValueError, TypeError):
        raise ValueError('分页参数应为整数') from None
    if offset < 0 or not 1 <= limit <= 100 or not isinstance(query, str) or len(query) > 100:
        raise ValueError('分页或搜索参数无效')
    if inspect and (not isinstance(inspect, str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', inspect)):
        raise ValueError('股票代码格式不正确')
    context, _ = _daily_context(conn, date, now)
    payload = dict(context, status='success', strategy='custom-v1', config=config, count=0,
                   results=[], counts={}, diagnostics=[], inspected=None,
                   stats={'total': context['total'], 'evaluated': 0, 'missing': 0},
                   offset=offset, limit=limit, filtered_count=0)
    if not context.get('selection_ready', context['daily_ready']):
        payload['note'] = context['note'].replace('低吸名单', '自定义名单')
        return payload
    if not compiled:
        payload['note'] = '先添加并开启至少一项条件，再开始选股'
        return payload
    day = context['date']
    # Keep previous close for display/reference checks even for a current-value rule.
    lookback = max(1, max(f.lookback for f in compiled.values()))
    dates = [r[0] for r in conn.execute('SELECT DISTINCT cal_date FROM trading_dates_cache '
                                      'WHERE cal_date<=? ORDER BY cal_date DESC LIMIT ?', (day, lookback+1))][::-1]
    fields = set().union(*(f.fields for f in compiled.values()))
    bars, flows, markets = defaultdict(dict), defaultdict(dict), defaultdict(dict)
    conn.row_factory = sqlite3.Row
    for r in conn.execute('SELECT ts_code,name,trade_date,open,high,low,close,change,volume,amount '
                          'FROM stock_daily WHERE trade_date BETWEEN ? AND ?', (dates[0], day)):
        bars[r['ts_code']][r['trade_date']] = dict(r)
    source = None
    if fields.intersection(FUNDS):
        source = flow_source_for_range(conn, dates[0], day)
        if source:
            where, args = _flow_where(source)
            columns = [k for k in FUNDS if k in fields or k == 'main_net']
            selected = ','.join(FUNDS[k] for k in columns)
            for r in conn.execute(f'SELECT ts_code,trade_date,{selected} FROM fund_flow '
                                  f'WHERE trade_date BETWEEN ? AND ? AND {where}', [dates[0], day, *args]):
                flows[r['ts_code']][r['trade_date']] = {k: r[FUNDS[k]] for k in columns}
    else:
        source = flow_source_for_range(conn, day, day)
        if source:
            where, args = _flow_where(source)
            for r in conn.execute(f'SELECT ts_code,trade_date,main_net_inflow FROM fund_flow '
                                  f'WHERE trade_date=? AND {where}', [day, *args]):
                flows[r['ts_code']][day] = {'main_net': r['main_net_inflow']}
    if fields.intersection(GLOBAL_FIELDS):
        iso_start, iso_day = _iso(dates[0]), _iso(day)
        if fields.intersection(GLOBAL_FIELDS - {'sh_change'}):
            for r in conn.execute('SELECT date,upCount,downCount,flatCount FROM overview '
                                  'WHERE date BETWEEN ? AND ? OR date BETWEEN ? AND ?', (dates[0], day, iso_start, iso_day)):
                markets[r['date'].replace('-', '')].update(up_count=r['upCount'], down_count=r['downCount'], flat_count=r['flatCount'])
        if 'sh_change' in fields:
            for r in conn.execute("SELECT date,change FROM index_daily WHERE name='上证指数' "
                                  'AND (date BETWEEN ? AND ? OR date BETWEEN ? AND ?)', (dates[0], day, iso_start, iso_day)):
                markets[r['date'].replace('-', '')]['sh_change'] = r['change']
    active = [r for g in config['groups'] for r in g['rules'] if r['enabled']]
    counts = {rid: dict(matched=0, evaluated=0, missing=0, total=0) for rid in compiled}
    relaxation = {r['id']: 0 for r in active}
    eligible, matches = [], []
    inspected_found = False
    for code in sorted(bars):
        current = bars[code].get(day)
        if not current or not _number(current['close']) or current['close'] <= 0:
            continue
        ev = Evaluator(bars[code], flows[code], markets, dates)
        states = {rid: ev.eval(f.tree) for rid, f in compiled.items()}
        # Invalid entry prices must not be selected even by a market-only formula.
        entry_valid = ev.valid_suffix > 0
        for rid, value in states.items():
            counts[rid]['total'] += 1
            counts[rid]['missing' if value is None else 'evaluated'] += 1
            counts[rid]['matched'] += int(value is True)
        state = combine(config, states)
        if active and state is not None and entry_valid:
            payload['stats']['evaluated'] += 1
        elif active:
            payload['stats']['missing'] += 1
        for rid in relaxation:
            # The real screen intentionally generates no list with every rule off.
            relaxation[rid] += int(len(active) > 1 and entry_valid and combine(config, states, rid) is True)
        row = dict(ts_code=code, name=current['name'], trade_date=day, close=current['close'],
                   change_pct=ev.field('change', 0),
                   main_net_today=(ev.field('main_net', 0) * 1e8 if ev.field('main_net', 0) is not None else None),
                   state='inactive' if not active else 'missing' if state is None or not entry_valid else 'matched' if state else 'unmet',
                   rule_values={rid: ev.detail(f) for rid, f in compiled.items()})
        if active and state is True and entry_valid:
            matches.append(row)
            if not query or query.lower() in (code + ' ' + (current['name'] or '')).lower():
                eligible.append(row)
        if code == inspect:
            inspected_found = True
            payload['inspected'] = dict(row, matched=bool(active and state is True and entry_valid), entry_valid=entry_valid)
    payload.update(count=len(matches), filtered_count=len(eligible),
                   results=eligible if all_results else eligible[offset:offset+limit],
                   counts=counts, source=source,
                   diagnostics=[dict(id=r['id'], name=r['name'], without_count=relaxation[r['id']],
                                     added=relaxation[r['id']]-len(matches)) for r in active])
    if inspect and not inspected_found:
        payload['inspected'] = dict(ts_code=inspect, state='missing', matched=False, note='筛选日没有该股日线')
    payload['note'] = ('没有开启条件，先开启至少一项' if not active else
                       f'已按开启的条件选出{len(matches)}只股票' if matches else
                       '所需数据不足，暂时没有可确认入选的股票' if not payload['stats']['evaluated'] else
                       '当天没有股票满足当前条件组合，可查看逐项试算与数据缺口')
    return payload


def _iso(day):
    return f'{day[:4]}-{day[4:6]}-{day[6:]}'


def _chinese_atom(text):
    text = re.sub(r'\s+', '', text).replace('％', '%').replace('今日', '今天').replace('交易日', '天')
    if text in ('今天缩量', '当日缩量', '缩量'):
        return 'volume < REF(volume, 1)'
    if text in ('今天上涨', '当日上涨', '个股上涨'):
        return 'change > 0'
    if text in ('今天下跌', '当日下跌', '个股下跌'):
        return 'change < 0'
    if text in ('没涨停', '没有涨停', '未涨停', '收盘未涨停', '但是没涨停'):
        return 'not limit_up'
    if text in ('均线多头排列', '均线多头'):
        return 'close > MA(5) > MA(10) > MA(20) > MA(60)'
    if text in ('上证上涨', '上证指数上涨'):
        return 'sh_change > 0'
    if text in ('上证下跌', '上证指数下跌'):
        return 'sh_change < 0'
    m = re.fullmatch(r'(?:最近|近)?(\d+)(?:日|天)(?:至少|不少于)(\d+)(?:日|天)主力(?:流入|净流入|资金流入)(?:为正)?', text)
    if m:
        return f'COUNT(main_net > 0, {m[1]}) >= {m[2]}'
    m = re.fullmatch(r'连续(\d+)天(?:成交量递减|缩量)', text)
    if m:
        return f'ALL(volume < REF(volume, 1), {m[1]})'
    m = re.fullmatch(r'(?:今天|当日)?(?:收盘|收盘价)(?:站上|高于|大于|超过)(\d+)日(?:线|均线)', text)
    if m:
        return f'close > MA({m[1]})'
    m = re.fullmatch(r'(?:收盘|收盘价)(?:突破|高于)(?:此前|前)(\d+)(?:日|天)(?:最高价|高点)', text)
    if m:
        return f'close > MAX(high, {m[1]}, 1)'
    m = re.fullmatch(r'(?:今天|当日|个股)?(?:涨幅|涨跌幅)介于(-?\d+(?:\.\d+)?)%(?:[~～到至]|和)(-?\d+(?:\.\d+)?)%(?:之间)?', text)
    if m and float(m[1]) <= float(m[2]):
        return f'{m[1]} <= change <= {m[2]}'
    m = re.fullmatch(r'(?:当日成交量|今天成交量|成交量)(?:÷|/|除以)(?:此前|前)(\d+)日(?:平均成交量|均量)(小于|低于|大于|高于)(\d+(?:\.\d+)?)(?:倍)?', text)
    if m:
        return f'volume / AVG(volume, {m[1]}, 1) {"<" if m[2] in ("小于", "低于") else ">"} {m[3]}'
    m = re.fullmatch(r'(?:最近|近)?(\d+)(?:日|天)主力(?:净额|资金净额)/(?:同期|对应)?成交额(?:≥|>=|大于等于|至少|超过|大于|小于|低于)(\d+(?:\.\d+)?)%', text)
    if m:
        op = '>=' if any(x in text for x in ('≥', '>=', '大于等于', '至少')) else '<' if any(x in text for x in ('小于', '低于')) else '>'
        return f'SUM(main_net, {m[1]}) / SUM(amount, {m[1]}) * 100 {op} {m[2]}'
    m = re.fullmatch(r'(?:最近|近)?(\d+)(?:日|天)累计涨幅(超过|大于|高于|小于|低于|不少于|不超过|至少)(-?\d+(?:\.\d+)?)%', text)
    if m:
        return f'RET({m[1]}) {_cn_op(m[2])} {m[3]}'
    labels = {'当日涨幅': 'change', '今天涨幅': 'change', '个股涨幅': 'change', '涨幅': 'change',
              '下跌家数': 'down_count', '上涨家数': 'up_count', '上证涨跌幅': 'sh_change',
              '收盘价': 'close', '主力净额': 'main_net', '主力累计净额': 'main_net',
              '主力净流入': 'main_net', '成交额': 'amount', '小单净额': 'small_net'}
    m = re.fullmatch(r'(?:最近|近)?(?:(\d+)(?:日|天))?(' + '|'.join(labels) + r')(超过|大于|高于|小于|低于|不少于|不超过|至少|>=|<=|≥|≤|>|<|=)(-?\d+(?:\.\d+)?)(亿元|亿|万元|元|%|家)?', text)
    if m:
        field, unit = labels[m[2]], m[5] or ''
        money = FIELDS[field][1] == '亿元'
        if unit and ((money and unit not in ('亿元', '亿', '万元', '元')) or
                     (not money and unit not in ('%' if FIELDS[field][1] == '%' else '家' if field.endswith('count') else '元',))):
            return None
        value = float(m[4]) * (1e-4 if unit == '万元' else 1e-8 if money and unit == '元' else 1)
        if m[1] and not money and field != 'change':
            return None
        left = f'SUM({field}, {m[1]})' if m[1] and money else f'RET({m[1]})' if m[1] else field
        return f'{left} {_cn_op(m[3])} {value:g}'
    return None


def _cn_op(word):
    return '>=' if word in ('不少于', '至少', '>=', '≥') else '<=' if word in ('不超过', '<=', '≤') else '<' if word in ('小于', '低于', '<') else '==' if word == '=' else '>'


def parse_chinese(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
        raise ValueError('请输入条件说明，最多2000字')
    parts = [p.strip(' 。\n') for p in re.split(r'[，,；;。\n]+|而且|并且|还有|且', text) if p.strip(' 。\n')]
    if len(parts) > 20:
        raise ValueError('最多一次生成20项条件')
    rules, unknown = [], []
    for part in parts:
        alternatives = re.split(r'或者|或', part)
        formulas = [_chinese_atom(p) for p in alternatives]
        if any(f is None for f in formulas):
            unknown.append(part)
            continue
        try:
            formula = Formula(' or '.join(f'({f})' for f in formulas))
        except ValueError as error:
            unknown.append(f'{part}（{error}）')
            continue
        rules.append(dict(name=part, formula=formula.text, explanation=formula.explanation,
                          lookback=formula.lookback))
    return dict(status='success', complete=bool(rules) and not unknown, rules=rules, unknown=unknown,
                note='请核对后再加入条件；最近N日含当日，缩量默认与前一日比较，区间含两端。' if not unknown else
                '以下内容未能完整识别，请改写或使用公式编辑；本次草稿不能加入，避免漏掉条件。')


CUSTOM_PREVIEW_MAX = 200   # 预览图最多画多少只; 超出只在图上画前 N 只并写明总数


def _preview_summary(config, total, shown):
    """预览图副标题: 开了哪些条件 + 命中总数 (图上画了多少只)."""
    parts = []
    enabled = [r['name'] for g in config.get('groups', []) for r in g.get('rules', []) if r.get('enabled')]
    if enabled:
        parts.append('条件：' + ' · '.join(enabled))
    if total > shown:
        parts.append(f'共 {total} 只，图上显示前 {shown} 只')
    return ' ｜ '.join(parts)


def register_custom_picks(app, db_path, outcome_loader=None, preview_dir=None, preview_builder=None, preview_renderer=None,
                          config_path=None, intraday_provider=None):
    """outcome_loader: 名单确定后再补 T+1~T+5 回看结果.
    preview_dir / preview_builder / preview_renderer: 预览图能力, 由 app 注入
    (HTML 模板和 playwright 截图都住在 app.py, 避免这里反向 import app).
    config_path: 条件同步用的落盘位置, 同一台后端上的所有设备共用一份."""
    from flask import jsonify, request

    def body():
        if request.content_length and request.content_length > 64000:
            raise ValueError('请求过大，请减少条件数量')
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('请求内容应为条件对象')
        return data

    def respond(operation):
        try:
            result = operation()
            response = jsonify(result)
            response.headers['Cache-Control'] = 'no-store'
            return response
        except ValueError as error:
            return jsonify(status='error', message=str(error)), 400
        except sqlite3.Error:
            return jsonify(status='error', message='本地数据暂时无法读取，请稍后重试'), 503

    @app.get('/api/picks/custom/catalog')
    def custom_catalog():
        return respond(lambda: dict(status='success', fields=[dict(key=k, name=v[0], unit=v[1]) for k, v in FIELDS.items()],
                                    functions=FUNCTION_HELP, examples=EXAMPLES, max_rules=20, max_groups=6, max_lookback=250))

    @app.post('/api/picks/custom/validate')
    def custom_validate():
        def operation():
            f = Formula(body().get('formula'))
            return dict(status='success', formula=f.text, explanation=f.explanation, lookback=f.lookback)
        return respond(operation)

    @app.post('/api/picks/custom/parse')
    def custom_parse():
        return respond(lambda: parse_chinese(body().get('text')))

    @app.post('/api/picks/custom/screen')
    def custom_screen():
        def operation():
            data = body()
            if not isinstance(data.get('date', ''), str):
                raise ValueError('日期应为YYYYMMDD')
            validate_config(data.get('config'))
            with _scan_lock, picks_connection(db_path(), intraday_provider, data.get('date', ''),
                    data.get('intraday') in (True, 1, '1', 'true', 'True'),
                    data.get('snapshot_id', ''), data.get('refresh') in (True, 1, '1')) as conn:
                payload = screen_custom(conn, data.get('date', ''), data.get('config'),
                                        offset=data.get('offset', 0), limit=data.get('limit', 50),
                                        query=data.get('query', ''), inspect=data.get('inspect', ''))
                attach_sector_history(conn, payload['results'])
                if outcome_loader and payload['results'] and payload['daily_ready']:
                    days, live = outcome_loader(payload['results'], payload['date'], conn, key='custom_outcomes')
                    payload.update(next_dates=days, live_next_index=live)
                return payload
        return respond(operation)

    @app.get('/api/picks/custom/config')
    def custom_config_get():
        """读后端保存的条件配置 (给所有连这台后端的设备用). 没有就返回 config=None."""
        def operation():
            if not config_path:
                return dict(status='success', config=None, rev=0, updated_at=None)
            saved = _read_saved_config(config_path)
            if not saved:
                return dict(status='success', config=None, rev=0, updated_at=None)
            return dict(status='success', config=saved.get('config'),
                        rev=int(saved.get('rev') or 0), updated_at=saved.get('updated_at'))
        return respond(operation)

    @app.post('/api/picks/custom/config')
    def custom_config_save():
        """存条件配置: 后端是唯一真相源, 每次保存 rev+1, 设备按 rev 高低决定谁新."""
        def operation():
            if not config_path:
                raise ValueError('条件同步功能未启用')
            clean, _ = validate_config(body().get('config'))
            with _config_lock:
                saved = _read_saved_config(config_path) or {}
                payload = dict(config=clean, rev=int(saved.get('rev') or 0) + 1,
                               updated_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                _write_saved_config(config_path, payload)
            return dict(status='success', rev=payload['rev'], updated_at=payload['updated_at'])
        return respond(operation)

    @app.post('/api/picks/custom/preview-image')
    def custom_preview_image():
        """把当前日期的自定义选股结果画成一张 PNG 长图 (样式同「条件选股新」).
        body: {date?, config?} → PNG 存 preview_dir, 返回 {status,date,count,shown,url,path}."""
        def operation():
            if not (preview_dir and preview_builder and preview_renderer):
                raise ValueError('预览图功能未启用')
            data = body()
            if not isinstance(data.get('date', ''), str):
                raise ValueError('日期应为YYYYMMDD')
            clean, _ = validate_config(data.get('config'))
            with _scan_lock, picks_connection(db_path(), intraday_provider, data.get('date', ''),
                    data.get('intraday') in (True, 1, '1', 'true', 'True'), data.get('snapshot_id', '')) as conn:
                payload = screen_custom(conn, data.get('date', ''), clean, offset=0, limit=100, all_results=True)
            if not payload.get('selection_ready', payload.get('daily_ready')):
                raise ValueError(payload.get('note') or '当前日期数据不足，暂时不能生成预览图')
            rows = payload.get('results') or []
            total = payload.get('count') or 0
            if not rows:
                raise ValueError(payload.get('note') or '当前条件没有选出股票，没有可画的名单')
            shown = rows[:CUSTOM_PREVIEW_MAX]
            html = preview_builder({
                'date': payload['date'], 'count': total, 'results': shown,
                'title_kind': 'custom', 'subtitle': (('盘中预选 · '+payload['intraday']['captured_at']+' · ') if payload.get('intraday_estimate') else '') + _preview_summary(clean, total, len(shown)),
            })
            os.makedirs(preview_dir, exist_ok=True)
            # 文件名带条件指纹: 同一套条件重复生成覆盖同一个文件, 换条件另存一张
            tag = hashlib.md5(json.dumps(clean, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:6]
            out_name = f"自定义选股_{payload['date']}_{tag}.png"
            out_path = os.path.join(preview_dir, out_name)
            try:
                asyncio.run(preview_renderer(html, out_path))
            except Exception as error:
                raise ValueError(f'截图失败：{error}') from None
            return dict(status='success', date=payload['date'], count=total, shown=len(shown),
                        url='/previews/' + out_name, path=out_path)
        return respond(operation)
