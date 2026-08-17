#!/usr/bin/env python3
"""中枢检测。Ch18, Ch20。

中枢 = 至少三个连续次级别(笔)走势重叠的部分。
ZG = min(重叠段高点的最小值) / ZD = max(重叠段低点的最大值)

⚠️ 算法文档: /root/data/backtest/ALGORITHM.md (第2.4节)
   修改影响算法时必须同步更新 ALGORITHM.md。
"""

from dataclasses import dataclass
from typing import List
from chan.bi import Bi
from chan.state import get_zs, set_zs

# Phase3 隔离实验开关(2026-08-06):
# True = 完全穿越停止延伸(20课定理一: Zn使dn>ZG或gn<ZD → 中枢结束)
#       修复"突破段并入中枢"缺陷(000537向下中枢GG=13.76含+62%主升段)
# False = 原逻辑(共同重叠区不消失就继续扩展) ← 默认
# ⚠️ ITERATION_LOG记录: 此前尝试过此修复, 副作用大(L1 42→62等), 已回滚;
#    本开关用于隔离实验对比, 副作用可控后再考虑合并
LEAVE_STOP = False
AMP_LIMIT = 1.30   # Phase3: 突破幅度限制(up笔高点>ZG×AMP_LIMIT 或 down笔低点<ZD/AMP_LIMIT → 中枢结束)


@dataclass
class Zhongshu:
    zg: float
    zd: float
    dd: float
    gg: float
    sdir: str
    start_dt: str
    end_dt: str
    occur_at: str = ''      # Phase R阶段3: 结构发生时间(第3笔结束)
    confirm_at: str = ''    # Phase R阶段3: 实际确认时间(第3笔确认)
    member_start_idx: int = -1  # 在输入笔序列中的首成员索引
    member_end_idx: int = -1    # 在输入笔序列中的末成员索引（含）
    is_complete: bool = False   # 已有一笔离开当前重叠区，中心边界不再延伸
    leave_confirm_at: str = ''  # 离开笔的确认时点；完整中枢的因果确认时间
    initial_bounds: object = None  # 最初三笔形成时的ZD/ZG/DD/GG审计快照
    member_bis: object = None      # 实际参与重叠/延伸的笔（只读审计引用）

    def __repr__(self):
        return f"ZS({self.sdir} zg={self.zg:.2f} zd={self.zd:.2f})"


CACHE_ENABLED = True   # 缓存总开关(Phase R验证: False=冷计算)


def build_zs(bi_list: List[Bi], level: str = 'D') -> List[Zhongshu]:
    """从笔列表构建中枢，带缓存。level: 'W'=周线 'D'=日线
    Phase R阶段6: key=(level, symbol, len)——避免跨股票串缓存"""
    if not CACHE_ENABLED:
        return _build_zs(bi_list)
    sym = bi_list[0]._bars[0].symbol if bi_list and bi_list[0]._bars else ''
    # Phase R阶段6: key加末笔end——cursor增量场景len相同但内容可能变(笔破坏回退)
    last_end = bi_list[-1].end_dt if bi_list else ''
    key = (level, sym, len(bi_list), last_end)
    cached = get_zs(level, key)
    if cached is not None:
        return cached
    result = _build_zs(bi_list)
    set_zs(level, key, result)
    return result


def _build_zs(bi_list: List[Bi]) -> List[Zhongshu]:
    """从笔列表构建中枢。对照 CZSC._zs_250705。"""
    if len(bi_list) < 3:
        return []

    zs_list = []
    i = 0

    while i < len(bi_list) - 2:
        b0, b1, b2 = bi_list[i], bi_list[i + 1], bi_list[i + 2]
        hs = [b0.high, b1.high, b2.high]
        ls = [b0.low, b1.low, b2.low]
        zg = min(hs)
        zd = max(ls)

        if zg <= zd:
            i += 1
            continue

        j = i + 3
        while j < len(bi_list):
            bj = bi_list[j]
            # 完全穿越停止延伸(Phase3实验, 20课定理一):
            # 突破段不再并入中枢。两种判定(避免"从内部启动的突破笔"漏判):
            # ① 笔完全离开: up笔低点>ZG / down笔高点<ZD(严格)
            # ② 突破幅度超限: up笔高点>ZG×1.30 / down笔低点<ZD×0.70
            #    (000537案例: 笔从ZD附近启动直拉13.76, 低点8.29<ZG=8.50,
            #     判定①漏判, 但GG扩展+62%远超30%阈值 → 判定②截断)
            if LEAVE_STOP:
                if bj.direction == 'up' and (bj.low > zg or bj.high > zg * AMP_LIMIT):
                    break
                if bj.direction == 'down' and (bj.high < zd or bj.low < zd / AMP_LIMIT):
                    break
            nzg = min(hs + [bj.high])
            nzd = max(ls + [bj.low])
            if nzg <= nzd:
                break
            hs.append(bj.high)
            ls.append(bj.low)
            j += 1

        # 中枢方向由入口笔决定（进入重叠区的那笔），不是重叠区第一笔
        entry_bi = bi_list[i-1] if i > 0 else b0
        sdir = '向下' if entry_bi.direction == 'down' else '向上'
        is_complete = j < len(bi_list)
        leave_bi = bi_list[j] if is_complete else None
        leave_confirm_at = ''
        if leave_bi is not None:
            leave_confirm_at = (getattr(leave_bi, 'confirm_at', '') or
                                leave_bi.end_dt)
        zs = Zhongshu(
            zg=float(min(hs)), zd=float(max(ls)),  # P1:ZG/ZD用扩展后hs/ls
            gg=float(max(hs)), dd=float(min(ls)),
            sdir=sdir,
            start_dt=bi_list[i].start_dt,
            end_dt=bi_list[j - 1].end_dt,
            occur_at=bi_list[i + 2].end_dt,
            confirm_at=bi_list[i + 2].get('confirm_at', bi_list[i + 2].end_dt) if hasattr(bi_list[i + 2], 'get') else (bi_list[i + 2].confirm_at or bi_list[i + 2].end_dt),
            member_start_idx=i,
            member_end_idx=j - 1,
            is_complete=is_complete,
            leave_confirm_at=leave_confirm_at,
            initial_bounds={
                'status': 'KNOWN',
                'ZD': float(max(ls[:3])), 'ZG': float(min(hs[:3])),
                'DD': float(min(ls[:3])), 'GG': float(max(hs[:3])),
            },
            member_bis=tuple(bi_list[i:j]),
        )
        zs_list.append(zs)
        i = j

    return zs_list
