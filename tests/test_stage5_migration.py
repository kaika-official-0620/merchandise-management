"""Isolated fixture migration guards; no application startup or external I/O."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from item_registry import RegistryError
from test_stage4_migration import seed,schema
from stage4_migration import migrate_test_connection as stage4
import stage5_migration as migration


class Stage5MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='kaika-stage5-migration-')
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'fixture.sqlite'
        with closing(sqlite3.connect(self.path)) as db:
            seed(db,self.path); stage4(db,allowed_test_path=self.path); db.commit()
            self.before=schema(db)
        self.calls=0
        def connect():
            self.calls+=1
            return sqlite3.connect(self.path)
        self.runtime=SimpleNamespace(DATABASE_URL=None,DATABASE=str(self.path),get_db=connect,
            app=SimpleNamespace(config={'STAGE5_PAYMENTS_ENABLED':False}))

    def unchanged(self):
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(schema(db),self.before)
            self.assertEqual(db.execute('SELECT * FROM payments').fetchall(),[(1,99999)])

    def test_idempotent_no_legacy_changes_or_activation(self):
        config=self.runtime.app.config.copy()
        for _ in range(2):
            migration.prepare_stage5_fixture(self.runtime,allowed_test_path=self.path)
        self.assertEqual(self.runtime.app.config,config)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('SELECT * FROM payments').fetchall(),[(1,99999)])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stage5_payment_attempts').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stage3_fund_records').fetchone()[0],0)

    def test_memory_and_caller_rollback(self):
        with closing(sqlite3.connect(':memory:')) as db:
            seed(db); stage4(db); db.commit()
            before=schema(db); db.execute('BEGIN')
            migration.migrate_test_connection(db)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM stage5_schema').fetchone()[0],1)
            db.rollback(); self.assertEqual(schema(db),before)

    def test_missing_previous_stage(self):
        with closing(sqlite3.connect(':memory:')) as db:
            seed(db); before=schema(db)
            with self.assertRaises(RegistryError):
                migration.migrate_test_connection(db)
            self.assertEqual(schema(db),before)

    def test_partial_failure_rolls_back_all_tables_and_marker(self):
        with patch.object(migration,'statements',return_value=migration.statements()[:5]+['INVALID SQL']):
            with self.assertRaises(sqlite3.OperationalError):
                migration.prepare_stage5_fixture(self.runtime,allowed_test_path=self.path)
        self.unchanged()

    def test_missing_explicit_target_and_foreign_keys_rejected(self):
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(ValueError):
                migration.migrate_test_connection(db,allowed_test_path=self.path)
            db.execute('PRAGMA foreign_keys=ON')
            with self.assertRaises(ValueError):
                migration.migrate_test_connection(db)
        self.unchanged()

    def test_invalid_target_rejected_before_connect(self):
        for target in (self.path.parent/'absent.sqlite',Path(__file__).resolve()):
            self.runtime.DATABASE=str(target)
            with self.assertRaises(ValueError):
                migration.prepare_stage5_fixture(self.runtime,allowed_test_path=target)
        self.assertEqual(self.calls,0); self.unchanged()

    def test_postgres_not_executed(self):
        self.runtime.DATABASE_URL='postgresql://fixture.invalid/test'
        with self.assertRaises(ValueError):
            migration.prepare_stage5_fixture(self.runtime,allowed_test_path=self.path)
        sql=migration.postgres_review_sql()
        self.assertIn('NOT APPLIED',sql)
        self.assertIn('pg_advisory_xact_lock',sql)
        self.assertNotIn('typeof(',sql)
        self.assertNotIn('INSERT INTO stage3_fund_records',sql)
        self.assertEqual(self.calls,0); self.unchanged()

    def test_wrong_connection_rejected(self):
        other=self.path.parent/'other.sqlite'
        with closing(sqlite3.connect(other)) as db:
            seed(db,other); before=schema(db)
        self.runtime.get_db=lambda:sqlite3.connect(other)
        with self.assertRaises(ValueError):
            migration.prepare_stage5_fixture(self.runtime,allowed_test_path=self.path)
        with closing(sqlite3.connect(other)) as db:
            self.assertEqual(schema(db),before)
        self.unchanged()

    def test_attached_database_rejected(self):
        for attached in (':memory:',str(self.path.parent/'extra.sqlite')):
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('PRAGMA foreign_keys=ON'); db.execute('ATTACH DATABASE ? AS extra',(attached,))
                with self.assertRaises(ValueError):
                    migration.migrate_test_connection(db,allowed_test_path=self.path)
        self.unchanged()

    def money_fixture(self,db):
        seed(db); stage4(db); migration.migrate_test_connection(db)
        db.execute('''INSERT INTO stage3_invoice_revisions(invoice_id,customer_id,period,timezone_name,
            revision,content_hash,snapshot_json,recorded_at,source_key,status)
            VALUES ('invoice',1,'2026-10','Asia/Tokyo',1,'fixture','{}','fixture','invoice','draft')''')

    def insert_bill(self,db,amount):
        db.execute('''INSERT INTO stage5_physical_bills(bill_id,invoice_id,customer_id,period,timezone_name,
            due_date,source_hash,snapshot_json,amount_yen,currency,review_reference,actor_user_id,recorded_at)
            VALUES ('bill','invoice',1,'2026-10','Asia/Tokyo','2026-11-10','fixture','{}',?,'JPY','fixture',1,'fixture')''',(amount,))

    def insert_consent(self,db):
        db.execute('''INSERT INTO stage5_card_consents(consent_id,customer_id,terms_reference,
            provider_customer_reference,payment_method_reference,off_session,actor_user_id,recorded_at)
            VALUES ('consent',1,'fixture','cus_test_fixture','pm_test_fixture',0,1,'fixture')''')

    def insert_attempt(self,db,amount):
        db.execute('''INSERT INTO stage5_payment_attempts(attempt_id,bill_id,consent_id,customer_id,
            amount_yen,currency,off_session,provider_idempotency_key,actor_user_id,recorded_at)
            VALUES ('attempt','bill','consent',1,?,'JPY',0,'attempt-key',1,'fixture')''',(amount,))

    def test_bill_sqlite_rejects_fractional_and_non_numeric_amounts(self):
        with closing(sqlite3.connect(':memory:')) as db:
            self.money_fixture(db)
            for amount in (1.5,'1.5','not-a-number'):
                with self.subTest(amount=amount), self.assertRaises(sqlite3.IntegrityError):
                    self.insert_bill(db,amount)
            self.insert_bill(db,1100)
            self.assertEqual(db.execute('SELECT amount_yen,typeof(amount_yen) FROM stage5_physical_bills').fetchone(),(1100,'integer'))

    def test_attempt_sqlite_rejects_fractional_and_non_numeric_amounts(self):
        with closing(sqlite3.connect(':memory:')) as db:
            self.money_fixture(db); self.insert_bill(db,1100); self.insert_consent(db)
            for amount in (1.5,'1.5','not-a-number'):
                with self.subTest(amount=amount), self.assertRaises(sqlite3.IntegrityError):
                    self.insert_attempt(db,amount)
            self.insert_attempt(db,1100)
            self.assertEqual(db.execute('SELECT amount_yen,typeof(amount_yen) FROM stage5_payment_attempts').fetchone(),(1100,'integer'))

    def test_refund_sqlite_rejects_fractional_and_non_numeric_amounts(self):
        with closing(sqlite3.connect(':memory:')) as db:
            self.money_fixture(db); self.insert_bill(db,1100); self.insert_consent(db); self.insert_attempt(db,1100)
            sql='''INSERT INTO stage5_payment_refunds(refund_id,attempt_id,customer_id,amount_yen,currency,
                reason,provider_idempotency_key,actor_user_id,recorded_at)
                VALUES ('refund','attempt',1,?,'JPY','fixture','refund-key',1,'fixture')'''
            for amount in (1.5,'1.5','not-a-number'):
                with self.subTest(amount=amount), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql,(amount,))
            db.execute(sql,(700,))
            self.assertEqual(db.execute('SELECT amount_yen,typeof(amount_yen) FROM stage5_payment_refunds').fetchone(),(700,'integer'))


if __name__=='__main__':
    unittest.main()
