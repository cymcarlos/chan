#!/usr/bin/env python3
"""60分钟纯净管线 — L2中枢 → 买点 → 60min bar walk。
入场条件: (1)价格近关键位 (2)底分型在近期低点 (3)价格在回升

⚠️ 算法文档: /root/data/backtest/ALGORITHM.md (第3.3节 + 参数表)
   修改影响算法时必须同步更新 ALGORITHM.md。
"""

import sqlite3, time
from datetime import datetime, timedelta
from collections import defaultdict
from chan.bars import macd
from chan.scanner_60min import scan_60min
from chan.confirm_60min import load_60min
from chan.state import clear_all

DB_PATH = '/root/data/backtest/kline.db'

# 信号有效期: 104周→52周(一年), 太久的不算
SIGNAL_WINDOW_WEEKS = 13  # 监控模式: 3个月内不进A区则信号过期
FILTER_WEEKLY = False   # 周线过滤开关(精细化: 三买豁免)


def _recent_swing_low(bars, lookback=60):
    """最近N根bar的摆动低点"""
    n = min(lookback, len(bars))
    return min(b.low for b in bars[-n:])


def _has_bottom_fractal_near_low(bars, lookback=20, swing_lookback=60):
    """底分型是否在近期摆动低点附近(3%以内)"""
    if len(bars) < 3:
        return False
    swing_low = _recent_swing_low(bars, swing_lookback)
    n = min(lookback, len(bars))
    sub = bars[-n:]
    for i in range(len(sub) - 1, 1, -1):
        if sub[i-1].low < sub[i-2].low and sub[i-1].low < sub[i].low:
            # 底分型中间bar的低点必须在摆动低点3%以内
            if sub[i-1].low <= swing_low * 1.03:
                return True
    return False


def _is_recovering(bars, lookback=5):
    """最近几根bar是否在回升(收盘高于底分型)"""
    if len(bars) < 4:
        return False
    recent = bars[-lookback:]
    return recent[-1].close > recent[0].low


def _bottom_fractal_near_level(bars, target_level, lookback=20):
    """底分型本身的位置是否在关键位附近(5%以内)。

    课本: 回踩完成的标志是底分型在关键位附近形成。
    不是历史上是否曾经触及 — 是当前底分型是否就在那里。
    """
    df_idx = _find_bottom_fractal_idx(bars, lookback)
    if df_idx < 0:
        return False
    df_low = bars[df_idx].low
    # 底分型必须在关键位上方(不破A/ZG), 且在5%以内
    return target_level * 0.98 <= df_low <= target_level * 1.05


def _find_bottom_fractal_idx(bars, lookback=20):
    """找到最近底分型中间bar的索引"""
    if len(bars) < 3:
        return -1
    n = min(lookback, len(bars))
    sub = bars[-n:]
    for i in range(len(sub) - 1, 1, -1):
        if sub[i-1].low < sub[i-2].low and sub[i-1].low < sub[i].low:
            return len(bars) - n + i - 1
    return -1


def _fractal_confirmed(bars, fractal_idx, min_bars=8):
    """底分型形成后, 已经有min_bars根bar且最低价没被跌破"""
    bars_after = len(bars) - 1 - fractal_idx
    if bars_after < min_bars:
        return False
    # 底分型之后的所有bar最低价都高于底分型低点
    df_low = bars[fractal_idx].low
    for i in range(fractal_idx + 1, len(bars)):
        if bars[i].low < df_low:
            return False
    return True


def _divergence_at_fractal(bars, diff, dea, fractal_idx, lookback=300):
    """力竭确认 — 底分型为锚, 滚动评估到当前bar(面积比在分型bar处截断会失真,
    因低位钝化绿柱更深; 分型确认后反弹开始, 后段绿柱迅速消失, 面积比才有效)。

    下跌段起点 = 分型前40根内的摆动高点(最近一次回踩), 不在几个月前的高点。
    二买/三买是结构买点, 此处只过滤明显未力竭的下跌。
    """
    n = len(bars)  # 滚动评估到当前bar
    if n < 30:
        return False

    lb = min(lookback, n)
    sub_bars = bars[n - lb:n]

    # 找分型bar之前回看窗口内的显著高点(下跌段起点, 原版稳定逻辑)
    # 以分型bar为参照, 高点必须在分型之前
    fx_offset = len(bars) - 1 - fractal_idx  # 分型距当前bar的距离
    hi_end = lb - 1 - fx_offset
    if hi_end < 2:
        return False
    hi_idx = hi_end
    for i in range(hi_end - 1, max(0, hi_end - 200), -1):
        if sub_bars[i].high > sub_bars[hi_idx].high:
            hi_idx = i

    down_len = hi_end - hi_idx
    if down_len < 20:  # 至少20根bar的下跌段
        return False

    third = down_len // 3
    if third < 6:
        return False

    abs_first = n - lb + hi_idx
    abs_second = n - lb + hi_idx + third
    abs_third = n - lb + hi_idx + 2 * third

    area_front = 0.0
    for i in range(abs_first, abs_second):
        if i < len(diff) and i < len(dea):
            h = diff[i] - dea[i]
            if h < 0:
                area_front += abs(h)

    area_back = 0.0
    for i in range(abs_third, n):
        if i < len(diff) and i < len(dea):
            h = diff[i] - dea[i]
            if h < 0:
                area_back += abs(h)

    # 背驰: 后1/3面积 < 前1/3 * 0.6 (后段无绿柱 = 最强力竭) 且 DIFF拐头
    if area_front > 0 and area_back < area_front * 0.6:
        recent_diff = [diff[i] for i in range(n - 5, n) if i < len(diff)]
        if len(recent_diff) >= 3 and recent_diff[-1] > recent_diff[-2]:
            return True

    return False


def _macd_divergence_check(bars, diff, dea, lookback=300):
    """背驰检测: 最近一段下跌是否MACD力竭。

    从最近的高点开始下跌到当前形成底分型,
    比较下跌段前半和后半的MACD绿柱面积。
    后半面积必须显著小于前半(≤60%), 且DIFF开始拐头。
    """
    n = len(bars)
    if n < 30:
        return False

    lb = min(lookback, n)
    sub_bars = bars[-lb:]

    # 找最近的显著高点
    hi_idx = lb - 1
    for i in range(lb - 2, max(0, lb - 200), -1):
        if sub_bars[i].high > sub_bars[hi_idx].high:
            hi_idx = i

    down_len = lb - hi_idx
    if down_len < 20:  # 至少20根bar的下跌段
        return False

    # 分前中后三段, 比较前1/3和后1/3
    third = down_len // 3
    if third < 6:
        return False

    abs_first = n - lb + hi_idx
    abs_second = n - lb + hi_idx + third
    abs_third = n - lb + hi_idx + 2 * third

    # 前1/3绿柱面积
    area_front = 0.0
    for i in range(abs_first, abs_second):
        if i < len(diff) and i < len(dea):
            h = diff[i] - dea[i]
            if h < 0:
                area_front += abs(h)

    # 后1/3绿柱面积
    area_back = 0.0
    for i in range(abs_third, n):
        if i < len(diff) and i < len(dea):
            h = diff[i] - dea[i]
            if h < 0:
                area_back += abs(h)

    # 严格背驰: 后1/3面积 < 前1/3 * 0.6
    if area_front > 0 and area_back > 0:
        if area_back >= area_front * 0.6:
            return False
    else:
        return False

    # DIFF 拐头确认
    if n >= 5:
        recent_diff = [diff[i] for i in range(n-5, n) if i < len(diff)]
        if len(recent_diff) >= 3:
            if recent_diff[-1] <= recent_diff[-2]:
                return False

    return True


def backtest_one(symbol, sdt="20230101", edt="20260707", conn=None):
    clear_all()

    candidates = scan_60min(symbol, edt, conn=conn)
    if not candidates:
        return {'symbol': symbol, 'trades': []}

    # 缩短窗口: 一买用候选自带104周(一买后回踩A区可能隔很久, 如000591
    # 2024-10一买回踩在2026-07), 其余买点13周监控截断(3个月不进区过期)
    edt_dt = datetime.strptime(edt, "%Y%m%d")
    valid = []
    for c in candidates:
        sig_dt = datetime.strptime(c.signal_date[:10], "%Y-%m-%d")
        cand_end = datetime.strptime(c.window_end[:10], "%Y-%m-%d")
        if c.buy_type == '一买':
            window_end = cand_end
        else:
            window_end = min(cand_end, sig_dt + timedelta(weeks=SIGNAL_WINDOW_WEEKS))
        if FILTER_WEEKLY and c.weekly_dir == '向下' and c.buy_type != '三买':
            continue  # 精细化: 周线向下只滤一买/二买, 三买豁免(顺势离开结构)
        if window_end >= datetime.strptime(sdt, "%Y%m%d"):
            c.window_end = window_end.strftime("%Y-%m-%d")
            valid.append(c)
    candidates = valid

    if not candidates:
        return {'symbol': symbol, 'trades': []}

    bars = load_60min(symbol, conn=conn)
    if edt:
        edt_dt = datetime.strptime(edt, "%Y%m%d")
        bars = [b for b in bars if b.dt <= edt_dt]
    if len(bars) < 200:
        return {'symbol': symbol, 'trades': []}

    sdt_dt = datetime.strptime(sdt, "%Y%m%d")
    start_idx = next((i for i, b in enumerate(bars) if b.dt >= sdt_dt), 0)

    closes = [b.close for b in bars]
    diff, dea, _ = macd(closes)

    pos = None
    trades = []
    last_exit_idx = -999
    consumed = set()
    ENTRY_GAP = 20
    # 监控状态: 价格进入关键区(A*0.98~A*1.05)才激活
    # None=未进区, int=进区时的bar_index
    in_zone = {}
    # 结构破坏跟踪(课本): 破位即作废
    dead = set()          # 已破坏的候选
    leave_phase = {}      # 三买: 'wait_leave' -> 'pullback'

    for idx in range(start_idx, len(bars)):
        bar = bars[idx]
        cur = bar.close
        day_str = bar.dt.strftime("%Y-%m-%d")

        # ── 候选结构检查(破位作废, 课本) ──
        # 二买: 信号后任何bar低点 < A*0.97 → C破A, 结构破坏, 不允许破位反抽入场
        # 三买: L2完成后, 离开ZG后回试破ZG → 结构破坏
        for c in candidates:
            key = (c.buy_type, c.signal_date)
            if key in dead or day_str < c.signal_date[:10]:
                continue
            if c.buy_type == '一买' and c.a_price > 0:
                # 一买破位作废(2026-08-06 Phase1同步, 37/53课): 跌破A×0.995 → 作废
                if bar.low < c.a_price * 0.995:
                    dead.add(key)
            elif c.buy_type == '二买' and c.a_price > 0:
                if bar.low < c.a_price * 0.995:
                    dead.add(key)   # 破A: 二买不成立(课本: C不破A)
            elif c.buy_type == '三买':
                zg = c.zs_zg if c.zs_zg > 0 else (c.l2.zg if c.l2 else 0)
                if zg <= 0:
                    continue
                eff_start = max(c.signal_date[:10], c.zs_end_date[:10]) if c.zs_end_date else c.signal_date[:10]
                if day_str < eff_start:
                    continue  # L2中枢未完成, 不算离开
                ph = leave_phase.get(key)
                if ph is None:
                    ph = 'wait_leave'
                    leave_phase[key] = ph
                if ph == 'wait_leave':
                    if bar.high > zg:  # 离开ZG
                        leave_phase[key] = 'pullback'
                else:  # pullback
                    if bar.low < zg * 0.98:  # 回试破ZG: 三买不成立
                        dead.add(key)

        # ── 退出 ──
        if pos is not None:
            pnl = (cur - pos['entry_price']) / pos['entry_price']
            peak = max(pos.get('peak_price', pos['entry_price']), cur)
            pos['peak_price'] = peak
            exit_sig = None

            if pos.get('a_low') and cur < pos['a_low'] * 0.995:
                exit_sig = f"结构止损(A={pos['a_low']:.2f})"
            elif pos.get('entry_zg') and cur < pos['entry_zg'] * 0.97:
                exit_sig = f"结构止损(ZG={pos['entry_zg']:.2f})"
            if not exit_sig and pnl < -0.10:
                exit_sig = "止损10%"
            if not exit_sig and peak > pos['entry_price'] * 1.05:
                if cur < peak * 0.90:
                    exit_sig = "移动止盈10%"

            if exit_sig:
                trades.append({
                    'symbol': symbol, 'name': pos['name'],
                    'entry': pos['entry_dt'][:10], 'exit': day_str[:10],
                    'entry_price': pos['entry_price'], 'exit_price': cur,
                    'pnl_pct': round(pnl * 100, 2), 'hold': idx - pos['entry_idx'],
                    'reason': exit_sig, 'entry_reason': pos.get('entry_reason', ''),
                })
                pos = None
                last_exit_idx = idx
                continue

        # ── 入场 ──
        if pos is None and idx - last_exit_idx > ENTRY_GAP:
            if 'ST' in symbol or '退' in symbol:
                continue

            active = []
            for c in candidates:
                key = (c.buy_type, c.signal_date)
                if key in consumed or key in dead:
                    continue
                # 三买: L2中枢完成后才有效 (max(signal, zs_end))
                if c.buy_type == '三买' and c.zs_end_date:
                    start = max(c.signal_date[:10], c.zs_end_date[:10])
                else:
                    start = c.signal_date[:10]
                if start <= day_str[:10] <= c.window_end[:10]:
                    active.append(c)
            if not active:
                continue

            # 新信号作废旧信号(2026-08-06 Phase1同步, 49课): 一买只保留最新signal_date
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
                    if zg <= 0: continue

                    # 监控模式: 价格在ZG区才激活
                    key = (bc.buy_type, bc.signal_date)
                    if leave_phase.get(key) != 'pullback':
                        continue  # 未完成离开→回试结构, 不追突破
                    in_zg_zone = zg < cur <= zg * 1.05
                    if in_zg_zone:
                        if key not in in_zone or in_zone[key] is None:
                            in_zone[key] = idx
                    else:
                        in_zone[key] = None

                    if not in_zg_zone:
                        continue
                    # 锚定底分型: 分型最近确认, 分型低点在ZG区
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
                    if bc.a_price <= 0: continue
                    # A区收窄(2026-08-06 Phase1同步): 0.92~1.12 → 0.95~1.08
                    if cur < bc.a_price * 0.95 or cur > bc.a_price * 1.08:
                        continue
                    # 锚定底分型: 分型最近确认
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
                    entry_zg = 0; entry_a = bc.a_price; entry_b = 0

                elif bc.buy_type == '二买':
                    if bc.a_price <= 0: continue

                    key = (bc.buy_type, bc.signal_date)
                    A = bc.a_price          # 一买低点(结构破坏参考)
                    C = bc.c_price or A     # 回调低点(入场参考, 课本: 二买=回调低点确认)
                    in_a_zone = C * 0.98 <= cur <= C * 1.05

                    # 跟踪价格是否在C区(回调低点附近)
                    if in_a_zone:
                        if key not in in_zone or in_zone[key] is None:
                            in_zone[key] = idx  # 刚进入C区,标记开始
                    else:
                        in_zone[key] = None  # 离开C区,重置

                    # 入场条件: 当前在C区 + 底分型最近确认 + 分型在C区 + 力竭确认
                    if not in_a_zone:
                        continue
                    df_idx = _find_bottom_fractal_idx(sub_bars)
                    if df_idx < len(sub_bars) - 6:
                        continue  # 分型必须最近确认(错过不追)
                    df_low = sub_bars[df_idx].low
                    if not (C * 0.98 <= df_low <= C * 1.05):
                        continue  # 分型低点必须在C区附近
                    if bar.low < df_low:
                        continue  # 当前bar没破分型低点
                    if not _divergence_at_fractal(sub_bars, diff[:idx+1], dea[:idx+1], df_idx):
                        continue  # 力竭确认(面积比+拐头, 锚定分型)

                    fired = True; pname = '二买'
                    entry_zg = 0; entry_a = A; entry_b = bc.b_price

                if fired:
                    consumed.add((bc.buy_type, bc.signal_date))
                    in_zone.pop((bc.buy_type, bc.signal_date), None)
                    break

            if fired:
                pos = {
                    'name': pname, 'entry_price': cur,
                    'entry_idx': idx,
                    'entry_dt': bar.dt.strftime("%Y-%m-%d %H:%M"),
                    'peak_price': cur,
                    'a_low': entry_a, 'b_high': entry_b,
                    'entry_zg': entry_zg,
                    'entry_reason': f"60min L2 {pname}",
                }

    if pos:
        last_bar = bars[-1]
        pnl = (last_bar.close - pos['entry_price']) / pos['entry_price']
        trades.append({
            'symbol': symbol, 'name': pos['name'],
            'entry': pos['entry_dt'][:10],
            'exit': last_bar.dt.strftime("%Y-%m-%d"),
            'entry_price': pos['entry_price'], 'exit_price': last_bar.close,
            'pnl_pct': round(pnl * 100, 2),
            'hold': len(bars) - 1 - pos['entry_idx'],
            'reason': '回测结束', 'entry_reason': pos.get('entry_reason', ''),
        })

    return {'symbol': symbol, 'trades': trades}


def run(symbols, sdt="20230101", edt="20260707"):
    results = defaultdict(list)
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    from chan.confirm_60min import clear_60min_cache
    for i, sym in enumerate(symbols):
        clear_all(); clear_60min_cache()
        try:
            r = backtest_one(sym, sdt, edt, conn=conn)
            for t in r['trades']:
                results[t['name']].append(t)
        except Exception as e:
            pass
        if (i + 1) % 10 == 0:
            total = sum(len(v) for v in results.values())
            print(f"  [{i+1}/{len(symbols)}] {total} trades ({time.time()-t0:.0f}s)", flush=True)
    conn.close()
    return dict(results)


def summary(results: dict):
    print(f"\n{'='*60}")
    print(f"  60min L2中枢 回测")
    for name in ['一买', '二买', '三买']:
        ts = results.get(name, [])
        if not ts: print(f"  {name}: 0"); continue
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
    conn = sqlite3.connect(DB_PATH)
    stocks = [r[0] for r in conn.execute(
        "SELECT DISTINCT code FROM kline_60min ORDER BY code LIMIT 50"
    ).fetchall()]
    conn.close()
    print(f"60min回测: {len(stocks)}只 仅2026年")
    results = run(stocks, sdt='20260101')
    summary(results)
    print(f"\n── 明细 ──")
    for name in ['一买','二买','三买']:
        for t in sorted(results.get(name,[]), key=lambda t: t['pnl_pct'], reverse=True):
            print(f"  {t['symbol']} {name} {t['entry']}->{t['exit']} {t['pnl_pct']:+.1f}% {t['reason']}")
