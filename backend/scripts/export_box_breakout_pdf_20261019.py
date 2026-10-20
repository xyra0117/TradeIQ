"""Format the completed research into a PDF; no SQLite or business imports."""
from __future__ import annotations

import hashlib
import json
import re
from html import escape
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Flowable, Frame, KeepTogether, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / 'docs'
PREFIX = 'box-breakout-20261018'
WORK = Path('/tmp/tradeiq-box-pdf-20261019')
OUTPUT = DOCS / (PREFIX + '.pdf')
PAGE_W, PAGE_H = A4
MARGIN = 45
WIDTH = PAGE_W - MARGIN * 2
INK = colors.HexColor('#243644')
TEAL = colors.HexColor('#205C71')
MUTED = colors.HexColor('#657582')
LINE = colors.HexColor('#DAE2E6')
PALE = colors.HexColor('#EFF5F7')
RED = colors.HexColor('#BF4949')
GREEN = colors.HexColor('#27846B')
GOLD = colors.HexColor('#AD7721')


def fonts():
    pdfmetrics.registerFont(TTFont('CJK', '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'))
    pdfmetrics.registerFont(TTFont('CJK-Bold', '/System/Library/Fonts/STHeiti Medium.ttc', subfontIndex=0))
    pdfmetrics.registerFontFamily('CJK', normal='CJK', bold='CJK-Bold', italic='CJK', boldItalic='CJK-Bold')


def plain(text):
    return text.replace('−', '-').replace('—', '-').replace('–', '-').replace('\u2011', '-')


def rich(text):
    text = escape(plain(text))
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'`([^`]+)`', r'<font name="Courier" size="8.1">\1</font>', text)
    return text


def styles():
    common = dict(fontName='CJK', textColor=INK, wordWrap='CJK', splitLongWords=True)
    return {
        'body': ParagraphStyle('body', fontSize=10, leading=16.5, spaceAfter=8, **common),
        'small': ParagraphStyle('small', fontSize=8.5, leading=13.5, spaceAfter=6, **common),
        'h1': ParagraphStyle('h1', fontName='CJK-Bold', fontSize=17, leading=23, textColor=TEAL,
                             spaceBefore=15, spaceAfter=10, keepWithNext=True, wordWrap='CJK'),
        'h2': ParagraphStyle('h2', fontName='CJK-Bold', fontSize=12, leading=18, textColor=TEAL,
                             spaceBefore=10, spaceAfter=7, keepWithNext=True, wordWrap='CJK'),
        'cell': ParagraphStyle('cell', fontSize=9, leading=13.5, **common),
        'headcell': ParagraphStyle('headcell', fontName='CJK-Bold', fontSize=8.7, leading=13,
                                   textColor=TEAL, wordWrap='CJK'),
        'code': ParagraphStyle('code', fontName='Courier', fontSize=8.7, leading=13,
                               textColor=TEAL, leftIndent=10, rightIndent=8,
                               borderColor=LINE, borderWidth=.5, borderPadding=10,
                               backColor=PALE, spaceBefore=5, spaceAfter=14, splitLongWords=True),
        'title': ParagraphStyle('title', fontName='CJK-Bold', fontSize=28, leading=38, textColor=INK,
                                spaceAfter=7),
        'subtitle': ParagraphStyle('subtitle', fontName='CJK', fontSize=10.5, leading=17, textColor=MUTED,
                                   wordWrap='CJK', spaceAfter=17),
    }


class ResearchDoc(BaseDocTemplate):
    def __init__(self, path):
        super().__init__(str(path), pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN,
                         topMargin=53, bottomMargin=46, title='箱体突破研究：11例形态与筛选条件',
                         author='TradeIQ 本地研究', subject='2026年指定样本、筛选条件和形态图')
        self.heading_index = 0
        frame = Frame(MARGIN, 46, WIDTH, PAGE_H - 99, leftPadding=0, rightPadding=0,
                      topPadding=0, bottomPadding=0)
        self.addPageTemplates(PageTemplate(id='research', frames=frame, onPage=self.decorate))

    def decorate(self, canvas, doc):
        canvas.saveState()
        canvas.setStrokeColor(LINE)
        canvas.setLineWidth(.5)
        canvas.line(MARGIN, PAGE_H - 35, PAGE_W - MARGIN, PAGE_H - 35)
        canvas.setFont('Helvetica-Bold', 8)
        canvas.setFillColor(TEAL)
        canvas.drawString(MARGIN, PAGE_H - 25, 'TRADEIQ  /  RESEARCH')
        canvas.setFont('CJK', 8)
        canvas.setFillColor(MUTED)
        canvas.drawRightString(PAGE_W - MARGIN, PAGE_H - 25, '箱体突破样本研究 | 2026')
        canvas.line(MARGIN, 33, PAGE_W - MARGIN, 33)
        canvas.setFont('CJK', 7.4)
        canvas.drawString(MARGIN, 21, '本地数据研究 · 收盘确认 · 样本覆盖不等于胜率')
        canvas.setFont('Helvetica', 8)
        canvas.drawRightString(PAGE_W - MARGIN, 21, f'{doc.page:02d}')
        canvas.restoreState()

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and flowable.style.name in ('h1', 'h2'):
            self.heading_index += 1
            key = f'section-{self.heading_index}'
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(flowable.getPlainText(), key, level=0 if flowable.style.name == 'h1' else 1, closed=False)


def table(rows, s):
    n = len(rows[0])
    first = rows[0][0]
    if n == 3:
        widths = [91, 171, WIDTH - 262]
    elif n == 4 and first == '版本':
        widths = [147, 60, 70, WIDTH - 277]
    elif n == 4:
        widths = [57, 106, 233, WIDTH - 396]
    elif n == 6 and rows[0][1] == '指定日期':
        widths = [66, 51, 62, 85, 111, WIDTH - 375]
    elif n == 6:
        widths = [80] + [(WIDTH - 80) / 5] * 5
    else:
        widths = [WIDTH / n] * n
    cells = [[Paragraph(rich(x), s['headcell'] if i == 0 else s['cell']) for x in row]
             for i, row in enumerate(rows)]
    t = Table(cells, colWidths=widths, repeatRows=1, hAlign='LEFT', spaceBefore=5, spaceAfter=12)
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), PALE),
        ('LINEBELOW', (0, 0), (-1, 0), .7, LINE),
        ('LINEBELOW', (0, 1), (-1, -1), .35, LINE),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FAFCFC')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 7),
    ]))
    return t


def markdown_flow(text, s):
    lines = text.splitlines()
    story = []
    i = 0
    # Keep page order readable without forcing every small section onto a new page.
    fresh_page = {'可以怎么设置', '逐只核对', '收紧后会漏掉谁', '资金与市场补充',
                  '本地缺什么，影响什么', '现有“自定义选股”可用的公式'}
    while i < len(lines):
        line = lines[i].strip()
        if not line or line.startswith('# ') or line.startswith('开始于'):
            i += 1
            continue
        if line.startswith('```'):
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code.append(lines[i])
                i += 1
            story.append(Paragraph(escape('\n'.join(code)), s['code']))
            i += 1
            continue
        if line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].strip().startswith('|'):
                row = [x.strip() for x in lines[i].strip().strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?', x) for x in row):
                    rows.append(row)
                i += 1
            story.append(table(rows, s))
            continue
        if line.startswith('## '):
            title = line[3:]
            if title in fresh_page:
                story.append(PageBreak())
            story.append(Paragraph(rich(title), s['h1']))
        elif line.startswith('### '):
            story.append(Paragraph(rich(line[4:]), s['h2']))
        elif line.startswith('- '):
            style = ParagraphStyle('bullet', parent=s['body'], leftIndent=12, firstLineIndent=-10)
            story.append(Paragraph('• ' + rich(line[2:]), style))
        else:
            story.append(Paragraph(rich(line), s['body']))
        i += 1
    return story


class CandleChart(Flowable):
    """Vector price and volume plot; all candles end on the specified signal day."""
    def __init__(self, item, width=WIDTH, height=223):
        super().__init__()
        self.item, self.width, self.height = item, width, height

    def draw(self):
        c = self.canv
        bars = self.item['bars'][-61:]
        assert bars[-1]['trade_date'] == self.item['date']
        assert all(b['trade_date'] <= self.item['date'] for b in bars)
        box = bars[-21:-1]
        top, bottom = max(b['close'] for b in box), min(b['close'] for b in box)
        wick = max(b['high'] for b in box)
        left, right, y0, chart_h = 40, self.width - 12, 65, 142
        pmin, pmax = min(b['low'] for b in bars), max(b['high'] for b in bars)
        pad = (pmax - pmin) * .09 or 1
        pmin, pmax = pmin - pad, pmax + pad
        py = lambda v: y0 + (v - pmin) / (pmax - pmin) * chart_h
        step = (right - left) / len(bars)
        px = lambda i: left + step * (i + .5)
        vol_max = max(b['volume'] for b in bars) * 1.08
        c.setFillColor(PALE)
        c.rect(px(len(bars) - 21) - step / 2, py(bottom), step * 20,
               py(top) - py(bottom), fill=1, stroke=0)
        c.setFont('Helvetica', 7)
        for k in range(5):
            val = pmin + (pmax - pmin) * k / 4
            y = py(val)
            c.setStrokeColor(colors.HexColor('#EDF1F3'))
            c.setLineWidth(.4)
            c.line(left, y, right, y)
            c.setFillColor(MUTED)
            c.drawRightString(left - 6, y - 2.3, f'{val:.1f}')
        for level, color, dash in [(top, TEAL, []), (bottom, colors.HexColor('#A6BEC8'), []), (wick, GOLD, [4, 2])]:
            c.setStrokeColor(color)
            c.setLineWidth(.8)
            c.setDash(dash)
            c.line(px(len(bars) - 21) - step / 2, py(level), px(len(bars) - 2) + step / 2, py(level))
        c.setDash([])
        body_w = min(step * .63, 5.8)
        for i, b in enumerate(bars):
            color = RED if b['close'] >= b['open'] else GREEN
            x = px(i)
            c.setStrokeColor(color)
            c.setFillColor(color)
            c.setLineWidth(.55)
            c.line(x, py(b['low']), x, py(b['high']))
            ymin = min(py(b['open']), py(b['close']))
            c.rect(x - body_w / 2, ymin, body_w, max(abs(py(b['close']) - py(b['open'])), .65), fill=1, stroke=0)
            c.setFillColor(colors.HexColor('#D79797') if b['close'] >= b['open'] else colors.HexColor('#92BEAE'))
            c.rect(x - body_w / 2, 23, body_w, b['volume'] / vol_max * 30, fill=1, stroke=0)
        c.setFillColor(MUTED)
        c.setFont('CJK', 6.8)
        c.drawString(0, chart_h + y0 - 2, '价格/元')
        c.drawString(0, 49, '成交量')
        c.setFont('Helvetica', 6.6)
        c.drawString(0, 37, f'{vol_max / 10000:.0f}')
        c.setFont('CJK', 6.5)
        c.drawString(15, 37, '万手')
        c.setFont('Helvetica', 6.7)
        for i in sorted({0, 12, 24, 36, 48, len(bars) - 1}):
            if i < len(bars):
                day = bars[i]['trade_date']
                c.setFillColor(MUTED)
                c.drawCentredString(px(i), 11, day[4:6] + '/' + day[6:8])
        c.setFillColor(TEAL)
        c.circle(px(len(bars) - 1), py(bars[-1]['high']) + 8, 5, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont('Helvetica-Bold', 6)
        c.drawCentredString(px(len(bars) - 1), py(bars[-1]['high']) + 6, 'D')


def case_card(item, case, s, index):
    bars = item['bars']
    name, day = item['name'], item['date']
    title = f'{index:02d}  {name}  <font size="9" color="#657582">{item["code"]} · {day[:4]}-{day[4:6]}-{day[6:]}</font>'
    metrics = (f'当日 {case["day_change"]:.2f}%   |   量/前5日均量 {case["volume_ratio5"]:.2f}倍   |   '
               f'收盘箱顶突破 +{case["break_close20"]:.2f}%   |   相对最高价 {case["break_high20"]:+.2f}%')
    if case['break_high20'] <= 0:
        note = '突破收盘压力，但尚未完全越过此前20日最高价。严格要求越过上影线高点会漏掉此例。'
    elif case['prior_close_break20'] > 0:
        note = '前一交易日已突破其当时的20日最高收盘价；本日更接近继续确认，不能称该口径下首次突破。'
    else:
        note = '本日突破此前20日最高收盘价及最高价；昨日尚未突破昨日自身的20日收盘箱顶。'
    if not case['valid60']:
        note += ' 60日展示含价格参考断点，长箱体暂不可信；蓝色20日区间已单独核验。'
    note += ' 此图只展示D日及以前数据。'
    return KeepTogether([
        Paragraph(title, s['h2']),
        Paragraph(plain(metrics), s['small']),
        CandleChart(item),
        Paragraph(note, s['small']),
        Spacer(1, 14),
    ])


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    fonts()
    s = styles()
    source = DOCS / (PREFIX + '.md')
    raw = json.loads((DOCS / (PREFIX + '-results.json')).read_text())
    assert len(raw['charts']) == len(raw['cases']) == 11
    story = [
        Spacer(1, 12),
        Paragraph('箱体突破研究', s['title']),
        Paragraph('11个指定样本的共同形态与选股条件', s['subtitle']),
        Paragraph('研究完成：2026-10-19　｜　样本日期按2026年解释　｜　基于本地数据库', s['small']),
        Spacer(1, 11),
    ]
    kpis = [[Paragraph('<font name="Helvetica-Bold" size="22">11 / 11</font><br/>指定样本覆盖', s['small']),
             Paragraph('<font name="Helvetica-Bold" size="22">86.3</font><br/>指定日期日均候选/只', s['small']),
             Paragraph('<font name="Helvetica-Bold" size="22">20</font><br/>整理窗口/交易日', s['small'])]]
    k = Table(kpis, colWidths=[WIDTH / 3] * 3, spaceAfter=10)
    k.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, -1), PALE), ('TOPPADDING', (0, 0), (-1, -1), 17),
                          ('BOTTOMPADDING', (0, 0), (-1, -1), 13), ('LEFTPADDING', (0, 0), (-1, -1), 15),
                          ('VALIGN', (0, 0), (-1, -1), 'TOP')]))
    story.append(k)
    text = source.read_text().replace(
        '保持同样阈值，只把周期20改为30/40/60日，样本覆盖分别为11/7/5/2例，候选日均86.3/47.3/29.5/12.5只。',
        '保持同样阈值，周期分别取20/30/40/60日时，样本覆盖分别为11/7/5/2例，候选日均分别为86.3/47.3/29.5/12.5只。')
    story.extend(markdown_flow(text, s))
    story.extend([PageBreak(), Paragraph('附录：11例价格与成交量形态图', s['h1']),
                  Paragraph('统一展示信号日前60个交易日及D日。蓝色区域为前20日最高/最低收盘价区间；蓝线为最高收盘价，金色虚线为同窗最高价。红涨绿跌，D为指定入池日。原始未复权，所有图不包含未来行情。', s['small']),
                  Paragraph('注意：图中的蓝色收盘区间宽度，与筛选条件中的“最高价÷最低价-1”是两种口径；35%阈值使用包含上下影线的高低范围。', s['small'])])
    for i, item in enumerate(raw['charts']):
        if i and i % 2 == 0:
            story.append(PageBreak())
        case = next(x for x in raw['cases'] if x['code'] == item['code'] and x['date'] == item['date'])
        story.append(case_card(item, case, s, i + 1))
    story.extend([Spacer(1, 10), Paragraph('阅读提醒：覆盖全部11例只说明条件能描述这批人工选出的形态。是否能减少假突破、获得可成交收益，需要冻结规则后使用新的样本继续验证。', s['small'])])
    ResearchDoc(OUTPUT).build(story)
    print(json.dumps({'output': str(OUTPUT), 'bytes': OUTPUT.stat().st_size,
                      'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                      'sha256': hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == '__main__':
    main()
