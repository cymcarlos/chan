import copy
import inspect
import sqlite3
import unittest
from pathlib import Path

import backtest_daily_30min as engine


PROJECT = Path(__file__).resolve().parent


def _reset_engine_caches():
    engine.clear_all()
    engine.clear_daily_cache()
    engine.clear_30min_cache()
    engine._FRESH_B1_CACHE.clear()


def _without_audit(result):
    result = copy.deepcopy(result)
    result.pop('audit_trace', None)
    for trade in result.get('trades', []):
        trade.pop('audit', None)
    for candidate in result.get('candidates', []):
        candidate.pop('audit', None)
    return result


class AuditTraceCompatibilityTest(unittest.TestCase):
    """项目真实数据上的 API/行为兼容门禁。"""

    @classmethod
    def setUpClass(cls):
        db_uri = (PROJECT / 'kline.db').as_uri() + '?mode=ro'
        cls.conn = sqlite3.connect(db_uri, uri=True)
        cls.outputs = []
        for value in ('omitted', False, True, True):
            _reset_engine_caches()
            kwargs = {} if value == 'omitted' else {'audit_trace': value}
            cls.outputs.append(engine.backtest_one(
                '300314.SZ', '20240101', '20260805', conn=cls.conn, **kwargs))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_api_defaults_to_disabled(self):
        sig = inspect.signature(engine.backtest_one)
        self.assertIn('audit_trace', sig.parameters)
        self.assertIs(sig.parameters['audit_trace'].default, False)

    def test_omitted_and_explicit_false_are_identical(self):
        self.assertEqual(self.outputs[0], self.outputs[1])
        self.assertNotIn('audit_trace', self.outputs[0])
        self.assertTrue(all('audit' not in t for t in self.outputs[0]['trades']))
        self.assertTrue(all('audit' not in c for c in self.outputs[0]['candidates']))

    def test_enabled_only_adds_audit_fields(self):
        traced = self.outputs[2]
        self.assertEqual(_without_audit(traced), self.outputs[1])
        self.assertIn('audit_trace', traced)
        self.assertTrue(traced['trades'], 'fixture symbol must exercise trade audit')
        required = {
            'trade_id', 'candidate_id', 'source_b1_id', 'occur_at', 'confirm_at',
            'first_seen_at', 'decision_at', 'fill_at', 'structure', 'abc_macd',
            'entry_predicates', 'exit_predicates', 'full_precision',
        }
        self.assertTrue(required <= set(traced['trades'][0]['audit']))

    def test_trace_ids_and_events_are_repeatable(self):
        left, right = self.outputs[2], self.outputs[3]
        self.assertEqual(
            [t['audit']['trade_id'] for t in left['trades']],
            [t['audit']['trade_id'] for t in right['trades']],
        )
        self.assertEqual(left['audit_trace'], right['audit_trace'])


if __name__ == '__main__':
    unittest.main()
