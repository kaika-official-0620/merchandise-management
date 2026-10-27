"""Real custody handlers + self inventory, temporary SQLite and fake users only.

Reuses the isolated fixture builders; never imports app.py or render_app.
"""
from datetime import datetime
import html
import io
import json
import re
import unittest
import test_self_inventory as fixtures
from inventory_custody import register_inventory_custody
from self_inventory import _lock_submission


class InventoryCustodyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.SelfInventoryTest.setUpClass.__func__(cls)

    db = fixtures.SelfInventoryTest.db
    login = fixtures.SelfInventoryTest.login
    form = fixtures.SelfInventoryTest.form
    create = fixtures.SelfInventoryTest.create
    record = fixtures.SelfInventoryTest.record
    png = staticmethod(fixtures.SelfInventoryTest.png)
    tearDown = fixtures.SelfInventoryTest.tearDown

    def setUp(self):
        fixtures.SelfInventoryTest.setUp(self)
        self.runtime.get_jst_now = lambda: datetime(2026, 9, 11, 12, 0)
        with self.db() as conn:
            conn.execute('ALTER TABLE merchandise ADD COLUMN storage_start_date TEXT')
            conn.execute('ALTER TABLE merchandise ADD COLUMN other_cost INTEGER DEFAULT 0')
            conn.execute('ALTER TABLE users ADD COLUMN display_name TEXT')
            conn.execute('ALTER TABLE users ADD COLUMN username TEXT')
            conn.execute("UPDATE users SET username='fixture-user-' || id,display_name='架空利用者' || id")
        self.custody = register_inventory_custody(self.runtime)
        # These inert sentinels exercise the before_request guard independently
        # of legacy business mutations. Real routes run in the parity suite.
        self.legacy_calls = []
        single_args = {'submit_sale_request': 'item_id', 'edit_item': 'id', 'delete_item': 'id',
                       'admin_delete_item': 'id', 'admin_transfer_item': 'id',
                       'admin_proxy_service_toggle_item': 'item_id', 'admin_proxy_service_toggle_item_quick': 'item_id'}
        for endpoint, argument in single_args.items():
            def sentinel(endpoint=endpoint, **_kwargs):
                self.legacy_calls.append(endpoint)
                return 'passed custody guard'
            self.app.add_url_rule(f'/_guard/{endpoint}/<int:{argument}>', endpoint, sentinel, methods=['GET', 'POST'])
        for endpoint in ('sales_agency_apply', 'submit_disposal_request', 'submit_long_term_disposal_request',
                         'admin_transfer_items_bulk', 'admin_proxy_service_bulk_toggle'):
            def sentinel(endpoint=endpoint):
                self.legacy_calls.append(endpoint)
                return 'passed custody guard'
            self.app.add_url_rule('/_guard/' + endpoint, endpoint, sentinel, methods=['POST'])

    def row(self, sql, args=()):
        with self.db() as conn:
            value = conn.execute(sql, args).fetchone()
            return dict(value) if value else None

    def operation(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        match = re.search(r'name="operation_token" value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return {'operation_token': html.unescape(match[1]), 'csrf_token': 'fixture-csrf'}

    def new_intake(self, item_id=None, count=1):
        data = self.operation('/inventory/intakes/new')
        data.update(kind='transfer' if item_id else 'registration', expected_count=str(count), client_note='架空検証')
        if item_id:
            data['item_ids'] = str(item_id)
        response = self.client.post('/inventory/intakes/new', data=data)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return int(response.location.rsplit('/', 1)[1]), data

    def action(self, intake_id, action, user=1, status=302, **extra):
        self.login(user)
        path = f'/inventory/intakes/{intake_id}'
        if status == 409:
            # Hidden UI actions still need a valid signed token to exercise the
            # server's state guard, rather than failing early at missing CSRF.
            version = self.row('SELECT version FROM inventory_intakes WHERE id=?', (intake_id,))['version']
            data = {'csrf_token': 'fixture-csrf', 'operation_token': self.custody.signer.dumps(
                {'user': user, 'purpose': 'intake', 'target': intake_id, 'version': version, 'nonce': 'negative-fixture'})}
        else:
            data = self.operation(path)
        data.update(action=action, **extra)
        response = self.client.post(path, data=data)
        self.assertEqual(response.status_code, status, response.get_data(as_text=True))
        return data

    def receive(self, intake_id, count=1):
        self.action(intake_id, 'approve', user=3, shipping_instructions='架空の検証専用発送先')
        self.action(intake_id, 'ship', carrier='架空便', tracking_number='TEST-000')
        self.action(intake_id, 'receive', user=3, received_count=str(count))

    def registration(self, intake_id):
        self.login(3)
        path = f'/inventory/intakes/{intake_id}/register'
        data = self.operation(path)
        data.update(product_name='架空受領バッグ', purchase_price='1500', listing_price='2400',
                    expected_shipping='0', expected_commission='0', purchase_date='2026-09-10')
        return path, data

    def test_existing_stock_stays_kaika_new_self_stock_is_self(self):
        self.assertEqual(self.record(100)['custody_location'], 'kaika')
        item_id = self.create()
        self.assertEqual(self.record(item_id)['custody_location'], 'self')
        event = self.row('SELECT * FROM inventory_custody_events WHERE merchandise_id=?', (item_id,))
        self.assertEqual((event['event_type'], event['actor_id'], event['user_id']), ('self_created', 1, 1))

    def test_transfer_receipt_preserves_item_id_and_owner(self):
        item_id = self.create()
        old_edit_url, old_edit = self.form(item_id)
        intake_id, data = self.new_intake(item_id)
        self.assertEqual(self.record(item_id)['custody_location'], 'self')
        self.assertEqual(self.client.post(old_edit_url, data=old_edit).status_code, 409)
        self.assertEqual(self.client.post('/inventory/intakes/new', data=data).status_code, 302)
        self.assertEqual(self.row('SELECT COUNT(*) AS n FROM inventory_intakes')['n'], 1)
        self.action(intake_id, 'approve', user=3, shipping_instructions='架空発送先')
        self.action(intake_id, 'ship', carrier='架空便', tracking_number='TEST-001')
        self.assertEqual(self.record(item_id)['custody_location'], 'transit')
        self.action(intake_id, 'receive', user=3, received_count='1')
        item = self.record(item_id)
        self.assertEqual((item['id'], item['user_id'], item['custody_location'], item['storage_start_date']), (item_id, 1, 'kaika', '2026-09-11'))
        self.assertEqual(self.row('SELECT status FROM inventory_intakes WHERE id=?', (intake_id,))['status'], 'completed')
        self.login(1)
        self.assertEqual(self.client.post(old_edit_url, data=old_edit).status_code, 409)
        self.assertEqual(self.client.get(f'/inventory/self/{item_id}/sale').status_code, 409)

    def test_staff_registration_requires_receipt_then_completes(self):
        intake_id, _ = self.new_intake(count=2)
        self.login(3)
        self.assertEqual(self.client.get(f'/inventory/intakes/{intake_id}/register').status_code, 409)
        self.receive(intake_id, count=2)
        self.action(intake_id, 'complete', user=3, status=409)
        path, data = self.registration(intake_id)
        data.update(user_id='2', scope='admin', custody_location='self')
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        first = self.row('SELECT m.* FROM merchandise m JOIN inventory_intake_items ii ON ii.merchandise_id=m.id WHERE ii.intake_id=?', (intake_id,))
        self.assertEqual((first['user_id'], first['scope'], first['custody_location']), (1, 'user', 'kaika'))
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.row('SELECT COUNT(*) AS n FROM inventory_intake_items WHERE intake_id=?', (intake_id,))['n'], 1)
        path, second_data = self.registration(intake_id)
        self.assertEqual(self.client.post(path, data=second_data).status_code, 302)
        _, excessive = self.registration(intake_id)
        self.assertEqual(self.client.post(path, data=excessive).status_code, 409)
        self.action(intake_id, 'complete', user=3)
        self.login(1)
        self.assertEqual(self.client.get(f'/inventory/self/{first["id"]}/edit').status_code, 404)
        self.assertEqual(self.client.get(f'/inventory/self/{first["id"]}/sale').status_code, 409)
        self.assertEqual(self.client.get(f'/inventory/intakes/{intake_id}').status_code, 200)

    def test_transfer_count_mismatch_rejects_without_partial_custody_change(self):
        item_id = self.create()
        intake_id, _ = self.new_intake(item_id)
        self.action(intake_id, 'approve', user=3, shipping_instructions='架空発送先')
        self.action(intake_id, 'receive', user=3, received_count='2', status=409)
        self.assertEqual(self.record(item_id)['custody_location'], 'self')
        self.assertEqual(self.row('SELECT status FROM inventory_intakes WHERE id=?', (intake_id,))['status'], 'approved')

    def test_received_registration_uses_physical_receipt_date_after_day_changes(self):
        intake_id, _ = self.new_intake()
        self.receive(intake_id)
        received = self.row('SELECT received_at FROM inventory_intakes WHERE id=?', (intake_id,))['received_at']
        self.runtime.get_jst_now = lambda: datetime(2026, 9, 13, 15, 0)
        path, data = self.registration(intake_id)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        item = self.row('SELECT m.* FROM merchandise m JOIN inventory_intake_items ii ON ii.merchandise_id=m.id WHERE ii.intake_id=?', (intake_id,))
        self.assertEqual(item['storage_start_date'], '2026-09-11')
        self.assertEqual(item['custody_received_at'], received)

    def test_user_cannot_approve_or_receive(self):
        item_id = self.create()
        intake_id, _ = self.new_intake(item_id)
        self.action(intake_id, 'approve', shipping_instructions='偽案内', status=409)
        self.action(intake_id, 'receive', received_count='1', status=409)
        self.assertEqual(self.record(item_id)['custody_location'], 'self')
        self.assertEqual(self.row('SELECT status FROM inventory_intakes WHERE id=?', (intake_id,))['status'], 'requested')

    def test_cross_owner_transfer_and_detail_are_rejected(self):
        item_id = self.create()
        intake_id, owner_token = self.new_intake(item_id)
        self.login(2)
        self.assertEqual(self.client.get(f'/inventory/intakes/{intake_id}').status_code, 404)
        token = self.operation('/inventory/intakes/new')
        token.update(kind='transfer', item_ids=str(item_id))
        self.assertEqual(self.client.post('/inventory/intakes/new', data=token).status_code, 404)
        self.assertEqual(self.client.post('/inventory/intakes/new', data=owner_token).status_code, 400)
        self.assertEqual(self.client.get(f'/inventory/self/{item_id}/sale').status_code, 404)

    def test_csrf_and_wrong_operation_tokens_do_not_create_intakes(self):
        item_id = self.create()
        data = self.operation('/inventory/intakes/new')
        data.update(kind='transfer', item_ids=str(item_id), csrf_token='wrong')
        self.assertEqual(self.client.post('/inventory/intakes/new', data=data).status_code, 400)
        data['csrf_token'] = 'fixture-csrf'
        data['operation_token'] = self.operation(f'/inventory/self/{item_id}/sale')['operation_token']
        self.assertEqual(self.client.post('/inventory/intakes/new', data=data).status_code, 400)
        self.assertEqual(self.row('SELECT COUNT(*) AS n FROM inventory_intakes')['n'], 0)

    def test_intake_stale_and_cross_target_tokens_rejected(self):
        intake_id, _ = self.new_intake()
        other_id, _ = self.new_intake()
        self.login(3)
        path = f'/inventory/intakes/{intake_id}'
        data = self.operation(path)
        data.update(action='approve', shipping_instructions='架空発送先')
        self.assertEqual(self.client.post(f'/inventory/intakes/{other_id}', data=data).status_code, 400)
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.client.post(path, data=data).status_code, 409)

    def test_cancellation_unlocks_own_stock_only_before_shipping(self):
        item_id = self.create()
        intake_id, _ = self.new_intake(item_id)
        self.action(intake_id, 'cancel')
        self.assertEqual(self.record(item_id)['custody_location'], 'self')
        self.assertEqual(self.client.get(f'/inventory/self/{item_id}/sale').status_code, 200)
        second_id, _ = self.new_intake(item_id)
        self.action(second_id, 'approve', user=3, shipping_instructions='架空発送先')
        self.action(second_id, 'ship', carrier='架空便', tracking_number='TEST-001')
        self.action(second_id, 'cancel', status=409)
        self.assertEqual(self.record(item_id)['custody_location'], 'transit')

    def test_self_sale_can_be_corrected_and_cancelled_with_audit(self):
        item_id = self.create()
        path = f'/inventory/self/{item_id}/sale'
        data = self.operation(path)
        data.update(action='sale', sale_date='2026-09-10', sale_price='5000', shipping_cost='500', commission='300', other_cost='200', sales_destination='架空販売先', is_shipped='1')
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual((self.record(item_id)['sale_price'], self.record(item_id)['is_shipped'], self.record(item_id)['other_cost']), (5000, 1, 200))
        old = dict(data)
        data.update(self.operation(path), sale_price='6000')
        self.assertEqual(self.client.post(path, data=data).status_code, 400)
        data['correction_reason'] = '売価入力を確認して訂正'
        data.pop('other_cost')  # Older clients must preserve the saved cost.
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.record(item_id)['other_cost'], 200)
        self.assertEqual(self.client.post(path, data=old).status_code, 409)
        corrected = self.row("SELECT details FROM inventory_custody_events WHERE event_type='sale' ORDER BY id DESC LIMIT 1")
        self.assertEqual(json.loads(corrected['details'])['before']['sale_price'], 5000)
        data = self.operation(path)
        data.update(action='cancel_sale', correction_reason='取引取消を確認')
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        item = self.record(item_id)
        self.assertEqual((item['sale_date'], item['sale_price'], item['shipping_cost'], item['commission'], item['is_shipped'], item['other_cost']), (None, 0, 0, 0, 0, 0))
        self.assertEqual(self.row('SELECT COUNT(*) AS n FROM sale_requests')['n'], 0)
        self.assertEqual(self.row('SELECT COUNT(*) AS n FROM sales_agency_requests')['n'], 0)

    def test_self_sale_invalid_values_do_not_change_financial_state(self):
        item_id = self.create()
        path = f'/inventory/self/{item_id}/sale'
        for key, value in [('sale_price', '-1'), ('sale_price', '1.5'), ('sale_date', '2026-09-12'), ('sale_date', '2026-02-30')]:
            data = self.operation(path)
            data.update(action='sale', sale_date='2026-09-10', sale_price='5000', shipping_cost='0', commission='0')
            data[key] = value
            self.assertEqual(self.client.post(path, data=data).status_code, 400)
        self.assertIsNone(self.record(item_id)['sale_date'])

    def test_active_workflow_blocks_self_sale_and_transfer(self):
        item_id = self.create()
        with self.db() as conn:
            conn.execute("INSERT INTO sale_requests (merchandise_id,status) VALUES (?,'pending')", (item_id,))
        self.assertEqual(self.client.get(f'/inventory/self/{item_id}/sale').status_code, 409)
        data = self.operation('/inventory/intakes/new')
        data.update(kind='transfer', item_ids=str(item_id))
        self.assertEqual(self.client.post('/inventory/intakes/new', data=data).status_code, 409)

    def test_registration_owner_is_server_controlled_and_nonadmin_denied(self):
        intake_id, _ = self.new_intake()
        self.receive(intake_id)
        self.login(1)
        self.assertEqual(self.client.get(f'/inventory/intakes/{intake_id}/register').status_code, 403)
        self.assertEqual(self.client.post(f'/inventory/intakes/{intake_id}/register', data={}).status_code, 403)

    def test_received_registration_bad_photo_rolls_back_and_removes_saved_files(self):
        intake_id, _ = self.new_intake()
        self.receive(intake_id)
        path, data = self.registration(intake_id)
        before_count = self.row('SELECT COUNT(*) AS n FROM merchandise')['n']
        before_version = self.row('SELECT version FROM inventory_intakes WHERE id=?', (intake_id,))['version']
        before_files = sorted(str(p.relative_to(self.uploads)) for p in self.uploads.rglob('*') if p.is_file())
        # The valid main photo is written first; a subsequent invalid extra
        # photo must remove that file as well as roll back all database writes.
        data.update(photo=self.png(), additional_photos=(io.BytesIO(b'not an image'), 'invalid.png'))
        response = self.client.post(path, data=data)
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))
        self.assertIn('商品一覧', response.get_data(as_text=True))
        self.assertEqual(self.row('SELECT COUNT(*) AS n FROM merchandise')['n'], before_count)
        self.assertEqual(self.row('SELECT COUNT(*) AS n FROM inventory_intake_items WHERE intake_id=?', (intake_id,))['n'], 0)
        self.assertEqual(self.row('SELECT version FROM inventory_intakes WHERE id=?', (intake_id,))['version'], before_version)
        self.assertEqual(sorted(str(p.relative_to(self.uploads)) for p in self.uploads.rglob('*') if p.is_file()), before_files)

    def test_role_and_plan_enforcement_preserves_read_access(self):
        item_id = self.create()
        intake_id, _ = self.new_intake(item_id)
        for user in (3, 4, 5):
            self.login(user)
            self.assertEqual(self.client.get('/inventory/intakes/new').status_code, 403)
            self.assertEqual(self.client.get(f'/inventory/self/{item_id}/sale').status_code, 403)
        self.login(1)
        with self.db() as conn:
            conn.execute("UPDATE users SET entitled=0,subscription_status='past_due' WHERE id=1")
        self.assertEqual(self.client.get(f'/inventory/intakes/{intake_id}').status_code, 200)
        self.assertEqual(self.client.get('/inventory/intakes').status_code, 200)

    def test_legacy_single_guard_owner_admin_and_three_custody_states(self):
        endpoints = ('submit_sale_request', 'edit_item', 'delete_item', 'admin_delete_item',
                     'admin_transfer_item', 'admin_proxy_service_toggle_item', 'admin_proxy_service_toggle_item_quick')
        for location in ('self', 'transit', 'kaika'):
            with self.db() as conn:
                conn.execute('UPDATE merchandise SET custody_location=? WHERE id=100', (location,))
            for user in (1, 2, 3):
                self.login(user)
                expected = 404 if user == 2 else 200 if location == 'kaika' else 409
                for endpoint in endpoints:
                    with self.subTest(location=location, user=user, endpoint=endpoint):
                        previous = len(self.legacy_calls)
                        response = self.client.post(f'/_guard/{endpoint}/100')
                        self.assertEqual(response.status_code, expected)
                        self.assertEqual(len(self.legacy_calls) - previous, 1 if expected == 200 else 0)

    def test_legacy_batch_guard_rejects_mixed_self_or_transit_before_handler(self):
        fields = {'sales_agency_apply': 'merchandise_ids', 'submit_disposal_request': 'merchandise_ids[]',
                  'submit_long_term_disposal_request': 'merchandise_ids', 'admin_transfer_items_bulk': 'item_ids',
                  'admin_proxy_service_bulk_toggle': 'item_ids'}
        self.login(3)
        for location in ('self', 'transit'):
            with self.db() as conn:
                conn.execute('UPDATE merchandise SET custody_location=? WHERE id=101', (location,))
            for endpoint, field in fields.items():
                with self.subTest(location=location, endpoint=endpoint):
                    previous = len(self.legacy_calls)
                    response = self.client.post('/_guard/' + endpoint, data={field: ['100', '101']})
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(len(self.legacy_calls), previous)
        self.login(1)
        self.assertEqual(self.client.post('/_guard/sales_agency_apply', data={'merchandise_ids': ['100', '101']}).status_code, 404)

    def test_proxy_guard_json_items_and_raw_json_fallback_match_handler(self):
        self.login(3)
        path = '/_guard/admin_proxy_service_bulk_toggle'
        for location, expected in [('self', 409), ('transit', 409), ('kaika', 200)]:
            with self.db() as conn:
                conn.execute('UPDATE merchandise SET custody_location=? WHERE id=100', (location,))
            for payload in ({'items': [{'id': 100}]}, {'item_ids': [100]}):
                with self.subTest(location=location, payload=payload):
                    self.assertEqual(self.client.post(path, json=payload).status_code, expected)
                    self.assertEqual(self.client.post(path, data=json.dumps(payload), content_type='text/plain').status_code, expected)
        for payload in ({'items': {}}, {'items': [None]}, {'items': [{'id': '../100'}]}, {'item_ids': 100}):
            self.assertEqual(self.client.post(path, json=payload).status_code, 400)

    def test_proxy_guard_json_ids_require_array_and_form_ids_use_getlist(self):
        self.login(3)
        with self.db() as conn:
            conn.execute("INSERT INTO merchandise (id,user_id,scope,product_name,custody_location) VALUES (1,1,'user','架空自己保管','self')")
        # JSON has an explicit array contract. Form getlist preserves a whole
        # ID, as does the effective safe_bulk_toggle runtime handler.
        for kwargs in ({'json': {'item_ids': '100'}},
                       {'data': json.dumps({'item_ids': '100'}), 'content_type': 'text/plain'}):
            self.assertEqual(self.client.post('/_guard/admin_proxy_service_bulk_toggle', **kwargs).status_code, 400)
        self.assertEqual(self.client.post('/_guard/admin_proxy_service_bulk_toggle', data={'item_ids': '100'}).status_code, 200)

    def pg_duplicate_request(self, flow):
        """Record SQL from actual handlers; never open a PostgreSQL connection."""
        if flow == 'intake':
            path = '/inventory/intakes/new'
            data = self.operation(path)
            data.update(kind='registration', expected_count='1')
            self.custody.pg, self.custody.mark = True, '%s'
        else:
            path, data = self.form()
            self.service.postgres, self.service.mark = True, '%s'
        calls = []
        class Cursor:
            def execute(self, sql, args=()):
                self.sql = sql
                calls.append((' '.join(sql.split()), args))
            def fetchone(self):
                if 'to_regclass' in self.sql:
                    return {'table_name': 'self_inventory_items'}
                if 'WHERE submission_hash=' in self.sql or 'WHERE submission_hash =' in self.sql:
                    return {'id' if flow == 'intake' else 'merchandise_id': 444}
                return None
            def fetchall(self):
                return []
            def close(self):
                pass
        class Connection:
            def cursor(self):
                return Cursor()
            def commit(self):
                calls.append(('COMMIT', ()))
            def rollback(self):
                calls.append(('ROLLBACK', ()))
            def close(self):
                pass
        self.runtime.get_db = Connection
        response = self.client.post(path, data=data)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        self.assertTrue(response.location.endswith('/444'))
        lock_positions = [i for i, (sql, _) in enumerate(calls) if sql.startswith('SELECT pg_advisory_xact_lock(')]
        check_positions = [i for i, (sql, _) in enumerate(calls) if 'WHERE submission_hash' in sql]
        self.assertEqual(len(lock_positions), 1)
        self.assertEqual(len(check_positions), 1)
        self.assertLess(lock_positions[0], check_positions[0])
        self.assertTrue(any(sql == 'COMMIT' for sql, _ in calls))
        self.assertFalse(any(sql.startswith('INSERT') or sql == 'ROLLBACK' for sql, _ in calls))

    def test_pg_self_create_retry_locks_before_existing_key_lookup(self):
        self.pg_duplicate_request('self')

    def test_pg_intake_create_retry_locks_before_existing_key_lookup(self):
        self.pg_duplicate_request('intake')

    def test_pg_submission_lock_keys_are_stable_namespaced_signed_bigints(self):
        calls = []
        class Cursor:
            def execute(self, sql, args):
                calls.append((sql, args))
        cursor = Cursor()
        for namespace, nonce in [('self', 'a'), ('self', 'a'), ('intake', 'a'), ('self', 'b')]:
            _lock_submission(cursor, namespace, nonce)
        keys = [args[0] for _, args in calls]
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(len(set(keys)), 3)
        self.assertTrue(all(-(2**63) <= key < 2**63 for key in keys))
        self.assertTrue(all(sql == 'SELECT pg_advisory_xact_lock(%s)' for sql, _ in calls))

    def test_pg_first_schema_creation_locks_but_existing_table_does_not(self):
        self.service.postgres = True
        for exists in (True, False):
            for dictionary_row in (True, False):
                calls = []
                class Cursor:
                    def execute(self, sql, args=()):
                        calls.append((' '.join(sql.split()), args))
                    def fetchone(self):
                        name = 'self_inventory_items' if exists else None
                        return {'table_name': name} if dictionary_row else (name,)
                self.service._ensure_schema(Cursor())
                self.assertIn('to_regclass', calls[0][0])
                if exists:
                    self.assertEqual(len(calls), 1)
                else:
                    self.assertEqual(calls[1][0], 'SELECT pg_advisory_xact_lock(%s)')
                    self.assertTrue(calls[2][0].startswith('CREATE TABLE IF NOT EXISTS self_inventory_items'))


if __name__ == '__main__':
    unittest.main()
