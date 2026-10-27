import sqlite3
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest

from flask import Flask
from flask_login import LoginManager, UserMixin

from stage2_legacy_guard import (LinkedInventoryMutationPending, linked_ids,
    register_stage2_legacy_guard, reject_linked_items)


class User(UserMixin):
    id = 1

    def is_admin(self):
        return True


class LegacyGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'guard.sqlite3'
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only-session-secret'
        self.app.config.update(TESTING=True, STAGE2_INVENTORY_ENABLED=True)
        login = LoginManager(self.app)
        login.user_loader(lambda user_id: User())
        self.opened = 0
        def connect():
            self.opened += 1
            conn = sqlite3.connect(str(self.path))
            self.addCleanup(conn.close)
            conn.row_factory = sqlite3.Row
            return conn
        self.runtime = SimpleNamespace(app=self.app, DATABASE_URL=None, get_db=connect)
        with connect() as conn:
            conn.executescript('''
                CREATE TABLE merchandise(id INTEGER PRIMARY KEY,auction_id INTEGER);
                INSERT INTO merchandise VALUES(10,5),(11,6);
                CREATE TABLE business_item_links(merchandise_id INTEGER PRIMARY KEY,physical_item_id TEXT);
                INSERT INTO business_item_links VALUES(10,'physical');
                CREATE TABLE sale_requests(id INTEGER PRIMARY KEY,merchandise_id INTEGER);
                INSERT INTO sale_requests VALUES(20,10),(21,11);
                CREATE TABLE item_disposal_requests(id INTEGER PRIMARY KEY,merchandise_id INTEGER);
                INSERT INTO item_disposal_requests VALUES(30,10);
                CREATE TABLE sales_agency_request_items(request_id INTEGER,merchandise_id INTEGER);
                INSERT INTO sales_agency_request_items VALUES(40,10);
                CREATE TABLE proxy_service_auction_items(auction_id INTEGER,merchandise_id INTEGER);
                INSERT INTO proxy_service_auction_items VALUES(7,10);
                CREATE TABLE user_keisan(id INTEGER PRIMARY KEY,is_admin_created INTEGER,status TEXT,proxy_service_auction_id INTEGER);
                INSERT INTO user_keisan VALUES(50,1,'completed',5),(51,1,'completed',6);
                CREATE TABLE user_keisan_items(keisan_id INTEGER,merchandise_id INTEGER,proxy_source_item_id INTEGER);
                INSERT INTO user_keisan_items VALUES(50,11,10),(51,11,NULL);
            ''')
        self.calls = []
        routes = {
            'edit_item': '/edit/<int:id>', 'admin_transfer_item': '/transfer/<int:id>',
            'admin_transfer_items_bulk': '/transfer-bulk',
            'approve_sale_request': '/approve/<int:request_id>',
            'admin_sale_request_shipment': '/shipment/<int:request_id>',
            'process_disposal_request': '/disposal/<int:request_id>',
            'admin_sales_agency_process': '/agency/<int:id>',
            'sales_agency_apply': '/agency/apply',
            'proxy_service_purchase': '/proxy-service/purchase',
            'proxy_service_bid': '/proxy-service/bid',
            'admin_proxy_service_finalize': '/finalize/<int:auction_id>',
            'admin_proxy_service_bulk_toggle': '/proxy-bulk/<int:auction_id>',
            'admin_auction_keisan_submit': '/document/<int:id>',
            'admin_auction_keisan_send_bulk': '/document-bulk',
            'self_inventory_new': '/inventory/self/new',
            'inventory_intake_detail': '/inventory/intakes/<int:request_id>',
            'inventory_registry.event': '/inventory/items/<item_id>/events',
        }
        for endpoint, path in routes.items():
            def handler(_endpoint=endpoint, **kwargs):
                self.calls.append(_endpoint)
                return 'legacy-or-synchronized-handler', 200
            self.app.add_url_rule(path, endpoint, handler, methods=['GET', 'POST'])
        register_stage2_legacy_guard(self.runtime)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['_user_id'] = '1'
            session['_fresh'] = True

    def held(self, path, data=None, json=None):
        before = len(self.calls)
        response = self.client.post(path, data=data, json=json)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error']['code'], 'linked_inventory_mutation_pending')
        self.assertEqual(len(self.calls), before)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')

    def test_staff_edit_and_owner_transfer_are_held_for_linked_stock(self):
        self.held('/edit/10', {'item_status': 'sold', 'sale_price': '50000'})
        self.held('/transfer/10', {'target_user_id': '99'})
        self.held('/transfer-bulk', {'item_ids': ['11', '10'], 'target_user_id': '99'})

    def test_sale_shipping_and_disposal_look_up_the_request_item(self):
        self.held('/approve/20')
        self.held('/shipment/20', {'action': 'revert'})
        self.held('/disposal/30', {'action': 'liquidation_completed'})

    def test_sales_agency_group_checks_all_linked_members(self):
        self.held('/agency/40', json={'action': 'complete', 'sale_price': 50000})
        self.held('/agency/apply', {'merchandise_ids': ['11', '10']})

    def test_proxy_json_form_bulk_payload_and_historical_auction_sources(self):
        self.held('/proxy-service/purchase', json={'merchandise_id': 10})
        self.held('/proxy-service/bid', {'merchandise_id': '10'})
        self.held('/proxy-bulk/6', json={'items': [{'id': 11}, {'id': 10}]})
        self.held('/finalize/5')
        self.held('/finalize/7')

    def test_document_send_and_bulk_original_source_links_are_held(self):
        self.held('/document/50')
        self.held('/document-bulk', {'document_ids': ['51', '50']})
        self.held('/document-bulk', {'send_scope': 'all'})

    def test_legacy_numeric_coercions_and_raw_json_cannot_hide_linked_target(self):
        for target in (' +10 ', '10.0', '1e1', '１０', 10.0, 10.9):
            with self.subTest(target=target):
                self.held('/proxy-service/purchase', json={'merchandise_id': target})
        response = self.client.post('/proxy-bulk/6', data='{"items":[{"id":" +10 "}]}', content_type='text/plain')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.calls, [])

    def test_unchanged_get_pages_unlinked_stock_and_synchronized_flows_work(self):
        for path in ('/edit/10', '/finalize/5', '/document/50'):
            self.assertEqual(self.client.get(path).status_code, 200)
        for path in ('/edit/11', '/transfer/11', '/approve/21', '/finalize/6',
                     '/inventory/self/new', '/inventory/intakes/20', '/inventory/items/physical/events'):
            self.assertEqual(self.client.post(path).status_code, 200)

    def test_flag_off_does_not_open_database_or_change_legacy_behavior(self):
        self.app.config['STAGE2_INVENTORY_ENABLED'] = False
        count = self.opened
        self.assertEqual(self.client.post('/transfer/10').status_code, 200)
        self.assertEqual(self.opened, count)
        self.assertEqual(linked_ids(self.runtime, None, [10]), [])

    def test_anonymous_request_preserves_original_authentication_handling(self):
        count = self.opened
        # Dummy handler has no authentication; original application's decorators
        # produce login/401. The guard intentionally does not disclose the link.
        self.assertEqual(self.app.test_client().post('/transfer/10').status_code, 200)
        self.assertEqual(self.opened, count)

    def test_lookup_failure_holds_writes_without_echoing_database_error(self):
        with self.runtime.get_db() as conn:
            conn.execute('DROP TABLE business_item_links')
        response = self.client.post('/transfer/10')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json['error']['code'], 'inventory_link_check_unavailable')
        self.assertNotIn('no such table', response.get_data(as_text=True))
        self.assertEqual(self.calls, [])

    def test_transaction_helper_rejects_before_caller_mutation(self):
        with self.runtime.get_db() as conn:
            with self.assertRaises(LinkedInventoryMutationPending):
                reject_linked_items(self.runtime, conn, [10])
                conn.execute('DELETE FROM merchandise')
            reject_linked_items(self.runtime, conn, [11])
            self.assertEqual(conn.execute('SELECT count(*) FROM merchandise').fetchone()[0], 2)

    def test_guard_registration_is_idempotent(self):
        first = self.app.extensions['stage2_legacy_guard']
        before = len(self.app.before_request_funcs[None])
        self.assertIs(register_stage2_legacy_guard(self.runtime), first)
        self.assertEqual(len(self.app.before_request_funcs[None]), before)


if __name__ == '__main__':
    unittest.main()
