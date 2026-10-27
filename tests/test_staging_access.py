"""One-use access tests on new isolated fictional SQLite fixtures only."""
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, current_user
from flask_login.utils import encode_cookie

import staging_access as access
from staging_environment import StagingConfigurationError, register_staging_boundary, validate_environment


class Inputs(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.values = {}
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == 'input' and fields.get('name'):
            self.values[fields['name']] = fields.get('value', '')


class StagingAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kaika-staging-access-test-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fictional.sqlite3'
        self.environment = {
            'KAIKA_RUNTIME_ENV': 'staging', 'KAIKA_STAGING_ACCESS_ENABLED': '1',
            'DATABASE_URL': 'postgresql://fixture:fixture-password@db.invalid/kaika_staging',
            'SECRET_KEY': 'isolated-access-secret-' * 3, 'STAGING_HOSTNAME': access.HOST,
            **{'STAGING_' + role.upper() + '_PASSWORD': 'fixture-' + role + '-password-012345'
               for role in ('admin', 'normal', 'business')},
        }
        self.env = patch.dict(os.environ, self.environment, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.config = validate_environment(self.environment)
        self.app = Flask(__name__)
        self.app.secret_key = self.environment['SECRET_KEY']
        self.app.testing = True
        manager = LoginManager(self.app)

        class User(UserMixin):
            def __init__(self, row):
                self.id, self.username, self.role = row['id'], row['username'], row['role']

        def get_db():
            db = sqlite3.connect(self.path)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            return db

        def load(user_id):
            db = get_db()
            try:
                row = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
                return User(row) if row else None
            finally:
                db.close()

        manager.user_loader(load)
        self.runtime = SimpleNamespace(app=self.app, DATABASE_URL=self.config.database_url,
                                       get_db=get_db, build_user_from_record=User)
        self.db = get_db()
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE kaika_environment(name TEXT PRIMARY KEY);
            INSERT INTO kaika_environment VALUES ('staging');
            CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT UNIQUE,role TEXT,password_hash TEXT);
            INSERT INTO users VALUES (1,'staging_business','user','fixture-original-password-hash');
            INSERT INTO users VALUES (2,'staging_admin','admin','fixture-admin-password-hash');
            INSERT INTO users VALUES (3,'staging_normal','user','fixture-normal-password-hash');
            CREATE TABLE kaika_staging_seeds(seed_key TEXT PRIMARY KEY,record_id INTEGER NOT NULL);
            INSERT INTO kaika_staging_seeds VALUES ('user:business',1);
        ''')

        @self.app.get('/login')
        def login():
            return 'fixture login'

        @self.app.get('/workspace-home')
        def home():
            return jsonify(user=current_user.username, role=current_user.role)

        register_staging_boundary(self.runtime)
        access.register_staging_access(self.runtime, self.config, connection=self.db)
        self.client = self.app.test_client()
        self.origin = 'https://' + access.HOST

    def mint(self, ttl=900):
        return access.mint_staging_access(self.config, connection=self.db, ttl_seconds=ttl)

    def claim(self, token):
        return access.claim_staging_access(self.config, connection=self.db, token=token)

    def csrf(self, client=None):
        response = (client or self.client).get('/staging/access', base_url=self.origin)
        self.assertEqual(response.status_code, 200)
        return Inputs(response.get_data(as_text=True)).values['csrf_token']

    def post(self, token, csrf, *, client=None, origin=None):
        return (client or self.client).post('/staging/access/consume', base_url=self.origin,
            json={'token': token, 'csrf_token': csrf},
            headers={'Origin': self.origin if origin is None else origin})

    def consumed(self):
        return self.db.execute('SELECT consumed_at FROM kaika_staging_access_tokens').fetchone()[0]

    def test_mint_stores_hash_only_and_is_bound_to_fixed_business_account(self):
        token = self.mint()
        self.assertRegex(token, r'^[A-Za-z0-9_-]{43}$')
        record = dict(self.db.execute('SELECT * FROM kaika_staging_access_tokens').fetchone())
        self.assertEqual(record['token_hash'], access._digest(token))
        self.assertNotIn(token, json.dumps(record))
        self.assertNotIn('fixture-original-password-hash', json.dumps(record))
        self.assertEqual(record['user_id'], 1)
        self.assertEqual(record['expires_at'] - record['created_at'], 900)
        self.assertIsNone(record['consumed_at'])

    def test_explicit_opt_in_and_exact_staging_host_required(self):
        for key, value in (('KAIKA_STAGING_ACCESS_ENABLED', '0'), ('KAIKA_RUNTIME_ENV', 'production'),
                           ('STAGING_HOSTNAME', 'other.example.invalid')):
            with self.subTest(key=key), patch.dict(os.environ, {key: value}):
                with self.assertRaises(StagingConfigurationError):
                    self.mint()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM kaika_staging_access_tokens').fetchone()[0], 0)

    def test_ttl_is_capped_and_not_coerced(self):
        for ttl in (0, -1, 901, True, '900', 1.5):
            with self.subTest(ttl=ttl), self.assertRaises(StagingConfigurationError):
                self.mint(ttl)

    def test_claim_is_once_and_consumed_evidence_cannot_be_rewritten_or_deleted(self):
        token = self.mint()
        self.assertEqual(self.claim(token), 1)
        self.assertIsNone(self.claim(token))
        self.assertIsNotNone(self.consumed())
        for sql in ('UPDATE kaika_staging_access_tokens SET consumed_at=NULL',
                    'UPDATE kaika_staging_access_tokens SET user_id=2',
                    'DELETE FROM kaika_staging_access_tokens'):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)
            self.db.rollback()
        self.assertIsNotNone(self.consumed())

    def test_expired_or_future_token_cannot_be_claimed(self):
        with patch.object(access, '_now', return_value=1000):
            token = self.mint(10)
        for now in (999, 1010, 2000):
            with self.subTest(now=now), patch.object(access, '_now', return_value=now):
                self.assertIsNone(self.claim(token))
        self.assertIsNone(self.consumed())

    def test_password_rotation_invalidates_outstanding_token(self):
        token = self.mint()
        self.db.execute("UPDATE users SET password_hash='fixture-rotated-hash' WHERE id=1")
        self.db.commit()
        self.assertIsNone(self.claim(token))
        self.assertIsNone(self.consumed())

    def test_admin_role_renamed_user_or_swapped_seed_cannot_mint_or_claim(self):
        for sql in ("UPDATE users SET role='admin' WHERE id=1",
                    "UPDATE users SET username='changed-business' WHERE id=1",
                    "UPDATE kaika_staging_seeds SET record_id=3 WHERE seed_key='user:business'"):
            token = self.mint()
            self.db.execute(sql)
            self.db.commit()
            with self.subTest(sql=sql):
                self.assertIsNone(self.claim(token))
                with self.assertRaises(StagingConfigurationError):
                    self.mint()
            self.db.execute("UPDATE users SET role='user',username='staging_business' WHERE id=1")
            self.db.execute("UPDATE kaika_staging_seeds SET record_id=1 WHERE seed_key='user:business'")
            self.db.commit()

    def test_closed_account_and_quarantined_database_are_rejected(self):
        token = self.mint()
        self.db.execute('CREATE TABLE account_deletion_requests(customer_id INTEGER,state TEXT)')
        self.db.execute("INSERT INTO account_deletion_requests VALUES (1,'access_closed')")
        self.db.commit()
        self.assertIsNone(self.claim(token))
        with self.assertRaises(StagingConfigurationError):
            self.mint()
        self.db.execute('CREATE TABLE kaika_recovery_quarantine(id INTEGER)')
        self.db.commit()
        with self.assertRaises(StagingConfigurationError):
            self.claim(token)

    def test_get_is_display_only_even_with_scanner_query_and_get_consume(self):
        token = self.mint()
        page = self.client.get('/staging/access', base_url=self.origin)
        self.assertEqual(page.status_code, 200)
        self.assertIn('確認サイトを開く', page.get_data(as_text=True))
        self.assertIsNone(self.consumed())
        self.assertEqual(self.client.get('/staging/access/consume', base_url=self.origin).status_code, 405)
        response = self.client.get('/staging/access', query_string={'token': token}, base_url=self.origin)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(token, response.get_data(as_text=True))
        self.assertIsNone(self.consumed())
        self.assertEqual(self.client.get('/workspace-home', base_url=self.origin).status_code, 302)

    def test_landing_has_no_external_assets_and_removes_fragment_before_submit(self):
        response = self.client.get('/staging/access', base_url=self.origin)
        body = response.get_data(as_text=True)
        self.assertLess(body.index('history.replaceState'), body.index('fetch('))
        self.assertIn("addEventListener('submit'", body)
        self.assertNotIn('<script src=', body)
        self.assertNotIn('unsafe-inline', response.headers['Content-Security-Policy'])
        self.assertIn("script-src 'nonce-", response.headers['Content-Security-Policy'])
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn('noindex', response.headers['X-Robots-Tag'])

    def test_csrf_origin_and_https_fail_before_consumption(self):
        token, csrf = self.mint(), self.csrf()
        for origin in ('', 'https://other.example.invalid', self.origin + '/', 'null'):
            with self.subTest(origin=origin):
                self.assertEqual(self.post(token, csrf, origin=origin).status_code, 403)
        self.assertEqual(self.post(token, 'wrong-csrf').status_code, 403)
        self.assertEqual(self.post(token, 'あ' * 43).status_code, 403)
        self.assertEqual(self.post(token, {'invalid': 'csrf'}).status_code, 403)
        other = self.app.test_client()
        self.assertEqual(self.post(token, csrf, client=other).status_code, 403)
        self.assertEqual(self.client.post('/staging/access/consume', base_url='http://' + access.HOST,
            json={'token': token, 'csrf_token': csrf}, headers={'Origin': self.origin}).status_code, 403)
        self.assertIsNone(self.consumed())

    def test_success_clears_previous_admin_session_and_remember_cookie(self):
        token = self.mint()
        self.client.set_cookie('kaika_staging_remember', encode_cookie('2', key=self.app.secret_key), domain=access.HOST)
        with self.client.session_transaction(base_url=self.origin) as state:
            state['_user_id'] = '2'
            state['previous_admin_state'] = 'must-not-survive'
        response = self.post(token, self.csrf())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'ok': True, 'next': '/workspace-home'})
        self.assertIsNone(self.client.get_cookie('kaika_staging_remember', domain=access.HOST))
        with self.client.session_transaction(base_url=self.origin) as state:
            self.assertEqual(state['_user_id'], '1')
            self.assertNotIn('previous_admin_state', state)
            self.assertNotIn('_staging_access_csrf', state)
        self.assertEqual(self.client.get('/workspace-home', base_url=self.origin).json,
                         {'user': 'staging_business', 'role': 'user'})

    def test_web_reuse_fails_from_another_browser_with_opaque_error(self):
        token = self.mint()
        self.assertEqual(self.post(token, self.csrf()).status_code, 200)
        other = self.app.test_client()
        response = self.post(token, self.csrf(other), client=other)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json, {'ok': False, 'message': access.MESSAGE})
        self.assertNotIn(token, response.get_data(as_text=True))
        self.assertEqual(other.get('/workspace-home', base_url=self.origin).status_code, 302)

    def test_invalid_claim_rate_limit_persists_across_csrf_refresh(self):
        token, csrf = self.mint(), self.csrf()
        for _ in range(access.MAX_ATTEMPTS):
            self.assertEqual(self.post('invalid', csrf).status_code, 400)
        self.assertEqual(self.post(token, self.csrf()).status_code, 429)
        self.assertIsNone(self.consumed())
        other = self.app.test_client()
        self.assertEqual(self.post(token, self.csrf(other), client=other).status_code, 200)

    def test_malformed_or_oversize_request_never_echoes_token(self):
        token, csrf = self.mint(), self.csrf()
        for body in ('not json', json.dumps([token]), json.dumps({'token': token, 'csrf_token': csrf, 'extra': 'x' * 3000})):
            response = self.client.post('/staging/access/consume', base_url=self.origin, data=body,
                content_type='application/json', headers={'Origin': self.origin})
            self.assertIn(response.status_code, (400, 403))
            self.assertNotIn(token, response.get_data(as_text=True))
        self.assertIsNone(self.consumed())

    def test_runtime_opt_out_stops_existing_routes_and_minting(self):
        token, csrf = self.mint(), self.csrf()
        with patch.dict(os.environ, {'KAIKA_STAGING_ACCESS_ENABLED': '0'}):
            self.assertEqual(self.post(token, csrf).status_code, 404)
            with self.assertRaises(StagingConfigurationError):
                self.mint()
        self.assertIsNone(self.consumed())


if __name__ == '__main__':
    unittest.main()
