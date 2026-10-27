"""Fictional records, SQLite, and memory PSP only; no runtime import or network."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest

from item_registry import Actor, Registry, RegistryError, _json
from item_registry_schema import migrate_registry
from stage3_common import FinanceStore, migration_statements as finance_schema
from stage3_billing import BillingService, _catalog
from stage3_billing_schema import migration_statements as billing_schema
from stage4_common import migration_statements as commerce_schema
from stage5_common import Stage5Store, migration_statements as common_schema
from stage5_payments_schema import migration_statements
from stage5_payments import PhysicalPaymentService, MemoryTestPSP, RetrievedTestObject


STAFF, CUSTOMER, OTHER = Actor(9, True), Actor(1), Actor(2)
T = '2026-10-22T00:00:00+09:00'


def fixture(path=':memory:'):
    db = sqlite3.connect(path, timeout=20)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY, role TEXT, admin_permissions TEXT)')
    db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,product_name TEXT)')
    db.executemany('INSERT INTO users VALUES (?,?,?)', [(1, 'user', None), (2, 'user', None), (9, 'owner', None),
        (8, 'admin', '["merchandise"]'), (7, 'admin', '["invoices"]')])
    migrate_registry(db, allowed_test_path=path if path != ':memory:' else None)
    for sql in finance_schema() + billing_schema() + commerce_schema() + common_schema() + migration_statements():
        db.execute(sql)
    db.commit()
    return db


def approved_component(db, key, amount=1000, kind='expense', *, customer=1, reasons=(), original=None,
                       tax=10, period='2026-10', approved=True):
    """Direct TEST seeding; no HTTP/domain method approves a draft agreement."""
    evidence = dict(consent_reference='fixture-consent', signed_document_reference='fixture-contract',
        billing_timezone='Asia/Tokyo', billing_timezone_approved=True, rounding_scope='invoice_per_rate', rounding_method='floor')
    db.execute('''INSERT INTO stage3_agreements(agreement_id,customer_id,actor_user_id,recorded_at,
        evidence_json,catalog_json,status,source_key) VALUES (?,?,9,?,?,?,?,?)''',
        ('agreement-' + key, customer, T, _json(evidence), _json(_catalog()), 'fixture_approved' if approved else 'draft', 'agreement-' + key))
    store = FinanceStore(Registry(db))
    agreement = BillingService(store)._load_agreement(customer, 'agreement-' + key)
    snapshot = dict(agreement_snapshot=agreement, calculation={'rounding_method': 'floor'})
    db.execute('''INSERT INTO stage3_charge_drafts(draft_id,customer_id,agreement_id,fee_kind,amount_net_yen,
        tax_rate_percent,tax_treatment,occurred_at,recorded_at,evidence_reference,snapshot_json,reasons_json,source_key,status,original_draft_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (key, customer, 'agreement-' + key, kind, amount, tax, 'exclusive', period + '-22T00:00:00+09:00', T,
         'evidence-' + key, _json(snapshot), _json(list(reasons)), 'component-' + key, 'needs_review' if reasons else 'draft', original))
    return key


class PhysicalPaymentTests(unittest.TestCase):
    def setUp(self):
        self.db = fixture()
        self.addCleanup(self.db.close)
        self.store = Stage5Store(Registry(self.db))
        self.adapter = MemoryTestPSP()
        self.service = PhysicalPaymentService(self.store, self.adapter)
        self.billing = BillingService(self.store.finance)
        self.sequence = 0

    def key(self):
        self.sequence += 1
        return 'operation-' + str(self.sequence)

    def assertCode(self, code, action):
        with self.assertRaises(RegistryError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)

    def month(self, amount=1000, kind='expense', **changes):
        key = self.key()
        approved_component(self.db, key, amount, kind, **changes)
        return self.billing.create_month(STAFF, changes.get('customer', 1), changes.get('period', '2026-10'), source_key=self.key())['invoice_id']

    def bill(self, **changes):
        invoice = self.month(**changes)
        return self.service.prepare_invoice(STAFF, invoice, review_reference='fixture-review', source_key=self.key())['bill_id']

    def consent(self, customer=1, off_session=False):
        return self.service.record_consent(Actor(customer), customer, terms_reference=self.key(),
            payment_method_reference='pm_test_1', provider_customer_reference='cus_test_' + str(customer),
            off_session=off_session, source_key=self.key())['consent_id']

    def attempt(self, **changes):
        bill = self.bill()
        consent = self.consent(off_session=changes.get('off_session', False))
        return self.service.create_attempt(STAFF if changes.get('off_session') else CUSTOMER, bill, consent, source_key=self.key(), **changes)['attempt_id']

    def paid(self):
        attempt = self.attempt()
        self.adapter.next_payment_status = 'succeeded'
        self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key())
        return attempt

    def provider_key(self, attempt, refund=False):
        return self.db.execute('SELECT provider_idempotency_key FROM ' + ('stage5_payment_refunds WHERE refund_id=?' if refund else 'stage5_payment_attempts WHERE attempt_id=?'), (attempt,)).fetchone()[0]

    def test_physical_tax_separate_from_digital_and_due_next_month_tenth(self):
        approved_component(self.db, 'digital', 2980, 'digital_monthly', reasons=['monthly_terms_unconfirmed'])
        bill = self.service.get_bill(CUSTOMER, self.bill(amount=1050))
        self.assertEqual(bill['amount_yen'], 1155)
        self.assertEqual(bill['due_date'], '2026-11-10')
        self.assertIsNone(bill['due_cutoff_time'])
        self.assertFalse(bill['automatic_collection_enabled'])
        self.assertEqual([row['fee_kind'] for row in bill['lines']], ['expense'])

    def test_due_year_rollover(self):
        bill = self.service.get_bill(CUSTOMER, self.bill(period='2026-12'))
        self.assertEqual(bill['due_date'], '2027-01-10')

    def test_digital_adjustment_excluded_too(self):
        approved_component(self.db, 'digital', 2980, 'digital_monthly', reasons=['monthly_terms_unconfirmed'])
        approved_component(self.db, 'digital-adjust', -100, 'adjustment', original='digital', reasons=['correction_requires_review'])
        bill = self.service.get_bill(CUSTOMER, self.bill())
        self.assertEqual(bill['amount_yen'], 1100)
        self.assertEqual(len(bill['lines']), 1)

    def test_digital_only_is_not_physical_invoice(self):
        invoice = self.month(kind='digital_monthly')
        self.assertCode('physical_invoice_requires_review', lambda: self.service.prepare_invoice(STAFF, invoice, review_reference='review', source_key=self.key()))

    def test_later_month_digital_credit_is_not_a_physical_discount(self):
        approved_component(self.db, 'last-month-digital', 2980, 'digital_monthly', period='2026-09')
        approved_component(self.db, 'digital-adjust', -100, 'adjustment', original='last-month-digital', reasons=['correction_requires_review'])
        bill = self.service.get_bill(CUSTOMER, self.bill())
        self.assertEqual(bill['amount_yen'], 1100)
        self.assertEqual(len(bill['lines']), 1)

    def test_unknown_amount_blocked(self):
        invoice = self.month(amount=None)
        self.assertIn('physical_positive_known_total_required', self.service.inspect_invoice(STAFF, invoice)['review_reasons'])

    def test_zero_not_unknown_but_not_chargeable(self):
        invoice = self.month(amount=0)
        self.assertCode('physical_invoice_requires_review', lambda: self.service.prepare_invoice(STAFF, invoice, review_reference='review', source_key=self.key()))

    def test_unapproved_contract_blocked_even_with_forged_review_reference(self):
        invoice = self.month(approved=False)
        self.assertCode('physical_invoice_requires_review', lambda: self.service.prepare_invoice(STAFF, invoice, review_reference='approved', source_key=self.key()))

    def test_bundle_scope_unresolved_is_not_waived(self):
        invoice = self.month(kind='work_bundle', reasons=['work_scope_unconfirmed'])
        self.assertCode('physical_invoice_requires_review', lambda: self.service.prepare_invoice(STAFF, invoice, review_reference='review', source_key=self.key()))

    def test_unknown_tax_blocks(self):
        invoice = self.month(tax=None)
        self.assertIn('physical_tax_or_amount_unresolved', self.service.inspect_invoice(STAFF, invoice)['review_reasons'])

    def test_stale_month_cannot_prepare(self):
        invoice = self.month()
        approved_component(self.db, 'later', 100)
        self.assertCode('physical_invoice_requires_review', lambda: self.service.prepare_invoice(STAFF, invoice, review_reference='review', source_key=self.key()))

    def test_new_version_cannot_double_charge_same_month(self):
        self.bill()
        invoice = self.month(amount=100)
        self.assertCode('physical_period_already_prepared', lambda: self.service.prepare_invoice(STAFF, invoice, review_reference='review', source_key=self.key()))

    def test_duplicate_review_returns_same_bill(self):
        bill = self.bill()
        row = self.service.get_bill(STAFF, bill)
        again = self.service.prepare_invoice(STAFF, row['invoice_id'], review_reference='another-review', source_key=self.key())
        self.assertEqual(bill, again['bill_id'])

    def test_customer_cannot_approve_own_physical_bill(self):
        invoice = self.month()
        self.assertCode('staff_permission_required', lambda: self.service.prepare_invoice(CUSTOMER, invoice, review_reference='own', source_key=self.key()))

    def test_customer_and_permission_isolation(self):
        bill = self.bill()
        for actor in (OTHER, Actor(8, True), Actor(7, True)):
            with self.assertRaises(RegistryError):
                self.service.get_bill(actor, bill)
        self.assertEqual(self.service.overview(OTHER)['bills'], [])

    def test_test_consent_rejects_raw_or_live_payment_details(self):
        for reference in ('4242424242424242', 'pm_live_actual', 'pm_test_\nsecret'):
            self.assertCode('test_payment_reference_required', lambda: self.service.record_consent(CUSTOMER, 1,
                terms_reference='terms', payment_method_reference=reference, provider_customer_reference='cus_test_1', source_key=self.key()))

    def test_consent_other_customer_inaccessible_and_bind_checked(self):
        bill = self.bill()
        consent = self.consent(customer=2)
        self.assertCode('consent_customer_mismatch', lambda: self.service.create_attempt(STAFF, bill, consent, off_session=True, source_key=self.key()))
        self.assertCode('customer_access_denied', lambda: self.service.revoke_consent(CUSTOMER, consent, reason='x', source_key=self.key()))

    def test_off_session_requires_independent_consent(self):
        bill, consent = self.bill(), self.consent()
        self.assertCode('off_session_consent_required', lambda: self.service.create_attempt(STAFF, bill, consent, flow='off_session', source_key=self.key()))

    def test_staff_cannot_bypass_automatic_consent_by_claiming_customer_initiated(self):
        bill, consent = self.bill(), self.consent()
        for arguments in ({}, {'off_session': False}, {'flow': 'customer_initiated'}):
            self.assertCode('customer_payment_action_required', lambda: self.service.create_attempt(
                STAFF, bill, consent, source_key=self.key(), **arguments))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage5_payment_attempts').fetchone()[0], 0)
        self.assertEqual(self.adapter.create_calls, 0)

    def test_manual_dispatch_requires_customer_but_staff_can_reconcile_and_refund(self):
        attempt = self.attempt()
        self.assertCode('customer_payment_action_required', lambda: self.service.dispatch_attempt(STAFF, attempt, source_key=self.key()))
        self.assertEqual(self.adapter.create_calls, 0)
        self.assertEqual(self.service.get_attempt(STAFF, attempt)['state'], 'prepared')
        self.adapter.next_payment_status = 'succeeded'
        self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key())
        self.assertEqual(self.service.reconcile_attempt(STAFF, attempt, source_key=self.key())['state'], 'succeeded')
        refund = self.service.request_refund(STAFF, attempt, 100, reason='fixture-return', source_key=self.key())
        self.assertEqual(self.service.dispatch_refund(STAFF, refund['refund_id'], source_key=self.key())['state'], 'succeeded')

    def test_off_session_dispatch_requires_staff_and_explicit_automatic_consent(self):
        attempt = self.attempt(off_session=True)
        self.assertCode('staff_permission_required', lambda: self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key()))
        self.assertEqual(self.adapter.create_calls, 0)
        self.assertEqual(self.service.dispatch_attempt(STAFF, attempt, source_key=self.key())['state'], 'requires_action')

    def test_customer_can_start_and_reconcile_own_manual_test_flow(self):
        attempt = self.service.create_attempt(CUSTOMER, self.bill(), self.consent(), flow='customer_initiated', source_key=self.key())
        result = self.service.dispatch_attempt(CUSTOMER, attempt['attempt_id'], source_key=self.key())
        self.assertEqual(result['state'], 'requires_action')
        self.assertEqual(result['flow'], 'customer_initiated')
        self.adapter.advance(self.provider_key(attempt['attempt_id']), 'succeeded')
        result = self.service.reconcile_attempt(CUSTOMER, attempt['attempt_id'], source_key=self.key())
        self.assertEqual(result['state'], 'succeeded')

    def test_customer_cannot_initiate_off_session(self):
        bill, consent = self.bill(), self.consent(off_session=True)
        self.assertCode('staff_permission_required', lambda: self.service.create_attempt(CUSTOMER, bill, consent, flow='off_session', source_key=self.key()))

    def test_success_and_receipt_never_real_money(self):
        attempt = self.paid()
        details = self.service.get_attempt(CUSTOMER, attempt)
        bill = self.service.get_bill(CUSTOMER, details['bill_id'])
        self.assertEqual(bill['test_paid_yen'], 1100)
        self.assertFalse(bill['real_payment_enabled'])
        self.assertFalse(bill['real_receipt_issued'])
        self.assertNotIn('provider_customer_reference', _json(bill))
        self.assertNotIn('payment_method_reference', _json(bill))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 1)

    def test_default_disconnected_cannot_dispatch(self):
        attempt = self.attempt()
        service = PhysicalPaymentService(self.store)
        self.assertCode('physical_test_adapter_unavailable', lambda: service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key()))

    def test_arbitrary_adapter_cannot_run_network_under_transaction(self):
        class Fake:
            mode = 'test'
        self.assertCode('physical_test_adapter_required', lambda: PhysicalPaymentService(self.store, Fake()))

    def test_new_attempt_blocked_while_requires_action(self):
        attempt = self.attempt()
        self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key())
        row = self.service.get_attempt(STAFF, attempt)
        self.assertCode('payment_attempt_exists_reconcile_first', lambda: self.service.create_attempt(CUSTOMER, row['bill_id'], self.consent(), source_key=self.key()))

    def test_timeout_reserves_attempt_and_needs_retrieval(self):
        attempt = self.attempt()
        self.adapter.next_payment_status = 'succeeded'
        self.adapter.timeout_after_create = True
        result = self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key())
        self.assertEqual(result['state'], 'unknown')
        self.adapter.timeout_after_create = False
        result = self.service.reconcile_attempt(STAFF, attempt, source_key=self.key())
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(self.adapter.create_calls, 1)

    def test_duplicate_dispatch_only_one_provider_object(self):
        attempt = self.attempt()
        key = self.key()
        first = self.service.dispatch_attempt(CUSTOMER, attempt, source_key=key)
        second = self.service.dispatch_attempt(CUSTOMER, attempt, source_key=key)
        self.assertEqual(first['state'], second['state'])
        self.assertEqual(self.adapter.create_calls, 1)

    def test_duplicate_retrieval_does_not_duplicate_event(self):
        attempt = self.paid()
        before = self.db.execute('SELECT COUNT(*) FROM stage5_payment_events').fetchone()[0]
        self.service.reconcile_attempt(STAFF, attempt, source_key=self.key())
        self.assertEqual(before, self.db.execute('SELECT COUNT(*) FROM stage5_payment_events').fetchone()[0])

    def test_reversed_snapshot_does_not_downgrade_success(self):
        attempt = self.attempt()
        self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key())
        key = self.provider_key(attempt)
        older = self.adapter.retrieve(key)
        self.adapter.advance(key, 'succeeded')
        self.service.reconcile_attempt(STAFF, attempt, source_key=self.key())
        self.adapter.objects[key] = older
        self.assertEqual(self.service.reconcile_attempt(STAFF, attempt, source_key=self.key())['state'], 'succeeded')

    def test_terminal_canceled_then_success_is_conflict(self):
        attempt = self.attempt()
        self.adapter.next_payment_status = 'canceled'
        self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key())
        self.adapter.advance(self.provider_key(attempt), 'succeeded')
        self.assertCode('payment_terminal_state_conflict', lambda: self.service.reconcile_attempt(STAFF, attempt, source_key=self.key()))

    def test_failed_payment_needs_reconcile_before_retry_new_intent(self):
        attempt = self.attempt()
        self.adapter.next_payment_status = 'failed'
        self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key())
        bill = self.service.get_attempt(STAFF, attempt)['bill_id']
        self.assertCode('payment_attempt_exists_reconcile_first', lambda: self.service.create_attempt(CUSTOMER, bill, self.consent(), source_key=self.key()))
        self.adapter.advance(self.provider_key(attempt), 'canceled')
        self.service.reconcile_attempt(STAFF, attempt, source_key=self.key())
        second = self.service.create_attempt(CUSTOMER, bill, self.consent(), source_key=self.key())
        self.assertNotEqual(second['attempt_id'], attempt)

    def test_stale_bill_blocks_dispatch_after_attempt_prepared(self):
        attempt = self.attempt()
        approved_component(self.db, 'late', 100)
        self.assertCode('physical_invoice_stale', lambda: self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key()))
        self.assertEqual(self.adapter.create_calls, 0)

    def test_consent_revoked_between_prepare_and_dispatch(self):
        attempt = self.attempt()
        consent = self.db.execute('SELECT consent_id FROM stage5_payment_attempts WHERE attempt_id=?', (attempt,)).fetchone()[0]
        self.service.revoke_consent(CUSTOMER, consent, reason='withdrawn', source_key=self.key())
        self.assertCode('payment_consent_revoked', lambda: self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key()))

    def test_provider_customer_amount_currency_metadata_mode_must_match(self):
        attempt = self.attempt()
        self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key())
        key = self.provider_key(attempt)
        original = self.adapter.retrieve(key)
        changes = [dict(amount_yen=1), dict(currency='USD'), dict(customer_reference='cus_test_2'),
                   dict(metadata={}), dict(idempotency_key='another'), dict(object_kind='refund'),
                   dict(livemode=True), dict(provider='stripe_live')]
        for change in changes:
            self.adapter.objects[key] = replace(original, revision=2, status='succeeded', **change)
            with self.assertRaises(RegistryError):
                self.service.reconcile_attempt(STAFF, attempt, source_key=self.key())
        self.assertEqual(self.service.get_attempt(STAFF, attempt)['state'], 'requires_action')

    def test_same_provider_revision_conflicting_status_rejected(self):
        attempt = self.attempt()
        self.service.dispatch_attempt(CUSTOMER, attempt, source_key=self.key())
        key = self.provider_key(attempt)
        self.adapter.objects[key] = replace(self.adapter.retrieve(key), status='succeeded')
        self.assertCode('payment_provider_revision_conflict', lambda: self.service.reconcile_attempt(STAFF, attempt, source_key=self.key()))

    def test_unpaid_or_customer_refund_forbidden(self):
        attempt = self.attempt()
        self.assertCode('refund_requires_verified_payment', lambda: self.service.request_refund(STAFF, attempt, 100, reason='return', source_key=self.key()))
        self.assertCode('staff_permission_required', lambda: self.service.request_refund(CUSTOMER, attempt, 100, reason='return', source_key=self.key()))

    def test_refund_reserved_and_success_bounded_to_original_payment(self):
        attempt = self.paid()
        refund = self.service.request_refund(STAFF, attempt, 700, reason='fixture-partial-return', source_key=self.key())
        self.assertCode('refund_exceeds_original_payment', lambda: self.service.request_refund(STAFF, attempt, 401, reason='return', source_key=self.key()))
        self.service.dispatch_refund(STAFF, refund['refund_id'], source_key=self.key())
        result = self.service.get_attempt(CUSTOMER, attempt)
        self.assertEqual(result['refunded_yen'], 700)
        self.assertEqual(result['refund_reserved_yen'], 0)
        self.assertCode('refund_exceeds_original_payment', lambda: self.service.request_refund(STAFF, attempt, 401, reason='return', source_key=self.key()))

    def test_refund_failure_releases_reservation_but_cannot_later_succeed(self):
        attempt = self.paid()
        refund = self.service.request_refund(STAFF, attempt, 1100, reason='return', source_key=self.key())
        self.adapter.next_refund_status = 'failed'
        self.service.dispatch_refund(STAFF, refund['refund_id'], source_key=self.key())
        self.service.request_refund(STAFF, attempt, 1100, reason='retry-reviewed', source_key=self.key())
        self.adapter.advance(self.provider_key(refund['refund_id'], True), 'succeeded')
        self.assertCode('payment_terminal_state_conflict', lambda: self.service.reconcile_refund(STAFF, refund['refund_id'], source_key=self.key()))

    def test_refund_timeout_keeps_capacity_reserved(self):
        attempt = self.paid()
        refund = self.service.request_refund(STAFF, attempt, 1100, reason='return', source_key=self.key())
        self.adapter.timeout_after_create = True
        result = self.service.dispatch_refund(STAFF, refund['refund_id'], source_key=self.key())
        self.assertEqual(result['state'], 'unknown')
        self.assertCode('refund_exceeds_original_payment', lambda: self.service.request_refund(STAFF, attempt, 1, reason='retry', source_key=self.key()))

    def test_refund_must_match_original_provider_payment(self):
        attempt = self.paid()
        refund = self.service.request_refund(STAFF, attempt, 100, reason='return', source_key=self.key())
        self.service.dispatch_refund(STAFF, refund['refund_id'], source_key=self.key())
        key = self.provider_key(refund['refund_id'], True)
        self.adapter.objects[key] = replace(self.adapter.retrieve(key), payment_id='another', revision=2)
        self.assertCode('payment_snapshot_mismatch', lambda: self.service.reconcile_refund(STAFF, refund['refund_id'], source_key=self.key()))

    def test_all_payment_evidence_append_only(self):
        self.paid()
        for table in ('stage5_physical_bills', 'stage5_card_consents', 'stage5_payment_attempts', 'stage5_payment_events'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('DELETE FROM ' + table)

    def test_postgres_migration_is_review_only_and_keeps_constraints(self):
        sql = '\n'.join(migration_statements('postgres'))
        self.assertIn('BIGINT', sql)
        self.assertIn("CHECK(execution_mode='test')", sql)
        self.assertIn('UNIQUE(attempt_id,provider_revision)', sql)
        self.assertNotIn('typeof(', sql)

    def test_two_simultaneous_refunds_cannot_overreserve(self):
        with tempfile.TemporaryDirectory(prefix='kaika-stage5-unit-') as folder:
            path = str(Path(folder) / 'fictional.db')
            db = fixture(path)
            service = PhysicalPaymentService(Stage5Store(Registry(db)), MemoryTestPSP())
            approved_component(db, 'line')
            invoice = BillingService(service.store.finance).create_month(STAFF, 1, '2026-10', source_key='month')['invoice_id']
            bill = service.prepare_invoice(STAFF, invoice, review_reference='fixture-review', source_key='bill')['bill_id']
            consent = service.record_consent(CUSTOMER, 1, terms_reference='terms', payment_method_reference='pm_test_1', provider_customer_reference='cus_test_1', source_key='consent')['consent_id']
            attempt = service.create_attempt(CUSTOMER, bill, consent, source_key='attempt')['attempt_id']
            service.adapter.next_payment_status = 'succeeded'
            service.dispatch_attempt(CUSTOMER, attempt, source_key='dispatch')
            db.commit()
            db.close()
            barrier = threading.Barrier(2)
            def refund(index):
                connection = sqlite3.connect(path, timeout=20)
                connection.execute('PRAGMA foreign_keys=ON')
                local = PhysicalPaymentService(Stage5Store(Registry(connection)))
                try:
                    barrier.wait()
                    local.request_refund(STAFF, attempt, 700, reason='return', source_key='concurrent-' + str(index))
                    connection.commit()
                    return 'ok'
                except RegistryError as exc:
                    connection.rollback()
                    return exc.code
                finally:
                    connection.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(refund, (1, 2)))
            self.assertCountEqual(results, ['ok', 'refund_exceeds_original_payment'])


if __name__ == '__main__':
    unittest.main()
