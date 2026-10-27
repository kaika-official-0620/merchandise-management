import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import date, datetime

from business_schema import create_test_schema
from item_registry import Actor, Registry, RegistryError
from item_registry_schema import migrate_registry, migration_statements


TIME = '2026-10-22T01:00:00+00:00'


def fixture(stage1=False):
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT)')
    db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),product_name TEXT,purchase_price INTEGER,storage_start_date TEXT)')
    db.executemany('INSERT INTO users VALUES (?,?,?)', [(1, 'user', None), (2, 'user', None), (9, 'owner', None),
        (8, 'admin', '["merchandise"]'), (7, 'admin', '["users"]'), (6, 'admin', '{broken')])
    db.executemany('INSERT INTO merchandise VALUES (?,?,?,?,?)', [(11, 1, '旧商品A', 30000, None), (12, 1, '旧商品B', None, None), (21, 2, '旧商品C', 45000, '2026-09-01')])
    if stage1:
        create_test_schema(db)
    migrate_registry(db)
    return db, Registry(db)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.db, self.registry = fixture()
        self.addCleanup(self.db.close)
        self.customer = Actor(1)
        self.staff = Actor(9, True)

    def create(self, key='create-a', customer=1, custodian='self'):
        return self.registry.create_item(self.staff if customer != 1 or custodian != 'self' else self.customer,
            '商品A', owner_user_id=customer, custodian=custodian, occurred_at=TIME, evidence_reference='fixture-inventory', source_key=key)

    def event(self, item, kind, minute=1, actor=None, **details):
        return self.registry.record_event(actor or self.staff, item['physical_item_id'], kind,
            '2026-10-22T01:%02d:00+00:00' % minute, 'event:' + kind + ':' + str(minute), 'fixture-event', details)

    def assertCode(self, code, callback):
        with self.assertRaises(RegistryError) as error:
            callback()
        self.assertEqual(error.exception.code, code)

    def test_create_generates_uuid_without_chosen_label_or_any_charge(self):
        item = self.create()
        self.assertTrue(item['physical_item_id'].startswith('ki_'))
        self.assertIsNone(item['label_code'])
        self.assertIsNone(item['external_label'])
        self.assertEqual(item['deadline_status'], 'unknown')
        self.assertEqual(item['physical_state'], 'with_customer')
        self.assertEqual(item['events'], [])
        self.assertEqual(item['works'], [])
        self.assertFalse(item['charge_created'])
        names = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('business_charge_drafts', names)
        self.assertNotIn('business_fund_records', names)

    def test_customer_cannot_choose_another_owner_or_assert_staff(self):
        self.assertCode('cannot_assign_other_customer', lambda: self.registry.create_item(self.customer, 'x', 2,
            occurred_at=TIME, evidence_reference='x', source_key='other'))
        self.assertCode('staff_permission_required', lambda: self.registry.list_items(Actor(1, True)))
        self.assertCode('staff_permission_required', lambda: self.registry.reconcile(Actor(7, True)))
        self.assertCode('staff_permission_required', lambda: self.registry.reconcile(Actor(6, True)))
        self.assertEqual(self.registry.reconcile(Actor(8, True))['legacy_row_count'], 3)

    def test_customer_isolation_on_list_and_detail(self):
        first = self.create()
        second = self.create('other', 2)
        self.assertEqual([x['physical_item_id'] for x in self.registry.list_items(self.customer)], [first['physical_item_id']])
        self.assertCode('item_not_found', lambda: self.registry.detail(self.customer, second['physical_item_id']))

    def test_idempotency_and_changed_request_and_actor(self):
        first = self.create()
        self.assertEqual(self.create()['physical_item_id'], first['physical_item_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_items').fetchone()[0], 1)
        self.assertCode('idempotency_conflict', lambda: self.registry.create_item(self.customer, 'changed',
            occurred_at=TIME, evidence_reference='fixture-inventory', source_key='create-a'))
        self.assertCode('operation_not_owned', lambda: self.registry.operation('create-a', Actor(2)))

    def test_attach_legacy_unknown_does_not_create_historical_work_or_deadline(self):
        item = self.registry.attach_legacy(self.staff, 11, occurred_at=TIME, evidence_reference='old-list', source_key='attach')
        self.assertEqual(item['custodian'], 'unknown')
        self.assertEqual(item['physical_state'], 'unknown')
        self.assertEqual(item['legacy_ids'], [11])
        self.assertEqual(item['imported_fields']['purchase_price'], 30000)
        self.assertEqual(item['events'], [])
        self.assertEqual(item['works'], [])
        self.assertIsNone(item['received_at'])
        self.assertIsNone(item['registration_completed_at'])
        self.assertIsNone(item['deadline_at'])
        replay = self.registry.attach_legacy(self.staff, 11, occurred_at=TIME, evidence_reference='second-check', source_key='attach2')
        self.assertEqual(item['physical_item_id'], replay['physical_item_id'])
        self.assertEqual(len(replay['legacy_snapshots']), 1)
        self.assertEqual(self.db.execute('SELECT purchase_price FROM merchandise WHERE id=11').fetchone()[0], 30000)

    def test_legacy_cross_customer_and_relink_denied(self):
        item = self.create()
        self.assertCode('legacy_not_found', lambda: self.registry.attach_legacy(self.customer, 21,
            physical_item_id=item['physical_item_id'], occurred_at=TIME, evidence_reference='x', source_key='forbidden'))
        self.assertCode('legacy_owner_mismatch', lambda: self.registry.attach_legacy(self.staff, 21,
            physical_item_id=item['physical_item_id'], occurred_at=TIME, evidence_reference='x', source_key='mismatch'))
        self.registry.attach_legacy(self.staff, 11, physical_item_id=item['physical_item_id'], occurred_at=TIME, evidence_reference='x', source_key='link')
        second = self.create('second')
        self.assertCode('legacy_already_linked', lambda: self.registry.attach_legacy(self.staff, 11,
            physical_item_id=second['physical_item_id'], occurred_at=TIME, evidence_reference='x', source_key='relink'))

    def test_manual_same_item_links_preserve_single_physical_id(self):
        item = self.create()
        for number in (11, 12):
            self.registry.attach_legacy(self.staff, number, physical_item_id=item['physical_item_id'], occurred_at=TIME, evidence_reference='actual-label-inspection', source_key='match:' + str(number))
        self.registry.verify_identity(self.staff, item['physical_item_id'], evidence_reference='checked-two-legacy-records-one-object', occurred_at=TIME, source_key='verify', external_label='既存-009')
        result = self.registry.reconcile(self.staff)
        self.assertEqual(result['linked_row_count'], 2)
        self.assertEqual(result['physical_item_count'], 1)
        self.assertEqual(result['unresolved_count'], 1)
        detail = self.registry.detail(self.customer, item['physical_item_id'])
        self.assertEqual(detail['external_label'], '既存-009')
        self.assertIsNone(detail['identity_evidence'])

    def test_receipt_changes_custodian_without_duplication_or_fee(self):
        item = self.create()
        self.event(item, 'expected', actor=self.customer)
        received = self.event(item, 'received', 2)
        self.assertEqual(received['custodian'], 'kaika')
        self.assertEqual(received['physical_state'], 'received')
        self.assertEqual(len(self.registry.list_items(self.staff)), 1)
        self.assertEqual(received['works'], [])
        self.assertCode('staff_work_required', lambda: self.registry.record_work(self.customer, item['physical_item_id'], 'photography', TIME, 'work-no', 'bad'))

    def test_staff_receipt_required(self):
        item = self.create()
        self.assertCode('staff_receipt_required', lambda: self.event(item, 'received', actor=self.customer))
        self.assertEqual(self.registry.detail(self.customer, item['physical_item_id'])['physical_state'], 'with_customer')

    def test_work_actual_or_unknown_no_estimate_inserted(self):
        item = self.create()
        work = self.registry.record_work(self.customer, item['physical_item_id'], 'photography', TIME, 'work1', 'actual-photo')
        self.assertIsNone(work['duration_seconds'])
        measured = self.registry.record_work(self.customer, item['physical_item_id'], 'registration', TIME, 'work2', 'actual-registration', 125, 'timer')
        self.assertEqual(measured['duration_seconds'], 125)
        self.assertEqual(len(self.registry.detail(self.customer, item['physical_item_id'])['works']), 2)
        self.assertCode('actual_duration_required', lambda: self.registry.record_work(self.customer, item['physical_item_id'], 'registration', TIME, 'invalid', 'x', 2.5, 'observed'))
        self.assertCode('actual_duration_required', lambda: self.registry.record_work(self.customer, item['physical_item_id'], 'registration', TIME, 'invalid2', 'x', True, 'observed'))

    def test_rework_requires_reason_and_matching_item(self):
        item, other = self.create(), self.create('other')
        work = self.registry.record_work(self.customer, item['physical_item_id'], 'photography', TIME, 'w1', 'photo')
        self.assertCode('invalid_rework_reason', lambda: self.registry.record_work(self.customer, item['physical_item_id'], 'photography', TIME, 'w2', 'again', rework_of=work['work_id']))
        self.assertCode('rework_item_mismatch', lambda: self.registry.record_work(self.customer, other['physical_item_id'], 'photography', TIME, 'w3', 'again', rework_of=work['work_id'], rework_reason='blur'))

    def test_sale_shipping_return_same_item_and_not_double_registration(self):
        item = self.create()
        self.event(item, 'sold', 1, actor=self.customer)
        self.event(item, 'shipped', 2, actor=self.customer)
        returned = self.event(item, 'returned', 3, actor=self.customer, return_reason='サイズ違い')
        self.assertEqual(returned['physical_item_id'], item['physical_item_id'])
        self.assertEqual(returned['physical_state'], 'with_customer')
        self.assertEqual(returned['sales_state'], 'unlisted')
        self.assertEqual(returned['held'], 1)
        self.assertCode('item_on_hold', lambda: self.event(item, 'sold', 4, actor=self.customer))
        self.event(item, 'release_hold', 5, actor=self.customer, note='状態を確認')
        self.event(item, 'sold', 6, actor=self.customer)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_items').fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM business_item_events WHERE kind='registration_completed'").fetchone()[0], 0)

    def test_invalid_shipping_rolls_back_and_event_idempotent(self):
        item = self.create()
        self.assertCode('sale_before_shipping_required', lambda: self.event(item, 'shipped', actor=self.customer))
        self.assertEqual(self.registry.detail(self.customer, item['physical_item_id'])['events'], [])
        self.event(item, 'sold', actor=self.customer)
        self.event(item, 'sold', actor=self.customer)
        self.assertEqual(len(self.registry.detail(self.customer, item['physical_item_id'])['events']), 1)

    def test_owner_change_hides_old_private_record_and_preserves_history(self):
        item = self.registry.attach_legacy(self.staff, 11, occurred_at=TIME, evidence_reference='old-seller-private-note', source_key='legacy')
        self.event(item, 'received', 1)
        self.event(item, 'hold', 2, note='旧顧客への私的連絡')
        self.registry.record_work(self.staff, item['physical_item_id'], 'support', TIME, 'private-work', '旧顧客の住所')
        self.registry.set_deadline(self.staff, item['physical_item_id'], deadline_at='2026-12-01T00:00:00+09:00', evidence_reference='old-contract', source_key='deadline')
        changed = self.registry.change_assignment(self.staff, item['physical_item_id'], 'owner', 'customer', 2,
            occurred_at='2026-10-22T02:00:00+00:00', evidence_reference='manual-title-transfer', source_key='transfer')
        after = self.registry.detail(Actor(2), item['physical_item_id'])
        self.assertEqual(after['events'], [])
        self.assertEqual(after['works'], [])
        self.assertEqual(after['imported_fields'], {})
        self.assertEqual(after['legacy_ids'], [])
        self.assertEqual(after['deadline_status'], 'unknown')
        self.assertNotIn('旧顧客', json.dumps(after, ensure_ascii=False))
        self.assertCode('item_not_found', lambda: self.registry.detail(self.customer, item['physical_item_id']))
        self.assertEqual(sum(x['role'] == 'owner' for x in changed['assignment_history']), 2)

    def test_append_only_db_constraints(self):
        item = self.create()
        self.registry.record_work(self.customer, item['physical_item_id'], 'registration', TIME, 'immutable', 'x')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM business_work_records')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE business_item_assignments SET evidence_reference='rewrite'")

    def test_event_time_and_unknown_custody_fail_closed(self):
        item = self.create()
        self.assertCode('timezone_required', lambda: self.registry.record_event(self.customer, item['physical_item_id'], 'sold', '2026-10-22', 'badtime', 'x'))
        unknown = self.create('unknown', custodian='unknown')
        self.assertCode('staff_work_required', lambda: self.event(unknown, 'sold', actor=self.customer))

    def test_migration_compatible_with_stage1_tables_and_no_fee_backfill(self):
        db, registry = fixture(stage1=True)
        self.addCleanup(db.close)
        registry.attach_legacy(Actor(9, True), 11, occurred_at=TIME, evidence_reference='fixture', source_key='legacy')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM business_charge_drafts').fetchone()[0], 0)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM business_fund_records').fetchone()[0], 0)
        migrate_registry(db)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM merchandise').fetchone()[0], 3)

    def test_schema_refuses_file_unless_exact_temp_fixture(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.sqlite'
            db = sqlite3.connect(path)
            try:
                db.execute('PRAGMA foreign_keys=ON')
                db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY)')
                db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY)')
                with self.assertRaises(ValueError):
                    migrate_registry(db)
                migrate_registry(db, allowed_test_path=path)
                with self.assertRaises(ValueError):
                    migrate_registry(db, mode='production', allowed_test_path=path)
                with self.assertRaises(ValueError):
                    migrate_registry(db, dialect='postgres', allowed_test_path=path)
            finally:
                db.close()

    def test_postgres_migration_is_sql_only_without_sqlite_functions(self):
        statements = '\n'.join(migration_statements('postgres'))
        self.assertNotIn('julianday(', statements)
        self.assertNotIn('typeof(', statements)
        self.assertNotIn('datetime(', statements)
        self.assertNotIn('RAISE(ABORT', statements)
        self.assertIn('FOR EACH ROW', statements)
        self.assertNotIn('UPDATE merchandise', statements)

    def test_no_automatic_connection_or_migration_constructor(self):
        class NoCalls:
            def cursor(self):
                raise AssertionError('unexpected DB access')
        Registry(NoCalls())

    def test_legacy_dates_are_retained_without_invented_timezone(self):
        item = self.registry.create_item(self.staff, '過去商品', owner_user_id=1, custodian='unknown', occurred_at=TIME,
            evidence_reference='historic-csv', source_key='historical-date', origin='legacy_import',
            imported_fields={'purchase_date': date(2026, 9, 1), 'received_at': datetime(2026, 9, 2, 14, 30)})
        self.assertEqual(item['imported_fields']['purchase_date'], '2026-09-01')
        self.assertEqual(item['imported_fields']['received_at'], '2026-09-02T14:30:00')
        self.assertIsNone(item['received_at'])

    def test_deadline_and_identity_review_history_is_append_only(self):
        item = self.create()
        for index in (1, 2):
            self.registry.set_deadline(self.staff, item['physical_item_id'], deadline_at='2026-12-0%dT00:00:00+09:00' % index,
                evidence_reference='contract-v%d' % index, source_key='deadline%d' % index)
        history = self.registry.detail(self.staff, item['physical_item_id'])['review_history']
        self.assertEqual(len(history), 2)
        second = next(row for row in history if row['source_key'] == 'deadline2')
        self.assertEqual(json.loads(second['before_json'])['deadline_evidence'], 'contract-v1')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM business_item_reviews')

    def test_backdated_work_cannot_be_attributed_to_new_owner(self):
        item = self.create()
        self.registry.change_assignment(self.staff, item['physical_item_id'], 'owner', 'customer', 2,
            occurred_at='2026-10-22T02:00:00+00:00', evidence_reference='transfer', source_key='transfer')
        self.assertCode('work_owner_period_mismatch', lambda: self.registry.record_work(self.staff,
            item['physical_item_id'], 'photography', TIME, 'old-owner-work', 'old-date'))

    def test_return_to_customer_requires_separate_delivery_confirmation(self):
        item = self.create()
        self.event(item, 'received', 1)
        dispatched = self.event(item, 'return_shipped', 2)
        self.assertEqual(dispatched['custodian'], 'kaika')
        self.assertEqual(dispatched['physical_state'], 'return_in_transit')
        delivered = self.event(item, 'return_received', 3)
        self.assertEqual(delivered['custodian'], 'customer')
        self.assertEqual(delivered['physical_state'], 'with_customer')

    def test_title_update_is_authorized_audited_and_repeat_safe(self):
        item = self.create()
        for _ in range(2):
            updated = self.registry.update_title(self.customer, item['physical_item_id'], '訂正した名称',
                occurred_at=TIME, evidence_reference='self-edit', source_key='title-edit')
        self.assertEqual(updated['title'], '訂正した名称')
        history = self.registry.detail(self.staff, item['physical_item_id'])['review_history']
        self.assertEqual(len(history), 1)
        self.assertEqual(json.loads(history[0]['before_json'])['title'], '商品A')
        self.event(item, 'hold', note='確認中')
        self.assertCode('item_on_hold', lambda: self.registry.update_title(self.customer, item['physical_item_id'], 'また変更',
            occurred_at=TIME, evidence_reference='held', source_key='held-title'))

    def test_direct_write_without_explicit_migration_is_domain_hold(self):
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT)')
        db.execute("INSERT INTO users VALUES(1,'user')")
        registry = Registry(db)
        self.assertCode('registry_migration_required', lambda: registry.record_work(Actor(1), 'missing',
            'photography', TIME, 'missing-schema', 'fixture'))

    def test_repeat_registration_does_not_reset_completion_date(self):
        item = self.create()
        self.event(item, 'photographed', 1, actor=self.customer)
        completed = self.event(item, 'registration_completed', 2, actor=self.customer)
        self.event(item, 'registration_completed', 2, actor=self.customer)
        self.assertCode('invalid_workflow_transition', lambda: self.event(item, 'registration_completed', 3, actor=self.customer))
        current = self.registry.detail(self.customer, item['physical_item_id'])
        self.assertEqual(current['registration_completed_at'], completed['registration_completed_at'])
        self.assertEqual(sum(event['kind'] == 'registration_completed' for event in current['events']), 1)

    def test_company_inventory_cannot_be_misclassified_as_customer_owned(self):
        self.assertCode('customer_owner_required', lambda: self.registry.create_item(self.staff, '開花自己在庫',
            owner_user_id=9, occurred_at=TIME, evidence_reference='company', source_key='company-create'))
        self.db.execute('ALTER TABLE merchandise ADD COLUMN scope TEXT')
        self.db.execute("UPDATE merchandise SET scope='user'")
        self.db.execute("INSERT INTO merchandise(id,user_id,product_name,scope) VALUES(91,9,'開花商品','admin')")
        self.db.execute("INSERT INTO merchandise(id,user_id,product_name,scope) VALUES(92,1,'区分不整合商品','admin')")
        self.db.execute("INSERT INTO merchandise(id,user_id,product_name,scope) VALUES(93,1,'区分未確認商品',NULL)")
        self.assertCode('customer_owner_required', lambda: self.registry.attach_legacy(self.staff, 91,
            occurred_at=TIME, evidence_reference='company', source_key='company-link'))
        self.assertCode('customer_inventory_scope_required', lambda: self.registry.attach_legacy(self.staff, 92,
            occurred_at=TIME, evidence_reference='company', source_key='scope-link'))
        self.assertCode('customer_inventory_scope_required', lambda: self.registry.attach_legacy(self.staff, 93,
            occurred_at=TIME, evidence_reference='unknown-scope', source_key='unknown-scope-link'))
        report = self.registry.reconcile(self.staff)
        self.assertEqual(report['legacy_row_count'], 3)
        self.assertFalse(any(row['id'] in (91, 92, 93) for row in report['rows']))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_items').fetchone()[0], 0)

    def test_incomplete_optional_listing_schema_holds_return_instead_of_server_error(self):
        item = self.create()
        self.event(item, 'received')
        self.db.execute('CREATE TABLE stage4_channel_listings(listing_id TEXT,physical_item_id TEXT)')
        self.db.execute('INSERT INTO stage4_channel_listings VALUES (?,?)', ('fixture-listing', item['physical_item_id']))
        self.assertCode('return_listing_stop_schema_required', lambda: self.event(item, 'return_shipped', minute=2))
        self.assertEqual(self.registry.detail(self.customer, item['physical_item_id'])['physical_state'], 'received')

    def test_same_time_sale_return_resale_preserves_append_order_when_clock_repeats(self):
        item = self.create()
        kinds = ['sold', 'shipped', 'returned', 'release_hold', 'sold']
        with patch('item_registry._now', return_value='2026-10-26T02:00:00.000000+00:00'):
            for index, kind in enumerate(kinds):
                details = {'return_reason': 'fixture'} if kind == 'returned' else {'note': 'fixture'} if kind == 'release_hold' else {}
                self.registry.record_event(self.customer, item['physical_item_id'], kind,
                    TIME, 'same-time-' + str(index), 'fixture', details)
        rows = self.registry.query('''SELECT kind,occurred_at,recorded_at FROM business_item_observations
            WHERE physical_item_id=? ORDER BY occurred_at,recorded_at,observation_id''', (item['physical_item_id'],))
        self.assertEqual([row['kind'] for row in rows], kinds)
        self.assertEqual(len({row['recorded_at'] for row in rows}), len(kinds))
        self.assertEqual(len({row['occurred_at'] for row in rows}), 1)
        with patch('item_registry._now', return_value='2026-10-26T01:00:00.000000+00:00'):
            self.registry.record_event(self.customer, item['physical_item_id'], 'shipped', TIME, 'clock-backwards', 'fixture')
        latest = self.registry.query('SELECT kind,recorded_at FROM business_item_observations ORDER BY recorded_at DESC LIMIT 1')[0]
        self.assertEqual(latest['kind'], 'shipped')
        self.assertGreater(latest['recorded_at'], rows[-1]['recorded_at'])

    def test_future_actual_records_do_not_change_item_owner_work_or_state(self):
        self.registry.clock = lambda: '2026-10-22T02:00:00+00:00'
        item = self.create()
        item_id = item['physical_item_id']
        future = '2026-10-22T02:00:00.000001+00:00'
        args = dict(occurred_at=future, evidence_reference='fixture', source_key='future')
        actions = [
            lambda: self.registry.create_item(self.customer, '未来の商品', **args),
            lambda: self.registry.attach_legacy(self.staff, 12, **args),
            lambda: self.registry.verify_identity(self.staff, item_id, **args),
            lambda: self.registry.change_assignment(self.staff, item_id, 'owner', 'customer', 2, **args),
            lambda: self.registry.update_title(self.customer, item_id, '未来の名称', **args),
            lambda: self.registry.record_event(self.staff, item_id, 'received', future, 'future', 'fixture'),
            lambda: self.registry.record_work(self.customer, item_id, 'photography', future, 'future', 'fixture',
                duration_seconds=60, measurement_source='observed'),
        ]
        for action in actions:
            with self.subTest(action=actions.index(action)):
                self.assertCode('registry_time_in_future', action)
        self.assertEqual(self.registry.detail(self.customer, item_id), item)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_work_records').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_items').fetchone()[0], 1)
        self.registry.record_event(self.staff, item_id, 'received', self.registry.clock(), 'now', 'fixture')
        self.assertEqual(self.registry.detail(self.customer, item_id)['physical_state'], 'received')

    def test_future_deadline_is_a_plan_not_a_completed_operation(self):
        self.registry.clock = lambda: TIME
        item = self.create()
        result = self.registry.set_deadline(self.staff, item['physical_item_id'],
            deadline_at='2026-12-01T00:00:00+09:00', evidence_reference='fixture-contract', source_key='deadline')
        self.assertEqual(result['deadline_status'], 'confirmed')
        self.assertEqual(result['events'], [])

    def test_manual_owner_change_cannot_assign_an_admin_as_customer(self):
        item = self.create()
        self.assertCode('customer_owner_required', lambda: self.registry.change_assignment(self.staff, item['physical_item_id'],
            'owner', 'customer', 9, occurred_at=TIME, evidence_reference='unsupported-company-transfer', source_key='company-transfer'))
        self.assertEqual(self.registry.detail(self.staff, item['physical_item_id'])['owner_user_id'], 1)


if __name__ == '__main__':
    unittest.main()
