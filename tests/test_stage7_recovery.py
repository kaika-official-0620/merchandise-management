"""Only fresh fictional DBs/files; no legacy app import or live connection."""
from contextlib import closing
import ast
import sqlite3
import unittest
from unittest.mock import Mock

from recovery_runtime_guard import guard_new_connection, RecoveryQuarantineError
from scripts import platform_recovery as recovery
from scripts.verify_stage7_recovery import ROOT, run_drill


class RecoveryGuardTests(unittest.TestCase):
    def test_empty_database_is_unchanged_and_usable(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            self.assertIs(guard_new_connection(connection),connection)
            self.assertEqual(connection.execute('SELECT name FROM sqlite_master').fetchall(),[])
            self.assertFalse(connection.in_transaction)

    def test_any_marker_state_refuses_and_closes_database(self):
        for state in ('quarantined','apparently-cleared'):
            with self.subTest(state=state):
                connection=sqlite3.connect(':memory:')
                connection.execute('CREATE TABLE kaika_recovery_quarantine(state TEXT)')
                connection.execute('INSERT INTO kaika_recovery_quarantine VALUES(?)',(state,))
                connection.commit()
                with self.assertRaises(RecoveryQuarantineError):
                    guard_new_connection(connection)
                with self.assertRaises(sqlite3.ProgrammingError):
                    connection.execute('SELECT 1')

    def test_postgres_normal_check_finishes_metadata_transaction(self):
        connection=Mock()
        connection.cursor.return_value.fetchone.return_value=(None,)
        guard_new_connection(connection,postgres=True)
        connection.cursor.return_value.execute.assert_called_once_with("SELECT to_regclass('public.kaika_recovery_quarantine')")
        connection.rollback.assert_called_once_with()
        connection.close.assert_not_called()

    def test_postgres_marker_rejects_and_closes_without_business_statement(self):
        connection=Mock()
        connection.cursor.return_value.fetchone.return_value={'to_regclass':'kaika_recovery_quarantine'}
        with self.assertRaises(RecoveryQuarantineError):
            guard_new_connection(connection,postgres=True)
        self.assertEqual(connection.cursor.return_value.execute.call_count,1)
        connection.close.assert_called_once_with()

    def test_driver_or_cleanup_failure_never_prints_private_details(self):
        connection=Mock()
        connection.cursor.return_value.execute.side_effect=RuntimeError('secret-fixture-connection')
        connection.cursor.return_value.close.side_effect=RuntimeError('secret-fixture-cursor')
        connection.close.side_effect=RuntimeError('secret-fixture-password')
        with self.assertRaises(RecoveryQuarantineError) as raised:
            guard_new_connection(connection,postgres=True)
        self.assertNotIn('secret',str(raised.exception))
        connection.close.assert_called_once_with()

    def test_both_actual_connectors_guard_before_timezone_or_row_factory(self):
        tree=ast.parse((ROOT/'app.py').read_text(encoding='utf-8-sig'))
        connectors=[node for node in ast.walk(tree) if isinstance(node,ast.FunctionDef) and node.name=='get_db']
        self.assertEqual(len(connectors),2)
        for function in connectors:
            calls=[node for node in function.body if isinstance(node,ast.Expr) and isinstance(node.value,ast.Call)
                and isinstance(node.value.func,ast.Name) and node.value.func.id=='guard_new_connection']
            self.assertEqual(len(calls),1)
            guard=calls[0]
            later=[node.lineno for node in ast.walk(function) if isinstance(node,ast.Attribute)
                and node.attr in ('autocommit','row_factory')]
            self.assertTrue(later)
            self.assertLess(guard.lineno,min(later))

    def test_partial_notification_schema_rolls_back_quarantine_changes(self):
        with closing(sqlite3.connect(':memory:')) as connection:
            connection.executescript('''CREATE TABLE stage6_destinations(revoked_at BIGINT);
                INSERT INTO stage6_destinations VALUES(NULL);
                CREATE TABLE stage6_notification_outbox(delivery_id TEXT,state TEXT);''')
            with self.assertRaisesRegex(recovery.RecoveryError,'audit schema'):
                recovery.quarantine(connection,False,{})
            self.assertIsNone(connection.execute('SELECT revoked_at FROM stage6_destinations').fetchone()[0])
            self.assertFalse(connection.execute("SELECT name FROM sqlite_master WHERE name='kaika_recovery_quarantine'").fetchall())


class Stage7RecoveryDrillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report,cls.path=run_drill()

    def test_all_schema_rows_and_business_invariants_survive(self):
        self.assertEqual(self.report['failures'],[])
        self.assertTrue(self.report['include_stage7'])
        self.assertIn('rows_stage7_observations',self.report['checks'])
        self.assertIn('rows_stage7_economic_entries',self.report['checks'])
        self.assertIn('rows_stage5_payment_attempts',self.report['checks'])

    def test_read_permissions_and_current_photo_owner_survive(self):
        self.assertTrue(self.report['checks']['tenant_access_denied_all'])
        self.assertTrue(self.report['checks']['photo_access_owner_only'])
        self.assertTrue(self.report['checks']['semantic_customer_items'])

    def test_unknown_send_results_require_reconciliation_not_resend(self):
        restored=self.report['quarantine']
        self.assertEqual(restored['unknown_notifications'],3)
        self.assertEqual(restored['suppressed_notifications'],4)
        self.assertTrue(restored['external_reconciliation_required'])
        self.assertTrue(self.report['checks']['ordinary_runtime_refuses_quarantined_database'])

    def test_fixture_proves_no_real_or_postgres_readiness(self):
        for key in ('production_data_accessed','application_started','external_communications','real_payments',
                    'live_notifications','postgres_verified_this_drill','age_encryption_verified_this_drill'):
            self.assertFalse(self.report[key])
        self.assertIn('not accepted RPO/RTO',self.report['timing_scope'])


if __name__=='__main__':
    unittest.main()
