#!/usr/bin/env python3
"""次级别(60分钟)确认模块 — 日线定方向,60分钟定精确入场。

管线: 日线信号 → 加载60min数据 → 60min笔/中枢 → 次级别确认

⚠️ 算法文档: /root/data/backtest/ALGORITHM.md (第3.2节)
   修改影响算法时必须同步更新 ALGORITHM.md。
"""

import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional, Dict
from chan.bars import RawBar, macd, macd_area
from chan.bi import build_bi
from chan.zhongshu import build_zs

DB_PATH = '/root/data/backtest/kline.db'

# 60min 数据缓存 (per session)
_60MIN_CACHE: Dict[str, List[RawBar]] = {}
# 60min 确认结果缓存 (symbol+date → result), 避免同一股票同日多次调用
_CONFIRM_CACHE: Dict[str, tuple] = {}


def load_60min(symbol: str, conn=None) -> List[RawBar]:
    """加载股票的60分钟K线数据,带会话缓存。"""
    if symbol in _60MIN_CACHE:
        return _60MIN_CACHE[symbol]

    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        own = True

    rows = conn.execute(
        "SELECT code, datetime, open, high, low, close, vol, amount "
        "FROM kline_60min WHERE code=? ORDER BY datetime",
        (symbol,)
    ).fetchall()

    if own:
        conn.close()

    if not rows:
        _60MIN_CACHE[symbol] = []
        return []

    bars = []
    for i, r in enumerate(rows):
        try:
            dt = datetime.strptime(r[1], "%Y-%m-%d %H:%M:%S")
        except (ValueError, IndexError):
            continue  # 跳过损坏的日期行
        try:
            o, h, l, c = float(r[2] or 0), float(r[3] or 0), float(r[4] or 0), float(r[5] or 0)
            v, a = float(r[6] or 0), float(r[7] or 0)
        except (ValueError, IndexError):
            continue
        bars.append(RawBar(
            symbol=r[0], id=len(bars), dt=dt,
            open=o, high=h, low=l, close=c, vol=v, amount=a,
        ))

    _60MIN_CACHE[symbol] = bars
    return bars


def _bars_near(bars: List[RawBar], target_date: str, window_before: int = 500,
               window_after: int = 100) -> List[RawBar]:
    """截取目标日期附近的bars。window_before/after 是 bar 数量。"""
    target_dt = datetime.strptime(target_date[:10], "%Y-%m-%d")
    start_dt = target_dt - timedelta(days=window_before * 0.3)  # rough
    end_dt = target_dt + timedelta(days=window_after * 0.3 + 10)

    result = []
    for b in bars:
        if start_dt <= b.dt <= end_dt:
            result.append(b)
    # 限制数量
    if len(result) > window_before + window_after:
        # 找到 target_date 的 index
        t_idx = next((i for i, b in enumerate(result) if b.dt >= target_dt), len(result) // 2)
        start = max(0, t_idx - window_before)
        end = min(len(result), t_idx + window_after)
        result = result[start:end]
    return result


def _find_60min_di_fen(bars: List[RawBar]) -> Optional[int]:
    """在60min bars末尾找最近的底分型,返回底分型中间bar的index。"""
    if len(bars) < 3:
        return None
    # 从末尾往回找
    for i in range(len(bars) - 1, 1, -1):
        b0, b1, b2 = bars[i - 2], bars[i - 1], bars[i]
        if b1.low < b0.low and b1.low < b2.low:
            return i - 1  # return middle bar index
    return None


def confirm_sanmai_60min(symbol: str, zg: float, signal_date: str,
                         daily_window: List[RawBar], conn=None) -> tuple:
    """三买60分钟次级别确认。

    日线: 离开中枢+回试不破ZG。
    60min: 用原始K线找近期回试低点近ZG + MACD止跌信号。

    不求60min笔(太稀疏),直接用价格行为判断。

    Returns (confirmed: bool, entry_price: float, reason: str)
    """
    # 检查缓存
    cache_key = f"{symbol}|{signal_date[:10]}"
    if cache_key in _CONFIRM_CACHE:
        return _CONFIRM_CACHE[cache_key]

    bars_60 = load_60min(symbol, conn=conn)
    if not bars_60:
        _CONFIRM_CACHE[cache_key] = (False, 0, '无60min数据')
        return _CONFIRM_CACHE[cache_key]

    window_60 = _bars_near(bars_60, signal_date, window_before=800, window_after=200)
    if len(window_60) < 100:
        _CONFIRM_CACHE[cache_key] = (False, 0, '60min数据不足')
        return _CONFIRM_CACHE[cache_key]

    sig_dt = datetime.strptime(signal_date[:10], "%Y-%m-%d")
    sig_idx = next((i for i, b in enumerate(window_60) if b.dt >= sig_dt), len(window_60) - 1)
    pre_bars = window_60[:sig_idx + 1]
    if len(pre_bars) < 60:
        _CONFIRM_CACHE[cache_key] = (False, 0, '60min前序数据不足')
        return _CONFIRM_CACHE[cache_key]

    cur_price = pre_bars[-1].close
    if cur_price <= zg:
        _CONFIRM_CACHE[cache_key] = (False, 0, f'价格破ZG(cur={cur_price:.2f} ZG={zg:.2f})')
        return _CONFIRM_CACHE[cache_key]

    # ── 用原始K线找近期回试低点 (不用笔,笔太稀疏) ──
    # 回看最近60根60min bar (~15个交易日),找最低点
    lookback = min(240, len(pre_bars))  # ~60 trading hours
    recent = pre_bars[-lookback:]
    recent_low = min(b.low for b in recent)
    recent_high = max(b.high for b in recent)

    # 回试低点必须在ZG附近
    dist_from_zg = (recent_low - zg) / zg
    if dist_from_zg > 0.30:  # 回试太远=没真正回试
        r = (False, 0, f'回试不充分(近期低点距ZG {dist_from_zg*100:.0f}%)')
        _CONFIRM_CACHE[cache_key] = r; return r
    if dist_from_zg < -0.10:  # 跌太深=结构破坏
        r = (False, 0, f'回试过深(近期低点距ZG {dist_from_zg*100:.0f}%)')
        _CONFIRM_CACHE[cache_key] = r; return r

    # 价格必须在上升中 (近期高点 > 近期低点 * 1.05)
    if recent_high < recent_low * 1.05:
        r = (False, 0, '无有效上升')
        _CONFIRM_CACHE[cache_key] = r; return r

    # ── MACD信号 ──
    closes_60 = [b.close for b in pre_bars]
    diff_60, dea_60, hist_60 = macd(closes_60)
    if len(diff_60) < 20:
        r = (False, 0, 'MACD不足')
        _CONFIRM_CACHE[cache_key] = r; return r

    signals = []

    # 核心信号1: 底分型 (在最近15根bar内,必须在近期低点附近)
    di_fen_idx = _find_60min_di_fen(pre_bars[-15:])
    if di_fen_idx is not None:
        df_abs = len(pre_bars) - 15 + di_fen_idx
        df_low = pre_bars[df_abs].low if 0 <= df_abs < len(pre_bars) else 0
        if df_low > 0 and df_low <= recent_low * 1.03:
            signals.append('底分型')

    # 核心信号2: 绿柱缩小 (比DIFF/DIF更可靠)
    if len(hist_60) >= 12:
        recent_g = [h for h in hist_60[-6:] if h < 0]
        prev_g = [h for h in hist_60[-12:-6] if h < 0]
        if recent_g and prev_g:
            if sum(abs(h) for h in recent_g) < sum(abs(h) for h in prev_g) * 0.75:
                signals.append('绿柱缩小')

    # 辅助信号: DIFF金叉 (仅当DIFF刚刚上穿DEA,而不是已经在上方很久)
    if len(diff_60) >= 3 and len(dea_60) >= 3:
        if diff_60[-2] <= dea_60[-2] and diff_60[-1] > dea_60[-1]:
            signals.append('DIFF金叉')

    # 辅助信号: 缩量回试 (近期下跌bars的vol在缩小)
    dn_bars = [b for b in recent[-30:] if b.close < b.open]
    if len(dn_bars) >= 3:
        vol_first = sum(b.vol for b in dn_bars[:len(dn_bars)//2])
        vol_second = sum(b.vol for b in dn_bars[len(dn_bars)//2:])
        if vol_second > 0 and vol_first > 0 and vol_second < vol_first * 0.8:
            signals.append('缩量')

    # 必须: 底分型 + 至少一个其他信号 (绿柱缩小/DIFF金叉/缩量)
    has_difen = '底分型' in signals
    has_other = any(s in signals for s in ['绿柱缩小', 'DIFF金叉', '缩量'])

    if not has_difen:
        r = (False, 0, '无60min底分型')
        _CONFIRM_CACHE[cache_key] = r; return r
    if not has_other:
        r = (False, 0, f'底分型无辅助信号')
        _CONFIRM_CACHE[cache_key] = r; return r

    r = (True, cur_price, f'60min({" + ".join(signals)})')
    _CONFIRM_CACHE[cache_key] = r
    return r


def confirm_ermai_60min(symbol: str, a_price: float, b_price: float,
                        signal_date: str, daily_window: List[RawBar], conn=None) -> tuple:
    """二买60分钟次级别确认。

    日线: trend一买后,B反弹+C回调不破A。
    60min: C回调段在60min上形成盘整背驰/底分型。

    Returns (confirmed: bool, entry_price: float, reason: str)
    """
    bars_60 = load_60min(symbol, conn=conn)
    if not bars_60:
        return False, 0, '无60min数据'

    window_60 = _bars_near(bars_60, signal_date, window_before=800, window_after=200)
    if len(window_60) < 100:
        return False, 0, '60min数据不足'

    sig_dt = datetime.strptime(signal_date[:10], "%Y-%m-%d")
    sig_idx = next((i for i, b in enumerate(window_60) if b.dt >= sig_dt), len(window_60) - 1)
    pre_bars = window_60[:sig_idx + 1]

    if len(pre_bars) < 60:
        return False, 0, '60min前序数据不足'

    cur_price = pre_bars[-1].close
    if cur_price < a_price * 0.99:
        return False, 0, f'破A({a_price:.2f})'

    # 60min笔
    bis_60 = build_bi(pre_bars, level='60')
    if len(bis_60) < 3:
        return False, 0, '60min笔不足'

    closes_60 = [b.close for b in pre_bars]
    diff_60, dea_60, hist_60 = macd(closes_60)

    # 找最近的向下笔 + MACD背驰
    dn_bis = [bi for bi in bis_60 if bi.direction == 'down']
    if not dn_bis:
        return False, 0, '无向下笔'

    last_dn = dn_bis[-1]
    if last_dn.low <= a_price:
        return False, 0, f'60min向下笔破A({a_price:.2f})'

    # 找倒数第二个向下笔用于背驰比较
    if len(dn_bis) >= 2:
        prev_dn = dn_bis[-2]
        # 比较两段向下笔对应的MACD面积
        c_start = next((i for i, b in enumerate(pre_bars)
                        if b.dt.strftime("%Y-%m-%d %H:%M") >= last_dn.start_dt[:16]), 0)
        c_end = next((i for i, b in enumerate(pre_bars)
                      if b.dt.strftime("%Y-%m-%d %H:%M") >= last_dn.end_dt[:16]), len(pre_bars) - 1)
        a_start = next((i for i, b in enumerate(pre_bars)
                        if b.dt.strftime("%Y-%m-%d %H:%M") >= prev_dn.start_dt[:16]), 0)
        a_end = next((i for i, b in enumerate(pre_bars)
                      if b.dt.strftime("%Y-%m-%d %H:%M") >= prev_dn.end_dt[:16]), c_start)

        area_a = macd_area(diff_60, dea_60, a_start, a_end, 'down')
        area_c = macd_area(diff_60, dea_60, c_start, c_end, 'down')
        has_divergence = area_a < 0 and area_c < 0 and abs(area_c) < abs(area_a) * 0.9
    else:
        has_divergence = False

    # DIFF 拐头
    diff_rising = len(diff_60) >= 3 and diff_60[-1] > diff_60[-2]

    # 底分型
    di_fen_idx = _find_60min_di_fen(pre_bars[-20:])

    if not (has_divergence or diff_rising or di_fen_idx):
        return False, 0, f'无60min确认信号'

    entry_price = pre_bars[-1].close
    signals = []
    if has_divergence:
        signals.append('60min背驰')
    if diff_rising:
        signals.append('DIFF拐头')
    if di_fen_idx:
        signals.append('底分型')

    return True, entry_price, f'60min二买({" + ".join(signals)})'


def confirm_yimai_60min(symbol: str, a_price: float, signal_date: str,
                        daily_window: List[RawBar], conn=None) -> tuple:
    """一买60分钟次级别确认。

    日线已确认趋势背驰一买。
    60min: 底分型 + DIFF拐头 + 可能60min级别背驰。

    Returns (confirmed: bool, entry_price: float, reason: str)
    """
    bars_60 = load_60min(symbol, conn=conn)
    if not bars_60:
        return False, 0, '无60min数据'

    window_60 = _bars_near(bars_60, signal_date, window_before=1000, window_after=200)
    if len(window_60) < 100:
        return False, 0, '60min数据不足'

    sig_dt = datetime.strptime(signal_date[:10], "%Y-%m-%d")
    sig_idx = next((i for i, b in enumerate(window_60) if b.dt >= sig_dt), len(window_60) - 1)
    pre_bars = window_60[:sig_idx + 1]

    if len(pre_bars) < 100:
        return False, 0, '60min前序数据不足'

    cur_price = pre_bars[-1].close
    if cur_price < a_price * 0.92 or cur_price > a_price * 1.08:
        return False, 0, f'价格距A太远({cur_price/a_price:.2f}x)'

    # 60min笔
    bis_60 = build_bi(pre_bars, level='60')
    if len(bis_60) < 5:
        return False, 0, '60min笔不足'

    closes_60 = [b.close for b in pre_bars]
    diff_60, dea_60, hist_60 = macd(closes_60)

    # 底分型 + DIFF拐头
    di_fen_idx = _find_60min_di_fen(pre_bars[-20:])
    diff_rising = len(diff_60) >= 4 and diff_60[-1] > diff_60[-2] and diff_60[-2] > diff_60[-3]

    # 60min级别的趋势背驰(两下移中枢)
    zs_60 = build_zs(bis_60, level='60')
    down_zs_60 = [z for z in zs_60 if z.sdir == '向下']
    has_60min_div = False
    if len(down_zs_60) >= 2:
        za, zb = down_zs_60[-2], down_zs_60[-1]
        if zb.zg < za.zd:
            a_s = next((i for i, b in enumerate(pre_bars)
                        if b.dt.strftime("%Y-%m-%d %H:%M") >= za.start_dt[:16]), 0)
            a_e = next((i for i, b in enumerate(pre_bars)
                        if b.dt.strftime("%Y-%m-%d %H:%M") >= za.end_dt[:16]), 0)
            c_s = next((i for i, b in enumerate(pre_bars)
                        if b.dt.strftime("%Y-%m-%d %H:%M") >= zb.start_dt[:16]), 0)
            c_e = next((i for i, b in enumerate(pre_bars)
                        if b.dt.strftime("%Y-%m-%d %H:%M") >= zb.end_dt[:16]), 0)
            area_a = macd_area(diff_60, dea_60, a_s, a_e, 'down')
            area_c = macd_area(diff_60, dea_60, c_s, c_e, 'down')
            has_60min_div = area_a < 0 and area_c < 0 and abs(area_c) < abs(area_a) * 0.9

    if not (di_fen_idx and diff_rising):
        return False, 0, f'无60min底分型+DIFF拐头'

    entry_price = pre_bars[-1].close
    signals = ['底分型', 'DIFF拐头']
    if has_60min_div:
        signals.append('60min背驰')

    return True, entry_price, f'60min一买({" + ".join(signals)})'


def confirm_zhenmai_60min(symbol: str, zg: float, zd: float, signal_date: str,
                          daily_window: List[RawBar], conn=None) -> tuple:
    """震买60分钟次级别确认。

    日线: 向上中枢, 价格在中枢下沿附近。
    60min: ZD附近底分型 + MACD绿柱缩小。

    Returns (confirmed: bool, entry_price: float, reason: str)
    """
    bars_60 = load_60min(symbol, conn=conn)
    if not bars_60:
        return False, 0, '无60min数据'

    window_60 = _bars_near(bars_60, signal_date, window_before=500, window_after=100)
    if len(window_60) < 60:
        return False, 0, '60min数据不足'

    sig_dt = datetime.strptime(signal_date[:10], "%Y-%m-%d")
    sig_idx = next((i for i, b in enumerate(window_60) if b.dt >= sig_dt), len(window_60) - 1)
    pre_bars = window_60[:sig_idx + 1]

    cur_price = pre_bars[-1].close
    if cur_price > zd * 1.02 or cur_price < zd * 0.92:
        return False, 0, f'价格不在ZD附近({cur_price/zd:.2f}x)'

    # 60min MACD
    closes_60 = [b.close for b in pre_bars]
    diff_60, dea_60, hist_60 = macd(closes_60)
    if len(hist_60) < 8:
        return False, 0, 'MACD数据不足'

    # 绿柱缩小
    recent = [h for h in hist_60[-4:] if h < 0]
    prev = [h for h in hist_60[-8:-4] if h < 0]
    green_shrinking = recent and prev and sum(abs(h) for h in recent) < sum(abs(h) for h in prev) * 0.8

    # 底分型
    di_fen_idx = _find_60min_di_fen(pre_bars[-15:])

    if not (green_shrinking or di_fen_idx):
        return False, 0, '无60min确认信号'

    entry_price = pre_bars[-1].close
    signals = []
    if green_shrinking:
        signals.append('绿柱缩小')
    if di_fen_idx:
        signals.append('底分型')

    return True, entry_price, f'60min震买({" + ".join(signals)})'


def clear_60min_cache():
    """清空60min数据缓存。"""
    _60MIN_CACHE.clear()
    _CONFIRM_CACHE.clear()
