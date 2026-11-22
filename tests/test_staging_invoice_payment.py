"""Real Flask routes and SQLite rehearsal records. No provider or network is used."""
from contextlib import closing
from datetime import datetime
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
from stage3_profit_schema import migration_statements as profit_schema
from stage3_web import register_stage3_finance
from stage6_common import migration_statements as notifications_common_schema
from stage6_notifications_schema import migration_statements as notification_schema
from stage6_web import register_stage6_web
from stage9_payment_schema import statements as sandbox_payments_schema
from staging_invoice_payment import DEMO_INVOICE_ID, migration_statements as demo_schema
from test_stage5_payments import CUSTOMER, STAFF, fixture as finance_fixture


ROOT = Path(__file__).resolve().parents[1]
INVOICE_PATH = '/finance/months/' + DEMO_INVOICE_ID
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
            for statement in (list(funds_schema()) + list(profit_schema()) + list(notifications_common_schema()) + list(notification_schema())
                              + list(sandbox_payments_schema()) + list(demo_schema('sqlite'))):
                db.execute(statement)
            registry = Registry(db)
            billing = BillingService(FinanceStore(registry))
            item_id = registry.create_item(STAFF, TITLE, owner_user_id=CUSTOMER.user_id, custodian='self',
                occurred_at='2026-10-01T08:00:00+09:00', evidence_reference=MARKER + '受付', source_key='demo-wallet')['physical_item_id']
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

    def signed_fields(self, role, action, invoice_id=DEMO_INVOICE_ID):
        with self.app.test_request_context():
            user = self.users[role]
            login_user(user)
            fields = {'registry_csrf': self.web.csrf(),
                      'operation_token': self.web.operation('finance_staging_demo_' + action, invoice_id)}
        with self.clients[role].session_transaction() as session:
            session['_registry_actor'] = str(user.id)
            session['_registry_csrf'] = fields['registry_csrf']
        return fields

    def action_form(self, role, action):
        page = self.clients[role].get(INVOICE_PATH)
        self.assertEqual(page.status_code, 200)
        target = re.escape(INVOICE_PATH + '/test-payment/' + action)
        form = re.search(r'<form\b[^>]*\baction="' + target + r'"[^>]*>(.*?)</form>',
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
