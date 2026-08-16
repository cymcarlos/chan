#!/usr/bin/env python3
"""周线过滤 — 大级别方向(区间套最外层)。

课本 Ch49: "没必要参与操作级别及以上级别的下跌与超过操作级别的盘整"。
操作级别 = 日线, 周线是日线的上级 → 周线下跌时, 日线买点不参与。

构建: 周线K线 → 周线笔(level='W') → 周线笔中枢 → 最近走势方向。

⚠️ 算法文档: /root/data/backtest/ALGORITHM.md (第3.4节)
   修改影响算法时必须同步更新 ALGORITHM.md。
"""
import os
import sqlite3
import sys
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chan.bars import RawBar
from chan.state import clear_level
from chan.bi import build_bi
from chan.zhongshu import build_zs

DB_PATH = '/root/data/backtest/kline.db'


def load_weekly(symbol: str, conn=None) -> list:
    """加载周线K线(带会话缓存)。"""
    own = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        own = True
    rows = conn.execute(
        "SELECT code, date, open, high, low, close, vol, amount "
        "FROM kline_weekly WHERE code=? ORDER BY date", (symbol,)
    ).fetchall()
    if own:
        conn.close()
    bars = []
    for i, r in enumerate(rows):
        try:
            dt = datetime.strptime(str(r[1])[:8], "%Y%m%d")
        except (ValueError, IndexError):
            continue
        try:
            o, h, l, c = float(r[2] or 0), float(r[3] or 0), float(r[4] or 0), float(r[5] or 0)
        except (ValueError, IndexError):
            continue
        bars.append(RawBar(symbol=r[0], id=i, dt=dt, open=o, high=h, low=l, close=c,
                           vol=float(r[6] or 0), amount=float(r[7] or 0)))
    return bars


def weekly_direction(symbol: str, conn=None) -> str:
    """最近周线走势方向: '向上' / '向下' / '盘整' / '无数据'。

    规则: 取最近一个"完成"的周线笔中枢方向(最后一个中枢的 sdir)。
    若最近中枢是向下 → 周线下跌中, 日线买点应被过滤(Ch49)。
    """
    clear_level('W')
    bars = load_weekly(symbol, conn=conn)
    if len(bars) < 30:
        return '无数据'
    bis = build_bi(bars, level='W')
    if len(bis) < 5:
        return '无数据'
    zs = build_zs(bis, level='W')
    if not zs:
        return '无数据'
    last = zs[-1]
    # 最后一个中枢结束距现在多久? 太旧(>1年)则参考倒数第二个
    try:
        last_end = datetime.strptime(last.end_dt[:10], "%Y-%m-%d")
        now = bars[-1].dt
        months = (now - last_end).days / 30
    except Exception:
        months = 0
    if months > 12 and len(zs) >= 2:
        last = zs[-2]
    return last.sdir  # '向上' / '向下'


if __name__ == '__main__':
    import sys
    for sym in ['000591.SZ', '001282.SZ', '000895.SZ', '000566.SZ']:
        print(f'{sym}: 周线方向 = {weekly_direction(sym)}')


def weekly_direction_at(symbol: str, date_str: str, conn=None) -> str:
    """信号日时点的周线方向(历史时点查询, 回测用)。

    取 date 之前最近完成的一个周线中枢方向; 若无 → '无数据'。
    """
    # 不 clear_level('W'): build_bi/build_zs 自带缓存, 同股票多候选只构建一次
    bars = load_weekly(symbol, conn=conn)
    if len(bars) < 30:
        return '无数据'
    bis = build_bi(bars, level='W')
    if len(bis) < 5:
        return '无数据'
    zs = build_zs(bis, level='W')
    if not zs:
        return '无数据'
    # 找 date 之前最近结束的中枢
    target = datetime.strptime(date_str[:10], "%Y-%m-%d")
    done = [z for z in zs if datetime.strptime(z.end_dt[:10], "%Y-%m-%d") <= target]
    if not done:
        return '无数据'
    return done[-1].sdir


def weekly_direction_at(symbol: str, date_str: str, conn=None) -> str:
    """信号日时点的周线方向(历史时点查询, 回测用)。

    取 date 之前最近完成的一个周线中枢方向; 若无 → '无数据'。
    """
    clear_level('W')
    bars = load_weekly(symbol, conn=conn)
    if len(bars) < 30:
        return '无数据'
    bis = build_bi(bars, level='W')
    if len(bis) < 5:
        return '无数据'
    zs = build_zs(bis, level='W')
    if not zs:
        return '无数据'
    target = datetime.strptime(date_str[:10], "%Y-%m-%d")
    done = [z for z in zs if datetime.strptime(z.end_dt[:10], "%Y-%m-%d") <= target]
    if not done:
        return '无数据'
    return done[-1].sdir
