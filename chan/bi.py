#!/usr/bin/env python3
"""笔的构建 — 顶底交替。Ch62-65。自实现，算法逻辑复刻 CZSC。

⚠️ 算法文档: 仓库根 ALGORITHM.md (第2.3节)
   修改影响算法时必须同步更新 ALGORITHM.md。
   笔破坏回退是 CZSC 官方注释点名的易错处, 改动需对比 czsc __update_bi。
"""

from dataclasses import dataclass, field
from typing import List
from chan.bars import RawBar
from chan.state import get_bi_state, set_bi_state


@dataclass
class Bi:
    direction: str
    start_price: float
    end_price: float
    high: float
    low: float
    start_dt: str
    end_dt: str
    occur_at: str = ''          # Phase R阶段3: 结构发生时间(终点分型形态形成)
    confirm_at: str = ''        # Phase R阶段3: 实际确认时间(终点分型第3根K线)
    _bars: List[RawBar] = field(default_factory=list, repr=False)
    _start_fx: dict = field(default_factory=dict, repr=False)  # 起点分型
    _end_fx: dict = field(default_factory=dict, repr=False)    # 终点分型


def _fx_dict(fx: dict) -> dict:
    """分型 dict 规范化(去掉 bar 引用, 存价格区间, 供锚点复用)。"""
    return {
        'type': fx['type'],
        'price': fx['price'],
        'dt': fx['dt'],
        'high': fx['bar'].high,
        'low': fx['bar'].low,
    }


def _check_bi(bars_ubi: List[RawBar], min_bi_len: int = 6, anchor: dict = None):
    """成笔检测。

    CZSC check_bi 原版: 取 ubi 第一个分型 + 最极端对面分型。
    缺陷: 成笔后 ubi 截断到终点分型前一根, 若该处恰好是对面类型的
    分型(如底分型的 left 是顶分型), fxs[0] 会漂移, 导致下一笔从
    中间开始, 产生连续同向笔(60min 级别大量出现)。

    修复(锚点机制): 传 anchor = 上一笔终点分型, 用它作为 fx_a 固定锚,
    对面分型必须在其之后, 起点不再漂移。
    """
    from chan.fractal import find_fractals

    fxs = find_fractals(bars_ubi, tail=30)
    if len(fxs) < 1:
        return None, bars_ubi

    # Phase R性能: 分型都在末尾30根内, 查找限窗口(避免O(n)每根bar)
    # 注意: 锚点分支的a0_idx——锚点=上一笔终点分型, ubi成笔后截断到
    # 终点前一根, 锚点在ubi【开头】——必须用开头窗口(2026-08-10修复)
    def _ubi_idx(dt_str, ge=False, head=False):
        if head:
            rng = range(0, min(40, len(bars_ubi)))
            default = 0
        else:
            rng = range(max(0, len(bars_ubi) - 40), len(bars_ubi))
            default = len(bars_ubi) - 1
        for j in rng:
            d = str(bars_ubi[j].dt)[:10]
            if (d >= dt_str[:10]) if ge else (d == dt_str[:10]):
                return j
        return default

    if anchor is not None:
        # ── 锚点分支: fx_a 固定为上一笔终点分型 ──
        # (2026-08-11 P0修复: 原"最高顶优先"在最高顶笔长不足(<min_bi_len)时
        #  永久死锁, 直到tail=30窗口把最高顶挤出才巧合成笔 → 卖点延迟10-12天。
        #  62课本义: "顶和底之间的其他波动都可以忽略不算" → 笔终点=最后确认的
        #  分型, 笔内更高点只是笔内high。改为: 从最极端分型起按序降级,
        #  取第一个满足笔长的候选 —— 000066: 04-10顶17.30(4根<6)降级到
        #  05-27顶15.99(成笔, h=17.30保留笔内高点))
        fx_a = anchor
        if fx_a['type'] == 'bottom':
            candidates = [f for f in fxs if f['type'] == 'top'
                          and f['price'] > fx_a['price']
                          and f['dt'] > fx_a['dt']]
            if not candidates:
                return None, bars_ubi
            candidates.sort(key=lambda f: f['price'], reverse=True)  # 顶: 高→低
        else:
            candidates = [f for f in fxs if f['type'] == 'bottom'
                          and f['price'] < fx_a['price']
                          and f['dt'] > fx_a['dt']]
            if not candidates:
                return None, bars_ubi
            candidates.sort(key=lambda f: f['price'])               # 底: 低→高

        fx_b = None
        for _cand in candidates:
            _b2 = min(len(bars_ubi) - 1, _ubi_idx(_cand['dt']) + 1)
            _a0 = _ubi_idx(fx_a['dt'], ge=True, head=True)
            if len(bars_ubi[_a0:_b2 + 1]) >= min_bi_len:
                fx_b = _cand
                break
        if fx_b is None:
            return None, bars_ubi

        h_a, l_a = fx_a['high'], fx_a['low']
        h_b, l_b = fx_b['bar'].high, fx_b['bar'].low
        ab_include = (h_a > h_b and l_a < l_b) or (h_a < h_b and l_a > l_b)

        # 锚点可能已被包含处理合并, 起点取锚点时间后的第一根
        a0_idx = _ubi_idx(fx_a['dt'], ge=True, head=True)
        fx_b_idx = _ubi_idx(fx_b['dt'])
        b2_idx = min(len(bars_ubi) - 1, fx_b_idx + 1)
        b0_idx = max(0, fx_b_idx - 1)

        full_bars = bars_ubi[a0_idx:b2_idx + 1]
        remaining = bars_ubi[b0_idx:]

        if (not ab_include) and len(full_bars) >= min_bi_len:
            direction = 'up' if fx_a['type'] == 'bottom' else 'down'
            return {
                'direction': direction,
                'start_price': fx_a['price'], 'end_price': fx_b['price'],
                'start_dt': fx_a['dt'], 'end_dt': fx_b['dt'],
            'occur_at': fx_b['dt'],
            'confirm_at': fx_b.get('confirm_at', fx_b['dt']),
                'high': max(b.high for b in full_bars),
                'low': min(b.low for b in full_bars),
                '_bars': full_bars,
                'start_fx': fx_a, 'end_fx': _fx_dict(fx_b),
            }, remaining
        return None, bars_ubi

    # ── 原版分支: 第一个分型 + 最极端对面分型(用于第一笔) ──
    if len(fxs) < 2:
        return None, bars_ubi
    fx_a = fxs[0]
    if fx_a['type'] == 'bottom':
        candidates = [f for f in fxs if f['type'] == 'top' and f['price'] > fx_a['price']]
        if not candidates:
            return None, bars_ubi
        fx_b = max(candidates, key=lambda f: f['price'])
    else:
        candidates = [f for f in fxs if f['type'] == 'bottom' and f['price'] < fx_a['price']]
        if not candidates:
            return None, bars_ubi
        fx_b = min(candidates, key=lambda f: f['price'])

    h_a, l_a = fx_a['bar'].high, fx_a['bar'].low
    h_b, l_b = fx_b['bar'].high, fx_b['bar'].low
    ab_include = (h_a > h_b and l_a < l_b) or (h_a < h_b and l_a > l_b)

    fx_a_idx = _ubi_idx(fx_a['dt'])
    fx_b_idx = _ubi_idx(fx_b['dt'])

    a0_idx = max(0, fx_a_idx - 1)
    b2_idx = min(len(bars_ubi) - 1, fx_b_idx + 1)
    b0_idx = max(0, fx_b_idx - 1)

    full_bars = bars_ubi[a0_idx:b2_idx + 1]     # 完整跨度 = CZSC bars_a
    remaining = bars_ubi[b0_idx:]               # 剩余 K 线

    # CZSC: min_bi_len 检查用完整跨度（等价于 len(bars_a) >= min_bi_len）
    if (not ab_include) and len(full_bars) >= min_bi_len:
        direction = 'up' if fx_a['type'] == 'bottom' else 'down'

        return {
            'direction': direction,
            'start_price': fx_a['price'], 'end_price': fx_b['price'],
            'start_dt': fx_a['dt'], 'end_dt': fx_b['dt'],
            'occur_at': fx_b['dt'],
            'confirm_at': fx_b.get('confirm_at', fx_b['dt']),
            'high': max(b.high for b in full_bars),
            'low': min(b.low for b in full_bars),
            '_bars': full_bars,
            'start_fx': _fx_dict(fx_a), 'end_fx': _fx_dict(fx_b),
        }, remaining
    return None, bars_ubi


def _build_bi(bars: List[RawBar], min_bi_len: int = 6) -> List[Bi]:
    """自实现，复刻 CZSC 笔构建流程。

    CZSC.update() 逐根原始K线 → 包含处理 → __update_bi():
      1. check_bi() 尝试找新笔 → 找到则截断 ubi
      2. 笔破坏: 价格延伸超出最后一笔 → pop + 恢复 ubi
    """
    from chan.fractal import _remove_include, _raw_to_merged, find_fractals

    bis: List[Bi] = []
    ubi: List[RawBar] = []
    anchor = None  # 锚点 = 上一笔终点分型(防起点漂移)

    def _append_bi(bi_data: dict) -> None:
        """按 bi_data 追加一笔, 并维护锚点。"""
        nonlocal anchor
        bi = Bi(**{k: v for k, v in bi_data.items()
                   if k not in ('_bars', 'start_fx', 'end_fx')})
        bi._bars = bi_data['_bars']
        bi._start_fx = bi_data.get('start_fx', {})
        bi._end_fx = bi_data.get('end_fx', {})
        bis.append(bi)
        anchor = bi._end_fx or None

    for bar in bars:
        # ── 包含处理（等同 CZSC update:270-283）──
        if len(ubi) < 2:
            ubi.append(_raw_to_merged(bar))
        else:
            k1, k2 = ubi[-2], ubi[-1]
            has_include, k3 = _remove_include(k1, k2, bar)
            if has_include:
                ubi[-1] = k3
            else:
                ubi.append(k3)

        if len(ubi) < 3:
            continue

        # ── __update_bi: 第一笔 (CZSC:215-233) ──
        if not bis:
            fxs = find_fractals(ubi)
            if not fxs:
                continue
            fx_a = fxs[0]
            for fx in fxs:
                if fx['type'] == fx_a['type']:
                    if (fx_a['type'] == 'bottom' and fx['price'] <= fx_a['price']) or \
                       (fx_a['type'] == 'top' and fx['price'] >= fx_a['price']):
                        fx_a = fx
            fx_idx = next((j for j, b in enumerate(ubi)
                          if str(b.dt)[:10] == fx_a['dt']), 0)
            start_idx = max(0, fx_idx - 1)
            ubi = ubi[start_idx:]

            bi_data, ubi = _check_bi(ubi, min_bi_len)
            if bi_data is not None:
                _append_bi(bi_data)
            continue

        # ── __update_bi: 后续笔 (CZSC:239-252) ──
        # Step 1: check_bi 尝试找新笔(带锚点, 起点不漂移)
        bi_data, ubi = _check_bi(ubi, min_bi_len, anchor)
        if bi_data is not None:
            _append_bi(bi_data)

        # Step 2: 笔破坏 — 价格延伸超出最后一笔 → pop + 恢复 ubi
        # CZSC: 向上笔破高 / 向下笔破低
        if bis and ubi:
            last_bi = bis[-1]
            if (last_bi.direction == 'up' and ubi[-1].high > last_bi.high) or \
               (last_bi.direction == 'down' and ubi[-1].low < last_bi.low):
                bars_of_bi = last_bi._bars
                if len(bars_of_bi) >= 2:
                    cutoff_dt = str(bars_of_bi[-2].dt)[:10]
                    cut_idx = next((j for j, b in enumerate(ubi)
                                   if str(b.dt)[:10] >= cutoff_dt), len(ubi))
                    restored = list(bars_of_bi[:-2]) + ubi[cut_idx:]
                    ubi = restored
                bis.pop()
                # 锚点回退到被 pop 笔的起点分型(延伸后从原起点重新成笔)
                anchor = last_bi._start_fx or None

    return bis


class BiCursor:
    """逐根K线增量笔分析器(Phase R阶段5)。

    维护 ubi(无包含K线)/anchor(锚点)/bis 状态, 每根新K线只更新局部——
    等价于 _build_bi(全量) 的结果(有断言验证)。
    用于回测bar walk: 30min逐根更新(O(1)级), 避免每次全量重算(0.9s)。
    """
    def __init__(self, min_bi_len: int = 6):
        from chan.fractal import _remove_include, _raw_to_merged, find_fractals
        self._ri = _remove_include
        self._raw = _raw_to_merged
        self._ff = find_fractals
        self.min_bi_len = min_bi_len
        self.ubi = []
        self.anchor = None
        self.bis = []
        self.n = 0
        self._fx_init_done = False   # 第一笔初始化完成(避免反复全量扫分型)

    def _append_bi(self, bi_data: dict) -> None:
        bi = Bi(**{k: v for k, v in bi_data.items()
                   if k not in ('_bars', 'start_fx', 'end_fx')})
        bi._bars = bi_data['_bars']
        bi._start_fx = bi_data.get('start_fx', {})
        bi._end_fx = bi_data.get('end_fx', {})
        self.bis.append(bi)
        self.anchor = bi._end_fx or None

    def update(self, bar) -> None:
        """喂入一根新K线(必须按时间顺序, 与_build_bi循环体等价)"""
        self.n += 1
        if len(self.ubi) < 2:
            self.ubi.append(self._raw(bar))
        else:
            k1, k2 = self.ubi[-2], self.ubi[-1]
            has_include, k3 = self._ri(k1, k2, bar)
            if has_include:
                self.ubi[-1] = k3
            else:
                self.ubi.append(k3)
        if len(self.ubi) < 3:
            return
        if not self.bis:
            if not self._fx_init_done:
                # 第一次: 全量找第一个分型并截断ubi(只执行一次)
                fxs = self._ff(self.ubi)
                if not fxs:
                    return
                fx_a = fxs[0]
                for fx in fxs:
                    if fx['type'] == fx_a['type']:
                        if (fx_a['type'] == 'bottom' and fx['price'] <= fx_a['price']) or \
                           (fx_a['type'] == 'top' and fx['price'] >= fx_a['price']):
                            fx_a = fx
                fx_idx = next((j for j, b in enumerate(self.ubi)
                              if str(b.dt)[:10] == fx_a['dt']), 0)
                start_idx = max(0, fx_idx - 1)
                self.ubi = self.ubi[start_idx:]
                self._fx_init_done = True
            # 初始化后: 只用_check_bi(内部find_fractals tail=30, O(1))
            bi_data, self.ubi = _check_bi(self.ubi, self.min_bi_len)
            if bi_data is not None:
                self._append_bi(bi_data)
            return
        bi_data, self.ubi = _check_bi(self.ubi, self.min_bi_len, self.anchor)
        if bi_data is not None:
            self._append_bi(bi_data)
        if self.bis and self.ubi:
            last_bi = self.bis[-1]
            if (last_bi.direction == 'up' and self.ubi[-1].high > last_bi.high) or \
               (last_bi.direction == 'down' and self.ubi[-1].low < last_bi.low):
                bars_of_bi = last_bi._bars
                if len(bars_of_bi) >= 2:
                    cutoff_dt = str(bars_of_bi[-2].dt)[:10]
                    cut_idx = next((j for j, b in enumerate(self.ubi)
                                   if str(b.dt)[:10] >= cutoff_dt), len(self.ubi))
                    restored = list(bars_of_bi[:-2]) + self.ubi[cut_idx:]
                    self.ubi = restored
                self.bis.pop()
                self.anchor = last_bi._start_fx or None

    def update_many(self, bars) -> None:
        for b in bars:
            self.update(b)

    def clone(self):
        """状态快照(回测时点使用; bis为追加模式, 浅拷贝安全)"""
        import copy
        c = BiCursor(self.min_bi_len)
        c.ubi = [copy.copy(b) for b in self.ubi]
        c.anchor = dict(self.anchor) if self.anchor else None
        c.bis = list(self.bis)
        c.n = self.n
        return c


CACHE_ENABLED = True   # 缓存总开关(Phase R验证: False=冷计算, 用于对比)


def build_bi(bars: List[RawBar], level: str = 'D') -> List[Bi]:
    """构建笔。自实现，算法逻辑复刻 CZSC。"""
    if len(bars) < 5:
        return []
    if not CACHE_ENABLED:
        return _build_bi(bars)

    sym = bars[0].symbol if bars else ''
    if level not in ('D', 'W', '60', '30'):
        level = 'D'

    n = len(bars)
    first_dt = str(bars[0].dt)[:10] if bars else ''
    last_dt = str(bars[-1].dt)[:10] if bars else ''
    cache_level = level   # Phase R阶段6: 统一(level, symbol), clear_level精确匹配
    st = get_bi_state(cache_level, sym)
    if n == st.get('last_len', 0):
        return st['bis']
    # 增量缓存只允许"向前追加"(n>last_len且差<3根); 截断(n<last_len)必须重算,
    # 否则返回含未来数据的笔 → 前视偏差(2026-08-07修复: v6入场验证发现
    # 2024-03-29时点出现2024-08-08的中枢)
    if level == 'D' and st.get('last_len', 0) > 0 and 0 <= n - st['last_len'] < 3:
        return st['bis']

    bis = _build_bi(bars)

    st['last_len'] = n
    st['last_dt'] = last_dt
    st['bis'] = bis
    set_bi_state(cache_level, sym, st)
    return bis
