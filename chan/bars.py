#!/usr/bin/env python3
"""基础数据结构：RawBar, MACD, EMA

完全自实现，零外部依赖。对照 CZSC 源码和 108 课标准。

⚠️ 算法文档: /root/data/backtest/ALGORITHM.md (第2.1节)
   修改影响算法时必须同步更新 ALGORITHM.md。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Tuple


@dataclass
class RawBar:
    """K线。对照 czsc.RawBar。"""
    symbol: str
    dt: datetime
    open: float
    high: float
    low: float
    close: float
    vol: float = 0.0
    amount: float = 0.0
    id: int = 0


def ema(data: List[float], period: int) -> List[float]:
    """EMA。对照 talib.EMA / czsc._ema_250705。"""
    if len(data) < period:
        return [sum(data) / len(data)] * len(data) if data else []
    result = [sum(data[:period]) / period] * period
    k = 2.0 / (period + 1)
    for i in range(period, len(data)):
        result.append(data[i] * k + result[-1] * (1 - k))
    return result


def macd(closes: List[float], fast=12, slow=26, signal=9) -> Tuple[List[float], List[float], List[float]]:
    """MACD。对照 czsc._macd_250705。
    Returns (diff, dea, hist): hist = diff - dea
    """
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    diff = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema(diff, signal)
    hist = [d - e for d, e in zip(diff, dea)]
    return diff, dea, hist


def macd_area(diff, dea, start_idx, end_idx, direction='down') -> float:
    """MACD柱子面积。Ch24: 比较柱子面积而非DIFF之和。
    direction='down': 只累积绿柱(hist<0), 'up': 只累积红柱(hist>0)
    """
    total = 0.0
    for i in range(max(0, start_idx), min(end_idx + 1, len(diff))):
        if i >= len(dea):
            break
        h = diff[i] - dea[i]
        if direction == 'down' and h < 0:
            total += h  # 负值 = 下跌力度
        elif direction == 'up' and h > 0:
            total += h  # 正值 = 上涨力度
    return total
