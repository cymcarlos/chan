import sqlite3
import unittest
from pathlib import Path

import backtest_daily_30min as engine


PROJECT = Path(__file__).resolve().parent


def _reset():
    engine.clear_all()
    engine.clear_daily_cache()
    engine.clear_30min_cache()
    engine._FRESH_B1_CACHE.clear()


class PerformanceEquivalenceTest(unittest.TestCase):
    def test_indexed_daily_prefix_is_field_identical_to_baseline(self):
        conn = sqlite3.connect(
            (PROJECT / 'kline.db').resolve().as_uri() + '?mode=ro', uri=True)
        conn.execute('PRAGMA query_only=ON')
        old_flags = (engine.FAST_DAILY_AVAIL, engine.FAST_DAILY_MACD_PREFIX)
        try:
            engine.FAST_DAILY_AVAIL = False
            engine.FAST_DAILY_MACD_PREFIX = False
            _reset()
            baseline = engine.backtest_one(
                '300314.SZ', '20240101', '20260805', conn=conn)

            engine.FAST_DAILY_AVAIL = True
            engine.FAST_DAILY_MACD_PREFIX = True
            _reset()
            optimized = engine.backtest_one(
                '300314.SZ', '20240101', '20260805', conn=conn)
        finally:
            engine.FAST_DAILY_AVAIL, engine.FAST_DAILY_MACD_PREFIX = old_flags
            conn.close()
            _reset()

        self.assertEqual(baseline, optimized)


if __name__ == '__main__':
    unittest.main()
