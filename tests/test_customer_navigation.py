"""Navigation security/availability tests without importing the business app or DB."""
from types import SimpleNamespace
import unittest

from flask import Flask, render_template_string
from flask_login import LoginManager, UserMixin
from jinja2 import DictLoader

from customer_navigation import STAGE_FLAGS, current_section, register_customer_navigation


class Customer(UserMixin):
    def __init__(self, identifier, role='user'):
        self.id, self.role = identifier, role
        self.edit_allowed = True

    def is_admin(self):
        return self.role == 'admin'

    def can_edit_merchandise(self):
        return self.edit_allowed


class Plans:
    enabled = True
    allowed = True
    fail = False

    def has_feature(self, feature):
        if self.fail:
            raise RuntimeError('private capability failure detail')
        return self.allowed and feature == 'inventory_manage'


DESTINATIONS = (
    'user_home', 'workspace_home.home', 'index', 'self_inventory_new',
    'inventory_registry.index', 'long_term_items', 'commerce.index', 'commerce.orders',
    'inventory_intake_new', 'inventory_intakes', 'commerce.listing_index',
    'sales_agency_my_requests', 'public_proxy_service_history', 'public_proxy_service',
    'finance_review.index', 'payment_review.index', 'feature_plans.plans',
    'user_analytics', 'reports', 'user_document_list', 'documents',
    'workspace_home.inbox', 'user_announcements_page', 'inquiry_list', 'inquiry_new',
    'workspace_home.settings', 'profile', 'account_lifecycle.page',
)


class CustomerNavigationTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='isolated-navigation-fixture',
                               ACCOUNT_LIFECYCLE_ENABLED=True)
        self.app.config.update({flag: True for flag in STAGE_FLAGS})
        self.app.jinja_loader = DictLoader({
            'customer_navigation/hub.html':
                '{{ {"section":section,"navigation":navigation,"available":customer_navigation_available,"current":customer_nav_current}|tojson }}',
            'customer_navigation/guide.html':
                '{{ {"sections":guide_sections,"inquiry":inquiry_url,"history":history_url,"preview":private_preview,"current":customer_nav_current}|tojson }}'
        })
        self.users = {str(user.id): user for user in (
            Customer(11), Customer(22), Customer(90, 'admin'), Customer(91, 'supplier'))}
        manager = LoginManager(self.app)
        manager.login_view = 'login'
        manager.user_loader(lambda identifier: self.users.get(identifier))
        self.app.add_url_rule('/login', 'login', lambda: 'Login')
        for endpoint in DESTINATIONS:
            self.app.add_url_rule('/dest/' + endpoint, endpoint, lambda: 'Existing page')
        self.app.add_url_rule('/finance/customers/<int:customer_id>', 'finance_review.customer',
                              lambda customer_id: str(customer_id))
        self.app.add_url_rule('/context', 'context_test', lambda: render_template_string(
            '{{ {"available":customer_navigation_available,"navigation":customer_nav_sections,"current":customer_nav_current}|tojson }}'))
        self.plans = Plans()
        self.app.extensions['kaika_feature_plans'] = self.plans
        self.runtime = SimpleNamespace(app=self.app, get_db=self.unexpected_db)
        register_customer_navigation(self.runtime)
        self.client = self.app.test_client()

    @staticmethod
    def unexpected_db(*args, **kwargs):
        raise AssertionError('Navigation must not connect to the business DB')

    def login(self, identifier=11):
        with self.client.session_transaction() as session:
            session['_user_id'] = str(identifier)
            session['_fresh'] = True

    def hub(self, key, query=''):
        response = self.client.get('/my/' + key + query)
        self.assertEqual(response.status_code, 200)
        return response.get_json(force=True)

    def card_urls(self, key):
        return {card['url'] for card in self.hub(key)['section']['cards']}

    def test_anonymous_and_noncustomer_cannot_open_customer_hubs(self):
        response = self.client.get('/my/products')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.startswith('/login?'))
        for identifier in (90, 91):
            self.login(identifier)
            self.assertEqual(self.client.get('/my/products').status_code, 403)
            context = self.client.get('/context').get_json(force=True)
            self.assertFalse(context['available'])
            self.assertEqual(context['navigation'], [])

    def test_six_entries_and_one_active_category(self):
        self.login()
        for key in ('products', 'requests', 'money', 'documents', 'support'):
            data = self.hub(key)
            self.assertEqual([entry['key'] for entry in data['navigation']],
                             ['home', 'products', 'requests', 'money', 'documents', 'support'])
            self.assertEqual([entry['key'] for entry in data['navigation'] if entry['active']], [key])
            self.assertEqual(data['current']['url'], '/my/' + key)
            self.assertTrue(data['available'])
        self.assertEqual(data['navigation'][0]['url'], '/dest/workspace_home.home')

    def test_navigation_is_get_only_and_does_not_accept_arbitrary_sections(self):
        self.login()
        for key in ('home', 'admin', 'unknown'):
            self.assertEqual(self.client.get('/my/' + key).status_code, 404)
        self.assertEqual(self.client.post('/my/products').status_code, 405)

    def test_disabled_dependency_hides_downstream_links(self):
        self.login()
        self.app.config['STAGE2_INVENTORY_ENABLED'] = False
        data = self.hub('products')
        self.assertEqual(data['navigation'][0]['url'], '/dest/user_home')
        product_urls = {card['url'] for card in data['section']['cards']}
        self.assertNotIn('/dest/inventory_registry.index', product_urls)
        self.assertNotIn('/dest/commerce.index', product_urls)
        self.assertNotIn('/dest/commerce.orders', product_urls)
        money_urls = self.card_urls('money')
        self.assertNotIn('/dest/finance_review.index', money_urls)
        self.assertNotIn('/finance/customers/11', money_urls)
        self.assertNotIn('/dest/payment_review.index', money_urls)
        self.assertNotIn('/dest/workspace_home.inbox', self.card_urls('support'))
        # The older plan-authorized registration remains available independently.
        self.assertIn('/dest/self_inventory_new', product_urls)

    def test_each_feature_flag_hides_only_its_dependent_destinations(self):
        self.login()
        self.app.config['STAGE5_PAYMENTS_ENABLED'] = False
        self.assertIn('/dest/commerce.index', self.card_urls('products'))
        self.assertIn('/dest/finance_review.index', self.card_urls('money'))
        self.assertNotIn('/dest/payment_review.index', self.card_urls('money'))
        self.assertNotIn('/dest/workspace_home.settings', self.card_urls('support'))
        self.app.config['ACCOUNT_LIFECYCLE_ENABLED'] = False
        self.assertNotIn('/dest/account_lifecycle.page', self.card_urls('support'))

    def test_missing_endpoints_do_not_produce_dead_links_or_url_build_errors(self):
        self.login()
        for endpoint in ('commerce.index', 'finance_review.customer', 'account_lifecycle.page',
                         'workspace_home.home', 'user_home'):
            del self.app.view_functions[endpoint]
        data = self.hub('products')
        self.assertEqual(data['navigation'][0]['url'], '/dest/index')
        self.assertNotIn('/dest/commerce.index', self.card_urls('products'))
        self.assertNotIn('/finance/customers/11', self.card_urls('money'))
        self.assertNotIn('/dest/account_lifecycle.page', self.card_urls('support'))

    def test_existing_plan_permission_controls_both_registration_entries(self):
        self.login()
        self.plans.allowed = False
        self.assertNotIn('/dest/self_inventory_new', self.card_urls('products'))
        self.assertNotIn('/dest/inventory_intake_new', self.card_urls('requests'))
        self.assertIn('/dest/inventory_intakes', self.card_urls('requests'))
        self.assertIn('/dest/feature_plans.plans', self.card_urls('money'))

    def test_unavailable_plan_lookup_fails_closed_without_exposing_error(self):
        self.login()
        self.plans.fail = True
        with self.assertLogs(self.app.logger, level='WARNING') as logs:
            urls = self.card_urls('products')
        self.assertNotIn('/dest/self_inventory_new', urls)
        self.assertNotIn('private capability', ''.join(logs.output))

    def test_restricted_customer_keeps_free_self_registration_but_not_new_intake(self):
        self.login()
        self.users['11'].edit_allowed = False
        self.assertIn('/dest/self_inventory_new', self.card_urls('products'))
        self.assertNotIn('/dest/inventory_intake_new', self.card_urls('requests'))
        self.assertIn('/dest/inventory_intakes', self.card_urls('requests'))
        self.assertIn('/dest/feature_plans.plans', self.card_urls('money'))

    def test_disabled_plans_and_inventory_do_not_offer_registration(self):
        self.login()
        self.plans.enabled = False
        self.app.config['STAGE2_INVENTORY_ENABLED'] = False
        self.assertNotIn('/dest/self_inventory_new', self.card_urls('products'))
        self.assertNotIn('/dest/inventory_intake_new', self.card_urls('requests'))
        self.assertNotIn('/dest/inventory_intakes', self.card_urls('requests'))
        self.assertNotIn('/dest/feature_plans.plans', self.card_urls('money'))

    def test_customer_identifier_is_derived_from_login_not_query(self):
        for identifier in (11, 22):
            self.login(identifier)
            data = self.hub('money', '?customer_id=90&section_key=support')
            urls = {card['url'] for card in data['section']['cards']}
            self.assertIn('/finance/customers/' + str(identifier), urls)
            self.assertNotIn('/finance/customers/90', urls)
            self.assertEqual(data['current']['key'], 'money')

    def test_legacy_records_remain_reachable_without_new_proxy_procurement_entry(self):
        self.login()
        products = self.card_urls('products')
        self.assertTrue({'/dest/index', '/dest/inventory_registry.index'} <= products)
        requests = self.hub('requests')['section']['cards']
        self.assertNotIn('/dest/public_proxy_service', {card['url'] for card in requests})
        history = next(card for card in requests if card['url'] == '/dest/public_proxy_service_history')
        self.assertTrue(history['secondary'])
        self.assertEqual(history['title'], '以前の仕入れ履歴')
        self.assertTrue({'/dest/user_document_list', '/dest/documents'} <= self.card_urls('documents'))
        self.assertTrue({'/dest/workspace_home.inbox', '/dest/user_announcements_page'} <= self.card_urls('support'))

    def test_all_card_links_are_internal_registered_get_routes(self):
        self.login()
        adapter = self.app.url_map.bind('localhost')
        for key in ('products', 'requests', 'money', 'documents', 'support'):
            response = self.client.get('/my/' + key)
            self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
            for card in response.get_json(force=True)['section']['cards']:
                self.assertTrue(card['url'].startswith('/') and not card['url'].startswith('//'))
                endpoint, _ = adapter.match(card['url'], method='GET')
                self.assertIn(endpoint, self.app.view_functions)

    def test_existing_destination_contexts_highlight_their_category(self):
        cases = {
            'user_home': 'home', 'workspace_home.home': 'home',
            'index': 'products', 'view_item': 'products', 'edit_item': 'products',
            'self_inventory_edit': 'products', 'inventory_registry.item': 'products',
            'commerce.orders': 'products', 'commerce.listing_item': 'requests',
            'inventory_intake_detail': 'requests', 'sales_agency_my_requests': 'requests',
            'public_proxy_service_history': 'requests', 'finance_review.customer': 'money',
            'payment_review.index': 'money', 'feature_plans.plans': 'money', 'reports': 'money',
            'user_document_list': 'documents', 'service_document_view': 'documents',
            'workspace_home.inbox': 'support', 'inquiry_new': 'support',
            'account_lifecycle.page': 'support', 'profile': 'support',
            'customer_navigation.guide': 'support',
        }
        for endpoint, key in cases.items():
            self.assertEqual(current_section(endpoint), key, endpoint)
        self.assertIsNone(current_section('admin_dashboard'))

    def test_guide_is_customer_only_and_reuses_available_destinations(self):
        self.assertEqual(self.client.get('/my/support/guide').status_code, 302)
        self.login(90)
        self.assertEqual(self.client.get('/my/support/guide').status_code, 403)
        self.login()
        self.app.extensions['kaika_staging_review'] = {'schema_only': True}
        response = self.client.get('/my/support/guide')
        self.assertEqual(response.status_code, 200)
        self.assertIn('no-store', response.headers['Cache-Control'])
        guide = response.get_json(force=True)
        self.assertEqual(guide['current']['key'], 'support')
        self.assertTrue(guide['preview'])
        self.assertEqual(guide['inquiry'], '/dest/inquiry_new')
        self.app.config['STAGE2_INVENTORY_ENABLED'] = False
        guide = self.client.get('/my/support/guide').get_json(force=True)
        links = {card['url'] for section in guide['sections'] for card in section['cards']}
        self.assertNotIn('/dest/inventory_registry.index', links)

    def test_legacy_custody_is_history_and_not_advertised_as_contract_deadline(self):
        self.login()
        cards = self.hub('products')['section']['cards']
        legacy = next(card for card in cards if card['url'] == '/dest/long_term_items')
        self.assertTrue(legacy['secondary'])
        self.assertEqual(legacy['title'], '以前の預かり記録')
        current = next(card for card in cards if card['url'] == '/dest/inventory_registry.index')
        self.assertIn('契約ごとの', current['description'])

    def test_registration_is_idempotent_and_does_not_access_business_data(self):
        bp = register_customer_navigation(self.runtime)
        self.assertIs(bp, self.app.blueprints['customer_navigation'])
        self.assertEqual(len([rule for rule in self.app.url_map.iter_rules()
                              if rule.endpoint == 'customer_navigation.hub']), 1)


if __name__ == '__main__':
    unittest.main()
