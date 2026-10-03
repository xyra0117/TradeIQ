"""Build the Chinese report and readable ledgers from verified, frozen research files."""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs'
STAMP = '20261002'
GROUPS = [('all', '全部完整样本'), ('all_positive', 'A：5天每日最高累计涨幅均>0'),
          ('touch10', 'B：5天任一天最高累计涨幅>10%'), ('both', 'C：同时满足A和B')]


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |',
                      '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
                     ['| ' + ' | '.join(str(v) for v in row) + ' |' for row in rows])


def main():
    report = json.loads((OUT / f'dip-picks-analysis-{STAMP}.json').read_text())
    verification = json.loads((OUT / f'dip-picks-verification-{STAMP}.json').read_text())
    assert verification['raw_sql_selection_checks'] == report['selection_count'] == 244
    frame = pd.read_csv(OUT / f'dip-picks-events-{STAMP}.csv', dtype={'ts_code': str, 'trade_date': str})
    settled = frame[frame.complete]
    status = {'complete': '完整可评估', 'pending': '未来5日尚未全部入库',
              'reference_mismatch': '价格参考不连续', 'missing_daily': '未来窗口缺日线'}
    readable = pd.DataFrame({
        '入选日期': pd.to_datetime(frame.trade_date, format='%Y%m%d').dt.strftime('%Y-%m-%d'),
        '股票代码': frame.ts_code, '股票名称': frame.name, '交易板': frame.board,
        '入选时历史行业': frame.industry, '评估状态': frame.outcome_status.map(status),
        '已核验未来交易日数': frame.completed_days,
    })
    for key, label in GROUPS[1:]:
        readable[label] = frame[key].map({True: '是', False: '否'}).where(frame.complete, '未评估')
    columns = {
        'close': '入选收盘价', 'day_return': '入选日涨跌幅(%)', 'volume_ratio5': '当日量/此前5日均量',
        'amplitude20': '20日平均振幅(%)', 'bias20': '收盘相对MA20偏离(%)',
        'bias60': '收盘相对MA60偏离(%)', 'drawdown20': '收盘距近20日最高价(%)',
        'drawdown60': '收盘距近60日最高价(%)', 'return120': '120日涨幅(%)',
        'main1_yi': '东财当日主力净额(亿元)', 'main3_yi': '东财3日主力净额(亿元)',
        'main5_yi': '东财5日主力净额(亿元)', 'main10_yi': '东财10日主力净额(亿元)',
        'main20_yi': '东财20日主力净额(亿元)', 'main5_strength': '东财5日主力/同期成交额(%)',
        'big5_yi': '东财5日大单净额(亿元)', 'big5_strength': '东财5日大单/同期成交额(%)',
        'small1_yi': '东财当日小单净额(亿元)', 'small5_yi': '东财5日小单净额(亿元)',
        'main_positive_days5': '近5日主力净流入天数', 'ths_turnover': '同花顺当日换手率(%)',
        'ths_net_yi': '同花顺当日资金净额(亿元)', 'peak5': '5日最高累计涨幅(%)',
        'close5': 'T+5收盘累计涨幅(%)', 'adverse5': '5日最低价相对入选收盘价(%)',
        'market_downCount': '大盘下跌家数', 'index_sh_day_return': '上证入选日涨跌幅(%)',
        'excess_sh_close5': 'T+5收盘相对上证超额(百分点)',
    }
    for n in range(1, 6):
        readable[f'T+{n}交易日期'] = frame[f't{n}_date']
        readable[f'T+{n}最高累计涨幅(%)'] = frame[f't{n}_high_pct'].round(4)
        readable[f'T+{n}收盘累计涨幅(%)'] = frame[f't{n}_close_pct'].round(4)
    for key, label in columns.items():
        readable[label] = frame[key].round(4)
    readable.to_csv(OUT / f'dip-picks-review-{STAMP}.csv', index=False, encoding='utf-8-sig')
    readable[frame.complete & frame.both].to_csv(
        OUT / f'dip-picks-both-winners-{STAMP}.csv', index=False, encoding='utf-8-sig')
    stock_rows = []
    for (code, name), group in frame.groupby(['ts_code', 'name']):
        complete = group[group.complete]
        stock_rows.append({'股票代码': code, '股票名称': name, '交易板': group.board.iloc[0],
                           '入选次数': len(group), '完整可评估次数': len(complete),
                           '未完整评估次数': int((~group.complete).sum()),
                           'A命中次数': int(complete.all_positive.sum()),
                           'B命中次数': int(complete.touch10.sum()), 'C命中次数': int(complete.both.sum()),
                           '完整样本最高冲高(%)': complete.peak5.max(),
                           '完整样本T+5收盘涨幅中位数(%)': complete.close5.median()})
    pd.DataFrame(stock_rows).round(4).to_csv(OUT / f'dip-picks-stock-review-{STAMP}.csv',
                                          index=False, encoding='utf-8-sig')
    outcome_rows = []
    for key, label in GROUPS:
        g = report['outcomes'][key]
        outcome_rows.append([label, g['n'], g['stocks'], f"{g['n']/201*100:.2f}%",
                             f"{g['peak5']['median']:.2f}%", f"{g['close5']['median']:.2f}%",
                             f"{g['close5']['positive_rate']:.2f}%"])
    outcomes = table(['分组', '入选次数', '股票数', '占201次比例', '5日冲高中位数',
                      'T+5收盘中位数', 'T+5收盘>0比例'], outcome_rows)

    def feature_table(target, keys):
        records = {r['feature']: r for r in report['features'] if r['target'] == target}
        return table(['入选日及此前的指标', '命中组中位数', '未命中组中位数'],
                     [[records[k]['label'], f"{records[k]['winner_median']:.3f}",
                       f"{records[k]['other_median']:.3f}"] for k in keys])

    def rules_table(target, names):
        records = {r['rule']: r for r in report['rules'] if r['target'] == target}
        return table(['观察条件', '满足次数/命中率', '不满足次数/命中率', '同日同板差值', '校正后q值'],
                     [[name, f"{records[name]['yes_n']} / {records[name]['yes_rate']:.2f}%",
                       f"{records[name]['no_n']} / {records[name]['no_rate']:.2f}%",
                       f"{records[name]['adjusted_difference_pp']:+.2f}个百分点",
                       f"{records[name]['fdr_q']:.3f}"] for name in names])

    dates = table(['入选日', '全部/完整次数', 'A命中率', 'B命中率', 'C命中率',
                   '上证当日', '上证之后5日收盘涨幅'],
                  [[r['trade_date'], f"{r['selected']}/{r['complete']}",
                    *[f"{r.get(k+'_rate', 0):.2f}%" if r['complete'] else '待评估'
                      for k in ['all_positive', 'touch10', 'both']],
                    f"{r['index_sh_day_return']:.2f}%",
                    f"{r['post_index_sh_close5']:.2f}%" if r['post_index_sh_close5'] is not None else '未完整']
                   for r in report['perdate']])
    boards = table(['交易板', '完整次数', 'A命中率', 'B命中率', 'C命中率'],
                   [[k, v['n'], *[f"{v[t+'_rate']:.2f}%" for t in ['all_positive', 'touch10', 'both']]]
                    for k, v in report['boards'].items()])
    industries = table(['历史行业（至少8次）', '完整次数', 'A命中率', 'B命中率', 'C命中率'],
                       [[k, v['n'], *[f"{v[t+'_rate']:.2f}%" for t in ['all_positive', 'touch10', 'both']]]
                        for k, v in report['industries'].items() if v['n'] >= 8])
    slices = table(['复核方式', '次数', 'A命中率', 'B命中率', 'C命中率'],
                   [[k, v['n'], *[f"{v[t+'_rate']:.2f}%" for t in ['all_positive', 'touch10', 'both']]]
                    for k, v in report['time_slices'].items()])
    tails = table(['分组', 'T+5均值', 'T+5第5百分位', 'T+5最差', 'T+5跌超10%比例',
                   '相对上证超额中位数', '剔除最强5%后均值'],
                  [[label, f"{g['close5']['mean']:.2f}%", f"{g['close5']['p05']:.2f}%",
                    f"{g['close5']['min']:.2f}%", f"{g['close5_loss10_rate']:.2f}%",
                    f"{g['excess_sh_close5']['median']:.2f}个百分点",
                    f"{g['close5_after_removing_top5_mean']:.2f}%"]
                   for key, label in GROUPS for g in [report['outcomes'][key]]])
    sources = verification['source_coverage']
    source_rows = []
    for key, label, usable in [
        ('stock_daily', '个股日线', '201次完整；此前121日连续且价格参考匹配'),
        ('eastmoney_only', '东方财富个股资金', '244次选股及201次完整样本；策略唯一资金源'),
        ('index_daily', '四大指数日线', '201次/11个入选日；指数完整498日'),
        ('overview', '大盘涨跌家数等', '201次/11个入选日'),
        ('market_fflow', '大盘分档资金', '201次/11日；只用同日complete=1记录'),
        ('fund_flow_ths', '同花顺换手率/净额', '103次/5个入选日；单独比较'),
        ('limitup', '历史涨停/题材表', '有记录才使用；未出现不等于从未涨停'),
        ('stock_minline_bars', '个股分时', '42次/2个入选日；非交易日、参考不符均排除'),
        ('index_minline_bars', '指数分时', '上证严格核验通过20次/1日，其他指数按各自覆盖'),
        ('sector_fund_flow', '板块当日资金', '42次/2日；采集到的板块子集'),
    ]:
        x = sources[key]
        source_rows.append([label, f"{x['first']}—{x['last']}", x['dates'], x['rows'], usable])
    coverage = table(['数据源', '库内日期范围', '日期数', '行数', '研究可用范围'], source_rows)
    c = settled[settled.both].sort_values(['trade_date', 'ts_code'])
    winners = table(['入选日', '代码', '名称', '5日最低的单日最高累计涨幅', '5日最高累计涨幅', 'T+5收盘'],
                    [[r.trade_date, r.ts_code, r.name, f'{r.min_high5:.2f}%', f'{r.peak5:.2f}%',
                      f'{r.close5:.2f}%'] for r in c.itertuples()])
    text = f"""# 低吸选股全部历史样本研究（2026-10-02）

数据截至2026-09-30收盘。按目前实际策略重建全部历史名单，主力资金来自**东方财富**，同花顺只提供部分辅助比较。研究只读取本地数据库；没有新增选股过滤条件。

较清楚的探索线索是：A组更靠近均线、远离近期高位，并更常伴随当日小单净流出；B组的近5日主力和大单承接更强，短期回落幅度更大。两类目标有区别，单日主力为正、缩量越多都没有表现为两组通用的必要条件。这些是样本内关联，经过多重比较校正后尚无可靠验证的新规则。

## 1. 样本、计算口径与排除

检查本地433个个股日线日期，98个日期触发大盘条件；13个日期实际选出244次、180只股票。201次、161只股票、11个入选日具备完整未来5个交易日，入选日期2026-07-16至2026-09-17。全部名单依据当前条件重新计算，不能解释为当时已经保存或实际交易的名单。

其余43次单独保留：39次未来窗口尚不完整、2次缺未来日线、2次价格参考不连续。缺失不是收益为0或失败。具体异常是7月17日兖矿能源的T+5参考价、7月30日宝莱特T+2缺日线、9月2日富煌钢构T+3缺日线、9月28日东材科技T+1参考价。9月24日和9月28日尚无完整5日窗口。

对每次入选，以入选日收盘价P0为基准：T+N最高累计涨幅=(第N个交易日最高价/P0−1)×100%；T+N收盘累计涨幅=(第N个交易日收盘价/P0−1)×100%。与页面当前实际口径一致，不把日涨跌幅相加。第N列最高价是第N天当天的最高价，5日最高累计涨幅才是5个数的最大值。

* A：T+1、T+2、T+3、T+4、T+5五个最高累计涨幅各自均严格>0。
* B：这五个数中至少一个严格>10%。
* C：A与B的交集。

{outcomes}

当前策略：下跌家数>2800，上证当天跌幅不足2%且前三日非连涨，或者当天跌超2%；个股当天收跌、120日涨幅<300%、3/5/10/20日主力累计全正且任一>0.5亿元、当日量/此前5日均量<1.2、收盘>MA20、20日平均振幅≥5%。当日主力不单独要求>0或过0.5亿门槛，仍进入累计值。全部入选股本来就满足的条件，不能直接算作成功组独有共性。

## 2. A组：五天每天均有正向冲高

A组117次对比未满足A的84次。更显眼的是接近均线、过去的回落更充分、近期均线/成交量扩张较温和。

{feature_table('all_positive', ['bias20', 'bias60', 'drawdown60', 'volume_trend5_20', 'ma10_slope5', 'main_positive_days5', 'small1_strength'])}

{rules_table('all_positive', ['收盘在MA60下方', '当日小单净额<0', '收盘在MA20上方5%以内'])}

以上三个条件控制入选日和交易板后，方向在7—8月、9月、剔除同股重叠窗口、每股仅首个事件四种复核中一致。但q值均>0.05，仍不足以确认为稳定过滤条件。MA60下方也不是A组的多数特征：A组51/117次满足，属于相对命中率较高的子组。

## 3. B组：五天内曾冲高超过10%

B组70次对比未满足B的131次。更突出的资金特征在近5日累计承接，而非当日资金方向。

{feature_table('touch10', ['main5_yi', 'main5_strength', 'big5_yi', 'big5_strength', 'small5_yi', 'main_positive_days5', 'drawdown20', 'bias60'])}

资金强度=累计净额/同期成交额×100%，日线成交额由千元换算为元，以减少仅因大盘股成交额大而净额大的干扰。东财“大单”是其分档净额，主力为超大单与大单合计；小单净流出只表明这个分档方向，不能据此证明具体投资者身份。

{rules_table('touch10', ['收盘距20日最高价回落≥10%', '5日主力净额/成交额≥5%', '收盘在MA60下方'])}

这些方向在前后时段和去重检查中延续，但近5日资金强度的同日同板差值从7—8月+44.69个百分点降到9月+9.94个百分点；效应大小明显变化。表中所有q值仍未达0.05。

单日主力为正并非B组必要条件：70次中41次当日主力为负；所有完整样本中，当日主力>0的B命中率32.58%，≤0的为36.61%。不应据此恢复先前移除的“当日主力必须为正”条件。

缩量至此前5日均量80%及以下时，B命中率32.89%，其余36.00%；“缩量越多越好”没有得到支持。20日平均振幅≥8%的子组B命中率40.82%，其余32.89%，但控制日期/交易板及多重比较后也不稳健。

70次首次冲破10%的日期分布：T+1为9次、T+2为22次、T+3为15次、T+4为14次、T+5为10次。这是事后触及分布，不是推荐卖出日。

## 4. C组：同时满足两个目标

C组58次、49只股票，对比其余143次：

{feature_table('both', ['main5_yi', 'main5_strength', 'big5_yi', 'small5_yi', 'main_positive_days5', 'bias20', 'bias60', 'ma10_slope5'])}

更像已有活跃度、回到均线附近、近5日持续承接的组合。但不存在所有C组股票共同满足的新增必要条件。C组当日主力为正只有25/58次，近5日主力净流入天数中位数为4天也不表示每一只都满足4天。

例子可以说明形态，不能替代全量对照：9月2日华盛昌5日冲高41.68%、T+5收盘38.53%；9月4日利扬芯片37.37%/35.54%；9月2日沃尔德34.63%/33.31%。这些极端赢家已包含在全样本，报告另做去极值检查。

## 5. 大盘、交易板和行业

同日所有股票共享相同大盘环境，市场分析单位只有11个入选日，不能把201次股票事件当作201次独立市场观察。使用涨跌家数、严重下跌家数、四大指数当日/近5日/近20日/均线/成交额、大盘分档资金、可核验分时和已采集板块数据作对照。

{dates}

结果有明显日期差异。7月17日A命中率14.29%，7月30日92.31%；前者上证之后5日收盘仍上涨1.33%，所以只看最终大盘反弹不能解释“每天都有正向机会”。上证当天跌超2%的两日合计22次，A命中率27.27%；跌不足2%的9日179次为62.01%。两日样本不能支持添加一个大盘阈值。

按11个日期等权，A命中率58.06%、B为34.31%、C为27.17%，与股票事件加权总体接近。同日全市场主力净额占比与A日期命中率的秩相关约+0.52，上证当日涨跌幅约+0.49；但B相应约−0.04/−0.18。A和B没有统一可靠的大盘择时共性；相关系数仅作描述，不作为新增规则。

{boards}

创业板、科创板的A/B命中率相对较高，比较资金和技术指标时已经控制交易板；不同交易板的涨跌幅制度、波动和构成会影响触及10%的难度。科创板B命中率较高，但全体科创样本T+5收盘中位数仍为−0.57%，不能只看冲高比例。

{industries}

机械设备B命中13/22次、电子20/46次；二者占B组33/70次、C组28/58次。但行业分类使用有in_date/out_date的申万SW2014历史归属，12次完整样本没有归属；行业和日期/交易板交织、样本不大，不将排名解释成必然受益板块。历史涨停题材只用入选日及之前已有记录；没有以当前热门题材补历史标签，也不推断公告/业绩因果。

## 6. 收盘收益、尾部与稳定性

冲高只是价格曾经触及，不等于可按那个价格成交。A组33/117次T+5收盘为负、1次为零；B组10/70次最终收盘仍为负；C组5/58次最终收盘为负。分组本身用未来结果定义，其收盘表现不能当作事前策略回测收益。

{tails}

全体201次T+5收盘均值+1.16%、中位数−0.17%、收盘为正48.76%；剔除收盘最强5%即11次之后，均值变为−0.22%。相对所属交易板指数超额中位数−0.49个百分点。全体5日内最低价相对入选收盘价的中位数−4.58%、最差−29.48%，没有模拟成交、止损或手续费。

{slices}

A/B/C的总体比例在去重后没有根本改变；但A前后月份差异较大。基准全部股票T+5收盘均值在7—8月为−0.49%，9月+2.25%。这批数据尚不能证明跨市场阶段稳定盈利。

对109项可用个股数值指标及28项预设观察条件，每个目标共137项比较，采用同入选日×交易板分层置换、4999次置换和BH多重比较校正。最小q值：A=0.082、B=0.376、C=0.274；均未达0.05。规则的置信区间按入选日聚类重采样5000次，只有11个日期，仍应谨慎解释；置信区间未覆盖0不代表通过全部多重检验。没有围绕成功股反复优化阈值、没有后验组合条件或预测评分，也没有将未来大盘回报作为事前特征。

## 7. “所有能用的数据”的覆盖边界

{coverage}

东方财富资金库从2026-06-16才连续覆盖75个日期，因此无法凭更早的零散日线组成多年低吸验证；要求20日资金完整之后才可能入选。同花顺只有103次完整样本，换手率/资金净额独立检查，绝不和东财混加。两家口径不同，不能用同花顺补缺失的东财策略资金。

个股分时和板块资金仅覆盖2个完整入选日；同花顺也仅5日，不足以给出可靠的盘中买点、尾盘拉升或换手率过滤规则。数据库里的10月1日个股分时不属于本地交易日历，未用作交易日。上证分时除日期外还需通过昨收/收盘价格校验，完整样本只在9月15日通过；未以最新分时替代历史。

股本数据大多是9月30日最新快照，不回填成早期市值。stock_basic行业/市值字段为空；无历史市值估算结论。theme_heat_daily只有8月14日，与入选日不重合；market_notes主要为人工描述，数值稀疏且非独立行情源，未用于推断。申万行业日线、北向资金、AKShare涨停池、板块累计资金表为空。派生均线从原始日线重算，其他策略快照不作为低吸名单。个人持仓、交易、配置及凭据均不是本研究变量。overview.netFlow采集可能回退其他日期，改用明确同日的market_fflow，不混用。

## 8. 完整文件与复核

* [244次中文事件台账](dip-picks-review-20261002.csv)：A/B/C标签、每个T+N的最高与收盘涨幅、来源独立的资金、均线和缺失状态。未完整样本标为“未评估”。
* [180只股票中文台账](dip-picks-stock-review-20261002.csv)：逐只入选/完整评估/A/B/C次数，所有入选股票均保留。
* [同时满足A和B的58次名单](dip-picks-both-winners-20261002.csv)：49只股票，各次日期和完整T+1至T+5数据。
* [全部原始特征](dip-picks-events-20261002.csv)、[逐日统计](dip-picks-dates-20261002.csv)、[全部指标比较](dip-picks-features-20261002.csv)、[全部条件比较](dip-picks-rule-comparisons-20261002.csv)、[分析JSON](dip-picks-analysis-20261002.json)、[独立复核JSON](dip-picks-verification-20261002.json)。

独立SQL重算244次入选的121日日线连续性、价格参考、120日涨幅、成交量比、MA20、20日振幅及4个资金累计窗口；逐次与页面实际T+N计算函数核对244次结果，并核对201次显示舍入后分类。重复代码+入选日为0。SQLite连接mode=ro、query_only=1，未写数据库。

复现顺序：`python3 backend/scripts/analyze_dip_picks_20261002.py` → `python3 backend/scripts/summarize_dip_picks_20261002.py` → `python3 backend/scripts/verify_dip_picks_20261002.py` → `python3 backend/scripts/report_dip_picks_20261002.py`。原始库若更新需重新运行；本报告是固定日期研究。策略源文件SHA256：`{report['rule_hash']}`。

## 附录：同时满足A和B的全部58次事件

{winners}
"""
    (OUT / f'dip-picks-research-{STAMP}.md').write_text(text)
    print(json.dumps({'report': f'docs/dip-picks-research-{STAMP}.md', 'review_rows': len(readable),
                      'stock_rows': len(stock_rows), 'both_rows': len(c)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
