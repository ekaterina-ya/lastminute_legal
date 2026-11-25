"""Учёт платных thinking и cached-токенов без API и отправки отчётов."""
from datetime import datetime
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

_tmp = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'AGGREGATOR_LOG_PATH': os.path.join(_tmp.name, 'aggregator.log')}):
    import aggregator


def tearDownModule():
    aggregator.file_handler.close()
    aggregator.logger.removeHandler(aggregator.file_handler)
    _tmp.cleanup()


class UsageCostTests(unittest.TestCase):
    def test_new_and_old_log_lines(self):
        data = aggregator.parse_request_block(
            '[USAGE] stage=preprocess model=gemini-3.1-pro-preview input=100 output=20 cached=0 ok=1\n'
            '[USAGE] stage=final model=gemini-4-hydrogen input=1000 output=20 cached=100 ok=1 thinking=50 level=high duration_ms=20\n',
            '1', 'test',
        )
        self.assertEqual(len(data['usage_events']), 2)
        self.assertEqual([e['thinking'] for e in data['usage_events']], [0, 50])
        event = data['usage_events'][1]
        self.assertAlmostEqual(aggregator._estimate_cost_usd(
            event['model'], event['input'], event['output'], event['cached'], event['thinking'],
        ), 0.0018825)

    def test_reserve_cost_and_long_context_boundary(self):
        self.assertAlmostEqual(aggregator._estimate_cost_usd(
            'gemini-3.1-pro-preview', 1000, 20, 100, 50), 0.00266)
        self.assertAlmostEqual(aggregator._estimate_cost_usd(
            'gemini-3.1-pro-preview', 200_000, 10, 0, 20), 0.40036)
        self.assertAlmostEqual(aggregator._estimate_cost_usd(
            'gemini-3.1-pro-preview', 200_001, 10, 1, 20), 0.8005404)
        self.assertIsNone(aggregator._estimate_cost_usd('unknown', 100, 20))

    def test_daily_report_prices_each_call_and_excludes_failed_calls(self):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        block = now + ' - --- Новый запрос от пользователя 1 (@test) ---\n'
        block += ('[USAGE] stage=final model=gemini-3.1-pro-preview '
                  'input=150000 output=500 cached=0 ok=1 thinking=1000\n') * 2
        block += ('[USAGE] stage=final model=gemini-4-hydrogen '
                  'input=999999 output=999999 cached=0 ok=0 thinking=999999\n')
        data = aggregator.parse_request_block(block, '1', 'test')
        df = pd.DataFrame([data]).rename(columns={'feedback_rating': 'полезность'})
        df['request_date'] = pd.to_datetime(df['request_date'])
        db_path = os.path.join(_tmp.name, 'users.db')
        with sqlite3.connect(db_path) as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS users (user_id INTEGER)')
        report = aggregator.generate_summary_report(df, db_path)
        self.assertIn('Итого ≈ *$0.64*', report)
        self.assertIn('thinking *2.0k*', report)
        self.assertNotIn('gemini-4-hydrogen', report)


if __name__ == '__main__':
    unittest.main()
