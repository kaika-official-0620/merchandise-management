import unittest
from contextlib import contextmanager
from unittest.mock import patch

from item_registry import Actor
from stage3_billing import BillingService
from stage6_events import EventScanner
from stage6_actions import CustodyActionService
from stage5_payments import PhysicalPaymentService, MemoryTestPSP
from test_stage5_payments import approved_component
from test_stage6_home import HomeFixture, STAFF, CUSTOMER, OTHER, time


class EventTests(HomeFixture, unittest.TestCase):
    def setUp(self):
        self.init_home()
        self.scanner = EventScanner(self.store, clock=lambda:self.now)

    def events(self, kind=None):
        rows = self.scanner.scan(CUSTOMER)
        return [row for row in rows if row['event_type']==kind] if kind else rows

    def test_unknown_deadline_has_no_invented_event(self):
        self.register()
        self.assertEqual(self.events(),[])

    def test_seven_days_boundary_and_due_have_distinct_dedup_keys(self):
        self.register()
        self.deadline('2026-10-29T01:50:00+00:00')
        before = self.events('storage_pre_due')[0]
        self.assertTrue(self.scanner.is_current(CUSTOMER,before))
        self.now='2026-10-29T01:50:00+00:00'
        due = self.events('storage_due')[0]
        self.assertNotEqual(before['dedupe_key'],due['dedupe_key'])
        self.assertFalse(self.scanner.is_current(CUSTOMER,before))
        self.assertEqual(len(self.events('return_selection')),1)

    def test_more_than_seven_days_and_customer_custody_do_not_notify(self):
        self.deadline('2026-10-30T01:50:00+00:00')
        self.assertEqual(self.events(),[])
        self.register()
        self.assertEqual(self.events(),[])

    def test_repeated_scan_is_stable_and_no_writes(self):
        self.register(); self.deadline()
        before=self.db.total_changes
        self.assertEqual(self.events(),self.events())
        self.assertEqual(self.db.total_changes,before)

    def test_validating_one_item_does_not_rescan_all_ownerships_or_finances(self):
        self.register(); self.deadline()
        event=self.events('storage_due')[0]
        captured=[]
        self.db.set_trace_callback(captured.append)
        self.assertTrue(self.scanner.is_current(CUSTOMER,event))
        self.db.set_trace_callback(None)
        item_queries=[query for query in captured if 'FROM business_item_profiles p' in query]
        self.assertTrue(item_queries)
        self.assertTrue(all('p.physical_item_id=' in query for query in item_queries))
        self.assertFalse(any('FROM stage3_invoice_revisions' in query for query in captured))

    def test_new_deadline_invalidates_old_and_return_to_same_date_has_new_version(self):
        self.register(); self.deadline()
        first=self.events('storage_due')[0]
        self.deadline('2026-11-30T00:00:00+00:00','extended')
        self.assertFalse(self.scanner.is_current(CUSTOMER,first))
        self.deadline(key='reverted')
        self.assertNotEqual(first['dedupe_key'],self.events('storage_due')[0]['dedupe_key'])

    def test_equal_review_timestamps_do_not_reuse_prior_deadline_version(self):
        self.register()
        with patch('item_registry._now',return_value='2026-10-22T00:00:00.000000+00:00'):
            self.deadline()
            first=self.events('storage_due')[0]
            self.deadline('2026-12-01T00:00:00+00:00','change')
            self.deadline(key='back')
        self.assertNotEqual(first['dedupe_key'],self.events('storage_due')[0]['dedupe_key'])

    def test_other_customer_cannot_scan_scope_or_resolve(self):
        self.register(); self.deadline()
        event=self.events()[0]
        self.assertCode('customer_access_denied',lambda:self.scanner.scan(OTHER,1))
        self.assertFalse(self.scanner.is_current(OTHER,event))
        self.assertCode('notification_source_no_longer_current',lambda:self.scanner.resolve(OTHER,event['dedupe_key']))

    def test_ownership_change_invalidates_previous_event(self):
        self.register(); self.deadline()
        event=self.events()[0]
        self.registry.change_assignment(STAFF,self.item,'owner','customer',2,occurred_at=time(20),evidence_reference='ownership',source_key='transfer')
        self.assertFalse(self.scanner.is_current(CUSTOMER,event))
        self.assertFalse(self.scanner.is_current(STAFF,event))
        self.assertEqual(self.scanner.scan(OTHER),[])

    def test_sold_item_does_not_request_return_choice(self):
        self.register(); self.deadline(); self.event('sold',20)
        self.assertEqual(self.events(),[])

    def test_customer_approval_invalidates_notification(self):
        service, proposal=self.propose()
        event=self.events('listing_approval')[0]
        service.approve(CUSTOMER,proposal['version_id'],source_key='approve',evidence_reference='consent',occurred_at=time(20))
        self.assertFalse(self.scanner.is_current(CUSTOMER,event))

    def test_staff_approval_does_not_duplicate_customer_request(self):
        service, proposal=self.propose()
        before=self.events('listing_approval')[0]
        service.approve(STAFF,proposal['version_id'],source_key='approve',evidence_reference='consent',occurred_at=time(20))
        self.assertEqual(before['dedupe_key'],self.events('listing_approval')[0]['dedupe_key'])

    def test_item_changed_or_expired_proposal_is_not_actionable(self):
        service, proposal=self.propose()
        event=self.events('listing_approval')[0]
        self.registry.update_title(STAFF,self.item,'different',occurred_at=time(20),evidence_reference='edit',source_key='edit')
        self.assertFalse(self.scanner.is_current(CUSTOMER,event))

    def test_invoice_revision_invalidates_old_and_is_test_only(self):
        billing=BillingService(self.store.finance)
        billing.create_month(STAFF,1,'2026-10',source_key='first')
        event=self.events('invoice_draft')[0]
        billing.record_component(STAFF,1,fee_kind='expense',amount_net_yen=500,tax_rate_percent=10,
            occurred_at=time(10),evidence_reference='new-receipt',source_key='receipt')
        billing.create_month(STAFF,1,'2026-10',source_key='second')
        self.assertFalse(self.scanner.is_current(CUSTOMER,event))
        latest=self.events('invoice_draft')[0]
        self.assertEqual(latest['execution_mode'],'test')
        self.assertFalse(latest['external_delivery_allowed'])

    def test_new_choice_suppresses_old_storage_and_choice_notifications(self):
        self.register(); self.deadline()
        old=self.events('return_selection')[0]
        CustodyActionService(self.store).request(CUSTOMER,self.item,choice='return',note='返送の相談',source_key='choice')
        self.assertFalse(self.scanner.is_current(CUSTOMER,old))
        self.assertEqual(self.events(),[])

    def test_forged_link_or_source_cannot_be_current(self):
        self.register(); self.deadline()
        event=self.events()[0]
        self.assertFalse(self.scanner.is_current(CUSTOMER,dict(event,path='https://evil.invalid')))
        self.assertFalse(self.scanner.is_current(CUSTOMER,dict(event,source_id='different')))
        self.assertFalse(self.scanner.is_current(CUSTOMER,dict(event,external_delivery_allowed=True)))
        self.assertFalse(self.scanner.is_current(CUSTOMER,dict(event,execution_mode='live')))

    def test_notification_text_contains_no_item_or_money(self):
        self.register(); self.deadline()
        for event in self.events():
            self.assertNotIn('自分の商品',event['body']+event['title'])
            self.assertNotIn('円',event['body']+event['title'])
            self.assertFalse(event['external_delivery_allowed'])

    def test_payment_challenge_is_test_only_and_success_invalidates_old_event(self):
        approved_component(self.db,'test-charge')
        invoice=BillingService(self.store.finance).create_month(STAFF,1,'2026-10',source_key='invoice')
        adapter=MemoryTestPSP()
        payments=PhysicalPaymentService(self.store,adapter)
        bill=payments.prepare_invoice(STAFF,invoice['invoice_id'],review_reference='fixture',source_key='bill')
        consent=payments.record_consent(CUSTOMER,1,terms_reference='fixture',payment_method_reference='pm_test_fixture',
            provider_customer_reference='cus_test_fixture',source_key='consent')
        attempt=payments.create_attempt(CUSTOMER,bill['bill_id'],consent['consent_id'],source_key='attempt')
        payments.dispatch_attempt(CUSTOMER,attempt['attempt_id'],source_key='dispatch')
        event=self.events('payment_failed')[0]
        self.assertEqual(event['execution_mode'],'test')
        self.assertFalse(event['external_delivery_allowed'])
        key=self.db.execute('SELECT provider_idempotency_key FROM stage5_payment_attempts WHERE attempt_id=?',(attempt['attempt_id'],)).fetchone()[0]
        adapter.advance(key,'succeeded')
        payments.reconcile_attempt(CUSTOMER,attempt['attempt_id'],source_key='reconcile')
        self.assertFalse(self.scanner.is_current(CUSTOMER,event))
        self.assertIsNone(self.home.customer(CUSTOMER)['money']['actual_unpaid_fees_yen'])

    def sandbox_job(self, actor=CUSTOMER, state='unknown'):
        from stage9_payment_schema import migrate_test_connection
        from stage9_payments import SandboxPayments
        from stage9_stripe import SandboxScope
        scope = SandboxScope('fixture-event-status-20261026', ((1,'cus_fixture1'),(2,'cus_fixture2')), 'https://sandbox.example.test')
        migrate_test_connection(self.db, sandbox_id=scope.sandbox_id, customer_ids=(1,2))
        @contextmanager
        def connection(write=False):
            yield self.store
        job = SandboxPayments(connection, scope).queue(actor, 'setup',
            terms_reference='fictional-only-terms', source_key='setup-'+str(actor.user_id))['job_id']
        self.db.execute('UPDATE stage9_payment_jobs SET state=? WHERE job_id=?', (state,job))
        return job

    def test_sandbox_payment_problem_appears_on_home_without_provider_details(self):
        job = self.sandbox_job(state='requires_action')
        events = self.events('payment_failed')
        self.assertEqual(len(events),1)
        event = events[0]
        self.assertEqual((event['source_type'],event['source_id']),('sandbox_payment',job))
        self.assertEqual(event['path'],'/payments/sandbox/'+job)
        self.assertEqual(event['execution_mode'],'test')
        self.assertFalse(event['external_delivery_allowed'])
        self.assertTrue(self.scanner.is_current(CUSTOMER,event))
        self.assertNotIn('cus_fixture1',str(event))
        self.assertNotIn('fictional-only-terms',str(event))
        self.assertTrue(any(row.get('source_id')==job for row in self.home.customer(CUSTOMER)['next_actions']))
        self.assertTrue(any(row.get('path')=='/payments/sandbox/'+job for row in self.home.staff(STAFF)['tasks']))

    def test_sandbox_payment_repeated_scan_is_stable_and_success_invalidates_notice(self):
        job = self.sandbox_job()
        before = self.events('payment_failed')[0]
        changes = self.db.total_changes
        self.assertEqual(before,self.events('payment_failed')[0])
        self.assertEqual(changes,self.db.total_changes)
        self.db.execute("UPDATE stage9_payment_jobs SET state='succeeded' WHERE job_id=?",(job,))
        self.assertEqual(self.events('payment_failed'),[])
        self.assertFalse(self.scanner.is_current(CUSTOMER,before))

    def test_sandbox_payment_customer_and_staff_finance_boundaries(self):
        self.sandbox_job()
        event = self.events('payment_failed')[0]
        self.assertEqual(self.scanner.scan(OTHER),[])
        self.assertFalse(self.scanner.is_current(OTHER,event))
        self.assertEqual(self.scanner.scan(Actor(8,True)),[])
        self.assertFalse(self.scanner.is_current(Actor(8,True),event))
        self.assertFalse(self.scanner.is_current(CUSTOMER,dict(event,path='/payments/sandbox/another')))
        self.assertEqual(len(self.scanner.scan(STAFF)),1)

    def test_sandbox_payment_non_attention_states_and_disabled_customer_are_suppressed(self):
        job = self.sandbox_job()
        event = self.events('payment_failed')[0]
        for state in ('queued','open','processing','succeeded','canceled','suppressed'):
            self.db.execute('UPDATE stage9_payment_jobs SET state=? WHERE job_id=?',(state,job))
            self.assertEqual(self.events('payment_failed'),[],state)
        self.db.execute("UPDATE stage9_payment_jobs SET state='unknown' WHERE job_id=?",(job,))
        self.db.execute('UPDATE stage9_payment_customers SET enabled=0 WHERE customer_id=1')
        self.assertEqual(self.events('payment_failed'),[])
        self.assertFalse(self.scanner.is_current(CUSTOMER,event))

    def test_sandbox_payment_all_attention_states_have_distinct_safe_events(self):
        job = self.sandbox_job()
        keys = set()
        for state in ('unknown','failed','requires_action','requires_payment_method','requires_confirmation','requires_capture'):
            self.db.execute('UPDATE stage9_payment_jobs SET state=? WHERE job_id=?',(state,job))
            event = self.events('payment_failed')[0]
            keys.add(event['dedupe_key'])
            self.assertTrue(self.scanner.is_current(CUSTOMER,event))
        self.assertEqual(len(keys),6)

    def test_older_database_without_sandbox_tables_still_scans_without_writes(self):
        before = self.db.total_changes
        self.assertEqual(self.events('payment_failed'),[])
        self.assertEqual(self.db.total_changes,before)

    def test_disabled_sandbox_route_hides_candidate_and_invalidates_old_link_only(self):
        self.sandbox_job()
        old = self.events('payment_failed')[0]
        self.register(); self.deadline()
        scanner = EventScanner(self.store, clock=lambda:self.now, sandbox_payments_enabled=False)
        changes = self.db.total_changes
        self.assertFalse(any(event['source_type']=='sandbox_payment' for event in scanner.scan(CUSTOMER)))
        self.assertTrue(any(event['event_type']=='storage_due' for event in scanner.scan(CUSTOMER)))
        self.assertFalse(scanner.is_current(CUSTOMER,old))
        self.assertEqual(self.db.total_changes,changes)
        self.assertEqual(self.db.execute('SELECT state FROM stage9_payment_jobs').fetchone()[0],'unknown')


if __name__=='__main__':
    unittest.main()
