"""Feature billing tests with temporary SQLite and an in-memory Stripe double.

Never imports app/render_app, initializes the real DB, or calls a payment API.
Run: python -m unittest discover -s tests -p test_feature_plans.py -v
"""
from copy import deepcopy
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask
from flask_login import LoginManager, UserMixin
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader, StrictUndefined

from feature_plans import register_feature_plans


class FixtureUser(UserMixin):
    def __init__(self, row):
        self.__dict__.update(dict(row))

    def is_admin(self):
        return self.role in ('admin', 'owner')

    def has_permission(self, permission):
        return self.role == 'owner' or permission in json.loads(self.permissions or '[]')


class FakeStripe:
    """Only the API methods used by the service; unknown methods fail locally."""
    def __init__(self):
        self.calls = []
        self.prices = {}
        self.subscriptions = {}
        self.sessions = {}
        self.schedules = {}
        self.change_config = {'id': 'bpc_change', 'active': True, 'features': {
            'subscription_update': {'enabled': True, 'default_allowed_updates': ['price'],
                'proration_behavior': 'always_invoice', 'billing_cycle_anchor': 'unchanged',
                'schedule_at_period_end': {'conditions': [{'type': 'decreasing_item_amount'}]},
                'products': [{'product': 'prod_feature', 'prices': ['price_normal', 'price_business']}]},
            'subscription_cancel': {'enabled': True, 'mode': 'at_period_end'},
        }}
        self.portal_config = {'id': 'bpc_fixture', 'active': True, 'features': {
            'subscription_update': {'enabled': False},
            'subscription_cancel': {'enabled': True, 'mode': 'at_period_end'},
        }}
        self.subscription_list = {'data': [], 'has_more': False}
        self.fail_subscription_once = False
        self.fail_checkout_once = False
        self.Price = SimpleNamespace(retrieve=self.retrieve_price)
        self.Customer = SimpleNamespace(create=self.create_customer)
        self.Subscription = SimpleNamespace(retrieve=self.retrieve_subscription, list=self.list_subscriptions)
        self.SubscriptionSchedule = SimpleNamespace(retrieve=self.retrieve_schedule)
        self.checkout = SimpleNamespace(Session=SimpleNamespace(create=self.create_checkout, retrieve=self.retrieve_checkout))
        self.billing_portal = SimpleNamespace(Session=SimpleNamespace(create=self.create_portal),
            Configuration=SimpleNamespace(retrieve=self.retrieve_portal_config))
        self.Webhook = SimpleNamespace(construct_event=self.construct_event)

    def record(self, method, kwargs):
        self.calls.append((method, deepcopy(kwargs)))

    def retrieve_price(self, price_id, **kwargs):
        self.record('Price.retrieve', dict(kwargs, price_id=price_id))
        return deepcopy(self.prices[price_id])

    def create_customer(self, **kwargs):
        self.record('Customer.create', kwargs)
        return {'id': 'cus_feature_' + kwargs['metadata']['kaika_feature_user_id']}

    def list_subscriptions(self, **kwargs):
        self.record('Subscription.list', kwargs)
        return deepcopy(self.subscription_list)

    def retrieve_subscription(self, subscription_id, **kwargs):
        self.record('Subscription.retrieve', dict(kwargs, subscription_id=subscription_id))
        if self.fail_subscription_once:
            self.fail_subscription_once = False
            raise RuntimeError('Fixture temporary Stripe failure')
        return deepcopy(self.subscriptions[subscription_id])

    def create_checkout(self, **kwargs):
        self.record('checkout.Session.create', kwargs)
        if self.fail_checkout_once:
            self.fail_checkout_once = False
            raise RuntimeError('Fixture temporary checkout failure')
        session = {'id': 'cs_fixture', 'url': 'https://checkout.stripe.com/c/pay/cs_fixture',
                   'status': 'open', 'expires_at': int(time.time()) + 86400}
        self.sessions[session['id']] = session
        return deepcopy(session)

    def retrieve_checkout(self, session_id, **kwargs):
        self.record('checkout.Session.retrieve', dict(kwargs, session_id=session_id))
        return deepcopy(self.sessions[session_id])

    def create_portal(self, **kwargs):
        self.record('billing_portal.Session.create', kwargs)
        return {'url': 'https://billing.stripe.com/p/session/fixture'}

    def retrieve_portal_config(self, configuration_id, **kwargs):
        self.record('billing_portal.Configuration.retrieve', dict(kwargs, configuration_id=configuration_id))
        return deepcopy(self.change_config if configuration_id == 'bpc_change' else self.portal_config)

    def retrieve_schedule(self, schedule_id, **kwargs):
        self.record('SubscriptionSchedule.retrieve', dict(kwargs, schedule_id=schedule_id))
        return deepcopy(self.schedules[schedule_id])

    @staticmethod
    def signature(body):
        return hmac.new(b'whsec-fixture', body, hashlib.sha256).hexdigest()

    def construct_event(self, body, signature, secret):
        self.record('Webhook.construct_event', {'body_length': len(body), 'secret': secret})
        if secret != 'whsec-fixture' or not hmac.compare_digest(self.signature(body), signature):
            raise ValueError('Fixture signature mismatch')
        return json.loads(body)


class FeaturePlansTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='kaika-feature-fixture-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'fixture.db'
        self.connections = []
        self.addCleanup(self.close_connections)
        self.env = patch.dict(os.environ, {
            'FEATURE_PLANS_ENABLED': '1', 'FEATURE_STRIPE_SECRET_KEY': 'sk_test_fixture',
            'FEATURE_STRIPE_WEBHOOK_SECRET': 'whsec-fixture',
            'FEATURE_BILLING_ORIGIN': 'https://fixture.invalid',
            'FEATURE_STRIPE_PORTAL_CONFIG_ID': 'bpc_fixture',
            'FEATURE_STRIPE_CHANGE_PORTAL_CONFIG_ID': '',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        with self.db() as conn:
            conn.execute('''CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT,
                display_name TEXT, role TEXT, permissions TEXT, stripe_subscription_id TEXT,
                subscription_status TEXT)''')
            for user_id, role, permissions in ((1, 'user', []), (2, 'user', []),
                    (3, 'admin', ['users']), (4, 'admin', ['merchandise']), (5, 'owner', [])):
                conn.execute('INSERT INTO users VALUES (?, ?, ?, ?, ?, NULL, ?)',
                             (user_id, f'user{user_id}', f'Fixture {user_id}', role, json.dumps(permissions), 'inactive'))
        self.app = Flask('feature-fixture')
        self.app.config.update(TESTING=True, SECRET_KEY='fixture-cookie-secret')
        self.app.jinja_loader = DictLoader({
            'feature_plans.html': '{{ feature_csrf_token() }}',
            'feature_plans_admin.html': '{{ feature_csrf_token() }}',
            'feature_plan_required.html': 'Plan required: {{ feature_label }}',
        })
        manager = LoginManager(self.app)

        @manager.user_loader
        def load_user(user_id):
            conn = self.db()
            try:
                row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
                return FixtureUser(row) if row else None
            finally:
                conn.close()

        self.app.add_url_rule('/fixture/sales-apply', endpoint='sales_agency_apply',
                              view_func=lambda: {'handler_called': True}, methods=['POST'])
        self.app.add_url_rule('/profile', endpoint='profile',
                              view_func=lambda: {'saved': True}, methods=['GET', 'POST'])
        self.app.add_url_rule('/admin/stripe', endpoint='legacy_stripe', view_func=lambda: 'legacy')
        self.app.add_url_rule('/api/stripe/batch-update', endpoint='legacy_stripe_api',
                              view_func=lambda: 'legacy', methods=['POST'])
        self.app.add_url_rule('/', endpoint='index', view_func=lambda: 'inventory')
        self.app.add_url_rule('/inventory/new', endpoint='self_inventory_new', view_func=lambda: 'new inventory')
        self.stripe = FakeStripe()
        self.runtime = SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=self.db,
                                       STRIPE_SECRET_KEY='', stripe=self.stripe)
        self.service = register_feature_plans(self.runtime)
        self.client = self.app.test_client()
        self.login()

    def close_connections(self):
        for conn in self.connections:
            conn.close()

    def db(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def login(self, user_id=1):
        with self.client.session_transaction() as session:
            session.clear()
            session['_user_id'] = str(user_id)
            session['_fresh'] = True

    def token(self):
        response = self.client.get('/plans')
        self.assertEqual(response.status_code, 200)
        return response.get_data(as_text=True)

    def post(self, url, data=None, *, native=False, csrf=True):
        fields = dict(data or {})
        if csrf:
            fields['csrf_token'] = self.token()
        return self.client.post(url, data=fields,
            headers={'User-Agent': 'KaikaApp/0.2'} if native else {})

    def configure(self, code='normal', price_id='price_normal', amount=2980, selling=1):
        with self.service.db(write=True) as cur:
            cur.execute('UPDATE feature_plan_catalog SET stripe_price_id=?,monthly_amount=?,selling=? WHERE code=?',
                        (price_id, amount, selling, code))
            cur.execute('INSERT OR IGNORE INTO feature_plan_prices VALUES (?,?,?)', (price_id, code, amount))
        self.stripe.prices[price_id] = {'id': price_id, 'active': True, 'currency': 'jpy',
            'product': 'prod_feature',
            'unit_amount': amount, 'tax_behavior': 'inclusive',
            'recurring': {'interval': 'month', 'interval_count': 1, 'usage_type': 'licensed'}}

    def account(self, user_id=1):
        with self.service.db(write=True) as cur:
            cur.execute('INSERT OR REPLACE INTO feature_billing_accounts (user_id,customer_id) VALUES (?,?)',
                        (user_id, f'cus_feature_{user_id}'))

    def subscription(self, subscription_id='sub_feature', user_id=1, price_id='price_normal', **changes):
        sub = {'id': subscription_id, 'customer': f'cus_feature_{user_id}',
               'metadata': {'kaika_feature_billing': '1'}, 'status': 'active',
               'current_period_end': int(time.time()) + 86400,
               'cancel_at_period_end': False, 'latest_invoice': {'id': 'in_fixture', 'status': 'paid'},
               'items': {'data': [{'id': 'si_fixture', 'quantity': 1,
                                  'price': deepcopy(self.stripe.prices[price_id])}]}}
        sub.update(changes)
        self.stripe.subscriptions[subscription_id] = sub
        return sub

    def event(self, event_id='evt_fixture', event_type='customer.subscription.updated',
              user_id=1, subscription_id='sub_feature', **object_changes):
        obj = {'id': subscription_id, 'customer': f'cus_feature_{user_id}', 'subscription': subscription_id}
        obj.update(object_changes)
        return {'id': event_id, 'type': event_type, 'data': {'object': obj}}

    def webhook(self, event=None, *, signature=True, prefix=b'', lengthless=False):
        body = prefix + json.dumps(event or self.event()).encode()
        headers = {'Stripe-Signature': self.stripe.signature(body)} if signature else {}
        if lengthless:
            return self.client.open('/billing/webhook', method='POST', headers=headers,
                content_type='application/json', environ_overrides={
                    'wsgi.input': io.BytesIO(body), 'wsgi.input_terminated': True, 'CONTENT_LENGTH': ''})
        return self.client.post('/billing/webhook', data=body, headers=headers, content_type='application/json')

    def state(self):
        response = self.client.get('/api/plans/me')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        return response.get_json()

    def assert_no_checkout(self):
        self.assertFalse(any(name in ('Customer.create', 'checkout.Session.create') for name, _ in self.stripe.calls))

    def test_disabled_registration_never_touches_database_or_payment_api(self):
        app = Flask('disabled-feature-fixture')
        runtime = SimpleNamespace(app=app, DATABASE_URL=None,
                                  get_db=lambda: self.fail('Disabled registration opened database'))
        with patch.dict(os.environ, {'FEATURE_PLANS_ENABLED': '0'}):
            service = register_feature_plans(runtime)
        self.assertFalse(service.enabled)
        self.assertIs(register_feature_plans(runtime), service)
        self.assertEqual(app.test_client().get('/plans').status_code, 404)
        user = SimpleNamespace(is_authenticated=True, is_admin=lambda: False)
        self.assertFalse(service.has_feature('inventory_manage', user))
        self.assertTrue(service.has_feature('dealer_sales', user))
        self.assertFalse(service.has_feature('unknown', user))

    def test_anonymous_and_admin_permissions(self):
        with self.client.session_transaction() as session:
            session.clear()
        for path in ('/plans', '/api/plans/me', '/admin/feature-plans'):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post('/billing/checkout').status_code, 401)
        for user_id, expected in ((1, 403), (4, 403), (3, 200), (5, 200)):
            with self.subTest(user_id=user_id):
                self.login(user_id)
                self.assertEqual(self.client.get('/admin/feature-plans').status_code, expected)

    def test_new_user_and_cross_user_plan_isolation(self):
        self.assertEqual(self.state()['features'], [])
        with self.service.db(write=True) as cur:
            cur.execute('INSERT INTO feature_manual_grants VALUES (?,?,?)', (2, 'business', int(time.time()) + 86400))
        self.assertEqual(self.state()['features'], [])
        self.login(2)
        self.assertEqual(self.state()['features'], ['inventory_manage', 'dealer_sales'])

    def test_csrf_missing_wrong_other_session_and_valid_header(self):
        self.configure()
        for path in ('/billing/checkout', '/billing/portal', '/billing/change-plan'):
            self.assertEqual(self.client.post(path, data={'plan_code': 'normal'}).status_code, 400)
            self.token()
            self.assertEqual(self.client.post(path, data={'csrf_token': 'wrong'}).status_code, 400)
        token = self.token()
        self.login(2)
        self.assertEqual(self.client.post('/billing/checkout', data={'plan_code': 'normal', 'csrf_token': token}).status_code, 400)
        self.assert_no_checkout()
        response = self.client.post('/billing/checkout', data={'plan_code': 'normal'}, headers={'X-CSRF-Token': self.token()})
        self.assertEqual(response.status_code, 303)

    def test_native_checkout_portal_and_admin_are_denied(self):
        self.configure()
        for path in ('/billing/checkout', '/billing/portal', '/billing/change-plan'):
            self.assertEqual(self.post(path, {'plan_code': 'normal'}, native=True).status_code, 403)
        self.login(3)
        self.assertEqual(self.client.get('/admin/feature-plans', headers={'User-Agent': 'KaikaApp/0.2'}).status_code, 403)
        self.assertEqual(self.post('/admin/feature-plans', {'action': 'catalog'}, native=True).status_code, 403)
        self.assert_no_checkout()

    def test_native_legacy_plan_changes_are_blocked_but_ordinary_profile_remains(self):
        headers = {'User-Agent': 'KaikaApp/0.2'}
        self.assertEqual(self.client.get('/admin/stripe', headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/stripe/batch-update', headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/profile', data={'requested_monthly_plan': 'monthly_plan_50'}, headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/profile', data={'display_name': 'Fixture'}, headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/profile', headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/profile', data={'requested_monthly_plan': 'monthly_plan_50'}).status_code, 200)

    def test_admin_catalog_csrf_validation_price_history_and_audit(self):
        self.login(3)
        form = {'action': 'catalog', 'plan_code': 'normal', 'name': 'ノーマル',
                'monthly_amount': '2980', 'stripe_price_id': 'price_first', 'selling': '1'}
        self.assertEqual(self.post('/admin/feature-plans', form, csrf=False).status_code, 400)
        for changed in ({'monthly_amount': '49'}, {'monthly_amount': '２９８０'},
                        {'stripe_price_id': 'price_bad/slash'}, {'name': ''}, {'plan_code': 'owner'}):
            self.assertEqual(self.post('/admin/feature-plans', dict(form, **changed)).status_code, 400)
        self.assertEqual(self.post('/admin/feature-plans', form).status_code, 303)
        self.assertEqual(self.post('/admin/feature-plans', dict(form, stripe_price_id='price_second', monthly_amount='3980')).status_code, 303)
        self.assertEqual(self.post('/admin/feature-plans', dict(form, plan_code='business')).status_code, 409)
        self.assertEqual(self.post('/admin/feature-plans', dict(form, monthly_amount='5980')).status_code, 409)
        with self.service.db() as cur:
            cur.execute('SELECT price_id FROM feature_plan_prices ORDER BY price_id')
            self.assertEqual([r['price_id'] for r in self.service.rows(cur)], ['price_first', 'price_second'])
            cur.execute('SELECT actor_id,action FROM feature_plan_audit')
            self.assertEqual([(r['actor_id'], r['action']) for r in self.service.rows(cur)], [(3, 'catalog'), (3, 'catalog')])

    def test_manual_grant_expiry_revocation_and_audit(self):
        self.login(3)
        form = {'action': 'grant', 'user_id': '1', 'plan_code': 'business', 'days': '7', 'reason': 'Fixture approved trial'}
        for changed, status in (({'days': '0'}, 400), ({'days': '91'}, 400), ({'reason': ''}, 400), ({'user_id': '999'}, 404)):
            self.assertEqual(self.post('/admin/feature-plans', dict(form, **changed)).status_code, status)
        self.assertEqual(self.post('/admin/feature-plans', form).status_code, 303)
        self.login(1)
        self.assertEqual(self.state()['plan_code'], 'business')
        self.assertEqual(self.post('/fixture/sales-apply').status_code, 200)
        with self.service.db(write=True) as cur:
            cur.execute('UPDATE feature_manual_grants SET expires_at=?', (int(time.time()) - 1,))
        self.assertEqual(self.state()['features'], [])
        self.assertEqual(self.client.post('/fixture/sales-apply', json={}).status_code, 403)
        self.login(3)
        self.assertEqual(self.post('/admin/feature-plans', dict(form, action='revoke')).status_code, 303)
        with self.service.db() as cur:
            cur.execute('SELECT * FROM feature_manual_grants')
            self.assertEqual(self.service.rows(cur), [])
            cur.execute('SELECT action FROM feature_plan_audit')
            self.assertEqual([r['action'] for r in self.service.rows(cur)], ['grant', 'revoke'])

    def test_dealer_application_requires_plan_and_csrf(self):
        self.assertEqual(self.post('/fixture/sales-apply').status_code, 403)
        with self.service.db(write=True) as cur:
            cur.execute('INSERT INTO feature_manual_grants VALUES (?,?,?)',
                        (1, 'business', int(time.time()) + 86400))
        self.assertEqual(self.client.post('/fixture/sales-apply').status_code, 400)
        self.assertEqual(self.client.post('/fixture/sales-apply', data={'csrf_token': 'incorrect'}).status_code, 400)
        self.assertEqual(self.post('/fixture/sales-apply').get_json(), {'handler_called': True})
        self.login(3)
        self.assertEqual(self.client.post('/fixture/sales-apply').status_code, 400)
        self.assertEqual(self.post('/fixture/sales-apply').status_code, 200)

    def test_checkout_price_amount_currency_period_tax_and_quantity_mode_validation(self):
        self.configure()
        original = deepcopy(self.stripe.prices['price_normal'])
        for changed in ({'active': False}, {'currency': 'usd'}, {'unit_amount': 1},
                {'tax_behavior': 'exclusive'}, {'recurring': {'interval': 'year', 'interval_count': 1, 'usage_type': 'licensed'}},
                {'recurring': {'interval': 'month', 'interval_count': 2, 'usage_type': 'licensed'}},
                {'recurring': {'interval': 'month', 'interval_count': 1, 'usage_type': 'metered'}}):
            with self.subTest(changed=changed):
                self.stripe.prices['price_normal'] = dict(original, **changed)
                self.assertEqual(self.post('/billing/checkout', {'plan_code': 'normal'}).status_code, 409)
                self.assert_no_checkout()
        self.stripe.prices['price_normal'] = original
        self.assertEqual(self.post('/billing/checkout', {'plan_code': 'unknown'}).status_code, 409)
        self.configure(selling=0)
        self.assertEqual(self.post('/billing/checkout', {'plan_code': 'normal'}).status_code, 409)

    def test_legacy_subscription_id_blocks_second_billing_even_when_status_is_inactive(self):
        self.configure()
        for status in ('active', 'trialing', 'past_due', 'canceling', 'unpaid', 'incomplete', 'inactive', 'canceled', None):
            with self.subTest(status=status):
                with self.db() as conn:
                    conn.execute('UPDATE users SET stripe_subscription_id=?,subscription_status=? WHERE id=1', ('sub_legacy', status))
                self.assertEqual(self.post('/billing/checkout', {'plan_code': 'normal'}).status_code, 409)
                self.assert_no_checkout()

    def test_checkout_remote_subscription_blocks_duplicates_and_unfinished_pages(self):
        self.configure()
        self.account()
        for remote in ({'data': [], 'has_more': True}, *({'data': [{'status': status}], 'has_more': False}
                    for status in ('active', 'past_due', 'trialing', 'incomplete', 'unpaid', 'paused'))):
            self.stripe.subscription_list = remote
            self.assertEqual(self.post('/billing/checkout', {'plan_code': 'normal'}).status_code, 409)
        self.assert_no_checkout()

    def test_checkout_reuses_session_and_success_return_does_not_grant(self):
        self.configure()
        self.configure('business', 'price_business', 5980)
        self.assertEqual(self.post('/billing/checkout', {'plan_code': 'normal', 'user_id': '2', 'price_id': 'price_business'}).status_code, 303)
        first = next(args for method, args in self.stripe.calls if method == 'checkout.Session.create')
        self.assertEqual(first['customer'], 'cus_feature_1')
        self.assertEqual(first['client_reference_id'], '1')
        self.assertEqual(first['line_items'], [{'price': 'price_normal', 'quantity': 1}])
        self.assertEqual(first['subscription_data']['metadata']['kaika_feature_billing'], '1')
        self.assertEqual(first['success_url'], 'https://fixture.invalid/plans?result=pending')
        self.assertEqual(self.post('/billing/checkout', {'plan_code': 'normal'}).status_code, 303)
        self.assertEqual(sum(method == 'checkout.Session.create' for method, _ in self.stripe.calls), 1)
        self.assertEqual(self.post('/billing/checkout', {'plan_code': 'business'}).status_code, 409)
        self.assertEqual(self.client.get('/plans?result=pending&user_id=2&session_id=cs_paid').status_code, 200)
        self.assertEqual(self.state()['features'], [])

    def test_checkout_retry_reuses_persisted_idempotency_key(self):
        self.configure()
        self.stripe.fail_checkout_once = True
        with self.assertLogs(self.app.logger.name, level='ERROR'):
            response = self.post('/billing/checkout', {'plan_code': 'normal'})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('Fixture temporary checkout failure', response.get_data(as_text=True))
        with self.service.db() as cur:
            cur.execute('SELECT pending_key,pending_session FROM feature_billing_accounts WHERE user_id=1')
            persisted = self.service.one(cur)
        self.assertTrue(persisted['pending_key'])
        self.assertIsNone(persisted['pending_session'])
        self.assertEqual(self.post('/billing/checkout', {'plan_code': 'normal'}).status_code, 303)
        keys = [args['idempotency_key'] for name, args in self.stripe.calls if name == 'checkout.Session.create']
        self.assertEqual(keys, [persisted['pending_key'], persisted['pending_key']])

    def test_checkout_configuration_must_have_signature_secret_and_https_origin(self):
        self.configure()
        for changed in ({'FEATURE_STRIPE_WEBHOOK_SECRET': ''},
                        {'FEATURE_STRIPE_PORTAL_CONFIG_ID': ''},
                        {'FEATURE_BILLING_ORIGIN': 'http://fixture.invalid'},
                        {'FEATURE_BILLING_ORIGIN': 'https://fixture.invalid/path'},
                        {'FEATURE_BILLING_ORIGIN': 'https://name:password@fixture.invalid'}):
            with patch.dict(os.environ, changed):
                self.assertEqual(self.post('/billing/checkout', {'plan_code': 'normal'}).status_code, 503)
        self.assert_no_checkout()

    def test_portal_uses_current_users_account_and_requires_configuration(self):
        self.account()
        self.account(2)
        self.assertEqual(self.post('/billing/portal', {'customer_id': 'cus_feature_2'}).status_code, 303)
        arguments = next(args for name, args in self.stripe.calls if name == 'billing_portal.Session.create')
        self.assertEqual(arguments['customer'], 'cus_feature_1')
        self.assertEqual(arguments['configuration'], 'bpc_fixture')
        with patch.dict(os.environ, {'FEATURE_STRIPE_PORTAL_CONFIG_ID': ''}):
            self.assertEqual(self.post('/billing/portal').status_code, 503)

    def test_portal_rejects_disabled_or_unsafe_provider_configuration(self):
        self.account()
        original = deepcopy(self.stripe.portal_config)
        for case in ('inactive', 'can_change_plan', 'cannot_cancel', 'cancel_immediately', 'missing_settings'):
            with self.subTest(case=case):
                self.stripe.portal_config = deepcopy(original)
                config = self.stripe.portal_config
                if case == 'inactive': config['active'] = False
                elif case == 'can_change_plan': config['features']['subscription_update']['enabled'] = True
                elif case == 'cannot_cancel': config['features']['subscription_cancel']['enabled'] = False
                elif case == 'cancel_immediately': config['features']['subscription_cancel']['mode'] = 'immediately'
                elif case == 'missing_settings': config['features'] = {}
                self.assertEqual(self.post('/billing/portal').status_code, 503)
        self.assertFalse(any(name == 'billing_portal.Session.create' for name, _ in self.stripe.calls))

    def test_checkout_requires_working_cancellation_before_starting_payment(self):
        self.configure()
        self.stripe.portal_config['features']['subscription_cancel']['enabled'] = False
        self.assertEqual(self.post('/billing/checkout', {'plan_code': 'normal'}).status_code, 503)
        self.assert_no_checkout()

    def test_real_plan_templates_render_web_native_entitled_and_admin_states(self):
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({'base.html': '<!doctype html><html><head><title>{% block title %}{% endblock %}</title></head><body>{% block content %}{% endblock %}</body></html>'}),
            FileSystemLoader(str(Path(__file__).resolve().parents[1] / 'templates')),
        ])
        self.app.jinja_env.undefined = StrictUndefined
        self.app.jinja_env.cache.clear()
        self.configure()
        response = self.client.get('/plans')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('action="/billing/checkout"', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('税込／月', html)
        native_headers = {'User-Agent': 'KaikaApp/0.2'}
        response = self.client.get('/plans', headers=native_headers)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('利用できる機能', html)
        self.assertNotIn('/billing/', html)
        self.assertNotIn('税込／月', html)
        self.account()
        self.subscription(cancel_at_period_end=True)
        self.assertEqual(self.webhook().status_code, 200)
        response = self.client.get('/plans')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('action="/billing/portal"', html)
        self.assertIn('href="/inventory/new"', html)
        self.assertIn('解約予約済み', html)
        self.assertNotIn('action="/billing/checkout"', html)
        response = self.client.get('/plans', headers=native_headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('/billing/', response.get_data(as_text=True))
        response = self.client.post('/fixture/sales-apply')
        self.assertEqual(response.status_code, 403)
        self.assertIn('この機能はご契約の対象外です', response.get_data(as_text=True))
        self.login(3)
        response = self.client.get('/admin/feature-plans')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('機能プラン管理', html)
        self.assertIn('name="csrf_token"', html)
        self.assertIn('name="stripe_price_id"', html)
        self.assertIn('操作履歴', html)

    def test_webhook_requires_signature_and_configured_secret(self):
        self.assertEqual(self.webhook(signature=False).status_code, 400)
        body = json.dumps(self.event()).encode()
        response = self.client.post('/billing/webhook', data=body + b' ',
            headers={'Stripe-Signature': self.stripe.signature(body)})
        self.assertEqual(response.status_code, 400)
        with patch.dict(os.environ, {'FEATURE_STRIPE_WEBHOOK_SECRET': ''}):
            self.assertEqual(self.webhook().status_code, 503)
        self.assertFalse(any(name == 'Subscription.retrieve' for name, _ in self.stripe.calls))

    def test_contract_page_dates_change_reservation_and_post_cancellation_access(self):
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({'base.html': '{% block content %}{% endblock %}'}),
            FileSystemLoader(str(Path(__file__).resolve().parents[1] / 'templates')),
        ])
        self.app.jinja_env.undefined = StrictUndefined; self.app.jinja_env.cache.clear()
        future = int(time.time()) + 86400
        with self.service.db(write=True) as cur:
            cur.execute('INSERT INTO feature_subscriptions VALUES (?,?,?,?,?,?,?,?)',
                ('stripe', 'private-contract', 1, 'business', 'active', future, 0, int(time.time())))
            cur.execute('INSERT INTO feature_plan_changes VALUES (?,?,?,?)',
                ('private-contract', 'normal', future, int(time.time())))
        html = self.client.get('/plans', headers={'User-Agent': 'KaikaApp/0.2'}).get_data(as_text=True)
        for label in ('Webで契約', '次回請求予定', '日本時間', 'ノーマルへ変更する予約', '自己在庫の新規登録・編集', 'プラン終了だけを理由に停止しません'):
            self.assertIn(label, html)
        self.assertNotIn('private-contract', html)
        self.assertNotIn('action="/billing/checkout"', html)
        self.assertNotIn('action="/billing/portal"', html)
        self.assertEqual(self.stripe.calls, [])

    def test_legacy_contract_page_never_offers_second_checkout(self):
        self.app.jinja_loader = ChoiceLoader([
            DictLoader({'base.html': '{% block content %}{% endblock %}'}),
            FileSystemLoader(str(Path(__file__).resolve().parents[1] / 'templates')),
        ])
        self.app.jinja_env.cache.clear(); self.configure()
        with self.service.db(write=True) as cur:
            cur.execute('UPDATE users SET stripe_subscription_id=? WHERE id=1', ('private-legacy',))
        html = self.client.get('/plans').get_data(as_text=True)
        self.assertIn('以前からの在庫料金', html)
        self.assertNotIn('action="/billing/checkout"', html)

    def test_webhook_rejects_oversize_body_with_or_without_content_length(self):
        prefix = b' ' * (1024 * 1024 + 1)
        for lengthless in (False, True):
            with self.subTest(lengthless=lengthless):
                self.assertEqual(self.webhook(prefix=prefix, lengthless=lengthless).status_code, 413)

    def test_webhook_paid_state_duplicate_out_of_order_and_recovery(self):
        self.configure()
        self.account()
        sub = self.subscription()
        self.assertEqual(self.webhook(self.event('evt_new')).status_code, 200)
        self.assertEqual(self.state()['features'], ['inventory_manage'])
        self.assertEqual(self.webhook(self.event('evt_new')).status_code, 200)
        self.assertEqual(sum(name == 'Subscription.retrieve' for name, _ in self.stripe.calls), 1)
        # An older payload says past_due; latest provider truth still says paid.
        self.assertEqual(self.webhook(self.event('evt_old', status='past_due')).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        sub['status'] = 'past_due'
        sub['latest_invoice']['status'] = 'open'
        self.assertEqual(self.webhook(self.event('evt_failed', 'invoice.payment_failed')).status_code, 200)
        self.assertEqual(self.state()['features'], [])
        sub['status'] = 'active'
        sub['latest_invoice']['status'] = 'paid'
        self.assertEqual(self.webhook(self.event('evt_recovered', 'invoice.paid')).status_code, 200)
        self.assertEqual(self.state()['features'], ['inventory_manage'])

    def test_webhook_temporary_failure_is_retryable_and_not_marked_processed(self):
        self.configure()
        self.account()
        self.subscription()
        self.stripe.fail_subscription_once = True
        with self.assertLogs(self.app.logger.name, level='ERROR'):
            response = self.webhook()
        self.assertEqual(response.status_code, 503)
        with self.service.db() as cur:
            cur.execute('SELECT * FROM feature_billing_events')
            self.assertEqual(self.service.rows(cur), [])
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')

    def test_webhook_unknown_customer_mismatch_and_legacy_metadata_cannot_grant(self):
        self.configure()
        self.account()
        self.subscription(user_id=2)
        self.assertEqual(self.webhook(self.event(user_id=2)).status_code, 200)
        self.assertFalse(any(name == 'Subscription.retrieve' for name, _ in self.stripe.calls))
        with self.assertLogs(self.app.logger.name, level='ERROR'):
            self.assertEqual(self.webhook().status_code, 503)
        self.assertEqual(self.state()['features'], [])
        self.subscription(metadata={'legacy': '1'})
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.state()['features'], [])

    def test_webhook_subscription_bound_to_other_user_cannot_be_reassigned(self):
        self.configure()
        self.account()
        self.subscription()
        with self.service.db(write=True) as cur:
            cur.execute('''INSERT INTO feature_subscriptions
                (provider,subscription_id,user_id,plan_code,status,period_end,verified_at)
                VALUES ('stripe','sub_feature',2,'business','canceled',?,?)''',
                (int(time.time()) + 86400, int(time.time())))
        with self.assertLogs(self.app.logger.name, level='ERROR'):
            self.assertEqual(self.webhook().status_code, 503)
        self.assertEqual(self.state()['features'], [])
        with self.service.db() as cur:
            cur.execute("SELECT user_id,plan_code,status FROM feature_subscriptions WHERE subscription_id='sub_feature'")
            self.assertEqual(self.service.one(cur), {'user_id': 2, 'plan_code': 'business', 'status': 'canceled'})
            cur.execute('SELECT * FROM feature_billing_events')
            self.assertEqual(self.service.rows(cur), [])

    def test_webhook_lifecycle_cancellation_expiry_and_unpaid_never_unlock(self):
        self.configure()
        self.account()
        changes = [
            {'status': status} for status in ('trialing', 'incomplete', 'incomplete_expired', 'canceled', 'unpaid', 'paused')
        ] + [{'latest_invoice': {'status': 'open'}}, {'latest_invoice': None},
             {'current_period_end': int(time.time()) - 1}, {'pause_collection': {'behavior': 'void'}}]
        for index, changed in enumerate(changes):
            with self.subTest(changed=changed):
                self.subscription(**changed)
                self.assertEqual(self.webhook(self.event(f'evt_life_{index}')).status_code, 200)
                self.assertEqual(self.state()['features'], [])
        self.subscription(cancel_at_period_end=True)
        self.assertEqual(self.webhook(self.event('evt_cancel_scheduled')).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')

    def test_webhook_old_price_mapping_survives_catalog_replacement(self):
        self.configure()
        self.configure('normal', 'price_replacement', 3980)
        self.account()
        self.subscription(price_id='price_normal')
        self.assertEqual(self.webhook().status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')

    def test_webhook_unknown_price_currency_amount_quantity_and_multiple_items_do_not_grant(self):
        self.configure()
        self.account()
        for index, mutation in enumerate(('unknown', 'currency', 'amount', 'quantity', 'multiple')):
            sub = self.subscription()
            item = sub['items']['data'][0]
            if mutation == 'unknown': item['price']['id'] = 'price_unmapped'
            elif mutation == 'currency': item['price']['currency'] = 'usd'
            elif mutation == 'amount': item['price']['unit_amount'] = 1
            elif mutation == 'quantity': item['quantity'] = 2
            elif mutation == 'multiple': sub['items']['data'].append(deepcopy(item))
            self.assertEqual(self.webhook(self.event(f'evt_invalid_{index}')).status_code, 200)
            self.assertEqual(self.state()['features'], [])

    def test_webhook_new_invoice_parent_and_item_period_end_fields(self):
        self.configure('business', 'price_business', 5980)
        self.account()
        sub = self.subscription(price_id='price_business', current_period_end=0)
        sub['items']['data'][0]['current_period_end'] = int(time.time()) + 86400
        event = self.event('evt_new_fields', 'invoice.paid', subscription=None,
                           parent={'subscription_details': {'subscription': 'sub_feature'}})
        self.assertEqual(self.webhook(event).status_code, 200)
        self.assertEqual(self.state()['features'], ['inventory_manage', 'dealer_sales'])

    def change_fixture(self, source='normal'):
        os.environ['FEATURE_STRIPE_CHANGE_PORTAL_CONFIG_ID'] = 'bpc_change'
        self.configure()
        self.configure('business', 'price_business', 5980)
        self.account()
        sub = self.subscription(price_id='price_' + source)
        self.stripe.subscription_list = {'data': [sub], 'has_more': False}
        self.assertEqual(self.webhook().status_code, 200)
        return sub

    def test_change_plan_only_opens_confirmation_and_never_creates_second_subscription(self):
        self.change_fixture()
        for _ in range(2):
            response = self.post('/billing/change-plan', {'plan_code': 'business', 'subscription_id': 'sub_someone_else'})
            self.assertEqual(response.status_code, 303)
        name, args = next((name, args) for name, args in self.stripe.calls if name == 'billing_portal.Session.create')
        self.assertEqual(args['configuration'], 'bpc_change')
        self.assertEqual(args['customer'], 'cus_feature_1')
        self.assertEqual(args['flow_data']['subscription_update_confirm'], {
            'subscription': 'sub_feature', 'items': [{'id': 'si_fixture', 'price': 'price_business', 'quantity': 1}]})
        self.assertEqual(self.state()['plan_code'], 'normal')
        self.assertEqual(self.client.get('/plans?result=pending').status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        self.assert_no_checkout()

    def test_change_plan_missing_configuration_and_wrong_plan_cannot_open_portal(self):
        self.change_fixture()
        with patch.dict(os.environ, {'FEATURE_STRIPE_CHANGE_PORTAL_CONFIG_ID': ''}):
            self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 503)
        for plan, expected in (('normal', 409), ('owner', 400)):
            self.assertEqual(self.post('/billing/change-plan', {'plan_code': plan}).status_code, expected)
        self.assertFalse(any(n == 'billing_portal.Session.create' for n, _ in self.stripe.calls))

    def test_change_plan_rejects_unsafe_portal_settings(self):
        self.change_fixture()
        original = deepcopy(self.stripe.change_config)
        cases = ['inactive', 'disabled', 'quantity', 'discount', 'no_immediate_invoice', 'reset_period',
                 'immediate_downgrade', 'wrong_schedule', 'foreign_price', 'foreign_product', 'multiple_products', 'quantity_override', 'immediate_cancel']
        for case in cases:
            with self.subTest(case=case):
                self.stripe.change_config = deepcopy(original)
                cfg = self.stripe.change_config
                update = cfg['features']['subscription_update']
                if case == 'inactive': cfg['active'] = False
                elif case == 'disabled': update['enabled'] = False
                elif case == 'quantity': update['default_allowed_updates'].append('quantity')
                elif case == 'discount': update['default_allowed_updates'].append('promotion_code')
                elif case == 'no_immediate_invoice': update['proration_behavior'] = 'create_prorations'
                elif case == 'reset_period': update['billing_cycle_anchor'] = 'now'
                elif case == 'immediate_downgrade': update['schedule_at_period_end'] = {}
                elif case == 'wrong_schedule': update['schedule_at_period_end']['conditions'] = [{'type': 'shortening_interval'}]
                elif case == 'foreign_price': update['products'][0]['prices'].append('price_unknown')
                elif case == 'foreign_product': update['products'][0]['product'] = 'prod_other'
                elif case == 'multiple_products': update['products'].append(deepcopy(update['products'][0]))
                elif case == 'quantity_override': update['products'][0]['adjustable_quantity'] = {'enabled': True}
                elif case == 'immediate_cancel': cfg['features']['subscription_cancel']['mode'] = 'immediately'
                self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 503)
        self.assertFalse(any(n == 'billing_portal.Session.create' for n, _ in self.stripe.calls))

    def test_change_plan_rejects_unpaid_cancelled_pending_foreign_or_multiple_contracts(self):
        sub = self.change_fixture()
        original = deepcopy(sub)
        changes = [{'customer': 'cus_feature_2'}, {'metadata': {}}, {'status': 'past_due'},
                   {'cancel_at_period_end': True}, {'cancel_at': int(time.time()) + 3600},
                   {'pending_update': {'expires_at': int(time.time()) + 3600}}, {'schedule': 'sched_pending'},
                   {'pause_collection': {'behavior': 'void'}}, {'latest_invoice': {'status': 'open'}},
                   {'current_period_end': int(time.time()) - 1}, {'items': {'data': []}}]
        for change in changes:
            with self.subTest(change=change):
                self.stripe.subscriptions['sub_feature'] = dict(deepcopy(original), **change)
                self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 409)
        self.stripe.subscriptions['sub_feature'] = original
        for remote in ({'data': [original, dict(original, id='sub_extra')], 'has_more': False}, {'data': [original], 'has_more': True}):
            self.stripe.subscription_list = remote
            self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 409)
        self.assertFalse(any(n == 'billing_portal.Session.create' for n, _ in self.stripe.calls))

    def test_change_plan_rejects_legacy_cross_user_and_cross_provider_contracts(self):
        self.change_fixture()
        with self.db() as conn:
            conn.execute("UPDATE users SET stripe_subscription_id='sub_legacy' WHERE id=1")
        self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 409)
        with self.db() as conn:
            conn.execute('UPDATE users SET stripe_subscription_id=NULL WHERE id=1')
        with self.service.db(write=True) as cur:
            cur.execute("UPDATE feature_subscriptions SET user_id=2 WHERE subscription_id='sub_feature'")
        self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 409)
        with self.service.db(write=True) as cur:
            cur.execute("UPDATE feature_subscriptions SET user_id=1,provider='apple' WHERE subscription_id='sub_feature'")
        self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 409)
        self.assertEqual(self.post('/billing/checkout', {'plan_code': 'business'}).status_code, 409)

    def test_change_plan_rejects_price_shape_and_historical_direction_mismatch(self):
        sub = self.change_fixture()
        self.stripe.prices['price_business']['product'] = 'prod_other'
        self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 503)
        self.stripe.prices['price_business']['product'] = 'prod_feature'
        self.stripe.prices['price_business']['unit_amount'] = 1
        self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 409)
        self.stripe.prices['price_business']['unit_amount'] = 5980
        sub['items']['data'][0]['quantity'] = 2
        self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 409)
        self.configure('normal', 'price_oldhigh', 6980, selling=0)
        self.configure()
        sub['items']['data'][0] = {'id': 'si_fixture', 'quantity': 1, 'price': self.stripe.prices['price_oldhigh']}
        self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'business'}).status_code, 409)

    def test_pending_upgrade_retains_only_previously_paid_plan_then_unlocks_on_payment(self):
        sub = self.change_fixture()
        paid_end = sub['current_period_end']
        sub['pending_update'] = {'expires_at': int(time.time()) + 3600}
        sub['latest_invoice']['status'] = 'open'
        sub['current_period_end'] += 86400
        self.assertEqual(self.webhook(self.event('evt_pending')).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        with self.service.db() as cur:
            cur.execute("SELECT period_end FROM feature_subscriptions WHERE subscription_id='sub_feature'")
            self.assertEqual(self.service.one(cur)['period_end'], paid_end)
        sub['pending_update'] = None
        sub['items']['data'][0]['price'] = deepcopy(self.stripe.prices['price_business'])
        sub['latest_invoice']['status'] = 'paid'
        self.assertEqual(self.webhook(self.event('evt_upgrade_paid')).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'business')

    def test_pending_upgrade_cannot_create_first_entitlement_or_unlock_new_price(self):
        sub = self.change_fixture()
        sub['pending_update'] = {'expires_at': int(time.time()) + 3600}
        sub['latest_invoice']['status'] = 'open'
        sub['items']['data'][0]['price'] = deepcopy(self.stripe.prices['price_business'])
        self.assertEqual(self.webhook(self.event('evt_unpaid_new')).status_code, 200)
        self.assertEqual(self.state()['features'], [])

    def test_downgrade_reservation_retains_business_and_only_paid_renewal_changes_plan(self):
        sub = self.change_fixture('business')
        self.assertEqual(self.post('/billing/change-plan', {'plan_code': 'normal'}).status_code, 303)
        sub['schedule'] = 'sched_change'
        self.stripe.schedules['sched_change'] = {'customer': 'cus_feature_1', 'subscription': 'sub_feature',
            'status': 'active', 'phases': [{'start_date': sub['current_period_end'], 'items': [{'price': 'price_normal', 'quantity': 1}]}]}
        self.assertEqual(self.webhook(self.event('evt_scheduled')).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'business')
        with self.app.test_request_context():
            with self.service.db() as cur:
                cur.execute('SELECT plan_code FROM feature_plan_changes')
                self.assertEqual(self.service.one(cur)['plan_code'], 'normal')
        sub['schedule'] = None
        sub['items']['data'][0]['price'] = deepcopy(self.stripe.prices['price_normal'])
        sub['latest_invoice']['status'] = 'open'
        self.assertEqual(self.webhook(self.event('evt_renew_unpaid')).status_code, 200)
        self.assertEqual(self.state()['features'], [])
        sub['latest_invoice']['status'] = 'paid'
        self.assertEqual(self.webhook(self.event('evt_renew_paid')).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        with self.service.db() as cur:
            cur.execute('SELECT * FROM feature_plan_changes')
            self.assertEqual(self.service.rows(cur), [])

    def test_removed_feature_metadata_revokes_existing_entitlement(self):
        sub = self.change_fixture()
        sub['metadata'] = {}
        self.assertEqual(self.webhook(self.event('evt_marker_removed')).status_code, 200)
        self.assertEqual(self.state()['features'], [])

    def test_refund_event_does_not_invent_plan_or_cancel_existing_subscription(self):
        self.change_fixture()
        self.assertEqual(self.webhook(self.event('evt_refund', 'charge.refunded', refunded=True)).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        # Refund policy and subscription cancellation are separate Stripe actions.
        # A verified cancellation notification does remove access.
        self.stripe.subscriptions['sub_feature']['status'] = 'canceled'
        self.assertEqual(self.webhook(self.event('evt_refund_cancel', 'customer.subscription.deleted')).status_code, 200)
        self.assertEqual(self.state()['features'], [])


if __name__ == '__main__':
    unittest.main()
