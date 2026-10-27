"""Scope, failure and non-destructive schema checks; no real DB or network."""
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from staging_environment import StagingConfigurationError, validate_environment
import staging_review as review


class StagingReviewScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix='kaika-review-scope-')
        self.addCleanup(self.directory.cleanup)
        self.volume = Path(self.directory.name)
        (self.volume / '.kaika-staging-volume').write_text('staging\n', encoding='utf-8')
        self.environment = {
            'KAIKA_RUNTIME_ENV': 'staging', 'KAIKA_STAGING_REVIEW_ENABLED': '1',
            'DATABASE_URL': 'postgresql://fixture:fixture-password@db.invalid/kaika_staging',
            'SECRET_KEY': 'isolated-review-secret-' * 3,
            'STAGING_ADMIN_PASSWORD': 'fixture-admin-password-012345',
            'STAGING_NORMAL_PASSWORD': 'fixture-normal-password-012345',
            'STAGING_BUSINESS_PASSWORD': 'fixture-business-password-012345',
            'STAGING_HOSTNAME': 'review.example.invalid',
        }
        self.config = validate_environment(self.environment)
        self.app = Flask(__name__)
        self.app.config['UPLOAD_FOLDER'] = str(self.volume)
        self.app.extensions['kaika_staging'] = True
        self.runtime = SimpleNamespace(app=self.app, DATABASE_URL=self.config.database_url)
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value
        self.cursor.fetchone.side_effect = [('kaika_staging',), None]
        self.cursor.fetchall.return_value = [('staging',)]

    def bootstrap(self):
        return review.bootstrap_staging_review(self.runtime, self.config, connection=self.connection)

    def test_absent_opt_in_does_not_connect_or_read_volume_and_disables_inherited_ui_flags(self):
        self.app.config.update({name: True for name in review.REVIEW_FLAGS + review.DISABLED_REVIEW_FLAGS})
        with patch.dict(os.environ, {}, clear=True), patch.object(review, 'check_upload_disk') as disk:
            self.assertEqual(self.bootstrap(), {'enabled': False})
        self.connection.cursor.assert_not_called()
        disk.assert_not_called()
        self.assertTrue(all(self.app.config[name] is False for name in review.REVIEW_FLAGS + review.DISABLED_REVIEW_FLAGS))

    def test_only_exact_explicit_opt_in_is_accepted(self):
        for value in ('true', 'yes', '', '2'):
            with self.subTest(value=value), patch.dict(os.environ, {'KAIKA_STAGING_REVIEW_ENABLED': value}, clear=True):
                with self.assertRaises(StagingConfigurationError):
                    self.bootstrap()
        self.connection.cursor.assert_not_called()

    def test_production_or_mismatched_runtime_cannot_start_review(self):
        for key, value in (('KAIKA_RUNTIME_ENV', 'production'), ('DATABASE_URL', 'postgresql://a:b@db.invalid/production')):
            environment = {**self.environment, key: value}
            with self.subTest(key=key), patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(StagingConfigurationError):
                    self.bootstrap()
        self.runtime.DATABASE_URL = 'postgresql://fixture:other@db.invalid/kaika_staging'
        with patch.dict(os.environ, self.environment, clear=True), self.assertRaises(StagingConfigurationError):
            self.bootstrap()
        self.connection.cursor.assert_not_called()

    def test_staging_http_boundary_must_already_be_registered(self):
        self.app.extensions.pop('kaika_staging')
        with patch.dict(os.environ, self.environment, clear=True), self.assertRaises(StagingConfigurationError):
            self.bootstrap()
        self.connection.cursor.assert_not_called()

    def test_volume_failure_stops_before_schema(self):
        with patch.dict(os.environ, self.environment, clear=True), patch.object(review, 'check_upload_disk',
                side_effect=StagingConfigurationError('No staging volume')):
            with self.assertRaises(StagingConfigurationError):
                self.bootstrap()
        self.connection.cursor.assert_not_called()

    def test_wrong_actual_database_marker_or_quarantine_prevents_schema(self):
        cases = [('actual-database', [('production',)], [('staging',)]),
                 ('marker', [('kaika_staging',)], [('production',)]),
                 ('quarantine', [('kaika_staging',), ('kaika_recovery_quarantine',)], [('staging',)])]
        for name, singles, rows in cases:
            self.cursor.fetchone.side_effect = singles
            self.cursor.fetchall.return_value = rows
            with self.subTest(name=name), patch.dict(os.environ, self.environment, clear=True), \
                    patch.object(review, 'check_upload_disk'), patch.object(review, 'apply_additive_schema') as ddl:
                with self.assertRaises(StagingConfigurationError):
                    self.bootstrap()
                ddl.assert_not_called()
                self.assertTrue(all(self.app.config[flag] is False for flag in review.REVIEW_FLAGS))
        self.assertEqual(self.connection.rollback.call_count, 3)
        self.connection.commit.assert_not_called()

    def test_schema_failure_rolls_back_and_redacts_provider_diagnostic(self):
        with patch.dict(os.environ, self.environment, clear=True), patch.object(review, 'check_upload_disk'), \
                patch.object(review, '_check_columns'), patch.object(review, 'apply_additive_schema',
                    side_effect=ValueError('sensitive customer row and connection details')):
            with self.assertRaises(StagingConfigurationError) as caught:
                self.bootstrap()
        self.assertNotIn('sensitive', str(caught.exception))
        self.connection.rollback.assert_called_once()
        self.connection.commit.assert_not_called()
        self.assertTrue(all(self.app.config[flag] is False for flag in review.REVIEW_FLAGS))

    def test_success_enables_only_review_after_commit_and_never_seeds_work(self):
        with patch.dict(os.environ, self.environment, clear=True), patch.object(review, 'check_upload_disk'), \
                patch.object(review, '_check_columns'), patch.object(review, 'apply_additive_schema', return_value=291) as ddl:
            result = self.bootstrap()
        self.assertTrue(result['enabled'])
        self.assertEqual(result['legacy_reconciliation'], 'manual')
        ddl.assert_called_once_with(self.cursor)
        self.connection.commit.assert_called_once()
        self.connection.rollback.assert_not_called()
        self.assertTrue(all(self.app.config[flag] is True for flag in review.REVIEW_FLAGS))
        self.assertTrue(all(self.app.config[flag] is False for flag in review.DISABLED_REVIEW_FLAGS))
        statements = [call.args[0] for call in self.cursor.execute.call_args_list]
        self.assertFalse(any(re.match(r'\s*(INSERT|UPDATE|DELETE|ALTER)', sql) for sql in statements))


class StagingReviewSchemaTests(unittest.TestCase):
    def test_all_current_postgres_definitions_use_non_destructive_executor(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        with patch.object(review, '_check_columns'):
            self.assertGreater(review.apply_additive_schema(cursor), 200)
        executed = [call.args[0] for call in cursor.execute.call_args_list]
        self.assertFalse(any(sql.startswith(('DROP', 'ALTER', 'UPDATE', 'DELETE', 'CREATE OR REPLACE')) for sql in executed))
        inserts = [sql for sql in executed if sql.startswith('INSERT')]
        self.assertTrue(inserts)
        self.assertTrue(all(re.match(r'INSERT INTO (business_registry_schema|stage[3-7]_schema)\(version\)', sql) for sql in inserts))
        self.assertFalse(any('AUTOINCREMENT' in sql or 'PRAGMA ' in sql for sql in executed))

    def test_unrecognized_mutation_is_refused(self):
        for statement in ('UPDATE merchandise SET purchase_price=0', 'DELETE FROM users',
                          'DROP TABLE merchandise', 'ALTER TABLE merchandise ADD COLUMN invented TEXT',
                          "INSERT INTO stage3_charge_drafts VALUES ('invented')"):
            cursor = MagicMock()
            with self.subTest(statement=statement), patch.object(review, 'schema_statements', return_value=(statement,)):
                with self.assertRaises(StagingConfigurationError):
                    review.apply_additive_schema(cursor)
            cursor.execute.assert_not_called()

    def test_missing_existing_columns_are_not_silently_backfilled(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [('id',), ('user_id',)]
        with self.assertRaises(StagingConfigurationError):
            review._check_columns(cursor, 'merchandise', {'id', 'user_id', 'custody_location'})
        self.assertTrue(all(call.args[0].startswith('SELECT ') for call in cursor.execute.call_args_list))

    def test_column_discovery_handles_constraints_and_comma_strings(self):
        statement = "CREATE TABLE IF NOT EXISTS fixture (id TEXT PRIMARY KEY, label TEXT CHECK(label IN ('a,b','c')), amount BIGINT, UNIQUE(id,label), FOREIGN KEY(id) REFERENCES users(id))"
        self.assertEqual(review._column_names(statement), {'id', 'label', 'amount'})

    def test_existing_function_must_match_and_is_never_replaced(self):
        sql = "CREATE OR REPLACE FUNCTION fixture_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$"
        cursor = MagicMock()
        cursor.fetchall.return_value = [(" BEGIN RAISE EXCEPTION 'append_only'; END; ", 'plpgsql', True, False, None)]
        review._function(cursor, sql, 'fixture_guard')
        self.assertEqual(len(cursor.execute.call_args_list), 1)
        cursor.fetchall.return_value = [('BEGIN RETURN NEW; END;', 'plpgsql', True, False, None)]
        with self.assertRaises(StagingConfigurationError):
            review._function(cursor, sql, 'fixture_guard')
        self.assertTrue(all(call.args[0].startswith('SELECT ') for call in cursor.execute.call_args_list))

    def test_existing_trigger_must_have_guard_events_and_enabled_state(self):
        sql = 'CREATE TRIGGER fixture_guard BEFORE UPDATE OR DELETE ON fixture FOR EACH ROW EXECUTE FUNCTION fixture_reject()'
        cursor = MagicMock()
        cursor.fetchall.return_value = [(27, 'O', 0, None, '', 0, 'fixture_reject', 'public')]
        review._trigger(cursor, sql, 'fixture_guard')
        self.assertEqual(len(cursor.execute.call_args_list), 1)
        for kind, enabled, function in ((7, 'O', 'fixture_reject'), (27, 'D', 'fixture_reject'), (27, 'O', 'unprotected')):
            cursor.fetchall.return_value = [(kind, enabled, 0, None, '', 0, function, 'public')]
            with self.subTest(kind=kind, enabled=enabled, function=function), self.assertRaises(StagingConfigurationError):
                review._trigger(cursor, sql, 'fixture_guard')
        self.assertTrue(all(call.args[0].startswith('SELECT ') for call in cursor.execute.call_args_list))

    def test_schema_classification_includes_guarded_insert_triggers(self):
        sql = 'CREATE TRIGGER scope_guard BEFORE INSERT ON fixture FOR EACH ROW EXECUTE FUNCTION fixture_scope()'
        cursor = MagicMock()
        cursor.fetchall.return_value = [(7, 'O', 0, None, '', 0, 'fixture_scope', 'public')]
        review._trigger(cursor, sql, 'scope_guard')
        self.assertEqual(len(cursor.execute.call_args_list), 1)


if __name__ == '__main__':
    unittest.main()
