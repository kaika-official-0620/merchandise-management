"""Stage-2 basic management on temporary SQLite and synthetic photos only."""
import ast
from datetime import timedelta, timezone
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask

import test_inventory_custody as custody_fixtures
import test_mobile_api as mobile_fixtures
from feature_plans import FeaturePlans
from item_registry import Registry, Actor, RegistryError
from item_registry_schema import migrate_registry
from stage2_self_management import (PHOTO_RECORDS_DDL, basic_self_management_allowed,
    enabled, photo_summary, record_item_photos, registration_quota, enforce_new_registration)


class Stage2SelfManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        custody_fixtures.InventoryCustodyTests.setUpClass.__func__(cls)

    db = custody_fixtures.InventoryCustodyTests.db
    login = custody_fixtures.InventoryCustodyTests.login
    form = custody_fixtures.InventoryCustodyTests.form
    create = custody_fixtures.InventoryCustodyTests.create
    record = custody_fixtures.InventoryCustodyTests.record
    png = staticmethod(custody_fixtures.InventoryCustodyTests.png)
    tearDown = custody_fixtures.InventoryCustodyTests.tearDown
    row = custody_fixtures.InventoryCustodyTests.row
    operation = custody_fixtures.InventoryCustodyTests.operation
    action = custody_fixtures.InventoryCustodyTests.action
    new_intake = custody_fixtures.InventoryCustodyTests.new_intake
    receive = custody_fixtures.InventoryCustodyTests.receive
    registration = custody_fixtures.InventoryCustodyTests.registration

    def setUp(self):
        custody_fixtures.InventoryCustodyTests.setUp(self)
        with self.service.connection():
            pass
        with self.db() as conn:
            conn.execute('PRAGMA foreign_keys=ON')
            migrate_registry(conn, allowed_test_path=self.path)
            conn.execute(PHOTO_RECORDS_DDL)
        self.app.config.update(STAGE2_INVENTORY_ENABLED=True, STAGE2_FREE_LIMIT_PROPOSAL_ENABLED=True)
        self.plans.snapshot = lambda user=None: {'plan_code': None}

    def add_unlinked(self, count, user_id=1, **extra):
        with self.db() as conn:
            for index in range(count):
                values = dict(user_id=user_id, scope='user', product_name='架空旧在庫', custody_location='self')
                values.update(extra)
                conn.execute('INSERT INTO merchandise (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))

    def quota(self, user_id=1, plan_code=None):
        with self.db() as conn:
            return registration_quota(conn.cursor(), user_id, self.app, plan_code=plan_code)

    def test_stage_flag_and_proposal_are_explicit_separate_switches(self):
        app = Flask('flag-fixture')
        user = SimpleNamespace(is_authenticated=True, role='user')
        self.assertFalse(enabled(app))
        app.config['STAGE2_INVENTORY_ENABLED'] = 'true'
        self.assertFalse(basic_self_management_allowed(user, app))
        self.app.config['STAGE2_FREE_LIMIT_PROPOSAL_ENABLED'] = False
        self.assertIsNone(self.quota()['active_limit'])
        self.assertFalse(self.quota()['can_add'])
        url, data = self.form()
        self.assertEqual(self.client.post(url, data=data).status_code, 409)

    def test_free_and_past_due_customers_keep_basic_records_without_paid_grant(self):
        for user_id in (4, 5):
            self.login(user_id)
            item_id = self.create()
            self.assertEqual(self.record(item_id)['user_id'], user_id)
            url, data = self.form(item_id)
            data['notes'] = '無料で記録を継続'
            self.assertEqual(self.client.post(url, data=data).status_code, 302)
            self.assertEqual(self.record(item_id)['notes'], '無料で記録を継続')
        self.assertFalse(self.row("SELECT name FROM sqlite_master WHERE name='business_charge_drafts'"))

    def test_last_slot_replay_sale_and_over_limit_edit(self):
        self.add_unlinked(99)
        url, data = self.form()
        response = self.client.post(url, data=data)
        self.assertEqual(response.status_code, 302)
        item_id = int(response.location.rsplit('/', 1)[1])
        self.assertEqual(self.quota()['active_items'], 100)
        self.assertEqual(self.client.post(url, data=data).status_code, 302)
        next_url, next_data = self.form()
        self.assertEqual(self.client.post(next_url, data=next_data).status_code, 409)
        edit_url, edit_data = self.form(item_id)
        edit_data['notes'] = '上限でも編集可能'
        self.assertEqual(self.client.post(edit_url, data=edit_data).status_code, 302)
        sale_path = f'/inventory/self/{item_id}/sale'
        sale = self.operation(sale_path)
        sale.update(action='sale', sale_date='2026-09-10', sale_price='3000', shipping_cost='200', commission='300')
        self.assertEqual(self.client.post(sale_path, data=sale).status_code, 302)
        self.assertEqual(self.quota()['active_items'], 99)
        self.create()
        cancel = self.operation(sale_path)
        cancel.update(action='cancel_sale', correction_reason='購入者による取消')
        self.assertEqual(self.client.post(sale_path, data=cancel).status_code, 302)
        self.assertEqual(self.quota()['active_items'], 101)
        self.assertEqual(self.client.get(f'/inventory/self/{item_id}/edit').status_code, 200)
        self.assertFalse(self.quota()['can_add'])

    def test_monthly_counts_sold_and_kaika_stock_do_not_share_self_limit(self):
        self.add_unlinked(150, sale_date='2026-09-01')
        self.add_unlinked(150, custody_location='kaika')
        self.assertEqual(self.quota()['active_items'], 0)
        self.assertIsNone(self.quota()['monthly_registration_limit'])
        self.create()
        self.assertEqual(self.quota()['active_items'], 1)
        paid = self.quota(plan_code='business')
        self.assertEqual(paid['active_limit'], 100)
        self.assertTrue(paid['paid_extension_pending'])

    def test_same_physical_links_count_once_but_unknown_rows_are_not_guessed(self):
        item_id = self.create()
        with self.db() as conn:
            linked = conn.execute('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=?', (item_id,)).fetchone()[0]
            second = conn.execute("INSERT INTO merchandise(user_id,scope,product_name,custody_location) VALUES(1,'user','架空明示参照','self')").lastrowid
            conn.execute("INSERT INTO business_item_links VALUES(?,?,'verified','synthetic-same-good')", (second, linked))
        self.assertEqual(self.quota()['active_items'], 1)
        self.add_unlinked(2)
        self.assertEqual(self.quota()['active_items'], 3)
        self.assertTrue(self.quota()['needs_reconciliation'])

    def test_existing_unlinked_sale_remains_available_without_fabricated_registry_event(self):
        self.add_unlinked(1)
        with self.db() as conn:
            item_id = conn.execute('SELECT MAX(id) FROM merchandise').fetchone()[0]
        sale_path = f'/inventory/self/{item_id}/sale'
        sale = self.operation(sale_path)
        sale.update(action='sale', sale_date='2026-09-10', sale_price='3000', shipping_cost='200', commission='300')
        self.assertEqual(self.client.post(sale_path, data=sale).status_code, 302)
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM business_item_observations').fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inventory_custody_events WHERE event_type='sale'").fetchone()[0], 1)

    def test_missing_migration_prevents_new_writes_without_implicit_ddl(self):
        with self.db() as conn:
            conn.execute('DROP TABLE stage2_photo_records')
        self.assertEqual(self.client.get('/inventory/self/new').status_code, 409)
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM merchandise').fetchone()[0], 3)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='stage2_photo_records'").fetchone())

    def test_stage2_registration_does_not_run_legacy_custody_migration(self):
        from inventory_custody import register_inventory_custody
        app = Flask('no-startup-migration')
        app.config.update(SECRET_KEY='only-a-fixture-secret', STAGE2_INVENTORY_ENABLED=True)
        app.extensions['kaika_feature_plans'] = self.plans
        runtime = SimpleNamespace(app=app, DATABASE_URL=None,
                                  get_db=lambda: self.fail('registration must not open the database'))
        service = register_inventory_custody(runtime)
        self.assertIs(app.extensions['inventory_custody'], service)

    def test_real_bootstrap_initializes_flags_before_custody_extension(self):
        from inventory_custody import register_inventory_custody
        from runtime_security import require_session_secret, install_runtime_security
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text(encoding='utf-8-sig'))
        # Execute only the actual Flask/config bootstrap, stopping before any
        # function definitions or DB initializer. No application import runs.
        start = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == 'app' for target in node.targets))
        end = next(i for i in range(start, len(tree.body)) if isinstance(tree.body[i], ast.FunctionDef))
        code = compile(ast.Module(body=tree.body[start:end], type_ignores=[]), 'isolated-app-bootstrap', 'exec')
        for flag in (None, '1'):
            with self.subTest(flag=flag):
                env = {'SECRET_KEY': 'fixture-only-abcdef0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'}
                if flag:
                    env['STAGE2_INVENTORY_ENABLED'] = flag
                with patch.dict(os.environ, env, clear=True):
                    namespace = dict(__name__='stage2-bootstrap-fixture', Flask=Flask, os=os,
                        require_session_secret=require_session_secret, install_runtime_security=install_runtime_security,
                        timedelta=timedelta, timezone=timezone)
                    exec(code, namespace)
                app = namespace['app']
                self.assertEqual(enabled(app), flag == '1')
                self.assertFalse(app.config['STAGE2_FREE_LIMIT_PROPOSAL_ENABLED'])
                if flag:
                    app.extensions['kaika_feature_plans'] = self.plans
                    runtime = SimpleNamespace(app=app, DATABASE_URL=None,
                        get_db=lambda: self.fail('enabled startup must not run legacy migration'))
                    register_inventory_custody(runtime)

    def test_missing_custody_migration_is_not_recreated_by_request(self):
        with self.db() as conn:
            conn.execute('DROP TABLE inventory_custody_events')
        self.assertEqual(self.client.get('/inventory/intakes').status_code, 409)
        with self.db() as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='inventory_custody_events'").fetchone())

    def test_photo_measurements_keep_replaced_original_and_mark_unknown_separately(self):
        item_id = self.create({'photo': self.png()})
        original = self.record(item_id)['photo_path']
        self.assertTrue((self.static / original).is_file())
        with self.db() as conn:
            metadata = photo_summary(conn.cursor(), item_id, 1)
        self.assertEqual(metadata['photo_count'], 1)
        self.assertGreater(metadata['known_bytes'], 0)
        url, data = self.form(item_id)
        data['photo'] = self.png('replacement.png')
        self.assertEqual(self.client.post(url, data=data).status_code, 302)
        self.assertTrue((self.static / original).is_file())
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM stage2_photo_records WHERE merchandise_id=?', (item_id,)).fetchone()[0], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM stage2_photo_records WHERE merchandise_id=? AND removed_from_item_at IS NOT NULL', (item_id,)).fetchone()[0], 1)
            result = record_item_photos(conn.cursor(), item_id, 1,
                {'photo_path': 'uploads/../../private.jpg'}, self.app)
            self.assertEqual(result, {'photo_count': 1, 'known_bytes': 0, 'unknown_size_count': 1})

    def test_first_edit_keeps_metadata_for_preexisting_photo_removed_from_current_item(self):
        self.app.config['STAGE2_INVENTORY_ENABLED'] = False
        item_id = self.create({'photo': self.png()})
        self.app.config['STAGE2_INVENTORY_ENABLED'] = True
        original = self.record(item_id)['photo_path']
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM stage2_photo_records').fetchone()[0], 0)
        path, data = self.form(item_id)
        data['remove_photo'] = '1'
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertTrue((self.static / original).is_file())
        with self.db() as conn:
            row = conn.execute('SELECT * FROM stage2_photo_records WHERE merchandise_id=?', (item_id,)).fetchone()
            self.assertIsNotNone(row['removed_from_item_at'])
            self.assertGreater(row['byte_size'], 0)

    def test_registry_failure_rolls_back_legacy_item_and_photo_file(self):
        url, data = self.form()
        data['photo'] = self.png()
        with patch('stage2_registry_hooks.Registry.attach_legacy', side_effect=RegistryError('invalid_state_transition', status=409)):
            self.assertEqual(self.client.post(url, data=data).status_code, 409)
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM merchandise').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM business_items').fetchone()[0], 0)
        self.assertEqual(list(self.uploads.glob('self_*')), [])

    def test_other_customer_cannot_edit_or_measure_records_through_form(self):
        item_id = self.create({'photo': self.png()})
        self.login(2)
        self.assertEqual(self.client.get(f'/inventory/self/{item_id}/edit').status_code, 404)
        with self.db() as conn:
            self.assertEqual(photo_summary(conn.cursor(), item_id, 2)['photo_count'], 0)

    def test_free_inventory_does_not_unlock_new_kaika_intake(self):
        self.assertEqual(self.client.get('/inventory/intakes/new').status_code, 403)
        self.plans.snapshot = lambda user=None: {'plan_code': 'normal'}
        item_id = self.create()
        intake_id, _ = self.new_intake(item_id)
        self.receive(intake_id)
        with self.db() as conn:
            linked = conn.execute('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=?', (item_id,)).fetchone()[0]
            state = Registry(conn).detail(Actor(3, staff=True), linked)
            self.assertEqual(state['custodian'], 'kaika')
            self.assertEqual(state['physical_state'], 'received')
            self.assertEqual(len(state['legacy_ids']), 1)
            self.assertEqual(state['works'], [])

    def test_paid_expiry_preserves_existing_intake_read_and_cancel(self):
        self.plans.snapshot = lambda user=None: {'plan_code': 'normal'}
        item_id = self.create()
        intake_id, _ = self.new_intake(item_id)
        self.plans.snapshot = lambda user=None: {'plan_code': None}
        self.assertEqual(self.client.get('/inventory/intakes/new').status_code, 403)
        self.action(intake_id, 'cancel')
        self.assertEqual(self.row('SELECT status FROM inventory_intakes WHERE id=?', (intake_id,))['status'], 'cancelled')

    def test_kaika_registration_requires_real_photo_confirmation_before_completion(self):
        self.plans.snapshot = lambda user=None: {'plan_code': 'normal'}
        intake_id, _ = self.new_intake()
        self.receive(intake_id)
        path, data = self.registration(intake_id)
        data['photo'] = self.png()
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.action(intake_id, 'complete', user=3, status=409)
        self.assertEqual(self.row('SELECT status FROM inventory_intakes WHERE id=?', (intake_id,))['status'], 'received')
        from datetime import datetime, timezone, timedelta
        with self.db() as conn:
            item_id = conn.execute('SELECT physical_item_id FROM business_item_profiles').fetchone()[0]
            Registry(conn).record_event(Actor(3, staff=True), item_id, 'photographed',
                datetime(2026,9,11,12,tzinfo=timezone(timedelta(hours=9))),
                'fixture-observed-photo', 'fixture staff confirmed shooting')
        self.action(intake_id, 'complete', user=3)
        with self.db() as conn:
            item = Registry(conn).detail(Actor(3, staff=True), item_id)
            self.assertEqual(item['workflow_state'], 'registered')
            self.assertEqual(item['works'], [])
            self.assertFalse(item['charge_created'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM stage2_photo_records').fetchone()[0], 1)

    def test_self_title_changes_update_same_registry_item_with_audit(self):
        item_id = self.create()
        path, data = self.form(item_id)
        data['product_name'] = '訂正後の架空商品名'
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT title FROM business_item_profiles').fetchone()[0], data['product_name'])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM business_item_reviews').fetchone()[0], 1)

    def test_database_lock_is_by_customer_not_submission(self):
        class Cursor:
            def __init__(self): self.calls=[]
            def execute(self, sql, args=()): self.calls.append((sql,args))
        first, second = Cursor(), Cursor()
        with patch('stage2_self_management.registration_quota', return_value={'can_add': True}):
            enforce_new_registration(first, 1, self.app, postgres=True)
            enforce_new_registration(second, 1, self.app, postgres=True)
        self.assertEqual(first.calls, second.calls)
        self.assertIn('pg_advisory_xact_lock', first.calls[0][0])

    def test_return_reuses_item_id_and_holds_future_edits_until_review(self):
        item_id = self.create()
        path = f'/inventory/self/{item_id}/sale'
        data = self.operation(path)
        data.update(action='sale', sale_date='2026-09-10', sale_price='3000', shipping_cost='200', commission='300', is_shipped='1')
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        data = self.operation(path)
        data.update(action='cancel_sale', correction_reason='返品を受領した')
        self.assertEqual(self.client.post(path, data=data).status_code, 409)
        self.assertEqual(self.record(item_id)['is_shipped'], 1)
        data['return_received'] = '1'
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.quota()['active_items'], 1)
        with self.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM business_items').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT held FROM business_item_profiles').fetchone()[0], 1)
        url, data = self.form(item_id)
        self.assertEqual(self.client.post(url, data=data).status_code, 409)
        self.assertEqual(self.client.get(path).status_code, 409)

    def test_registry_return_counts_as_active_while_original_sale_is_preserved(self):
        item_id = self.create()
        path = f'/inventory/self/{item_id}/sale'
        data = self.operation(path)
        data.update(action='sale', sale_date='2026-09-10', sale_price='3000', shipping_cost='200', commission='300', is_shipped='1')
        self.assertEqual(self.client.post(path, data=data).status_code, 302)
        self.assertEqual(self.quota()['active_items'], 0)
        from datetime import datetime, timezone, timedelta
        with self.db() as conn:
            physical_id = conn.execute('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=?', (item_id,)).fetchone()[0]
            Registry(conn).record_event(Actor(1), physical_id, 'returned',
                datetime(2026,9,11,12,tzinfo=timezone(timedelta(hours=9))), 'fixture-direct-return',
                'customer confirmed return', {'return_reason': '返品受領'})
            # Real app has no item_status column. Keep this fixture aligned.
            self.assertNotIn('item_status', {row[1] for row in conn.execute('PRAGMA table_info(merchandise)')})
        self.assertEqual(self.record(item_id)['sale_date'], '2026-09-10')
        self.assertEqual(self.quota()['active_items'], 1)

    def test_foreign_keys_enabled_on_existing_handlers(self):
        with self.service.connection(write=True) as (_, cur):
            self.assertEqual(cur.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        with self.custody.db(write=True) as (_, cur):
            self.assertEqual(cur.execute('PRAGMA foreign_keys').fetchone()[0], 1)


class Stage2MobileBasicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        mobile_fixtures.MobileApiTest.setUpClass.__func__(cls)
    setUp = mobile_fixtures.MobileApiTest.setUp
    tearDown = mobile_fixtures.MobileApiTest.tearDown
    db = mobile_fixtures.MobileApiTest.db
    login = mobile_fixtures.MobileApiTest.login
    auth = mobile_fixtures.MobileApiTest.auth
    metrics = mobile_fixtures.MobileApiTest.metrics
    announcement_rows = mobile_fixtures.MobileApiTest.announcement_rows

    def test_native_overdue_customer_can_login_and_read_when_stage2_enabled(self):
        self.app.config['STAGE2_INVENTORY_ENABLED'] = True
        auth = self.auth(6)
        response = self.client.get('/api/mobile/v1/items', headers=auth)
        self.assertEqual(response.status_code, 200)

    def test_native_old_mutations_cannot_bypass_registry_or_photo_ledger(self):
        self.app.config['STAGE2_INVENTORY_ENABLED'] = True
        auth = self.auth(3)
        self.assertEqual(self.client.patch('/api/mobile/v1/items/11', headers=auth,
                         json={'product_name': 'bypass'}).status_code, 409)
        self.assertEqual(self.client.post('/api/mobile/v1/items/11/photo', headers=auth).status_code, 409)


class Stage2PlanTests(unittest.TestCase):
    def test_basic_feature_does_not_query_paid_catalog_or_grant_dealer_sales(self):
        app = Flask('plan-fixture')
        app.config['STAGE2_INVENTORY_ENABLED'] = True
        plans = FeaturePlans(SimpleNamespace(app=app, DATABASE_URL=None, get_db=lambda: self.fail('basic access must not query billing')))
        plans.enabled = True
        user = SimpleNamespace(is_authenticated=True, id=1, role='user', is_admin=lambda: False)
        with app.test_request_context('/'):
            self.assertTrue(plans.has_feature('inventory_manage', user))
            with patch.object(plans, 'snapshot', return_value={'features': []}):
                self.assertFalse(plans.has_feature('dealer_sales', user))

if __name__ == '__main__':
    unittest.main()
