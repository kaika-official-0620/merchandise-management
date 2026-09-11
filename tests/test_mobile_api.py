"""Mobile integration tests; only an explicitly created temporary SQLite DB.

No import of app/render_app, environment DB, scheduler, or external services.
Run: python -m unittest discover -s tests -p test_mobile_api.py -v
"""
from datetime import datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager, UserMixin, current_user
from PIL import Image
from werkzeug.security import generate_password_hash

from mobile_api import register_mobile_api


class FixtureUser(UserMixin):
    def __init__(self, row):
        self.__dict__.update(dict(row))

    def is_admin(self):
        return self.role in ('admin', 'owner')

    def can_edit_merchandise(self):
        return self.role == 'owner' or self.subscription_status != 'past_due'

    def has_permission(self, permission):
        return self.role == 'owner' or (self.role == 'admin' and
            (not self.admin_permissions or permission in json.loads(self.admin_permissions)))


class MobileApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash('fixture-password')

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kaika-mobile-test-')
        self.root = Path(self.temporary.name)
        self.path = self.root / 'fixture.db'
        self.static = self.root / 'static'
        self.static.mkdir()
        self.now = datetime(2026, 9, 9, 12)
        self.metrics_calls = []
        self.read_ids = set()
        self.connections = []
        self.app = Flask(__name__, static_folder=str(self.static))
        self.app.config.update(TESTING=True, SECRET_KEY='fixture-browser-only-secret')
        manager = LoginManager(self.app)

        @manager.user_loader
        def load_fixture_user(user_id):
            connection = self.db()
            try:
                row = connection.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
                return FixtureUser(row) if row else None
            finally:
                connection.close()

        @self.app.get('/browser-identity')
        def browser_identity():
            return {'user_id': current_user.get_id()}
        self.runtime = SimpleNamespace(
            app=self.app, DATABASE_URL=None, get_db=self.db,
            get_jst_now=lambda: self.now,
            build_user_from_record=lambda row: FixtureUser(row),
            merchandise_is_sold=lambda item: bool(item.get('sale_date') or item.get('item_status') == 'sold'),
            apply_inventory_display_metrics=self.metrics,
            get_fee_settings=lambda: {'fixture': True},
            enrich_item_sale_request_state=lambda item, include_all_users: None,
            attach_long_term_request_state=lambda item: None,
            resolve_inventory_mobile_status_label=lambda item: '売却済み' if item.get('is_sold') else '在庫',
            build_inventory_status_tags=lambda item: ['sold'] if item.get('is_sold') else ['active'],
            get_active_announcements=self.announcement_rows,
            serialize_announcement_payload=lambda item: dict(item),
            get_visible_announcement_ids_for_user=lambda user: [31] if user.id == 1 else [32],
            mark_announcements_read_for_user=lambda user_id, ids: self.read_ids.update((user_id, value) for value in ids),
        )
        with self.db() as conn:
            conn.executescript('''
                CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE,
                    password_hash TEXT, display_name TEXT, role TEXT,
                    subscription_status TEXT, overdue_since TEXT,
                    admin_permissions TEXT, last_login TEXT);
                CREATE TABLE merchandise (id INTEGER PRIMARY KEY, user_id INTEGER,
                    product_name TEXT, brand_name TEXT, model_number TEXT, item_condition TEXT,
                    kaika_product_code TEXT, scope TEXT, purchase_date TEXT,
                    sale_date TEXT, item_status TEXT, is_listed INTEGER, purchase_price INTEGER,
                    wholesale_price INTEGER, listing_price INTEGER, sale_price INTEGER,
                    photo_path TEXT, additional_photos TEXT, notes TEXT,
                    updated_at TEXT, updated_by INTEGER);
            ''')
            for user_id, role, status, overdue, permissions in (
                (1, 'user', 'active', None, None), (2, 'user', 'active', None, None),
                (3, 'admin', 'active', None, None), (4, 'owner', 'past_due', None, None),
                (5, 'admin', 'past_due', None, None),
                (6, 'user', 'past_due', (self.now - timedelta(days=100)).isoformat(), None),
                (7, 'admin', 'active', None, '["users"]'),
            ):
                conn.execute('''INSERT INTO users
                    (id, username, password_hash, display_name, role, subscription_status,
                     overdue_since, admin_permissions) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (user_id, f'user{user_id}', self.password_hash, f'Fixture {user_id}', role, status, overdue, permissions))
            for item_id, user_id, name, sold, listed in (
                (11, 1, 'Test bag', None, 0), (12, 1, 'Test watch', '2026-09-01', 1),
                (13, 2, 'Other user secret item', None, 1), (14, 3, 'Company item', None, 0),
            ):
                conn.execute('''INSERT INTO merchandise
                    (id,user_id,product_name,brand_name,scope,purchase_date,sale_date,is_listed,
                     purchase_price,wholesale_price,listing_price,sale_price,notes)
                    VALUES (?,?,?,'Brand','user','2026-08-01',?,?,1000,1500,3000,4000,'old note')''',
                    (item_id, user_id, name, sold, listed))
        with patch.dict(os.environ, {'MOBILE_API_ENABLED': '1'}):
            self.assertTrue(register_mobile_api(self.runtime))
        self.client = self.app.test_client()

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        self.temporary.cleanup()

    def db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def metrics(self, item, scope, fee_settings):
        self.metrics_calls.append((item['id'], scope, fee_settings))
        item['is_sold'] = self.runtime.merchandise_is_sold(item)
        item['display_purchase_price'] = item['wholesale_price'] if scope == 'user' else item['purchase_price']
        item['display_sale_price'] = item['sale_price']
        item['profit'] = 2345 if scope == 'user' else 2999
        return item

    def announcement_rows(self, user, limit=None):
        value = 31 if user.id == 1 else 32
        return [{'id': value, 'title': 'Notice', 'content': 'Fixture notice',
                 'is_read': (user.id, value) in self.read_ids}]

    def login(self, user_id=1):
        response = self.client.post('/api/mobile/v1/session', json={
            'username': f'user{user_id}', 'password': 'fixture-password'})
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()

    def auth(self, user_id=1):
        return {'Authorization': 'Bearer ' + self.login(user_id)['token']}

    def get(self, path, headers=None):
        return self.client.get('/api/mobile/v1' + path, headers=headers)

    def test_disabled_registration_never_opens_database(self):
        disabled = Flask('disabled')
        runtime = SimpleNamespace(app=disabled, get_db=lambda: self.fail('must not open DB'))
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(register_mobile_api(runtime))
        self.assertEqual(disabled.test_client().get('/api/mobile/v1/session').status_code, 404)

    def test_sessions_require_bearer_and_dont_accept_browser_cookie(self):
        with self.client.session_transaction() as browser:
            browser['_user_id'] = '3'
        response = self.get('/items')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers['X-Kaika-Mobile-API'], '1')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', response.headers)
        self.assertEqual(self.get('/session', {'Authorization': 'Bearer arbitrary'}).status_code, 401)
        self.assertEqual(self.get('/session', self.auth(1)).get_json()['user']['id'], 1)
        self.assertEqual(self.client.get('/browser-identity').get_json()['user_id'], '3')

    def test_token_hashed_at_rest_response_whitelist_and_logout(self):
        response = self.login()
        token = response['token']
        headers = {'Authorization': 'Bearer ' + token}
        self.assertNotIn('password_hash', json.dumps(response))
        self.assertFalse(response['user']['capabilities']['can_create_item'])
        with self.db() as conn:
            stored = dict(conn.execute('SELECT * FROM mobile_sessions').fetchone())
        self.assertEqual(stored['token_hash'], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, str(stored))
        self.assertEqual(self.get('/session', headers).get_json()['user']['id'], 1)
        deleted = self.client.delete('/api/mobile/v1/session', headers=headers)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.get('/session', headers).status_code, 401)

    def test_expiry_password_change_deleted_user_revoke(self):
        for mode in ('expiry', 'password', 'deleted'):
            with self.subTest(mode=mode):
                headers = self.auth()
                with self.db() as conn:
                    if mode == 'expiry':
                        conn.execute('UPDATE mobile_sessions SET expires_at=1')
                    elif mode == 'password':
                        conn.execute("UPDATE users SET password_hash='changed' WHERE id=1")
                    else:
                        conn.execute('DELETE FROM users WHERE id=1')
                self.assertEqual(self.get('/session', headers).status_code, 401)
                if mode == 'password':
                    with self.db() as conn:
                        conn.execute('UPDATE users SET password_hash=? WHERE id=1', (self.password_hash,))

    def test_bad_login_validation_and_rate_limit(self):
        for body in ({}, {'username': ['bad'], 'password': 'x'}, {'username': 'a', 'password': 'x', 'role': 'admin'}):
            self.assertEqual(self.client.post('/api/mobile/v1/session', json=body).status_code, 400)
        for _ in range(10):
            response = self.client.post('/api/mobile/v1/session', json={'username': 'user1', 'password': 'wrong'})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.post('/api/mobile/v1/session', json={'username': 'user1', 'password': 'wrong'}).status_code, 429)

    def test_lengthless_json_payload_still_has_size_limit(self):
        data = b' ' * 20000 + json.dumps({'username': 'user1', 'password': 'fixture-password'}).encode()
        response = self.client.post('/api/mobile/v1/session', data=data, content_type='application/json',
            environ_overrides={'CONTENT_LENGTH': '', 'wsgi.input_terminated': True})
        self.assertEqual(response.status_code, 413)

    def test_overdue_client_denied_admin_owner_can_login(self):
        response = self.client.post('/api/mobile/v1/session', json={'username': 'user6', 'password': 'fixture-password'})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(self.login(4)['user']['capabilities']['can_edit_items'])
        self.assertFalse(self.login(5)['user']['capabilities']['can_edit_items'])

    def test_tenant_filter_search_pagination_admin_visibility(self):
        headers = self.auth()
        response = self.get('/items?page=1&limit=1&user_id=2&scope=all', headers).get_json()
        self.assertEqual(response['total'], 2)
        self.assertTrue(response['has_more'])
        self.assertEqual(response['items'][0]['id'], 12)
        self.assertEqual(self.get('/items?page=2&limit=1', headers).get_json()['items'][0]['id'], 11)
        self.assertEqual(self.get('/items?q=watch', headers).get_json()['total'], 1)
        self.assertEqual(self.get('/items?q=%27%20OR%201%3D1--', headers).get_json()['total'], 0)
        self.assertEqual(self.get('/items?status=sold', headers).get_json()['total'], 1)
        self.assertEqual(self.get('/items/13', headers).status_code, 404)
        self.assertEqual(self.get('/items', self.auth(3)).get_json()['total'], 4)

    def test_bad_pagination_status_rejected(self):
        headers = self.auth()
        for query in ('page=0', 'page=-1', 'page=abc', 'limit=0', 'limit=101', 'status=invalid', 'q=' + 'a' * 101):
            self.assertEqual(self.get('/items?' + query, headers).status_code, 400, query)

    def test_sold_predicate_and_management_code_search_match_details(self):
        with self.db() as conn:
            conn.execute("UPDATE merchandise SET item_status='sold', kaika_product_code='KA-777', model_number='MD-999' WHERE id=11")
        headers = self.auth()
        self.assertEqual(self.get('/items?status=sold', headers).get_json()['total'], 2)
        self.assertEqual(self.get('/items/11', headers).get_json()['item']['status'], 'sold')
        self.assertEqual(self.get('/dashboard', headers).get_json()['inventory_count'], 0)
        self.assertEqual(self.get('/items?q=KA-777', headers).get_json()['total'], 1)
        self.assertEqual(self.get('/items?q=MD-999', headers).get_json()['total'], 1)

    def test_patch_preserves_financial_workflow_and_owner_permissions(self):
        self.assertEqual(self.client.patch('/api/mobile/v1/items/11', json={'notes': 'x'}, headers=self.auth()).status_code, 403)
        self.assertEqual(self.client.patch('/api/mobile/v1/items/13', json={'notes': 'x'}, headers=self.auth()).status_code, 404)
        admin = self.auth(3)
        for body in ({}, {'sale_price': 1}, {'is_shipped': True}, {'user_id': 2}, {'product_name': ''}, {'notes': None}):
            self.assertEqual(self.client.patch('/api/mobile/v1/items/11', json=body, headers=admin).status_code, 400)
        self.assertEqual(self.client.patch('/api/mobile/v1/items/11', json={'notes': 'x'}, headers=self.auth(5)).status_code, 403)
        changed = self.client.patch('/api/mobile/v1/items/11', json={'product_name': ' New name ', 'notes': 'New note'}, headers=self.auth(4))
        self.assertEqual(changed.status_code, 200, changed.get_json())
        with self.db() as conn:
            item = dict(conn.execute('SELECT * FROM merchandise WHERE id=11').fetchone())
        self.assertEqual(item['product_name'], 'New name')
        self.assertEqual(item['sale_price'], 4000)
        self.assertEqual(item['user_id'], 1)
        self.assertEqual(item['updated_by'], 4)

    def test_role_and_subscription_changes_apply_to_existing_token(self):
        headers = self.auth(3)
        with self.db() as conn:
            conn.execute("UPDATE users SET subscription_status='past_due' WHERE id=3")
        self.assertEqual(self.client.patch('/api/mobile/v1/items/11', json={'notes': 'x'}, headers=headers).status_code, 403)
        with self.db() as conn:
            conn.execute("UPDATE users SET role='user' WHERE id=3")
        self.assertEqual(self.get('/items/11', headers).status_code, 404)

    def test_dashboard_uses_server_pricing_helper_and_scopes(self):
        response = self.get('/dashboard', self.auth()).get_json()
        self.assertEqual(response['inventory_count'], 1)
        self.assertEqual(response['inventory_value'], 1500)
        self.assertEqual(response['total_profit'], 2345)
        self.assertEqual(response['current_month_profit'], 2345)
        self.assertEqual(response['scope'], 'mine')
        self.assertTrue(all(call[1] == 'user' for call in self.metrics_calls))
        restricted = self.get('/dashboard', self.auth(7)).get_json()
        self.assertIsNone(restricted['total_profit'])
        self.assertEqual(restricted['scope'], 'all')

    def image_bytes(self):
        output = io.BytesIO()
        Image.new('RGB', (40, 30), 'blue').save(output, 'PNG')
        return output.getvalue()

    def test_photo_validation_upload_and_protected_download(self):
        headers = self.auth(3)
        bad = self.client.post('/api/mobile/v1/items/11/photo', data={'photo': (io.BytesIO(b'<script>bad</script>'), 'photo.png')}, headers=headers)
        self.assertEqual(bad.status_code, 400)
        denied = self.client.post('/api/mobile/v1/items/11/photo', data={'photo': (io.BytesIO(self.image_bytes()), 'photo.png')}, headers=self.auth())
        self.assertEqual(denied.status_code, 403)
        result = self.client.post('/api/mobile/v1/items/11/photo', data={'photo': (io.BytesIO(self.image_bytes()), '../../escape.svg')}, headers=headers)
        self.assertEqual(result.status_code, 200, result.get_json())
        url = result.get_json()['item']['photo_url']
        self.assertIn('/api/mobile/v1/items/11/photos/0', url)
        self.assertNotIn('/static/', url)
        photo = self.client.get(url, headers=self.auth())
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.mimetype, 'image/jpeg')
        photo.close()
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, headers=self.auth(2)).status_code, 404)
        self.assertEqual(len(list((self.static / 'uploads' / '.mobile-private').glob('*.jpg'))), 1)

    def test_native_photo_preserves_previous_and_denies_static_aliases(self):
        headers = self.auth(3)
        uploaded = []
        for _ in range(2):
            response = self.client.post('/api/mobile/v1/items/11/photo',
                data={'photo': (io.BytesIO(self.image_bytes()), 'image.png')}, headers=headers)
            self.assertEqual(response.status_code, 200)
            uploaded.append(response.get_json()['item'])
        self.assertEqual(len(uploaded[-1]['photos']), 2)
        with self.db() as conn:
            row = conn.execute('SELECT photo_path, additional_photos FROM merchandise WHERE id=11').fetchone()
        self.assertEqual(len(json.loads(row['additional_photos'])), 1)
        filename = row['photo_path'].split('/')[-1]
        for url in (
            f'/static/uploads/.mobile-private/{filename}',
            f'/static/uploads/./.mobile-private/{filename}',
            f'/static/uploads/%2Emobile-private/{filename}',
            f'/static/uploads/%2e/.mobile-private/{filename}',
        ):
            self.assertEqual(self.client.get(url).status_code, 404, url)
        web_url = '/static/' + row['photo_path']
        self.assertEqual(self.client.get(web_url).status_code, 401)
        with self.client.session_transaction() as browser:
            browser['_user_id'] = '2'
        self.assertEqual(self.client.get(web_url).status_code, 404)
        with self.client.session_transaction() as browser:
            browser['_user_id'] = '1'
        photo = self.client.get(web_url)
        self.assertEqual(photo.status_code, 200)
        photo.close()

    def test_static_private_guard_registered_when_api_disabled(self):
        disabled_app = Flask('disabled-with-media', static_folder=str(self.static))
        runtime = SimpleNamespace(app=disabled_app, DATABASE_URL=None, get_db=lambda: self.fail('must not open DB'))
        root = self.static / 'uploads' / '.mobile-private'
        root.mkdir(parents=True)
        (root / 'private.jpg').write_bytes(self.image_bytes())
        with patch.dict(os.environ, {'MOBILE_API_ENABLED': '0'}):
            self.assertFalse(register_mobile_api(runtime))
        client = disabled_app.test_client()
        self.assertEqual(client.get('/api/mobile/v1/session').status_code, 404)
        self.assertEqual(client.get('/static/uploads/.mobile-private/private.jpg').status_code, 404)

    def test_public_static_files_work_with_api_enabled_or_disabled(self):
        css = 'body { color: #123456; }'
        (self.static / 'preview.css').write_text(css, encoding='utf-8')
        disabled = Flask('public-static-disabled', static_folder=str(self.static))
        with patch.dict(os.environ, {'MOBILE_API_ENABLED': '0'}):
            register_mobile_api(SimpleNamespace(app=disabled))
        for app in (self.app, disabled):
            response = app.test_client().get('/static/preview.css')
            try:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.mimetype, 'text/css')
                self.assertEqual(response.get_data(as_text=True), css)
            finally:
                response.close()

    def test_photo_path_traversal_omitted(self):
        with self.db() as conn:
            conn.execute("UPDATE merchandise SET photo_path='uploads/../../fixture.db' WHERE id=11")
        result = self.get('/items/11', self.auth()).get_json()['item']
        self.assertEqual(result['photos'], [])
        self.assertEqual(self.get('/items/11/photos/0', self.auth()).status_code, 404)

    def test_photo_size_limit_is_enforced_before_multipart_parse(self):
        response = self.client.post('/api/mobile/v1/items/11/photo', headers=self.auth(3),
            environ_overrides={'CONTENT_LENGTH': str(11 * 1024 * 1024)})
        self.assertEqual(response.status_code, 413)

    def test_overdue_change_revokes_existing_user_session(self):
        headers = self.auth()
        with self.db() as conn:
            conn.execute("UPDATE users SET subscription_status='past_due', overdue_since=? WHERE id=1",
                ((self.now - timedelta(days=100)).isoformat() + '+09:00',))
        self.assertEqual(self.get('/session', headers).status_code, 401)

    def test_announcement_targeting_and_read(self):
        headers = self.auth()
        self.assertEqual(self.get('/announcements', headers).get_json()['unread_count'], 1)
        self.assertEqual(self.client.post('/api/mobile/v1/announcements/32/read', headers=headers).status_code, 404)
        result = self.client.post('/api/mobile/v1/announcements/31/read', headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['unread_count'], 0)
        self.assertEqual(self.get('/announcements', self.auth(3)).get_json()['announcements'], [])

    def test_https_required_in_non_test_runtime(self):
        self.app.config['TESTING'] = False
        self.assertEqual(self.get('/session').status_code, 403)
        self.assertEqual(self.client.get('/api/mobile/v1/session', base_url='https://example.test').status_code, 401)


if __name__ == '__main__':
    unittest.main()
