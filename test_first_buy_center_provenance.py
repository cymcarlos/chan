import sqlite3
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from chan.bars import RawBar
from chan.daily_scan import clear_daily_cache, load_daily, scan_daily_on_bars, scan_daily_on_bis


PROJECT = Path(__file__).resolve().parent


def _bar(index, low=12.0, high=13.0):
    dt = datetime(2020, 1, 1) + timedelta(days=index)
    return RawBar(
        symbol='TEST.SZ', dt=dt, open=12.0, high=high,
        low=low, close=12.0, vol=1.0, amount=1.0,
    )


def _zs(start, end, zd, zg, dd, gg, direction='向下', confirmed=True):
    base = datetime(2020, 1, 1)
    return SimpleNamespace(
        start_dt=(base + timedelta(days=start)).strftime('%Y-%m-%d'),
        end_dt=(base + timedelta(days=end)).strftime('%Y-%m-%d'),
        zd=zd, zg=zg, dd=dd, gg=gg, sdir=direction,
        occur_at=(base + timedelta(days=end - 1)).strftime('%Y-%m-%d'),
        confirm_at=((base + timedelta(days=end)).strftime('%Y-%m-%d')
                    if confirmed else ''),
        is_complete=confirmed,
        leave_confirm_at=((base + timedelta(days=end + 1)).strftime('%Y-%m-%d')
                          if confirmed else ''),
        member_start_idx=start,
        member_end_idx=end,
    )


def _bis_with_confirmed_lows(bars, *indices):
    bis = [SimpleNamespace(
        direction='up', end_dt=bars[0].dt.strftime('%Y-%m-%d'),
        end_price=bars[0].high, confirm_at=bars[1].dt.strftime('%Y-%m-%d'),
    ) for _ in range(10)]
    for index in indices:
        bis.append(SimpleNamespace(
            direction='down', end_dt=bars[index].dt.strftime('%Y-%m-%d'),
            end_price=bars[index].low,
            confirm_at=bars[index + 1].dt.strftime('%Y-%m-%d'),
        ))
    return bis


class FirstBuyCenterProvenanceTest(unittest.TestCase):
    def test_two_adjacent_complete_centers_are_sufficient(self):
        bars = [_bar(i) for i in range(220)]
        bars[30].high = 20.0
        bars[110].low = 5.0
        centers = [
            _zs(40, 60, 9.0, 10.0, 8.0, 11.0),
            # 中心区间已下移，但完整波动范围仍与A重叠；后者仅作诊断。
            _zs(80, 100, 6.5, 7.0, 6.0, 8.5),
        ]
        fake_bis = _bis_with_confirmed_lows(bars, 110)

        def fake_area(_diff, _dea, _start, end, _direction):
            return -100.0 if end == 40 else -1.0

        with patch('chan.daily_scan.build_zs', return_value=centers), \
                patch('chan.daily_scan.macd_area', side_effect=fake_area):
            candidates = scan_daily_on_bis(
                fake_bis, bars, [0.0] * len(bars), [0.0] * len(bars))

        first_buys = [c for c in candidates if c.buy_type == '一买']
        self.assertEqual(1, len(first_buys))
        self.assertEqual(bars[110].dt.strftime('%Y-%m-%d'),
                         first_buys[0].signal_date)
        self.assertFalse(first_buys[0].first_buy_evidence['diagnostics'][
            'strong_full_range_isolation_GG2_lt_DD1'])

    def test_c_segment_stops_before_the_next_same_level_center(self):
        bars = [_bar(i) for i in range(220)]
        bars[30].high = 20.0
        bars[40].high = 5.0
        bars[70].high = 20.0
        bars[80].high = 5.0
        bars[110].low = 5.0
        bars[180].low = 3.0

        centers = [
            _zs(40, 60, 9.0, 10.0, 8.0, 11.0),
            _zs(80, 100, 6.5, 7.0, 6.0, 7.5),
            # 即使窄中枢会被候选层过滤，它仍必须截断旧C段。
            _zs(120, 140, 7.0, 7.01, 6.0, 8.0, direction='向上'),
            _zs(160, 170, 5.0, 6.0, 4.0, 7.0),
        ]
        fake_bis = _bis_with_confirmed_lows(bars, 110, 180)

        def fake_area(_diff, _dea, _start, end, _direction):
            return -100.0 if end in (40, 80) else -1.0

        with patch('chan.daily_scan.build_zs', return_value=centers), \
                patch('chan.daily_scan.macd_area', side_effect=fake_area):
            candidates = scan_daily_on_bis(
                fake_bis, bars, [0.0] * len(bars), [0.0] * len(bars))

        first_buys = [c for c in candidates if c.buy_type == '一买']
        bounded_signal = bars[110].dt.strftime('%Y-%m-%d')
        late_signal = bars[180].dt.strftime('%Y-%m-%d')
        self.assertTrue(any(
            c.signal_date == bounded_signal and c.l2.end_dt == centers[1].end_dt
            for c in first_buys
        ))
        formal = next(c for c in first_buys if c.signal_date == bounded_signal)
        evidence = formal.first_buy_evidence
        self.assertEqual('formal-first-buy/v1', evidence['schema_version'])
        self.assertEqual({'A': 0, 'B': 1}, evidence['raw_center_indices'])
        self.assertEqual(centers[1].leave_confirm_at, evidence['B_complete_at'])
        self.assertEqual(bars[101].dt.strftime('%Y-%m-%d'),
                         evidence['segments']['C']['start_at'])
        self.assertEqual(bounded_signal, evidence['segments']['C']['low_at'])
        self.assertEqual(5.0, evidence['segments']['C']['low'])
        self.assertTrue(all(evidence['invariants'].values()))
        self.assertFalse(any(
            c.signal_date == late_signal and c.l2.end_dt == centers[1].end_dt
            for c in first_buys
        ))

    def test_formal_first_buy_rejects_non_downshifted_or_unconfirmed_centers(self):
        bars = [_bar(i) for i in range(220)]
        bars[30].high = 20.0
        bars[110].low = 5.0
        fake_bis = _bis_with_confirmed_lows(bars, 110)

        def fake_area(_diff, _dea, _start, end, _direction):
            return -100.0 if end == 40 else -1.0

        late_confirm_b = _zs(80, 100, 6.5, 7.0, 6.0, 7.5)
        late_confirm_b.leave_confirm_at = bars[130].dt.strftime('%Y-%m-%d')
        cases = {
            'ZG2_not_below_ZD1': [
                _zs(40, 60, 9.0, 10.0, 8.0, 11.0),
                _zs(80, 100, 9.0, 9.5, 6.0, 10.0),
                _zs(140, 160, 4.0, 5.0, 3.0, 5.5, direction='向上'),
            ],
            'A_unconfirmed': [
                _zs(40, 60, 9.0, 10.0, 8.0, 11.0, confirmed=False),
                _zs(80, 100, 6.5, 7.0, 6.0, 7.5),
                _zs(140, 160, 4.0, 5.0, 3.0, 5.5, direction='向上'),
            ],
            'B_unconfirmed': [
                _zs(40, 60, 9.0, 10.0, 8.0, 11.0),
                _zs(80, 100, 6.5, 7.0, 6.0, 7.5, confirmed=False),
                _zs(140, 160, 4.0, 5.0, 3.0, 5.5, direction='向上'),
            ],
            'B_confirmed_after_C_low': [
                _zs(40, 60, 9.0, 10.0, 8.0, 11.0),
                late_confirm_b,
                _zs(140, 160, 4.0, 5.0, 3.0, 5.5, direction='向上'),
            ],
        }
        for label, centers in cases.items():
            with self.subTest(label=label), \
                    patch('chan.daily_scan.build_zs', return_value=centers), \
                    patch('chan.daily_scan.macd_area', side_effect=fake_area):
                candidates = scan_daily_on_bis(
                    fake_bis, bars, [0.0] * len(bars), [0.0] * len(bars))
                self.assertFalse(any(c.buy_type == '一买' for c in candidates))

    def test_formal_first_buy_does_not_skip_an_intervening_center(self):
        bars = [_bar(i) for i in range(220)]
        bars[30].high = 20.0
        bars[180].low = 3.0
        centers = [
            _zs(40, 60, 9.0, 10.0, 8.0, 11.0),
            _zs(80, 100, 7.0, 8.0, 6.0, 9.0, direction='向上'),
            _zs(120, 140, 4.5, 5.0, 4.0, 5.5),
        ]
        fake_bis = _bis_with_confirmed_lows(bars, 180)
        with patch('chan.daily_scan.build_zs', return_value=centers), \
                patch('chan.daily_scan.macd_area', return_value=-1.0):
            candidates = scan_daily_on_bis(
                fake_bis, bars, [0.0] * len(bars), [0.0] * len(bars))
        self.assertFalse(any(c.buy_type == '一买' for c in candidates))

    def test_formal_first_buy_requires_c_to_make_a_new_low(self):
        bars = [_bar(i, low=6.5) for i in range(220)]
        bars[30].high = 20.0
        centers = [
            _zs(40, 60, 9.0, 10.0, 8.0, 11.0),
            _zs(80, 100, 6.5, 7.0, 6.0, 7.5),
            _zs(140, 160, 4.0, 5.0, 3.0, 5.5, direction='向上'),
        ]
        fake_bis = _bis_with_confirmed_lows(bars, 110)
        with patch('chan.daily_scan.build_zs', return_value=centers), \
                patch('chan.daily_scan.macd_area', return_value=-1.0):
            candidates = scan_daily_on_bis(
                fake_bis, bars, [0.0] * len(bars), [0.0] * len(bars))
        self.assertFalse(any(c.buy_type == '一买' for c in candidates))

    def test_002486_old_centers_no_longer_bind_to_2024_low(self):
        db = PROJECT / 'kline.db'
        if not db.exists():
            self.skipTest('project database is unavailable')
        clear_daily_cache()
        conn = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
        conn.execute('PRAGMA query_only=ON')
        try:
            bars = [
                b for b in load_daily('002486.SZ', conn=conn)
                if b.dt <= datetime(2024, 6, 18)
            ]
            candidates = scan_daily_on_bars(bars)
        finally:
            conn.close()
            clear_daily_cache()

        bad = [
            c for c in candidates
            if c.buy_type == '一买' and c.signal_date == '2024-06-07'
        ]
        self.assertEqual([], bad)

    def test_002749_consolidation_divergence_is_not_a_formal_first_buy(self):
        db = PROJECT / 'kline.db'
        if not db.exists():
            self.skipTest('project database is unavailable')
        clear_daily_cache()
        conn = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
        conn.execute('PRAGMA query_only=ON')
        try:
            bars = [
                b for b in load_daily('002749.SZ', conn=conn)
                if b.dt <= datetime(2026, 5, 14)
            ]
            candidates = scan_daily_on_bars(bars)
        finally:
            conn.close()
            clear_daily_cache()

        self.assertFalse(any(
            c.buy_type == '一买' and c.signal_date == '2026-04-20'
            for c in candidates
        ))

    def test_300274_consolidation_divergences_are_not_formal_first_buys(self):
        db = PROJECT / 'kline.db'
        if not db.exists():
            self.skipTest('project database is unavailable')
        clear_daily_cache()
        conn = sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)
        conn.execute('PRAGMA query_only=ON')
        try:
            bars = [
                b for b in load_daily('300274.SZ', conn=conn)
                if b.dt <= datetime(2025, 3, 12)
            ]
            candidates = scan_daily_on_bars(bars)
        finally:
            conn.close()
            clear_daily_cache()

        first_buy_dates = {
            c.signal_date for c in candidates if c.buy_type == '一买'
        }
        self.assertTrue({'2024-07-10', '2025-02-24'}.isdisjoint(first_buy_dates))


if __name__ == '__main__':
    unittest.main()
