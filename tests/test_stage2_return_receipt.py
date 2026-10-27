"""Return delivery uses real HTTP/core/legacy audit on disposable SQLite."""
import sqlite3
import unittest

from flask_login import login_user

from inventory_custody import InventoryCustody
from item_registry import Actor, Registry
from stage2_self_management import SELF_INVENTORY_DDL, custody_ddl
from tests import test_inventory_registry_web as web_fixtures


class ReturnReceiptTests(unittest.TestCase):
    connect = web_fixtures.InventoryRegistryWebTests.connect
    login = web_fixtures.InventoryRegistryWebTests.login
    form = web_fixtures.InventoryRegistryWebTests.form
    item_form = web_fixtures.InventoryRegistryWebTests.item_form
    count = web_fixtures.InventoryRegistryWebTests.count

    def setUp(self):
        web_fixtures.InventoryRegistryWebTests.setUp(self)
        db = self.connect()
        for definition in ('scope TEXT DEFAULT \'user\'', 'custody_version INTEGER NOT NULL DEFAULT 1',
                'updated_by INTEGER', 'updated_at TEXT', 'sale_price INTEGER', 'shipping_cost INTEGER', 'commission INTEGER'):
            db.execute('ALTER TABLE merchandise ADD COLUMN ' + definition)
        for sql in SELF_INVENTORY_DDL + custody_ddl():
            db.execute(sql)
        db.execute("UPDATE merchandise SET custody_location='kaika',sale_price=76543,shipping_cost=789,commission=4321 WHERE id=11")
        registry = Registry(db)
        registry.attach_legacy(Actor(9, True), 11, physical_item_id=self.item,
            occurred_at='2026-10-21T01:00:00+00:00', evidence_reference='fixture-link', source_key='return-link')
        registry.record_event(Actor(9, True), self.item, 'received', '2026-10-21T02:00:00+00:00', 'return-received-at-kaika', 'fixture-receipt')
        registry.record_event(Actor(9, True), self.item, 'return_shipped', '2026-10-21T03:00:00+00:00', 'return-dispatch', 'fixture-return-dispatch')
        db.execute("INSERT INTO inventory_intakes(id,user_id,kind,status,expected_count,submission_hash) VALUES(10,1,'transfer','received',1,'fixture-original-intake')")
        db.execute('INSERT INTO inventory_intake_items(intake_id,merchandise_id) VALUES(10,11)')
        db.commit()
        db.close()
        self.app.extensions['kaika_feature_plans'] = object()
        self.custody = InventoryCustody(self.runtime)
        self.login()

    def payload(self):
        return dict(self.item_form('events'), kind='return_received', occurred_at='2026-10-22T12:00', note='本人の受取を確認した架空記録')

    def post(self, payload=None):
        return self.client.post('/inventory/items/' + self.item + '/events', data=payload or self.payload())

    def row(self, sql, params=()):
        db = self.connect()
        try:
            row = db.execute(sql, params).fetchone()
            return dict(row) if row else None
        finally:
            db.close()

    def assert_in_transit(self):
        self.assertEqual(self.row('SELECT physical_state FROM business_item_profiles WHERE physical_item_id=?', (self.item,))['physical_state'], 'return_in_transit')
        self.assertEqual(self.row('SELECT custody_location FROM merchandise WHERE id=11')['custody_location'], 'kaika')
        self.assertEqual(self.count('self_inventory_items'), 0)
        self.assertEqual(self.count('inventory_custody_events'), 0)

    def test_dispatch_alone_keeps_kaika_custody_and_no_self_entry(self):
        self.assert_in_transit()
        self.assertEqual(self.client.get('/inventory/items/' + self.item).status_code, 200)
        self.assert_in_transit()

    def test_confirm_same_item_self_entry_audit_and_original_money_retained(self):
        self.assertEqual(self.post().status_code, 302)
        row = self.row('SELECT * FROM merchandise WHERE id=11')
        self.assertEqual(row['custody_location'], 'self')
        self.assertEqual(row['custody_version'], 2)
        self.assertEqual((row['sale_price'], row['shipping_cost'], row['commission']), (76543, 789, 4321))
        self.assertEqual(self.row('SELECT * FROM self_inventory_items WHERE merchandise_id=11')['user_id'], 1)
        self.assertEqual(self.row('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=11')['physical_item_id'], self.item)
        audit = self.row('SELECT * FROM inventory_custody_events')
        self.assertEqual(audit['event_type'], 'return_received')
        self.assertEqual(audit['intake_id'], 10)
        self.assertEqual(self.row('SELECT status FROM inventory_intakes WHERE id=10')['status'], 'received')
        self.assertEqual(self.count('inventory_intake_items'), 1)
        self.assertEqual(self.count('business_work_records'), 0)

    def test_replay_does_not_duplicate_self_entry_versions_or_legacy_audit(self):
        payload = self.payload()
        self.assertEqual(self.post(payload).status_code, 302)
        self.assertEqual(self.post(payload).status_code, 302)
        self.assertEqual(self.count('self_inventory_items'), 1)
        self.assertEqual(self.count('inventory_custody_events'), 1)
        self.assertEqual(self.row('SELECT custody_version FROM merchandise WHERE id=11')['custody_version'], 2)

    def test_existing_self_entry_is_reused_and_its_edit_version_invalidated(self):
        db = self.connect()
        db.execute("INSERT INTO self_inventory_items(merchandise_id,user_id,submission_hash,version) VALUES(11,1,'existing-self-registration',4)")
        db.commit()
        db.close()
        self.assertEqual(self.post().status_code, 302)
        row = self.row('SELECT * FROM self_inventory_items WHERE merchandise_id=11')
        self.assertEqual(row['submission_hash'], 'existing-self-registration')
        self.assertEqual(row['version'], 5)
        self.assertEqual(self.count('self_inventory_items'), 1)

    def test_wrong_owner_in_any_link_rolls_back_all_state(self):
        db = self.connect()
        db.execute("UPDATE merchandise SET custody_location='kaika' WHERE id=21")
        db.execute("INSERT INTO business_item_links(merchandise_id,physical_item_id,link_status) VALUES(21,?,'unresolved')", (self.item,))
        db.commit()
        db.close()
        self.assertEqual(self.post().status_code, 409)
        self.assert_in_transit()

    def test_old_custody_or_scope_mismatch_is_rejected(self):
        db = self.connect()
        db.execute("UPDATE merchandise SET scope='admin' WHERE id=11")
        db.commit()
        db.close()
        self.assertEqual(self.post().status_code, 409)
        self.assert_in_transit()

    def test_failed_audit_insert_rolls_back_core_custody_and_self_registration(self):
        db = self.connect()
        db.execute("CREATE TRIGGER fixture_audit_failure BEFORE INSERT ON inventory_custody_events BEGIN SELECT RAISE(ABORT,'fixture audit failure'); END")
        db.commit()
        db.close()
        with self.assertRaises(sqlite3.IntegrityError):
            self.post()
        self.assert_in_transit()

    def test_received_return_releases_only_original_intake_and_new_intake_still_locks(self):
        self.assertEqual(self.post().status_code, 302)
        db = self.connect()
        try:
            self.assertIsNone(self.custody.active_for_item(db.cursor(), 11))
            with self.app.test_request_context('/'):
                login_user(web_fixtures.User(1))
                context = self.custody.item_context(self.row('SELECT * FROM merchandise WHERE id=11'))
                self.assertIsNone(context['intake'])
                self.assertTrue(context['can_self_manage'])
            db.execute("INSERT INTO inventory_intakes(id,user_id,kind,status,expected_count,submission_hash) VALUES(12,1,'transfer','requested',1,'fixture-new-intake')")
            db.execute('INSERT INTO inventory_intake_items(intake_id,merchandise_id) VALUES(12,11)')
            db.commit()
            self.assertEqual(self.custody.active_for_item(db.cursor(), 11)['id'], 12)
            with self.app.test_request_context('/'):
                login_user(web_fixtures.User(1))
                context = self.custody.item_context(self.row('SELECT * FROM merchandise WHERE id=11'))
                self.assertEqual(context['intake']['id'], 12)
                self.assertFalse(context['can_self_manage'])
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
