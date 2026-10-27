import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path

from business_policy import (ContractEvidence, FeePolicyEvidence, FundingApproval,
    InvoiceContext, WorkEvidence, draft_catalog, draft_sale_quote,
    evaluate_invoice_eligibility, example_invoice_context, funding_capability_status,
    integer_yen, period_key)
from business_schema import create_test_schema


def moment(day, hour=3):
    return datetime(2026, 11, day, hour, tzinfo=timezone.utc)


def complete_fixture():
    """Synthetic approval exists only in tests; no real client or contract."""
    contract = ContractEvidence(1, 'contract-v1', 'fee-v1', 'fixture-signed',
        'fixture-consent', moment(1), moment(10), 'none')
    policy = FeePolicyEvidence(version_id='fee-v1', status='approved', approval_reference='fixture-approval',
        approved_at=moment(1), effective_from=moment(10), amount_yen=650,
        tax_treatment='exclusive', tax_rate_percent=10, rounding_method='floor',
        rounding_scope='invoice_per_rate', billing_timezone='Asia/Tokyo',
        billing_timezone_approved=True, work_scope_confirmed=True)
    work = WorkEvidence('w1', 'item1', 'registration', moment(11), source_reference='fixture-work')
    return InvoiceContext(1, 'item1', 'work_bundle', contract, policy,
        event_id='shipment1', source_reference='fixture-shipment', charge_event_at=moment(12),
        sold_at=moment(11), shipped_at=moment(12), works=(work,), return_state='no_return')


class BusinessPolicyTests(unittest.TestCase):
    def blocked(self, context, reason):
        result = evaluate_invoice_eligibility(context)
        self.assertFalse(result.allowed)
        self.assertIn(reason, result.reason_codes)
        self.assertTrue(result.can_save_draft)
        self.assertTrue(result.can_record_work)
        self.assertFalse(result.real_payment_enabled)
        return result

    def test_catalog_never_enables_draft_prices_and_keeps_separate_limits(self):
        value = draft_catalog()
        self.assertFalse(value['enabled'])
        self.assertFalse(value['chargeable'])
        self.assertEqual(value['free']['active_individual_items'], 100)
        self.assertEqual(value['free']['monthly_limit_kind'], 'no_limit_proposed')
        self.assertIsNone(value['free']['monthly_registration_limit'])
        self.assertEqual([p['monthly_gross_example_yen'] for p in value['paid_plans']], [3278, 6578, 10780, 21780, None])
        self.assertEqual([p['kaika_monthly_registration_limit'] for p in value['paid_plans']], [20, 50, 100, 300, None])
        self.assertTrue(all(p['self_active_item_limit'] is None and p['self_limit_status'] == 'undecided' for p in value['paid_plans']))
        value['paid_plans'][0]['enabled'] = True
        self.assertFalse(draft_catalog()['paid_plans'][0]['enabled'])
        self.assertEqual(value['sales_fee_tax_treatment'], 'exclusive')
        self.assertFalse(value['customer_contract_fee_approval'])
        self.assertEqual(value['work_bundle']['mixed_pre_start_work_policy'], 'post_start_work_only')
        self.assertIsNone(value['work_bundle']['mixed_post_start_allocation_yen'])

    def test_exact_sale_boundaries_preserve_original_fee_bands(self):
        cases = ((19999, 500, 500, 300, 300), (20000, 650, 1000, 400, 400),
                 (49999, 650, 1000, 400, 400), (50000, 800, 1500, 500, 500),
                 (99999, 800, 1500, 500, 500), (100000, 1000, 2000, 500, 700),
                 (199999, 1000, 2000, 500, 700), (200000, 1000, 3000, 500, 700))
        for amount, work, wholesale, multi, auction in cases:
            for channel, fee in (('wholesale', wholesale), ('multichannel', multi), ('dealer_auction', auction)):
                with self.subTest(amount=amount, channel=channel):
                    result = draft_sale_quote(amount, channel)
                    self.assertEqual(result['work_bundle_net_yen'], work)
                    self.assertEqual(result['sales_fee_source_yen'], fee)
                    self.assertEqual(result['sales_fee_tax_treatment'], 'exclusive')
                    self.assertEqual(result['sales_fee_net_yen'], fee)
                    self.assertEqual(result['sales_fee_gross_example_yen'], fee * 110 // 100)
                    self.assertIsNone(result['total_charge_yen'])
                    self.assertFalse(result['chargeable'])

    def test_sales_tax_answer_changes_examples_not_contract_activation(self):
        result = draft_sale_quote(19999, 'wholesale')
        self.assertEqual(result['sales_fee_source_yen'], 500)
        self.assertEqual(result['sales_fee_net_yen'], 500)
        self.assertEqual(result['sales_fee_gross_example_yen'], 550)
        self.assertEqual(draft_sale_quote(20000, 'multichannel')['total_charge_gross_example_yen'], 1155)
        self.assertIsNone(result['total_charge_yen'])
        self.assertFalse(result['customer_contract_fee_approval'])
        self.assertFalse(result['chargeable'])
        live = draft_sale_quote(30000, 'live')
        self.assertIsNone(live['sales_fee_net_yen'])
        self.assertIsNone(live['sales_fee_gross_example_yen'])
        self.assertIsNone(live['total_charge_gross_example_yen'])

    def test_shipping_boundary_and_self_sale_never_authorize_channel(self):
        self.assertEqual(draft_sale_quote(30000, 'wholesale')['wholesale_shipping_payer_proposal'], 'customer')
        self.assertEqual(draft_sale_quote(30001, 'wholesale')['wholesale_shipping_payer_proposal'], 'kaika')
        result = draft_sale_quote(30000, 'self_sale_shipping')
        self.assertEqual(result['sales_fee_source_yen'], 0)
        self.assertFalse(result['channel_authorization_verified'])
        self.assertIsNone(draft_sale_quote(30000, 'live')['sales_fee_source_yen'])

    def test_unknown_zero_and_invalid_money_are_distinct(self):
        self.assertIsNone(integer_yen(None))
        self.assertEqual(integer_yen(0), 0)
        for value in (True, 1.1, '500', -1):
            with self.assertRaises(ValueError):
                integer_yen(value)
        self.assertEqual(integer_yen(-1, allow_negative=True), -1)
        for value in (None, 0, 100.1, True):
            with self.assertRaises(ValueError):
                draft_sale_quote(value, 'wholesale')

    def test_period_requires_zone_and_aware_time_with_jst_boundary(self):
        before = datetime(2026, 10, 31, 14, 59, 59, tzinfo=timezone.utc)
        after = before + timedelta(seconds=1)
        self.assertEqual(period_key(before, 'Asia/Tokyo'), '2026-10')
        self.assertEqual(period_key(after, 'Asia/Tokyo'), '2026-11')
        self.assertEqual(period_key(after, 'UTC'), '2026-10')
        with self.assertRaises(ValueError):
            period_key(after.replace(tzinfo=None), 'Asia/Tokyo')
        with self.assertRaises(ValueError):
            period_key(after, None)

    def test_complete_synthetic_evidence_allows_review_only(self):
        result = evaluate_invoice_eligibility(complete_fixture())
        self.assertTrue(result.allowed)
        self.assertEqual(result.reason_codes, ())
        self.assertFalse(result.real_payment_enabled)

    def test_customer_consent_version_and_effective_window_are_bound(self):
        context = complete_fixture()
        for changes, reason in (({'customer_id': 2}, 'customer_mismatch'),
            ({'consent_reference': None}, 'consent_missing'),
            ({'fee_version_id': 'other'}, 'fee_version_mismatch'),
            ({'signed_document_reference': None}, 'contract_version_missing'),
            ({'consent_at': moment(13)}, 'before_consent'),
            ({'charge_start_at': None}, 'charge_start_missing')):
            with self.subTest(changes=changes):
                self.blocked(replace(context, contract=replace(context.contract, **changes)), reason)
        self.blocked(replace(context, fee_policy=replace(context.fee_policy, effective_until=moment(12))), 'policy_not_effective')
        self.blocked(replace(context, fee_policy=replace(context.fee_policy, approved_at=moment(13))), 'fee_policy_draft')

    def test_drafts_unknown_tax_amount_and_rounding_never_finalize(self):
        context = complete_fixture()
        for changes, reason in (({'status': 'draft'}, 'fee_policy_draft'),
            ({'amount_yen': None}, 'unknown_amount'), ({'amount_yen': 650.0}, 'invalid_amount'),
            ({'currency': 'USD'}, 'invalid_amount'), ({'tax_treatment': None}, 'tax_unknown'),
            ({'tax_rate_percent': None}, 'tax_unknown'), ({'rounding_scope': None}, 'rounding_unknown'),
            ({'billing_timezone_approved': False}, 'period_unknown'),
            ({'effective_from': moment(10).replace(tzinfo=None)}, 'invalid_time')):
            with self.subTest(changes=changes):
                self.blocked(replace(context, fee_policy=replace(context.fee_policy, **changes)), reason)
        self.assertTrue(evaluate_invoice_eligibility(replace(context, fee_policy=replace(context.fee_policy, amount_yen=0))).allowed)

    def test_prestart_mixed_and_start_boundary_work(self):
        context = complete_fixture()
        old = replace(context.works[0], performed_at=moment(9))
        self.blocked(replace(context, works=(old,)), 'pre_start_work_not_billable')
        self.blocked(replace(context, works=(old, context.works[0])), 'mixed_start_bundle_unresolved')
        at_start = replace(context.works[0], performed_at=moment(10))
        self.assertTrue(evaluate_invoice_eligibility(replace(context, works=(at_start,))).allowed)

    def test_preconsent_work_and_sale_are_held_even_when_shipped_after_consent(self):
        context = complete_fixture()
        late_consent = replace(context.contract, consent_at=moment(11, 4))
        result = self.blocked(replace(context, contract=late_consent), 'pre_consent_work_unresolved')
        self.assertIn('sale_before_consent_unresolved', result.reason_codes)
        self.assertNotIn('before_consent', result.reason_codes)
        sale = replace(context, contract=late_consent, fee_kind='sales_fee', works=(),
                       channel_authorization_reference='fixture-permission')
        self.blocked(sale, 'sale_before_consent_unresolved')
        exact = replace(context, contract=replace(context.contract, consent_at=moment(11)))
        self.assertTrue(evaluate_invoice_eligibility(exact).allowed)

    def test_shipping_trigger_work_and_item_evidence(self):
        context = complete_fixture()
        self.blocked(replace(context, shipped_at=None), 'not_sold_and_shipped')
        self.blocked(replace(context, sold_at=None), 'not_sold_and_shipped')
        self.blocked(replace(context, charge_event_at=moment(13)), 'invalid_shipping_sequence')
        self.blocked(replace(context, works=()), 'work_evidence_missing')
        self.blocked(replace(context, works=(replace(context.works[0], physical_item_id='other'),)), 'work_item_mismatch')
        self.blocked(replace(context, works=(replace(context.works[0], performed_at=moment(13)),)), 'work_after_shipping')
        self.blocked(replace(context, fee_policy=replace(context.fee_policy, work_scope_confirmed=False)), 'work_scope_unknown')

    def test_exemption_return_cancel_and_duplicate_cannot_bypass_review(self):
        context = complete_fixture()
        for status, reason in (('undecided','exception_unresolved'), ('exempt','exempt_not_chargeable'), ('adjusted','adjustment_requires_review')):
            self.blocked(replace(context, contract=replace(context.contract, exception_status=status)), reason)
        self.blocked(replace(context, return_state='pending'), 'return_unresolved')
        self.blocked(replace(context, return_state='resolved', adjustment_review_complete=True), 'return_unresolved')
        self.blocked(replace(context, cancelled=True), 'cancelled_requires_correction')
        self.blocked(replace(context, already_billed_event=True), 'already_billed')

    def test_sales_fee_requires_channel_permission_and_start_boundary(self):
        context = replace(complete_fixture(), fee_kind='sales_fee')
        self.blocked(context, 'channel_not_authorized')
        authorized = replace(context, channel_authorization_reference='fixture-permission')
        self.assertTrue(evaluate_invoice_eligibility(authorized).allowed)
        self.blocked(replace(authorized, sold_at=moment(9)), 'sale_before_start_unresolved')

    def test_unsupported_monthly_or_expense_is_not_silently_confirmed(self):
        for kind in ('digital_monthly', 'physical_monthly', 'expense', 'adjustment'):
            self.blocked(replace(complete_fixture(), fee_kind=kind), 'unknown_fee_kind')

    def test_all_ui_examples_are_fictional_and_blocked(self):
        for case in ('normal', 'before_start', 'mixed_start', 'return_unresolved'):
            context = example_invoice_context(case)
            self.assertLess(context.customer_id, 0)
            self.blocked(context, 'fee_policy_draft')
        self.blocked(example_invoice_context('before_start'), 'pre_start_work_not_billable')
        self.blocked(example_invoice_context('mixed_start'), 'mixed_start_bundle_unresolved')
        self.blocked(example_invoice_context('return_unresolved'), 'return_unresolved')
        with self.assertRaises(ValueError):
            example_invoice_context('customer-123')

    def test_funding_requires_all_scoped_current_evidence_and_explicit_optin(self):
        capability = 'card_purchase_deposit'
        approval = FundingApproval(capability, 'regulatory', 'psp', 'contract', 'operator', moment(1), moment(20))
        self.assertFalse(funding_capability_status(capability, approval, now=moment(12)).allowed)
        result = funding_capability_status(capability, approval, now=moment(12), enabled=True)
        self.assertTrue(result.allowed)
        self.assertFalse(result.real_payment_enabled)
        for change in ({'psp_reference': None}, {'capability': 'sales_settlement'}, {'expires_at': moment(12)}, {'approved_at': moment(13)}):
            self.assertFalse(funding_capability_status(capability, replace(approval, **change), now=moment(12), enabled=True).allowed)
        self.assertFalse(funding_capability_status('peer_transfer', approval, now=moment(12), enabled=True).allowed)


class BusinessSchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('CREATE TABLE users(id INTEGER PRIMARY KEY); CREATE TABLE merchandise(id INTEGER PRIMARY KEY); INSERT INTO users VALUES(1); INSERT INTO merchandise VALUES(7);')
        create_test_schema(self.db)
        self.db.execute("INSERT INTO business_parties VALUES('customer','customer',1,'fixture')")
        self.db.execute("INSERT INTO business_parties VALUES('kaika','kaika',NULL,'fixture')")
        self.db.execute("INSERT INTO business_items(physical_item_id) VALUES('item')")

    def tearDown(self):
        self.db.close()

    def test_schema_is_additive_idempotent_and_preserves_existing_rows(self):
        first = create_test_schema(self.db)
        self.assertEqual(first, create_test_schema(self.db))
        self.assertEqual(self.db.execute('SELECT * FROM merchandise').fetchall(), [(7,)])
        self.assertEqual(self.db.execute('SELECT * FROM users').fetchall(), [(1,)])

    def test_file_backed_databases_are_refused_before_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'fixture.sqlite'
            with closing(sqlite3.connect(path)) as connection:
                with self.assertRaises(ValueError):
                    create_test_schema(connection)
                self.assertEqual(connection.execute('SELECT name FROM sqlite_master').fetchall(), [])

    def test_legacy_mapping_requires_existing_id_and_never_automerge(self):
        self.assertEqual(self.db.execute('SELECT * FROM business_item_links').fetchall(), [])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO business_item_links VALUES(8,'item','unresolved',NULL)")
        self.db.execute("INSERT INTO business_item_links VALUES(7,'item','unresolved',NULL)")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM merchandise WHERE id=7')

    def test_owner_and_custodian_are_distinct_but_current_owner_unique(self):
        sql = 'INSERT INTO business_item_assignments VALUES(?,?,?,?,?,?,?,?)'
        self.db.execute(sql, ('a','item','owner','customer','2026-11-10T12:00:00+09:00',None,'fixture','event1'))
        self.db.execute(sql, ('b','item','custodian','kaika','2026-11-10T12:00:00+09:00',None,'fixture','event2'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(sql, ('c','item','owner','kaika','2026-11-10T12:00:00+09:00',None,'fixture','event3'))

    def test_one_physical_item_cannot_have_two_active_test_orders(self):
        sql = 'INSERT INTO business_orders(order_id,physical_item_id,seller_party_id,buyer_party_id,status,source_event_key) VALUES(?,?,?,?,?,?)'
        self.db.execute(sql, ('one','item','kaika','customer','reserved','order1'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(sql, ('two','item','kaika','customer','reserved','order2'))
        self.db.execute("UPDATE business_orders SET status='cancelled' WHERE order_id='one'")
        self.db.execute(sql, ('two','item','kaika','customer','reserved','order2'))

    def test_work_record_keeps_unknown_duration_and_rejects_synthetic_measurement(self):
        sql = '''INSERT INTO business_work_records(work_id,physical_item_id,actor_party_id,kind,performed_at,
            duration_seconds,measurement_source,source_reference,source_event_key) VALUES(?,?,?,?,?,?,?,?,?)'''
        self.db.execute(sql, ('work','item','kaika','photography','2026-11-10T12:00:00+09:00',None,'unknown','fixture','w1'))
        self.assertIsNone(self.db.execute("SELECT duration_seconds FROM business_work_records WHERE work_id='work'").fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(sql, ('work2','item','kaika','photography','2026-11-10T12:00:00+09:00',60,'unknown','fixture','w2'))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("DELETE FROM business_work_records")
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_charge_drafts').fetchone()[0], 0)

    def test_no_active_fees_no_unknown_zero_conversion_no_final_invoice(self):
        self.db.execute("INSERT INTO business_fee_versions(fee_version_id,source_reference,policy_snapshot_json) VALUES('fee','fixture','{}')")
        self.assertEqual(self.db.execute('SELECT monthly_net_yen,self_active_item_limit,enabled FROM business_fee_versions').fetchone(), (None,None,0))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE business_fee_versions SET enabled=1')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO business_charge_drafts(draft_id,customer_id,fee_kind,review_reasons_json,status) VALUES('d',1,'work_bundle','[]','finalized')")

    def test_charge_draft_replay_and_fund_replay_are_unique_and_append_only(self):
        self.db.execute("INSERT INTO business_item_events VALUES('ship','item','shipped','2026-11-12T12:00:00+09:00','kaika','fixture','shipment',NULL,1)")
        sql = "INSERT INTO business_charge_drafts(draft_id,customer_id,physical_item_id,source_event_id,fee_kind,review_reasons_json) VALUES(?,1,'item','ship','work_bundle','[]')"
        self.db.execute(sql, ('d1',))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(sql, ('d2',))
        fund_sql = "INSERT INTO business_fund_records(record_id,customer_id,bucket,kind,amount_yen,occurred_at,source_reference,source_event_key) VALUES(?,1,'purchase_funds','deposit',100,'2026-11-12T12:00:00+09:00','fixture','deposit1')"
        self.db.execute(fund_sql, ('f1',))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(fund_sql, ('f2',))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE business_fund_records SET amount_yen=200')
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM business_fund_records')

    def test_funding_mode_cannot_become_live_and_no_peer_transfer(self):
        sql = "INSERT INTO business_funding_reviews(review_id,capability,enabled) VALUES(?,?,?)"
        for capability, enabled in (('card_purchase_deposit',1), ('peer_transfer',0)):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(sql, ('review',capability,enabled))

    def test_cross_customer_fund_correction_is_rejected(self):
        self.db.execute('INSERT INTO users VALUES(2)')
        self.db.execute("INSERT INTO business_fund_records(record_id,customer_id,bucket,kind,occurred_at,source_reference,source_event_key) VALUES('original',1,'purchase_funds','deposit','2026-11-12T12:00:00+09:00','fixture','original')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO business_fund_records(record_id,customer_id,bucket,kind,occurred_at,original_record_id,source_reference,source_event_key) VALUES('correction',2,'purchase_funds','correction','2026-11-12T12:00:00+09:00','original','fixture','correction')")

    def test_owner_history_can_close_but_cannot_rewrite_owner(self):
        self.db.execute("INSERT INTO business_item_assignments VALUES('a','item','owner','customer','2026-11-10T12:00:00+09:00',NULL,'fixture','event1')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE business_item_assignments SET party_id='kaika'")
        self.db.execute("UPDATE business_item_assignments SET ended_at='2026-11-12T12:00:00+09:00'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM business_item_assignments')

    def billing_fixture(self):
        self.db.execute('INSERT INTO users VALUES(2)')
        self.db.execute("INSERT INTO business_items(physical_item_id) VALUES('other-item')")
        self.db.execute('INSERT INTO business_contract_versions(contract_version_id,document_reference,content_sha256,terms_snapshot_json) VALUES(?,?,?,?)', ('contract','fixture','a'*64,'{}'))
        self.db.execute('INSERT INTO business_contract_versions(contract_version_id,document_reference,content_sha256,terms_snapshot_json) VALUES(?,?,?,?)', ('other-contract','fixture','b'*64,'{}'))
        for version in ('fee','other-fee'):
            self.db.execute('INSERT INTO business_fee_versions(fee_version_id,source_reference,policy_snapshot_json) VALUES(?,?,?)', (version,'fixture','{}'))
        for agreement, customer, version in (('a1',1,'fee'),('a2',2,'fee'),('a1v2',1,'other-fee')):
            self.db.execute('INSERT INTO business_customer_agreements(agreement_id,customer_id,contract_version_id,fee_version_id) VALUES(?,?,?,?)', (agreement,customer,'contract',version))
        for event, item, customer in (('e1','item',1),('e-other-item','other-item',1),('e-other-customer','item',2),('e-unknown-customer','item',None)):
            self.db.execute('INSERT INTO business_item_events(event_id,physical_item_id,kind,occurred_at,actor_party_id,source_reference,source_event_key,customer_id) VALUES(?,?,?,?,?,?,?,?)',
                (event,item,'shipped','2026-11-12T12:00:00+09:00','kaika','fixture',event,customer))
        return dict(draft_id='draft', customer_id=1, physical_item_id='item', agreement_id='a1', fee_version_id='fee', source_event_id='e1', fee_kind='work_bundle', review_reasons_json='[]')

    def insert_draft(self, values):
        columns = tuple(values)
        self.db.execute('INSERT INTO business_charge_drafts(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(values[c] for c in columns))

    def attribution_mismatches(self):
        return ({'customer_id':2}, {'agreement_id':'a2'}, {'fee_version_id':'other-fee'},
                {'fee_version_id':None}, {'physical_item_id':'other-item'},
                {'physical_item_id':None}, {'source_event_id':'e-other-item'},
                {'source_event_id':'e-other-customer'}, {'source_event_id':'e-unknown-customer'})

    def test_draft_insert_rejects_agreement_fee_event_and_customer_mismatches(self):
        base = self.billing_fixture()
        for changes in self.attribution_mismatches():
            with self.subTest(changes=changes), self.assertRaises(sqlite3.IntegrityError):
                self.insert_draft({**base, **changes})
        self.insert_draft(base)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_charge_drafts').fetchone()[0], 1)

    def test_draft_update_cannot_reassign_existing_charge_attribution(self):
        base = self.billing_fixture()
        self.insert_draft(base)
        for changes in self.attribution_mismatches():
            key, value = next(iter(changes.items()))
            with self.subTest(changes=changes), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('UPDATE business_charge_drafts SET ' + key + '=? WHERE draft_id=?', (value,'draft'))
        self.assertEqual(self.db.execute('SELECT customer_id,physical_item_id,agreement_id,fee_version_id,source_event_id FROM business_charge_drafts').fetchone(), (1,'item','a1','fee','e1'))

    def test_standalone_incomplete_draft_keeps_unknowns_without_borrowing_event(self):
        self.billing_fixture()
        self.insert_draft(dict(draft_id='unresolved', customer_id=1, physical_item_id=None,
            agreement_id=None, fee_version_id=None, source_event_id=None, fee_kind='work_bundle', review_reasons_json='["event_missing"]'))
        self.assertEqual(self.db.execute('SELECT amount_yen,status FROM business_charge_drafts').fetchone(), (None,'draft'))

    def test_referenced_agreement_cannot_be_changed_behind_a_draft(self):
        base = self.billing_fixture()
        self.insert_draft(base)
        for key, value in (('customer_id',2),('fee_version_id','other-fee'),('contract_version_id','other-contract')):
            with self.subTest(key=key), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('UPDATE business_customer_agreements SET '+key+'=? WHERE agreement_id=?', (value,'a1'))

    def test_correction_attribution_is_checked_on_insert_update_and_original_update(self):
        base = self.billing_fixture()
        self.insert_draft(base)
        correction = {**base, 'draft_id':'correction', 'fee_kind':'adjustment', 'original_draft_id':'draft', 'source_event_id':None}
        self.insert_draft(correction)
        for kind in ('insert','update'):
            with self.subTest(kind=kind), self.assertRaises(sqlite3.IntegrityError):
                if kind == 'insert':
                    self.insert_draft({**correction,'draft_id':'wrong','customer_id':2,'agreement_id':'a2'})
                else:
                    self.db.execute("UPDATE business_charge_drafts SET customer_id=2,agreement_id='a2' WHERE draft_id='correction'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE business_charge_drafts SET customer_id=2,agreement_id='a2',source_event_id='e-other-customer' WHERE draft_id='draft'")


if __name__ == '__main__':
    unittest.main()
