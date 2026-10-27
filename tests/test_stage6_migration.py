"""Migration safety against disposable DBs; never starts the application."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_stage4_migration import seed, schema
from stage4_migration import migrate_test_connection as stage4
from stage5_migration import migrate_test_connection as stage5
from stage6_common import Stage6Store
from item_registry import Registry
import stage6_migration as migration


class Stage6MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kaika-stage6-migration-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.sqlite'
        with closing(sqlite3.connect(self.path)) as db:
            seed(db, self.path)
            stage4(db, allowed_test_path=self.path)
            stage5(db, allowed_test_path=self.path)
            db.commit()
            self.before = schema(db)
        self.runtime = SimpleNamespace(DATABASE_URL=None, DATABASE=str(self.path),
            get_db=lambda: sqlite3.connect(self.path),
            app=SimpleNamespace(config={'STAGE6_ENABLED': False}))

    def unchanged(self):
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(schema(db), self.before)
            self.assertEqual(db.execute('SELECT * FROM payments').fetchall(), [(1, 99999)])

    def test_repeat_migration_preserves_old_money_and_flags(self):
        for _ in range(2):
            migration.prepare_stage6_fixture(self.runtime, allowed_test_path=self.path)
        with closing(sqlite3.connect(self.path)) as db:
            Stage6Store(Registry(db)).require_ready()
            self.assertEqual(db.execute('SELECT * FROM payments').fetchall(), [(1, 99999)])
        self.assertFalse(self.runtime.app.config['STAGE6_ENABLED'])

    def test_bad_sql_rolls_back_whole_migration(self):
        original = migration.statements
        with patch.object(migration, 'statements', side_effect=lambda dialect='sqlite': original(dialect) + ['INVALID SQL']):
            with self.assertRaises(sqlite3.OperationalError):
                migration.prepare_stage6_fixture(self.runtime, allowed_test_path=self.path)
        self.unchanged()

    def test_missing_previous_stage_does_not_create_new_tables(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with self.assertRaises(Exception):
                migration.migrate_test_connection(db)
            self.assertEqual(schema(db), [])

    def test_foreign_keys_required(self):
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(ValueError):
                migration.migrate_test_connection(db, allowed_test_path=self.path)
        self.unchanged()

    def test_wrong_explicit_path_rejected(self):
        with self.assertRaises(ValueError):
            migration.prepare_stage6_fixture(self.runtime, allowed_test_path=self.path.parent / 'other.sqlite')
        self.unchanged()

    def test_wrong_actual_connection_rejected(self):
        other = self.path.parent / 'other.sqlite'
        with closing(sqlite3.connect(other)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with self.assertRaises(ValueError):
                migration.migrate_test_connection(db, allowed_test_path=self.path)
        self.unchanged()

    def test_attached_database_rejected(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute("ATTACH DATABASE ':memory:' AS another")
            with self.assertRaises(ValueError):
                migration.migrate_test_connection(db, allowed_test_path=self.path)
        self.unchanged()

    def test_postgres_review_only(self):
        self.runtime.DATABASE_URL = 'postgresql://unreachable-fixture'
        with self.assertRaises(ValueError):
            migration.prepare_stage6_fixture(self.runtime, allowed_test_path=self.path)
        sql = migration.postgres_review_sql()
        self.assertIn('NOT APPLIED', sql)
        self.assertIn('pg_advisory_xact_lock', sql)
        self.assertIn('stage6_schema', sql)
        self.unchanged()


if __name__ == '__main__':
    unittest.main()
