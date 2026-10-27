"""Exercise retired operations and startup rules without importing the live app.

Real schema initialization runs only against disposable SQLite. HTTP tests use
a new Flask app; provider methods and scheduler jobs never execute.
"""
import ast
from contextlib import closing
from copy import deepcopy
import hashlib
from pathlib import Path
import secrets
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask

import runtime_security
from runtime_security import (LegacyOperationDisabled, StartupConfigurationError,
                              install_runtime_security, legacy_operation_disabled,
                              require_session_secret)


ROOT = Path(__file__).resolve().parents[1]


def extract(node, namespace):
    node = deepcopy(node)
    node.decorator_list = []
    namespace = dict(namespace)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 '<isolated-existing-function>', 'exec'), namespace)
    return namespace[node.name], namespace


class RuntimeSecurityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ast.parse((ROOT / 'app.py').read_text(encoding='utf-8-sig'))
        cls.functions = {node.name: node for node in cls.tree.body if isinstance(node, ast.FunctionDef)}

    def test_missing_weak_and_retired_session_secrets_fail_without_echoing_values(self):
        for value in (None, '', 'short-fixture', 'x' * 64, ' ' + secrets.token_urlsafe(48)):
            with self.subTest(configured=bool(value)):
                with self.assertRaises(StartupConfigurationError) as raised:
                    require_session_secret({'SECRET_KEY': value})
                if value:
                    self.assertNotIn(value, str(raised.exception))
        retired_fixture = secrets.token_urlsafe(48)
        with patch.object(runtime_security, '_RETIRED_SESSION_KEY_DIGESTS',
                          {hashlib.sha256(retired_fixture.encode()).hexdigest()}):
            with self.assertRaises(StartupConfigurationError) as raised:
                require_session_secret({'SECRET_KEY': retired_fixture})
            self.assertNotIn(retired_fixture, str(raised.exception))

    def test_explicit_random_session_secret_is_used_unchanged(self):
        value = secrets.token_urlsafe(48)
        self.assertEqual(require_session_secret({'SECRET_KEY': value}), value)

    def test_startup_requires_secret_before_database_initialization(self):
        assignment = next(node for node in self.tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Attribute) and target.attr == 'secret_key'
                                  for target in node.targets))
        db_calls = [node for node in self.tree.body if isinstance(node, ast.Expr)
                    and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == 'init_db']
        self.assertEqual(len(db_calls), 1)
        self.assertLess(assignment.lineno, db_calls[0].lineno)
        probe = SimpleNamespace(secret_key=None)
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(StartupConfigurationError):
                exec(compile(ast.Module(body=[assignment], type_ignores=[]), '<startup-secret>', 'exec'),
                     {'app': probe, 'require_session_secret': require_session_secret})
        self.assertIsNone(probe.secret_key)

    def test_new_database_has_no_automatic_admin_and_existing_credentials_survive_reinitialization(self):
        sqlite_initializer = [node for node in ast.walk(self.tree)
                              if isinstance(node, ast.FunctionDef) and node.name == 'init_db'][-1]
        with tempfile.TemporaryDirectory(prefix='kaika-security-schema-') as directory:
            path = Path(directory) / 'fixture.sqlite'
            initialize, _ = extract(sqlite_initializer, {'get_db': lambda: sqlite3.connect(path)})
            initialize()
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM users').fetchone()[0], 0)
                stored_hash = secrets.token_urlsafe(48)
                conn.execute('INSERT INTO users(username,email,password_hash,role) VALUES(?,?,?,?)',
                             ('fixture-owner', 'fixture@example.invalid', stored_hash, 'owner'))
                conn.commit()
            initialize()
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('SELECT username,password_hash,role FROM users').fetchall(),
                                 [('fixture-owner', stored_hash, 'owner')])

    def test_neither_database_initializer_contains_implicit_account_inserts(self):
        initializers = [node for node in ast.walk(self.tree)
                        if isinstance(node, ast.FunctionDef) and node.name == 'init_db']
        self.assertEqual(len(initializers), 2)
        for node in initializers:
            sql = [part.value.upper() for part in ast.walk(node)
                   if isinstance(part, ast.Constant) and isinstance(part.value, str)]
            self.assertFalse(any('INSERT INTO USERS' in query for query in sql))

    def test_all_retired_billing_functions_refuse_even_with_provider_configuration(self):
        arguments = {'get_or_create_stripe_price': (2980,), 'admin_stripe_subscribe': (1,),
                     'admin_stripe_change_plan': (1,), 'admin_stripe_cancel_subscription': (1,),
                     'admin_stripe_success': (), 'stripe_webhook': (),
                     'admin_stripe_batch_update': (), 'api_stripe_batch_update': (),
                     'run_monthly_batch_update': ()}
        for name, args in arguments.items():
            with self.subTest(function=name):
                database = Mock(side_effect=AssertionError('DB access forbidden'))
                stripe = Mock()
                view, _ = extract(self.functions[name], {
                    'legacy_operation_disabled': legacy_operation_disabled, 'get_db': database,
                    'stripe': stripe, 'STRIPE_ENABLED': True, 'STRIPE_SECRET_KEY': secrets.token_urlsafe(32)})
                with self.assertRaises(LegacyOperationDisabled):
                    view(*args)
                database.assert_not_called()
                self.assertEqual(stripe.mock_calls, [])

    def test_http_boundary_cannot_be_bypassed_by_replacing_a_legacy_view(self):
        app = Flask(__name__)
        legacy_paths = ['/stripe/webhook', '/api/stripe/batch-update', '/admin/stripe/batch-update',
                        '/admin/stripe/success', '/admin/stripe/subscribe/1',
                        '/admin/stripe/change-plan/1', '/admin/stripe/cancel-subscription/1']
        called = Mock(return_value='should not run')
        install_runtime_security(app)
        install_runtime_security(app)
        self.assertEqual(len(app.before_request_funcs[None]), 1)
        for index, path in enumerate(legacy_paths):
            app.add_url_rule(path, 'old_' + str(index), lambda: called(), methods=['GET', 'POST'])
        for index, path in enumerate(['/plans', '/billing/checkout', '/billing/webhook', '/api/plans/store-verify']):
            app.add_url_rule(path, 'new_' + str(index), lambda: 'available', methods=['GET', 'POST'])
        client = app.test_client()
        for path in legacy_paths:
            for method in ('GET', 'POST'):
                response = client.open(path, method=method, json={'user_id': 2, 'type': 'invoice.payment_succeeded'})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()['error']['code'], 'legacy_operation_disabled')
        called.assert_not_called()
        for path in ('/plans', '/billing/checkout', '/billing/webhook', '/api/plans/store-verify'):
            self.assertEqual(client.post(path).status_code, 200)

    def test_scheduler_never_registers_automatic_billing_or_property_transfer(self):
        scheduled = []
        class FixtureScheduler:
            running = False
            def __init__(self, **kwargs):
                pass
            def add_job(self, callback, *args, **kwargs):
                scheduled.append((callback, kwargs['id']))
            def start(self):
                self.running = True
        line_job, backup_job = Mock(), Mock()
        namespace = {'os': SimpleNamespace(environ={}), 'scheduler': None,
                     'SCHEDULER_ENABLED': True, 'BackgroundScheduler': FixtureScheduler,
                     'CronTrigger': lambda **kwargs: kwargs,
                     'run_scheduled_line_messages': line_job, 'run_auto_server_backup': backup_job,
                     'get_auto_backup_enabled': lambda: True, 'get_auto_backup_hour_minute': lambda: (4, 0),
                     'get_jst_now': lambda: 'fixture-time', 'print': lambda *args: None}
        initialize, namespace = extract(self.functions['init_scheduler'], namespace)
        initialize()
        self.assertEqual([identifier for _, identifier in scheduled], ['line_scheduled_messages', 'daily_server_backup'])
        self.assertTrue(namespace['scheduler'].running)
        line_job.assert_not_called(); backup_job.assert_not_called()


if __name__ == '__main__':
    unittest.main()
