#!/usr/bin/env python3
"""日K找买点 + 30min介入 回测引擎（区间套: 大级别买点 → 次级别介入）。

架构(2026-08-06, 课本 Ch17 p37 + Ch61 p67 区间套):
  日K层 scan_daily:  日线笔中枢(L1) 一买(双中枢下移背驰) / 三买(离开回试) — 只找买点
  30min介入层:       候选激活后, 30min bar 逐根行走:
                     - 一买: A区 + 30min底分型 + 30min力竭确认 → 入场
                     - 三买: ZG区 + 底分型 + 力竭 → 入场
                     - 二买: 日K一买后 第一次30min回调结束点(101课: 次级别走势结束点,
                       笔级即可无需中枢; 中阴边界内 BUY1_WINDOW_WEEKS) → C区入场
                     - 出场(2026-08-11 卖点体系): 30min一卖(顶背驰) / 30min三卖(破中枢)
                       / 结构止损(A/ZG) / 止损10%兜底 (移动止盈已删除)
"""

import hashlib, sqlite3, time, sys
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, '/root/data/backtest')

from chan.bars import RawBar, macd, macd_area
from chan.bi import build_bi, BiCursor
from chan.zhongshu import build_zs
from chan.state import clear_all, clear_level
from chan.scanner_60min import (BuyCandidate, build_trends, _add_weeks)
from chan.backtest_60min import (_find_bottom_fractal_idx, _is_recovering,
                                 _divergence_at_fractal)
from chan.daily_scan import scan_daily, scan_daily_on_bis, scan_daily_on_bars, load_daily, clear_daily_cache

DB_PATH = '/root/data/backtest/kline.db'
USE_30M_SWING = False   # A/B: 关(干净口径对照)
                       # (原False: 全仓清仓式短差破坏趋势股; 现保留pnl>3%+60根门限+回补机制,
                       #  验证"先浮盈后亏"单能否被30min一卖/二卖救出, 以及回补是否对冲卖飞)
SIGNAL_WINDOW_WEEKS = 26   # 二买/三买入场窗口(sig后26周; 笔级二买只生成一次无重复,
                           # 窗口需覆盖"回调后价格回踩C区"的时机: 002583 sig4/22入场6月)
BUY1_WINDOW_WEEKS = 26     # 一买中阴边界(101课: 只有中阴状态下才有第一、二类买点;
                           # 26周覆盖"一买后先涨后回"形态: 003002一买2月首合格回调7月)
# Phase4 L2背景过滤(2026-08-06, 49课"没必要参与操作级别及以上级别的下跌"):
# True = 周线L2向下时滤一买/二买(三买豁免: 顺势离开结构, 同60min引擎FILTER_WEEKLY)
# 跨年验证(2024/2025/2026 已平仓: 4.86/12.56/7.02 vs 关: 4.47/12.19/1.46) → 默认开启
FILTER_L2 = True
FAST_DAILY_AVAIL = True  # 仅用有序日期索引替代每日全表扫描；False保留等价基线路径
FAST_DAILY_MACD_PREFIX = True  # MACD已是因果前缀；避免每日复制两个历史列表
_30MIN_CACHE = {}
_FRESH_B1_CACHE = {}   # P1幽灵候选修复: (symbol, day_str) -> {a_price集合}, 每日每symbol至多重扫一次


def _audit_stable_id(prefix, *parts):
    """审计专用稳定 ID；只依赖可复现的业务字段，不依赖对象地址。"""
    raw = "\x1f".join('' if p is None else str(p) for p in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]}"


def _audit_bi(bi, level):
    start_fx = getattr(bi, '_start_fx', None) or {}
    end_fx = getattr(bi, '_end_fx', None) or {}
    bi_id = _audit_stable_id('bi', level, bi.direction, bi.start_dt, bi.end_dt,
                             repr(float(bi.start_price)), repr(float(bi.end_price)))
    def _fx(fx, role):
        if not fx:
            return None
        return {
            'fractal_id': _audit_stable_id('fx', level, role, fx.get('type'),
                                           fx.get('dt'), repr(fx.get('price'))),
            'type': fx.get('type'), 'dt': fx.get('dt'),
            'price': fx.get('price'), 'high': fx.get('high'), 'low': fx.get('low'),
        }
    return {
        'bi_id': bi_id, 'level': level, 'direction': bi.direction,
        'start_at': bi.start_dt, 'end_at': bi.end_dt,
        'occur_at': getattr(bi, 'occur_at', '') or None,
        'confirm_at': getattr(bi, 'confirm_at', '') or None,
        'start_price': float(bi.start_price), 'end_price': float(bi.end_price),
        'high': float(bi.high), 'low': float(bi.low),
        'start_fractal': _fx(start_fx, 'start'), 'end_fractal': _fx(end_fx, 'end'),
    }


def _audit_zs(zs, level):
    return {
        'zhongshu_id': _audit_stable_id('zs', level, zs.sdir, zs.start_dt, zs.end_dt,
                                        repr(float(zs.zd)), repr(float(zs.zg))),
        'level': level, 'direction': zs.sdir,
        'start_at': zs.start_dt, 'end_at': zs.end_dt,
        'occur_at': getattr(zs, 'occur_at', '') or None,
        'confirm_at': getattr(zs, 'confirm_at', '') or None,
        'ZD': float(zs.zd), 'ZG': float(zs.zg),
        'DD': float(zs.dd), 'GG': float(zs.gg),
        # Zhongshu 当前没有保存“最初三笔”和延伸成员，不能反推伪造。
        'initial_bounds': {'status': 'UNKNOWN', 'ZD': None, 'ZG': None,
                           'DD': None, 'GG': None},
        'extended_bounds': {'status': 'KNOWN', 'ZD': float(zs.zd), 'ZG': float(zs.zg),
                            'DD': float(zs.dd), 'GG': float(zs.gg)},
        'member_bi_ids': {'status': 'UNKNOWN', 'ids': []},
    }


def _audit_candidate_structure(c):
    l2 = getattr(c, 'l2', None)
    trends = []
    if l2 is not None:
        for t in getattr(l2, 'trends', []) or []:
            zss = [_audit_zs(z, 'L1') for z in (getattr(t, 'zss', []) or [])]
            trends.append({
                'trend_id': _audit_stable_id('trend', t.idx, t.sdir, t.start_dt, t.end_dt),
                'index': t.idx, 'direction': t.sdir, 'start_at': t.start_dt,
                'end_at': t.end_dt, 'DD': float(t.low), 'GG': float(t.high),
                'type': t.t_type, 'zhongshu_ids': [z['zhongshu_id'] for z in zss],
                'zhongshu': zss,
            })
    l2_dd = (float(l2.dd) if l2 is not None and hasattr(l2, 'dd') else
             min((t['DD'] for t in trends), default=None))
    l2_gg = (float(l2.gg) if l2 is not None and hasattr(l2, 'gg') else
             max((t['GG'] for t in trends), default=None))
    zg = float(c.zs_zg) if getattr(c, 'zs_zg', 0) else (
        float(l2.zg) if l2 is not None else None)
    zd = float(c.zs_zd) if getattr(c, 'zs_zd', 0) else (
        float(l2.zd) if l2 is not None else None)
    return {
        'engine_level': 'daily-pen proxy',
        'l2_zhongshu_id': (_audit_stable_id('l2zs', getattr(l2, 'idx', ''), l2.start_dt, l2.end_dt,
                                            repr(float(l2.zd)), repr(float(l2.zg)))
                             if l2 is not None else None),
        'ZD': zd, 'ZG': zg,
        'DD': l2_dd, 'GG': l2_gg,
        'trend_member_ids': [t['trend_id'] for t in trends], 'trends': trends,
        'initial_bounds': {'status': 'UNKNOWN', 'ZD': None, 'ZG': None,
                           'DD': None, 'GG': None},
        'extended_bounds': {'status': 'KNOWN' if zg is not None and zd is not None else 'UNKNOWN',
                            'ZD': zd, 'ZG': zg,
                            'DD': l2_dd, 'GG': l2_gg},
    }


def _fresh_b1_alive(symbol: str, day_str: str, a_price: float, tol=0.01,
                    audit_errors=None):
    """P1幽灵候选修复(2026-08-13): 链式活性校验。

    引擎候选"生成后留存不重校验", 结构漂移后已死的一买仍会借尸还魂链出二买
    (实证4笔: 000065#1/000058#1/000012#1 失败-26.78 + 000021 赢家+34.30)。
    用完整重建的日K笔/中枢重扫, 验证源一买(A价±1%)是否仍存活。
    仅在链式事件时触发(罕见), 每天每symbol至多重算一次; 校验异常时放行(保守)。
    """
    key = (symbol, day_str)
    if key not in _FRESH_B1_CACHE:
        try:
            bars_d = load_daily(symbol)
            sdt = datetime.strptime(day_str, '%Y-%m-%d')
            avail = [b for b in bars_d if b.dt <= sdt]
            clear_level('D')
            bis = build_bi(avail, level='D')
            dc = [b.close for b in avail]
            ddiff, ddea, _ = macd(dc)
            cands = scan_daily_on_bis(bis, avail, ddiff, ddea)
            # P1b(2026-08-14): 窗口过滤 — 与Phase4b中阴边界(BUY1_WINDOW_WEEKS)一致;
            # 只认"扫描日仍在窗口内"的候选, 过期窗口候选不再冒充活性
            # (否则陈旧一买借尸还魂通过A价±1%匹配, 幽灵校验形同虚设)
            _set = set()
            for c in cands:
                if c.buy_type != '一买' or c.a_price <= 0:
                    continue
                sig = c.signal_date[:10]
                wend = min(c.window_end[:10],
                           (datetime.strptime(sig, "%Y-%m-%d") + timedelta(weeks=BUY1_WINDOW_WEEKS)).strftime("%Y-%m-%d"))
                if sig <= day_str <= wend:
                    _set.add(c.a_price)
            _FRESH_B1_CACHE[key] = _set
        except Exception as exc:
            _FRESH_B1_CACHE[key] = None
            if audit_errors is not None:
                audit_errors.append({
                    'at': day_str, 'stage': 'fresh_b1_alive',
                    'error_type': type(exc).__name__, 'message': str(exc),
                    'effect': 'source_b1_liveness=UNKNOWN; engine kept conservative pass-through',
                })
    s = _FRESH_B1_CACHE.get(key)
    if s is None:
        return True
    return any(abs(p - a_price) / a_price < tol for p in s)


def load_30min(symbol: str, conn=None):
    """加载30min K线(全量, 2020年起)"""
    if symbol in _30MIN_CACHE:
        return _30MIN_CACHE[symbol]
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        own = True
    rows = conn.execute(
        "SELECT code, datetime, open, high, low, close, vol, amount "
        "FROM kline_30min WHERE code=? ORDER BY datetime", (symbol,)).fetchall()
    bars = []
    for r in rows:
        dt_str = r[1]
        try:
            # 兼容 '2026-07-03 150000' 与 '2020-01-02 09:30:00'
            if ':' in dt_str:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
            else:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H%M%S")
        except Exception:
            continue
        bars.append(RawBar(symbol=r[0], dt=dt, open=float(r[2]), high=float(r[3]),
                           low=float(r[4]), close=float(r[5]),
                           vol=float(r[6] or 0), amount=float(r[7] or 0)))
    if own:
        conn.close()
    _30MIN_CACHE[symbol] = bars
    return bars


def clear_30min_cache():
    _30MIN_CACHE.clear()


def _daily_down_zs(bars_d):
    """截至bars_d末尾的日K向下中枢序列(Phase5当下验证用)。"""
    clear_level('D')
    bis = build_bi(bars_d, level='D')
    if len(bis) < 6:
        return []
    clear_level('D')
    zs = build_zs(bis, level='D')
    return [z for z in zs if z.zg > z.zd * 1.003]


def _daily_c_divergence(bars_d, diff_d, dea_d, now, bis_d, d_idx):
    """日K当下验证v6(2026-08-07, 24课A段vs C段 + 27课三卖/低位 + 101课当下性)。

    Phase R阶段2(2026-08-10): now=当前30min bar时间(datetime)——日K可用性:
      盘中(now<15:00)只能用前一交易日日K; 15:00后当天日K可用(因果性)。
      ① 三卖检查(硬门槛, 24/27课"躲过盘整背驰→三卖"): c段内(排除前5根)
         最近回抽高点 < ZG×0.98 → 三卖确认 → 不买 (000838/000017)
      ② 面积背驰(24课基准): c段 vs 进入段, 平均力度
      ③ c段长度限制(≤200根≈10个月)
      ④ 盘整背驰(c未创新低): 仅当 c低 ≤ 近3年最低×1.08 (27课"低位意义不同")
    返回 (bool, 说明, c_start, c_low)。
    """
    from datetime import time as dtime
    # 可用日K最后根: dt.date()<now.date() 或 (==且now>=15:00)
    c_e = -1
    for i, b in enumerate(bars_d):
        if b.dt.date() < now.date() or (b.dt.date() == now.date() and now.time() >= dtime(15, 0)):
            c_e = i
        else:
            break
    if c_e < 0:
        return False, '当日无可用日K', '', 0
    sub = bars_d[:c_e + 1]
    if len(sub) < 200:
        return False, '日K数据不足', '', 0

    if not bis_d:
        return False, '无笔', '', 0
    clear_level('D')
    zs_d = build_zs(bis_d, level='D')
    downs = [z for z in zs_d if z.sdir == '向下' and z.zg > z.zd * 1.003]
    if not downs:
        return False, '无向下中枢', '', 0
    b = downs[-1]
    b_e = d_idx[b.end_dt[:10]]   # L1加固: 显式KeyError(原fallback指向未来端)
    c_s = b_e
    if c_e - c_s > 200:
        return False, 'c段过长(结构已变)', '', 0
    if c_e <= c_s + 5:
        # 中枢扩展中/离开段未形成(000537/000059型: 买点=中枢内低点):
        # 用中枢内最后一段下跌(入场日前60根内摆动高点 → 入场日)做背驰验证
        # (查找范围不限b_e: 中枢扩展中end≈当前, 限制b_e会查不到已结束的下跌段)
        hi = c_e
        for k in range(c_e - 1, max(0, c_e - 60), -1):
            if sub[k].high > sub[hi].high:
                hi = k
        c_s = hi
        if c_e <= c_s + 5:
            return False, '下跌段过短', '', 0
    c_sub = sub[c_s:c_e + 1]
    c_low = min(x.low for x in c_sub)
    # ① 三卖检查(24/27课): 下跌段内排除前5根后的回抽高点
    scan_part = c_sub[5:] if len(c_sub) > 5 else c_sub
    if scan_part:
        hi_rb = max(x.high for x in scan_part)
        if hi_rb < b.zg * 0.98:
            return False, f'三卖(回抽{hi_rb:.2f}<ZG{b.zg*0.98:.2f})', '', 0
    # ② 面积背驰(15课平均力度): 下跌段 vs 进入段(b.start前60根摆动高点→b.start,
    #    与scan_daily A段定义一致; 中枢相接时b段=1笔连接, 37课允许缺口)
    #    (2026-08-07修复: 原用prev.end→b.start, 中枢构建机制导致相邻中枢天然
    #    相接(b段=0), 371次误杀/年 → 改用摆动高点)
    b_s0 = d_idx[b.start_dt[:10]]   # L1加固: 显式KeyError(原fallback指向未来端)
    hi_p = b_s0
    for k in range(b_s0 - 1, max(0, b_s0 - 60), -1):
        if sub[k].high > sub[hi_p].high:
            hi_p = k
    p_s, p_e = hi_p, b_s0
    if p_e <= p_s + 3:
        return False, '进入段过短', '', 0
    area_p = macd_area(diff_d, dea_d, p_s, p_e, 'down')
    area_c = macd_area(diff_d, dea_d, c_s, c_e, 'down')
    len_p = max(p_e - p_s, 1); len_c = max(c_e - c_s, 1)
    if area_p >= 0 or area_c >= 0:
        return False, '无绿柱段', '', 0
    avg_p = abs(area_p) / len_p; avg_c = abs(area_c) / len_c
    if avg_c >= avg_p * 0.9:
        return False, f'面积未背驰({avg_c/avg_p:.2f})', '', 0
    # ④ 盘整背驰低位(27课): c未创新低需近3年低位(阈值1.08)
    if c_low >= b.dd * 0.999:
        low3y = min(x.low for x in sub[max(0, c_e - 756):c_e + 1])
        if c_low > low3y * 1.08:
            return False, f'非低位(c{c_low:.2f}>{low3y*1.08:.2f})', '', 0
    return True, f'c低{c_low:.2f} 力度{avg_c/avg_p:.2f}', b.end_dt, c_low


def _m30_trend_divergence(bis30, bars30, diff30, dea30, c_start_dt, idx, m30_idx):
    """30min次级别背驰段(2026-08-07, 27课区间套定理).

    日K c段内部, 30min级别找"针对最后30min中枢的背驰段":
      最后向下30min中枢(1个即可, 24课允许盘整背驰比较力度)
      + 离开段c'创新低(c'低点<DD×1.02 或 <ZG 离开中枢)
      + 面积背驰(c'段 vs 进入段, 平均力度<0.9)
    放宽理由: 日K c段通常1-3个月, 30min来不及形成2个向下中枢
    (Phase5 2中枢版=时序矛盾, 一买全灭); 39课"进入背驰段"即可作为信号。
    返回 (bool, 说明)。
    """
    if len(bis30) < 10:
        return False, '30m笔不足'
    clear_level('30')
    zs30 = build_zs(bis30, level='30')
    c_start = c_start_dt[:10]
    zs_after = [z for z in zs30 if z.end_dt[:10] >= c_start]
    downs = [z for z in zs_after if z.sdir == '向下']
    if not downs:
        return False, '30m无向下中枢'
    b = downs[-1]
    def _idx30(d):
        # L1加固(2026-08-12, 审查): 原fallback=len(bars30)-1指向未来端, 缺失时静默前视;
        # 改为显式KeyError暴露(正常路径日期均来自同一bars列表, 永不触发)
        if len(d) >= 19:
            return m30_idx[d]
        return m30_idx[d + ' 09:30:00']
    b_e = _idx30(b.end_dt)
    if idx <= b_e + 5:
        return False, '30m离开段过短'
    sub = bars30[:idx + 1]
    c_low = min(x.low for x in sub[b_e + 1:])
    if c_low >= b.zg * 1.02:
        return False, '30m未离开中枢'   # 离开段须跌破ZG(离开中枢)
    # 进入段: b前向下中枢end → b.start (24课A段基准)
    if len(downs) >= 2:
        prev = downs[-2]
        p_s, p_e = _idx30(prev.end_dt), _idx30(b.start_dt)
    else:
        p_s, p_e = 0, _idx30(b.start_dt)
    if p_e <= p_s + 3:
        return False, '30m进入段过短'
    area_p = macd_area(diff30, dea30, p_s, p_e, 'down')
    area_c = macd_area(diff30, dea30, b_e, idx, 'down')
    len_p = max(p_e - p_s, 1); len_c = max(idx - b_e, 1)
    if area_p >= 0 or area_c >= 0:
        return False, '30m无绿柱段'
    avg_p = abs(area_p) / len_p; avg_c = abs(area_c) / len_c
    if avg_c >= avg_p * 0.9:
        return False, f'30m面积未背驰({avg_c/avg_p:.2f})'
    return True, f'30m背驰 ZG{b.zg:.2f}->低{c_low:.2f} 力度{avg_c/avg_p:.2f}'


def _m30_top_divergence(bis30, bars30, diff30, dea30, bi_end_idx, m30_idx):
    """30min顶背驰 = 一卖(101课: "第一类买卖点就是背驰点"; 16课短差程序:
    大级别买点介入后, 次级别第一类卖点出现时减仓)。

    最近已确认向上笔(增量cursor末笔) + 红柱面积背驰(后1/3 < 前1/3×0.9)
    + 笔终点创段内新高(趋势顶背驰)。返回 (bool, 说明)。
    """
    if len(bis30) < 5:
        return False, '30m笔不足'
    last = bis30[-1]
    if last.direction != 'up':
        return False, '末笔非向上'

    def _idx30(d):
        # L1加固(2026-08-12, 审查): 原fallback=len(bars30)-1指向未来端, 缺失时静默前视;
        # 改为显式KeyError暴露(正常路径日期均来自同一bars列表, 永不触发)
        if len(d) >= 19:
            return m30_idx[d]
        return m30_idx[d + ' 09:30:00']

    s_i = _idx30(last.start_dt)
    e_i = min(_idx30(last.end_dt), bi_end_idx)
    if e_i <= s_i + 20:
        # P0(2026-08-11): 短段(<20根30min)面积三段划分失效(前1/3常无红柱)
        # → 与前一30min向上笔比较力度(16课"红柱没创新高"判据的笔间版)
        if e_i <= s_i + 5:
            return False, '30m向上段过短'
        prev_up = next((b for b in reversed(bis30[:-1]) if b.direction == 'up'), None)
        if prev_up is None:
            return False, '无前向上笔'
        p_s = _idx30(prev_up.start_dt)
        p_e = _idx30(prev_up.end_dt)
        if p_e <= p_s + 5 or p_e > s_i:
            return False, '前段异常'
        area_p = macd_area(diff30, dea30, p_s, p_e, 'up')
        area_c = macd_area(diff30, dea30, s_i, e_i, 'up')
        if area_p <= 0 or area_c <= 0:
            return False, '无红柱段'
        avg_p = area_p / max(p_e - p_s, 1)
        avg_c = area_c / max(e_i - s_i, 1)
        if avg_c >= avg_p * 0.9:
            return False, f'笔间力度未背驰({avg_c/avg_p:.2f})'
        return True, f'短段背驰(与前笔比 力度{avg_c/avg_p:.2f})'
    seg = bars30[s_i:e_i + 1]
    seg_high = max(x.high for x in seg)
    if last.high < seg_high * 0.999:
        return False, '笔终点未创新高'
    third = (e_i - s_i) // 3
    if third < 6:
        return False, '段过短'
    area_f = macd_area(diff30, dea30, s_i, s_i + third, 'up')
    area_b = macd_area(diff30, dea30, s_i + 2 * third, e_i, 'up')
    len_f = max(third, 1)
    len_b = max(e_i - (s_i + 2 * third), 1)
    if area_f <= 0 or area_b <= 0:
        return False, '无红柱段'
    avg_f = area_f / len_f
    avg_b = area_b / len_b
    if avg_b >= avg_f * 0.9:
        return False, f'面积未背驰({avg_b/avg_f:.2f})'
    return True, f'顶背驰 力度{avg_b/avg_f:.2f}'


def _daily_top_divergence(bis_d, bars_d, diff_d, dea_d, d_idx, target=None, allow_short=False):
    """日K顶背驰 = 日K级别一卖(101课"第一类买卖点就是背驰点")。

    与30min版对称, 但用于操作级别(日K)清仓: 日K向上笔确认结束
    + 红柱面积背驰(后1/3 < 前1/3×0.9) + 笔终点创段内新高。
    次级别(30min)一卖仅作短差(16课), 清仓以日K卖点为准。

    P0(2026-08-11): ① target参数——默认末笔; 末笔为down时可传前一支up笔
    (000592型: 笔确认当天bis已+1且末笔变down → 一卖检查错过) ② 短段
    (<12根日K)面积三段划分失效 → 与前一向上笔比较力度; allow_short=False
    时短段笔不检查(趋势中继小顶不误杀, 000592 2024-09 +8%短段被卖飞+400%)。
    """
    if len(bis_d) < 5:
        return False, '日K笔不足'
    last = target if target is not None else bis_d[-1]
    if last.direction != 'up':
        return False, '末笔非向上'

    def _idx(d):
        # L1加固(2026-08-12): 原fallback=len(bars_d)-1指向未来端; 改显式KeyError
        return d_idx[d[:10]]

    s_i = _idx(last.start_dt)
    e_i = _idx(last.end_dt)
    if e_i <= s_i + 5:
        return False, '向上段过短'
    seg = bars_d[s_i:e_i + 1]
    if last.high < max(x.high for x in seg) * 0.999:
        return False, '笔终点未创新高'
    if e_i - s_i < 12:
        # P0(2026-08-11二轮): 短段(<12根日K)不检查 — 笔级与前笔比力度在趋势中
        # 必然假背驰(中继回调力度必小于主升段, 000592 2025-01 +106%被卖飞+800%)。
        # 不创新高的顶部由"二卖(日K不创新高)"覆盖(2007-11-20专文: 不能创新高或背驰)
        return False, '短段不检查(趋势中继小顶, 二卖覆盖不创新高)'
    third = (e_i - s_i) // 3
    if third < 2:
        return False, '段过短'
    area_f = macd_area(diff_d, dea_d, s_i, s_i + third, 'up')
    area_b = macd_area(diff_d, dea_d, s_i + 2 * third, e_i, 'up')
    len_f = max(third, 1)
    len_b = max(e_i - (s_i + 2 * third), 1)
    if area_f <= 0 or area_b <= 0:
        return False, '无红柱段'
    avg_f = area_f / len_f
    avg_b = area_b / len_b
    if avg_b >= avg_f * 0.9:
        return False, f'面积未背驰({avg_b/avg_f:.2f})'
    return True, f'日K顶背驰 力度{avg_b/avg_f:.2f}'




def _m30_bottom_divergence(bis30, bars30, diff30, dea30, bi_end_idx, m30_idx):
    """30min底背驰 = 次级别一买(16课短差回补: "次级别第一类买点出现时回补")。

    对称于_m30_top_divergence: 最近已确认向下笔 + 绿柱面积背驰
    (后1/3 < 前1/3×0.9) + 笔终点创段内新低。返回 (bool, 说明)。
    """
    if len(bis30) < 5:
        return False, '30m笔不足'
    last = bis30[-1]
    if last.direction != 'down':
        return False, '末笔非向下'

    def _idx30(d):
        # L1加固(2026-08-12, 审查): 原fallback=len(bars30)-1指向未来端, 缺失时静默前视;
        # 改为显式KeyError暴露(正常路径日期均来自同一bars列表, 永不触发)
        if len(d) >= 19:
            return m30_idx[d]
        return m30_idx[d + ' 09:30:00']

    s_i = _idx30(last.start_dt)
    e_i = min(_idx30(last.end_dt), bi_end_idx)
    if e_i <= s_i + 20:
        # P0: 短段与前一30min向下笔比较力度
        if e_i <= s_i + 5:
            return False, '30m向下段过短'
        prev_dn = next((b for b in reversed(bis30[:-1]) if b.direction == 'down'), None)
        if prev_dn is None:
            return False, '无前向下笔'
        p_s = _idx30(prev_dn.start_dt)
        p_e = _idx30(prev_dn.end_dt)
        if p_e <= p_s + 5 or p_e > s_i:
            return False, '前段异常'
        area_p = macd_area(diff30, dea30, p_s, p_e, 'down')
        area_c = macd_area(diff30, dea30, s_i, e_i, 'down')
        if area_p >= 0 or area_c >= 0:
            return False, '无绿柱段'
        avg_p = abs(area_p) / max(p_e - p_s, 1)
        avg_c = abs(area_c) / max(e_i - s_i, 1)
        if avg_c >= avg_p * 0.9:
            return False, f'笔间力度未背驰({avg_c/avg_p:.2f})'
        return True, f'短段底背驰 力度{avg_c/avg_p:.2f}'
    seg = bars30[s_i:e_i + 1]
    seg_low = min(x.low for x in seg)
    if last.low > seg_low * 1.001:
        return False, '笔终点未创新低'
    third = (e_i - s_i) // 3
    if third < 6:
        return False, '段过短'
    area_f = macd_area(diff30, dea30, s_i, s_i + third, 'down')
    area_b = macd_area(diff30, dea30, s_i + 2 * third, e_i, 'down')
    if area_f >= 0 or area_b >= 0:
        return False, '无绿柱段'
    avg_f = abs(area_f) / max(third, 1)
    avg_b = abs(area_b) / max(e_i - (s_i + 2 * third), 1)
    if avg_b >= avg_f * 0.9:
        return False, f'面积未背驰({avg_b/avg_f:.2f})'
    return True, f'底背驰 力度{avg_b/avg_f:.2f}'


def scan_30min_entry(symbol: str, conn=None, edt=None):
    """30min介入层扫描: 日K候选 + 二买(次级别回调确认)"""
    daily_cands = scan_daily(symbol, edt or "20260804", conn=conn)
    if not daily_cands:
        return [], []

    bars30 = load_30min(symbol, conn=conn)
    if edt:
        edt_dt = datetime.strptime(edt, "%Y%m%d")
        bars30 = [b for b in bars30 if b.dt <= edt_dt]
    if len(bars30) < 500:
        return daily_cands, []

    # 30min走势序列(二买用: 一买后第一次30min反弹→回调)
    clear_level('30')
    bis30 = build_bi(bars30, level='30')
    clear_level('30')
    zs30 = build_zs(bis30, level='30')
    trends30 = build_trends(zs30) if len(zs30) >= 3 else []

    def _bar_idx(dt_str):
        for i, b in enumerate(bars30):
            if b.dt.strftime("%Y-%m-%d") >= dt_str[:10]:
                return i
        return len(bars30) - 1

    # ── 二买: 日K一买后, 30min走势 第一个向上→第一个向下, 回调不破A ──
    # Phase5约束(2026-08-07, 101课"中阴状态"+21课"二买位置"):
    #   ① C/A ≤ 1.15: 二买=次级别回调, C应接近A(000683 C/A=1.69是8个月后新下跌,
    #      中阴状态已结束, 不存在一二买)
    #   ② C不得跌破日K最后向上中枢ZD(000880 C=25.95<ZD=31.76, 21课
    #      "中枢下出现的二买, 力度值得怀疑"): 一买后上涨段被吞没=走势性质已变
    buy2_list = []
    if any(c.buy_type == '一买' for c in daily_cands) and trends30:
        # 日K最后中枢(截至当前, 二买位置约束用)
        clear_level('D')
        bars_d = load_daily(symbol, conn=conn)
        if edt:
            edt_dt = datetime.strptime(edt, "%Y%m%d")
            bars_d = [b for b in bars_d if b.dt <= edt_dt]
        zs_d = _daily_down_zs(bars_d) if len(bars_d) >= 200 else []
        # 全向中枢也要(找向上中枢)
        clear_level('D')
        bis_d = build_bi(bars_d, level='D') if len(bars_d) >= 200 else []
        clear_level('D')
        zs_d_all = build_zs(bis_d, level='D') if bis_d else []
        zs_d_all = [z for z in zs_d_all if z.zg > z.zd * 1.003]
        for ym in [c for c in daily_cands if c.buy_type == '一买']:
            # 课本修正(2026-08-06, p246"第一类买卖点就是背驰点"):
            # 二买起点=背驰点日期(a_date), 不是信号日(中枢完成日,滞后1-3.5个月,
            # 导致"第一次次级别回调"识别错位, 000400案例C=27.80应为23.94)
            ym_end = (ym.a_date or ym.signal_date)[:10]
            start_i = next((i for i, t in enumerate(trends30)
                            if t.start_dt[:10] >= ym_end), len(trends30))
            while start_i < len(trends30) and trends30[start_i].end_dt[:10] <= ym_end:
                start_i += 1
            bounce = None
            j = start_i
            while j < len(trends30):
                if trends30[j].sdir == '向上':
                    bounce = trends30[j]
                    break
                j += 1
            if bounce is None or j + 1 >= len(trends30):
                continue
            pullback = trends30[j + 1]
            if pullback.sdir != '向下':
                continue
            if pullback.low < ym.a_price * 0.98:
                continue
            sig_dt = pullback.end_dt[:10]
            pb_s = _bar_idx(pullback.start_dt)
            pb_e = _bar_idx(pullback.end_dt)
            pb_sub = bars30[pb_s:pb_e + 1]
            min_pb = min(pb_sub, key=lambda x: x.low) if pb_sub else None
            c_low = min_pb.low if min_pb else pullback.low
            c_date = min_pb.dt.strftime("%Y-%m-%d") if min_pb else sig_dt
            # Phase5 ① C/A约束: 次级别回调的C应贴近一买背驰点A
            # (2026-08-12收紧: 1.15→1.08, 与动态生成一致 — 入场/A≥1.15组18%胜率)
            if ym.a_price > 0 and c_low > ym.a_price * 1.08:
                continue
            # Phase5 ② C位置约束: pullback期间的最后日K向上中枢ZD
            if zs_d_all:
                up_zs = [z for z in zs_d_all if z.sdir == '向上'
                         and z.start_dt[:10] <= sig_dt <= z.end_dt[:10]]
                if up_zs:
                    z_last = up_zs[-1]
                    if c_low < z_last.zd * 0.999:
                        continue
            buy2_list.append(BuyCandidate(
                buy_type='二买', signal_date=sig_dt,
                window_end=_add_weeks(sig_dt, 26),
                l2=ym.l2, a_price=ym.a_price, a_date=ym.a_date,
                b_price=bounce.high, b_date=bounce.end_dt[:10],
                c_price=c_low, c_date=c_date,
                div_type=ym.div_type))   # P1-1(2026-08-13): 传承一买源类型(27课分组)

    return daily_cands, buy2_list


def backtest_one(symbol: str, sdt="20260101", edt="20260804", conn=None,
                 audit_trace=False):
    """单只: 日K买点动态生成 + 30min介入 bar walk (Phase R阶段4a)。

    因果性(2026-08-10 Phase R):
      ① 日K可用性: 盘中(now<15:00)只用前一交易日日K
      ② 候选动态生成: 每天用可用日K重扫(scan_daily_on_bars), 无末日预扫描
      ③ 成交延迟: 信号t收盘确认 → t+1开盘成交
      ④ 二买: 暂禁用(等增量分析器后4b恢复)
    """
    from datetime import time as dtime
    clear_all()
    edt_dt = datetime.strptime(edt, "%Y%m%d")

    bars = load_30min(symbol, conn=conn)
    if edt:
        bars = [b for b in bars if b.dt <= edt_dt]
    if len(bars) < 500:
        result = {'symbol': symbol, 'trades': [], 'candidates': []}
        if audit_trace:
            result['audit_trace'] = {
                'schema_version': '1.0', 'symbol': symbol, 'status': 'INSUFFICIENT_DATA',
                'candidate_events': [], 'decision_events': [], 'errors': [],
            }
        return result

    sdt_dt = datetime.strptime(sdt, "%Y%m%d")
    start_idx = next((i for i, b in enumerate(bars) if b.dt >= sdt_dt), 0)

    closes = [b.close for b in bars]
    diff, dea, _ = macd(closes)

    # 日K(全量; MACD因果性: 索引边界内取前段即无前视)
    bars_d = load_daily(symbol, conn=conn)
    if edt:
        bars_d = [b for b in bars_d if b.dt <= edt_dt]
    _c = [b.close for b in bars_d]
    diff_d, dea_d, _ = macd(_c) if len(bars_d) >= 200 else (None, None, None)

    # ── 动态候选池(日K每天重扫生成) ──
    candidates = []
    last_scan_day = None

    pos = None
    trades = []
    last_exit_idx = -999
    consumed = set()
    ENTRY_GAP = 40
    in_zone = {}
    dead = set()
    cand_keys = set()   # signal槽去重；同日一买若来源更新则以最近中枢替换
    leave_phase = {}
    pending = None
    m30_ok = set()
    m30_last_check = {}
    reentry = None   # P0(2026-08-11): 30min一卖退出后的短差回补观察

    def _b1_source_rank(c):
        """同一信号槽的一买来源新旧顺序；只使用当时结构字段。"""
        source_zs = getattr(c, 'l2', None)
        return (
            getattr(source_zs, 'end_dt', ''),
            getattr(source_zs, 'start_dt', ''),
            repr(float(getattr(source_zs, 'zd', 0) or 0)),
            repr(float(getattr(source_zs, 'zg', 0) or 0)),
        )

    # 审计是只写旁路：关闭时不创建/修改任何旧返回字段。
    audit_log = ({
        'schema_version': '1.0', 'symbol': symbol, 'status': 'OK',
        'candidate_events': [], 'decision_events': [], 'errors': [],
    } if audit_trace else None)
    audit_cands = {}       # candidate_id -> metadata
    audit_obj_ids = {}     # id(BuyCandidate) -> candidate_id

    def _cand_id(c):
        source_zs = getattr(c, 'l2', None)
        return _audit_stable_id(
            'cand', symbol, c.buy_type, c.signal_date, c.a_date,
            repr(float(c.a_price or 0)), c.c_date, repr(float(c.c_price or 0)),
            getattr(source_zs, 'start_dt', ''), getattr(source_zs, 'end_dt', ''),
            repr(float(getattr(source_zs, 'zd', 0) or 0)),
            repr(float(getattr(source_zs, 'zg', 0) or 0)))

    def _register_cand(c, seen_at, source=None, occur_at=None, confirm_at=None):
        if not audit_trace:
            return None
        cid = audit_obj_ids.get(id(c)) or _cand_id(c)
        audit_obj_ids[id(c)] = cid
        source_id = audit_obj_ids.get(id(source)) if source is not None else None
        if source is not None and source_id is None:
            source_id = _register_cand(source, seen_at)
        if cid not in audit_cands:
            audit_cands[cid] = {
                'candidate_id': cid, 'source_b1_id': source_id,
                'buy_type': c.buy_type,
                'occur_at': occur_at or c.c_date or c.a_date or c.signal_date or None,
                'confirm_at': confirm_at or c.signal_date or None,
                'first_seen_at': seen_at,
                'signal_at': c.signal_date or None, 'window_end': c.window_end or None,
                'state': 'generated',
                'abc': {
                    'A': {'at': c.a_date or None, 'price': float(c.a_price) if c.a_price else None},
                    'B': {'at': c.b_date or None, 'price': float(c.b_price) if c.b_price else None},
                    'C': {'at': c.c_date or None, 'price': float(c.c_price) if c.c_price else None},
                    'macd': {'status': 'UNKNOWN', 'A_area': None, 'B_area': None,
                             'C_area': None, 'lengths': None, 'average_force': None,
                             'reason': 'BuyCandidate does not retain segment MACD evidence'},
                    'divergence_type': c.div_type or None,
                },
                'structure': _audit_candidate_structure(c),
            }
            audit_log['candidate_events'].append({
                'at': seen_at, 'candidate_id': cid, 'event': 'generated',
                'state': 'generated', 'reason': 'engine scanner output',
            })
        elif source_id and not audit_cands[cid].get('source_b1_id'):
            audit_cands[cid]['source_b1_id'] = source_id
        return cid

    def _cand_event(c, at, event, state, reason, extra=None):
        if not audit_trace:
            return
        cid = _register_cand(c, at)
        audit_cands[cid]['state'] = state
        item = {'at': at, 'candidate_id': cid, 'event': event,
                'state': state, 'reason': reason}
        if extra:
            item['evidence'] = extra
        audit_log['candidate_events'].append(item)

    def _decision_candidate(c, bar_, idx_, sub_bars_, diff_, dea_):
        """在入场决策时冻结所有候选的同口径谓词；异常显式为 UNKNOWN。"""
        cid = _register_cand(c, bar_.dt.strftime('%Y-%m-%d %H:%M:%S'))
        cur_ = float(bar_.close)
        key_ = (c.buy_type, c.signal_date)
        out = {
            'candidate_id': cid, 'source_b1_id': audit_cands[cid].get('source_b1_id'),
            'buy_type': c.buy_type,
            'predicates': {
                'not_consumed': key_ not in consumed, 'not_invalidated': key_ not in dead,
                'window_active': c.signal_date[:10] <= bar_.dt.strftime('%Y-%m-%d') <= c.window_end[:10],
                'buy_type_entry_enabled': c.buy_type != '一买',
            },
            'thresholds': {}, 'actual_reason': None,
        }
        try:
            df_idx_ = _find_bottom_fractal_idx(sub_bars_)
            recent = df_idx_ >= len(sub_bars_) - 6
            df_low_ = float(sub_bars_[df_idx_].low) if 0 <= df_idx_ < len(sub_bars_) else None
            out['predicates']['bottom_fractal_recent'] = recent
            out['full_precision'] = {'close': cur_, 'bar_low': float(bar_.low),
                                     'bar_open': float(bar_.open), 'fractal_low': df_low_}
            if c.buy_type == '三买':
                zg_ = c.zs_zg if c.zs_zg > 0 else (c.l2.zg if c.l2 else 0)
                out['thresholds'] = {'ZG': float(zg_) if zg_ else None,
                                     'zone_low_exclusive': float(zg_) if zg_ else None,
                                     'zone_high': float(zg_ * 1.05) if zg_ else None,
                                     'fractal_low': float(zg_ * 0.98) if zg_ else None,
                                     'fractal_high': float(zg_ * 1.05) if zg_ else None}
                out['predicates'].update({
                    'has_ZG': zg_ > 0, 'leave_then_pullback': leave_phase.get(key_) == 'pullback',
                    'price_in_zone': bool(zg_ and zg_ < cur_ <= zg_ * 1.05),
                    'fractal_in_zone': bool(zg_ and df_low_ is not None and zg_ * 0.98 <= df_low_ <= zg_ * 1.05),
                })
            elif c.buy_type == '二买':
                A_, C_ = float(c.a_price or 0), float(c.c_price or c.a_price or 0)
                cap_ = 1.05 if (c.div_type or '') == '盘整背驰' else 1.10
                out['thresholds'] = {'A': A_ or None, 'C': C_ or None,
                                     'entry_cap_ratio': cap_, 'entry_cap': A_ * cap_ if A_ else None,
                                     'zone_low': C_ * .98 if C_ else None,
                                     'zone_high': C_ * 1.05 if C_ else None}
                out['predicates'].update({
                    'has_A': A_ > 0, 'below_entry_cap': bool(A_ and cur_ <= A_ * cap_),
                    'price_in_zone': bool(C_ and C_ * .98 <= cur_ <= C_ * 1.05),
                    'fractal_in_zone': bool(C_ and df_low_ is not None and C_ * .98 <= df_low_ <= C_ * 1.05),
                })
            out['predicates']['no_new_low_after_fractal'] = (
                df_low_ is not None and float(bar_.low) >= df_low_)
            out['predicates']['fractal_divergence'] = bool(
                recent and _divergence_at_fractal(sub_bars_, diff_[:idx_ + 1],
                                                  dea_[:idx_ + 1], df_idx_))
        except Exception as exc:
            out['predicate_error'] = {'status': 'UNKNOWN', 'error_type': type(exc).__name__,
                                      'message': str(exc)}
            audit_log['errors'].append({'at': bar_.dt.strftime('%Y-%m-%d %H:%M:%S'),
                                        'stage': 'entry_predicate_snapshot',
                                        'error_type': type(exc).__name__, 'message': str(exc)})
        return out

    def _exit_predicates(pos_, bar_, idx_, pnl_, adopted):
        cur_ = float(bar_.close)
        zg_ = float(pos_.get('entry_zg') or 0)
        peak_ = float(pos_.get('peak30') or 0)
        zd_ = pos_.get('d_zs_last_zd')
        return {
            'adopted_reason': adopted,
            'predicates': {
                'm30_first_or_second_sell': adopted.startswith(('一卖(30min', '二卖(30min')),
                'daily_first_or_second_sell': adopted.startswith(('一卖(日K', '二卖(日K')),
                'daily_third_sell_break_ZD': bool(zd_ is not None and cur_ < float(zd_) * .97),
                'break_A_confirmed': adopted.startswith('破A确认'),
                'third_buy_consolidation_drawdown': bool(zg_ and idx_ - pos_['entry_idx'] > 40
                                                          and cur_ < peak_ * .95),
                'third_buy_break_ZG': bool(zg_ and cur_ < zg_ * .99),
                'engineering_stop_10pct': pnl_ < -.10,
            },
            'thresholds': {'daily_ZD_x_0_97': float(zd_) * .97 if zd_ is not None else None,
                           'A_x_0_995': float(pos_.get('a_low') or 0) * .995,
                           'ZG_x_0_99': zg_ * .99 if zg_ else None,
                           'peak_x_0_95': peak_ * .95 if peak_ else None,
                           'pnl_stop': -.10},
            'full_precision': {'decision_close': cur_, 'pnl_ratio': float(pnl_)},
        }

    # ── Phase R阶段5: 增量游标(30min逐根 / 日K按可用性) ──
    from datetime import time as _dtime
    d_cursor = BiCursor()
    for _b in bars_d:
        if _b.dt.date() < sdt_dt.date():
            d_cursor.update(_b)
        else:
            break
    last_d_bi_n = len(d_cursor.bis)   # 卖点检查: 日K新笔增量检测(2026-08-11)
    # Phase R性能优化: 日期→索引映射(30min用完整datetime, 一天多根不冲突)
    m30_idx = {}
    for _i, _x in enumerate(bars):
        m30_idx.setdefault(_x.dt.strftime('%Y-%m-%d %H:%M:%S'), _i)

    def _bar_idx30(d):
        if len(d) >= 19:
            return m30_idx.get(d, len(bars) - 1)
        return m30_idx.get(d + ' 09:30:00', len(bars) - 1)

    d_idx = {}
    for _i, _x in enumerate(bars_d):
        d_idx.setdefault(_x.dt.strftime('%Y-%m-%d'), _i)
    d_dates = [_x.dt.date() for _x in bars_d]

    m30_cursor = BiCursor()
    for _b in bars[:start_idx]:
        m30_cursor.update(_b)
    last_bi_n = len(m30_cursor.bis)   # 卖点检查: 新笔增量检测(2026-08-11)
    last_daily_updated = None   # 已喂入日K的最后日期

    for idx in range(start_idx, len(bars)):
        bar = bars[idx]
        cur = bar.close
        day_str = bar.dt.strftime("%Y-%m-%d")
        m30_cursor.update(bar)   # Phase R阶段5: 30min增量(本根已收盘)

        # ── P0: 短差回补成交(30min底背驰确认 → 下一根bar开盘回补, 16课) ──
        if USE_30M_SWING and pos is None and reentry is not None and reentry.get('pending'):
            pp = reentry.pop('pending')
            pos = {
                'name': '回补(30min一买)', 'entry_price': bar.open,
                'entry_idx': idx,
                'entry_dt': bar.dt.strftime("%Y-%m-%d %H:%M"),
                'peak_price': bar.open,
                'a_low': pp.get('a_low') or 0, 'b_high': 0, 'entry_zg': 0,
                'div_type': '', 'entry_reason': '30min短差回补(16课)',
                'd_bi_n': len(d_cursor.bis), 'm30_bi_n': len(m30_cursor.bis),
                'peak30': bar.open, 'a_warn': None, 'a_warn_bi': len(m30_cursor.bis),
            }
            if audit_trace:
                fill_at = bar.dt.strftime("%Y-%m-%d %H:%M:%S")
                pos['_audit'] = {
                    'trade_id': _audit_stable_id('trade', symbol, 'reentry', fill_at),
                    'candidate_id': None, 'source_b1_id': None,
                    'occur_at': None, 'confirm_at': pp.get('confirm_at'),
                    'first_seen_at': reentry.get('born_at'),
                    'decision_at': pp.get('decision_at'), 'fill_at': fill_at,
                    'structure': {'status': 'UNKNOWN', 'reason': 'short-swing reentry has no daily candidate'},
                    'abc_macd': {'status': 'UNKNOWN'},
                    'entry_predicates': pp.get('entry_predicates', {}),
                    'full_precision': {'entry_price': float(bar.open)},
                }
            continue

        # ── pending成交(信号t收盘确认 → t+1开盘成交) ──
        if pending is not None:
            p = pending
            pending = None
            if p['kind'] == 'entry':
                pos = {
                    'name': p['pname'], 'entry_price': bar.open,
                    'entry_idx': idx,
                    'entry_dt': bar.dt.strftime("%Y-%m-%d %H:%M"),
                    'peak_price': bar.open,
                    'a_low': p['entry_a'], 'b_high': p['entry_b'],
                    'entry_zg': p['entry_zg'],
                    'div_type': p.get('div_type', ''),
                    'entry_reason': f"日K买点+30min介入 {p['pname']}",
                    'd_bi_n': len(d_cursor.bis),   # 卖点: 入场时日K笔数(2026-08-11)
                    'm30_bi_n': len(m30_cursor.bis),  # P0: 30min卖点增量检测
                    'peak30': bar.open,               # P0: 持仓期30min高点(二卖不创新高)
                    'a_warn': None,                   # P0: 破A预警状态
                    'a_warn_bi': len(m30_cursor.bis), # P0: 预警时30min笔数
                }
                if audit_trace:
                    cand_meta = audit_cands.get(p.get('candidate_id'), {})
                    fill_at = bar.dt.strftime("%Y-%m-%d %H:%M:%S")
                    pos['_audit'] = {
                        'trade_id': _audit_stable_id('trade', symbol, p.get('candidate_id'), fill_at),
                        'candidate_id': p.get('candidate_id'),
                        'source_b1_id': cand_meta.get('source_b1_id'),
                        'occur_at': cand_meta.get('occur_at'),
                        'confirm_at': cand_meta.get('confirm_at'),
                        'first_seen_at': cand_meta.get('first_seen_at'),
                        'decision_at': p.get('decision_at'), 'fill_at': fill_at,
                        'structure': p.get('structure'), 'abc_macd': cand_meta.get('abc'),
                        'entry_predicates': p.get('entry_predicates', {}),
                        'full_precision': {'entry_price': float(bar.open),
                                           'decision_close': p.get('decision_close')},
                    }
                reentry = None   # P0: 30min卖点退出后进入回补观察
            else:
                exit_price = bar.open
                pnl = (exit_price - p['entry_price']) / p['entry_price']
                trade = {
                    'symbol': symbol, 'name': p['name'],
                    'entry': p['entry_dt'][:10], 'exit': bar.dt.strftime("%Y-%m-%d"),
                    'entry_price': p['entry_price'], 'exit_price': exit_price,
                    'pnl_pct': round(pnl * 100, 2),
                    'hold': idx - p['entry_idx'],
                    'reason': p['reason'], 'entry_reason': p.get('entry_reason', ''),
                    'div_type': p.get('div_type', ''),
                }
                if audit_trace:
                    ta = dict(p.get('_audit') or {})
                    ta['exit_decision_at'] = p.get('exit_decision_at')
                    ta['exit_fill_at'] = bar.dt.strftime("%Y-%m-%d %H:%M:%S")
                    ta['exit_predicates'] = p.get('exit_predicates', {})
                    ta.setdefault('full_precision', {})['exit_price'] = float(exit_price)
                    ta['full_precision']['pnl_ratio'] = float(pnl)
                    trade['audit'] = ta
                trades.append(trade)
                pos = None
                last_exit_idx = idx
                continue

        # ── Phase R阶段4a: 动态候选生成(每天一次, 用可用日K) ──
        # 15:00后: 当天日K可用 → 喂入日K游标
        # (2026-08-11修复: 原逻辑在day_str变化块内, 每天10:00首根bar进入块但15:00
        #  才满足时间条件 → 日K永不喂入, d_cursor.bis停滞在sdt前 → 卖点检查全失效)
        if bar.dt.time() >= _dtime(15, 0) and last_daily_updated != day_str:
            last_daily_updated = day_str
            # P0-1性能(2026-08-12, 审查H2): d_idx O(1)查表替代逐根strftime(~52万次/只)
            _dj = d_idx.get(day_str)
            if _dj is not None:       # 日K缺失(新股首日等)则跳过, 与原strftime未命中等价
                d_cursor.update(bars_d[_dj])
        if day_str != last_scan_day:
            last_scan_day = day_str
            if diff_d is not None:
                # 可用日K: dt.date()<now.date() 或 (==且now>=15:00)
                if FAST_DAILY_AVAIL:
                    # bars_d按日期有序。二分得到与下方基线路径逐项相同的前缀，
                    # 避免每个交易日从第一根日K扫描到未来数据末端。
                    if bar.dt.time() >= _dtime(15, 0):
                        avail_end = bisect_right(d_dates, bar.dt.date())
                    else:
                        avail_end = bisect_left(d_dates, bar.dt.date())
                    avail = bars_d[:avail_end]
                else:
                    avail = []
                    for b in bars_d:
                        if b.dt.date() < bar.dt.date() or (b.dt.date() == bar.dt.date() and bar.dt.time() >= _dtime(15, 0)):
                            avail.append(b)
                        else:
                            break
                if len(avail) >= 200:
                    # MACD因果截断: 全量diff的前缀=avail的macd(EMA只依赖前缀)
                    _c_e = len(avail) - 1
                    if FAST_DAILY_MACD_PREFIX:
                        _df, _de = diff_d, dea_d
                    else:
                        _df = diff_d[:_c_e + 1]
                        _de = dea_d[:_c_e + 1]
                    new_cands = scan_daily_on_bis(d_cursor.bis, avail, _df, _de, d_idx)
                    for c in new_cands:
                        audit_at = bar.dt.strftime('%Y-%m-%d %H:%M:%S')
                        if audit_trace:
                            _register_cand(c, audit_at)
                        key = (c.buy_type, c.signal_date)
                        if key in consumed or key in dead:
                            if audit_trace:
                                why = ('already_consumed' if key in consumed else
                                       'already_invalidated')
                                cid = _register_cand(c, audit_at)
                                audit_log['candidate_events'].append({
                                    'at': audit_at, 'candidate_id': cid, 'event': 'rejected',
                                    'state': audit_cands[cid]['state'], 'reason': why})
                            continue
                        existing_same = next((
                            x for x in candidates
                            if (x.buy_type, x.signal_date) == key
                        ), None)
                        source_update = bool(
                            key in cand_keys and c.buy_type == '一买'
                            and existing_same is not None
                            and _b1_source_rank(c) > _b1_source_rank(existing_same)
                        )
                        if key in cand_keys and not source_update:
                            if audit_trace:
                                cid = _register_cand(c, audit_at)
                                audit_log['candidate_events'].append({
                                    'at': audit_at, 'candidate_id': cid, 'event': 'rejected',
                                    'state': audit_cands[cid]['state'], 'reason': 'duplicate_key'})
                            continue
                        # FILTER_L2(49课): 周线L2向下滤一买/二买(三买豁免)
                        if FILTER_L2 and c.weekly_dir == '向下' and c.buy_type != '三买':
                            if audit_trace:
                                _cand_event(c, audit_at, 'filtered', 'invalidated',
                                            'FILTER_L2 weekly direction is down')
                            continue
                        if c.buy_type == '三买' and c.zs_end_date:
                            wend = min(c.window_end[:10],
                                       (datetime.strptime(c.signal_date[:10], "%Y-%m-%d") + timedelta(weeks=SIGNAL_WINDOW_WEEKS)).strftime("%Y-%m-%d"))
                            c.window_end = wend
                        # 一买中阴边界(2026-08-11, 101课): 一买后BUY1_WINDOW_WEEKS内
                        # 才有二买(紧接的次级别回调); 之后中阴结束, 旧一买作废
                        if c.buy_type == '一买':
                            wend = min(c.window_end[:10],
                                       (datetime.strptime(c.signal_date[:10], "%Y-%m-%d") + timedelta(weeks=BUY1_WINDOW_WEEKS)).strftime("%Y-%m-%d"))
                            c.window_end = wend
                        if source_update:
                            # 同一背驰日后来确认了更近的中枢来源：消费前替换旧源，
                            # 并移除尚未消费、由旧源派生的二买，随后在本轮重新生成。
                            stale_children = [
                                x for x in candidates
                                if x.buy_type == '二买'
                                and getattr(x, '_source_b1_slot', None) == key
                                and (x.buy_type, x.signal_date) not in consumed
                            ]
                            if audit_trace:
                                _cand_event(
                                    existing_same, audit_at, 'superseded', 'invalidated',
                                    'same signal date now has a newer center provenance',
                                    {'new_candidate_id': _register_cand(c, audit_at)})
                                for child in stale_children:
                                    _cand_event(
                                        child, audit_at, 'invalidated', 'invalidated',
                                        'source first-buy provenance was superseded')
                            stale_ids = {id(x) for x in stale_children}
                            candidates = [
                                x for x in candidates
                                if x is not existing_same and id(x) not in stale_ids
                            ]
                            candidates.append(c)
                            if audit_trace:
                                _cand_event(
                                    c, audit_at, 'activated', 'active',
                                    'replaced older same-date center provenance')
                            continue
                        # 一买: 同类型保留最新signal(新信号作废旧信号, 49课)
                        if c.buy_type == '一买':
                            if audit_trace:
                                for old in candidates:
                                    if old.buy_type == '一买' and old.signal_date < c.signal_date:
                                        _cand_event(old, audit_at, 'superseded', 'invalidated',
                                                    f'newer first-buy signal {c.signal_date}')
                            candidates = [x for x in candidates
                                          if not (x.buy_type == '一买' and x.signal_date < c.signal_date)]
                        candidates.append(c)
                        cand_keys.add(key)
                        if audit_trace:
                            _cand_event(c, audit_at, 'activated', 'active', 'passed generation filters')

                    # ── Phase R阶段4b: 二买动态生成(活跃一买[中阴边界内] + 30min首次次级别回调笔) ──
                    # 课本101课(2008-03-04答疑): "所谓第二类买点, 就是第一类买点的次级别
                    # 回抽结束后再次探底或回试的那个次级别走势的结束点" — 次级别回调=30min
                    # 一段走势(笔级即可, 无需30min中枢); 一二买"紧接"(中阴状态内)
                    # (2026-08-11 修改: trend版[需30min中枢]错过首次浅回调, 信号延迟60-229天)
                    ym_active = [x for x in candidates
                                 if x.buy_type == '一买' and x.signal_date[:10] <= day_str
                                 and day_str <= x.window_end[:10]          # 中阴边界(BUY1_WINDOW_WEEKS)
                                 and (x.buy_type, x.signal_date) not in dead]
                    if ym_active:
                        ym = max(ym_active, key=lambda x: x.signal_date)
                        ym_end = (ym.a_date or ym.signal_date)[:10]
                        bis30 = m30_cursor.bis
                        # A: 一买后第一次"合格"次级别回调(向下笔, 101课"回抽结束后再次
                        # 探底或回试的那个次级别走势的结束点"; 笔级即可无需中枢)
                        # 前几次回调若离A太远(C/A>1.08, 追高)或深破(C/A<0.90, 结构破坏)
                        # 则跳过, 取首个贴近A的回调(003002: 4-6月C/A=1.16~1.36跳过, 7月1.04✓)
                        # 同时跟踪"最新合格回调"更新C区(101课: 中阴状态内震荡买点也可
                        # 看作二买; 002583首C=3.57后价格未回踩, 6月新回调C=3.83→更新)
                        # (2026-08-12收紧: 1.15→1.08 — 胜率分析: 入场/A≥1.15组18%胜率
                        #  vs 1.05-1.10组55%; C/A=1.14的二买实为"8周后新下跌"追高单)
                        first_pb = None
                        last_pb = None
                        if ym.a_price > 0:
                            for _i, _bi in enumerate(bis30):
                                if _bi.direction == 'down' and _bi.start_dt[:10] >= ym_end:
                                    if ym.a_price * 0.90 <= _bi.low <= ym.a_price * 1.08:
                                        if first_pb is None:
                                            first_pb = (_bi, _i)
                                        last_pb = (_bi, _i)
                        if first_pb is not None:
                            pb, pb_i = first_pb
                            # 向下笔结束点=二买(以confirm_at为准, 因果性)
                            sig_dt = pb.confirm_at[:10] if len(pb.confirm_at) >= 10 else pb.end_dt[:10]
                            # 因果: 向下笔必须已确认(confirm_at <= 当前扫描时刻)
                            if sig_dt <= day_str:
                                # P1幽灵候选修复(2026-08-13): 链式活性校验 —
                                # 源一买在完整重建重扫中已不存在 → 一买作废, 不链二买
                                # (实证: 3失败-26.78+1赢家+34.30; 000062源一买存活不受影响)
                                _alive = (ym.a_price <= 0) or _fresh_b1_alive(
                                    symbol, day_str, ym.a_price,
                                    audit_errors=(audit_log['errors'] if audit_trace else None))
                                if not _alive:
                                    if audit_trace and (ym.buy_type, ym.signal_date) not in dead:
                                        _cand_event(ym, bar.dt.strftime('%Y-%m-%d %H:%M:%S'),
                                                    'invalidated', 'invalidated',
                                                    'fresh rebuild no longer contains source first-buy')
                                    dead.add((ym.buy_type, ym.signal_date))
                                key2 = ('二买', sig_dt)
                                if _alive and key2 not in consumed and key2 not in dead:
                                    exist = next((c for c in candidates
                                                  if c.buy_type == '二买' and c.signal_date == sig_dt), None)
                                    if exist is None:
                                        bounce = bis30[pb_i - 1] if pb_i > 0 else None
                                        b2 = BuyCandidate(
                                            buy_type='二买', signal_date=sig_dt,
                                            window_end=(datetime.strptime(sig_dt, "%Y-%m-%d") + timedelta(weeks=SIGNAL_WINDOW_WEEKS)).strftime("%Y-%m-%d"),
                                            l2=ym.l2, a_price=ym.a_price, a_date=ym.a_date,
                                            b_price=(bounce.high if bounce else pb.low),
                                            b_date=(bounce.end_dt[:10] if bounce else sig_dt),
                                            c_price=pb.low, c_date=pb.end_dt[:10],
                                            div_type=ym.div_type)   # P1-1(2026-08-13): 传承一买源类型
                                        b2._source_b1_slot = (ym.buy_type, ym.signal_date)
                                        b2._source_b1_rank = _b1_source_rank(ym)
                                        candidates.append(b2)
                                        if audit_trace:
                                            _register_cand(
                                                b2, bar.dt.strftime('%Y-%m-%d %H:%M:%S'), source=ym,
                                                occur_at=pb.end_dt,
                                                confirm_at=(pb.confirm_at or pb.end_dt))
                                            _cand_event(b2, bar.dt.strftime('%Y-%m-%d %H:%M:%S'),
                                                        'activated', 'active',
                                                        'source first-buy plus confirmed 30-minute down pen')
                                    else:
                                        # 更新C区(101课震荡买点)
                                        # (2026-08-12修复, 胜率分析: 原"最新合格回调覆盖"
                                        #  把正宗二买漂移到追高单 — 000021 C:12.58→14.25,
                                        #  000004 C:9.23→9.86; 只允许向下更新=更贴近A)
                                        if last_pb is not None and last_pb[0].low < exist.c_price:
                                            old_c = float(exist.c_price)
                                            exist.c_price = last_pb[0].low
                                            exist.c_date = last_pb[0].end_dt[:10]
                                            if audit_trace:
                                                cid = _register_cand(exist, bar.dt.strftime('%Y-%m-%d %H:%M:%S'), source=ym)
                                                audit_cands[cid]['abc']['C'] = {
                                                    'at': exist.c_date, 'price': float(exist.c_price)}
                                                _cand_event(exist, bar.dt.strftime('%Y-%m-%d %H:%M:%S'),
                                                            'updated', 'active', 'lower C-zone update',
                                                            {'old_C': old_c, 'new_C': float(exist.c_price)})

        # ── 结构破坏跟踪 ──
        for c in candidates:
            key = (c.buy_type, c.signal_date)
            if key in dead or day_str < c.signal_date[:10]:
                continue
            if c.buy_type == '一买' and c.a_price > 0:
                if bar.low < c.a_price * 0.995:
                    if audit_trace and key not in dead:
                        _cand_event(c, bar.dt.strftime('%Y-%m-%d %H:%M:%S'),
                                    'invalidated', 'invalidated', 'bar low broke A*0.995',
                                    {'bar_low': float(bar.low), 'threshold': float(c.a_price * .995)})
                    dead.add(key)
            elif c.buy_type == '二买' and c.a_price > 0:
                if bar.low < c.a_price * 0.995:
                    if audit_trace and key not in dead:
                        _cand_event(c, bar.dt.strftime('%Y-%m-%d %H:%M:%S'),
                                    'invalidated', 'invalidated', 'bar low broke source A*0.995',
                                    {'bar_low': float(bar.low), 'threshold': float(c.a_price * .995)})
                    dead.add(key)
            elif c.buy_type == '三买':
                zg = c.zs_zg if c.zs_zg > 0 else (c.l2.zg if c.l2 else 0)
                if zg <= 0:
                    continue
                eff_start = max(c.signal_date[:10], c.zs_end_date[:10]) if c.zs_end_date else c.signal_date[:10]
                if day_str < eff_start:
                    continue
                ph = leave_phase.get(key)
                if ph is None:
                    ph = 'wait_leave'
                    leave_phase[key] = ph
                if ph == 'wait_leave':
                    if bar.high > zg:
                        leave_phase[key] = 'pullback'
                else:
                    if bar.low < zg * 0.98:
                        if audit_trace and key not in dead:
                            _cand_event(c, bar.dt.strftime('%Y-%m-%d %H:%M:%S'),
                                        'invalidated', 'invalidated', 'pullback broke ZG*0.98',
                                        {'bar_low': float(bar.low), 'threshold': float(zg * .98)})
                        dead.add(key)
                    # (2026-08-13 r6: 曾加P0-2"第一次回试完成→候选作废"追踪 —
                    #  杀3笔失败单-9.8%但误杀2笔盈利单+19.5%, 净-9.7pt, 已回滚)

        # ── 退出(收盘判断, t+1成交) ──
        # 卖点体系(2026-08-11 P0课本对齐版, 101课/16课/89课/2007-11-20二卖专文):
        #   ① 30min一卖(顶背驰)/二卖(不创新高): 16课短差程序"次级别卖点退出" — 优先
        #   ② 日K一卖(背驰): 101课"一卖=背驰点" — 末笔down时检查前一支up笔(P0修复)
        #   ③ 日K三卖(破中枢ZD): 101课"三卖=中枢破坏点" — 保留简化版
        #   ④ 破A预警+次级别回抽确认: 89课"被中阴震出"是错的 — 破A不立即卖,
        #      等30min反弹确认(顶背驰/不创新高→卖, 收复A→解除), 10天新低→卖
        #   ⑤ 破三买ZG: 三买失败(29课回抽不回) — 保留
        #   ⑥ 止损10%兜底(工程保险, 防单边暴跌)
        # (旧"破A×0.995直接止损"已删除 — 000012#1差0.006触发被甩下+33%)
        if pos is not None:
            pnl = (cur - pos['entry_price']) / pos['entry_price']
            exit_sig = None

            # 持仓期30min高点更新(二卖"不创新高"判定基准)
            # P0-2性能(2026-08-12, 审查H1): 滚动max替代全持仓窗重扫 — O(1), 语义等价
            # (原 seg_hi=max(b.high for b in bars[entry_idx:idx+1]) 长持仓O(n²), 000592 760万次)
            pos['peak30'] = max(pos.get('peak30', 0), bar.high)

            # ① 30min一卖/二卖(16课短差: 次级别卖点退出, 新30min笔确认时检查)
            # P0修正: 仅浮盈>=3%时触发(16课"减仓"的全仓近似=止盈型高抛);
            # 浮亏/微利时30min卖点禁用(避免震荡中被反复割, 等日K结构卖点)
            if USE_30M_SWING and pnl > 0.03 and (idx - pos['entry_idx']) > 60 \
                    and len(m30_cursor.bis) > pos.get('m30_bi_n', 0):
                pos['m30_bi_n'] = len(m30_cursor.bis)
                if m30_cursor.bis and m30_cursor.bis[-1].direction == 'up':
                    last30 = m30_cursor.bis[-1]
                    ok30, note30 = _m30_top_divergence(
                        m30_cursor.bis, bars, diff, dea, idx, m30_idx)
                    if ok30:
                        exit_sig = f"一卖(30min背驰) {note30}"
                    else:
                        # 二卖: 30min向上笔终点不创新高(相对持仓期高点)
                        if last30.high < pos.get('peak30', 0) * 0.99:
                            exit_sig = (f"二卖(30min不创新高 "
                                        f"{last30.high:.2f}<{pos.get('peak30', 0):.2f})")
            # ② 日K一卖: 新日K笔确认时检查(末笔up检查末笔; 末笔down检查前一支up笔 — P0)
            if not exit_sig and (idx - pos['entry_idx']) > 160 \
                    and len(d_cursor.bis) > pos.get('d_bi_n', 0):
                pos['d_bi_n'] = len(d_cursor.bis)
                if d_cursor.bis:
                    tgt = d_cursor.bis[-1] if d_cursor.bis[-1].direction == 'up' else (
                        d_cursor.bis[-2] if len(d_cursor.bis) >= 2
                        and d_cursor.bis[-2].direction == 'up' else None)
                    if tgt is not None:
                        # 一卖: 长段(>=12根)面积背驰(101课"一卖=背驰点")
                        ok, note = _daily_top_divergence(
                            d_cursor.bis, bars_d, diff_d, dea_d, d_idx, target=tgt)
                        if ok:
                            exit_sig = f"一卖(日K背驰) {note}"
                        else:
                            # 二卖(2007-11-20专文): 向上笔结束+笔终点不创新高
                            # "一旦一个向上过程不能创新高或背驰, 都将构成第二类卖点"
                            prev_up = next((b for b in reversed(d_cursor.bis[:-1])
                                            if b.direction == 'up'), None)
                            if prev_up is not None:
                                # P0三轮: 二卖须"前up笔已确认一卖(长段背驰)"+不创新高
                                # (101课: "二卖=一卖后的次级别反抽结束点" — 无卖无二卖!
                                #  000066 8.85<9.49中阴震荡/000062 9.25<9.73中继回调
                                #  均无前置一卖 → 不误杀; 000592 12.46<12.85需12.85
                                #  处确认一卖(短段顶无法确认 → 靠三卖/破位兜底))
                                p_ok, _p_note = _daily_top_divergence(
                                    d_cursor.bis, bars_d, diff_d, dea_d, d_idx,
                                    target=prev_up)
                                if p_ok:
                                    _pe = d_idx[prev_up.end_dt[:10]]   # L1加固
                                    _ce = d_idx[tgt.end_dt[:10]]
                                    if _ce - _pe >= 20 and \
                                            tgt.end_price < prev_up.end_price * 0.98:
                                        exit_sig = (f"二卖(日K不创新高 前高背驰 "
                                                    f"{tgt.end_price:.2f}<{prev_up.end_price:.2f})")
            # ③ 日K三卖: 跌破持仓期最后向上日K中枢ZD(破中枢不回, 简化版)
            # P0-3性能(2026-08-12, 审查H3): 中枢只在新日K笔时重建并缓存ZD,
            # 价格判断保持每bar — 语义等价(中枢仅依赖bis, 笔不变则ZD不变)
            if not exit_sig:
                try:
                    if len(d_cursor.bis) != pos.get('d_zs_bi_n', -1):
                        pos['d_zs_bi_n'] = len(d_cursor.bis)
                        clear_level('D')
                        zs_d = build_zs(d_cursor.bis, level='D')
                        # F1修复(2026-08-13, 审计P0-1): 三卖中枢必须与持仓相关 —
                        # 持仓峰值曾到达该中枢上沿(peak30>=ZG)才允许"跌破ZD"构成三卖;
                        # 否则是无关高位中枢误杀(000050#2/000063等5笔错位卖出的根因)
                        up_zs = [z for z in zs_d if z.sdir == '向上'
                                 and z.end_dt[:10] >= pos['entry_dt'][:10]
                                 and z.zg > z.zd * 1.003
                                 and pos.get('peak30', 0) >= z.zg]
                        pos['d_zs_last_zd'] = up_zs[-1].zd if up_zs else None
                    if pos.get('d_zs_last_zd') is not None \
                            and cur < pos['d_zs_last_zd'] * 0.97:
                        exit_sig = f"三卖(破日K中枢ZD={pos['d_zs_last_zd']:.2f})"
                except Exception as exc:
                    if audit_trace:
                        audit_log['errors'].append({
                            'at': bar.dt.strftime('%Y-%m-%d %H:%M:%S'),
                            'stage': 'daily_third_sell_zhongshu',
                            'error_type': type(exc).__name__, 'message': str(exc),
                            'effect': 'daily_third_sell_predicate=UNKNOWN',
                        })

            # ④ 破A预警+次级别确认(89课"不被震出" + 101课"最弱二买跌破一买完全可以"):
            # 破A×0.995不立即卖 — 观察30min次级别:
            #   - 30min向下笔确认结束 → 最弱二买豁免(101课: 跌破一买构成盘整背驰) → 暂持有
            #   - 30min反弹收复A(收盘) → 假破解除(000012#1型)
            #   - 30min反弹未收复A且顶背驰/不创新高 → 真破卖出(000012#5弱反弹型)
            #   - 10个交易日仍新低无反弹 → 真破卖出(000592#1阴跌型)
            # (2026-08-12修复, 胜率分析建议1): 原"30min向下笔完成=最弱二买"永久豁免
            #  (a_warn='b2'后无任何分支) — 6/9笔破A单被吞掉后阴跌到-10%止损;
            #  101课前提是"跌破后价格转上(盘整背驰成立)", 向下笔只是局部反弹起点;
            #  现改为: b2豁免仅一次, 之后向下延伸创新低即失效, 10bar新低规则继续生效
            if not exit_sig and pos.get('a_low'):
                a = pos['a_low']
                if pos.get('a_warn') is None:
                    if cur < a * 0.995:
                        pos['a_warn'] = {'days': 0, 'low': cur, 'b2': False}
                        pos['a_warn_bi'] = len(m30_cursor.bis)
                elif isinstance(pos['a_warn'], dict):
                    w = pos['a_warn']
                    w['days'] += 1
                    w['low'] = min(w['low'], cur)
                    # 30min新笔检查优先(结构信号强于时间信号)
                    if len(m30_cursor.bis) > pos.get('a_warn_bi', 0):
                        pos['a_warn_bi'] = len(m30_cursor.bis)
                        if m30_cursor.bis and m30_cursor.bis[-1].direction == 'down':
                            if not w.get('b2'):
                                # 首次30min向下笔确认结束 = 最弱二买(101课: 跌破
                                # 一买构成盘整背驰) → 豁免一次(000062 07-25双底型)
                                w['b2'] = True
                            elif cur < w.get('b2_low', w['low']):
                                # b2后向下笔延伸创新低 → 盘整背驰未成立(价格未转上)
                                # → 豁免失效, 重新武装(10bar新低规则继续)
                                w['b2'] = False
                            w['b2_low'] = min(w.get('b2_low', cur), cur)
                        elif m30_cursor.bis and m30_cursor.bis[-1].direction == 'up':
                            last30b = m30_cursor.bis[-1]
                            ok30b, note30b = _m30_top_divergence(
                                m30_cursor.bis, bars, diff, dea, idx, m30_idx)
                            if ok30b or last30b.high < pos.get('peak30', 0) * 0.99:
                                exit_sig = f"破A确认(次级别卖点 A={a:.2f})"
                            else:
                                # 反弹未收复A且无顶背驰 → 回预警(等10bar新低规则)
                                w['b2'] = False
                    elif cur >= a * 0.995:
                        # 收复A(收盘) → 假破, 解除预警(89课: 不被中阴震出)
                        pos['a_warn'] = None
                    elif w['days'] >= 80 and cur <= w['low']:
                        # (2026-08-12修复: 原10根30min bar=1.25个交易日, 与注释
                        #  "10个交易日"不符 — 改为80根=10个交易日; 配合b2失效机制)
                        exit_sig = f"破A确认(10天新低 A={a:.2f})"
            # ④-5 三买盘整高点回撤(21课: "一旦不能出现趋势, 一定要在盘整的高点出掉")
            # S2(2026-08-12): 仅三买(entry_zg非0); 持仓>40根后, 从持仓期高点回落>5%即出 —
            # 近似"不能出现趋势"(不创新高)→盘整高点离场。样本内13笔三买反事实+47.9pt, 3笔亏转盈
            # (2026-08-13 r2回归: 曾尝试扩展至"盘整型二买"(peak30<入场×1.10),
            #  结果000062 +236%→-2.6%等趋势赢家全灭, 总盈亏+429%→+113%, 已回滚)
            if not exit_sig and pos.get('entry_zg') and (idx - pos['entry_idx']) > 40 \
                    and cur < pos.get('peak30', 0) * 0.95:
                exit_sig = f"盘整高点回撤(21课) peak={pos.get('peak30', 0):.2f}"
            # ⑤ 破三买ZG(29课: 三买失败=回抽破中枢; S2收紧0.97→0.99: 快死单提前离场)
            if not exit_sig and pos.get('entry_zg') and cur < pos['entry_zg'] * 0.99:
                exit_sig = f"结构止损(ZG={pos['entry_zg']:.2f})"
            # ⑥ 兜底(工程保险)
            if not exit_sig and pnl < -0.10:
                exit_sig = "止损10%"

            if exit_sig:
                # 30min一卖/二卖退出 → 进入短差回补观察(16课); 结构退出不回补
                if exit_sig.startswith(('一卖(30min', '二卖(30min')):
                    reentry = {'bi_n': len(m30_cursor.bis), 'rounds': 0,
                               'a_low': pos.get('a_low'), 'born': idx,
                               'exit_price': cur}
                pending = {'kind': 'exit', 'reason': exit_sig,
                           'entry_price': pos['entry_price'],
                           'entry_idx': pos['entry_idx'],
                           'entry_dt': pos['entry_dt'],
                           'name': pos['name'],
                           'entry_reason': pos.get('entry_reason', ''),
                           'div_type': pos.get('div_type', '')}
                if audit_trace:
                    decision_at = bar.dt.strftime('%Y-%m-%d %H:%M:%S')
                    exit_predicates = _exit_predicates(pos, bar, idx, pnl, exit_sig)
                    pending['_audit'] = pos.get('_audit')
                    pending['exit_decision_at'] = decision_at
                    pending['exit_predicates'] = exit_predicates
                    audit_log['decision_events'].append({
                        'at': decision_at, 'kind': 'exit',
                        'trade_id': (pos.get('_audit') or {}).get('trade_id'),
                        **exit_predicates,
                    })
                continue

        # ── P0: 短差回补(16课: 次级别一卖退出后, 次级别一买回补) ──
        if USE_30M_SWING and pos is None and reentry is not None and reentry['rounds'] < 3                 and idx - last_exit_idx > 40:
            # P0修正: 回补需回落>=3%(低吸, 避免追回); 未回落不回补
            if cur < reentry.get('exit_price', 0) * 0.97:
                if len(m30_cursor.bis) > reentry.get('bi_n', 0):
                    reentry['bi_n'] = len(m30_cursor.bis)
                    if m30_cursor.bis and m30_cursor.bis[-1].direction == 'down':
                        ok_r, note_r = _m30_bottom_divergence(
                            m30_cursor.bis, bars, diff, dea, idx, m30_idx)
                        if ok_r:
                            reentry['rounds'] += 1
                            reentry['pending'] = {'a_low': reentry.get('a_low')}
            elif cur > reentry.get('exit_price', 0) * 1.05:
                reentry = None   # 涨回5%以上未回补 → 放弃短差
        if reentry is not None and idx - reentry.get('born', idx) > 480:
            reentry = None   # 60个交易日无回补 → 放弃观察

        # ── 入场(收盘判断, t+1成交) ──
        if pos is None and idx - last_exit_idx > ENTRY_GAP:
            if 'ST' in symbol or '退' in symbol:
                continue
            active = []
            for c in candidates:
                key = (c.buy_type, c.signal_date)
                if key in consumed or key in dead:
                    continue
                if c.buy_type == '三买' and c.zs_end_date:
                    start = max(c.signal_date[:10], c.zs_end_date[:10])
                else:
                    start = c.signal_date[:10]
                if start <= day_str[:10] <= c.window_end[:10]:
                    active.append(c)
            if not active:
                continue

            ym_cands = [c for c in active if c.buy_type == '一买']
            if ym_cands:
                latest = max(ym_cands, key=lambda c: c.signal_date)
                active = [c for c in active if c.buy_type != '一买' or c is latest]

            priority = {'三买': 0, '二买': 1, '一买': 2}
            active.sort(key=lambda x: priority.get(x.buy_type, 9))
            sub_bars = bars[:idx+1]
            fired = False

            for bc in active:
                if bc.buy_type == '三买':
                    zg = bc.zs_zg if bc.zs_zg > 0 else (bc.l2.zg if bc.l2 else 0)
                    if zg <= 0:
                        continue
                    key = (bc.buy_type, bc.signal_date)
                    if leave_phase.get(key) != 'pullback':
                        continue
                    in_zg_zone = zg < cur <= zg * 1.05
                    if in_zg_zone:
                        if key not in in_zone or in_zone[key] is None:
                            in_zone[key] = idx
                    else:
                        in_zone[key] = None
                    if not in_zg_zone:
                        continue
                    df_idx = _find_bottom_fractal_idx(sub_bars)
                    if df_idx < len(sub_bars) - 6:
                        continue
                    df_low = sub_bars[df_idx].low
                    if not (zg * 0.98 <= df_low <= zg * 1.05):
                        continue
                    if bar.low < df_low:
                        continue
                    if not _divergence_at_fractal(sub_bars, diff[:idx+1], dea[:idx+1], df_idx):
                        continue
                    fired = True; pname = '三买'
                    entry_zg = zg; entry_a = 0; entry_b = 0

                elif bc.buy_type == '一买':
                    # S1(2026-08-12, 课本对齐): 一买不直接入场(101课: 一买处中阴状态,
                    # "跌破一买完全可以"; 16课操作级买点=次级别回抽结束点=二买)。
                    # 一买候选生成/结构破坏跟踪/二买Phase 4b均保留(二买链依赖),
                    # 仅关闭一买开仓分支 — 一买后等二买(次级别回抽)再介入。
                    continue
                    if bc.a_price <= 0:
                        continue
                    if bars_d is None or diff_d is None:
                        continue
                    d_ok, d_note, d_c_start, d_c_low = _daily_c_divergence(
                        bars_d, diff_d, dea_d, bar.dt, d_cursor.bis, d_idx)
                    if not d_ok:
                        continue
                    ref_a = d_c_low if d_c_low > 0 else bc.a_price
                    if cur < ref_a * 0.95 or cur > ref_a * 1.08:
                        continue
                    key = (bc.buy_type, bc.signal_date)
                    if key not in m30_ok:
                        if idx - m30_last_check.get(key, -999) < 40:
                            continue
                        m30_last_check[key] = idx
                        m_ok, m_note = _m30_trend_divergence(
                            m30_cursor.bis, bars, diff, dea, d_c_start, idx, m30_idx)
                        if not m_ok:
                            continue
                        m30_ok.add(key)
                    df_idx = _find_bottom_fractal_idx(sub_bars)
                    if df_idx < len(sub_bars) - 6:
                        continue
                    if bar.low < sub_bars[df_idx].low:
                        continue
                    if not _is_recovering(sub_bars):
                        continue
                    if not _divergence_at_fractal(sub_bars, diff[:idx+1], dea[:idx+1], df_idx):
                        continue
                    fired = True; pname = '一买'
                    entry_zg = 0; entry_a = ref_a; entry_b = 0

                elif bc.buy_type == '二买':
                    if bc.a_price <= 0:
                        continue
                    key = (bc.buy_type, bc.signal_date)
                    A = bc.a_price
                    C = bc.c_price or A
                    # (2026-08-12, 胜率分析建议2③): 入场执行价≤A×1.10 —
                    # 入场/A≥1.10组胜率明显低于1.05-1.10组(追高单7笔止损全部≥1.10)
                    # P1-3双条件(2026-08-13, 选项1): 盘整背驰源二买收紧至≤A×1.05
                    # (27课弱前提+追高双重弱; 审计: 盘整背驰源失败单入场比1.078/1.094);
                    # 趋势背驰源保持1.10(000062入场/A=1.09是最大赢家, 不伤)
                    _cap = 1.05 if (bc.div_type or '') == '盘整背驰' else 1.10
                    if cur > A * _cap:
                        continue
                    in_a_zone = C * 0.98 <= cur <= C * 1.05
                    if in_a_zone:
                        if key not in in_zone or in_zone[key] is None:
                            in_zone[key] = idx
                    else:
                        in_zone[key] = None
                    if not in_a_zone:
                        continue
                    df_idx = _find_bottom_fractal_idx(sub_bars)
                    if df_idx < len(sub_bars) - 6:
                        continue
                    df_low = sub_bars[df_idx].low
                    if not (C * 0.98 <= df_low <= C * 1.05):
                        continue
                    if bar.low < df_low:
                        continue
                    if not _divergence_at_fractal(sub_bars, diff[:idx+1], dea[:idx+1], df_idx):
                        continue
                    fired = True; pname = '二买'
                    entry_zg = 0; entry_a = A; entry_b = bc.b_price

                if fired:
                    consumed.add((bc.buy_type, bc.signal_date))
                    in_zone.pop((bc.buy_type, bc.signal_date), None)
                    if audit_trace:
                        _cand_event(bc, bar.dt.strftime('%Y-%m-%d %H:%M:%S'),
                                    'consumed', 'consumed', f'{pname} entry predicates passed')
                    break

            if fired:
                pending = {'kind': 'entry', 'pname': pname,
                           'entry_a': entry_a, 'entry_b': entry_b,
                           'entry_zg': entry_zg,
                           'div_type': bc.div_type or ''}
                if audit_trace:
                    decision_at = bar.dt.strftime('%Y-%m-%d %H:%M:%S')
                    adopted_id = _register_cand(bc, decision_at)
                    snapshots = [_decision_candidate(x, bar, idx, sub_bars, diff, dea)
                                 for x in active]
                    for snap in snapshots:
                        snap['actual_reason'] = (
                            f'adopted by priority and all engine predicates passed ({pname})'
                            if snap['candidate_id'] == adopted_id else
                            'not adopted at this timestamp')
                    structure = {
                        'candidate': audit_cands[adopted_id]['structure'],
                        'daily_bis': [_audit_bi(x, 'D') for x in d_cursor.bis],
                        'm30_bis': [_audit_bi(x, '30m') for x in m30_cursor.bis],
                        'daily_bi_member_ids': [_audit_bi(x, 'D')['bi_id'] for x in d_cursor.bis],
                        'm30_bi_member_ids': [_audit_bi(x, '30m')['bi_id'] for x in m30_cursor.bis],
                    }
                    pending.update({
                        'candidate_id': adopted_id, 'decision_at': decision_at,
                        'decision_close': float(cur), 'entry_predicates': snapshots,
                        'structure': structure,
                    })
                    audit_log['decision_events'].append({
                        'at': decision_at, 'kind': 'entry', 'candidate_id': adopted_id,
                        'adopted_reason': f'{pname} passed and won engine priority',
                        'all_candidate_predicates': snapshots,
                    })

    if pos:
        last_bar = bars[-1]
        pnl = (last_bar.close - pos['entry_price']) / pos['entry_price']
        trade = {
            'symbol': symbol, 'name': pos['name'],
            'entry': pos['entry_dt'][:10],
            'exit': last_bar.dt.strftime("%Y-%m-%d"),
            'entry_price': pos['entry_price'], 'exit_price': last_bar.close,
            'pnl_pct': round(pnl * 100, 2),
            'hold': len(bars) - 1 - pos['entry_idx'],
            'reason': '回测结束', 'entry_reason': pos.get('entry_reason', ''),
            'div_type': pos.get('div_type', ''),
        }
        if audit_trace:
            ta = dict(pos.get('_audit') or {})
            ta['exit_decision_at'] = None
            ta['exit_fill_at'] = None
            ta['censor_at'] = last_bar.dt.strftime('%Y-%m-%d %H:%M:%S')
            ta['exit_predicates'] = {'status': 'CENSORED',
                                     'reason': 'position valued at final close; no exit order/fill'}
            ta.setdefault('full_precision', {})['censor_price'] = float(last_bar.close)
            ta['full_precision']['pnl_ratio'] = float(pnl)
            trade['audit'] = ta
        trades.append(trade)

    candidate_rows = [{'type': c.buy_type, 'signal': c.signal_date[:10],
                       'a': round(c.a_price, 2) if c.a_price else 0,
                       'c': round(c.c_price, 2) if c.c_price else 0,
                       'zg': round(c.zs_zg, 2) if c.zs_zg else 0}
                      for c in candidates]
    result = {'symbol': symbol, 'trades': trades, 'candidates': candidate_rows}
    if audit_trace:
        for c, row in zip(candidates, candidate_rows):
            cid = _register_cand(c, None)
            row['audit'] = audit_cands[cid]
        audit_log['candidates'] = list(audit_cands.values())
        date_only_bis = [
            _audit_bi(x, '30m')['bi_id'] for x in m30_cursor.bis
            if len(str(x.start_dt)) < 19 or len(str(x.end_dt)) < 19
        ]
        audit_log['risk_flags'] = ([{
            'code': 'M30_BI_TIMESTAMP_DATE_ONLY', 'status': 'FAIL',
            'message': ('30-minute pen endpoints do not retain full datetime; '
                        'same-day bar identity cannot be proven from pen fields'),
            'affected_bi_ids': date_only_bis,
        }] if date_only_bis else [])
        result['audit_trace'] = audit_log
    return result

def run(symbols, sdt="20260101", edt="20260804"):
    results = defaultdict(list)
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    for i, sym in enumerate(symbols):
        clear_all(); clear_daily_cache(); clear_30min_cache()
        try:
            r = backtest_one(sym, sdt, edt, conn=conn)
            for t in r['trades']:
                results[t['name']].append(t)
        except Exception as e:
            print(f'  ERR {sym}: {e}')
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(symbols)}] {sum(len(v) for v in results.values())} trades ({time.time()-t0:.0f}s)", flush=True)
    conn.close()
    return dict(results)


def summary(results: dict, tag="日K买点+30min介入"):
    print(f"\n{'='*60}")
    print(f"  {tag} 回测")
    for name in ['一买', '二买', '三买']:
        ts = results.get(name, [])
        if not ts:
            print(f"  {name}: 0"); continue
        pnls = [t['pnl_pct'] for t in ts]
        wins = [p for p in pnls if p > 0]; loss = [p for p in pnls if p <= 0]
        wr = len(wins)/len(pnls)*100; avg = sum(pnls)/len(pnls)
        aw = sum(wins)/len(wins) if wins else 0
        al = sum(loss)/len(loss) if loss else 0
        holds = [t['hold'] for t in ts]
        print(f"  {name}: {len(ts)}笔 {wr:.0f}% avg={avg:+.2f}% "
              f"赢+{aw:.1f}% 输{al:.1f}% hold={sum(holds)/len(holds):.0f}bar")
    total = sum(len(ts) for ts in results.values())
    all_p = [t['pnl_pct'] for ts in results.values() for t in ts]
    if all_p:
        w = [p for p in all_p if p > 0]
        print(f"  Total: {total}笔 {len(w)/len(all_p)*100:.0f}% avg={sum(all_p)/len(all_p):+.2f}%")
    print(f"{'='*60}")


if __name__ == '__main__':
    import json
    symbols = sys.argv[1:] or ['000537.SZ', '000503.SZ', '000423.SZ', '000539.SZ', '000591.SZ']
    print(f"回测: {len(symbols)}只 | 日K买点 + 30min介入 | 2026-01-01~08-04")
    results = run(symbols, sdt='20260101', edt='20260804')
    summary(results)
    print(f"\n── 明细 ──")
    for name in ['一买','二买','三买']:
        for t in sorted(results.get(name,[]), key=lambda t: t['pnl_pct'], reverse=True):
            print(f"  {t['symbol']} {name} {t['entry']}->{t['exit']} {t['pnl_pct']:+.1f}% {t['reason']}")
