#!/usr/bin/env python3
"""分型检测 — 包含处理 + 顶底分型。Ch62-65。对照 CZSC analyze.py。

⚠️ 算法文档: 仓库根 ALGORITHM.md (第2.2节)
   修改影响算法时必须同步更新 ALGORITHM.md。
"""

import copy
from typing import List
from chan.bars import RawBar


def merge_bars(bars: List[RawBar]) -> List[RawBar]:
    """包含处理 Ch62。对照 CZSC remove_include + CZSC.update。

    关键规则:
    - k1, k2 是最后两根无包含K线, 不随合并改变
    - 方向只用 HIGH 判断: k1.high < k2.high → up, > → down
    """
    if len(bars) < 2:
        return [copy.copy(b) for b in bars]

    ubi = []  # 无包含K线序列

    for bar in bars:
        if len(ubi) < 2:
            # 前两根直接加入
            ubi.append(_raw_to_merged(bar))
        else:
            k1, k2 = ubi[-2], ubi[-1]
            has_include, k3 = _remove_include(k1, k2, bar)
            if has_include:
                ubi[-1] = k3
            else:
                ubi.append(k3)

    return ubi


def _raw_to_merged(bar: RawBar) -> RawBar:
    """原始K线转合并K线（单根无包含）。"""
    return copy.copy(bar)


def _remove_include(k1: RawBar, k2: RawBar, k3_raw: RawBar):
    """去除包含关系。对照 CZSC remove_include。

    k1, k2: 最后两根无包含K线 (k1在前, k2在后)
    k3_raw: 新来的原始K线
    返回: (has_include, merged_k3)
    """
    k3 = copy.copy(k3_raw)

    # 方向: 只用 HIGH 判断 (CZSC行35-38)
    if k1.high < k2.high:
        direction = 'up'
    elif k1.high > k2.high:
        direction = 'down'
    else:
        # high相等 → 方向不定, 不合并
        return False, k3

    # 判断 k2 和 k3 是否存在包含关系 (CZSC行45)
    k2_contains_k3 = k2.high >= k3.high and k2.low <= k3.low
    k3_contains_k2 = k2.high <= k3.high and k2.low >= k3.low

    if not (k2_contains_k3 or k3_contains_k2):
        # 无包含 → 直接前进
        return False, k3

    # 按方向合并 (CZSC行47-68)
    if direction == 'up':
        # CZSC: dt 取贡献更高 high 的那根 bar
        if k2.high > k3.high:
            k3.dt = k2.dt
        k3.high = max(k2.high, k3.high)
        k3.low = max(k2.low, k3.low)
    else:  # down
        # CZSC: dt 取贡献更低 low 的那根 bar
        if k2.low < k3.low:
            k3.dt = k2.dt
        k3.high = min(k2.high, k3.high)
        k3.low = min(k2.low, k3.low)

    return True, k3


def find_fractals(bars: List[RawBar], tail: int = None) -> List[dict]:
    """找分型。对照 CZSC check_fx: 顶底都需要 HIGH+LOW 双重条件。
    tail: 只扫描末尾N根(增量update用, 分型只由末尾3根决定); None=全量。"""
    if len(bars) < 5:
        return []
    if tail is not None:
        bars = bars[-tail:]

    fractals = []
    for i in range(2, len(bars)):
        b0, b1, b2 = bars[i - 2], bars[i - 1], bars[i]

        # 顶分型: high1最高 AND low1最高 (CZSC行96)
        # Phase R阶段3: confirm_at=识别时刻(第3根K线时间=形态完成可确认)
        if b1.high > b0.high and b1.high > b2.high and \
           b1.low > b0.low and b1.low > b2.low:
            fractals.append({'type': 'top', 'price': b1.high, 'bar': b1,
                           'dt': str(b1.dt),
                           'confirm_at': str(b2.dt)})

        # 底分型: low1最低 AND high1最低 (CZSC行100)
        elif b1.low < b0.low and b1.low < b2.low and \
             b1.high < b0.high and b1.high < b2.high:
            fractals.append({'type': 'bottom', 'price': b1.low, 'bar': b1,
                           'dt': str(b1.dt),
                           'confirm_at': str(b2.dt)})

    return fractals
