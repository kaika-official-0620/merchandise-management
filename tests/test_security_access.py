"""Security boundary tests using only disposable files/SQLite, no app import."""
import html
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask, abort, render_template_string
from flask_login import LoginManager, UserMixin

from mobile_api import _register_mobile_media
from security_access import register_security_access, HELD_DELETIONS, MUTATIONS


class FixtureUser(UserMixin):
    def __init__(self, identifier):
        self.id = int(identifier)
        self.role = 'user' if self.id in (1, 2) else ('owner' if self.id == 3 else 'admin')

    def is_admin(self):
        return self.role in ('owner', 'admin')

    def has_permission(self, permission):
        return self.id == 3 or (self.id == 4 and permission == 'invoices')


class SecurityAccessTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kaika-private-access-')
        self.root = Path(self.temp.name)
        self.static = self.root / 'static'
        self.uploads = self.static / 'uploads'
        self.uploads.mkdir(parents=True)
        self.database = self.root / 'fixture.sqlite'
        self.calls = []
        self.opened = 0
        self.connections = []
        with self.db() as connection:
            connection.executescript('''
                CREATE TABLE users(id INTEGER PRIMARY KEY, role TEXT);
                INSERT INTO users VALUES (1,'user'),(2,'user'),(3,'owner'),(4,'admin');
                CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,photo_path TEXT,
                    additional_photos TEXT,id_document_path TEXT,consent_form_path TEXT,
                    auction_id INTEGER,show_in_proxy_service INTEGER);
                CREATE TABLE sale_requests(id INTEGER PRIMARY KEY,user_id INTEGER,merchandise_id INTEGER,
                    qr_image_path TEXT,qr_image_path2 TEXT,additional_image_paths TEXT);
                CREATE TABLE inquiries(id INTEGER PRIMARY KEY,user_id INTEGER,image_path TEXT);
                CREATE TABLE vendor_documents(id INTEGER PRIMARY KEY,user_id INTEGER,stored_path TEXT);
                CREATE TABLE admin_kaitori_shoudaku(id INTEGER PRIMARY KEY,vendor_response_file_path TEXT);
                CREATE TABLE sales_agency_requests(id INTEGER PRIMARY KEY,user_id INTEGER);
                CREATE TABLE sales_agency_request_items(id INTEGER PRIMARY KEY,request_id INTEGER,snapshot_photo_path TEXT);
                CREATE TABLE proxy_service_settings(id INTEGER PRIMARY KEY,is_public INTEGER);
                CREATE TABLE proxy_service_auction_users(auction_id INTEGER,user_id INTEGER,is_enabled INTEGER);
                INSERT INTO merchandise VALUES(11,1,'uploads/main.jpg','["uploads/extra.jpg"]',
                    'uploads/id.pdf','uploads/consent.pdf',NULL,0);
                INSERT INTO merchandise VALUES(12,2,'uploads/other.jpg',NULL,NULL,NULL,NULL,0);
                INSERT INTO sale_requests VALUES(21,1,11,'uploads/qr/a.png','uploads/qr/b.png','["uploads/qr/c.png"]');
                INSERT INTO inquiries VALUES(31,1,'["uploads/inquiry.jpg"]');
                INSERT INTO vendor_documents VALUES(41,1,'uploads/vendor.pdf');
                INSERT INTO admin_kaitori_shoudaku VALUES(51,'uploads/vendor-response.pdf');
                INSERT INTO sales_agency_requests VALUES(61,1);
                INSERT INTO sales_agency_request_items VALUES(71,61,'uploads/snapshot.jpg');
                INSERT INTO proxy_service_settings VALUES(81,1);
                INSERT INTO proxy_service_auction_users VALUES(81,1,1);
                INSERT INTO merchandise VALUES(13,3,'uploads/catalog.jpg',NULL,NULL,NULL,81,1);
            ''')
        for name in ('main.jpg', 'extra.jpg', 'other.jpg', 'id.pdf', 'consent.pdf',
                     'qr/a.png', 'qr/b.png', 'qr/c.png', 'inquiry.jpg', 'vendor.pdf',
                     'vendor-response.pdf', 'snapshot.jpg', 'orphan.jpg', 'catalog.jpg'):
            self.file(name)
        (self.static / 'test.css').write_text('body{}', encoding='utf-8')
        self.app = Flask(__name__, static_folder=str(self.static), static_url_path='/static')
        self.app.config.update(TESTING=True, SECRET_KEY='fixture-not-for-production')
        manager = LoginManager(self.app)
        manager.user_loader(lambda identifier: FixtureUser(identifier))
        self.runtime = SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=self.db)
        _register_mobile_media(self.runtime)
        for endpoint in MUTATIONS:
            def original(endpoint=endpoint, **_):
                self.calls.append(endpoint)
                if endpoint == 'user_invoice_delete':
                    abort(403)  # sentinel: original per-record permission remains effective
                return 'mutated'
            methods = ['POST'] if endpoint in {'admin_delete_item', 'admin_vendor_document_delete'} else ['GET']
            self.app.add_url_rule('/fixture/' + endpoint + '/<int:id>', endpoint, original, methods=methods)
        self.app.add_url_rule('/admin/users/<int:id>/set_role/<role>', 'admin_set_role',
                             self.app.view_functions['admin_set_role'], methods=['GET'])
        @self.app.get('/form')
        def form():
            return render_template_string('<meta name="kaika-security-csrf" content="{{ security_form_token }}">')
        opened = self.opened
        self.service = register_security_access(self.runtime)
        self.assertEqual(self.opened, opened, 'registration must not open database')
        self.client = self.app.test_client()

    def db(self):
        self.opened += 1
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        self.connections.append(connection)
        return connection

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        self.temp.cleanup()

    def file(self, name, contents=b'fictional-upload-only'):
        path = self.uploads / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def login(self, user=1):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(user)
            session['_fresh'] = True

    def get(self, name, expected):
        response = self.client.get('/static/uploads/' + name)
        try:
            self.assertEqual(response.status_code, expected, name)
            if expected == 200:
                self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
                self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
            return response.get_data()
        finally:
            response.close()

    def token(self):
        response = self.client.get('/form')
        return html.unescape(re.search(r'content="([^"]+)"', response.get_data(as_text=True))[1])

    def test_registration_idempotent_and_public_assets_do_not_query_database(self):
        before = self.opened
        self.assertIs(register_security_access(self.runtime), self.service)
        response = self.client.get('/static/test.css')
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.opened, before)

    def test_anonymous_uploads_denied_without_database_access(self):
        before = self.opened
        self.get('main.jpg', 401)
        self.assertEqual(self.opened, before)

    def test_owner_photos_additional_and_other_customer(self):
        self.login(1)
        self.get('main.jpg', 200)
        self.get('extra.jpg', 200)
        self.get('other.jpg', 404)
        self.login(2)
        self.get('main.jpg', 404)
        self.get('other.jpg', 200)

    def test_staff_can_read_existing_photos_but_not_orphans(self):
        self.login(3)
        self.get('main.jpg', 200)
        self.get('other.jpg', 200)
        self.get('orphan.jpg', 404)

    def test_identity_and_consent_only_authorized_staff(self):
        for user, expected in ((1, 404), (2, 404), (4, 404), (3, 200)):
            self.login(user)
            self.get('id.pdf', expected)
            self.get('consent.pdf', expected)

    def test_sensitive_reference_wins_over_forged_public_photo_reference(self):
        with self.db() as connection:
            connection.execute("UPDATE merchandise SET additional_photos='[\"uploads/id.pdf\"]' WHERE id=11")
        self.login(1)
        self.get('id.pdf', 404)

    def test_qr_images_only_request_owner_and_staff(self):
        for user, expected in ((1, 200), (2, 404), (3, 200)):
            self.login(user)
            for name in ('qr/a.png', 'qr/b.png', 'qr/c.png'):
                self.get(name, expected)

    def test_inquiry_json_and_legacy_scalar_reference(self):
        self.login(1)
        self.get('inquiry.jpg', 200)
        with self.db() as connection:
            connection.execute("UPDATE inquiries SET image_path='uploads/inquiry.jpg'")
        self.get('inquiry.jpg', 200)
        self.login(2)
        self.get('inquiry.jpg', 404)

    def test_vendor_originals_are_not_customer_documents(self):
        for user, expected in ((1, 404), (2, 404), (3, 200), (4, 200)):
            self.login(user)
            self.get('vendor.pdf', expected)
            self.get('vendor-response.pdf', expected)

    def test_agency_historical_snapshot_has_request_owner_scope(self):
        self.login(1)
        self.get('snapshot.jpg', 200)
        self.login(2)
        self.get('snapshot.jpg', 404)
        with self.db() as connection:
            connection.execute('DELETE FROM sales_agency_requests')
        self.login(3)
        self.get('snapshot.jpg', 404)

    def test_catalog_photos_require_published_company_stock_and_explicit_audience(self):
        self.login(1)
        self.get('catalog.jpg', 200)
        self.login(2)
        self.get('catalog.jpg', 404)
        self.login(1)
        with self.db() as connection:
            connection.execute('UPDATE proxy_service_settings SET is_public=0')
        self.get('catalog.jpg', 404)
        with self.db() as connection:
            connection.execute('UPDATE proxy_service_settings SET is_public=1')
            connection.execute('UPDATE merchandise SET user_id=2 WHERE id=13')
        self.get('catalog.jpg', 404)

    def test_json_substring_and_sql_wildcards_do_not_grant_access(self):
        self.login(1)
        self.file('or.jpg')
        self.file('%.jpg')
        with self.db() as connection:
            connection.execute("UPDATE merchandise SET additional_photos=? WHERE id=11", (json.dumps(['uploads/or.jpg-other', 'uploads/anything.jpg']),))
        self.get('or.jpg', 404)
        self.get('%25.jpg', 404)

    def test_missing_optional_tables_do_not_open_unknown_files(self):
        with self.db() as connection:
            for table in ('vendor_documents', 'admin_kaitori_shoudaku', 'sale_requests', 'inquiries', 'sales_agency_request_items'):
                connection.execute('DROP TABLE ' + table)
        self.login(1)
        self.get('main.jpg', 200)
        self.get('orphan.jpg', 404)

    def test_authorization_database_failure_is_closed_without_exception_details(self):
        self.login(1)
        with patch.object(self.runtime, 'get_db', side_effect=RuntimeError('sensitive-connection-string')):
            with self.assertLogs(self.app.logger, level='ERROR') as logs:
                body = self.get('main.jpg', 503)
        self.assertNotIn(b'sensitive-connection-string', body)
        self.assertNotIn('sensitive-connection-string', ''.join(logs.output))

    def test_traversal_aliases_and_hidden_files_rejected(self):
        self.login(3)
        self.file('.hidden.jpg')
        for url in ('/static/uploads/../test.css', '/static/uploads/%2e%2e/test.css',
                    '/static/uploads/./main.jpg', '/static/x/../uploads/main.jpg',
                    '/static/uploads%5cmain.jpg', '/static/uploads/.hidden.jpg',
                    '/static/uploads//main.jpg', '/static/uploads/.mobile-private/a.jpg'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404, url)
            response.close()

    def test_symlink_is_not_served(self):
        target = self.root / 'outside.jpg'
        target.write_bytes(b'outside')
        try:
            (self.uploads / 'main.jpg').unlink()
            (self.uploads / 'main.jpg').symlink_to(target)
        except OSError:
            self.skipTest('Host does not allow creating temporary symlinks')
        self.login(3)
        self.get('main.jpg', 404)

    def test_mobile_private_virtual_route_preserves_ownership(self):
        name = 'a' * 40 + '.jpg'
        self.file('.mobile-private/' + name)
        with self.db() as connection:
            connection.execute('UPDATE merchandise SET photo_path=? WHERE id=11', ('uploads/mobile/' + name,))
        self.login(1)
        response = self.client.get('/static/uploads/mobile/' + name)
        self.assertEqual(response.status_code, 200)
        response.close()
        self.get('.mobile-private/' + name, 404)
        self.login(2)
        self.get('mobile/' + name, 404)

    def test_account_and_item_deletes_never_call_original_or_delete_file(self):
        self.login(3)
        original = (self.uploads / 'qr/a.png').read_bytes()
        for endpoint in HELD_DELETIONS:
            path = '/fixture/' + endpoint + '/1'
            self.assertEqual(self.client.get(path).status_code, 200)
            self.assertEqual(self.client.post(path).status_code, 400)
            response = self.client.post(path, data={'security_csrf_token': self.token()})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()['error']['code'], 'deletion_policy_pending')
        self.assertEqual(self.calls, [])
        self.assertEqual((self.uploads / 'qr/a.png').read_bytes(), original)
        with self.db() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM users').fetchone()[0], 4)

    def test_all_guarded_get_and_head_are_non_mutating(self):
        self.login(3)
        for endpoint in MUTATIONS:
            path = '/fixture/' + endpoint + '/1'
            self.assertEqual(self.client.get(path).status_code, 200, endpoint)
            self.assertEqual(self.client.head(path).status_code, 200, endpoint)
        self.assertEqual(self.calls, [])

    def test_mutation_requires_post_csrf_and_preserves_original_record_authorization(self):
        self.login(1)
        path = '/fixture/delete_customer/1'
        self.assertEqual(self.client.post(path).status_code, 400)
        self.assertEqual(self.client.post(path, data={'security_csrf_token': 'wrong'}).status_code, 400)
        token = self.token()
        self.assertEqual(self.client.post(path, data={'security_csrf_token': token}).status_code, 200)
        self.assertEqual(self.calls, ['delete_customer'])
        self.assertEqual(self.client.post('/fixture/user_invoice_delete/1', data={'security_csrf_token': token}).status_code, 403)

    def test_mutations_require_authentication_admin_permission_and_user_bound_token(self):
        self.assertEqual(self.client.get('/fixture/delete_customer/1').status_code, 401)
        self.login(1)
        token = self.token()
        self.assertEqual(self.client.get('/fixture/delete_user/1').status_code, 403)
        self.login(2)
        self.assertEqual(self.client.post('/fixture/delete_customer/1', data={'security_csrf_token': token}).status_code, 400)
        self.login(4)
        self.assertEqual(self.client.get('/fixture/delete_user/1').status_code, 403)
        self.assertEqual(self.calls, [])

    def test_csrf_form_patterns_exclude_self_registration_and_external_routes(self):
        self.login(1)
        with self.client:
            self.client.get('/form')
            values = {}
            self.app.update_template_context(values)
        patterns = values['security_guarded_form_patterns']
        self.assertTrue(any(re.fullmatch(pattern, '/fixture/delete_customer/1') for pattern in patterns))
        for unrelated in ('/inventory/self/new', '/inventory/intakes/new', '/business-review', 'https://example.test/fixture/delete_customer/1'):
            self.assertFalse(any(re.fullmatch(pattern, unrelated) for pattern in patterns))

    def test_role_change_get_cannot_mutate_and_post_requires_users_permission(self):
        path = '/admin/users/2/set_role/owner'
        self.login(1)
        self.assertEqual(self.client.get(path).status_code, 403)
        self.login(4)  # invoices-only administrator
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post(path, data={'security_csrf_token': self.token()}).status_code, 403)
        self.login(3)
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.post(path).status_code, 400)
        self.assertEqual(self.client.post(path, data={'security_csrf_token': self.token()}).status_code, 200)
        self.assertEqual(self.calls, ['admin_set_role'])

    def test_invoice_send_and_approval_get_aliases_never_reach_business_mutations(self):
        self.login(3)
        for endpoint in ('user_invoice_send', 'admin_invoice_approve', 'admin_invoice_reject', 'admin_seisan_send', 'admin_shikiriosho_send'):
            path = '/fixture/' + endpoint + '/1'
            self.assertEqual(self.client.get(path).status_code, 200)
            self.assertEqual(self.client.post(path).status_code, 400)
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
