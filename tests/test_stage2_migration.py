"""Combined stage-2 migration never touches a non-explicit disposable DB."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import stage2_migration as migration


class Stage2MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kaika-stage2-migration-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.sqlite'
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript("""
                CREATE TABLE users(id INTEGER PRIMARY KEY);
                CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),product_name TEXT);
                INSERT INTO users VALUES(1);
                INSERT INTO merchandise VALUES(11,1,'fictional-old-stock');
            """)
        self.calls = 0
        def connect():
            self.calls += 1
            return sqlite3.connect(self.path)
        self.runtime = SimpleNamespace(DATABASE_URL=None,DATABASE=str(self.path),get_db=connect)

    def test_additive_repeat_keeps_old_inventory_unknown_and_creates_no_work_or_fees(self):
        for _ in range(2):
            migration.prepare_stage2_fixture(self.runtime,allowed_test_path=self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT id,user_id,product_name,custody_location,custody_received_at FROM merchandise').fetchall(),[(11,1,'fictional-old-stock','unknown',None)])
            for table in ('business_items','business_work_records','business_item_events','stage2_photo_records'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='business_charge_events'").fetchone()[0],0)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertFalse(hasattr(self.runtime,'STAGE2_INVENTORY_ENABLED'))

    def test_mismatched_target_is_rejected_before_connect(self):
        with self.assertRaises(ValueError):
            migration.prepare_stage2_fixture(self.runtime,allowed_test_path=self.path.parent/'different.sqlite')
        self.assertEqual(self.calls,0)

    def test_non_temporary_target_is_rejected_before_connect(self):
        self.runtime.DATABASE = str(Path(__file__).resolve())
        with self.assertRaises(ValueError):
            migration.prepare_stage2_fixture(self.runtime,allowed_test_path=Path(__file__).resolve())
        self.assertEqual(self.calls,0)

    def test_postgres_configuration_has_no_execution_path(self):
        self.runtime.DATABASE_URL = 'postgresql://fixture-only.invalid/fictional'
        with self.assertRaises(ValueError):
            migration.prepare_stage2_fixture(self.runtime,allowed_test_path=self.path)
        self.assertEqual(self.calls,0)

    def test_failure_rolls_back_added_columns_and_tables(self):
        with patch.object(migration,'PHOTO_RECORDS_DDL','INVALID FIXTURE SQL'):
            with self.assertRaises(sqlite3.OperationalError):
                migration.prepare_stage2_fixture(self.runtime,allowed_test_path=self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual([row[1] for row in conn.execute('PRAGMA table_info(merchandise)')],['id','user_id','product_name'])
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall(),[('merchandise',),('users',)])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM merchandise').fetchone()[0],1)

    def test_unexpected_attachment_is_rejected_without_writes(self):
        extra = self.path.parent/'extra.sqlite'
        sqlite3.connect(extra).close()
        def connect():
            conn=sqlite3.connect(self.path)
            conn.execute('ATTACH DATABASE ? AS unexpected',(str(extra),))
            return conn
        self.runtime.get_db=connect
        with self.assertRaises(ValueError):
            migration.prepare_stage2_fixture(self.runtime,allowed_test_path=self.path)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(len(conn.execute('PRAGMA table_info(merchandise)').fetchall()),3)

    def test_postgres_candidate_has_transaction_and_no_sqlite_constructs(self):
        sql=migration.postgres_review_sql()
        self.assertIn('BEGIN;',sql)
        self.assertIn('COMMIT;',sql)
        self.assertIn('pg_advisory_xact_lock',sql)
        self.assertNotIn('AUTOINCREMENT',sql)
        self.assertNotIn('PRAGMA',sql)
        self.assertNotIn('INSERT INTO merchandise',sql)
        self.assertNotIn('UPDATE merchandise',sql)
        self.assertEqual(self.calls,0)


if __name__ == '__main__':
    unittest.main()
