"""Stage 1 HTTP boundaries: fictional users and temporary SQLite only."""
import ast
from copy import deepcopy
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask, request
from flask_login import LoginManager, UserMixin
from jinja2 import ChoiceLoader, DictLoader
from business_review import register_business_review

ROOT = Path(__file__).resolve().parents[1]


class User(UserMixin):
    def __init__(self, identifier):
        self.id = identifier
    def is_admin(self):
        return self.id == 9
    def is_owner(self):
        return self.id == 9


class BusinessReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='kaika-stage1-review-')
        self.addCleanup(self.temp.cleanup)
        self.db_path = Path(self.temp.name) / 'fixture.sqlite3'
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE invoices(id INTEGER PRIMARY KEY, sender_id INTEGER, status TEXT,
                document_scope TEXT, source_admin_kaitori_id INTEGER);
            INSERT INTO invoices VALUES(1,1,'draft','admin_kaitori',1);
            INSERT INTO invoices VALUES(2,1,'draft','user_created',NULL);
            CREATE TABLE shikiriosho(id INTEGER PRIMARY KEY, status TEXT);
            INSERT INTO shikiriosho VALUES(1,'sent');
            INSERT INTO shikiriosho VALUES(2,'draft');
        """)
        conn.close()
        self.calls = []
        self.app = Flask('business-review-fixture', template_folder=str(ROOT / 'templates'))
        self.app.config.update(TESTING=True, SECRET_KEY='fictional-review-secret-only')
        self.app.jinja_loader = ChoiceLoader([DictLoader({'base.html': '{% block title %}{% endblock %}{% block content %}{% endblock %}'}), self.app.jinja_loader])
        login = LoginManager(self.app)
        login.user_loader(lambda identifier: User(int(identifier)))
        self.runtime = SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=self.db)
        def mutation(**kwargs):
            self.calls.append((request.endpoint, request.method))
            return 'original-handler'
        for endpoint, path, methods in [
            ('admin_monthly_settlement_create', '/monthly', ['GET','POST']),
            ('admin_step4_client_invoice_create', '/step4', ['GET','POST']),
            ('admin_shikiriosho_edit', '/settlement/<int:id>/edit', ['GET','POST']),
            ('admin_shikiriosho_send', '/settlement/<int:id>/send', ['GET','POST']),
            ('admin_shikiriosho_send_bulk', '/settlement/send-bulk', ['POST']),
            ('admin_seisan_add', '/seisan/add', ['GET','POST']),
            ('admin_seisan_edit', '/seisan/<int:id>/edit', ['GET','POST']),
            ('admin_seisan_send', '/seisan/<int:id>/send', ['GET','POST']),
            ('admin_invoice_approve', '/admin/invoice/<int:id>/approve', ['GET','POST']),
            ('admin_shikiriosho_delete', '/settlement/<int:id>/delete', ['GET','POST']),
            ('admin_seisan_delete', '/seisan/<int:id>/delete', ['GET','POST']),
            ('admin_kaitori_delete', '/admin/invoice/<int:id>/delete', ['GET','POST']),
            ('user_invoice_send', '/invoice/<int:id>/send', ['GET','POST']),
        ]:
            self.app.add_url_rule(path, endpoint=endpoint, view_func=mutation, methods=methods)
        register_business_review(self.runtime)
        self.client = self.app.test_client()

    def db(self):
        return sqlite3.connect(self.db_path)

    def login(self, identifier=9):
        with self.client.session_transaction() as session:
            session.clear()
            session['_user_id'] = str(identifier)
            session['_fresh'] = True

    def token(self):
        page = self.client.get('/admin/business-policy')
        self.assertEqual(page.status_code, 200)
        return re.search(r'name="business_review_csrf" value="([^"]+)"', page.get_data(as_text=True)).group(1)

    def test_registration_and_review_do_not_open_business_database(self):
        self.login()
        with patch.object(self.runtime, 'get_db', side_effect=AssertionError('No DB in policy review')):
            register_business_review(self.runtime)
            response = self.client.get('/admin/business-policy')
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('¥2,980', text)
        self.assertIn('¥21,780', text)
        self.assertIn('100個品', text.replace('</strong>', ''))
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_customer_and_anonymous_cannot_review_internal_policy(self):
        self.assertEqual(self.client.get('/admin/business-policy').status_code, 401)
        self.login(1)
        self.assertEqual(self.client.get('/admin/business-policy').status_code, 403)

    def test_csrf_and_account_switch_cannot_create_a_preview(self):
        self.login()
        token = self.token()
        data = dict(case='normal', channel='wholesale', sale_price='30000')
        self.assertEqual(self.client.post('/admin/business-policy/preview', data=data).status_code, 403)
        self.login(1)
        data['business_review_csrf'] = token
        self.assertEqual(self.client.post('/admin/business-policy/preview', data=data).status_code, 403)

    def test_forged_approvals_do_not_turn_draft_into_invoice(self):
        self.login()
        token = self.token()
        for case in ('normal', 'before_start', 'mixed_start', 'return_unresolved'):
            with self.subTest(case=case), patch.object(self.runtime, 'get_db', side_effect=AssertionError('No DB writes')):
                response = self.client.post('/admin/business-policy/preview', data={
                    'case':case, 'channel':'multichannel', 'sale_price':'30000',
                    'business_review_csrf':token, 'download':'json',
                    'status':'approved', 'approved': 'true', 'customer_id':'9'})
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload['status'], 'draft')
            self.assertFalse(payload['live_payment_enabled'])
            self.assertFalse(payload['eligibility']['allowed'])
            self.assertIsNone(payload['quote']['total_charge_yen'])
            self.assertIn('attachment', response.headers['Content-Disposition'])

    def test_invalid_money_or_unrecognized_case_is_rejected(self):
        self.login()
        token = self.token()
        for amount in ('0','-1','1.5','1e3','9999999999999','３００００'):
            response = self.client.post('/admin/business-policy/preview', data={
                'case':'normal','channel':'wholesale','sale_price':amount,'business_review_csrf':token})
            self.assertEqual(response.status_code, 400, amount)

    def test_review_renders_confirmed_tax_direction_but_withholds_invoice_total(self):
        self.login()
        response = self.client.post('/admin/business-policy/preview', data={
            'case':'mixed_start','channel':'dealer_auction','sale_price':'50000','business_review_csrf':self.token()})
        self.assertEqual(response.status_code, 200)
        text = response.get_data(as_text=True)
        self.assertIn('請求確定は保留', text)
        self.assertIn('請求合計は未確定', text)
        self.assertIn('880', text)
        self.assertIn('税別 500円', text)
        self.assertIn('税込例 550円', text)
        self.assertIn('配分額は未定', text)

    def test_unbound_monthly_and_step4_finalization_do_not_reach_original_handlers(self):
        self.login()
        for path, data in [('/monthly', {}),('/monthly', {'status':'completed'}),
                           ('/monthly', {'status':'sent','approved':'true'}),
                           ('/step4', {'status':'draft'}),('/settlement/1/send', {}),
                           ('/settlement/send-bulk', {'send_scope':'all'}),
                           ('/seisan/add', {}), ('/seisan/add', {'status':'completed'}),
                           ('/seisan/1/send', {}), ('/admin/invoice/1/approve', {}),
                           ('/admin/invoice/1/approve', {})]:
            self.assertEqual(self.client.post(path, data=data).status_code, 409)
        self.assertEqual(self.calls, [])

    def test_monthly_draft_can_continue_but_sent_invoice_cannot_be_rewritten(self):
        self.login()
        self.assertEqual(self.client.post('/monthly', data={'status':'draft'}).status_code, 200)
        self.assertEqual(self.client.post('/settlement/2/edit', data={'status':'draft'}).status_code, 200)
        self.assertEqual(self.client.post('/seisan/add', data={'status':'draft'}).status_code, 200)
        self.assertEqual(self.client.post('/seisan/2/edit', data={'status':'draft'}).status_code, 200)
        self.calls.clear()
        self.assertEqual(self.client.post('/settlement/1/edit', data={'status':'draft'}).status_code, 409)
        self.assertEqual(self.client.post('/seisan/1/edit', data={'status':'draft'}).status_code, 409)
        self.assertEqual(self.calls, [])

    def test_kaika_draft_cannot_be_sent_through_customer_route(self):
        self.login(1)
        self.assertEqual(self.client.get('/invoice/1/send').status_code, 409)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.get('/invoice/2/send').status_code, 200)

    def test_other_customer_cannot_send_invoice(self):
        self.login(2)
        self.assertEqual(self.client.get('/invoice/1/send').status_code, 403)
        self.assertEqual(self.calls, [])

    def test_issued_settlement_cannot_be_erased_through_primary_or_alias_route(self):
        self.login()
        for path in ('/settlement/1/delete', '/seisan/1/delete'):
            self.assertEqual(self.client.post(path).status_code, 409)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client.post('/settlement/2/delete').status_code, 200)
        self.calls.clear()
        with patch.object(self.runtime, 'get_db', side_effect=RuntimeError('fixture unavailable')):
            self.assertEqual(self.client.post('/settlement/2/delete').status_code, 409)
        self.assertEqual(self.calls, [])
        self.login(1)
        self.assertEqual(self.client.post('/admin/invoice/1/delete').status_code, 403)
        self.assertEqual(self.calls, [])

    def test_database_failure_never_grants_finalization(self):
        self.login(1)
        with patch.object(self.runtime, 'get_db', side_effect=RuntimeError('fictional failure')):
            self.assertEqual(self.client.get('/invoice/1/send').status_code, 409)
        self.assertEqual(self.calls, [])


class SourceOnlyLoaderTests(unittest.TestCase):
    def test_security_startup_failure_never_falls_back_to_bytecode(self):
        tree = ast.parse((ROOT / 'render_app.py').read_text(encoding='utf-8-sig'))
        node = deepcopy(next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'load_runtime_module'))
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), '<loader-under-test>', 'exec'), namespace)
        original = __import__
        class StartupRejected(Exception):
            pass
        def reject_app(name, *args, **kwargs):
            if name == 'app':
                raise StartupRejected('configuration not ready')
            return original(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=reject_app):
            with self.assertRaises(StartupRejected):
                namespace['load_runtime_module']()


if __name__ == '__main__':
    unittest.main()

