"""Real Flask routes and SQLite rehearsal records. No provider or network is used."""
from contextlib import closing
import csv
from datetime import datetime
from io import StringIO
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID
from zoneinfo import ZoneInfo

from flask import Flask
from flask_login import LoginManager, UserMixin, login_user
from jinja2 import ChoiceLoader, DictLoader

from inventory_registry_web import register_inventory_registry
from item_registry import Registry
from stage3_billing import BillingService
from stage3_common import FinanceStore
from stage3_funds_schema import migration_statements as funds_schema
from stage3_profit import ProfitService
from stage3_profit_schema import migration_statements as profit_schema
from stage3_web import register_stage3_finance
from stage4_listings_schema import migration_statements as listing_schema
from stage6_common import migration_statements as notifications_common_schema
from stage6_notifications_schema import migration_statements as notification_schema
from stage6_web import register_stage6_web
from stage9_payment_schema import statements as sandbox_payments_schema
from staging_invoice_payment import DEMO_INVOICE_ID, migration_statements as demo_schema
from test_stage5_payments import CUSTOMER, STAFF, fixture as finance_fixture


ROOT = Path(__file__).resolve().parents[1]
INVOICE_PATH = '/finance/months/' + DEMO_INVOICE_ID
RESALE_INVOICE_ID = 'in3_13d28ed903074e4886b1e9ff7666fa43'
MARKER = '【運用サンプル】'
TITLE = MARKER + 'グレージュの二つ折り財布'


class SessionUser(UserMixin):
    def __init__(self, user_id, role='user', permissions=()):
        self.id, self.role, self.permissions = user_id, role, permissions

    def is_admin(self):
        return self.role in ('owner', 'admin')

    def has_permission(self, permission):
        return self.role == 'owner' or permission in self.permissions


class StagingInvoicePaymentTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='kaika-staging-invoice-payment-')
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'fictional.sqlite3'
        with closing(finance_fixture(str(self.path))) as db:
            for statement in (list(funds_schema()) + list(profit_schema()) + list(listing_schema())
                              + list(notifications_common_schema()) + list(notification_schema())
                              + list(sandbox_payments_schema()) + list(demo_schema('sqlite'))):
                db.execute(statement)
            registry = Registry(db)
            billing = BillingService(FinanceStore(registry))
            item_id = registry.create_item(STAFF, TITLE, owner_user_id=CUSTOMER.user_id, custodian='self',
                occurred_at='2026-10-01T08:00:00+09:00', evidence_reference=MARKER + '受付', source_key='demo-wallet')['physical_item_id']
            self.item_id = item_id
            for day, kind in ((2, 'received'), (3, 'sold'), (4, 'shipped')):
                registry.record_event(STAFF, item_id, kind, f'2026-10-0{day}T08:00:00+09:00', 'demo-' + kind, MARKER + ' ' + kind)
            billing.create_item_drafts(STAFF, item_id, source_key='demo-original-fees')
            for line in billing.list_item_drafts(STAFF, item_id):
                amount, suffix = {'work_bundle': (500, 'WORK'), 'sales_fee': (300, 'SALE')}[line['fee_kind']]
                billing.record_component(STAFF, CUSTOMER.user_id, fee_kind='adjustment', amount_net_yen=amount,
                    tax_rate_percent=None, occurred_at='2026-10-05T08:00:00+09:00', item_id=item_id,
                    original_draft_id=line['draft_id'], evidence_reference=MARKER + ' 仮料金 ／ DEMO-WALLET-O01-' + suffix,
                    source_key='demo-reference-' + suffix)
            with patch('stage3_billing.uuid.uuid4', return_value=UUID(hex=DEMO_INVOICE_ID[4:])):
                invoice = billing.create_month(STAFF, CUSTOMER.user_id, '2026-10', source_key='demo-october-draft')
            self.assertEqual(invoice['invoice_id'], DEMO_INVOICE_ID)
            db.commit()

        self.app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='fictional-staging-invoice-fixture', SESSION_PROTECTION=None,
                               STAGE2_INVENTORY_ENABLED=True, STAGE3_FINANCE_ENABLED=True, STAGE4_COMMERCE_ENABLED=True,
                               STAGE5_PAYMENTS_ENABLED=True, STAGE6_ENABLED=True, STAGE6_TEST_DELIVERY_ENABLED=False)
        # Keep the production invoice/frame templates. Only the unrelated site's
        # outer layout and navigation are removed from this scoped test fixture.
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({'base.html': '<!DOCTYPE html><html><body>{% block content %}{% endblock %}</body></html>'}),
            self.app.jinja_loader,
        ])
        self.app.jinja_env.globals['customer_navigation_available'] = True
        self.app.extensions.update(kaika_staging=True, kaika_staging_review={'schema_only': True})
        self.users = {'member': SessionUser(1), 'other': SessionUser(2), 'staff': SessionUser(9, 'owner'),
                      'limited_staff': SessionUser(7, 'admin', ('invoices',))}
        login_manager = LoginManager(self.app)
        login_manager.user_loader(lambda value: next((user for user in self.users.values() if str(user.id) == value), None))
        runtime = SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=self.connect)
        register_inventory_registry(runtime)
        self.web = register_stage3_finance(runtime)
        notifications = register_stage6_web(runtime)
        self.notification_projection = self.enterContext(patch.object(notifications, 'refresh', wraps=notifications.refresh))
        self.clients = {role: self.app.test_client() for role in (*self.users, 'anonymous')}
        for role, user in self.users.items():
            with self.clients[role].session_transaction() as session:
                session['_user_id'], session['_fresh'] = str(user.id), True

    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def post(self, role, action, fields=None, *, invoice_path=INVOICE_PATH):
        return self.clients[role].post(f'{invoice_path}/test-payment/{action}', data=fields or {})

    def signed_fields(self, role, action, invoice_id=DEMO_INVOICE_ID, *, purpose=None):
        with self.app.test_request_context():
            user = self.users[role]
            login_user(user)
            fields = {'registry_csrf': self.web.csrf(),
                      'operation_token': self.web.operation(purpose or 'finance_staging_demo_' + action, invoice_id)}
        with self.clients[role].session_transaction() as session:
            session['_registry_actor'] = str(user.id)
            session['_registry_csrf'] = fields['registry_csrf']
        return fields

    def action_form(self, role, action):
        return self.form(role, INVOICE_PATH, INVOICE_PATH + '/test-payment/' + action)

    def form(self, role, source_path, target_path):
        page = self.clients[role].get(source_path)
        self.assertEqual(page.status_code, 200)
        form = re.search(r'<form\b[^>]*\baction="' + re.escape(target_path) + r'"[^>]*>(.*?)</form>',
                         page.get_data(as_text=True), re.DOTALL)
        if not form:
            return None
        return {name: re.search(r'\bname="' + name + r'"\s+value="([^"]+)"', form.group(1))[1]
                for name in ('registry_csrf', 'operation_token')}

    def rehearsal_rows(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM staging_invoice_payment_demo')]

    def return_rows(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM staging_invoice_payment_return_demo')]

    def resale_rows(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM staging_wallet_resale_payment_demo')]

    def resale_cost_rows(self):
        with closing(self.connect()) as db:
            return [dict(row) for row in db.execute('SELECT * FROM staging_wallet_resale_test_costs')]

    def record_resale_costs(self, page_path, invoice, payment):
        target = page_path + '/test-costs'
        purpose = 'finance_staging_resale_costs'
        amounts = {'purchase_cost_yen': '4200', 'shipping_cost_yen': '450', 'channel_fee_yen': '850'}
        for role in ('member', 'other', 'limited_staff'):
            with self.subTest(unauthorized_costs=role):
                fields = self.signed_fields(role, 'costs', invoice['invoice_id'], purpose=purpose)
                self.assertEqual(self.clients[role].post(target, data={**fields, **amounts}).status_code, 403)
        with patch.dict(self.app.extensions, {'kaika_staging': False}):
            self.assertIsNone(self.form('staff', page_path, target))
            fields = self.signed_fields('staff', 'costs', invoice['invoice_id'], purpose=purpose)
            self.assertEqual(self.clients['staff'].post(target, data={**fields, **amounts}).status_code, 404)
        fields = self.signed_fields('staff', 'costs', DEMO_INVOICE_ID, purpose=purpose)
        self.assertEqual(self.clients['staff'].post(INVOICE_PATH + '/test-costs', data={**fields, **amounts}).status_code, 404)
        self.assertIsNone(self.form('member', page_path, target))
        form = self.form('staff', page_path, target)
        self.assertTrue(form)
        self.assertEqual(self.clients['staff'].post(target, data={**form, 'purchase_cost_yen': '4200'}).status_code, 400)
        self.assertEqual(self.clients['staff'].post(target,
            data={**form, **amounts, 'shipping_cost_yen': '-1'}).status_code, 400)
        self.assertEqual(self.resale_cost_rows(), [])
        self.assertEqual(self.clients['staff'].post(target, data={**form, **amounts}).status_code, 302)
        costs = self.resale_cost_rows()[0]
        self.assertEqual(costs['invoice_id'], invoice['invoice_id'])
        self.assertEqual(costs['sale_event_id'], invoice['snapshot']['lines'][0]['snapshot']['sale_event_id'])
        self.assertEqual(costs['recorded_by'], STAFF.user_id)
        self.assertEqual({key: costs[key] for key in amounts}, {key: int(value) for key, value in amounts.items()})
        self.assertGreaterEqual(datetime.fromisoformat(costs['recorded_at']), datetime.fromisoformat(payment['paid_at']))
        self.assertEqual(self.clients['staff'].post(target, data={**form, **amounts}).status_code, 302)
        self.assertEqual(self.clients['staff'].post(target,
            data={**form, **amounts, 'shipping_cost_yen': '451'}).status_code, 409)
        self.assertEqual(self.resale_cost_rows(), [costs])
        return costs, {**form, **amounts}

    def inbox(self, role):
        response = self.clients[role].get('/notifications')
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def unaffected_tables(self):
        with closing(self.connect()) as db:
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
            names = [row['name'] for row in tables if row['name'].startswith(('business_', 'stage3_', 'stage4_', 'stage5_', 'stage6_', 'stage9_'))
                     and row['name'] != 'stage3_customer_locks']
            return {name: db.execute('SELECT * FROM ' + name).fetchall() for name in names}

    def add_late_charge(self):
        with closing(self.connect()) as db:
            BillingService(FinanceStore(Registry(db))).record_component(STAFF, CUSTOMER.user_id, fee_kind='expense',
                amount_net_yen=100, tax_rate_percent=10, occurred_at='2026-10-06T08:00:00+09:00',
                evidence_reference='different source expense', source_key='late-expense-in-october')
            db.commit()

    def test_staff_sends_and_only_member_can_complete_rehearsal(self):
        before = self.unaffected_tables()
        csv_path = INVOICE_PATH + '/export.csv'
        before_csv = self.clients['member'].get(csv_path)
        self.assertEqual(before_csv.status_code, 200)
        self.assertIsNone(self.action_form('member', 'send'))
        self.assertIsNone(self.action_form('member', 'pay'))
        invoice_link = 'href="' + INVOICE_PATH + '"'
        self.assertNotIn(invoice_link, self.inbox('member'))
        self.assertEqual(self.post('member', 'pay', self.signed_fields('member', 'pay')).status_code, 409)
        self.assertEqual(self.rehearsal_rows(), [])

        send = self.action_form('staff', 'send')
        self.assertTrue(send)
        self.assertEqual(self.post('staff', 'send', {**send, 'amount_yen': '1'}).status_code, 302)
        sent = self.rehearsal_rows()[0]
        self.assertEqual((sent['invoice_id'], sent['customer_id'], sent['sent_by'], sent['amount_yen']),
                         (DEMO_INVOICE_ID, 1, 9, 880))
        self.assertTrue(sent['sent_at'])
        self.assertIsNone(sent['paid_at'])
        self.assertEqual(self.post('staff', 'send', send).status_code, 302)
        self.assertEqual(self.rehearsal_rows(), [sent])
        member_inbox = self.inbox('member')
        self.assertIn(invoice_link, member_inbox)
        self.assertIn('支払待ち（検証）', member_inbox)
        self.assertNotIn(invoice_link, self.inbox('other'))

        self.assertIsNone(self.action_form('staff', 'pay'))
        self.assertEqual(self.post('staff', 'pay', self.signed_fields('staff', 'pay')).status_code, 403)
        self.assertEqual(self.post('other', 'pay', self.signed_fields('other', 'pay')).status_code, 403)
        pay = self.action_form('member', 'pay')
        self.assertTrue(pay)
        self.assertEqual(self.post('member', 'pay', {**pay, 'amount_yen': '1'}).status_code, 302)
        paid = self.rehearsal_rows()[0]
        self.assertEqual((paid['paid_by'], paid['amount_yen']), (1, 880))
        self.assertGreaterEqual(datetime.fromisoformat(paid['paid_at']), datetime.fromisoformat(paid['sent_at']))
        self.assertEqual(self.post('member', 'pay', pay).status_code, 302)
        self.assertEqual(self.rehearsal_rows(), [paid])
        self.assertIsNone(self.action_form('member', 'pay'))
        self.assertIn('支払い済み（検証）', self.inbox('member'))
        self.assertEqual(self.clients['member'].get(csv_path).data, before_csv.data)
        self.assertEqual(self.unaffected_tables(), before)
        self.notification_projection.assert_not_called()

    def test_endpoint_rejects_unauthorized_and_nonstaging_actions(self):
        self.assertEqual(self.clients['other'].get(INVOICE_PATH).status_code, 403)
        self.assertEqual(self.clients['anonymous'].get(INVOICE_PATH).status_code, 401)
        self.assertEqual(self.post('anonymous', 'send').status_code, 401)
        self.assertEqual(self.post('staff', 'send').status_code, 400)
        self.assertEqual(self.post('staff', 'send', self.signed_fields('staff', 'pay')).status_code, 403)
        self.assertEqual(self.post('member', 'send', self.signed_fields('member', 'send')).status_code, 403)
        self.assertEqual(self.post('limited_staff', 'send', self.signed_fields('limited_staff', 'send')).status_code, 403)
        wrong_id = 'in3_unrelated'
        self.assertEqual(self.post('staff', 'send', self.signed_fields('staff', 'send', wrong_id),
                                   invoice_path='/finance/months/' + wrong_id).status_code, 404)
        with patch.dict(self.app.extensions, {'kaika_staging': False}):
            self.assertIsNone(self.action_form('staff', 'send'))
            self.assertEqual(self.post('staff', 'send', self.signed_fields('staff', 'send')).status_code, 404)
        with patch.dict(self.app.extensions, {'kaika_staging_review': None}):
            self.assertIsNone(self.action_form('staff', 'send'))
            self.assertEqual(self.post('staff', 'send', self.signed_fields('staff', 'send')).status_code, 404)
        self.assertEqual(self.rehearsal_rows(), [])

    def test_new_source_line_hides_notice_and_blocks_old_pay_form(self):
        self.assertEqual(self.post('staff', 'send', self.action_form('staff', 'send')).status_code, 302)
        pay = self.action_form('member', 'pay')
        self.assertTrue(pay)
        sent = self.rehearsal_rows()
        self.add_late_charge()
        self.assertEqual(self.post('member', 'pay', pay).status_code, 409)
        self.assertEqual(self.rehearsal_rows(), sent)
        self.assertIsNone(self.action_form('member', 'pay'))
        self.assertNotIn('href="' + INVOICE_PATH + '"', self.inbox('member'))

    def test_staff_records_return_then_refund_without_changing_original_payment(self):
        before = self.unaffected_tables()
        csv_path = INVOICE_PATH + '/export.csv'
        before_csv = self.clients['member'].get(csv_path)
        self.assertEqual(before_csv.status_code, 200)
        for action in ('return', 'refund'):
            with self.subTest(unpaid_action=action):
                self.assertIsNone(self.action_form('staff', action))
                self.assertEqual(self.post('staff', action, self.signed_fields('staff', action)).status_code, 409)
        self.assertEqual(self.rehearsal_rows(), [])
        self.assertEqual(self.return_rows(), [])
        self.assertEqual(self.post('staff', 'send', self.action_form('staff', 'send')).status_code, 302)
        self.assertEqual(self.post('staff', 'return', self.signed_fields('staff', 'return')).status_code, 409)
        self.assertEqual(self.post('member', 'pay', self.action_form('member', 'pay')).status_code, 302)
        paid = self.rehearsal_rows()[0]
        self.assertEqual(self.post('staff', 'refund', self.signed_fields('staff', 'refund')).status_code, 409)
        self.assertIsNone(self.action_form('member', 'return'))
        self.assertEqual(self.post('member', 'return', self.signed_fields('member', 'return')).status_code, 403)

        return_form = self.action_form('staff', 'return')
        self.assertTrue(return_form)
        self.assertEqual(self.post('staff', 'return', return_form).status_code, 302)
        returned = self.return_rows()[0]
        self.assertEqual((returned['invoice_id'], returned['returned_by']), (DEMO_INVOICE_ID, STAFF.user_id))
        self.assertEqual(self.rehearsal_rows(), [paid])
        self.assertGreaterEqual(datetime.fromisoformat(returned['returned_at']), datetime.fromisoformat(paid['paid_at']))
        self.assertIsNone(returned['refunded_at'])
        self.assertEqual(self.post('staff', 'return', return_form).status_code, 302)
        self.assertEqual(self.return_rows(), [returned])
        self.assertIn('返品受付（検証）', self.inbox('member'))
        self.assertIsNone(self.action_form('member', 'refund'))
        for role in ('member', 'limited_staff'):
            with self.subTest(unauthorized_refund=role):
                self.assertEqual(self.post(role, 'refund', self.signed_fields(role, 'refund')).status_code, 403)

        refund_form = self.action_form('staff', 'refund')
        self.assertTrue(refund_form)
        self.assertEqual(self.post('staff', 'refund', {**refund_form, 'amount_yen': '1'}).status_code, 302)
        refunded = self.return_rows()[0]
        self.assertEqual((refunded['returned_at'], refunded['refunded_by']), (returned['returned_at'], STAFF.user_id))
        self.assertGreaterEqual(datetime.fromisoformat(refunded['refunded_at']), datetime.fromisoformat(returned['returned_at']))
        self.assertEqual(self.post('staff', 'refund', refund_form).status_code, 302)
        self.assertEqual(self.return_rows(), [refunded])
        self.assertEqual(self.rehearsal_rows(), [paid])
        member_inbox = self.inbox('member')
        self.assertIn('返金済み（検証）', member_inbox)
        self.assertIn('880円', member_inbox)
        self.assertNotIn(INVOICE_PATH, self.inbox('other'))

        for role in ('staff', 'member'):
            with self.subTest(history_view=role):
                page = self.clients[role].get(INVOICE_PATH)
                self.assertEqual(page.status_code, 200)
                body = re.sub(r'<[^>]+>', ' ', page.get_data(as_text=True))
                for field, label in (('returned_at', '返品受付日時（日本時間）'), ('refunded_at', 'テスト返金日時（日本時間）')):
                    local_time = datetime.fromisoformat(refunded[field]).astimezone(ZoneInfo('Asia/Tokyo'))
                    self.assertRegex(body, re.escape(label) + r'[：:]?\s*' +
                                     re.escape(local_time.strftime('%Y/%m/%d %H:%M')))
        self.assertEqual(self.clients['member'].get(csv_path).data, before_csv.data)
        self.assertEqual(self.unaffected_tables(), before)
        self.notification_projection.assert_not_called()

    def test_new_source_line_hides_notice_and_blocks_old_refund_form(self):
        for role, action in (('staff', 'send'), ('member', 'pay'), ('staff', 'return')):
            self.assertEqual(self.post(role, action, self.action_form(role, action)).status_code, 302)
        refund = self.action_form('staff', 'refund')
        self.assertTrue(refund)
        paid, returned = self.rehearsal_rows(), self.return_rows()
        self.add_late_charge()
        self.assertEqual(self.post('staff', 'refund', refund).status_code, 409)
        self.assertEqual(self.rehearsal_rows(), paid)
        self.assertEqual(self.return_rows(), returned)
        self.assertIsNone(self.action_form('staff', 'refund'))
        self.assertNotIn(INVOICE_PATH, self.inbox('member'))

    def test_refunded_history_stays_on_the_saved_revision_after_the_wallet_returns(self):
        completed_forms = []
        for role, action in (('staff', 'send'), ('member', 'pay'), ('staff', 'return'), ('staff', 'refund')):
            fields = self.action_form(role, action)
            self.assertTrue(fields)
            self.assertEqual(self.post(role, action, fields).status_code, 302)
            completed_forms.append((role, action, fields))
        completed_payment, completed_return = self.rehearsal_rows(), self.return_rows()

        with closing(self.connect()) as db:
            registry = Registry(db)
            billing = BillingService(FinanceStore(registry))
            old_invoice = billing.get_month(CUSTOMER, DEMO_INVOICE_ID)
            self.assertTrue(old_invoice['is_current_snapshot'])
            registry.record_event(STAFF, self.item_id, 'returned', '2026-10-06T08:00:00+09:00',
                'wallet-arrived-back', MARKER + ' 実際の在庫処理を模擬するテストの返品記録',
                details={'return_reason': 'fixture wallet return'})
            stale_invoice = billing.get_month(CUSTOMER, DEMO_INVOICE_ID)
            self.assertFalse(stale_invoice['is_current_snapshot'])
            self.assertIn('source_changed_after_snapshot', stale_invoice['current_review_reasons'])
            self.assertEqual(stale_invoice['content_hash'], old_invoice['content_hash'])
            self.assertEqual(stale_invoice['snapshot_json'], old_invoice['snapshot_json'])
            updated = billing.create_month(STAFF, CUSTOMER.user_id, '2026-10', source_key='wallet-month-revised-after-return')
            self.assertNotEqual(updated['invoice_id'], DEMO_INVOICE_ID)
            db.commit()

        before_replay = self.unaffected_tables()
        for role, action, fields in completed_forms:
            with self.subTest(stale_replay=action):
                self.assertIsNone(self.action_form(role, action))
                self.assertEqual(self.post(role, action, fields).status_code, 409)
        self.assertEqual(self.rehearsal_rows(), completed_payment)
        self.assertEqual(self.return_rows(), completed_return)
        self.assertEqual(self.unaffected_tables(), before_replay)
        self.notification_projection.assert_not_called()

        for role in ('staff', 'member'):
            with self.subTest(history_view=role):
                page = self.clients[role].get(INVOICE_PATH)
                self.assertEqual(page.status_code, 200)
                body = page.get_data(as_text=True)
                for text in (TITLE, '500円', '300円', '80円', '880円', '返金済み（検証）'):
                    self.assertIn(text, body)
        invoice_link = 'href="' + INVOICE_PATH + '"'
        old_inbox = self.inbox('member')
        self.assertIn('返金済み（検証）', old_inbox)
        self.assertIn(invoice_link, old_inbox)
        self.assertNotIn(invoice_link, self.inbox('other'))
        self.assertEqual(self.clients['other'].get(INVOICE_PATH).status_code, 403)
        new_page = self.clients['member'].get('/finance/months/' + updated['invoice_id'])
        self.assertEqual(new_page.status_code, 200)
        self.assertNotIn('返金済み（検証）', new_page.get_data(as_text=True))

        with closing(self.connect()) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE staging_invoice_payment_demo SET amount_yen=881 WHERE invoice_id=?', (DEMO_INVOICE_ID,))
            db.execute('UPDATE staging_invoice_payment_demo SET content_hash=? WHERE invoice_id=?', ('wrong-fixture-hash', DEMO_INVOICE_ID))
            db.commit()
        self.assertNotIn('返金済み（検証）', self.clients['member'].get(INVOICE_PATH).get_data(as_text=True))
        self.assertNotIn(invoice_link, self.inbox('member'))

    def test_same_month_resale_keeps_refunded_fee_lines_in_history_only_on_staging(self):
        for role, action in (('staff', 'send'), ('member', 'pay'), ('staff', 'return'), ('staff', 'refund')):
            self.assertEqual(self.post(role, action, self.action_form(role, action)).status_code, 302)
        completed_payment, completed_return = self.rehearsal_rows(), self.return_rows()

        with closing(self.connect()) as db:
            registry = Registry(db)
            store = FinanceStore(registry)
            billing = BillingService(store)
            old_invoice = billing.get_month(CUSTOMER, DEMO_INVOICE_ID)
            old_lines = old_invoice['snapshot']['lines']
            old_drafts = {line['draft_id'] for line in old_lines}
            old_shipments = {line['source_event_id'] for line in old_lines if line['source_event_id']}
            self.assertEqual((len(old_drafts), len(old_shipments)), (4, 1))
            registry.record_event(STAFF, self.item_id, 'returned', '2026-10-06T08:00:00+09:00',
                'wallet-back', MARKER + ' 返品受領', details={'return_reason': 'fixture-only'})
            registry.record_event(STAFF, self.item_id, 'release_hold', '2026-10-07T08:00:00+09:00',
                'wallet-release', MARKER + ' 再検品済み', details={'note': 'fixture-only'})
            registry.record_event(STAFF, self.item_id, 'sold', '2026-10-08T08:00:00+09:00',
                'wallet-second-sale', MARKER + ' 再販売')
            ProfitService(store).record_facts(STAFF, self.item_id, amounts={'sale_gross_yen': 8500},
                channel='multichannel', source_reference=MARKER + ' 再販売の明細', occurred_at='2026-10-08T09:00:00+09:00',
                source_key='wallet-resale-facts')
            registry.record_event(STAFF, self.item_id, 'shipped', '2026-10-09T08:00:00+09:00',
                'wallet-second-shipment', 'DEMO-WALLET-S02 ' + MARKER + ' 再発送')
            second_collection = billing.create_item_drafts(STAFF, self.item_id, source_key='wallet-resale-fees')
            repeated = billing.create_item_drafts(STAFF, self.item_id, source_key='wallet-resale-fees-repeat')
            new_drafts = set(second_collection['draft_ids'])
            self.assertEqual(new_drafts, set(repeated['draft_ids']))
            self.assertEqual(len(new_drafts), 2)
            self.assertTrue(new_drafts.isdisjoint(old_drafts))
            db.commit()

        month_source = f'/finance/customers/{CUSTOMER.user_id}'
        month_target = month_source + '/month'
        with patch.dict(self.app.extensions, {'kaika_staging': False}):
            fields = self.form('staff', month_source, month_target)
            generic = self.clients['staff'].post(month_target, data={**fields, 'period': '2026-10'})
            self.assertEqual(generic.status_code, 302)
        with closing(self.connect()) as db:
            billing = BillingService(FinanceStore(Registry(db)))
            normal_month_id = billing.list_months(CUSTOMER, CUSTOMER.user_id)[0]['invoice_id']
            normal_month = billing.get_month(CUSTOMER, normal_month_id)
            self.assertTrue(normal_month['is_current_snapshot'])
            self.assertEqual({line['draft_id'] for line in normal_month['snapshot']['lines']}, old_drafts | new_drafts)
            self.assertNotIn('staging_refund_history', normal_month['snapshot'])

        fields = self.form('staff', month_source, month_target)
        with patch('stage3_billing.uuid', SimpleNamespace(uuid4=lambda: UUID(hex=RESALE_INVOICE_ID[4:]))):
            staged = self.clients['staff'].post(month_target, data={**fields, 'period': '2026-10'})
        self.assertEqual(staged.status_code, 302)
        with closing(self.connect()) as db:
            billing = BillingService(FinanceStore(Registry(db)))
            current_id = billing.list_months(CUSTOMER, CUSTOMER.user_id)[0]['invoice_id']
            self.assertEqual(current_id, RESALE_INVOICE_ID)
            current = billing.get_month(CUSTOMER, current_id)
            self.assertTrue(current['is_current_snapshot'])
            snapshot = current['snapshot']
            self.assertEqual({line['draft_id'] for line in snapshot['lines']}, new_drafts)
            self.assertEqual({line['fee_kind'] for line in snapshot['lines']}, {'work_bundle', 'sales_fee'})
            shipment_ids = {line['source_event_id'] for line in snapshot['lines']}
            self.assertEqual(len(shipment_ids), 1)
            self.assertTrue(shipment_ids.isdisjoint(old_shipments))
            self.assertFalse(snapshot['uncollected_shipments'])
            self.assertEqual(snapshot['staging_refund_history']['invoice_id'], DEMO_INVOICE_ID)
            self.assertEqual(set(snapshot['staging_refund_history']['draft_ids']), old_drafts)
            self.assertEqual(set(snapshot['staging_refund_history']['shipment_event_ids']), old_shipments)
            for line in snapshot['lines']:
                self.assertIsNone(line['amount_net_yen'])
                self.assertIn('return_cycle_requires_review', line['reason_codes'])
                self.assertEqual(line['snapshot']['sale_fact_snapshot']['sale_gross_yen'], 8500)
            self.assertIsNone(snapshot['totals']['payable_total_yen'])
            self.assertIsNone(snapshot['totals']['reference_gross_total_yen'])
            self.assertFalse(snapshot['chargeable'])
            self.assertEqual(billing.get_month(CUSTOMER, DEMO_INVOICE_ID)['snapshot_json'], old_invoice['snapshot_json'])

        resale_path = '/finance/months/' + current_id
        export_path = resale_path + '/export.csv'
        before_resale_csv = self.clients['member'].get(export_path)
        self.assertEqual(before_resale_csv.status_code, 200)
        self.assertNotIn('DEMO-WALLET-O01', before_resale_csv.get_data(as_text=True))
        revised_fields = self.form('staff', month_source, month_target)
        repeated_save = self.clients['staff'].post(month_target, data={**revised_fields, 'period': '2026-10'})
        self.assertEqual(repeated_save.status_code, 302)
        with closing(self.connect()) as db:
            months = BillingService(FinanceStore(Registry(db))).list_months(CUSTOMER, CUSTOMER.user_id)
            self.assertEqual([month['invoice_id'] for month in months], [current_id, normal_month_id, DEMO_INVOICE_ID])
        self.assertEqual(self.rehearsal_rows(), completed_payment)
        self.assertEqual(self.return_rows(), completed_return)
        self.assertIn('返金済み（検証）', self.inbox('member'))

        self.notification_projection.reset_mock()
        before = self.unaffected_tables()
        original_csv = self.clients['member'].get(INVOICE_PATH + '/export.csv').data
        self.assertEqual(self.resale_rows(), [])
        member_page = self.clients['member'].get(resale_path)
        self.assertEqual(member_page.status_code, 200)
        member_body = member_page.get_data(as_text=True)
        self.assertIn('販売料', member_body)
        for amount in (300, 30, 330):
            self.assertRegex(member_body, rf'>\s*{amount}円\s*<')
        item_path = '/finance/items/' + self.item_id
        reconciliation_label = '販売料だけを差し引いた参考額'
        for path in (item_path, month_source, resale_path):
            self.assertNotIn('8,170円', self.clients['member'].get(path).get_data(as_text=True))
        self.assertNotIn('検証集計（再販売分）', before_resale_csv.get_data(as_text=True))
        costs_target = resale_path + '/test-costs'
        self.assertIsNone(self.form('staff', resale_path, costs_target))
        premature_costs = self.signed_fields('staff', 'costs', current_id, purpose='finance_staging_resale_costs')
        self.assertEqual(self.clients['staff'].post(costs_target, data={**premature_costs,
            'purchase_cost_yen': '4200', 'shipping_cost_yen': '450', 'channel_fee_yen': '850'}).status_code, 409)
        self.assertEqual(self.resale_cost_rows(), [])
        send_target = resale_path + '/test-payment/send'
        pay_target = resale_path + '/test-payment/pay'
        self.assertIsNone(self.form('member', resale_path, pay_target))
        self.assertEqual(self.post('member', 'pay', self.signed_fields('member', 'pay', current_id),
                                   invoice_path=resale_path).status_code, 409)
        self.assertEqual(self.post('member', 'send', self.signed_fields('member', 'send', current_id),
                                   invoice_path=resale_path).status_code, 403)
        for action in ('return', 'refund'):
            with self.subTest(unavailable_resale_action=action):
                self.assertEqual(self.post('staff', action, self.signed_fields('staff', action, current_id),
                                           invoice_path=resale_path).status_code, 404)
        normal_path = '/finance/months/' + normal_month_id
        self.assertIsNone(self.form('staff', normal_path, normal_path + '/test-payment/send'))
        self.assertEqual(self.post('staff', 'send', self.signed_fields('staff', 'send', normal_month_id),
                                   invoice_path=normal_path).status_code, 404)
        with patch.dict(self.app.extensions, {'kaika_staging': False}):
            self.assertIsNone(self.form('staff', resale_path, send_target))
            self.assertEqual(self.post('staff', 'send', self.signed_fields('staff', 'send', current_id),
                                       invoice_path=resale_path).status_code, 404)

        send = self.form('staff', resale_path, send_target)
        self.assertTrue(send)
        self.assertEqual(self.post('staff', 'send', {**send, 'amount_yen': '1'}, invoice_path=resale_path).status_code, 302)
        sent = self.resale_rows()[0]
        self.assertEqual((sent['invoice_id'], sent['customer_id'], sent['sent_by'], sent['amount_yen'], sent['content_hash']),
                         (current_id, CUSTOMER.user_id, STAFF.user_id, 330, current['content_hash']))
        self.assertIsNone(sent['paid_at'])
        self.assertEqual(self.post('staff', 'send', send, invoice_path=resale_path).status_code, 302)
        self.assertEqual(self.resale_rows(), [sent])
        inbox_pending = self.inbox('member')
        other_inbox = self.inbox('other')
        for path in (INVOICE_PATH, resale_path):
            self.assertIn('href="' + path + '"', inbox_pending)
            self.assertNotIn('href="' + path + '"', other_inbox)
        self.assertIn('支払待ち（検証）', inbox_pending)
        self.assertIn('返金済み（検証）', inbox_pending)

        for role in ('staff', 'other'):
            with self.subTest(unauthorized_resale_payment=role):
                self.assertEqual(self.post(role, 'pay', self.signed_fields(role, 'pay', current_id),
                                           invoice_path=resale_path).status_code, 403)
        pay = self.form('member', resale_path, pay_target)
        self.assertTrue(pay)
        self.assertEqual(self.post('member', 'pay', {**pay, 'amount_yen': '1'}, invoice_path=resale_path).status_code, 302)
        paid = self.resale_rows()[0]
        self.assertEqual((paid['amount_yen'], paid['paid_by']), (330, CUSTOMER.user_id))
        self.assertGreaterEqual(datetime.fromisoformat(paid['paid_at']), datetime.fromisoformat(paid['sent_at']))
        self.assertEqual(self.post('member', 'pay', pay, invoice_path=resale_path).status_code, 302)
        self.assertEqual(self.resale_rows(), [paid])
        self.assertIsNone(self.form('member', resale_path, pay_target))
        inbox_paid = self.inbox('member')
        for text in ('330円', '880円', '支払い済み（検証）', '返金済み（検証）'):
            self.assertIn(text, inbox_paid)
        self.assertEqual(self.rehearsal_rows(), completed_payment)
        self.assertEqual(self.return_rows(), completed_return)
        self.assertEqual(self.unaffected_tables(), before)
        self.assertEqual(self.clients['member'].get(INVOICE_PATH + '/export.csv').data, original_csv)
        self.notification_projection.assert_not_called()

        for role in ('staff', 'member'):
            for path in (item_path, month_source, resale_path):
                with self.subTest(paid_reconciliation_role=role, path=path):
                    response = self.clients[role].get(path)
                    self.assertEqual(response.status_code, 200)
                    body = response.get_data(as_text=True)
                    for text in (reconciliation_label, '8,500円', '330円', '8,170円'):
                        self.assertIn(text, body)
                    self.assertNotIn('880円', body)
        self.assertNotIn(reconciliation_label, self.clients['member'].get(INVOICE_PATH).get_data(as_text=True))
        self.assertIn(self.clients['other'].get(item_path).status_code, (403, 404))
        for path in (month_source, resale_path, export_path):
            self.assertEqual(self.clients['other'].get(path).status_code, 403)

        paid_csv = self.clients['member'].get(export_path)
        self.assertEqual(paid_csv.status_code, 200)
        original_rows = list(csv.reader(StringIO(before_resale_csv.get_data(as_text=True))))
        paid_rows = list(csv.reader(StringIO(paid_csv.get_data(as_text=True))))
        self.assertEqual(paid_rows[:len(original_rows)], original_rows)
        reconciliation_rows = [
            ['検証集計（再販売分）', '売上（税込・円）', '8500'],
            ['検証集計（再販売分）', 'テスト販売料（税込・円）', '330'],
            ['検証集計（再販売分）', '販売料だけを差し引いた参考額（円）', '8170'],
            ['検証集計（再販売分）', '最終的な手残り', '未確認'],
        ]
        self.assertEqual(paid_rows[len(original_rows):], reconciliation_rows)
        with patch.dict(self.app.extensions, {'kaika_staging': False}):
            for path in (item_path, month_source, resale_path):
                self.assertNotIn(reconciliation_label, self.clients['member'].get(path).get_data(as_text=True))
            self.assertEqual(self.clients['member'].get(export_path).data, before_resale_csv.data)

        costs, cost_request = self.record_resale_costs(resale_path, current, paid)
        final_label = '最終的な手残り（検証用）'
        for role in ('staff', 'member'):
            for path in (item_path, month_source, resale_path):
                with self.subTest(recorded_costs_role=role, path=path):
                    response = self.clients[role].get(path)
                    self.assertEqual(response.status_code, 200)
                    text = response.get_data(as_text=True)
                    for value in (final_label, '4,200円', '450円', '850円', '2,670円'):
                        self.assertIn(value, text)
        self.assertEqual(self.unaffected_tables(), before)
        self.notification_projection.assert_not_called()
        self.assertEqual(self.rehearsal_rows(), completed_payment)
        self.assertEqual(self.return_rows(), completed_return)
        self.assertEqual(self.resale_rows(), [paid])
        self.assertEqual(self.clients['member'].get(INVOICE_PATH + '/export.csv').data, original_csv)
        self.assertNotIn('2,670円', self.clients['member'].get(INVOICE_PATH).get_data(as_text=True))
        cost_reconciliation_rows = reconciliation_rows[:3] + [
            ['検証集計（再販売分）', '仕入原価（円）', '4200'],
            ['検証集計（再販売分）', '送料（円）', '450'],
            ['検証集計（再販売分）', '販路手数料（円）', '850'],
            ['検証集計（再販売分）', '最終的な手残り（検証用・円）', '2670'],
        ]
        costs_csv = list(csv.reader(StringIO(self.clients['member'].get(export_path).get_data(as_text=True))))
        self.assertEqual(costs_csv, original_rows + cost_reconciliation_rows)

        with closing(self.connect()) as db:
            registry = Registry(db)
            store = FinanceStore(registry)
            profit = ProfitService(store).profit(CUSTOMER, self.item_id)
            self.assertEqual(profit['sale_payment_status'], 'unpaid')
            for real_cost in ('purchase_cost_yen', 'shipping_cost_yen', 'channel_fee_yen', 'kaika_fee_yen'):
                self.assertIsNone(profit['facts'][real_cost])
            self.assertIsNone(profit['realized_profit_yen'])
            registry.record_event(STAFF, self.item_id, 'returned', '2026-10-10T08:00:00+09:00',
                'wallet-resale-returned', MARKER + ' 再販売後の返品を検証', details={'return_reason': 'fixture-only'})
            db.commit()
        for path in (item_path, month_source):
            self.assertNotIn(reconciliation_label, self.clients['member'].get(path).get_data(as_text=True))
        self.assertEqual(self.clients['staff'].post(costs_target, data=cost_request).status_code, 409)
        self.assertEqual(self.resale_cost_rows(), [costs])
        historic = self.clients['member'].get(resale_path).get_data(as_text=True)
        self.assertIn('8,170円', historic)
        self.assertIn('2,670円', historic)
        historic_rows = list(csv.reader(StringIO(self.clients['member'].get(export_path).get_data(as_text=True))))
        self.assertEqual(historic_rows[-len(cost_reconciliation_rows):], cost_reconciliation_rows)
        self.assertEqual(self.resale_rows(), [paid])
