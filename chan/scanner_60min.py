#!/usr/bin/env python3
"""60分钟 L2中枢 扫描器。

管线:
  60min K线 → 笔 → 笔中枢(L1) → 走势类型归并 → L2中枢 → 买点检测

已验证: 000333.SZ 57个L1中枢 → 30个走势类型 → 24个L2中枢 → 1一买/9二买/3三买

⚠️ 算法文档: /root/data/backtest/ALGORITHM.md (第3.1节)
   修改影响算法时必须同步更新 ALGORITHM.md。
"""

import sqlite3
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Optional
from chan.bars import RawBar, macd, macd_area
from chan.bi import build_bi
from chan.zhongshu import build_zs, Zhongshu
from chan.confirm_60min import load_60min
from chan.state import clear_level

DB_PATH = '/root/data/backtest/kline.db'
CACHE_VERSION = 1


@dataclass
class Trend:
    """走势类型: 同向不重叠中枢的归并"""
    idx: int
    sdir: str          # '向上' / '向下'
    start_dt: str
    end_dt: str
    high: float         # GG
    low: float          # DD
    n_zs: int           # 包含中枢数量
    t_type: str         # '趋势' / '盘整'
    zss: List[Zhongshu]


@dataclass
class L2Zhongshu:
    """L2中枢 = 3个走势类型重叠"""
    idx: int
    zg: float
    zd: float
    sdir: str           # 第一个走势的方向
    start_dt: str
    end_dt: str
    trends: List[Trend]


@dataclass
class BuyCandidate:
    """买点候选"""
    buy_type: str        # '一买' / '二买' / '三买'
    signal_date: str
    window_end: str
    l2: Optional[L2Zhongshu] = None
    a_price: float = 0.0
    a_date: str = ''
    b_price: float = 0.0
    b_date: str = ''
    c_price: float = 0.0    # 二买: 回调低点(入场参考); 一买/三买: 0
    c_date: str = ''
    weekly_dir: str = ''    # 周线方向(当前状态, Ch49 过滤参考)
    div_type: str = ''      # 一买: '趋势背驰'(b.GG<a.DD) / '盘整背驰'(GG/DD交错) (Phase2)
    zs_zg: float = 0.0
    zs_zd: float = 0.0
    zs_end_date: str = ''


def build_trends(zs_list: List[Zhongshu]) -> List[Trend]:
    """走势类型归并: 同向+不重叠→合并, 方向变/重叠→新走势"""
    if not zs_list:
        return []

    trends = []
    i = 0
    while i < len(zs_list):
        cur_zs = [zs_list[i]]
        direction = zs_list[i].sdir
        j = i + 1
        while j < len(zs_list):
            nxt = zs_list[j]
            prev = cur_zs[-1]
            overlap = min(prev.zg, nxt.zg) > max(prev.zd, nxt.zd)
            if nxt.sdir == direction and not overlap:
                cur_zs.append(nxt)
                j += 1
            else:
                break

        t_high = max(z.gg for z in cur_zs)
        t_low = min(z.dd for z in cur_zs)
        n_zs = len(cur_zs)
        t_type = '趋势' if n_zs >= 2 else '盘整'

        trends.append(Trend(
            idx=len(trends) + 1,
            sdir=direction,
            start_dt=cur_zs[0].start_dt,
            end_dt=cur_zs[-1].end_dt,
            high=t_high, low=t_low,
            n_zs=n_zs, t_type=t_type,
            zss=cur_zs,
        ))
        i = j

    return trends


def build_l2_zs(trends: List[Trend], min_trend_zs: int = 1) -> List[L2Zhongshu]:
    """L2中枢 = 3个连续走势类型 GG-DD 重叠(消费式, 依次衔接)。

    课本(Ch17): 中枢 = 3个次级别走势类型重叠, 中枢依次出现。
    消费式: 3个走势一旦形成重叠即消费掉(无论是否记录), 下一个中枢
    从其后开始 —— 避免滑动窗口产生时间重叠的影子中枢(重叠序列会让
    "两个连续向下L2"被误判为趋势两中枢 → 假一买)。

    min_trend_zs: 构成L2的3个走势中至少需要1个走势含≥min_trend_zs个
    L1中枢(默认1 = 不过滤; 3个盘整走势重叠也是有效中枢, 见000591
    2024-10~2025-02 的 L2[4.42,4.77])。
    """
    l2_list = []
    i = 0
    while i < len(trends) - 2:
        t0, t1, t2 = trends[i], trends[i + 1], trends[i + 2]
        zg = min(t0.high, t1.high, t2.high)
        zd = max(t0.low, t1.low, t2.low)
        if zg > zd:
            has_trend = any(t.n_zs >= min_trend_zs for t in [t0, t1, t2])
            if has_trend:
                l2_list.append(L2Zhongshu(
                    idx=len(l2_list) + 1,
                    zg=zg, zd=zd,
                    sdir=t0.sdir,
                    start_dt=t0.start_dt,
                    end_dt=t2.end_dt,
                    trends=[t0, t1, t2],
                ))
            i += 3  # 3个走势已形成重叠(中枢), 消费掉, 保证序列依次衔接
        else:
            i += 1
    return l2_list


def _add_weeks(dt_str: str, n: int) -> str:
    d = datetime.strptime(dt_str, "%Y-%m-%d")
    return (d + timedelta(weeks=n)).strftime("%Y-%m-%d")


def scan_60min(symbol: str, edt: str = "20260707", conn=None) -> List[BuyCandidate]:
    """60分钟 L2中枢 买点扫描"""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        own = True

    # 加载60min数据
    bars_60 = load_60min(symbol, conn=conn)
    # 前视偏差修复(2026-08-06): edt截断——历史回测不能用未来结构
    if edt:
        edt_dt = datetime.strptime(edt, "%Y%m%d")
        bars_60 = [b for b in bars_60 if b.dt <= edt_dt]
    if len(bars_60) < 200:
        if own: conn.close()
        return []

    # 60min笔 + 笔中枢(L1)
    from chan.state import clear_level
    clear_level('60')
    bis_60 = build_bi(bars_60, level='60')
    if len(bis_60) < 10:
        if own: conn.close()
        return []

    clear_level('60')
    zs_L1 = build_zs(bis_60, level='60')
    if len(zs_L1) < 3:
        if own: conn.close()
        return []

    # 走势类型 + L2中枢
    trends = build_trends(zs_L1)
    if len(trends) < 3:
        if own: conn.close()
        return []

    l2_list = build_l2_zs(trends)
    if not l2_list:
        if own: conn.close()
        return []

    # 60min MACD
    closes_60 = [b.close for b in bars_60]
    diff_60, dea_60, hist_60 = macd(closes_60)

    def _bar_idx(dt_str):
        for i, b in enumerate(bars_60):
            if b.dt.strftime("%Y-%m-%d") >= dt_str[:10]:
                return i
        return len(bars_60) - 1

    candidates = []

    # ── 一买: 两个连续向下L2中枢 + ZG下移 + MACD背驰 ──
    for i in range(len(l2_list) - 1):
        a, b = l2_list[i], l2_list[i + 1]
        if a.sdir == '向下' and b.sdir == '向下':
            if b.zg < a.zd:  # 中枢下移
                a_s = _bar_idx(a.start_dt)
                a_e = _bar_idx(a.end_dt)
                b_s = _bar_idx(b.start_dt)
                b_e = _bar_idx(b.end_dt)
                area_a = macd_area(diff_60, dea_60, a_s, a_e, 'down')
                area_b = macd_area(diff_60, dea_60, b_s, b_e, 'down')
                if area_a < 0 and area_b < 0 and abs(area_b) < abs(area_a) * 0.9:
                    # 背驰点 = 第二个向下L2 时间范围内最低bar(实际低点+日期)
                    # 修复(2026-08-05): 之前 signal_date 用 L2结束日(2024-10-08),
                    # 但背驰低点(3.97)发生在更早(2024-09-18), 图上标注悬空。
                    b_s = _bar_idx(b.start_dt)
                    b_e = _bar_idx(b.end_dt)
                    sub = bars_60[b_s:b_e + 1]
                    if not sub:
                        continue
                    min_bar = min(sub, key=lambda x: x.low)
                    a_low = min_bar.low
                    a_date = min_bar.dt.strftime("%Y-%m-%d")
                    # signal_date = L2结束日(反弹确认后, 回测窗口起点, 绩效验证更优);
                    # a_date = 背驰点实际日期(图标注位置, 不悬空)
                    sig = b.end_dt[:10] if len(b.end_dt) >= 10 else b.end_dt
                    candidates.append(BuyCandidate(
                        buy_type='一买',
                        signal_date=sig,
                        window_end=_add_weeks(sig, 104),
                        l2=b, a_price=a_low, a_date=a_date,
                    ))

    # ── 三买: 向上L2中枢 + 离开ZG + 回试不破ZG ──
    for l2 in l2_list:
        if l2.sdir != '向上':
            continue
        end_idx = _bar_idx(l2.end_dt)
        post = bars_60[end_idx:]
        if len(post) < 30:
            continue

        # 离开ZG
        leave_idx = None
        for k, b in enumerate(post):
            if b.high > l2.zg:
                leave_idx = k
                break
        if leave_idx is None:
            continue

        # 回试低点
        after_leave = post[leave_idx:]
        lookback = min(120, len(after_leave))
        pb_low = min(b.low for b in after_leave[:lookback])
        cur = after_leave[-1].close

        if pb_low > l2.zg and cur > l2.zg:
            # 信号日期 = DD最低的中枢完成日(背驰点)
            last_t = l2.trends[-1]
            if last_t.n_zs >= 1 and last_t.zss:
                best_z = min(last_t.zss, key=lambda z: z.dd)
                sig_dt = best_z.end_dt[:10]
            else:
                sig_dt = l2.end_dt[:10]

            candidates.append(BuyCandidate(
                buy_type='三买',
                signal_date=sig_dt,
                window_end=_add_weeks(sig_dt, 26),
                l2=l2,
                zs_zg=l2.zg, zs_zd=l2.zd,
                zs_end_date=l2.end_dt[:10] if len(l2.end_dt) >= 10 else l2.end_dt,
            ))

    # ── 二买(课本): 日线一买后, 第一次60min回调不破一买低点 ──
    # 买卖点定律一: 任何级别二买 = 次级别一买。
    # 日线二买 = 日线一买(两个L2下移背驰)结束后:
    #   60min走势序列第一个向上走势(反弹) → 第一个向下走势(回调),
    #   回调低点 > 一买A价×0.98 → 日线二买确认, 信号日 = 回调结束日。
    # (旧逻辑"三个L2 down-up-down"等的是日线级别回调, 跨年, 不符合课本)
    for ym in [c for c in candidates if c.buy_type == '一买']:
        # 课本修正(2026-08-06, p246): 二买起点=背驰点(a_date), 非信号日
        ym_end = (ym.a_date or ym.signal_date)[:10]
        start_i = next((i for i, t in enumerate(trends)
                        if t.start_dt[:10] >= ym_end), len(trends))
        # 一买信号日可能落在某走势内部, 从该走势之后开始
        while start_i < len(trends) and trends[start_i].end_dt[:10] <= ym_end:
            start_i += 1
        # 第一个向上走势(反弹)
        bounce = None
        j = start_i
        while j < len(trends):
            if trends[j].sdir == '向上':
                bounce = trends[j]
                break
            j += 1
        if bounce is None or j + 1 >= len(trends):
            continue
        # 第一个向下走势(回调)
        pullback = trends[j + 1]
        if pullback.sdir != '向下':
            continue
        # 回调不破 A(一买实际低点)
        if pullback.low < ym.a_price * 0.98:
            continue
        sig_dt = pullback.end_dt[:10]
        # 回调低点实际日期(修复: 之前c_date=回调结束日, 但低点可能更早, 图标注悬空)
        pb_s = _bar_idx(pullback.start_dt)
        pb_e = _bar_idx(pullback.end_dt)
        pb_sub = bars_60[pb_s:pb_e + 1]
        min_pb = min(pb_sub, key=lambda x: x.low) if pb_sub else None
        c_low = min_pb.low if min_pb else pullback.low
        c_date = min_pb.dt.strftime("%Y-%m-%d") if min_pb else sig_dt
        candidates.append(BuyCandidate(
            buy_type='二买',
            signal_date=sig_dt,
            window_end=_add_weeks(sig_dt, 26),
            l2=ym.l2, a_price=ym.a_price, a_date=ym.a_date,
            b_price=bounce.high, b_date=bounce.end_dt[:10],
            c_price=c_low, c_date=c_date,
        ))

    # 周线方向(候选信号日时点, 回测可过滤; Ch49 区间套最外层)
    try:
        from chan.weekly_filter import weekly_direction_at
        for c in candidates:
            c.weekly_dir = weekly_direction_at(symbol, c.signal_date, conn=conn)
    except Exception:
        pass

    if own:
        conn.close()
    return candidates
