"""Execute only the two parsed legacy job functions against disposable SQLite."""
import ast
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
NAMES = {'check_and_transfer_overdue_items', 'check_and_transfer_long_term_items'}


class CustodyTransferJobsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8-sig'))
        cls.nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in NAMES]
        assert {node.name for node in cls.nodes} == NAMES
        cls.code = compile(ast.Module(body=cls.nodes, type_ignores=[]), 'fixture-transfer-jobs', 'exec')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kaika-transfer-fixture-')
        self.path = Path(self.temp.name) / 'fixture.sqlite'
        conn = sqlite3.connect(self.path)
        conn.executescript('''
            CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,subscription_status TEXT,overdue_since TEXT,
                display_name TEXT,username TEXT,line_user_id TEXT,email TEXT);
            CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,custody_location TEXT,sale_date TEXT,
                product_name TEXT,purchase_date TEXT,created_at TEXT,storage_start_date TEXT,notes TEXT);
            INSERT INTO users VALUES(1,'user','past_due','2025-01-01','Fixture owner','fixture',NULL,NULL);
            INSERT INTO users VALUES(9,'owner','active',NULL,'Fixture administrator','admin',NULL,NULL);
        ''')
        for identifier, custody, sold, received in [
            (1, 'kaika', None, '2025-01-01'), (2, 'self', None, '2025-01-01'),
            (3, 'transit', None, '2025-01-01'), (4, None, None, '2025-01-01'),
            (5, 'kaika', '2026-08-01', '2025-01-01'), (6, 'kaika', None, '2026-09-01')]:
            conn.execute('INSERT INTO merchandise VALUES(?,1,?,?,?, ?,?,?,NULL)',
                (identifier, custody, sold, 'Fictional product', '2020-01-01', '2020-01-01', received))
        conn.commit(); conn.close()
        def reject_network(*args, **kwargs):
            raise AssertionError('External notification is not permitted in this fixture')
        self.namespace = dict(DATABASE_URL=None, get_db=lambda: sqlite3.connect(self.path),
            get_jst_now=lambda: datetime(2026, 9, 11), timedelta=timedelta,
            ensure_merchandise_storage_start_date_column=lambda conn: None,
            send_line_push=reject_network, print=lambda *args, **kwargs: None)
        exec(self.code, self.namespace)

    def tearDown(self):
        self.temp.cleanup()

    def owners(self):
        conn = sqlite3.connect(self.path)
        try:
            return dict(conn.execute('SELECT id,user_id FROM merchandise'))
        finally:
            conn.close()

    def test_overdue_job_preserves_self_and_transit_stock(self):
        self.namespace['check_and_transfer_overdue_items']()
        self.assertEqual(self.owners(), {1: 9, 2: 1, 3: 1, 4: 9, 5: 1, 6: 9})

    def test_long_term_job_uses_received_date_and_preserves_non_kaika_stock(self):
        self.namespace['check_and_transfer_long_term_items']()
        self.assertEqual(self.owners(), {1: 9, 2: 1, 3: 1, 4: 9, 5: 1, 6: 1})

    def test_both_database_branches_guard_selection_and_update(self):
        for node in self.nodes:
            queries = [part.value for part in ast.walk(node) if isinstance(part, ast.Constant) and isinstance(part.value, str)
                and ('UPDATE merchandise' in part.value or ('SELECT' in part.value and 'FROM merchandise' in part.value))]
            self.assertEqual(len(queries), 4)
            for query in queries:
                self.assertIn("custody_location, 'kaika') = 'kaika'", query)


if __name__ == '__main__':
    unittest.main()
