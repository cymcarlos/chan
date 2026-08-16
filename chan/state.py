#!/usr/bin/env python3
"""级别感知的状态管理 — 各级别(D/W/60)独立的笔/中枢缓存。

bi.py 和 zhongshu.py 原先用全局 dict keyed by symbol，导致同 symbol
跑完一个级别再跑另一个会互相覆盖。现在改为 (level, key) 复合 key。
60min 管线中: 同一进程内逐股回测时必须调用 clear_all() 清缓存,
避免上一只股票的笔/中枢状态串到下一只。

⚠️ 算法文档: /root/data/backtest/ALGORITHM.md (第2.5节)
   修改影响算法时必须同步更新 ALGORITHM.md。
"""

from typing import Dict, Optional, List, Tuple

# bi 增量状态: key = (level, symbol)
_bi_state: Dict[Tuple[str, str], dict] = {}

# zhongshu 缓存: key = (level, bi_len)
_zs_cache: Dict[Tuple[str, int], list] = {}


def get_bi_state(level: str, symbol: str) -> dict:
    """获取笔构建的增量状态"""
    key = (level, symbol)
    if key not in _bi_state:
        _bi_state[key] = {'last_len': 0, 'fractals': [], 'bis': []}
    return _bi_state[key]


def set_bi_state(level: str, symbol: str, state: dict) -> None:
    """设置笔构建的增量状态"""
    _bi_state[(level, symbol)] = state


def reset_bi_state(level: str, symbol: str) -> None:
    """重置某 symbol 在某级别的笔状态（强制全量重建）"""
    key = (level, symbol)
    if key in _bi_state:
        del _bi_state[key]


def get_zs(level: str, bi_len: int) -> Optional[list]:
    """获取缓存的中枢结果"""
    return _zs_cache.get((level, bi_len))


def set_zs(level: str, bi_len: int, result: list) -> None:
    """缓存中枢结果"""
    _zs_cache[(level, bi_len)] = result


def clear_level(level: str) -> None:
    """清理某个级别的所有缓存（symbol 间切换时用）"""
    keys_to_del = [k for k in _bi_state if k[0] == level]
    for k in keys_to_del:
        del _bi_state[k]
    keys_to_del = [k for k in _zs_cache if k[0] == level]
    for k in keys_to_del:
        del _zs_cache[k]


def clear_all() -> None:
    """清理所有缓存"""
    _bi_state.clear()
    _zs_cache.clear()
