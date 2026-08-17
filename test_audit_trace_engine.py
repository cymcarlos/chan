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
                '605507.SH', '20240101', '20260805', conn=cls.conn, **kwargs))
                # (2026-08-17: 原固件300314.SZ→600579.SH→605507.SH, 均在严格53课规则下被逐级过滤;
                #  它的三买正是"涨完跌回老中枢"型, 被 B3_NEW_CENTER_KILL 正确过滤)

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


class FormalSecondBuyLineageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_uri = (PROJECT / 'kline.db').as_uri() + '?mode=ro'
        cls.conn = sqlite3.connect(db_uri, uri=True)
        cls.results = {}
        for symbol in ('000826.SZ', '002749.SZ', '000810.SZ'):
            _reset_engine_caches()
            cls.results[symbol] = engine.backtest_one(
                symbol, '20240101', '20260805', conn=cls.conn, audit_trace=True)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_real_formal_second_buy_has_exact_non_vacuous_lineage(self):
        result = self.results['000826.SZ']
        trades = [trade for trade in result['trades'] if trade['name'] == '二买']
        self.assertTrue(trades, 'real-data positive gate must not be vacuous')
        candidate_map = {
            row['candidate_id']: row for row in result['audit_trace']['candidates']
        }
        trade_audit = trades[0]['audit']
        child = candidate_map[trade_audit['candidate_id']]
        source = candidate_map[trade_audit['source_b1_id']]
        self.assertEqual('一买', source['buy_type'])
        self.assertEqual('PASS', source['structure']['formal_first_buy_status'])
        self.assertTrue(all(source['structure']['formal_first_buy']['invariants'].values()))
        self.assertEqual(source['first_buy_provenance_id'],
                         child['source_b1_provenance_id'])
        self.assertEqual(source['first_buy_provenance_id'],
                         child['first_buy_provenance_id'])
        self.assertLessEqual(source['first_seen_at'], child['pullback_start_at'])
        self.assertLess(child['pullback_start_at'], child['pullback_confirm_at'])
        self.assertLessEqual(child['pullback_confirm_at'], trade_audit['decision_at'])
        self.assertLess(trade_audit['decision_at'], trade_audit['fill_at'])
        self.assertLessEqual(child['window_end'], source['window_end'])
        adopted = next(
            row for row in trade_audit['entry_predicates']
            if row['candidate_id'] == trade_audit['candidate_id'])
        self.assertTrue(adopted['predicates']['not_consumed'])

    def test_002749_backfilled_fake_second_buy_is_absent(self):
        result = self.results['002749.SZ']
        self.assertFalse(any(trade['name'] == '二买' for trade in result['trades']))

    def test_cross_period_adjustment_scale_is_bound_and_audited(self):
        result = self.results['000810.SZ']
        trades = [trade for trade in result['trades'] if trade['name'] == '二买']
        self.assertTrue(trades, 'real-data scale fixture must retain a non-vacuous second buy')
        candidate_map = {
            row['candidate_id']: row for row in result['audit_trace']['candidates']
        }
        audit = trades[0]['audit']
        child = candidate_map[audit['candidate_id']]
        source = candidate_map[audit['source_b1_id']]
        expected = {
            'daily_price': 7.78,
            'm30_price': 7.64157824,
            'factor': 0.982208,
            'ratio_spread': 1.1102230246251565e-16,
            'at': '2024-07-09',
        }
        self.assertEqual(expected, source['daily_a_to_m30'])
        self.assertEqual(expected, child['daily_a_to_m30'])
        self.assertEqual(7.78, child['source_b1_daily_a_price'])
        self.assertEqual(expected['m30_price'], child['abc']['A']['price'])
        self.assertAlmostEqual(
            expected['m30_price'] * 0.995,
            audit['exit_predicates']['thresholds']['A_x_0_995'],
        )


if __name__ == '__main__':
    unittest.main()
