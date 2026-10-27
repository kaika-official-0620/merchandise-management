import sqlite3
import unittest

from item_registry import Actor, Registry, RegistryError
from item_registry_schema import migrate_registry
from stage3_common import FinanceStore, migration_statements as common_sql
from stage3_profit import ProfitService, MONEY_FIELDS, shipping_summary
from stage3_profit_schema import migration_statements
from stage3_billing_schema import migration_statements as billing_sql


def time(minute=0):
    return '2026-10-22T01:%02d:00+00:00' % minute


class ProfitTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT)')
        self.db.executemany('INSERT INTO users VALUES (?,?,?)', [(1, 'user', None), (2, 'user', None), (9, 'owner', None), (8, 'admin', '["merchandise"]')])
        self.db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),product_name TEXT,purchase_price INTEGER)')
        self.db.execute("INSERT INTO merchandise VALUES(11,1,'旧0円',0)")
        migrate_registry(self.db)
        for statement in common_sql() + list(migration_statements()):
            self.db.execute(statement)
        self.registry = Registry(self.db)
        self.store = FinanceStore(self.registry)
        self.service = ProfitService(self.store)
        self.staff, self.customer, self.other = Actor(9, True), Actor(1), Actor(2)
        self.item = self.registry.create_item(self.customer, 'fixture', occurred_at=time(), evidence_reference='fixture', source_key='item')['physical_item_id']

    def record(self, key='facts', **changes):
        data = dict(amounts=dict(purchase_cost_yen=30000, sale_gross_yen=40000, channel_fee_yen=4000,
                                materials_yen=100, kaika_fee_yen=1155, return_adjustment_yen=0, monthly_allocation_yen=None),
                    channel='multichannel', source_reference='fixture-statement', occurred_at=time(10), source_key=key,
                    shipping_entries=[dict(source_id='carrier-one', amount_yen=800, origin='external', source_reference='carrier-proof')],
                    channel_fee_includes_shipping=False)
        data.update(changes)
        return self.service.record_facts(self.staff, self.item, **data)

    def event(self, kind, minute=1, **details):
        return self.registry.record_event(self.staff, self.item, kind, time(minute), kind + str(minute), 'fixture-event', details)

    def status(self, status, minute=20, **changes):
        values = dict(status=status, occurred_at=time(minute), source_key='status-' + status + str(minute), source_reference='fixture-status')
        values.update(changes)
        return self.service.record_sales_status(self.staff, self.item, **values)

    def assertCode(self, code, callback):
        with self.assertRaises(RegistryError) as error:
            callback()
        self.assertEqual(error.exception.code, code)

    def test_unrecorded_legacy_zero_remains_unknown(self):
        legacy = self.registry.attach_legacy(self.staff, 11, occurred_at=time(), evidence_reference='old', source_key='legacy')
        self.assertEqual(legacy['imported_fields']['purchase_price'], 0)
        result = self.service.snapshot(self.customer, legacy['physical_item_id'])
        self.assertIsNone(result['purchase_cost_yen'])
        self.assertIsNone(result['sale_gross_yen'])
        self.assertIn('financial_facts_not_recorded', result['review_reasons'])

    def test_explicit_zero_is_known_and_different_from_missing(self):
        self.record(amounts={name: 0 for name in MONEY_FIELDS}, shipping_entries=[])
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['profit_excluding_monthly_yen'], 0)
        self.assertEqual(result['profit_including_monthly_yen'], 0)
        self.assertIsNone(result['sales_margin_percent'])
        self.assertIsNone(result['cost_return_percent'])
        self.assertEqual(result['recognition'], 'estimate')

    def test_missing_cost_holds_total_without_turning_unknown_into_zero(self):
        self.record(amounts=dict(purchase_cost_yen=30000, sale_gross_yen=40000))
        result = self.service.profit(self.customer, self.item)
        self.assertIsNone(result['profit_excluding_monthly_yen'])
        self.assertIn('kaika_fee_yen', result['missing_amounts'])
        self.assertIn('return_adjustment_yen', result['missing_amounts'])

    def test_exact_integer_profit_and_separate_denominators(self):
        self.record()
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['profit_excluding_monthly_yen'], 3945)
        self.assertIsNone(result['profit_including_monthly_yen'])
        self.assertEqual(result['sales_margin_percent'], '9.86')
        self.assertEqual(result['cost_return_percent'], '13.15')
        self.assertIsNone(result['realized_profit_yen'])

    def test_monthly_cost_is_explicit_separate_allocation(self):
        amounts = dict(purchase_cost_yen=30000, sale_gross_yen=40000, channel_fee_yen=4000, materials_yen=100,
                       kaika_fee_yen=1155, return_adjustment_yen=0, monthly_allocation_yen=3278)
        self.record(amounts=amounts)
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['profit_excluding_monthly_yen'], 3945)
        self.assertEqual(result['profit_including_monthly_yen'], 667)
        self.assertEqual(result['monthly_cost_return_percent'], '2.22')

    def test_negative_return_adjustment_reduces_profit(self):
        amounts = {name: 0 for name in MONEY_FIELDS}
        amounts.update(purchase_cost_yen=1000, sale_gross_yen=1200, return_adjustment_yen=-300)
        self.record(amounts=amounts, shipping_entries=[])
        self.assertEqual(self.service.profit(self.customer, self.item)['profit_excluding_monthly_yen'], -100)

    def test_source_deduplication_of_shipping_is_independent_of_origin(self):
        entries = [dict(source_id='same-tracking', amount_yen=800, origin=origin, source_reference='statement-' + origin) for origin in ('external', 'staff')]
        self.record(shipping_entries=entries)
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['facts']['shipping_cost_yen'], 800)
        self.assertEqual(result['profit_excluding_monthly_yen'], 3945)
        self.assertEqual(result['facts']['shipping_duplicate_source_ids'], ['same-tracking'])

    def test_conflicting_shipping_sources_hold_profit(self):
        entries = [dict(source_id='same', amount_yen=800, origin='external', source_reference='external'),
                   dict(source_id='same', amount_yen=900, origin='staff', source_reference='staff')]
        self.record(shipping_entries=entries)
        result = self.service.profit(self.customer, self.item)
        self.assertIsNone(result['profit_excluding_monthly_yen'])
        self.assertIn('shipping_source_amount_conflict', result['review_reasons'])

    def test_inclusive_external_fee_and_shipping_requires_decomposition(self):
        self.record(channel_fee_includes_shipping=True)
        result = self.service.profit(self.customer, self.item)
        self.assertIsNone(result['profit_excluding_monthly_yen'])
        self.assertIn('channel_fee_shipping_double_count', result['review_reasons'])

    def test_unknown_external_fee_scope_not_assumed_separate(self):
        self.record(channel_fee_includes_shipping=None)
        self.assertIn('channel_fee_shipping_scope_unknown', self.service.profit(self.customer, self.item)['review_reasons'])

    def test_none_shipping_and_explicit_empty_are_distinct(self):
        self.assertIsNone(shipping_summary(None)['total_yen'])
        self.assertEqual(shipping_summary([])['total_yen'], 0)

    def test_correction_requires_current_version_and_keeps_history(self):
        first = self.record()
        second = self.record('correction', previous_version_id=first['version_id'], occurred_at=time(11), amounts={'purchase_cost_yen': 35000})
        history = self.service.history(self.customer, self.item)
        self.assertEqual([entry['purchase_cost_yen'] for entry in history], [30000, 35000])
        self.assertIsNone(history[1]['sale_gross_yen'])
        self.assertEqual(second['previous_version_id'], first['version_id'])
        self.assertCode('financial_revision_conflict', lambda: self.record('stale', previous_version_id=first['version_id']))

    def test_duplicate_submission_replays_exact_record(self):
        first = self.record()
        self.assertEqual(first, self.record())
        self.assertEqual(len(self.service.history(self.customer, self.item)), 1)
        self.assertCode('source_key_conflict', lambda: self.record(amounts={'sale_gross_yen': 999}))

    def test_permission_is_derived_from_database_not_staff_boolean(self):
        self.record()
        self.assertCode('item_not_found', lambda: self.service.snapshot(self.other, self.item))
        self.assertCode('staff_permission_required', lambda: self.service.snapshot(Actor(1, True), self.item))
        self.assertCode('finance_permission_required', lambda: self.service.snapshot(Actor(8, True), self.item))
        self.assertCode('finance_permission_required', lambda: self.service.record_facts(self.customer, self.item, amounts={}, channel=None,
            source_reference='not-staff', occurred_at=time(11), source_key='forbidden'))

    def test_new_owner_cannot_read_predecessor_money_or_revise_predecessor(self):
        first = self.record()
        self.registry.change_assignment(self.staff, self.item, 'owner', 'customer', 2, occurred_at=time(12), evidence_reference='fixture-transfer', source_key='transfer')
        result = self.service.snapshot(self.other, self.item)
        self.assertIsNone(result['version_id'])
        self.assertEqual(self.service.history(self.other, self.item), [])
        self.assertCode('item_not_found', lambda: self.service.snapshot(self.customer, self.item))
        self.assertCode('financial_revision_conflict', lambda: self.record('wrong-chain', previous_version_id=first['version_id'], occurred_at=time(13)))

    def test_same_customer_receiving_item_back_has_new_financial_epoch(self):
        self.record()
        self.registry.change_assignment(self.staff, self.item, 'owner', 'customer', 2, occurred_at=time(12), evidence_reference='transfer', source_key='transfer')
        self.registry.change_assignment(self.staff, self.item, 'owner', 'customer', 1, occurred_at=time(13), evidence_reference='transferback', source_key='transferback')
        self.assertIsNone(self.service.snapshot(self.customer, self.item)['purchase_cost_yen'])

    def test_timezones_and_revision_chronology_are_mandatory(self):
        self.assertCode('timezone_required', lambda: self.record(occurred_at='2026-10-22T01:10:00'))
        first = self.record()
        self.assertCode('financial_revision_time_reversed', lambda: self.record('backdate', previous_version_id=first['version_id'], occurred_at=time(9)))

    def test_floats_strings_bool_negative_costs_and_overflow_rejected(self):
        for bad in (1.0, '10', True, -1, 9000000000001):
            with self.subTest(bad=bad):
                self.assertCode('invalid_purchase_cost_yen', lambda: self.record(amounts={'purchase_cost_yen': bad}))

    def test_sql_append_only_money_facts(self):
        first = self.record()
        for statement in ('UPDATE stage3_item_financial_facts SET purchase_cost_yen=0', 'DELETE FROM stage3_item_financial_facts'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(statement)
        self.assertEqual(self.service.snapshot(self.customer, self.item)['version_id'], first['version_id'])

    def test_sale_binding_cannot_use_another_items_event(self):
        other = self.registry.create_item(self.staff, 'other', owner_user_id=2, occurred_at=time(), evidence_reference='other', source_key='other')['physical_item_id']
        event = self.registry.record_event(self.staff, other, 'sold', time(1), 'othersale', 'other-sale')['events'][-1]['event_id']
        self.assertCode('financial_sale_event_mismatch', lambda: self.record(sale_event_id=event))

    def test_sale_paid_ready_requires_shipment_and_does_not_credit_funds(self):
        self.event('sold', 1)
        self.record()
        self.status('unpaid', 20)
        self.status('paid', 21)
        self.assertCode('shipped_sale_amount_required', lambda: self.status('settlement_ready', 22))
        self.event('shipped', 23)
        self.status('settlement_ready', 24)
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['recognition'], 'realized')
        self.assertEqual(result['realized_profit_yen'], 3945)
        self.assertEqual(result['purchase_funds_credit_yen'], 0)

    def test_legacy_ambiguous_sale_cycle_holds_profit_and_financial_updates(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        facts = self.record()
        self.status('unpaid', 20)
        self.status('paid', 21)
        self.status('settlement_ready', 22)
        self.assertEqual(self.service.profit(self.customer,self.item)['recognition'],'realized')
        shipment = self.db.execute("SELECT occurred_at,recorded_at FROM business_item_observations WHERE physical_item_id=? AND kind='shipped'",
            (self.item,)).fetchone()
        self.db.execute('''INSERT INTO business_item_observations VALUES
            ('legacy-ambiguous-return',?,'returned',?,?,9,1,'fictional-old-log','{}',NULL,'legacy-tie')''',
            (self.item,*shipment))
        result = self.service.profit(self.customer,self.item)
        self.assertEqual(result['recognition'],'needs_review')
        self.assertIsNone(result['realized_profit_yen'])
        self.assertIn('financial_event_order_unconfirmed',result['review_reasons'])
        self.assertCode('financial_event_order_unconfirmed',lambda:self.status('return_hold',23))
        self.assertCode('financial_event_order_unconfirmed',lambda:self.record('revised',
            previous_version_id=facts['version_id'],occurred_at=time(24)))
        self.assertEqual(len(self.service.history(self.customer,self.item)),1)
        self.assertFalse(result['real_payment_enabled'])
        self.assertEqual(self.db.execute('SELECT SUM(purchase_funds_credit_yen) FROM stage3_sale_status_records').fetchone()[0], 0)

    def test_unpaid_not_confused_with_realized_profit(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        self.record()
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['profit_excluding_monthly_yen'], 3945)
        self.assertIsNone(result['realized_profit_yen'])
        self.assertEqual(result['recognition'], 'awaiting_payment_review')

    def test_status_update_requires_actual_sale_and_valid_transition(self):
        self.record(basis='estimate')
        self.assertCode('actual_sale_evidence_required', lambda: self.status('unpaid'))

    def test_skip_paid_status_cannot_settle(self):
        self.event('sold', 1)
        self.record()
        self.assertCode('invalid_sale_payment_transition', lambda: self.status('settlement_ready'))

    def test_status_replay_immutable_and_monotonic(self):
        self.event('sold', 1)
        self.record()
        first = self.status('unpaid', 20)
        self.assertEqual(first, self.status('unpaid', 20))
        self.assertCode('sale_status_time_reversed', lambda: self.status('paid', 19))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE stage3_sale_status_records SET status='paid'")

    def test_returns_hold_previous_settlement_and_require_resolution_evidence(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        self.record()
        self.status('unpaid', 20)
        self.status('paid', 21)
        self.status('settlement_ready', 22)
        self.event('returned', 23, return_reason='buyer-return')
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['recognition'], 'return_hold')
        self.assertIsNone(result['realized_profit_yen'])
        self.status('return_hold', 24)
        self.assertCode('return_resolution_required', lambda: self.status('unpaid', 25))
        self.status('unpaid', 25, resolution_reference='refund-record-reference')
        self.assertCode('returned_sale_cannot_settle', lambda: self.status('paid', 26))

    def test_resale_requires_new_sale_financial_binding(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        first = self.record()
        self.event('returned', 11, return_reason='returned')
        self.event('release_hold', 12, note='返品の現物確認済み')
        self.event('sold', 13)
        self.assertCode('financial_sale_event_stale', lambda: self.status('unpaid', 20))
        self.assertCode('financial_sale_event_stale', lambda: self.record('old-sale', previous_version_id=first['version_id'], occurred_at=time(14), sale_event_id=first['sale_event_id']))
        second = self.record('resale', previous_version_id=first['version_id'], occurred_at=time(14))
        self.assertNotEqual(first['sale_event_id'], second['sale_event_id'])
        self.assertEqual(len(self.service.history(self.customer, self.item)), 2)

    def test_unknown_work_time_is_not_filled_with_experience(self):
        self.registry.record_work(self.customer, self.item, 'photography', time(1), 'work-unknown', 'photo')
        self.registry.record_work(self.customer, self.item, 'registration', time(2), 'work-known', 'registration', 120, 'timer')
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['known_work_seconds'], 120)
        self.assertEqual(result['unknown_work_duration_count'], 1)
        self.assertIsNone(result['work_cost_yen'])

    def test_staff_profit_time_excludes_former_owners_work(self):
        self.registry.record_work(self.customer, self.item, 'photography', time(1), 'former-unknown', 'photo')
        self.registry.record_work(self.customer, self.item, 'registration', time(2), 'former-known', 'registration', 120, 'timer')
        self.registry.change_assignment(self.staff, self.item, 'owner', 'customer', 2, occurred_at=time(3), evidence_reference='transfer', source_key='transfer')
        self.registry.record_work(self.staff, self.item, 'photography', time(4), 'current-known', 'photo', 45, 'timer')
        self.assertEqual(len(self.registry.detail(self.staff, self.item)['works']), 3)
        for actor in (self.staff, self.other):
            result = self.service.profit(actor, self.item)
            self.assertEqual(result['known_work_seconds'], 45)
            self.assertEqual(result['unknown_work_duration_count'], 0)

    def test_work_time_for_returning_owner_excludes_previous_ownership_period(self):
        self.registry.record_work(self.customer, self.item, 'photography', time(1), 'old-unknown', 'photo')
        self.registry.record_work(self.customer, self.item, 'registration', time(2), 'old-known', 'registration', 120, 'timer')
        self.registry.change_assignment(self.staff, self.item, 'owner', 'customer', 2, occurred_at=time(3), evidence_reference='transfer', source_key='transfer')
        self.registry.record_work(self.staff, self.item, 'photography', time(4), 'interim-known', 'photo', 45, 'timer')
        self.registry.change_assignment(self.staff, self.item, 'owner', 'customer', 1, occurred_at=time(5), evidence_reference='transfer-back', source_key='transfer-back')
        self.registry.record_work(self.customer, self.item, 'photography', time(6), 'returned-known', 'photo', 30, 'timer')
        self.registry.record_work(self.customer, self.item, 'registration', time(7), 'returned-unknown', 'registration')
        for actor in (self.staff, self.customer):
            result = self.service.profit(actor, self.item)
            self.assertEqual(result['known_work_seconds'], 30)
            self.assertEqual(result['unknown_work_duration_count'], 1)

    def test_financial_correction_cannot_reuse_old_settlement_evidence(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        first = self.record()
        self.status('unpaid', 20)
        self.status('paid', 21)
        self.status('settlement_ready', 22)
        self.record('corrected', previous_version_id=first['version_id'], occurred_at=time(23))
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['recognition'], 'awaiting_payment_review')
        self.assertIsNone(result['realized_profit_yen'])
        self.assertIn('sale_status_fact_version_outdated', result['review_reasons'])
        self.assertCode('invalid_sale_payment_transition', lambda: self.status('settlement_ready', 24))
        self.status('paid', 25)
        self.status('settlement_ready', 26)
        self.assertEqual(self.service.profit(self.customer, self.item)['realized_profit_yen'], 3945)

    def test_revision_after_paid_requires_new_payment_evidence_before_ready(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        first = self.record()
        self.status('unpaid', 20)
        self.status('paid', 21)
        self.record('corrected', previous_version_id=first['version_id'], occurred_at=time(22))
        self.assertCode('revised_sale_payment_review_required', lambda: self.status('settlement_ready', 23))

    def test_sql_foreign_owner_and_version_chain_rejected(self):
        first = self.record()
        columns = [column[1] for column in self.db.execute('PRAGMA table_info(stage3_item_financial_facts)')]
        row = dict(zip(columns, self.db.execute('SELECT * FROM stage3_item_financial_facts').fetchone()))
        row.update(version_id='forged', revision_number=2, previous_version_id=first['version_id'], source_event_key='forged', customer_id=2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO stage3_item_financial_facts(' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))

    def test_sql_status_cannot_fork_initial_chain(self):
        self.event('sold', 1)
        self.record()
        self.status('unpaid', 20)
        columns = [column[1] for column in self.db.execute('PRAGMA table_info(stage3_sale_status_records)')]
        row = dict(zip(columns, self.db.execute('SELECT * FROM stage3_sale_status_records').fetchone()))
        row.update(status_record_id='forged', source_event_key='forged')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('INSERT INTO stage3_sale_status_records(' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))

    def billing_draft(self, kind='expense', customer_id=1, item_id=None, draft_id='pending-draft'):
        for statement in billing_sql():
            self.db.execute(statement)
        self.db.execute('''INSERT INTO stage3_charge_drafts(draft_id,customer_id,physical_item_id,fee_kind,
            amount_net_yen,tax_rate_percent,tax_treatment,occurred_at,recorded_at,evidence_reference,
            snapshot_json,reasons_json,source_key,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (draft_id, customer_id, item_id or self.item, kind, 500, 10, 'exclusive', time(10), time(10),
             'fixture-fee-proof', '{}', '["component_terms_unconfirmed"]', draft_id, 'needs_review'))

    def test_manual_zero_kaika_fee_cannot_bypass_pending_product_fee_draft(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        amounts = dict(purchase_cost_yen=30000, sale_gross_yen=40000, channel_fee_yen=4000,
                       materials_yen=100, kaika_fee_yen=0, return_adjustment_yen=0)
        self.record(amounts=amounts)
        self.status('unpaid', 20)
        self.status('paid', 21)
        self.status('settlement_ready', 22)
        self.assertEqual(self.service.profit(self.customer, self.item)['realized_profit_yen'], 5100)
        self.billing_draft()
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['profit_excluding_monthly_yen'], 5100)
        self.assertIsNone(result['realized_profit_yen'])
        self.assertEqual(result['recognition'], 'needs_review')
        self.assertIn('kaika_fee_draft_pending', result['review_reasons'])
        self.assertEqual(result['billing_reconciliation']['pending_draft_ids'], ['pending-draft'])
        self.assertEqual(result['facts']['kaika_fee_yen'], 0)

    def test_monthly_draft_not_mixed_into_product_fees(self):
        self.record()
        self.billing_draft(kind='digital_monthly')
        result = self.service.profit(self.customer, self.item)
        self.assertNotIn('kaika_fee_draft_pending', result['review_reasons'])
        self.assertEqual(result['billing_reconciliation']['pending_draft_count'], 0)

    def test_another_customers_draft_ids_are_not_disclosed(self):
        self.record()
        self.billing_draft(customer_id=2)
        result = self.service.profit(self.customer, self.item)
        self.assertEqual(result['billing_reconciliation']['pending_draft_ids'], [])

    def test_missing_billing_schema_is_checked_before_query(self):
        self.record()
        result = self.service.profit(self.customer, self.item)
        self.assertFalse(result['billing_reconciliation']['available'])
        self.assertEqual(result['profit_excluding_monthly_yen'], 3945)

    def test_postgres_sql_is_review_only_and_uses_bigint(self):
        statements = '\n'.join(migration_statements('postgres'))
        self.assertNotIn('typeof(', statements)
        self.assertIn('purchase_cost_yen BIGINT', statements)
        self.assertIn('stage3_profit_fact_guard', statements)
        self.assertIn('stage3_profit_status_guard', statements)


if __name__ == '__main__':
    unittest.main()
