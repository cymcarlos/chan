#!/usr/bin/env python3
"""日K+30min区间套回测结果 → 自包含交互式HTML(ECharts, 日K/30min双Tab)。

用法: python3 gen_30min_chart.py <结果json> [--out out.html]
"""
import sys, os, json, sqlite3
sys.path.insert(0, '/root/data/backtest')
from datetime import datetime, timedelta

from chan.bars import RawBar, macd
from chan.bi import build_bi
from chan.zhongshu import build_zs
from chan.state import clear_all, clear_level
from chan.daily_scan import load_daily, scan_daily
import backtest_daily_30min as b30

DB_PATH = '/root/data/backtest/kline.db'
OUT_HTML = '/root/data/backtest/charts/bt_daily30_2026_500.html'
ECHARTS_JS = '/root/data/backtest/charts/echarts.min.js'
D_PLOT_START = '2025-06-01'   # 日K图起点(留结构)
M_PLOT_START = '2026-01-01'   # 30min图起点(介入段)


def extract_daily(symbol, edt='20260804'):
    clear_all(); clear_level('D')
    conn = sqlite3.connect(DB_PATH, timeout=30)
    bars = load_daily(symbol, conn=conn)
    conn.close()
    edt_dt = datetime.strptime(edt, "%Y%m%d")
    bars_t = [b for b in bars if b.dt <= edt_dt]
    if len(bars_t) < 200:
        return None
    bis = build_bi(bars_t, level='D')
    zs = build_zs(bis, level='D')
    cands = scan_daily(symbol, edt)
    closes = [b.close for b in bars_t]
    diff, dea, hist = macd(closes)
    return {'bars': bars_t, 'bis': bis, 'zs': zs, 'cands': cands,
            'diff': diff, 'dea': dea, 'hist': hist}


def fmt_dt(s):
    return s[:10] if s else s


def daily_plot(ext, plot_start=None, plot_end=None):
    """日K视图。plot_start/plot_end: 自定义窗口(教学/案例用交易前后窗口, 默认全局)。"""
    bars = ext['bars']
    if plot_start is None:
        plot_start = D_PLOT_START
    start = datetime.strptime(plot_start, '%Y-%m-%d')
    i0 = next((i for i, b in enumerate(bars) if b.dt >= start), 0)
    if plot_end:
        i1 = next((i for i, b in enumerate(bars) if b.dt > datetime.strptime(plot_end, '%Y-%m-%d')), len(bars))
    else:
        i1 = len(bars)
    sub = bars[i0:i1]
    dates = [b.dt.strftime('%Y-%m-%d') for b in sub]

    # 中枢核心区(前三笔): 从中枢start之后的笔开始数3笔, 第3笔end=重叠区形成点
    # (用bi.start_dt>=z.start_dt: 笔首尾相接, 前一笔end=z.start会被误数)
    def zs_core(z):
        cnt = 0
        for bi in ext['bis']:
            if fmt_dt(bi.start_dt) >= fmt_dt(z.start_dt):
                cnt += 1
                if cnt == 3:
                    return fmt_dt(bi.end_dt)
        return fmt_dt(z.end_dt)

    bi_lines = []
    for bi in ext['bis']:
        s, e = fmt_dt(bi.start_dt), fmt_dt(bi.end_dt)
        if e < plot_start or s > (plot_end or '9999'):
            continue
        if bi.direction == 'up':
            bi_lines.append([s, round(bi.low, 2), e, round(bi.high, 2)])
        else:
            bi_lines.append([s, round(bi.high, 2), e, round(bi.low, 2)])
    zs_plot = []
    for z in ext['zs']:
        s, e = fmt_dt(z.start_dt), fmt_dt(z.end_dt)
        s_eff = max(s, plot_start)
        e_eff = min(e, plot_end or '9999')
        if e_eff < plot_start or s_eff > (plot_end or '9999'):
            continue
        core_eff = max(zs_core(z), s_eff)   # 核心区裁剪到窗口内
        if core_eff > e_eff:
            core_eff = e_eff
        zs_plot.append({'s': s_eff, 'e': e_eff,
                        'core_e': core_eff,   # 前三笔重叠区形成点(教学画法)
                        'zg': round(z.zg, 2), 'zd': round(z.zd, 2), 'dir': z.sdir})
    cand_pts = []
    for c in ext['cands']:
        if c.buy_type == '一买' and c.a_date and c.a_price > 0:
            cand_pts.append({'date': fmt_dt(c.a_date), 'price': c.a_price,
                             'div': c.div_type, 'sig': fmt_dt(c.signal_date)})
    def cut(a):
        return [round(x, 3) for x in a[i0:]]
    return {'dates': dates,
            'bars': [[round(b.open, 2), round(b.close, 2), round(b.low, 2), round(b.high, 2), b.vol] for b in sub],
            'bi_lines': bi_lines, 'zs': zs_plot, 'cands': cand_pts,
            'diff': cut(ext['diff']), 'dea': cut(ext['dea']), 'hist': cut(ext['hist'])}


def m30_plot(symbol, trades):
    """30min图: 介入段K线 + 入/出场点 (只画trade日期±40天窗口)"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    bars = b30.load_30min(symbol, conn=conn)
    conn.close()
    if not bars:
        return None
    # 窗口: min(entry)-40天 ~ max(exit)+40天 (支持多年份, 不设起点下限)
    ds = [t['entry'] for t in trades] + [t['exit'] for t in trades]
    w0 = min(datetime.strptime(d, '%Y-%m-%d') for d in ds) - timedelta(days=40)
    w1 = max(datetime.strptime(d, '%Y-%m-%d') for d in ds) + timedelta(days=40)
    sub = [b for b in bars if w0 <= b.dt <= w1]
    if len(sub) < 30:
        return None
    dates = [b.dt.strftime('%Y-%m-%d %H:%M') for b in sub]
    return {'dates': dates,
            'bars': [[round(b.open, 2), round(b.close, 2), round(b.low, 2), round(b.high, 2), b.vol] for b in sub],
            'w0': w0.strftime('%Y-%m-%d'), 'w1': w1.strftime('%Y-%m-%d')}


def main():
    argv = sys.argv[1:]
    out = OUT_HTML
    only = None
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == '--out' and i + 1 < len(argv):
            out = argv[i + 1]; i += 2
        elif argv[i] == '--symbols' and i + 1 < len(argv):
            only = set(s.strip() for s in argv[i + 1].split(',') if s.strip())
            i += 2
        else:
            rest.append(argv[i]); i += 1
    res_path = rest[0] if rest else '/root/data/backtest/results/bt_daily30_2026_500.json'
    results = json.load(open(res_path))
    trades_by_sym = {}
    for bt, ts in results.items():
        for t in ts:
            trades_by_sym.setdefault(t['symbol'], []).append(t)
    if only:
        trades_by_sym = {k: v for k, v in trades_by_sym.items() if k in only}
        miss = only - set(trades_by_sym)
        if miss:
            print(f"跳过(无交易): {sorted(miss)}")
    print(f"有交易: {len(trades_by_sym)}只 {sum(len(v) for v in trades_by_sym.values())}笔")
    data = {}
    for sym in sorted(trades_by_sym):
        try:
            ext = extract_daily(sym)
            if ext is None:
                continue
            dp = daily_plot(ext)
            mp = m30_plot(sym, trades_by_sym[sym])
            if mp is None:
                continue
            data[sym] = {
                'daily': dp,
                'm30': mp,
                'trades': [{'type': t.get('name', ''), 'entry': t['entry'], 'exit': t['exit'],
                            'ep': t['entry_price'], 'xp': t['exit_price'],
                            'pnl': t['pnl_pct'], 'reason': t['reason']} for t in trades_by_sym[sym]],
            }
            print(f"  {sym}: {len(trades_by_sym[sym])}笔", flush=True)
        except Exception as e:
            print(f"  {sym} 提取失败: {e}", flush=True)
    all_p = [t['pnl_pct'] for ts in trades_by_sym.values() for t in ts]
    n_t = len(all_p); n_w = len([p for p in all_p if p > 0])
    summary = {'n_stock': len(data), 'n_trade': n_t,
               'win_rate': round(n_w / n_t * 100, 1) if n_t else 0,
               'avg': round(sum(all_p) / n_t, 2) if n_t else 0,
               'max_win': round(max(all_p), 1) if all_p else 0,
               'max_loss': round(min(all_p), 1) if all_p else 0}
    build_html(data, summary, out)
    print(f"HTML: {out} ({os.path.getsize(out)/1024/1024:.1f}MB)")


def build_html(data, summary, out=OUT_HTML):
    echarts = open(ECHARTS_JS).read()
    payload = json.dumps(data, ensure_ascii=False)
    html = TEMPLATE.replace('/*__ECHARTS__*/', echarts).replace('__DATA__', payload).replace('__SUMMARY__', json.dumps(summary, ensure_ascii=False))
    with open(out, 'w') as f:
        f.write(html)


TEMPLATE = r'''<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>日K+30min区间套回测 2026</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:"PingFang SC","Microsoft YaHei",sans-serif; background:#1e222d; color:#d1d4dc; }
#topbar { display:flex; align-items:center; gap:12px; padding:8px 14px; background:#131722; border-bottom:1px solid #2a2e39; flex-wrap:wrap; }
#topbar h1 { font-size:15px; color:#e8eaf0; font-weight:600; white-space:nowrap; }
.badge { font-size:12px; padding:2px 8px; border-radius:10px; background:#2a2e39; color:#9aa0ab; white-space:nowrap; }
.badge b { color:#e8eaf0; }
#sel { background:#2a2e39; color:#e8eaf0; border:1px solid #3d4250; border-radius:4px; padding:3px 8px; font-size:13px; }
.tabbtn { padding:4px 14px; border-radius:4px; border:1px solid #3d4250; background:#2a2e39; color:#9aa0ab; font-size:13px; cursor:pointer; }
.tabbtn.on { background:#1e88e5; color:#fff; border-color:#1e88e5; }
label { font-size:12px; color:#9aa0ab; display:flex; align-items:center; gap:4px; cursor:pointer; }
#chart { width:100%; height:calc(100vh - 92px); }
#tradelist { display:flex; gap:8px; padding:6px 14px; overflow-x:auto; background:#131722; }
.tcard { font-size:11px; padding:4px 10px; border-radius:4px; border:1px solid #3d4250; white-space:nowrap; background:#1e222d; cursor:pointer; }
.tcard:hover { border-color:#1e88e5; }
.tcard .pn { font-weight:600; } .tcard .up { color:#26a69a; } .tcard .dn { color:#ef5350; }
.tcard .tt { color:#9aa0ab; }
</style>
</head>
<body>
<div id="topbar">
  <h1>日K+30min 区间套回测 2026</h1>
  <span class="badge" id="b1"></span><span class="badge" id="b2"></span>
  <select id="sel"></select>
  <button class="tabbtn on" id="tabD">日K</button>
  <button class="tabbtn" id="tabM">30min</button>
  <label><input type="checkbox" id="ckCands"> 背驰点</label>
  <span class="badge" id="curStat"></span>
</div>
<div id="chart"></div>
<div id="tradelist"></div>
<script>
/*__ECHARTS__*/
</script>
<script>
const SUMMARY = __SUMMARY__;
const DATA = __DATA__;
const chart = echarts.init(document.getElementById('chart'));
const sel = document.getElementById('sel');
const ckCands = document.getElementById('ckCands');
let tab = 'D';
document.getElementById('b1').innerHTML = '股票 <b>' + SUMMARY.n_stock + '</b> 笔 <b>' + SUMMARY.n_trade + '</b>';
document.getElementById('b2').innerHTML = '胜率 <b>' + SUMMARY.win_rate + '%</b> avg <b>' + SUMMARY.avg + '%</b>';
const syms = Object.keys(DATA).sort();
for (const s of syms) { const o = document.createElement('option'); o.value = s; o.textContent = s; sel.appendChild(o); }
function fmtP(p){ return (p>=0?'+':'') + p.toFixed(2) + '%'; }

function markPts(P, trades, showCands) {
  const buy = [], sell = [], cand = [];
  for (const t of trades) {
    buy.push({ name:'买', coord:[t.entry, t.ep], symbol:'arrow', symbolRotate:-90, symbolSize:15,
      itemStyle:{color:'#ef5350'},
      label:{ show:true, position:'top', distance:6, fontSize:10,
        formatter:'买 '+t.type+' @'+t.ep.toFixed(2)+' '+t.entry.slice(5),
        color:'#ef5350', backgroundColor:'rgba(19,23,34,0.78)', borderColor:'#ef5350', borderWidth:1, padding:[2,5], borderRadius:3 } });
    sell.push({ name:'卖', coord:[t.exit, t.xp], symbol:'arrow', symbolRotate:90, symbolSize:15,
      itemStyle:{color:'#26a69a'},
      label:{ show:true, position:'bottom', distance:6, fontSize:10,
        formatter:'卖 @'+t.xp.toFixed(2)+' '+fmtP(t.pnl)+' '+t.reason,
        color:'#26a69a', backgroundColor:'rgba(19,23,34,0.78)', borderColor:'#26a69a', borderWidth:1, padding:[2,5], borderRadius:3 } });
  }
  if (showCands) for (const c of P.cands) {
    cand.push({ name:'背驰', coord:[c.date, c.price], symbol:'diamond', symbolSize:8,
      itemStyle:{color:'#ab47bc'},
      label:{ show:true, position:'top', distance:3, fontSize:9,
        formatter:'背驰 '+c.price.toFixed(2)+'('+c.div+')', color:'#ce93d8',
        backgroundColor:'rgba(19,23,34,0.7)', borderColor:'#ab47bc', borderWidth:1, padding:[1,4], borderRadius:3 } });
  }
  return buy.concat(sell, cand);
}

function buildD(code) {
  const P = DATA[code].daily, d = DATA[code];
  const lineMap = {};
  for (const l of P.bi_lines) { lineMap[l[0]] = l[1]; lineMap[l[2]] = l[3]; }
  const lineData = P.dates.map(dt => lineMap[dt] != null ? lineMap[dt] : null);
  const zsArea = P.zs.map(z => [{ name:z.dir+'中枢', xAxis:z.s, yAxis:z.zd,
    itemStyle:{ color: z.dir==='向下' ? 'rgba(239,83,80,0.10)' : 'rgba(38,166,154,0.10)' },
    label:{ show:true, position:'insideTop', distance:2, formatter:z.dir+' ZG'+z.zg+' ZD'+z.zd,
      fontSize:10, color: z.dir==='向下' ? '#ef9a9a' : '#80cbc4' } },
    { xAxis:z.e, yAxis:z.zg }]);
  return {
    dates: P.dates, bars: P.bars, series2: [
      { name:'笔', type:'line', data:lineData, connectNulls:true, symbol:'none', z:3,
        lineStyle:{ color:'#ffb300', width:1.2, opacity:0.9 }, tooltip:{ show:false } },
      { name:'MACD', type:'bar', gi:2, data:P.hist },
      { name:'DIF', type:'line', gi:2, data:P.diff, lineStyle:{ color:'#ffb300', width:1 }, symbol:'none' },
      { name:'DEA', type:'line', gi:2, data:P.dea, lineStyle:{ color:'#42a5f5', width:1 }, symbol:'none' } ],
    zsArea: zsArea, mp: markPts(P, d.trades, ckCands.checked),
    tooltipFmt: (p) => { const b = P.bars[p.dataIndex];
      return '<b>'+p.axisValue+'</b><br>开 '+b[0]+' 高 '+b[3]+' 低 '+b[2]+' 收 '+b[1]+
        '<br>量 '+(b[4]/10000).toFixed(1)+'万手'; }
  };
}

function buildM(code) {
  const P = DATA[code].m30, d = DATA[code];
  // 30min图入/出场: 定位到日期内第一根30min bar
  const idxMap = {};
  P.dates.forEach((dt, i) => { const k = dt.slice(0,10); if (!(k in idxMap)) idxMap[k] = i; });
  const buy = [], sell = [];
  for (const t of d.trades) {
    const bi = idxMap[t.entry], si = idxMap[t.exit];
    if (bi != null) buy.push({ name:'买', coord:[P.dates[bi], t.ep], symbol:'arrow', symbolRotate:-90, symbolSize:14,
      itemStyle:{color:'#ef5350'}, label:{ show:true, position:'top', distance:5, fontSize:10,
        formatter:'买 '+t.type+' @'+t.ep.toFixed(2), color:'#ef5350',
        backgroundColor:'rgba(19,23,34,0.78)', borderColor:'#ef5350', borderWidth:1, padding:[2,5], borderRadius:3 } });
    if (si != null) sell.push({ name:'卖', coord:[P.dates[si], t.xp], symbol:'arrow', symbolRotate:90, symbolSize:14,
      itemStyle:{color:'#26a69a'}, label:{ show:true, position:'bottom', distance:5, fontSize:10,
        formatter:'卖 @'+t.xp.toFixed(2)+' '+fmtP(t.pnl)+' '+t.reason, color:'#26a69a',
        backgroundColor:'rgba(19,23,34,0.78)', borderColor:'#26a69a', borderWidth:1, padding:[2,5], borderRadius:3 } });
  }
  return { dates: P.dates, bars: P.bars, series2: [], zsArea: [], mp: buy.concat(sell),
    tooltipFmt: (p) => { const b = P.bars[p.dataIndex];
      return '<b>'+p.axisValue+'</b><br>开 '+b[0]+' 高 '+b[3]+' 低 '+b[2]+' 收 '+b[1]+
        '<br>量 '+(b[4]/100).toFixed(1)+'手'; } };
}

function buildOption(code) {
  const V = tab === 'D' ? buildD(code) : buildM(code);
  const P = V;
  const volColor = P.bars.map(b => b[1] >= b[0] ? '#ef5350' : '#26a69a');
  const volData = P.bars.map(b => b[4]);
  const histColor = (P.series2.find(s => s.name==='MACD') || {}).data ? P.series2.find(s => s.name==='MACD').data.map(v => v>=0?'#ef5350':'#26a69a') : [];
  const grids = [
    { left:58, right:16, top:44, height:'50%' },
    { left:58, right:16, top:'60%', height:'11%' },
    { left:58, right:16, top:'75%', height:'11%' } ];
  const xaxes = [
    { type:'category', data:P.dates, boundaryGap:true, axisLine:{ lineStyle:{ color:'#3d4250' } }, axisLabel:{ color:'#9aa0ab', fontSize:10 }, splitLine:{ show:false }, min:'dataMin', max:'dataMax' },
    { type:'category', gridIndex:1, data:P.dates, axisLabel:{ show:false }, axisLine:{ lineStyle:{ color:'#3d4250' } } },
    { type:'category', gridIndex:2, data:P.dates, axisLabel:{ show:true, color:'#9aa0ab', fontSize:10 }, axisLine:{ lineStyle:{ color:'#3d4250' } } } ];
  const series = [
    { name:'K线', type:'candlestick', data:P.bars.map(b => [b[0], b[1], b[2], b[3]]),
      itemStyle:{ color:'#ef5350', color0:'#26a69a', borderColor:'#ef5350', borderColor0:'#26a69a' },
      markArea:{ silent:true, itemStyle:{ borderColor:'rgba(255,255,255,0.15)', borderWidth:1 }, data:P.zsArea },
      markPoint:{ silent:true, data:P.mp } },
    { name:'成交量', type:'bar', xAxisIndex:1, yAxisIndex:1, data:volData,
      itemStyle:{ color:p => volColor[p.dataIndex] }, barWidth:'60%' } ];
  for (const s of P.series2) {
    if (s.name === 'MACD') {
      series.push({ name:'MACD', type:'bar', xAxisIndex:2, yAxisIndex:2, data:s.data,
        itemStyle:{ color:p => histColor[p.dataIndex] }, barWidth:'60%' });
    } else if (s.gi === 2) {
      series.push({ name:s.name, type:'line', xAxisIndex:2, yAxisIndex:2, data:s.data,
        lineStyle:s.lineStyle, symbol:'none' });
    } else {
      series.push(s);
    }
  }
  return { animation:false, backgroundColor:'#1e222d',
    axisPointer:{ link:[{ xAxisIndex:'all' }] },
    tooltip:{ trigger:'axis', axisPointer:{ type:'cross', label:{ backgroundColor:'#2a2e39' } },
      backgroundColor:'rgba(19,23,34,0.9)', borderColor:'#3d4250', textStyle:{ fontSize:11, color:'#d1d4dc' },
      formatter: ps => { const p = ps.find(x => x.seriesName === 'K线'); return p ? P.tooltipFmt(p) : ''; } },
    grid: grids,
    xAxis: xaxes,
    yAxis: [
      { scale:true, gridIndex:0, splitLine:{ lineStyle:{ color:'rgba(61,66,80,0.35)' } }, axisLabel:{ color:'#9aa0ab', fontSize:10 } },
      { gridIndex:1, axisLabel:{ show:false }, splitLine:{ show:false } },
      { gridIndex:2, axisLabel:{ show:true, color:'#9aa0ab', fontSize:10 }, splitLine:{ show:false } } ],
    dataZoom: [
      { type:'inside', xAxisIndex:[0,1,2], start:50, end:100 },
      { type:'slider', xAxisIndex:[0,1,2], bottom:2, height:14, borderColor:'#3d4250', backgroundColor:'#2a2e39',
        fillerColor:'rgba(38,166,154,0.15)', textStyle:{ color:'#9aa0ab', fontSize:9 } } ],
    series: series };
}

function render() {
  const code = sel.value; if (!code) return;
  chart.setOption(buildOption(code), true);
  const ts = DATA[code].trades, pns = ts.map(t => t.pnl);
  const w = pns.filter(p => p > 0).length;
  const avg = pns.reduce((a,b)=>a+b,0)/(pns.length||1);
  document.getElementById('curStat').innerHTML = '<b>'+code+'</b> '+ts.length+'笔 胜率'+
    (pns.length?Math.round(w/pns.length*100):0)+'% avg '+fmtP(avg);
  const tl = document.getElementById('tradelist'); tl.innerHTML = '';
  for (const t of ts) {
    const el = document.createElement('div'); el.className = 'tcard';
    el.innerHTML = '<span class="tt">'+t.entry+'→'+t.exit+' '+t.type+'</span> <span class="pn '+
      (t.pnl>=0?'up':'dn')+'">'+fmtP(t.pnl)+'</span> <span class="tt">'+t.reason+'</span>';
    tl.appendChild(el);
  }
}
document.getElementById('tabD').onclick = () => { tab='D'; document.getElementById('tabD').className='tabbtn on'; document.getElementById('tabM').className='tabbtn'; render(); };
document.getElementById('tabM').onclick = () => { tab='M'; document.getElementById('tabM').className='tabbtn on'; document.getElementById('tabD').className='tabbtn'; render(); };
sel.onchange = render;
ckCands.onchange = render;
render();
window.addEventListener('resize', () => chart.resize());
</script>
</body>
</html>'''

if __name__ == '__main__':
    main()
