"""HTTP boundaries against real registry/reconciliation and disposable SQLite."""
from html.parser import HTMLParser
import io
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager, UserMixin
from jinja2 import ChoiceLoader, DictLoader

from inventory_registry_web import register_inventory_registry
from inventory_reconciliation import migrate_import_schema
from item_registry import Actor, Registry
from item_registry_schema import migrate_registry
from stage2_self_management import PHOTO_RECORDS_DDL

ROOT = Path(__file__).resolve().parents[1]
TIME = '2026-10-20T01:00:00+00:00'


class Forms(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.forms, self.current = {}, None
        self.feed(html)
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'form':
            self.current = values.get('action', '')
            self.forms[self.current] = {}
        elif tag == 'input' and self.current is not None and values.get('type') == 'hidden':
            self.forms[self.current][values['name']] = values.get('value', '')
    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


class User(UserMixin):
    def __init__(self, identifier):
        self.id = identifier
        self.role = 'owner' if identifier == 9 else 'admin' if identifier in (7, 8) else 'user'
    def is_admin(self):
        return self.role in ('admin', 'owner')
    def has_permission(self, permission):
        return self.id in (8, 9)


class InventoryRegistryWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kaika-stage2-http-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.sqlite'
        db = self.connect()
        db.executescript('''
            CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT,username TEXT,display_name TEXT);
            INSERT INTO users VALUES(1,'user',NULL,'fixture1','架空顧客1');
            INSERT INTO users VALUES(2,'user',NULL,'fixture2','架空顧客2');
            INSERT INTO users VALUES(9,'owner',NULL,'fixtureowner','架空管理者');
            INSERT INTO users VALUES(8,'admin','["merchandise"]','fixturestaff','架空担当者');
            INSERT INTO users VALUES(7,'admin','["users"]','fixturerestricted','権限なし');
            CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),product_name TEXT,
                purchase_price INTEGER,sale_date TEXT,is_shipped INTEGER,custody_location TEXT);
            INSERT INTO merchandise VALUES(11,1,'既存商品A',30000,NULL,0,'unknown');
            INSERT INTO merchandise VALUES(21,2,'他顧客商品B',40000,NULL,0,'unknown');
        ''')
        migrate_registry(db, allowed_test_path=self.path)
        migrate_import_schema(db, allow_temporary=True)
        db.execute(PHOTO_RECORDS_DDL)
        registry = Registry(db)
        self.item = registry.create_item(Actor(1), '架空の自己商品', occurred_at=TIME,
            evidence_reference='fixture-only', source_key='fixture-self')['physical_item_id']
        self.other = registry.create_item(Actor(2), '他顧客の秘密商品', occurred_at=TIME,
            evidence_reference='fixture-only', source_key='fixture-other')['physical_item_id']
        db.commit()
        db.close()
        self.app = Flask('stage2-http-fixture', template_folder=str(ROOT / 'templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='stage2-fixture-random-enough-not-production', STAGE2_INVENTORY_ENABLED=True)
        self.app.jinja_loader = ChoiceLoader([DictLoader({'base.html': '{% block title %}{% endblock %}{% block content %}{% endblock %}'}), self.app.jinja_loader])
        self.app.add_url_rule('/fixture/intakes', endpoint='inventory_intakes', view_func=lambda: 'fixture')
        login = LoginManager(self.app)
        login.user_loader(lambda identifier: User(int(identifier)))
        self.runtime = SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=self.connect)
        self.service = register_inventory_registry(self.runtime)
        self.client = self.app.test_client()

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn

    def count(self, table):
        conn = self.connect()
        try:
            return conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
        finally:
            conn.close()

    def login(self, identifier=9, clear=True):
        with self.client.session_transaction() as session:
            if clear:
                session.clear()
            session['_user_id'] = str(identifier)
            session['_fresh'] = True

    def form(self, page, action):
        response = self.client.get(page)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        forms = Forms(response.get_data(as_text=True)).forms
        return forms[action] if action in forms else forms['']

    def item_form(self, suffix):
        return self.form('/inventory/items/' + self.item, '/inventory/items/' + self.item + '/' + suffix)

    def test_feature_disabled_does_not_open_database_or_write(self):
        self.login()
        self.app.config['STAGE2_INVENTORY_ENABLED'] = False
        with patch.object(self.runtime, 'get_db', side_effect=AssertionError('disabled feature DB access')):
            for path in ('/inventory/items', '/inventory/reconcile', '/inventory/imports'):
                self.assertEqual(self.client.get(path).status_code, 404)
            self.assertEqual(self.client.post('/inventory/items/' + self.item + '/work', data={}).status_code, 404)
            register_inventory_registry(self.runtime)

    def test_unauthenticated_and_customer_staff_boundaries(self):
        self.assertEqual(self.client.get('/inventory/items').status_code, 401)
        self.login(1)
        self.assertEqual(self.client.get('/inventory/reconcile').status_code, 403)
        self.assertEqual(self.client.get('/inventory/imports').status_code, 403)
        self.assertEqual(self.client.post('/inventory/items/' + self.item + '/work', data={}).status_code, 403)
        self.login(7)
        self.assertEqual(self.client.get('/inventory/items').status_code, 403)

    def test_real_templates_show_only_current_customer_items_and_private_headers(self):
        self.login(1)
        page = self.client.get('/inventory/items')
        self.assertEqual(page.status_code, 200)
        self.assertIn('架空の自己商品', page.get_data(as_text=True))
        self.assertNotIn('他顧客の秘密商品', page.get_data(as_text=True))
        self.assertIn('no-store', page.headers['Cache-Control'])
        self.assertEqual(page.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(self.client.get('/inventory/items/' + self.other).status_code, 404)
        detail = self.client.get('/inventory/items/' + self.item)
        self.assertNotIn('name="operation_token"', detail.get_data(as_text=True))

    def test_work_requires_csrf_and_actor_bound_operation(self):
        self.login()
        form = self.item_form('work')
        payload = dict(form, kind='photography', performed_at='2026-10-22T10:00', note='実際の写真撮影', duration_minutes='2.5')
        without_csrf = dict(payload, registry_csrf='wrong')
        path = '/inventory/items/' + self.item + '/work'
        self.assertEqual(self.client.post(path, data=without_csrf).status_code, 400)
        self.assertEqual(self.count('business_work_records'), 0)
        self.login(8, clear=False)
        self.assertEqual(self.client.post(path, data=payload).status_code, 400)
        self.assertEqual(self.count('business_work_records'), 0)

    def test_work_replay_is_single_record_and_preserves_actual_seconds(self):
        self.login()
        payload = dict(self.item_form('work'), kind='photography', performed_at='2026-10-22T10:00', note='実測', duration_minutes='2.5')
        path = '/inventory/items/' + self.item + '/work'
        self.assertEqual(self.client.post(path, data=payload).status_code, 302)
        self.assertEqual(self.client.post(path, data=payload).status_code, 302)
        self.assertEqual(self.count('business_work_records'), 1)
        db = self.connect()
        try:
            row = db.execute('SELECT * FROM business_work_records').fetchone()
            self.assertEqual(row['duration_seconds'], 150)
            self.assertEqual(row['measurement_source'], 'observed')
        finally:
            db.close()
        self.assertEqual(self.client.post(path, data=dict(payload, duration_minutes='3')).status_code, 409)
        self.assertEqual(self.count('business_work_records'), 1)

    def test_work_unknown_and_invalid_time_are_not_estimates(self):
        self.login()
        path = '/inventory/items/' + self.item + '/work'
        payload = dict(self.item_form('work'), kind='inspection', performed_at='2026-10-22T10:00', note='検品', duration_minutes='')
        self.assertEqual(self.client.post(path, data=dict(payload, duration_minutes='NaN')).status_code, 400)
        self.assertEqual(self.client.post(path, data=dict(payload, performed_at='not-time')).status_code, 400)
        self.assertEqual(self.client.post(path, data=payload).status_code, 302)
        db = self.connect()
        try:
            self.assertIsNone(db.execute('SELECT duration_seconds FROM business_work_records').fetchone()[0])
        finally:
            db.close()

    def test_invalid_work_keeps_input_in_same_form_and_retry_is_single_record(self):
        self.login()
        path = '/inventory/items/' + self.item + '/work'
        payload = dict(self.item_form('work'), kind='inspection', performed_at='',
            note='検品メモ <script>fictional()</script>', duration_minutes='2.5')
        response = self.client.post(path, data=payload)
        text = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 400)
        self.assertIn('role="alert"', text)
        self.assertIn('入力は下のフォームに残しています', text)
        self.assertIn('検品メモ &lt;script&gt;fictional()&lt;/script&gt;', text)
        self.assertNotIn('<script>fictional()</script>', text)
        self.assertIn('value="2.5"', text)
        self.assertEqual(self.count('business_work_records'), 0)
        retry_fields = Forms(text).forms[path]
        self.assertNotEqual(retry_fields['operation_token'], payload['operation_token'])
        retry = {**payload, **retry_fields, 'performed_at':'2026-10-22T10:00'}
        self.assertEqual(self.client.post(path, data=retry).status_code, 302)
        self.assertEqual(self.client.post(path, data=retry).status_code, 302)
        self.assertEqual(self.count('business_work_records'), 1)
        conflict = self.client.post(path, data={**retry,'duration_minutes':'3'})
        self.assertEqual(conflict.status_code, 409)
        self.assertIn('重複登録しないでください', conflict.get_data(as_text=True))
        self.assertNotIn('name="performed_at"', conflict.get_data(as_text=True))

    def test_invalid_event_keeps_note_and_selection_without_writing(self):
        self.login()
        path = '/inventory/items/' + self.item + '/events'
        payload = dict(self.item_form('events'), kind='registration_completed', occurred_at='2026-10-22T10:00', note='入力途中の確認メモ')
        response = self.client.post(path, data=payload)
        self.assertEqual(response.status_code, 400)
        text = response.get_data(as_text=True)
        self.assertIn('撮影・登録などの確認順を確認してください', text)
        self.assertIn('入力途中の確認メモ', text)
        self.assertIn('value="registration_completed" selected', text)
        self.assertEqual(self.count('business_item_observations'), 0)
        self.assertEqual(self.client.post('/inventory/items/missing/work', data={}).status_code, 400)

    def test_detail_dates_and_actual_event_names_are_japanese(self):
        self.login()
        db = self.connect()
        Registry(db).record_event(Actor(9,True), self.item, 'received',
            '2026-10-21T16:05:00+00:00', 'fixture-jst-receipt', 'fixture')
        db.commit(); db.close()
        text = self.client.get('/inventory/items/' + self.item).get_data(as_text=True)
        self.assertIn('2026/10/22 01:05', text)
        self.assertIn('— 開花で受領', text)
        self.assertNotIn('— received', text)
        self.assertNotIn('2026-10-21T16:05', text)

    def test_operation_token_is_bound_to_target(self):
        self.login()
        payload = dict(self.item_form('work'), kind='inspection', performed_at='2026-10-22T10:00', note='x')
        self.assertEqual(self.client.post('/inventory/items/' + self.other + '/work', data=payload).status_code, 403)
        self.assertEqual(self.count('business_work_records'), 0)

    def test_hold_records_once_and_blocks_forward_state(self):
        self.login()
        path = '/inventory/items/' + self.item + '/events'
        payload = dict(self.item_form('events'), kind='hold', occurred_at='2026-10-22T10:00', note='現物に差異あり')
        self.assertEqual(self.client.post(path, data=payload).status_code, 302)
        self.assertEqual(self.client.post(path, data=payload).status_code, 302)
        self.assertEqual(self.count('business_item_observations'), 1)
        next_payload = dict(self.item_form('events'), kind='photographed', occurred_at='2026-10-22T11:00', note='撮影を進める')
        self.assertEqual(self.client.post(path, data=next_payload).status_code, 409)
        self.assertEqual(self.count('business_item_observations'), 1)

    def test_link_requires_explicit_review_and_does_not_generate_work(self):
        self.login()
        path = '/inventory/reconcile/link'
        payload = dict(self.form('/inventory/reconcile', path), merchandise_id='11', note='旧台帳と現物一致')
        self.assertEqual(self.client.post(path, data=payload).status_code, 400)
        response = self.client.post(path, data=dict(payload, reviewed='yes'))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.post(path, data=dict(payload, reviewed='yes')).status_code, 302)
        self.assertEqual(self.count('business_item_links'), 1)
        self.assertEqual(self.count('business_work_records'), 0)
        self.assertEqual(self.count('business_item_events'), 0)
        db = self.connect()
        try:
            self.assertEqual(db.execute('SELECT link_status FROM business_item_links').fetchone()[0], 'verified')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM business_item_reviews WHERE kind='identity'").fetchone()[0], 1)
        finally:
            db.close()

    def preview(self, content=b'legacy_id,title\n11,existing\n'):
        self.login()
        payload = dict(self.form('/inventory/imports', '/inventory/imports'), customer_id='1', source_namespace='fixture-csv')
        payload['file'] = (io.BytesIO(content), 'fixture.csv')
        response = self.client.post('/inventory/imports', data=payload, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return response.headers['Location']

    def test_csv_preview_then_confirm_and_repeat_no_duplicate(self):
        location = self.preview('legacy_id,title\n11,既存商品A\n'.encode())
        page = self.client.get(location)
        self.assertEqual(page.status_code, 200)
        self.assertIn('既存商品A', page.get_data(as_text=True))
        self.assertEqual(self.count('business_item_links'), 0)
        action = location + '/confirm'
        form = self.form(location, action)
        self.assertEqual(self.client.post(action, data=form).status_code, 400)
        payload = dict(form, reviewed='yes')
        self.assertEqual(self.client.post(action, data=payload).status_code, 302)
        self.assertEqual(self.client.post(action, data=payload).status_code, 302)
        self.assertEqual(self.count('business_item_links'), 1)
        self.assertEqual(self.count('business_work_records'), 0)
        self.assertEqual(self.count('merchandise'), 2)

    def test_csv_other_customer_id_holds_entire_batch(self):
        location = self.preview('legacy_id,title\n21,他顧客商品B\n'.encode())
        action = location + '/confirm'
        payload = dict(self.form(location, action), reviewed='yes')
        self.assertEqual(self.client.post(action, data=payload).status_code, 409)
        self.assertEqual(self.count('business_item_links'), 0)

    def test_preview_changed_hash_rejected_and_customer_cannot_read_batch(self):
        location = self.preview('legacy_id,title\n11,既存商品A\n'.encode())
        action = location + '/confirm'
        payload = dict(self.form(location, action), reviewed='yes', preview_hash='0' * 64)
        self.assertEqual(self.client.post(action, data=payload).status_code, 409)
        self.assertEqual(self.count('business_item_links'), 0)
        self.login(1)
        self.assertEqual(self.client.get(location).status_code, 403)

    def test_real_registry_rechecks_database_staff_permission(self):
        self.login(8)
        payload = dict(self.item_form('work'), kind='inspection', performed_at='2026-10-22T10:00', note='x')
        db = self.connect()
        db.execute("UPDATE users SET admin_permissions='[\"users\"]' WHERE id=8")
        db.commit()
        db.close()
        self.assertEqual(self.client.post('/inventory/items/' + self.item + '/work', data=payload).status_code, 403)
        self.assertEqual(self.count('business_work_records'), 0)

    def test_new_csv_stock_joins_legacy_without_work_dates_or_charge(self):
        location = self.preview('external_key,title,purchase_price\nold-stock-1,過去の追加在庫,30000\n'.encode())
        action = location + '/confirm'
        payload = dict(self.form(location, action), reviewed='yes')
        self.assertEqual(self.client.post(action, data=payload).status_code, 302)
        self.assertEqual(self.count('merchandise'), 3)
        self.assertEqual(self.count('business_item_links'), 1)
        self.assertEqual(self.count('business_work_records'), 0)
        self.assertEqual(self.count('business_item_events'), 0)
        db = self.connect()
        try:
            item = db.execute("SELECT * FROM business_item_profiles WHERE title='過去の追加在庫'").fetchone()
            self.assertIsNone(item['received_at'])
            self.assertIsNone(item['registration_completed_at'])
            self.assertIsNone(item['deadline_at'])
            self.assertEqual(item['physical_state'], 'unknown')
        finally:
            db.close()

    def test_csv_confirmation_rejects_changed_legacy_source(self):
        location = self.preview('legacy_id,title\n11,既存商品A\n'.encode())
        action = location + '/confirm'
        payload = dict(self.form(location, action), reviewed='yes')
        db = self.connect()
        db.execute('UPDATE merchandise SET purchase_price=35000 WHERE id=11')
        db.commit()
        db.close()
        self.assertEqual(self.client.post(action, data=payload).status_code, 409)
        self.assertEqual(self.count('business_item_links'), 0)

    def test_deadline_requires_customer_contract_evidence_and_no_blanket_rule(self):
        self.login()
        csrf = self.item_form('work')['registry_csrf']
        token = self.service.signer.dumps(dict(actor='9', purpose='deadline', target=self.item, nonce='fixture-deadline'))
        path = '/inventory/items/' + self.item + '/deadline'
        payload = dict(registry_csrf=csrf, operation_token=token, deadline_at='2026-12-03T10:00', contract_reference='架空顧客1の署名版と期限')
        self.assertEqual(self.client.post(path, data=payload).status_code, 400)
        payload['contract_checked'] = 'yes'
        self.assertEqual(self.client.post(path, data=payload).status_code, 302)
        self.assertEqual(self.client.post(path, data=payload).status_code, 302)
        db = self.connect()
        try:
            profiles = {row['physical_item_id']: dict(row) for row in db.execute('SELECT * FROM business_item_profiles')}
            self.assertEqual(profiles[self.item]['deadline_status'], 'confirmed')
            self.assertEqual(profiles[self.other]['deadline_status'], 'unknown')
            self.assertIsNone(profiles[self.other]['deadline_at'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM business_item_reviews WHERE kind='deadline'").fetchone()[0], 1)
        finally:
            db.close()

    def test_missing_core_migration_is_explicit_hold(self):
        self.login()
        db = self.connect()
        db.execute('DELETE FROM business_registry_schema')
        db.commit()
        db.close()
        self.assertEqual(self.client.get('/inventory/items').status_code, 409)

    def test_missing_photo_migration_is_explicit_hold(self):
        self.login()
        db = self.connect()
        db.execute('DROP TABLE stage2_photo_records')
        db.commit()
        db.close()
        self.assertEqual(self.client.get('/inventory/items/' + self.item).status_code, 409)

    def test_photo_metadata_does_not_follow_previous_owner(self):
        db = self.connect()
        registry = Registry(db)
        registry.attach_legacy(Actor(9, True), 11, physical_item_id=self.item,
            evidence_reference='fixture-match', occurred_at=TIME, source_key='photo-link')
        db.execute('''INSERT INTO stage2_photo_records(merchandise_id,path_hash,photo_path,owner_user_id,
            byte_size,measurement_status,first_recorded_at,last_seen_at)
            VALUES(11,'fixture-old-path-hash','uploads/private-old-owner.jpg',1,12345,'measured',?,?)''', (TIME, TIME))
        db.commit()
        self.login(1)
        before = self.client.get('/inventory/items/' + self.item)
        self.assertEqual(before.status_code, 200)
        self.assertIn('12345', before.get_data(as_text=True))
        self.assertNotIn('private-old-owner.jpg', before.get_data(as_text=True))
        registry.change_assignment(Actor(9, True), self.item, 'owner', 'customer', 2,
            occurred_at='2026-10-22T01:00:00+00:00', evidence_reference='fixture-transfer', source_key='photo-transfer')
        db.commit()
        db.close()
        self.login(2)
        after = self.client.get('/inventory/items/' + self.item)
        self.assertEqual(after.status_code, 200)
        self.assertNotIn('12345', after.get_data(as_text=True))
        self.assertNotIn('private-old-owner.jpg', after.get_data(as_text=True))
        self.assertIn('枚数・容量は未記録', after.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
