import json
import os
import sqlite3
import tempfile
import unittest
from scripts import backfill_index_ohlc as backfill


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        for index, row in enumerate(self.rows):
            yield index, row


class FakePro:
    def __init__(self, by_code):
        self.by_code = by_code

    def index_daily(self, ts_code, start_date, end_date):
        return FakeFrame(self.by_code.get(ts_code, []))


class BackfillIndexOHLCTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE index_daily (id INTEGER PRIMARY KEY,date TEXT,name TEXT,'
                        'open REAL,high REAL,low REAL,close REAL,change REAL,volume REAL)')
        self.db.execute('INSERT INTO index_daily VALUES (1,"20250512","上证指数",NULL,NULL,NULL,11,1,0.1)')
        self.db.commit()
        self.target = backfill.load_targets(self.db)[0]
        self.api_row = {'trade_date': '20250512', 'open': 10, 'high': 12, 'low': 9,
                        'close': 11, 'pct_chg': 1, 'amount': 12345000}

    def tearDown(self):
        self.db.close()

    def pro(self, row=None):
        return FakePro({'000001.SH': [row or self.api_row]})

    def test_fetch_update_preserves_close_change_and_converts_amount(self):
        updates = backfill.fetch_updates(self.pro(), [self.target])
        self.assertEqual(updates[0]['volume'], 123.45)
        self.assertEqual((updates[0]['open'], updates[0]['high'], updates[0]['low']), (10, 12, 9))

    def test_mismatch_or_incomplete_source_aborts(self):
        for row in [dict(self.api_row, close=12), dict(self.api_row, pct_chg=2)]:
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                backfill.fetch_updates(self.pro(row), [self.target])
        with self.assertRaises(RuntimeError):
            backfill.fetch_updates(FakePro({}), [self.target])

    def test_apply_backs_up_and_updates_only_target_fields(self):
        updates = backfill.fetch_updates(self.pro(), [self.target])
        with tempfile.TemporaryDirectory() as directory:
            backup = os.path.join(directory, 'backup.json')
            backfill.apply_updates(self.db, [self.target], updates, backup)
            with open(backup, encoding='utf-8') as handle:
                self.assertEqual(json.load(handle)[0]['volume'], 0.1)
        row = tuple(self.db.execute('SELECT open,high,low,close,change,volume FROM index_daily').fetchone())
        self.assertEqual(row, (10, 12, 9, 11, 1, 123.45))


if __name__ == '__main__':
    unittest.main()
