#!/usr/bin/env python3
"""日线级别买点扫描 — 基础设施模块(原 backtest_daily.py 提取)。

日K扫描逻辑(scan_daily)被 日K+30min 引擎 和 画图脚本 共用:
  - load_daily:        日K数据加载(缓存)
  - clear_daily_cache: 清缓存
  - scan_daily:        日线 L1笔中枢 买点扫描
      * 一买: 两个连续向下日线笔中枢 + ZG下移 + 背驰点创新低 + MACD面积背驰
      * 三买: 向上日线笔中枢 + 离开ZG + 回试不破ZG
      * 二买: 不在日K层生成(课本p37: 二买=一买后第一次【次级别】回调,
              操作级别=日K时次级别=30min, 由30min介入层确认)
  前视偏差修复(2026-08-06): edt截断——扫描只能用edt之前的数据。
"""

import sqlite3
from datetime import datetime

from chan.bars import RawBar, macd, macd_area
from chan.bi import build_bi
from chan.zhongshu import build_zs
from chan.state import clear_level
from chan.scanner_60min import BuyCandidate, _add_weeks

DB_PATH = '/root/data/backtest/kline.db'

_DAILY_CACHE = {}


def load_daily(symbol: str, conn=None):
    """加载日线K线(全量历史, K线看整体)"""
    if symbol in _DAILY_CACHE:
        return _DAILY_CACHE[symbol]
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        own = True
    rows = conn.execute(
        "SELECT code, date, open, high, low, close, vol, amount "
        "FROM daily_kline WHERE code=? ORDER BY date", (symbol,)).fetchall()
    bars = []
    for r in rows:
        try:
            dt = datetime.strptime(r[1], "%Y%m%d")
        except Exception:
            continue
        bars.append(RawBar(symbol=r[0], dt=dt, open=float(r[2]), high=float(r[3]),
                           low=float(r[4]), close=float(r[5]),
                           vol=float(r[6] or 0), amount=float(r[7] or 0)))
    if own:
        conn.close()
    _DAILY_CACHE[symbol] = bars
    return bars


def clear_daily_cache():
    _DAILY_CACHE.clear()


def scan_daily_on_bis(bis, bars, diff, dea, d_idx=None):
    """在给定笔序列上扫描候选(Phase R阶段5: 用增量cursor的bis, 不重算笔)。
    bis/bars/diff/dea 必须来自同一时刻的可用数据(调用方保证因果性)。
    返回: BuyCandidate列表(一买/三买, sig=背驰点确认日, window_end已算)。"""
    if len(bars) < 200:
        return []

    clear_level('D')
    if len(bis) < 10:
        return []

    clear_level('D')
    all_zs_L1 = build_zs(bis, level='D')
    if len(all_zs_L1) < 3:
        return []

    # 窄中枢过滤(2026-08-06): 区间宽度 < 0.3% 的中枢是"多笔共同重叠"的数学产物
    # (如 [20.97,20.99]=0.1%), 操作意义≈0; 但 0.5%~1.5% 的窄中枢在低价股/低波动股
    # 上是真实窄幅整理(000537 一买中枢对 0.5%/1.4%), 不能一刀切滤掉
    zs_L1 = [z for z in all_zs_L1 if z.zg > z.zd * 1.003]
    if len(zs_L1) < 3:
        return []

    # L2(周线级)背景方向(Phase4, 49课"没必要参与操作级别及以上级别的下跌"):
    # 日K操作级别的上级=周线级=日K-L2中枢(3个日K走势重叠)。
    # 最近L2方向作为候选的 l2_dir 标注(与60min引擎 weekly_filter 同思路,
    # 取edt时点状态; L2方向数月不变, 历史近似可接受)
    l2_dir = ''
    try:
        from chan.scanner_60min import build_trends, build_l2_zs
        _trends = build_trends(zs_L1)
        if len(_trends) >= 3:
            _l2 = build_l2_zs(_trends)
            if _l2:
                l2_dir = _l2[-1].sdir  # 最近L2中枢方向
    except Exception:
        pass


    if d_idx is None:
        d_idx = {}
        for _i, _b in enumerate(bars):
            d_idx.setdefault(_b.dt.strftime('%Y-%m-%d'), _i)

    def _bar_idx(dt_str):
        return d_idx.get(dt_str[:10], len(bars) - 1)

    candidates = []
    b1_by_signal = {}

    # ── 一买: 相邻向下中枢对 + ZG下移 + 平均力度背驰(15/24/37课) ──
    # (2026-08-07 课本修正:
    #  ① 中枢对: 相邻向下中枢(中间可隔向上中枢, 20课"逐级下移"的大级别下跌)
    #  ② 判据: b.zg<a.zd(ZG下移) + 平均力度背驰(15课"趋势平均力度"=面积/时间)
    #  ③ 两类候选:
    #     - 趋势背驰: c段创新低(c_low<b.dd, 37课) → A=c段低点(当下背驰点)
    #     - 盘整背驰: c段未创新低(27课, 低位由入场层把关) → A=b.dd(中枢内低点,
    #       时点稳定不漂移; 000423二买依赖此信号)
    #  ④ sig=背驰点日(101课"一买就是背驰点"), 窗口52周(旧信号复活源))
    downs = [z for z in zs_L1 if z.sdir == '向下']
    # 候选可过滤无操作意义的窄中枢，但结构边界必须使用完整中枢序列；
    # 否则旧C段仍可能跨过一个被过滤的窄中枢，重新绑定后续低点。
    all_zs_pos = {id(z): i for i, z in enumerate(all_zs_L1)}
    for i in range(len(downs) - 1):
        a, b = downs[i], downs[i + 1]
        if b.zg >= a.zd:
            continue   # ZG未下移(20课中心定理二: 需逐级下移)
        # A段: a.start前60根摆动高点 → a.start
        a_s0 = _bar_idx(a.start_dt)
        hi = a_s0
        for k in range(a_s0 - 1, max(0, a_s0 - 60), -1):
            if bars[k].high > bars[hi].high:
                hi = k
        if a_s0 - hi < 5:
            continue
        area_A = macd_area(diff, dea, hi, a_s0, 'down')
        # C段: b.end之后离开b中枢的段(到c低点)。若其后已经形成新的同级
        # 中枢，C段必须在新中枢开始前截止；不能让历史b中枢跨过后续中枢，
        # 重新绑定到多年后的新低点。
        b_s = _bar_idx(b.start_dt)
        b_e = _bar_idx(b.end_dt)
        b_zs_i = all_zs_pos[id(b)]
        if b_zs_i + 1 < len(all_zs_L1):
            next_zs_start = _bar_idx(all_zs_L1[b_zs_i + 1].start_dt)
            c_sub = bars[b_e + 1:max(b_e + 1, next_zs_start)]
        else:
            c_sub = bars[b_e + 1:]
        if not c_sub:
            continue
        min_bar = min(c_sub, key=lambda x: x.low)
        c_low = min_bar.low
        c_low_i = b_e + 1 + c_sub.index(min_bar)
        area_C = macd_area(diff, dea, b_e, c_low_i, 'down')
        len_A = max(a_s0 - hi, 1); len_C = max(c_low_i - b_e, 1)
        if area_A < 0 and area_C < 0 and \
                abs(area_C) / len_C < abs(area_A) / len_A * 0.9:
            if c_low < b.dd * 0.999:
                # 趋势背驰: A=c段低点(当下背驰点)
                div_type = '趋势背驰'
                a_low = min_bar.low
                a_date = min_bar.dt.strftime("%Y-%m-%d")
            else:
                # 盘整背驰: A=b中枢内低点(时点稳定)
                div_type = '盘整背驰'
                b_sub = bars[b_s:b_e + 1]
                min_b = min(b_sub, key=lambda x: x.low) if b_sub else min_bar
                a_low = min_b.low
                a_date = min_b.dt.strftime("%Y-%m-%d")
            # sig=背驰点日(101课), 窗口52周
            sig = a_date
            candidate = BuyCandidate(
                buy_type='一买', signal_date=sig,
                window_end=_add_weeks(sig, 52),
                l2=b, a_price=a_low, a_date=a_date, div_type=div_type,
                weekly_dir=l2_dir)
            # 同一背驰点若仍被多个结构解释，只保留结束时间最近的中枢来源。
            # 外层交易引擎仍按(买点类型, signal_date)消费，避免同一买点重复交易。
            previous = b1_by_signal.get(sig)
            if previous is None or (b.end_dt, b.start_dt) > (
                    previous.l2.end_dt, previous.l2.start_dt):
                b1_by_signal[sig] = candidate

    candidates.extend(b1_by_signal.values())

    # ── 三买: 向上日线笔中枢(L1) + 离开ZG + 回试不破ZG ──
    for l2 in zs_L1:
        if l2.sdir != '向上':
            continue
        end_idx = _bar_idx(l2.end_dt)
        post = bars[end_idx:]
        if len(post) < 30:
            continue
        leave_idx = None
        for k, b in enumerate(post):
            if b.high > l2.zg:
                leave_idx = k
                break
        if leave_idx is None:
            continue
        after_leave = post[leave_idx:]
        lookback = min(120, len(after_leave))
        pb_low = min(b.low for b in after_leave[:lookback])
        cur = after_leave[-1].close
        if pb_low > l2.zg and cur > l2.zg:
            # L1笔中枢无trends引用, 信号日 = 中枢完成日(离开回试起点)
            sig_dt = l2.end_dt[:10]
            candidates.append(BuyCandidate(
                buy_type='三买', signal_date=sig_dt,
                window_end=_add_weeks(sig_dt, 26),
                l2=l2, zs_zg=l2.zg, zs_zd=l2.zd,
                zs_end_date=l2.end_dt[:10] if len(l2.end_dt) >= 10 else l2.end_dt,
                weekly_dir=l2_dir))

    # 二买不在日K层生成(2026-08-06 课本修正):
    # 课本 p37 "二买 = 一买后第一次【次级别】回调低点"
    # 操作级别=日K时, 次级别=30min → 二买由30min介入层确认
    # (日K层生成二买会产生同回调多一买复用 → 重复信号)

    return candidates


def scan_daily_on_bars(bars):
    """在给定日K bars上扫描候选(全量重算笔; 动态生成请用scan_daily_on_bis+cursor)。"""
    if len(bars) < 200:
        return []
    clear_level('D')
    bis = build_bi(bars, level='D')
    closes = [b.close for b in bars]
    diff, dea, _ = macd(closes)
    return scan_daily_on_bis(bis, bars, diff, dea)



def scan_daily(symbol: str, edt: str = "20260804", conn=None) -> list:
    """日线级别 L1中枢 买点扫描(包装器: 加载+截断 → scan_daily_on_bars)。
    Phase R阶段4: 回测中动态生成请用 scan_daily_on_bars(可用日K)。"""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        own = True
    bars = load_daily(symbol, conn=conn)
    if edt:
        edt_dt = datetime.strptime(edt, "%Y%m%d")
        bars = [b for b in bars if b.dt <= edt_dt]
    cands = scan_daily_on_bars(bars)
    if own:
        conn.close()
    return cands
