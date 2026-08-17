#!/usr/bin/env python3
"""策略说明书HTML生成器 v2 — 案例带买卖点标注 + 日K/30min双Tab。

含: 概念示意(分型/笔/中枢/背驰) + 真实案例(日K/30min Tab, 红箭头入场绿箭头出场)
   + 规则参数 + 三年回测结果。自包含(内嵌echarts.min.js)。
"""
import sys, os, json, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timedelta

from chan.paths import DB_PATH, PROJECT_ROOT
import gen_30min_chart as gc

OUT = os.path.join(PROJECT_ROOT, 'charts', '策略说明书.html')
ECHARTS = gc.ECHARTS_JS

# 案例: (sym, 标题, 描述, 结果文件年份)
CASES = [
    {'sym': '002816.SZ', 'title': '案例1 · 一买(趋势背驰) +103.8%',
     'desc': '2024-08-28入场@7.45 → 10-15出场。日K下跌趋势衰竭(两个向下中枢+创新低+MACD背驰), 30min区间套介入, 主升段吃到+103.8%。',
     'teach': '① 日K图找两个向下中枢(绿色矩形, 依次下移=下跌趋势) → ② 第二个中枢后的下跌段创新低且MACD面积变小(紫色箭头=一买背驰点) → ③ 价格回落到背驰点附近(红箭头=入场) → ④ 上涨后中枢上移, 移动止盈离场(绿箭头)。切30min视图看介入细节。',
     'year': '2024'},
    {'sym': '000592.SZ', 'title': '案例2 · 三买(中枢突破回踩) +151.7%',
     'desc': '2025-09-05入场@3.31 → 11-07出场。向上突破中枢后回踩不破ZG=最强买点, 30min确认介入, +151.7%。',
     'teach': '① 日K图找向上中枢(绿色矩形) → ② 价格向上突破ZG(黄虚线) → ③ 回踩不跌回中枢(红箭头=三买入场, 突破有效) → ④ 主升段+151%。三买是三类买点中最强的: 它出现在"已经涨起来"之后, 顺势而为。',
     'year': '2025'},
    {'sym': '002154.SZ', 'title': '案例3 · 二买止损 -10.6%(风险控制)',
     'desc': '2026-06-02入场@4.17 → 06-26出场。二买后结构破坏(跌破背驰点A), 触发止损-10.6%——风险底线展示。',
     'teach': '① 一买后的回调不破前低=二买(红箭头入场) → ② 入场后价格跌破背驰点A(紫色虚线=止损线) → ③ 结构破坏, 止损离场(绿箭头, -10.6%)。缠论的核心纪律: 结构破坏必须走——保住本金才能等下一个买点。',
     'year': '2026'},
]


def load_trades(year):
    r = json.load(open(os.path.join(PROJECT_ROOT, 'results', f'bt_full30_{year}.json')))
    out = {}
    for bt, ts in r.items():
        for t in ts:
            out.setdefault(t['symbol'], []).append(t)
    return out


def concept_schemas():
    """4个概念示意图 — 全部用真实算法(fractal/bi/zhongshu/macd)计算, 数据取自000537日K。

    概念①分型: find_fractals 识别顶/底分型 → 箭头标注
    概念②笔:   build_bi → 金色折线(算法笔序列)
    概念③中枢: build_bi+build_zs → 中枢矩形(算法ZG/ZD) + 虚线
    概念④背驰: macd面积对比(前段大后段小=背驰)
    """
    import sqlite3
    from chan.fractal import find_fractals
    from chan.bi import build_bi
    from chan.zhongshu import build_zs
    from chan.state import clear_level, clear_all
    from chan.bars import macd, macd_area
    from chan.daily_scan import load_daily

    conn = sqlite3.connect(DB_PATH, timeout=60)
    bars = load_daily('000537.SZ', conn=conn)
    conn.close()
    clear_all()

    def fmt(d):
        return d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d)[:10]

    # ── 概念① 分型: 最近40根, 算法识别 → 箭头标注 ──
    seg1 = bars[-40:]
    fx = find_fractals(seg1)
    marks = []
    for f in fx:
        marks.append({'x': fmt(f['dt']), 'label': '顶分型' if f['type'] == 'top' else '底分型',
                      'color': '#ef5350' if f['type'] == 'top' else '#26a69a',
                      'dir': -1 if f['type'] == 'top' else 1,
                      'price': f['price']})
    fx_schema = {
        'title': '概念① 分型——走势的"信号灯"(真实算法: 三根K线中间最高/最低)',
        'desc': '来自000537.SZ真实日K。算法检测: 红色箭头=顶分型(中间K线最高), 绿色箭头=底分型(中间K线最低)。分型是笔与中枢的基础。',
        'dates': [fmt(b.dt) for b in seg1],
        'bars': [[round(b.open, 2), round(b.close, 2), round(b.low, 2), round(b.high, 2)] for b in seg1],
        'fx_marks': marks,
    }

    # ── 概念② 笔: 近120根, build_bi → 笔折线 ──
    seg2 = bars[-120:]
    clear_level('D')
    bis2 = build_bi(seg2, level='D')
    line = []
    for bi in bis2:
        s, e = fmt(bi.start_dt), fmt(bi.end_dt)
        sp = bi.low if bi.direction == 'up' else bi.high
        ep = bi.high if bi.direction == 'up' else bi.low
        line.append([s, round(sp, 2)])
        line.append([e, round(ep, 2)])
    bi_schema = {
        'title': '概念② 笔——最小运动单元(真实算法: 顶底分型连线, ≥6根K线)',
        'desc': '来自000537.SZ真实日K。金色折线=算法识别出的笔序列(底→顶→底→顶)。一段段笔构成走势, 中枢由三笔重叠形成。',
        'dates': [fmt(b.dt) for b in seg2],
        'bars': [[round(b.open, 2), round(b.close, 2), round(b.low, 2), round(b.high, 2)] for b in seg2],
        'bi_line': line,
    }

    # ── 概念③ 中枢: 近200根, build_zs → 中枢矩形(算法ZG/ZD) ──
    seg3 = bars[-200:]
    clear_level('D')
    bis3 = build_bi(seg3, level='D')
    clear_level('D')
    zs3 = [z for z in build_zs(bis3, level='D') if z.zg > z.zd * 1.003]
    zs_schema = {
        'title': '概念③ 中枢——多空"拉锯区"(真实算法: 至少三笔重叠, ZG=重叠高最小值, ZD=重叠低最大值)',
        'desc': '来自000537.SZ真实日K。绿色矩形=算法识别的中枢(下沿ZD/上沿ZG)。价格在其中拉锯, 突破且回踩不回=中枢结束。',
        'dates': [fmt(b.dt) for b in seg3],
        'bars': [[round(b.open, 2), round(b.close, 2), round(b.low, 2), round(b.high, 2)] for b in seg3],
        'zs_list': [{'s': fmt(z.start_dt), 'e': fmt(z.end_dt), 'zg': round(z.zg, 2), 'zd': round(z.zd, 2)} for z in zs3],
    }

    # ── 概念④ 背驰: 近150根, MACD面积对比(下跌段前/后) ──
    seg4 = bars[-150:]
    closes4 = [b.close for b in seg4]
    diff4, dea4, hist4 = macd(closes4)
    # 找最近一段下跌: 从摆动高点 → 最低点, 前1/3面积 vs 后1/3面积
    n = len(seg4)
    hi_i = n - 1
    for i in range(n - 2, max(0, n - 60), -1):
        if seg4[i].high > seg4[hi_i].high:
            hi_i = i
    low_i = hi_i
    for i in range(hi_i + 1, n):
        if seg4[i].low < seg4[low_i].low:
            low_i = i
    third = (low_i - hi_i) // 3
    if third < 3:
        hi_i, low_i, third = 0, n - 1, (n - 1) // 3
    area_front = abs(macd_area(diff4, dea4, hi_i, hi_i + third, 'down'))
    area_back = abs(macd_area(diff4, dea4, low_i - third, low_i, 'down'))
    bc_schema = {
        'title': '概念④ 背驰——下跌的"油尽灯枯"(真实算法: MACD绿柱面积比较)',
        'desc': ('来自000537.SZ真实日K。下跌段前1/3绿柱面积=' + str(round(area_front, 1)) +
                 ', 后1/3面积=' + str(round(area_back, 1)) +
                 ' → 后段明显小于前段=力度衰竭(背驰)。价格创新低但跌不动了。'),
        'dates': [fmt(b.dt) for b in seg4],
        'bars': [[round(b.open, 2), round(b.close, 2), round(b.low, 2), round(b.high, 2)] for b in seg4],
        'macd_hist': [round(h, 3) for h in hist4],
        'macd_data': [{'value': round(h, 3), 'itemStyle': {'color': '#ef5350' if h >= 0 else '#26a69a'}} for h in hist4],
        'area_note': {'front': round(area_front, 1), 'back': round(area_back, 1)},
    }
    return [fx_schema, bi_schema, zs_schema, bc_schema]


def build_concept_option(s):
    """概念示意图 → ECharts option (按真实算法数据渲染)"""
    bars = s['bars']
    op = {
        'animation': False, 'backgroundColor': '#1e222d',
        'title': {'text': s['title'], 'left': 8, 'top': 2,
                  'textStyle': {'color': '#e8eaf0', 'fontSize': 13, 'fontWeight': 600}},
        'grid': {'left': 46, 'right': 10, 'top': 38, 'bottom': 24},
        'xAxis': {'type': 'category', 'data': s['dates'],
                  'axisLabel': {'color': '#9aa0ab', 'fontSize': 9, 'interval': 4},
                  'axisLine': {'lineStyle': {'color': '#3d4250'}}},
        'yAxis': {'scale': True,
                  'axisLabel': {'color': '#9aa0ab', 'fontSize': 9},
                  'splitLine': {'lineStyle': {'color': 'rgba(61,66,80,0.3)'}}},
        'series': [{'type': 'candlestick', 'data': bars,
                    'itemStyle': {'color': '#ef5350', 'color0': '#26a69a',
                                  'borderColor': '#ef5350', 'borderColor0': '#26a69a'}}],
    }
    # ① 分型: 算法识别点 → 箭头标注
    if 'fx_marks' in s:
        def bar_idx(dt):
            for i, x in enumerate(s['dates']):
                if x >= dt:
                    return i
            return len(s['dates']) - 1
        pts = []
        for m in s['fx_marks']:
            bi = bar_idx(m['x'])
            y = bars[bi][2] if m['dir'] > 0 else bars[bi][3]
            pts.append({'coord': [m['x'], y], 'symbol': 'arrow',
                        'symbolRotate': 0 if m['dir'] < 0 else 180, 'symbolSize': 16,
                        'symbolOffset': [0, 16 if m['dir'] > 0 else -16],
                        'itemStyle': {'color': m['color']},
                        'label': {'formatter': m['label'], 'fontSize': 10, 'color': m['color'],
                                  'position': 'bottom' if m['dir'] > 0 else 'top'}})
        op['series'][0]['markPoint'] = {'data': pts}
    # ② 笔: 算法笔折线
    if 'bi_line' in s:
        op['series'].append({'type': 'line', 'data': s['bi_line'], 'connectNulls': True,
                             'symbol': 'circle', 'symbolSize': 3,
                             'lineStyle': {'color': '#ffb300', 'width': 1.8}})
    # ③ 中枢: 算法矩形 + ZG/ZD虚线
    if 'zs_list' in s:
        area = []
        lines = []
        for z in s['zs_list']:
            area.append([{'xAxis': z['s'], 'yAxis': z['zd'],
                          'itemStyle': {'color': 'rgba(38,166,154,0.22)',
                                        'borderColor': '#26a69a', 'borderWidth': 1.5, 'borderType': 'dashed'},
                          'label': {'show': True, 'position': 'insideTop', 'distance': 2,
                                    'formatter': '◆ 中枢  ZG:' + str(z['zg']) + '  ZD:' + str(z['zd']),
                                    'fontSize': 10, 'color': '#fff',
                                    'backgroundColor': 'rgba(38,166,154,0.85)', 'padding': [2, 6], 'borderRadius': 3}},
                         {'xAxis': z['e'], 'yAxis': z['zg']}])
            lines.append([{'xAxis': z['s'], 'yAxis': z['zg'],
                           'label': {'formatter': 'ZG ' + str(z['zg']), 'position': 'insideEndTop', 'fontSize': 9, 'color': '#ffb300'},
                           'lineStyle': {'color': '#ffb300', 'type': 'dashed', 'width': 1, 'opacity': 0.8}},
                          {'xAxis': z['e'], 'yAxis': z['zg']}])
            lines.append([{'xAxis': z['s'], 'yAxis': z['zd'],
                           'label': {'formatter': 'ZD ' + str(z['zd']), 'position': 'insideEndBottom', 'fontSize': 9, 'color': '#42a5f5'},
                           'lineStyle': {'color': '#42a5f5', 'type': 'dashed', 'width': 1, 'opacity': 0.8}},
                          {'xAxis': z['e'], 'yAxis': z['zd']}])
        op['series'][0]['markArea'] = {'silent': True, 'data': area}
        op['series'][0]['markLine'] = {'silent': True, 'symbol': 'none', 'data': lines}
    # ④ 背驰: MACD柱副图
    if 'macd_hist' in s:
        op['grid'] = [{'left': 46, 'right': 10, 'top': 38, 'height': '52%'},
                      {'left': 46, 'right': 10, 'top': '62%', 'height': '20%'}]
        op['xAxis'] = [
            {'type': 'category', 'data': s['dates'], 'axisLabel': {'color': '#9aa0ab', 'fontSize': 9, 'interval': 4}, 'axisLine': {'lineStyle': {'color': '#3d4250'}}},
            {'type': 'category', 'gridIndex': 1, 'data': s['dates'], 'axisLabel': {'show': False}}]
        op['yAxis'] = [
            {'scale': True, 'axisLabel': {'color': '#9aa0ab', 'fontSize': 9}, 'splitLine': {'lineStyle': {'color': 'rgba(61,66,80,0.3)'}}},
            {'gridIndex': 1, 'axisLabel': {'show': False}, 'splitLine': {'show': False}}]
        op['series'] = [
            {'type': 'candlestick', 'data': bars, 'itemStyle': {'color': '#ef5350', 'color0': '#26a69a', 'borderColor': '#ef5350', 'borderColor0': '#26a69a'}},
            {'type': 'bar', 'xAxisIndex': 1, 'yAxisIndex': 1, 'data': s.get('macd_data', s['macd_hist']), 'barWidth': '55%'},
        ]
    return op


def case_data(sym, trades):
    """案例数据: 日K窗口=交易±150天(与30min对齐, 都能看到交易前后结构) + 30min"""
    ext = gc.extract_daily(sym)
    ds = [t['entry'] for t in trades] + [t['exit'] for t in trades]
    # 教学窗口: 买入前300天(看到完整下跌趋势+两个中枢) ~ 卖出后60天(看到持有段)
    w0 = (min(datetime.strptime(d, '%Y-%m-%d') for d in ds) - timedelta(days=300)).strftime('%Y-%m-%d')
    w1 = (max(datetime.strptime(d, '%Y-%m-%d') for d in ds) + timedelta(days=60)).strftime('%Y-%m-%d')
    dp = gc.daily_plot(ext, plot_start=w0, plot_end=w1)
    mp = gc.m30_plot(sym, trades)
    ts = [{'type': t.get('name', ''), 'entry': t['entry'], 'exit': t['exit'],
           'ep': t['entry_price'], 'xp': t['exit_price'],
           'pnl': t['pnl_pct'], 'reason': t['reason']} for t in trades]
    return {'dp': dp, 'm30': mp, 'trades': ts}


def main():
    trades_by_year = {c['year']: load_trades(c['year']) for c in CASES}
    cases = {}
    for c in CASES:
        trs = trades_by_year[c['year']].get(c['sym'], [])
        if not trs:
            print(f"  {c['sym']} 无交易记录!")
            continue
        try:
            cases[c['sym']] = {**case_data(c['sym'], trs), 'meta': c}
            print(f"  {c['sym']} OK 交易{len(trs)}笔")
        except Exception as e:
            print(f"  {c['sym']} 失败: {e}")

    echarts = open(ECHARTS).read()
    html = TEMPLATE
    html = html.replace('/*__ECHARTS__*/', echarts)
    html = html.replace('/*__CONCEPTS__*/', json.dumps([build_concept_option(s) for s in concept_schemas()], ensure_ascii=False))
    html = html.replace('/*__CASES__*/', json.dumps(cases, ensure_ascii=False))
    with open(OUT, 'w') as f:
        f.write(html)
    print(f"已生成: {OUT} ({os.path.getsize(OUT)/1024/1024:.1f}MB)")


TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>缠论量化策略说明书</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:"PingFang SC","Microsoft YaHei",sans-serif; background:#131722; color:#d1d4dc; line-height:1.75; }
.hero { background:linear-gradient(135deg,#1e3a5f 0%,#131722 70%); padding:48px 40px 40px; text-align:center; }
.hero h1 { font-size:30px; color:#fff; letter-spacing:2px; margin-bottom:8px; }
.hero .sub { color:#9aa0ab; font-size:15px; }
.hero .tag { display:inline-block; margin:10px 6px 0; padding:4px 14px; border-radius:16px; font-size:12px; background:#2a2e39; color:#80cbc4; border:1px solid #3d4250; }
.wrap { max-width:960px; margin:0 auto; padding:24px 20px 60px; }
h2 { color:#e8eaf0; font-size:21px; margin:40px 0 12px; padding-left:12px; border-left:4px solid #1e88e5; }
h3 { color:#e8eaf0; font-size:16px; margin:22px 0 8px; }
p, li { font-size:14px; color:#b8beca; margin-bottom:8px; }
.card { background:#1e222d; border:1px solid #2a2e39; border-radius:8px; padding:18px 20px; margin:14px 0; }
.concept { margin:22px 0; }
.concept .cdesc { font-size:13px; color:#9aa0ab; margin-top:8px; padding:0 6px; border-left:3px solid #3d4250; }
.case { background:#1e222d; border:1px solid #2a2e39; border-radius:8px; padding:18px; margin:20px 0; }
.case h3 { color:#ffb300; }
.case .cdesc { font-size:13px; color:#9aa0ab; margin:6px 0 10px; }
.teach { font-size:13px; color:#b8beca; background:#16212e; border-left:3px solid #1e88e5; padding:8px 12px; margin:8px 0 10px; border-radius:0 4px 4px 0; }
.tabs { display:flex; gap:8px; margin:10px 0; }
.tabbtn { padding:4px 16px; border-radius:4px; border:1px solid #3d4250; background:#2a2e39; color:#9aa0ab; font-size:13px; cursor:pointer; }
.tabbtn.on { background:#1e88e5; color:#fff; border-color:#1e88e5; }
.tabnote { font-size:12px; color:#9aa0ab; margin:6px 0; }
table { width:100%; border-collapse:collapse; margin:12px 0; font-size:13px; }
th, td { border:1px solid #2a2e39; padding:7px 10px; text-align:left; }
th { background:#2a2e39; color:#e8eaf0; }
td { color:#b8beca; }
.up { color:#26a69a; } .dn { color:#ef5350; }
.flow { display:flex; flex-wrap:wrap; gap:10px; margin:16px 0; }
.flow .step { flex:1; min-width:150px; background:#1e222d; border:1px solid #2a2e39; border-radius:8px; padding:12px 14px; }
.flow .step .n { color:#1e88e5; font-weight:600; font-size:12px; }
.flow .step .t { color:#e8eaf0; font-size:14px; margin:2px 0; }
.flow .step .d { color:#9aa0ab; font-size:12px; }
.warn { background:#2a1f1f; border:1px solid #ef5350; border-radius:8px; padding:14px 18px; margin:16px 0; font-size:13px; color:#e0b4b4; }
.footer { text-align:center; color:#5a6070; font-size:12px; padding:30px 0 10px; }
.chart { width:100%; height:360px; }
.chart.big { height:520px; }
.legend { font-size:12px; color:#9aa0ab; margin:4px 0; }
.legend .bu { color:#ef5350; font-weight:600; } .legend .se { color:#26a69a; font-weight:600; }
</style>
</head>
<body>
<div class="hero">
  <h1>缠论量化策略说明书</h1>
  <div class="sub">日K定买点 · 30min定介入 · 区间套精确制导</div>
  <div>
    <span class="tag">分型 → 笔 → 中枢</span>
    <span class="tag">一买/二买/三买</span>
    <span class="tag">背驰确认</span>
    <span class="tag">结构止损</span>
  </div>
</div>
<div class="wrap">

<h2>一、这个策略是干什么的？</h2>
<div class="card">
<p>本策略基于<b>缠论</b>（中国技术分析理论），核心思想：<b>走势有结构，结构有规律</b>。它把K线走势拆解成"分型→笔→中枢→背驰"的层级结构，在<b>下跌力度衰竭</b>时买入，在<b>结构破坏</b>时卖出。</p>
<p>系统用两级K线配合（这就是"区间套"）：</p>
<ul>
  <li><b>日K线</b>：找买点（判断大级别下跌是否衰竭）——管"买不买"</li>
  <li><b>30分钟K线</b>：精确定位介入点（日K确认后的次级别走势）——管"什么时候买"</li>
</ul>
<p>简单说：<b>日K告诉你"这票跌到头了"，30min告诉你"就现在这一刻买"</b>。</p>
</div>

<h2>二、五个基础概念</h2>
<div id="conceptCharts"></div>

<div class="card">
<h3>概念⑤ 三类买点</h3>
<ul>
  <li><b>一买（趋势背驰）</b>：下跌趋势最后衰竭点——"跌无可跌"，最底部，空间最大</li>
  <li><b>二买（回调确认）</b>：一买后第一次回调不破前低——"确认跌不动了"，更安全</li>
  <li><b>三买（突破回踩）</b>：向上突破中枢后回踩不跌回中枢——"突破有效"，最强信号</li>
</ul>
</div>

<h2>三、系统流程：从选股到出场</h2>
<div class="flow">
  <div class="step"><div class="n">STEP 1</div><div class="t">日K扫描候选</div><div class="d">全市场日K线, 找出一买/二买/三买结构(52周内有效)</div></div>
  <div class="step"><div class="n">STEP 2</div><div class="t">四道过滤</div><div class="d">①三卖检查 ②低位检查 ③当下重算背驰 ④窗口过期作废</div></div>
  <div class="step"><div class="n">STEP 3</div><div class="t">30min区间套</div><div class="d">最后中枢+离开段背驰+底分型+MACD力竭 → 入场</div></div>
  <div class="step"><div class="n">STEP 4</div><div class="t">持仓管理</div><div class="d">结构止损 / 止损10% / 移动止盈(峰值+5%回撤10%走)</div></div>
</div>

<h2>四、真实案例（日K / 30min 对照，红箭头=买入，绿箭头=卖出）</h2>
<div class="legend"><span class="bu">▲ 买入点</span>（含价格与买点类型） &nbsp; <span class="se">▼ 卖出点</span>（含价格、盈亏与出场原因） &nbsp; 绿色矩形=中枢</div>
<div id="caseCharts"></div>

<h2>五、规则与参数</h2>
<div class="card">
<table>
<tr><th>环节</th><th>规则</th><th>说明</th></tr>
<tr><td>笔</td><td>至少6根K线</td><td>过滤噪音，只保留有效转折</td></tr>
<tr><td>中枢</td><td>≥3笔重叠，宽度&gt;0.3%</td><td>多空拉锯区间</td></tr>
<tr><td>一买</td><td>两向下中枢+ZG下移+创新低+MACD面积背驰</td><td>趋势衰竭</td></tr>
<tr><td>二买</td><td>一买后第一次30min回调不破A，C/A≤1.15</td><td>回调确认，10周内入场</td></tr>
<tr><td>三买</td><td>向上中枢+离开+回踩不破ZG</td><td>最强信号</td></tr>
<tr><td>入场</td><td>30min底分型+力竭确认，收盘价买入</td><td>区间套精确制导</td></tr>
<tr><td>止损</td><td>破背驰点A×0.995 / 破ZG×0.97 / 亏10%</td><td>风险底线</td></tr>
<tr><td>止盈</td><td>浮盈峰值+5%后回撤10%</td><td>保住利润</td></tr>
<tr><td>信号窗口</td><td>一买52周 / 二买10周 / 三买26周</td><td>过期作废，只做当下</td></tr>
</table>
</div>

<h2>六、回测结果（2024-2026 · 1927只股票）</h2>
<div class="card">
<table>
<tr><th>年份</th><th>交易数</th><th>胜率</th><th>平均收益</th><th>一买</th><th>二买</th><th>三买</th></tr>
<tr><td>2024</td><td>148</td><td>79%</td><td class="up">+14.75%</td><td>52笔 +17.17%</td><td>62笔 +12.80%</td><td>34笔 +14.60%</td></tr>
<tr><td>2025</td><td>206</td><td>79%</td><td class="up">+13.09%</td><td>17笔 +3.09%</td><td>42笔 +4.95%</td><td>147笔 +16.58%</td></tr>
<tr><td>2026(1-7月)</td><td>19</td><td>84%</td><td class="up">+11.22%</td><td>6笔 +6.85%</td><td>7笔 -1.97%</td><td>6笔 +30.97%</td></tr>
<tr><td><b>合计</b></td><td><b>373</b></td><td><b>~80%</b></td><td class="up"><b>+13%~+15%</b></td><td colspan="3">最大单笔 +151.7% / 最大亏损 -10.6%（止损锁死）</td></tr>
</table>
</div>

<div class="warn">
⚠️ <b>风险提示</b>：本策略为历史回测结果，不代表未来收益。回测未计入交易成本与滑点（当前为收盘价成交）。任何技术策略都有失效期，请控制仓位、严格执行止损。
</div>

<div class="footer">缠论量化回测系统 · 日K+30min区间套 · 2026-08 生成</div>
</div>
<script>
/*__ECHARTS__*/
</script>
<script>
const CONCEPTS = /*__CONCEPTS__*/;
const CASES = /*__CASES__*/;

// 概念示意
const cc = document.getElementById('conceptCharts');
for (const opt of CONCEPTS) {
  const d = document.createElement('div');
  d.className = 'concept';
  const c = document.createElement('div');
  c.className = 'chart';
  d.appendChild(c);
  cc.appendChild(d);
  echarts.init(c).setOption(opt);
}

function fmtP(p){ return (p>=0?'+':'') + p.toFixed(2) + '%'; }

// 案例: 日K/30min 双Tab
function caseOptionDaily(d) {
  const lineMap = {};
  for (const l of d.dp.bi_lines) { lineMap[l[0]] = l[1]; lineMap[l[2]] = l[3]; }
  const lineData = d.dp.dates.map(dt => lineMap[dt] != null ? lineMap[dt] : null);
  // 中枢: 只画"前三笔重叠"核心区(core_e) — 教学画法: 三笔重叠段才是中枢, 扩展笔不拉长矩形
  const zsArea = d.dp.zs.map(z => [{
    xAxis: z.s, yAxis: z.zd,
    itemStyle: { color: z.dir === '向下' ? 'rgba(239,83,80,0.22)' : 'rgba(38,166,154,0.22)',
                 borderColor: z.dir === '向下' ? '#ef5350' : '#26a69a', borderWidth: 2,
                 borderType: 'dashed' },
    label: { show: true, position: 'insideTop', distance: 4,
             formatter: '◆ ' + z.dir + '中枢(3笔重叠)  ZG:' + z.zg + '  ZD:' + z.zd,
             fontSize: 10, color: '#fff',
             backgroundColor: z.dir === '向下' ? 'rgba(239,83,80,0.85)' : 'rgba(38,166,154,0.85)',
             padding: [2, 6], borderRadius: 3 }
  }, { xAxis: z.core_e || z.e, yAxis: z.zg }]);
  // ZG/ZD 水平虚线(带标签)
  const zsLines = [];
  for (const z of d.dp.zs) {
    const xe = z.core_e || z.e;   // 三笔重叠核心区右端(教学画法, 不拉长)
    zsLines.push([{ xAxis: z.s, yAxis: z.zg, label: { formatter: 'ZG ' + z.zg, position: 'insideEndTop', fontSize: 9, color: '#ffb300' },
      lineStyle: { color: '#ffb300', type: 'dashed', width: 1, opacity: 0.8 } },
      { xAxis: xe, yAxis: z.zg }]);
    zsLines.push([{ xAxis: z.s, yAxis: z.zd, label: { formatter: 'ZD ' + z.zd, position: 'insideEndBottom', fontSize: 9, color: '#42a5f5' },
      lineStyle: { color: '#42a5f5', type: 'dashed', width: 1, opacity: 0.8 } },
      { xAxis: xe, yAxis: z.zd }]);
  }
  const buy = [], sell = [];
  for (const t of d.trades) {
    buy.push({ coord: [t.entry, t.ep], symbol: 'arrow', symbolRotate: -90, symbolSize: 18, symbolOffset: [0, 6],
      itemStyle: { color: '#ef5350' },
      label: { show: true, position: 'top', fontSize: 11, formatter: '买入 ' + t.type + ' @' + t.ep.toFixed(2),
        color: '#fff', backgroundColor: 'rgba(239,83,80,0.9)', borderColor: '#ef5350', borderWidth: 1, padding: [3, 6], borderRadius: 3 } });
    sell.push({ coord: [t.exit, t.xp], symbol: 'arrow', symbolRotate: 90, symbolSize: 18, symbolOffset: [0, 6],
      itemStyle: { color: '#26a69a' },
      label: { show: true, position: 'bottom', fontSize: 11, formatter: '卖出 @' + t.xp.toFixed(2) + ' ' + fmtP(t.pnl) + '\n' + t.reason,
        color: '#fff', backgroundColor: 'rgba(38,166,154,0.9)', borderColor: '#26a69a', borderWidth: 1, padding: [3, 6], borderRadius: 3 } });
  }
  // 关键点: 一买背驰点(紫色箭头指向)
  const candPts = (d.dp.cands || []).filter(c => c.type === '一买' && c.date && c.price > 0).map(c => ({
    coord: [c.date, c.price], symbol: 'arrow', symbolRotate: -90, symbolSize: 16, symbolOffset: [0, 5],
    itemStyle: { color: '#ab47bc' },
    label: { show: true, position: 'bottom', fontSize: 10, formatter: '▼一买背驰点 ' + c.price.toFixed(2) + ' (' + (c.div || '') + ')',
      color: '#ce93d8', backgroundColor: 'rgba(19,23,34,0.85)', borderColor: '#ab47bc', borderWidth: 1, padding: [2, 5], borderRadius: 3 }
  }));
  const volColor = d.dp.bars.map(b => b[1] >= b[0] ? '#ef5350' : '#26a69a');
  return {
    animation: false, backgroundColor: '#1e222d',
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: 'rgba(19,23,34,0.9)', textStyle: { fontSize: 10 } },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [{ left: 50, right: 12, top: 30, height: '55%' }, { left: 50, right: 12, top: '64%', height: '15%' }],
    xAxis: [
      { type: 'category', data: d.dp.dates, axisLabel: { color: '#9aa0ab', fontSize: 9 }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
      { type: 'category', gridIndex: 1, data: d.dp.dates, axisLabel: { show: false } }
    ],
    yAxis: [
      { scale: true, splitLine: { lineStyle: { color: 'rgba(61,66,80,0.3)' } }, axisLabel: { color: '#9aa0ab', fontSize: 9 } },
      { gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } }
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 35, end: 100 }],
    series: [
      { name: '日K', type: 'candlestick', data: d.dp.bars.map(b => [b[0], b[1], b[2], b[3]]),
        itemStyle: { color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a' },
        markArea: { silent: true, data: zsArea },
        markLine: { silent: true, symbol: 'none', data: zsLines },
        markPoint: { silent: true, data: buy.concat(sell, candPts) } },
      { name: '笔', type: 'line', data: lineData, connectNulls: true, symbol: 'none', z: 3, lineStyle: { color: '#ffb300', width: 1.2 } },
      { name: '量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: d.dp.bars.map(b => b[4]),
        itemStyle: { color: p => volColor[p.dataIndex] }, barWidth: '60%' }
    ]
  };
}

function caseOption30(d) {
  const P = d.m30;
  const idxMap = {};
  P.dates.forEach((dt, i) => { const k = dt.slice(0, 10); if (!(k in idxMap)) idxMap[k] = i; });
  const buy = [], sell = [];
  for (const t of d.trades) {
    const bi = idxMap[t.entry], si = idxMap[t.exit];
    if (bi != null) buy.push({ coord: [P.dates[bi], t.ep], symbol: 'arrow', symbolRotate: -90, symbolSize: 14,
      itemStyle: { color: '#ef5350' },
      label: { show: true, position: 'top', fontSize: 10, formatter: '买入 ' + t.type + ' @' + t.ep.toFixed(2),
        color: '#ef5350', backgroundColor: 'rgba(19,23,34,0.8)', borderColor: '#ef5350', borderWidth: 1, padding: [2, 5], borderRadius: 3 } });
    if (si != null) sell.push({ coord: [P.dates[si], t.xp], symbol: 'arrow', symbolRotate: 90, symbolSize: 14,
      itemStyle: { color: '#26a69a' },
      label: { show: true, position: 'bottom', fontSize: 10, formatter: '卖出 @' + t.xp.toFixed(2) + ' ' + fmtP(t.pnl),
        color: '#26a69a', backgroundColor: 'rgba(19,23,34,0.8)', borderColor: '#26a69a', borderWidth: 1, padding: [2, 5], borderRadius: 3 } });
  }
  const volColor = P.bars.map(b => b[1] >= b[0] ? '#ef5350' : '#26a69a');
  return {
    animation: false, backgroundColor: '#1e222d',
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' }, backgroundColor: 'rgba(19,23,34,0.9)', textStyle: { fontSize: 10 } },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [{ left: 50, right: 12, top: 30, height: '55%' }, { left: 50, right: 12, top: '64%', height: '15%' }],
    xAxis: [
      { type: 'category', data: P.dates, axisLabel: { color: '#9aa0ab', fontSize: 9 }, splitLine: { show: false }, min: 'dataMin', max: 'dataMax' },
      { type: 'category', gridIndex: 1, data: P.dates, axisLabel: { show: false } }
    ],
    yAxis: [
      { scale: true, splitLine: { lineStyle: { color: 'rgba(61,66,80,0.3)' } }, axisLabel: { color: '#9aa0ab', fontSize: 9 } },
      { gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } }
    ],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 }],
    series: [
      { name: '30min', type: 'candlestick', data: P.bars.map(b => [b[0], b[1], b[2], b[3]]),
        itemStyle: { color: '#ef5350', color0: '#26a69a', borderColor: '#ef5350', borderColor0: '#26a69a' },
        markPoint: { silent: true, data: buy.concat(sell) } },
      { name: '量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: P.bars.map(b => b[4]),
        itemStyle: { color: p => volColor[p.dataIndex] }, barWidth: '60%' }
    ]
  };
}

const cc2 = document.getElementById('caseCharts');
for (const sym in CASES) {
  const d = CASES[sym];
  const meta = d.meta;
  const box = document.createElement('div');
  box.className = 'case';
  box.innerHTML = '<h3>' + meta.title + '</h3><div class="cdesc">' + meta.desc + '</div>' +
    '<div class="teach"><b>📖 看图步骤(教学):</b> ' + (meta.teach || '') + '</div>' +
    '<div class="tabs"><button class="tabbtn on" data-t="d">日K视图</button>' +
    (d.m30 ? '<button class="tabbtn" data-t="m">30min介入视图</button>' : '') + '</div>' +
    '<div class="tabnote" id="note"></div>' +
    '<div class="chart big"></div>';
  cc2.appendChild(box);
  const chartDiv = box.querySelector('.chart');
  const ch = echarts.init(chartDiv);
  const noteEl = box.querySelector('#note');
  const btnD = box.querySelector('[data-t="d"]');
  const btnM = box.querySelector('[data-t="m"]');
  function setNote(t) {
    noteEl.textContent = t === 'd'
      ? '日K视图: 金色折线=笔, 绿色/红色矩形=中枢(ZG/ZD), ▲红=买入点, ▼绿=卖出点。滚轮可缩放。'
      : '30min介入视图: 区间套精确介入点——放大看买入前后的30min走势(底分型+MACD力竭)。滚轮可缩放。';
  }
  function render(t) {
    if (t === 'd') { ch.setOption(caseOptionDaily(d), true); btnD.className = 'tabbtn on'; if (btnM) btnM.className = 'tabbtn'; }
    else { ch.setOption(caseOption30(d), true); btnM.className = 'tabbtn on'; btnD.className = 'tabbtn'; }
    setNote(t);
  }
  btnD.onclick = () => render('d');
  if (btnM) btnM.onclick = () => render('m');
  render('d');
}
window.addEventListener('resize', () => { document.querySelectorAll('.chart').forEach(el => { const i = echarts.getInstanceByDom(el); if (i) i.resize(); }); });
</script>
</body>
</html>'''

if __name__ == '__main__':
    main()
