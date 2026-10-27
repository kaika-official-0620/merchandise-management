"""Fictional SQLite fixtures only. No application startup, external I/O or money."""
import json
import sqlite3
import unittest

from item_registry import Actor, Registry, RegistryError, _json
from item_registry_schema import migrate_registry
from stage3_common import FinanceStore, migration_statements as common_statements
from stage3_billing import BillingService, _catalog, fee_proposal, invoice_totals, round_fraction
from stage3_billing_schema import migration_statements
from stage3_profit import ProfitService
from stage3_profit_schema import migration_statements as profit_statements


def at(day, hour=0):
    return '2026-10-%02dT%02d:00:00+09:00' % (day, hour)


def terms(**changes):
    evidence = dict(contract_version_id='fictional-contract-v1', fee_version_id='fictional-fee-v1',
        signed_document_reference='fixture-signed', consent_reference='fixture-consent',
        consent_at=at(1), charge_start_at=at(10), exception_status='none',
        tax_rate_percent=10, rounding_method='floor', rounding_scope='invoice_per_rate',
        billing_timezone='Asia/Tokyo', billing_timezone_approved=True,
        work_scope_confirmed=True, work_kinds=['photography', 'registration', 'packing', 'dispatch'],
        approval_reference='fixture-only-approval', approved_at=at(1), effective_from=at(1),
        channel_authorization_references={'multichannel': 'fixture-authorized', 'self_sale_shipping': 'fixture-authorized'},
        sale_price_basis_confirmed=True)
    evidence.update(changes)
    return evidence


class TaxTests(unittest.TestCase):
    def test_source_fee_price_boundaries(self):
        expected = [(19999, 500, 500), (20000, 650, 1000), (49999, 650, 1000),
                    (50000, 800, 1500), (99999, 800, 1500), (100000, 1000, 2000),
                    (199999, 1000, 2000), (200000, 1000, 3000)]
        for price, work, sales in expected:
            quote = fee_proposal(price, 'wholesale')
            self.assertEqual((quote['work_bundle_net_yen'], quote['sales_fee_net_yen']), (work, sales))
            self.assertFalse(quote['chargeable'])

    def test_wholesale_shipping_30000_30001(self):
        self.assertEqual(fee_proposal(30000, 'wholesale')['wholesale_shipping_payer_proposal'], 'customer')
        self.assertEqual(fee_proposal(30001, 'wholesale')['wholesale_shipping_payer_proposal'], 'kaika')

    def test_decimal_tax_rounded_once_per_rate(self):
        result = invoice_totals([{'amount_net_yen': 5, 'tax_treatment': 'exclusive', 'tax_rate_percent': 10}] * 3, 'floor')
        self.assertEqual(result['tax_total_yen'], 1)  # Per-line flooring would incorrectly produce 0.
        self.assertEqual(result['reference_gross_total_yen'], 16)
        self.assertIsNone(result['payable_total_yen'])

    def test_each_rate_aggregates_separately(self):
        result = invoice_totals([{'amount_net_yen': 9, 'tax_treatment': 'exclusive', 'tax_rate_percent': rate}
                                 for rate in (8, 8, 10, 10)], 'floor')
        self.assertEqual(result['tax_total_yen'], 2)
        self.assertEqual(len(result['groups']), 2)

    def test_missing_amount_or_rate_or_rounding_never_becomes_zero(self):
        for amount, rate, method in ((None, 10, 'floor'), (500, None, 'floor'), (500, 10, None)):
            result = invoice_totals([{'amount_net_yen': amount, 'tax_treatment': 'exclusive', 'tax_rate_percent': rate}], method)
            self.assertIsNone(result['reference_gross_total_yen'])

    def test_monetary_types_and_size_rejected(self):
        for value in (True, 1.5, '500', 9000000000001):
            with self.assertRaises(RegistryError):
                invoice_totals([{'amount_net_yen': value, 'tax_treatment': 'exclusive', 'tax_rate_percent': 10}], 'floor')

    def test_negative_credit_fraction_rounds_without_float(self):
        self.assertEqual(round_fraction(-15, 10, 'half_up'), -2)
        self.assertEqual(round_fraction(-11, 10, 'ceil'), -1)
        self.assertEqual(round_fraction(-11, 10, 'floor'), -2)

    def test_credit_only_group_keeps_tax_unknown_until_original_tax_policy_decided(self):
        result = invoice_totals([{'amount_net_yen': -11, 'tax_treatment': 'exclusive', 'tax_rate_percent': 10}], 'floor')
        self.assertEqual(result['known_net_subtotal_yen'], -11)
        self.assertIsNone(result['groups'][0]['tax_yen'])
        self.assertIsNone(result['tax_total_yen'])
        self.assertIsNone(result['reference_gross_total_yen'])
        self.assertIn('credit_tax_rule_unconfirmed', result['review_reasons'])

    def test_positive_remaining_invoice_group_aggregates_credit_before_tax(self):
        result = invoice_totals([{'amount_net_yen': amount, 'tax_treatment': 'exclusive', 'tax_rate_percent': 10}
                                 for amount in (100, -11)], 'floor')
        self.assertEqual(result['net_total_yen'], 89)
        self.assertEqual(result['tax_total_yen'], 8)
        self.assertEqual(result['reference_gross_total_yen'], 97)
        self.assertNotIn('credit_tax_rule_unconfirmed', result['review_reasons'])

    def test_conditional_reference_examples(self):
        def line(amount):
            return {'amount_net_yen': amount, 'tax_treatment': 'exclusive', 'tax_rate_percent': 10}
        self.assertEqual(invoice_totals([line(650), line(400)], 'floor')['reference_gross_total_yen'], 1155)
        for monthly, count, expected in ((2980, 10, 14828), (2980, 20, 26378), (5980, 21, 30833)):
            self.assertEqual(invoice_totals([line(monthly)] + [line(1050)] * count, 'floor')['reference_gross_total_yen'], expected)


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT)')
        self.db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),product_name TEXT)')
        self.db.executemany('INSERT INTO users VALUES (?,?,?)', [(1, 'user', None), (2, 'user', None), (9, 'owner', None),
            (8, 'admin', '["merchandise"]'), (7, 'admin', '["invoices"]')])
        migrate_registry(self.db)
        for sql in common_statements() + migration_statements() + list(profit_statements()):
            self.db.execute(sql)
        self.registry = Registry(self.db)
        self.store = FinanceStore(self.registry)
        self.billing = BillingService(self.store)
        self.profit = ProfitService(self.store)
        self.staff, self.customer = Actor(9, True), Actor(1)
        self.sequence = 0

    def agreement(self, approved=True, customer=1, **changes):
        self.sequence += 1
        if not approved:
            return self.billing.save_agreement(self.staff, customer, evidence=terms(**changes), source_key='ag' + str(self.sequence))['agreement_id']
        identifier = 'fixture-ag-' + str(self.sequence)
        # Direct, clearly labelled fixture seeding; runtime APIs cannot approve.
        self.db.execute('''INSERT INTO stage3_agreements(agreement_id,customer_id,actor_user_id,recorded_at,evidence_json,catalog_json,status,source_key)
            VALUES (?,?,9,?,?,?,'fixture_approved',?)''', (identifier, customer, at(1), _json(terms(**changes)), _json(_catalog()), identifier))
        return identifier

    def item(self, *, works=(11, 12), customer=1, custody='kaika', ship=True, sale_price=30000, channel='multichannel'):
        self.sequence += 1
        prefix = str(self.sequence)
        item = self.registry.create_item(self.staff, '架空商品' + prefix, owner_user_id=customer,
            custodian='self', occurred_at=at(2), evidence_reference='fixture-new', source_key='item' + prefix)
        identifier = item['physical_item_id']
        if custody == 'kaika':
            self.registry.record_event(self.staff, identifier, 'received', at(3), 'received' + prefix, 'fixture-receipt')
        for index, day in enumerate(works):
            self.registry.record_work(self.staff if custody == 'kaika' else Actor(customer), identifier,
                'photography' if index == 0 else 'registration', at(day), 'work' + prefix + ':' + str(index), 'fixture-work')
        self.registry.record_event(self.staff if custody == 'kaika' else Actor(customer), identifier, 'sold', at(20), 'sold' + prefix, 'fixture-sale')
        self.profit.record_facts(self.staff, identifier, amounts={'sale_gross_yen': sale_price}, channel=channel,
            source_reference='fixture-actual-sale', occurred_at=at(20, 1), source_key='facts' + prefix)
        if ship:
            self.registry.record_event(self.staff if custody == 'kaika' else Actor(customer), identifier, 'shipped', at(21), 'shipped' + prefix, 'fixture-dispatch')
        return identifier

    def collect(self, item, agreement=None, key='collect'):
        return self.billing.create_item_drafts(self.staff, item, source_key=key, agreement_id=agreement)

    def test_approved_fixture_full_bundle_and_sales_are_draft_not_payment(self):
        identifier, agreement = self.item(), self.agreement()
        preview = self.billing.preview_item(self.staff, identifier, agreement)
        self.assertEqual([line['amount_net_yen'] for line in preview['lines']], [650, 400])
        self.assertEqual([line['reason_codes'] for line in preview['lines']], [[], []])
        self.assertFalse(preview['real_payment_enabled'])

    def test_unapproved_agreement_cannot_become_approved_from_http_fields(self):
        identifier, agreement = self.item(), self.agreement(approved=False)
        self.assertIn('fee_policy_draft', self.billing.preview_item(self.staff, identifier, agreement)['lines'][0]['reason_codes'])
        with self.assertRaises(RegistryError):
            self.billing.save_agreement(self.staff, 1, evidence={'status': 'fixture_approved'}, source_key='malicious')

    def test_operator_tax_and_period_defaults_do_not_approve_customer_fees(self):
        self.billing.save_agreement(self.staff, 1, evidence={}, source_key='empty-ag')
        agreement = self.billing.list_agreements(self.staff, 1)[0]
        self.assertEqual(agreement['evidence']['rounding_method'], 'floor')
        self.assertEqual(agreement['evidence']['billing_timezone'], 'Asia/Tokyo')
        self.assertEqual(agreement['status'], 'draft')

    def test_malformed_agreement_references_and_work_kinds_are_validation_errors(self):
        for evidence in ({'fee_version_id': {'bad': 'object'}}, {'work_kinds': [{'bad': 'object'}]}, {'exception_status': 'approved'}):
            with self.assertRaises(RegistryError):
                self.billing.save_agreement(self.staff, 1, evidence=evidence, source_key='invalid')

    def test_all_pre_start_work_does_not_put_full_bundle_into_bill(self):
        preview = self.billing.preview_item(self.staff, self.item(works=(4, 5)), self.agreement())
        self.assertIn('pre_start_work_not_billable', preview['lines'][0]['reason_codes'])
        self.assertIsNone(preview['lines'][0]['amount_net_yen'])

    def test_mixed_start_only_post_part_pending_no_proration(self):
        preview = self.billing.preview_item(self.staff, self.item(works=(4, 12)), self.agreement())
        self.assertIn('mixed_start_bundle_unresolved', preview['lines'][0]['reason_codes'])
        self.assertIsNone(preview['lines'][0]['amount_net_yen'])
        self.assertEqual(preview['lines'][0]['proposed_full_amount_net_yen'], 650)

    def test_unknown_work_scope_blocks_final_eligibility(self):
        preview = self.billing.preview_item(self.staff, self.item(), self.agreement(work_scope_confirmed=False, work_kinds=None))
        self.assertIn('work_scope_unknown', preview['lines'][0]['reason_codes'])

    def test_unshipped_does_not_create_fee_rows(self):
        identifier = self.item(ship=False)
        with self.assertRaises(RegistryError):
            self.collect(identifier)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 0)

    def test_self_manage_and_self_work_do_not_acquire_kaika_fees(self):
        identifier = self.item(custody='self')
        result = self.collect(identifier)
        self.assertEqual(result['draft_ids'], [])
        self.assertIn('self_managed_no_kaika_fee', result['reason_codes'])

    def test_authorized_self_sale_shipping_sales_fee_zero_only(self):
        identifier = self.item(channel='self_sale_shipping')
        allowed = self.billing.preview_item(self.staff, identifier, self.agreement())
        self.assertEqual(allowed['lines'][1]['amount_net_yen'], 0)
        blocked = self.billing.preview_item(self.staff, identifier, self.agreement(channel_authorization_references={}))
        self.assertIsNone(blocked['lines'][1]['amount_net_yen'])

    def test_same_shipment_duplicated_keys_do_not_duplicate_fees(self):
        identifier, agreement = self.item(), self.agreement()
        first = self.collect(identifier, agreement)
        self.assertEqual(first, self.collect(identifier, agreement))
        self.assertEqual(first['draft_ids'], self.collect(identifier, agreement, 'different-click')['draft_ids'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 2)

    def test_operation_key_payload_conflict_rolls_back(self):
        identifier = self.item()
        self.collect(identifier)
        with self.assertRaises(RegistryError):
            self.collect(identifier, self.agreement())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 2)

    def test_owner_change_between_initial_read_and_locked_write_rejects_charge(self):
        identifier, agreement = self.item(), self.agreement()
        original_operation = self.store.operation
        def ownership_changed(actor, customer, source_key, payload, callback):
            self.registry.change_assignment(self.staff, identifier, 'owner', 'customer', 2,
                occurred_at=at(22), evidence_reference='fixture-concurrent-ownership', source_key='owner-moved')
            return original_operation(actor, customer, source_key, payload, callback)
        self.store.operation = ownership_changed
        with self.assertRaises(RegistryError) as error:
            self.collect(identifier, agreement)
        self.assertEqual(error.exception.code, 'financial_owner_changed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 0)

    def test_snapshot_remains_identical_after_source_price_change(self):
        identifier, agreement = self.item(), self.agreement()
        self.collect(identifier, agreement)
        before = self.db.execute('SELECT snapshot_json FROM stage3_charge_drafts ORDER BY draft_id').fetchall()
        current = self.profit.snapshot(self.staff, identifier)
        self.profit.record_facts(self.staff, identifier, amounts={'sale_gross_yen': 200000}, channel='multichannel',
            source_reference='fixture-corrected', occurred_at=at(22), source_key='corrected-facts', previous_version_id=current['version_id'])
        lines = self.billing.list_item_drafts(self.staff, identifier)
        self.assertTrue(all('source_changed_after_snapshot' in row['current_review_reasons'] for row in lines))
        self.assertEqual(before, self.db.execute('SELECT snapshot_json FROM stage3_charge_drafts ORDER BY draft_id').fetchall())
        self.assertEqual(sorted(row['amount_net_yen'] for row in lines), [400, 650])

    def test_returns_retain_original_and_need_review(self):
        identifier, agreement = self.item(), self.agreement()
        self.collect(identifier, agreement)
        self.registry.record_event(self.staff, identifier, 'returned', at(22), 'return', 'return-proof', {'return_reason': '不具合'})
        lines = self.billing.list_item_drafts(self.staff, identifier)
        self.assertTrue(all('source_changed_after_snapshot' in row['current_review_reasons'] for row in lines))
        preview = self.billing.preview_item(self.staff, identifier, agreement)
        self.assertIn('return_unresolved', preview['lines'][0]['reason_codes'])
        self.assertIsNone(preview['lines'][0]['amount_net_yen'])

    def test_same_timestamp_resale_cannot_reuse_previous_shipping_event(self):
        identifier, agreement = self.item(), self.agreement()
        self.registry.record_event(self.staff, identifier, 'returned', at(21), 'return', 'proof', {'return_reason': '不具合'})
        self.registry.record_event(self.staff, identifier, 'release_hold', at(21), 'release', 'proof', {'note': '再検品'})
        self.registry.record_event(self.staff, identifier, 'sold', at(21), 'resold', 'second-sale')
        preview = self.billing.preview_item(self.staff, identifier, agreement)
        self.assertIsNone(preview['shipment_event_id'])
        with self.assertRaises(RegistryError):
            self.collect(identifier, agreement)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 0)

    def legacy_ambiguous_return(self, identifier):
        # Fictional pre-fix data: same recorded time has no trustworthy order.
        shipment = self.db.execute("SELECT occurred_at,recorded_at FROM business_item_observations WHERE physical_item_id=? AND kind='shipped'",
            (identifier,)).fetchone()
        self.db.execute('''INSERT INTO business_item_observations VALUES
            ('legacy-ambiguous-return',?,'returned',?,?,9,1,'fictional-old-log','{}',NULL,'legacy-tie')''',
            (identifier,*shipment))

    def test_legacy_equal_recording_times_hold_new_fees_without_rewriting_history(self):
        identifier, agreement = self.item(), self.agreement()
        self.legacy_ambiguous_return(identifier)
        before = self.db.execute('SELECT * FROM business_item_observations ORDER BY observation_id').fetchall()
        preview = self.billing.preview_item(self.staff, identifier, agreement)
        self.assertIn('financial_event_order_unconfirmed', preview['lines'][0]['reason_codes'])
        self.assertIsNone(preview['shipment_event_id'])
        self.assertTrue(all(line['amount_net_yen'] is None for line in preview['lines']))
        with self.assertRaises(RegistryError):
            self.collect(identifier, agreement)
        self.assertEqual(before, self.db.execute('SELECT * FROM business_item_observations ORDER BY observation_id').fetchall())

    def test_legacy_equal_recording_times_mark_existing_invoice_for_review(self):
        identifier, agreement = self.item(), self.agreement()
        self.collect(identifier, agreement)
        month = self.billing.create_month(self.staff, 1, '2026-10', source_key='month')
        before = self.billing.get_month(self.customer, month['invoice_id'])['snapshot']
        self.legacy_ambiguous_return(identifier)
        invoice = self.billing.get_month(self.customer, month['invoice_id'])
        self.assertEqual(invoice['snapshot'], before)
        self.assertFalse(invoice['is_current_snapshot'])
        self.assertTrue(all('financial_event_order_unconfirmed' in reasons
            for reasons in invoice['current_line_review_reasons'].values()))

    def test_customer_separation_and_staff_permission(self):
        identifier = self.item()
        self.collect(identifier)
        self.assertEqual(len(self.billing.list_item_drafts(self.customer, identifier)), 2)
        for actor in (Actor(2), Actor(8, True), Actor(7, True)):
            with self.assertRaises(RegistryError):
                self.billing.list_item_drafts(actor, identifier)
        with self.assertRaises(RegistryError):
            self.billing.create_item_drafts(self.customer, identifier, source_key='customer-write')

    def test_foreign_agreement_rejected_even_by_staff(self):
        identifier = self.item()
        with self.assertRaises(RegistryError):
            self.collect(identifier, self.agreement(customer=2))

    def test_current_owner_does_not_inherit_previous_owner_work_or_facts(self):
        identifier = self.item()
        self.registry.change_assignment(self.staff, identifier, 'owner', 'customer', 2,
            occurred_at=at(22), evidence_reference='ownership-change', source_key='owner-change')
        preview = self.billing.preview_item(self.staff, identifier)
        self.assertIsNone(preview['shipment_event_id'])
        self.assertEqual(preview['work_snapshot'], [])
        self.assertIsNone(preview['sale_fact_snapshot']['sale_gross_yen'])

    def test_append_only_charge_and_agreement(self):
        identifier, agreement = self.item(), self.agreement()
        self.collect(identifier, agreement)
        for sql in ("UPDATE stage3_charge_drafts SET amount_net_yen=0", 'DELETE FROM stage3_charge_drafts',
                    "UPDATE stage3_agreements SET status='fixture_approved'", 'DELETE FROM stage3_agreements'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql)

    def component(self, *, amount=100, kind='expense', key='expense', when=None, item_id=None,
                  original=None, reference=None, agreement=None):
        return self.billing.record_component(self.staff, 1, fee_kind=kind, amount_net_yen=amount,
            tax_rate_percent=10, occurred_at=when or at(22), evidence_reference=reference or key,
            source_key=key, item_id=item_id, original_draft_id=original, agreement_id=agreement)

    def test_expense_same_evidence_no_double_charge(self):
        first = self.component(reference='receipt-line-1')
        replay = self.component(key='another', reference='receipt-line-1')
        self.assertEqual(first['draft_id'], replay['draft_id'])
        with self.assertRaises(RegistryError):
            self.component(key='changed', reference='receipt-line-1', amount=200)

    def test_expense_owner_change_between_initial_read_and_write_rejects_charge(self):
        identifier = self.item()
        operation = self.store.operation
        def transfer_before_operation(actor, customer, key, payload, callback):
            self.registry.change_assignment(self.staff, identifier, 'owner', 'customer', 2,
                occurred_at=at(22), evidence_reference='fixture-transfer', source_key='transfer')
            return operation(actor, customer, key, payload, callback)
        self.store.operation = transfer_before_operation
        with self.assertRaises(RegistryError) as error:
            self.component(item_id=identifier, when=at(23))
        self.assertEqual(error.exception.code, 'financial_owner_changed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 0)

    def test_expense_before_current_ownership_is_not_charged_to_new_customer(self):
        identifier = self.item(customer=2)
        self.registry.change_assignment(self.staff, identifier, 'owner', 'customer', 1,
            occurred_at=at(23), evidence_reference='fixture-transfer', source_key='transfer')
        with self.assertRaises(RegistryError) as error:
            self.component(item_id=identifier, when=at(22))
        self.assertEqual(error.exception.code, 'component_owner_period_mismatch')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 0)

    def test_expense_owner_roundtrip_between_read_and_write_is_also_rejected(self):
        identifier = self.item()
        operation = self.store.operation
        def transfer_before_operation(actor, customer, key, payload, callback):
            for owner,day in ((2,22),(1,23)):
                self.registry.change_assignment(self.staff, identifier, 'owner', 'customer', owner,
                    occurred_at=at(day), evidence_reference='fixture-transfer', source_key='transfer-'+str(owner))
            return operation(actor, customer, key, payload, callback)
        self.store.operation = transfer_before_operation
        with self.assertRaises(RegistryError) as error:
            self.component(item_id=identifier, when=at(24))
        self.assertEqual(error.exception.code, 'financial_owner_changed')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 0)

    def test_old_owner_correction_remains_possible_without_disclosing_new_owner(self):
        identifier = self.item()
        original = self.component(item_id=identifier, amount=500)
        original_row = self.db.execute('SELECT snapshot_json FROM stage3_charge_drafts WHERE draft_id=?',
            (original['draft_id'],)).fetchone()[0]
        self.registry.change_assignment(self.staff, identifier, 'owner', 'customer', 2,
            occurred_at=at(23), evidence_reference='new-owner-private-reference', source_key='transfer')
        correction = self.component(kind='adjustment', item_id=identifier, original=original['draft_id'],
            amount=-100, when=at(24), key='old-owner-credit')
        month = self.billing.create_month(self.staff, 1, '2026-10', source_key='old-owner-month')
        invoice = self.billing.get_month(self.customer, month['invoice_id'])
        self.assertEqual({line['customer_id'] for line in invoice['snapshot']['lines']}, {1})
        self.assertEqual(self.db.execute('SELECT SUM(amount_net_yen) FROM stage3_charge_drafts WHERE customer_id=1').fetchone()[0], 400)
        self.assertEqual(original_row, self.db.execute('SELECT snapshot_json FROM stage3_charge_drafts WHERE draft_id=?',
            (original['draft_id'],)).fetchone()[0])
        self.assertNotIn('new-owner-private-reference', _json(invoice))
        self.assertEqual(self.billing.list_item_drafts(Actor(2), identifier), [])
        with self.assertRaises(RegistryError):
            self.billing.get_month(Actor(2), month['invoice_id'])
        with self.assertRaises(RegistryError):
            self.billing.record_component(self.staff, 2, fee_kind='adjustment', amount_net_yen=-100,
                tax_rate_percent=10, occurred_at=at(24), evidence_reference='wrong-customer',
                source_key='wrong-customer', item_id=identifier, original_draft_id=original['draft_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 2)

    def test_correction_references_original_and_cannot_over_refund(self):
        original = self.component(amount=500)
        credit = self.component(kind='adjustment', amount=-300, key='credit1', original=original['draft_id'])
        self.assertNotEqual(credit['draft_id'], original['draft_id'])
        with self.assertRaises(RegistryError):
            self.component(kind='adjustment', amount=-201, key='credit2', original=original['draft_id'])
        self.component(kind='adjustment', amount=-200, key='credit2-ok', original=original['draft_id'])
        self.assertEqual(self.db.execute('SELECT SUM(amount_net_yen) FROM stage3_charge_drafts').fetchone()[0], 0)

    def test_correction_duplicate_source_under_new_operation_is_idempotent(self):
        original = self.component(amount=500)
        first = self.component(kind='adjustment', amount=-100, key='credit1', reference='refund-original', original=original['draft_id'])
        second = self.component(kind='adjustment', amount=-100, key='credit2', reference='refund-original', original=original['draft_id'])
        self.assertEqual(first['draft_id'], second['draft_id'])

    def test_correction_cannot_point_to_another_customer(self):
        other = self.billing.record_component(self.staff, 2, fee_kind='expense', amount_net_yen=500,
            tax_rate_percent=10, occurred_at=at(22), evidence_reference='other-receipt', source_key='other-receipt')
        with self.assertRaises(RegistryError):
            self.component(kind='adjustment', amount=-100, key='other-credit', original=other['draft_id'])

    def test_month_immutable_revision_and_late_information(self):
        identifier, agreement = self.item(), self.agreement()
        self.collect(identifier, agreement)
        first = self.billing.create_month(self.staff, 1, '2026-10', source_key='month1')
        first_snapshot = self.billing.get_month(self.customer, first['invoice_id'])['snapshot']
        self.assertEqual(first_snapshot['totals']['known_component_reference_gross_yen'], 1155)
        self.assertIsNone(first_snapshot['totals']['reference_gross_total_yen'])
        self.assertIn('monthly_component_missing', first_snapshot['reason_codes'])
        unchanged = self.billing.create_month(self.staff, 1, '2026-10', source_key='month-again')
        self.assertEqual(first['invoice_id'], unchanged['invoice_id'])
        self.component(amount=100)
        second = self.billing.create_month(self.staff, 1, '2026-10', source_key='month2')
        self.assertEqual(second['revision'], 2)
        self.assertEqual(self.billing.get_month(self.customer, first['invoice_id'])['snapshot'], first_snapshot)
        self.assertEqual(self.billing.get_month(self.customer, second['invoice_id'])['snapshot']['totals']['known_component_reference_gross_yen'], 1265)

    def test_month_return_changes_review_without_repricing_previous_snapshot(self):
        identifier, agreement = self.item(), self.agreement()
        self.collect(identifier, agreement)
        first = self.billing.create_month(self.staff, 1, '2026-10', source_key='month1')
        self.registry.record_event(self.staff, identifier, 'returned', at(22), 'return', 'proof', {'return_reason': '不具合'})
        second = self.billing.create_month(self.staff, 1, '2026-10', source_key='month2')
        self.assertEqual(second['revision'], 2)
        self.assertEqual(self.billing.get_month(self.customer, first['invoice_id'])['snapshot']['reason_codes'], ['monthly_component_missing'])
        self.assertIn('unresolved_adjustments', self.billing.get_month(self.customer, second['invoice_id'])['snapshot']['reason_codes'])

    def test_uncollected_shipments_are_visible_and_not_zero_bill(self):
        self.item()
        month = self.billing.create_month(self.staff, 1, '2026-10', source_key='uncollected')
        snapshot = self.billing.get_month(self.customer, month['invoice_id'])['snapshot']
        self.assertIn('uncollected_shipments', snapshot['reason_codes'])
        self.assertIsNone(snapshot['totals']['reference_gross_total_yen'])

    def test_saved_invoice_read_and_export_notice_return_without_regenerating(self):
        identifier, agreement = self.item(), self.agreement()
        self.collect(identifier, agreement)
        month = self.billing.create_month(self.staff, 1, '2026-10', source_key='before-return')
        self.registry.record_event(self.staff, identifier, 'returned', at(22), 'return', 'proof', {'return_reason': '不具合'})
        result = self.billing.get_month(self.customer, month['invoice_id'])
        self.assertFalse(result['is_current_snapshot'])
        self.assertIn('source_changed_after_snapshot', result['current_review_reasons'])
        self.assertIn('source_changed_after_snapshot', self.billing.csv_month(self.customer, month['invoice_id']))

    def test_saved_invoice_notices_late_monthly_line_without_regenerating(self):
        identifier, agreement = self.item(), self.agreement(monthly_net_yen=2980)
        self.collect(identifier, agreement)
        month = self.billing.create_month(self.staff, 1, '2026-10', source_key='before-monthly')
        self.component(amount=2980, kind='digital_monthly', key='monthly', agreement=agreement)
        result = self.billing.get_month(self.customer, month['invoice_id'])
        self.assertFalse(result['is_current_snapshot'])
        self.assertIn('invoice_new_lines_available', result['current_review_reasons'])
        self.assertIsNone(result['snapshot']['totals']['reference_gross_total_yen'])

    def test_saved_invoice_notices_new_shipment_without_any_collected_lines(self):
        month = self.billing.create_month(self.staff, 1, '2026-10', source_key='before-anything')
        self.item()
        result = self.billing.get_month(self.customer, month['invoice_id'])
        self.assertFalse(result['is_current_snapshot'])
        self.assertIn('uncollected_shipments', result['current_review_reasons'])

    def test_monthly_line_and_items_are_separate_amounts(self):
        identifier, agreement = self.item(), self.agreement(monthly_net_yen=2980)
        self.collect(identifier, agreement)
        self.component(amount=2980, kind='digital_monthly', key='monthly', agreement=agreement)
        month = self.billing.create_month(self.staff, 1, '2026-10', source_key='including-monthly')
        snapshot = self.billing.get_month(self.customer, month['invoice_id'])['snapshot']
        self.assertEqual(snapshot['totals']['reference_gross_total_yen'], 4433)
        self.assertIn('unresolved_adjustments', snapshot['reason_codes'])
        self.assertIsNone(snapshot['totals']['payable_total_yen'])

    def test_jst_month_boundary_is_not_utc_month(self):
        self.component(when='2026-10-31T14:59:59+00:00', key='oct-last')
        self.component(when='2026-10-31T15:00:00+00:00', key='nov-first')
        for month in ('2026-10', '2026-11'):
            result = self.billing.create_month(self.staff, 1, month, source_key='m' + month)
            self.assertEqual(len(self.billing.get_month(self.customer, result['invoice_id'])['snapshot']['lines']), 1)

    def test_csv_source_formula_escaped_and_cross_customer_denied(self):
        self.component(reference='=HYPERLINK("bad")')
        month = self.billing.create_month(self.staff, 1, '2026-10', source_key='csv')
        output = self.billing.csv_month(self.customer, month['invoice_id'])
        self.assertIn("'=HYPERLINK", output)
        self.assertIn('実請求,無効', output)
        with self.assertRaises(RegistryError):
            self.billing.csv_month(Actor(2), month['invoice_id'])

    def test_schema_has_no_live_or_finalized_state(self):
        original = self.component()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('''INSERT INTO stage3_charge_drafts(draft_id,customer_id,fee_kind,occurred_at,recorded_at,
                evidence_reference,snapshot_json,reasons_json,source_key,status,live_enabled)
                VALUES ('live',1,'expense',?,?,?,'{}','[]','live','finalized',1)''', (at(22), at(22), 'x'))

    def test_postgres_sql_is_review_only_and_has_reference_guards(self):
        sql = '\n'.join(migration_statements('postgres'))
        self.assertNotIn('typeof(', sql)
        self.assertIn('stage3_billing_reference_guard', sql)
        self.assertIn('stage3_invoice_reference_guard', sql)


if __name__ == '__main__':
    unittest.main()
