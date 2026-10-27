"""Migration permission boundaries use only memory and disposable SQLite files."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from item_registry import RegistryError
from item_registry_schema import migrate_registry
import stage3_migration as migration


def seed(conn, path=None, registry=True):
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript("""
        CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT);
        CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),product_name TEXT,purchase_price INTEGER);
        CREATE TABLE legacy_payments(id INTEGER PRIMARY KEY,amount_yen INTEGER NOT NULL);
        INSERT INTO users VALUES(1,'user',NULL);
        INSERT INTO merchandise VALUES(11,1,'fictional-old-stock',0);
        INSERT INTO legacy_payments VALUES(1,98765);
    """)
    if registry:
        migrate_registry(conn, allowed_test_path=path)
    conn.commit()


def schema(conn):
    return conn.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()


class Stage3MigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='kaika-stage3-migration-')
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'fixture.sqlite'
        with closing(sqlite3.connect(self.path)) as conn:
            seed(conn, self.path)
            self.original_schema = schema(conn)
        self.calls = 0

        def connect():
            self.calls += 1
            return sqlite3.connect(self.path)
        self.runtime = SimpleNamespace(DATABASE_URL=None, DATABASE=str(self.path), get_db=connect,
            app=SimpleNamespace(config={'STAGE2_INVENTORY_ENABLED': False, 'STAGE3_FINANCE_ENABLED': False,
                                       'STAGE3_TEST_LEDGER_ENABLED': False, 'BILLING_ENABLED': False}))

    def assert_unchanged(self):
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(schema(conn), self.original_schema)
            self.assertEqual(conn.execute('SELECT * FROM legacy_payments').fetchall(), [(1, 98765)])
            self.assertEqual(conn.execute('SELECT * FROM merchandise').fetchall(), [(11, 1, 'fictional-old-stock', 0)])

    def test_exact_temporary_fixture_idempotent_without_legacy_backfill_or_flag_changes(self):
        original_config = self.runtime.app.config.copy()
        for _ in range(2):
            migration.prepare_stage3_fixture(self.runtime, allowed_test_path=self.path)
        self.assertEqual(self.calls, 2)
        self.assertEqual(self.runtime.app.config, original_config)
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM legacy_payments').fetchall(), [(1, 98765)])
            self.assertEqual(conn.execute('SELECT * FROM merchandise').fetchall(), [(11, 1, 'fictional-old-stock', 0)])
            for table in ('stage3_fund_records', 'stage3_item_financial_facts', 'stage3_sale_status_records',
                          'stage3_charge_drafts', 'stage3_invoice_revisions', 'stage3_agreements', 'stage3_operations'):
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM stage3_schema').fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_memory_connection_is_allowed_without_config_discovery(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            seed(conn)
            migration.migrate_test_connection(conn)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM stage3_schema').fetchone()[0], 1)
        self.assertEqual(self.calls, 0)

    def test_explicit_file_required_even_if_it_is_under_temporary_directory(self):
        with closing(sqlite3.connect(self.path)) as conn:
            with self.assertRaises(ValueError):
                migration.migrate_test_connection(conn)
        self.assert_unchanged()

    def test_different_target_rejected_before_connect(self):
        with self.assertRaises(ValueError):
            migration.prepare_stage3_fixture(self.runtime, allowed_test_path=self.path.parent / 'other.sqlite')
        self.assertEqual(self.calls, 0)
        self.assert_unchanged()

    def test_nontemporary_target_rejected_before_connect(self):
        self.runtime.DATABASE = str(Path(__file__).resolve())
        with self.assertRaises(ValueError):
            migration.prepare_stage3_fixture(self.runtime, allowed_test_path=Path(__file__).resolve())
        self.assertEqual(self.calls, 0)
        self.assert_unchanged()

    def test_missing_target_not_created_or_connected(self):
        target = self.path.parent / 'not-created.sqlite'
        self.runtime.DATABASE = str(target)
        with self.assertRaises(ValueError):
            migration.prepare_stage3_fixture(self.runtime, allowed_test_path=target)
        self.assertEqual(self.calls, 0)
        self.assertFalse(target.exists())

    def test_postgres_setting_rejected_before_connect(self):
        self.runtime.DATABASE_URL = 'postgresql://fixture-only.invalid/test'
        with self.assertRaises(ValueError):
            migration.prepare_stage3_fixture(self.runtime, allowed_test_path=self.path)
        self.assertEqual(self.calls, 0)
        self.assert_unchanged()

    def test_stage2_migration_is_required_and_no_stage3_tables_created(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            seed(conn, registry=False)
            before = schema(conn)
            with self.assertRaises(RegistryError) as error:
                migration.migrate_test_connection(conn)
            self.assertEqual(error.exception.code, 'registry_migration_required')
            self.assertEqual(schema(conn), before)

    def test_partial_ddl_failure_rolls_back_every_stage3_table_and_marker(self):
        real_statements = migration.statements()
        with patch.object(migration, 'statements', return_value=real_statements[:15] + ['INVALID FIXTURE SQL']):
            with self.assertRaises(sqlite3.OperationalError):
                migration.prepare_stage3_fixture(self.runtime, allowed_test_path=self.path)
        self.assert_unchanged()

    def test_caller_can_rollback_successful_migration_with_its_transaction(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            migration.migrate_test_connection(conn, allowed_test_path=self.path)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM stage3_schema').fetchone()[0], 1)
            conn.rollback()
        self.assert_unchanged()

    def test_runtime_returning_other_file_connection_is_rejected(self):
        unexpected = self.path.parent / 'unexpected.sqlite'
        with closing(sqlite3.connect(unexpected)) as conn:
            seed(conn, unexpected)
            before = schema(conn)
        self.runtime.get_db = lambda: sqlite3.connect(unexpected)
        with self.assertRaises(ValueError):
            migration.prepare_stage3_fixture(self.runtime, allowed_test_path=self.path)
        self.assert_unchanged()
        with closing(sqlite3.connect(unexpected)) as conn:
            self.assertEqual(schema(conn), before)

    def test_attached_file_database_is_rejected_without_changes_to_either(self):
        extra = self.path.parent / 'attached.sqlite'
        with closing(sqlite3.connect(extra)) as conn:
            conn.execute('CREATE TABLE canary(value TEXT)')
            conn.execute("INSERT INTO canary VALUES('keep')")
            conn.commit()

        def connect():
            conn = sqlite3.connect(self.path)
            conn.execute('ATTACH DATABASE ? AS unexpected', (str(extra),))
            return conn
        self.runtime.get_db = connect
        with self.assertRaises(ValueError):
            migration.prepare_stage3_fixture(self.runtime, allowed_test_path=self.path)
        self.assert_unchanged()
        with closing(sqlite3.connect(extra)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM canary').fetchall(), [('keep',)])

    def test_attached_memory_database_also_rejected_as_unexpected_surface(self):
        def connect():
            conn = sqlite3.connect(self.path)
            conn.execute("ATTACH DATABASE ':memory:' AS unexpected")
            return conn
        self.runtime.get_db = connect
        with self.assertRaises(ValueError):
            migration.prepare_stage3_fixture(self.runtime, allowed_test_path=self.path)
        self.assert_unchanged()

    def test_postgres_review_contains_no_sqlite_or_legacy_mutation_and_does_not_connect(self):
        sql = migration.postgres_review_sql()
        for token in ('BEGIN;', 'COMMIT;', 'pg_advisory_xact_lock', 'NOT APPLIED', 'stage3_profit_fact_guard', 'stage3_charge_drafts'):
            self.assertIn(token, sql)
        for token in ('AUTOINCREMENT', 'PRAGMA', 'typeof(', 'INSERT INTO merchandise', 'UPDATE merchandise',
                      'INSERT INTO stage3_fund_records', 'INSERT INTO stage3_charge_drafts'):
            self.assertNotIn(token, sql)
        self.assertEqual(self.calls, 0)
        self.assert_unchanged()


if __name__ == '__main__':
    unittest.main()
