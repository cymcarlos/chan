#!/usr/bin/env python3
"""日线级别买点扫描 — 基础设施模块(原 backtest_daily.py 提取)。

日K扫描逻辑(scan_daily)被 日K+30min 引擎 和 画图脚本 共用:
  - load_daily:        日K数据加载(缓存)
  - clear_daily_cache: 清缓存
  - scan_daily:        日线 L1笔中枢 买点扫描
      * 一买: 两个相邻、完整、同向下日线笔中枢 + ZG2<ZD1中心区间下移
              + C段创新低 + MACD面积趋势背驰
      * 三买: 向上日线笔中枢 + 离开ZG + 回试不破ZG
      * 二买: 不在日K层生成(课本p37: 二买=一买后第一次【次级别】回调,
              操作级别=日K时次级别=30min, 由30min介入层确认)
  前视偏差修复(2026-08-06): edt截断——扫描只能用edt之前的数据。
"""

import hashlib
import json
import sqlite3
from datetime import datetime

from chan.paths import DB_PATH
from chan.bars import RawBar, macd, macd_area
from chan.bi import build_bi
from chan.zhongshu import build_zs
from chan.state import clear_level
from chan.scanner_60min import BuyCandidate, _add_weeks

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
    if len(all_zs_L1) < 2:
        return []

    # 窄中枢过滤(2026-08-06): 区间宽度 < 0.3% 的中枢是"多笔共同重叠"的数学产物
    # (如 [20.97,20.99]=0.1%), 操作意义≈0; 但 0.5%~1.5% 的窄中枢在低价股/低波动股
    # 上是真实窄幅整理(000537 一买中枢对 0.5%/1.4%), 不能一刀切滤掉
    zs_L1 = [z for z in all_zs_L1 if z.zg > z.zd * 1.003]
    if len(zs_L1) < 2:
        return []
    allow_third_buy = len(zs_L1) >= 3  # 保持三买原有“至少三个有效中枢”门槛

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

    # ── 一买: 两个相邻完整向下中枢 + 中心区间下移 + C创新低 + 趋势背驰 ──
    # 正式口径:
    #  ① A/B必须在原始同级中枢序列中直接相邻；不能跳过向上或窄中枢配对。
    #  ② 两者均已确认，且 B.ZG < A.ZD（108课的两个中枢区间不重叠）。
    #     更严格的 B.GG < A.DD 作为诊断项报告，不作为课本硬门槛。
    #  ③ 背驰段(C) = 终点为趋势最低点的已确认向下笔(61课: 趋势的最后一段下跌)。
    #     2026-08-17修复(§7.20): 旧版从"B完结确认之后"(b.end+1)划C并要求跌破B.DD —
    #     中枢延伸语义把趋势最后一段下跌吞进B(其低点就是DD), B完结时价格往往已反转,
    #     "之后创新低"结构上几乎不可能(485个候选仅4个过, 时点错位)。
    #  ④ C必须创出整个趋势的新低(低于背驰段之前的全部低点), 且MACD平均力度背驰；
    #     盘整背驰(未创趋势新低)不生成正式一买。
    eligible_zs_ids = {id(z) for z in zs_L1}

    def _complete_zs(z):
        """中枢已由后续离开笔封闭，且其确认时点落在当前日K前缀内。"""
        start_i = d_idx.get(z.start_dt[:10])
        end_i = d_idx.get(z.end_dt[:10])
        if not getattr(z, 'is_complete', False):
            return None
        confirm_at = getattr(z, 'leave_confirm_at', '') or ''
        confirm_i = d_idx.get(confirm_at[:10]) if confirm_at else None
        if start_i is None or end_i is None or confirm_i is None:
            return None
        if not (start_i <= end_i < len(bars) and confirm_i < len(bars)):
            return None
        return confirm_i

    def _zs_evidence(z):
        """冻结正式一买所用中枢字段；不把可变对象引用写入审计证据。"""
        return {
            'direction': z.sdir,
            'start_at': z.start_dt,
            'end_at': z.end_dt,
            'occur_at': getattr(z, 'occur_at', '') or None,
            'initial_confirm_at': getattr(z, 'confirm_at', '') or None,
            'complete_at': getattr(z, 'leave_confirm_at', '') or None,
            'member_start_idx': getattr(z, 'member_start_idx', -1),
            'member_end_idx': getattr(z, 'member_end_idx', -1),
            'is_complete': bool(getattr(z, 'is_complete', False)),
            'ZD': float(z.zd), 'ZG': float(z.zg),
            'DD': float(z.dd), 'GG': float(z.gg),
        }

    # 候选宽度过滤与结构相邻/边界判定分离：后两者必须使用未过滤序列。
    for b_zs_i in range(1, len(all_zs_L1)):
        a, b = all_zs_L1[b_zs_i - 1], all_zs_L1[b_zs_i]
        if id(a) not in eligible_zs_ids or id(b) not in eligible_zs_ids:
            continue
        if a.sdir != '向下' or b.sdir != '向下':
            continue
        if getattr(a, 'member_start_idx', -1) <= 0:
            continue   # 数据前缀首中枢没有真实进入笔，方向不可用于正式一买
        a_confirm_i = _complete_zs(a)
        b_confirm_i = _complete_zs(b)
        if a_confirm_i is None or b_confirm_i is None:
            continue
        if b.zg >= a.zd:
            continue   # 中心区间未下移，不能构成两个下降中枢的趋势
        # A段: a.start前60根摆动高点 → a.start
        a_s0 = _bar_idx(a.start_dt)
        hi = a_s0
        for k in range(a_s0 - 1, max(0, a_s0 - 60), -1):
            if bars[k].high > bars[hi].high:
                hi = k
        if a_s0 - hi < 5:
            continue
        area_A = macd_area(diff, dea, hi, a_s0, 'down')
        # 背驰段(C)重定义(2026-08-17, §7.20): 趋势终点区 = B.start → 下一原始
        # 同级中枢start(或数据末端); 背驰段 = 终点为趋势最低点的已确认向下笔。
        # 该笔在中枢延伸语义下常是B的成员笔(旧版因此漏判); 创新低参照 = 背驰段
        # 之前的整个趋势低点(而非b.dd — DD常被延伸压到背驰点本身, 判定退化)。
        b_e = _bar_idx(b.end_dt)
        z_s0 = _bar_idx(b.start_dt)
        if b_zs_i + 1 < len(all_zs_L1):
            next_zs_start = _bar_idx(all_zs_L1[b_zs_i + 1].start_dt)
            z_e0 = next_zs_start
        else:
            next_zs_start = None
            z_e0 = len(bars)
        if z_e0 <= z_s0:
            continue
        zone = bars[z_s0:z_e0]
        min_bar = min(zone, key=lambda x: x.low)
        c_low = min_bar.low
        c_low_i = z_s0 + zone.index(min_bar)
        c_confirm_bis = [
            bi for bi in bis
            if bi.direction == 'down'
            and bi.end_dt[:10] == min_bar.dt.strftime('%Y-%m-%d')
            and abs(float(bi.end_price) - float(c_low)) <= max(abs(c_low), 1.0) * 1e-9
            and getattr(bi, 'confirm_at', '')
        ]
        if not c_confirm_bis:
            continue   # 背驰段低点必须是已确认向下笔终点(因果门), 不能是漂移中的临时低点
        c_bi = max(c_confirm_bis, key=lambda x: x.end_dt)
        c_low_confirm_at = c_bi.confirm_at
        c_confirm_i = d_idx.get(c_low_confirm_at[:10])
        if c_confirm_i is None or c_confirm_i >= len(bars):
            continue
        # 创新低: 低于背驰段之前的整个趋势低点(进入段高点→背驰段起点),
        # 未创趋势新低 = 盘整背驰 → 不生成正式一买(27课/§7.17.1)
        c_s0 = _bar_idx(c_bi.start_dt)
        if c_s0 <= a_s0:
            continue
        prev_low = min(x.low for x in bars[hi:c_s0])
        if c_low >= prev_low:
            continue
        area_C = macd_area(diff, dea, c_s0, c_low_i, 'down')
        len_A = max(a_s0 - hi, 1); len_C = max(c_low_i - c_s0, 1)
        if area_A < 0 and area_C < 0 and \
                abs(area_C) / len_C < abs(area_A) / len_A * 0.9:
            div_type = '趋势背驰'
            a_low = min_bar.low
            a_date = min_bar.dt.strftime("%Y-%m-%d")
            # sig=背驰点日(101课), 窗口52周
            sig = a_date
            candidate = BuyCandidate(
                buy_type='一买', signal_date=sig,
                window_end=_add_weeks(sig, 52),
                l2=b, a_price=a_low, a_date=a_date, div_type=div_type,
                weekly_dir=l2_dir)
            avg_A = abs(area_A) / len_A
            avg_C = abs(area_C) / len_C
            next_center_at = (
                all_zs_L1[b_zs_i + 1].start_dt
                if b_zs_i + 1 < len(all_zs_L1) else None)
            # 运行时审计旁路：BuyCandidate dataclass与旧序列化字段均不变化。
            evidence = {
                'schema_version': 'formal-first-buy/v1',
                'centers': {
                    'A': _zs_evidence(a),
                    'B': _zs_evidence(b),
                },
                'raw_center_indices': {'A': b_zs_i - 1, 'B': b_zs_i},
                'segments': {
                    'A': {
                        'start_at': bars[hi].dt.strftime("%Y-%m-%d"),
                        'end_at': bars[a_s0].dt.strftime("%Y-%m-%d"),
                        'area': float(area_A), 'length': len_A,
                        'average_force': float(avg_A),
                    },
                    'C': {
                        'start_at': c_bi.start_dt[:10],
                        'end_at': a_date,
                        'low_at': a_date, 'low': float(c_low),
                        'low_confirm_at': c_low_confirm_at,
                        'area': float(area_C), 'length': len_C,
                        'average_force': float(avg_C),
                        'next_center_start_at': next_center_at,
                        'prev_trend_low': float(prev_low),
                    },
                },
                'average_force_ratio_C_to_A': (
                    float(avg_C / avg_A) if avg_A else None),
                'B_complete_at': getattr(b, 'leave_confirm_at', '') or None,
                'invariants': {
                    'adjacent_in_raw_center_sequence': True,
                    'same_direction_down': a.sdir == b.sdir == '向下',
                    'both_centers_width_eligible': (
                        id(a) in eligible_zs_ids and id(b) in eligible_zs_ids),
                    'A_complete': bool(getattr(a, 'is_complete', False)),
                    'B_complete': bool(getattr(b, 'is_complete', False)),
                    'A_has_real_entry_pen': getattr(a, 'member_start_idx', -1) > 0,
                    'center_interval_downshift_ZG2_lt_ZD1': b.zg < a.zd,
                    'C_low_within_terminal_zone': z_s0 <= c_low_i < z_e0,
                    'C_low_is_confirmed_down_pen_end': bool(c_confirm_bis),
                    'C_low_confirmed_in_available_prefix': c_confirm_i < len(bars),
                    'C_makes_new_low_below_prev_trend_low': c_low < prev_low,
                    'A_and_C_have_down_macd_area': area_A < 0 and area_C < 0,
                    'C_average_force_lt_A_x_0_9': avg_C < avg_A * 0.9,
                    'trend_divergence_only': div_type == '趋势背驰',
                },
                'diagnostics': {
                    'strong_full_range_isolation_GG2_lt_DD1': b.gg < a.dd,
                    'full_range_overlap': not (b.gg < a.dd),
                    'C_low_vs_B_DD': float(c_low - b.dd),
                },
            }
            canonical = json.dumps(
                evidence, ensure_ascii=False, sort_keys=True,
                separators=(',', ':'), allow_nan=False).encode('utf-8')
            provenance_id = 'b1prov_' + hashlib.sha256(canonical).hexdigest()[:24]
            evidence['provenance_id'] = provenance_id
            candidate.first_buy_evidence = evidence
            candidate.first_buy_provenance_id = provenance_id
            # 同一背驰点若仍被多个结构解释，只保留结束时间最近的中枢来源。
            # 外层交易引擎仍按(买点类型, signal_date)消费，避免同一买点重复交易。
            previous = b1_by_signal.get(sig)
            if previous is None or (b.end_dt, b.start_dt) > (
                    previous.l2.end_dt, previous.l2.start_dt):
                b1_by_signal[sig] = candidate

    candidates.extend(b1_by_signal.values())

    # ── 三买: 向上日线笔中枢(L1) + 离开ZG + 回试不破ZG ──
    for l2 in (zs_L1 if allow_third_buy else []):
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
