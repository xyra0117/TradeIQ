#!/usr/bin/env python3
"""Render an offline, past-only chart companion for the box breakout study.

Input and output stay in docs/. ECharts is embedded from the repository's pinned
mobile vendor copy, including its license notice. This script never opens SQLite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def prepare_charts(raw):
    charts = raw.get("charts")
    if not isinstance(charts, list) or not charts:
        raise ValueError("results.json 必须包含非空 charts 数组")
    prepared = []
    for item in charts:
        name, code, day = (str(item[k]) for k in ("name", "code", "date"))
        bars = sorted(item["bars"], key=lambda bar: bar["trade_date"])
        if not bars or bars[-1]["trade_date"] != day:
            raise ValueError(f"{name}: 日线必须以指定信号日结束，不得包含未来数据")
        if len({b["trade_date"] for b in bars}) != len(bars):
            raise ValueError(f"{name}: 日线日期重复")
        for bar in bars:
            if bar["trade_date"] > day:
                raise ValueError(f"{name}: 含未来日线")
            for key in ("open", "high", "low", "close", "volume", "amount", "change"):
                value = bar.get(key)
                if not isinstance(value, (float, int)) or not math.isfinite(value):
                    raise ValueError(f"{name}: {bar['trade_date']} {key} 无效")
            if not (0 < bar["low"] <= min(bar["open"], bar["close"])
                    <= max(bar["open"], bar["close"]) <= bar["high"]):
                raise ValueError(f"{name}: OHLC 无效")
        if len(bars) < 21:
            raise ValueError(f"{name}: 不足20日箱体加信号日")
        previous, target = bars[:-1], bars[-1]
        window = previous[-20:]
        top, bottom = max(b["close"] for b in window), min(b["close"] for b in window)
        wick = max(b["high"] for b in window)
        volume_base = sum(b["volume"] for b in previous[-5:]) / 5
        if volume_base <= 0:
            raise ValueError(f"{name}: 前5日成交量基准无效")
        discontinuities = []
        for before, bar in zip(bars, bars[1:]):
            delta = (bar["close"] / before["close"] - 1) * 100 - bar["change"]
            if abs(delta) > .15:
                discontinuities.append({"date": bar["trade_date"], "difference_pp": delta})
        fields = ("trade_date", "open", "high", "low", "close", "volume", "amount", "change")
        prepared.append({
            "name": name, "code": code, "date": day,
            "bars": [{key: b[key] for key in fields} for b in bars[-121:]],
            "box_top": top, "box_bottom": bottom, "wick_top": wick,
            "box_start": window[0]["trade_date"], "box_end": window[-1]["trade_date"],
            "close_breakout_pct": (target["close"] / top - 1) * 100,
            "wick_breakout_pct": (target["close"] / wick - 1) * 100,
            "volume_ratio5": target["volume"] / volume_base,
            "box_width_pct": (top / bottom - 1) * 100,
            "discontinuities": discontinuities,
        })
    return prepared


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light"><title>箱体突破 · 11例形态核验</title>
<style>
:root{--paper:#f4f3ef;--card:#fff;--text:#202e38;--muted:#6d7982;--line:#dbe0e3;--ink:#205c71;--box:#e8f1f5;--gold:#ad7721;--up:#c34242;--down:#218669}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--text);font:14px/1.55 "PingFang SC","Microsoft YaHei",sans-serif}button,select{font:inherit}button{cursor:pointer}button:focus-visible,select:focus-visible{outline:3px solid #8dbbce;outline-offset:3px}header{max-width:1500px;margin:auto;padding:30px 36px 24px;display:flex;align-items:flex-end;justify-content:space-between;gap:24px}h1{font-size:30px;letter-spacing:-.6px;margin:3px 0 8px;font-weight:650}p{margin:0}.eyebrow{color:var(--ink);font-size:11px;letter-spacing:2px;font-weight:700}.intro{color:var(--muted);font-size:13px}.offline{font-size:12px;color:var(--muted);white-space:nowrap}.offline:before{content:"";display:inline-block;width:6px;height:6px;border-radius:100%;background:#438a78;margin:0 7px 2px 0}.layout{max-width:1500px;margin:auto;padding:0 36px 30px;display:grid;grid-template-columns:228px minmax(0,1fr);gap:22px}.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}.sidebar{align-self:start}.side-head{padding:16px 17px 12px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}.stock-list{padding:6px}.stock-item{width:100%;border:0;background:none;padding:12px 11px;display:grid;grid-template-columns:1fr auto;text-align:left;gap:3px 8px;border-radius:6px;color:var(--text)}.stock-item:hover{background:#f4f7f8}.stock-item.active{background:var(--box);box-shadow:inset 3px 0 var(--ink)}.stock-name{font-weight:600;font-size:14px}.stock-index{font-size:11px;color:var(--muted);margin-right:6px}.stock-date{font-size:11px;color:var(--muted)}.stock-change{grid-column:2;grid-row:1/3;align-self:center;font-variant-numeric:tabular-nums;font-size:12px;color:var(--up)}main{min-width:0}.chart-head{padding:22px 24px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:18px}.stock-title{font-size:24px;line-height:1.25;margin:0;font-weight:650}.stock-sub{margin-top:7px;color:var(--muted);font-size:12px}.signal{border:1px solid #aacbd7;background:var(--box);color:var(--ink);border-radius:5px;padding:7px 10px;white-space:nowrap;font-size:12px}.metrics{display:grid;grid-template-columns:repeat(4,1fr);padding:19px 8px;border-bottom:1px solid var(--line)}.metric{padding:0 16px;border-right:1px solid var(--line)}.metric:last-child{border:0}.metric-label{font-size:12px;color:var(--muted)}.metric-value{font-family:"SFMono-Regular",Consolas,monospace;font-size:23px;line-height:1.3;font-weight:600;margin:6px 0 2px;letter-spacing:-.7px}.metric-note{font-size:11px;color:var(--muted)}.positive{color:var(--up)}.negative{color:var(--down)}.toolbar{padding:18px 24px 12px;display:flex;align-items:center;justify-content:space-between;gap:12px}.legend{display:flex;flex-wrap:wrap;gap:8px 17px;font-size:11px;color:var(--muted)}.legend span:before{content:"";display:inline-block;width:19px;margin:0 6px 3px 0;border-top:2px solid var(--ink)}.legend .shade:before{height:9px;background:var(--box);border:1px solid #a8c6d3;margin-bottom:0}.legend .wick:before{border-top:2px dashed var(--gold)}.ranges{display:flex;gap:3px;padding:3px;background:#f0f3f4;border-radius:6px;flex-shrink:0}.ranges button{background:none;border:0;padding:5px 10px;border-radius:4px;color:var(--muted);font-size:12px}.ranges button.active{background:#fff;color:var(--ink);box-shadow:0 1px 3px #22374413;font-weight:650}.chart{height:490px;width:100%}.reading{padding:0 24px 20px}.reading strong{font-weight:600;color:var(--text)}.reading p{color:var(--muted);font-size:12px;margin:6px 0}.notice{padding:12px 14px;background:#f9f3e7;border-left:3px solid var(--gold);color:#755619;font-size:12px;border-radius:2px;margin-top:11px}.hidden{display:none}.footer{padding:16px 21px;margin-top:15px;background:transparent;border:1px solid var(--line);border-radius:8px;color:var(--muted);font-size:12px}.footer strong{color:var(--text);font-weight:600}.footer p+p{margin-top:6px}.mobile-select{display:none}.tip{line-height:1.7;color:#34434d;font:12px/1.7 "PingFang SC",sans-serif}.tip b{font-weight:600}.empty{padding:35px;color:#8d443d}
@media(min-width:1600px){.chart{height:570px}}@media(max-width:1000px){header{padding:23px 20px 20px}.layout{padding:0 20px 24px;grid-template-columns:190px minmax(0,1fr);gap:15px}.metrics{grid-template-columns:repeat(2,1fr);gap:16px 0}.metric:nth-child(2){border:0}.toolbar{align-items:flex-start;flex-direction:column}.chart{height:460px}.metric-value{font-size:21px}}@media(max-width:700px){header{padding:20px 15px 18px;display:block}h1{font-size:26px}.offline{margin-top:10px}.layout{display:block;padding:0 12px 24px}.sidebar{display:none}.mobile-select{display:block;width:100%;margin:0 0 12px;padding:10px 12px;background:#fff;border:1px solid var(--line);border-radius:6px;color:var(--text)}.chart-head{padding:18px 17px}.stock-title{font-size:21px}.signal{font-size:10px}.metrics{padding:17px 0}.metric{padding:0 16px}.metric-value{font-size:20px}.toolbar{padding:14px 17px 6px}.chart{height:430px}.reading{padding:0 17px 16px}.footer{padding:14px 15px}.stock-sub{font-size:11px}}
</style></head><body>
<header><div><p class="eyebrow">TRADEIQ / LOCAL RESEARCH</p><h1>箱体突破，逐只看清</h1><p class="intro">11个指定样本 · 只展示信号日及以前的真实日线 · 2026年</p></div><p class="offline">离线可用 · 本地数据库核验</p></header>
<div class="layout"><aside class="panel sidebar"><div class="side-head"><span>样本股票</span><span>信号日涨幅</span></div><nav id="stockList" class="stock-list" aria-label="选择股票"></nav></aside><main><select id="mobileSelect" class="mobile-select" aria-label="选择股票"></select><article class="panel"><div class="chart-head"><div><h2 id="stockTitle" class="stock-title"></h2><p id="stockSub" class="stock-sub"></p></div><span id="signalDate" class="signal"></span></div><div id="metrics" class="metrics"></div><div class="toolbar"><div class="legend"><span>最高收盘箱顶</span><span class="shade">前20日收盘区间</span><span class="wick">前20日最高价</span></div><div id="ranges" class="ranges" role="group" aria-label="显示历史交易日数"><button data-days="20">20日</button><button data-days="40">40日</button><button class="active" data-days="60">60日</button><button data-days="120">120日</button></div></div><div id="chart" class="chart" role="img" aria-label="个股蜡烛图及成交量"></div><div class="reading"><p id="reading"></p><p>蓝色箱体只覆盖信号日前20个交易日，不含D日。D为你指定的入池日；下方柱形为成交量。红涨绿跌，悬停可看具体行情。</p><p id="rangeNote"></p><div id="warning" class="notice hidden"></div></div></article><div class="footer"><p><strong>读图口径：</strong>箱顶、箱底采用前20日最高／最低收盘价；金色虚线为同窗最高价（含上影线）。突破幅度=信号日收盘÷箱顶−1；量倍数=信号日成交量÷此前5日平均成交量。</p><p>原始未复权价格。数据库没有复权因子；价格参考异常可能与除权除息或数据错误有关，跨异常窗口不能作为可信箱体。此页展示指定样本的历史形态，不证明未来收益或构成买入建议。</p><p>来源：本地 stock_daily。只读取研究结果文件生成，无外部接口或网络资源。</p></div></main></div>
<script>__ECHARTS__</script>
<script id="study-data" type="application/json">__DATA__</script>
<script>
'use strict';
const studies=JSON.parse(document.getElementById('study-data').textContent);let selected=0,viewDays=60;
const $=id=>document.getElementById(id),escapeHtml=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(n,d=2)=>Number(n).toFixed(d),signed=n=>(n>0?'+':'')+fmt(n)+'%',date=s=>s.slice(0,4)+'-'+s.slice(4,6)+'-'+s.slice(6,8);
const chart=echarts.init($('chart'),null,{renderer:'canvas'});
$('stockList').innerHTML=studies.map((s,i)=>`<button class="stock-item" data-index="${i}" aria-pressed="false"><span class="stock-name"><span class="stock-index">${String(i+1).padStart(2,'0')}</span>${escapeHtml(s.name)}</span><span class="stock-date">${date(s.date)} · ${escapeHtml(s.code.split('.')[0])}</span><span class="stock-change">${signed(s.bars.at(-1).change)}</span></button>`).join('');
$('mobileSelect').innerHTML=studies.map((s,i)=>`<option value="${i}">${escapeHtml(s.name)} · ${date(s.date)}</option>`).join('');
function render(){
 const s=studies[selected],last=s.bars.at(-1),bars=s.bars.slice(-(viewDays+1)),dates=bars.map(b=>b.trade_date),small=window.innerWidth<700;
 $('stockTitle').textContent=s.name;$('stockSub').textContent=s.code+' · D日收盘 '+fmt(last.close)+'元 · 当日涨幅 '+signed(last.change);$('signalDate').textContent='D · '+date(s.date);
 const metric=(label,value,note,cls='')=>`<div class="metric"><div class="metric-label">${label}</div><div class="metric-value ${cls}">${value}</div><div class="metric-note">${note}</div></div>`;
 $('metrics').innerHTML=metric('突破最高收盘箱顶',signed(s.close_breakout_pct),'箱顶 '+fmt(s.box_top)+' 元','positive')+metric('相对前20日最高价',signed(s.wick_breakout_pct),'上影线高点 '+fmt(s.wick_top)+' 元',s.wick_breakout_pct>=0?'positive':'negative')+metric('当日成交量 / 前5日均量',fmt(s.volume_ratio5)+' 倍','此前5日，不含信号日')+metric('前20日收盘区间宽度',fmt(s.box_width_pct)+'%','箱底 '+fmt(s.box_bottom)+' 元');
 $('reading').innerHTML=`<strong>${escapeHtml(s.name)}</strong>：收盘超过前20日最高收盘价 <strong>${fmt(s.close_breakout_pct)}%</strong>，${s.wick_breakout_pct>0?'也超过':'仍低于'}此前最高价 <strong>${fmt(Math.abs(s.wick_breakout_pct))}%</strong>。${s.wick_breakout_pct<=0?'按“突破最高价”筛选会漏掉这个指定日期。':''}`;
 $('rangeNote').textContent='当前展示：'+date(dates[0])+' 至 '+date(s.date)+'，共'+bars.length+'个交易日（信号日前'+(bars.length-1)+'日 + D）。';
 const warnings=s.discontinuities.filter(x=>x.date>dates[0]&&x.date<=s.date);$('warning').classList.toggle('hidden',!warnings.length);$('warning').textContent=warnings.length?'本次显示区间含价格参考断点：'+warnings.map(x=>date(x.date)).join('、')+'。跨越这些日期的长箱体暂不可信；图中20日箱体另按自身窗口核验。':'';
 const inBox=d=>d>=s.box_start&&d<=s.box_end;const level=(name,value,color,dashed)=>({name,type:'line',data:dates.map(d=>inBox(d)?value:null),symbol:'none',lineStyle:{width:1.5,color,type:dashed?'dashed':'solid'},itemStyle:{color},connectNulls:false,silent:true,z:4});
 const option={animation:false,backgroundColor:'#fff',textStyle:{fontFamily:'PingFang SC,Microsoft YaHei,sans-serif'},grid:[{left:small?49:65,right:small?22:37,top:19,height:'63%'},{left:small?49:65,right:small?22:37,top:'74%',height:'16%'}],axisPointer:{link:[{xAxisIndex:'all'}],label:{backgroundColor:'#205c71'}},tooltip:{trigger:'axis',axisPointer:{type:'cross'},borderColor:'#dbe0e3',backgroundColor:'#fff',extraCssText:'box-shadow:0 4px 20px #203e5114;border-radius:6px;',formatter:params=>{const p=params.find(p=>p.seriesName==='日线')||params[0];const b=bars[p.dataIndex];if(!b)return '';return `<div class="tip"><b>${escapeHtml(s.name)} · ${date(b.trade_date)}${b.trade_date===s.date?' · D':''}</b><br>开 ${fmt(b.open)}　收 ${fmt(b.close)}<br>高 ${fmt(b.high)}　低 ${fmt(b.low)}<br>当日涨跌 ${signed(b.change)}<br>成交量 ${fmt(b.volume/10000)} 万手<br>成交额 ${fmt(b.amount/100000)} 亿元</div>`}},xAxis:[{type:'category',data:dates,boundaryGap:true,axisLine:{lineStyle:{color:'#dbe0e3'}},axisLabel:{show:false},axisTick:{show:false},splitLine:{show:false}},{type:'category',gridIndex:1,data:dates,boundaryGap:true,axisLine:{lineStyle:{color:'#dbe0e3'}},axisLabel:{color:'#7e8990',fontSize:10,formatter:d=>d.slice(4,6)+'/'+d.slice(6,8),showMaxLabel:true,hideOverlap:true},axisTick:{show:false}}],yAxis:[{type:'value',scale:true,splitNumber:5,axisLabel:{color:'#7e8990',fontSize:10,formatter:v=>Number(v).toFixed(1)},splitLine:{lineStyle:{color:'#edf0f2'}},axisPointer:{label:{precision:2}}},{type:'value',scale:true,gridIndex:1,splitNumber:2,axisLabel:{color:'#7e8990',fontSize:10,formatter:v=>fmt(v/10000,0)+'万'},splitLine:{lineStyle:{color:'#edf0f2'}}}],series:[{name:'日线',type:'candlestick',data:bars.map(b=>[b.open,b.close,b.low,b.high]),itemStyle:{color:'#c74949',color0:'#29866b',borderColor:'#c74949',borderColor0:'#29866b'},barMaxWidth:17,markArea:{silent:true,itemStyle:{color:'rgba(74,130,154,.10)'},label:{show:false},data:[[{xAxis:s.box_start,yAxis:s.box_bottom},{xAxis:s.box_end,yAxis:s.box_top}]]}},level('最高收盘箱顶',s.box_top,'#205c71',false),level('最低收盘箱底',s.box_bottom,'#a6bec8',false),level('前20日最高价',s.wick_top,'#ad7721',true),{name:'信号日',type:'scatter',data:[[s.date,last.high]],symbol:'pin',symbolSize:32,silent:true,itemStyle:{color:'#205c71'},label:{show:true,formatter:'D',color:'#fff',fontSize:11},z:6},{name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:bars.map(b=>({value:b.volume,itemStyle:{color:b.close>=b.open?'#d79191':'#8cb9a9'}})),barMaxWidth:17}],aria:{enabled:true,description:s.name+'，信号日'+date(s.date)+'，此前20日最高收盘价'+fmt(s.box_top)+'元，D日收盘'+fmt(last.close)+'元，成交量为前5日均量的'+fmt(s.volume_ratio5)+'倍。'}};
 chart.setOption(option,true);$('chart').setAttribute('aria-label',option.aria.description);
 document.querySelectorAll('.stock-item').forEach((el,i)=>{el.classList.toggle('active',i===selected);el.setAttribute('aria-pressed',String(i===selected));});$('mobileSelect').value=String(selected);document.querySelectorAll('[data-days]').forEach(el=>{el.classList.toggle('active',Number(el.dataset.days)===viewDays);el.setAttribute('aria-pressed',String(Number(el.dataset.days)===viewDays));});
}
$('stockList').addEventListener('click',e=>{const b=e.target.closest('[data-index]');if(b){selected=Number(b.dataset.index);render();}});$('mobileSelect').addEventListener('change',e=>{selected=Number(e.target.value);render();});$('ranges').addEventListener('click',e=>{const b=e.target.closest('[data-days]');if(b){viewDays=Number(b.dataset.days);render();}});window.addEventListener('resize',()=>{chart.resize();render();});render();
</script></body></html>'''


def render(source: Path, destination: Path):
    charts = prepare_charts(json.loads(source.read_text(encoding="utf-8")))
    vendor = ROOT / "dashboard/mobile/vendor/echarts.min.js"
    library = vendor.read_text(encoding="utf-8")
    if "Licensed to the Apache Software Foundation" not in library:
        raise ValueError("ECharts 许可证注释缺失")
    data = json.dumps(charts, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    html = TEMPLATE.replace("__ECHARTS__", library.replace("</script", "<\\/script")).replace("__DATA__", data)
    destination.write_text(html, encoding="utf-8")
    return {"output": str(destination), "cases": len(charts), "future_bars": 0,
            "bytes": destination.stat().st_size,
            "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            "vendor_sha256": hashlib.sha256(vendor.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "docs/box-breakout-20261018-results.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/box-breakout-20261018-charts.html")
    args = parser.parse_args()
    print(json.dumps(render(args.input, args.output), ensure_ascii=False))
