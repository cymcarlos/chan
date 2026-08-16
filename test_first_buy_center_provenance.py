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


def _zs(start, end, zd, zg, dd, gg, direction='向下'):
    base = datetime(2020, 1, 1)
    return SimpleNamespace(
        start_dt=(base + timedelta(days=start)).strftime('%Y-%m-%d'),
        end_dt=(base + timedelta(days=end)).strftime('%Y-%m-%d'),
        zd=zd, zg=zg, dd=dd, gg=gg, sdir=direction,
    )


class FirstBuyCenterProvenanceTest(unittest.TestCase):
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
            _zs(80, 100, 7.0, 8.0, 6.0, 9.0),
            # 即使窄中枢会被候选层过滤，它仍必须截断旧C段。
            _zs(120, 140, 7.0, 7.01, 6.0, 8.0, direction='向上'),
            _zs(160, 170, 5.0, 6.0, 4.0, 7.0),
        ]
        fake_bis = [SimpleNamespace() for _ in range(10)]

        def fake_area(_diff, _dea, _start, end, _direction):
            return -100.0 if end in (40, 80) else -1.0

        with patch('chan.daily_scan.build_zs', return_value=centers), \
                patch('chan.daily_scan.macd_area', side_effect=fake_area):
            candidates = scan_daily_on_bis(
                fake_bis, bars, [0.0] * len(bars), [0.0] * len(bars))

        first_buys = [c for c in candidates if c.buy_type == '一买']
        late_signal = bars[180].dt.strftime('%Y-%m-%d')
        self.assertTrue(any(
            c.signal_date == late_signal and c.l2.end_dt == centers[-1].end_dt
            for c in first_buys
        ))
        self.assertFalse(any(
            c.signal_date == late_signal and c.l2.end_dt == centers[1].end_dt
            for c in first_buys
        ))

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


if __name__ == '__main__':
    unittest.main()
