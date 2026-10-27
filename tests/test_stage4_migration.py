"""Stage 4 installation is possible only into explicitly isolated fixtures."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from item_registry import RegistryError
from item_registry_schema import migrate_registry
from stage3_migration import migrate_test_connection as stage3
import stage4_migration as migration


def seed(db,path=None,finance=True):
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript("""CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT);
        CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),product_name TEXT);
        CREATE TABLE payments(id INTEGER PRIMARY KEY,amount INTEGER);
        INSERT INTO users VALUES(1,'user',NULL);
        INSERT INTO merchandise VALUES(11,1,'fixture');
        INSERT INTO payments VALUES(1,99999);""")
    migrate_registry(db,allowed_test_path=path)
    if finance:
        stage3(db,allowed_test_path=path)
    db.commit()


def schema(db):
    return db.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()


class Stage4MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='kaika-stage4-migration-')
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'fixture.sqlite'
        with closing(sqlite3.connect(self.path)) as db:
            seed(db,self.path)
            self.before=schema(db)
        self.calls=0
        def connect():
            self.calls+=1
            return sqlite3.connect(self.path)
        self.runtime=SimpleNamespace(DATABASE_URL=None,DATABASE=str(self.path),get_db=connect,
            app=SimpleNamespace(config={'STAGE4_COMMERCE_ENABLED':False,'BILLING_ENABLED':False}))

    def unchanged(self):
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(schema(db),self.before)
            self.assertEqual(db.execute('SELECT * FROM payments').fetchall(),[(1,99999)])
            self.assertEqual(db.execute('SELECT * FROM merchandise').fetchall(),[(11,1,'fixture')])

    def test_idempotent_and_no_backfill_or_activation(self):
        config=self.runtime.app.config.copy()
        for _ in range(2):
            migration.prepare_stage4_fixture(self.runtime,allowed_test_path=self.path)
        self.assertEqual(self.runtime.app.config,config)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            for table in ('stage4_catalog_events','stage4_purchase_orders','stage4_listing_versions','stage4_channel_orders','stage4_media','stage3_fund_records'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT * FROM payments').fetchall(),[(1,99999)])

    def test_memory_allowed(self):
        with closing(sqlite3.connect(':memory:')) as db:
            seed(db)
            migration.migrate_test_connection(db)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stage4_schema').fetchone()[0],1)

    def test_missing_stage3_rolls_back(self):
        with closing(sqlite3.connect(':memory:')) as db:
            seed(db,finance=False)
            before=schema(db)
            with self.assertRaises(RegistryError):
                migration.migrate_test_connection(db)
            self.assertEqual(schema(db),before)

    def test_failure_rolls_back_marker_and_tables(self):
        with patch.object(migration,'statements',return_value=migration.statements()[:8]+['INVALID FIXTURE SQL']):
            with self.assertRaises(sqlite3.OperationalError):
                migration.prepare_stage4_fixture(self.runtime,allowed_test_path=self.path)
        self.unchanged()

    def test_caller_rollback_preserved(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN IMMEDIATE')
            migration.migrate_test_connection(db,allowed_test_path=self.path)
            db.rollback()
        self.unchanged()

    def test_file_without_explicit_target_rejected(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with self.assertRaises(ValueError):
                migration.migrate_test_connection(db)
        self.unchanged()

    def test_foreign_keys_off_rejected(self):
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(ValueError):
                migration.migrate_test_connection(db,allowed_test_path=self.path)
        self.unchanged()

    def test_wrong_missing_nontemporary_targets_rejected_before_connect(self):
        for target in (self.path.parent/'absent.sqlite',Path(__file__).resolve()):
            with self.subTest(target=target):
                self.runtime.DATABASE=str(target)
                with self.assertRaises(ValueError):
                    migration.prepare_stage4_fixture(self.runtime,allowed_test_path=target)
        self.assertEqual(self.calls,0)
        self.unchanged()

    def test_postgres_rejected_before_connect(self):
        self.runtime.DATABASE_URL='postgresql://fixture.invalid/test'
        with self.assertRaises(ValueError):
            migration.prepare_stage4_fixture(self.runtime,allowed_test_path=self.path)
        self.assertEqual(self.calls,0)
        self.unchanged()

    def test_unexpected_connection_rejected(self):
        other=self.path.parent/'other.sqlite'
        with closing(sqlite3.connect(other)) as db:
            seed(db,other); before=schema(db)
        self.runtime.get_db=lambda:sqlite3.connect(other)
        with self.assertRaises(ValueError):
            migration.prepare_stage4_fixture(self.runtime,allowed_test_path=self.path)
        with closing(sqlite3.connect(other)) as db:
            self.assertEqual(schema(db),before)
        self.unchanged()

    def test_attached_database_rejected(self):
        for attached in (':memory:',str(self.path.parent/'extra.sqlite')):
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('ATTACH DATABASE ? AS extra',(attached,))
                with self.assertRaises(ValueError):
                    migration.migrate_test_connection(db,allowed_test_path=self.path)
        self.unchanged()

    def test_postgres_sql_review_without_execution(self):
        sql=migration.postgres_review_sql()
        for expected in ('NOT APPLIED','pg_advisory_xact_lock','stage4_purchase_orders','stage4_order_claims'):
            self.assertIn(expected,sql)
        for forbidden in ('AUTOINCREMENT','PRAGMA','INSERT INTO merchandise','UPDATE merchandise','INSERT INTO stage3_fund_records'):
            self.assertNotIn(forbidden,sql)
        self.assertEqual(self.calls,0)
        self.unchanged()


if __name__=='__main__':
    unittest.main()
