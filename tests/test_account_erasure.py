import json
import sqlite3
import os
import hashlib
from unittest.mock import patch

from account_erasure import ErasureService, migration_statements
from account_lifecycle import access_allowed_in_store
from item_registry import Actor
from test_account_lifecycle import LifecycleFixture


class ErasureTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        from stage6_notifications_schema import migration_statements as notifications
        for sql in notifications()+migration_statements():
            self.db.execute(sql)
        self.db.commit()
        self.erasure=ErasureService(self.store, clock=lambda:'2026-10-26T10:00:00+09:00')
        self.request=self.api.request(self.customer)

    def prepare(self):
        return self.erasure.prepare(self.staff,1,self.request['request_id'])

    def decide(self,plan,key,action='erase',**kwargs):
        return self.erasure.decide(self.staff,1,self.request['request_id'],revision=plan['revision'],
            task_key=key,action=action,reason='架空データのみの削除検証',reference='fixture-policy-v1',**kwargs)

    def execute(self,plan):
        return self.erasure.rehearse_local(self.staff,1,self.request['request_id'],fixture_database=self.path,revision=plan['revision'])

    def test_explicit_migration_and_repeat(self):
        for sql in migration_statements():self.db.execute(sql)
        self.assertTrue(self.erasure.ready())
        self.db.execute('DELETE FROM account_erasure_schema')
        self.error('account_erasure_migration_required',self.prepare)

    def test_plan_is_idempotent_counts_only_and_incomplete(self):
        one=self.prepare();two=self.prepare()
        self.assertEqual(one,two)
        self.assertFalse(one['complete'])
        self.assertIn('money',one['unresolved'])
        serialized=json.dumps(one['inventory'])
        self.assertNotIn('@example.invalid',serialized)
        self.assertNotIn('fictional address',serialized)
        self.assertEqual(1,self.store.query('SELECT COUNT(*) AS n FROM account_erasure_events')[0]['n'])

    def test_customer_and_unpermitted_staff_denied(self):
        for actor in (self.customer,self.other,Actor(4),Actor(1,True)):
            self.error('account_access_denied',lambda:self.erasure.prepare(actor,1,self.request['request_id']))

    def test_withdrawn_and_old_request_plan_cannot_execute(self):
        plan=self.decide(self.decide(self.prepare(),'profile'),'notifications')
        self.api.withdraw(self.customer,self.request['request_id'])
        self.error('deletion_state_conflict',lambda:self.execute(plan))
        new=self.api.request(self.customer)
        self.error('deletion_state_conflict',lambda:self.execute(plan))
        self.assertNotEqual(new['request_id'],self.request['request_id'])
        self.assertTrue(access_allowed_in_store(self.store,1))

    def test_decisions_require_revision_evidence_and_explicit_retention(self):
        plan=self.prepare()
        newer=self.decide(plan,'profile')
        self.error('erasure_plan_stale',lambda:self.decide(plan,'notifications'))
        self.error('retention_expiry_required',lambda:self.decide(newer,'records','retain'))
        self.error('retention_expiry_required',lambda:self.decide(newer,'records','retain',retain_until='2026-10-25'))
        self.error('erasure_scope_unresolved',lambda:self.decide(newer,'money','erase'))
        self.error('erasure_scope_unresolved',lambda:self.decide(newer,'unclassified','erase'))
        held=self.decide(newer,'records','retain',retain_until='2027-10-26')
        self.assertEqual('2027-10-26',held['decisions']['records']['retain_until'])
        self.assertFalse(held['complete'])

    def test_unreviewed_plan_cannot_rehearse(self):
        plan=self.prepare()
        self.error('erasure_evidence_required',lambda:self.execute(plan))
        self.assertTrue(access_allowed_in_store(self.store,1))

    def seed_notifications(self):
        for customer in (1,2):
            self.db.execute('INSERT INTO stage6_destinations VALUES (?,?,?,?,?,?,?,?,?,?)',
                ('dest'+str(customer),customer,'email','fixture'+str(customer)+'@example.invalid','hash'+str(customer),None,None,None,1,None))
            self.db.execute('INSERT INTO stage6_destination_proofs VALUES (?,?,?,?,?,?,?,?)',
                ('proof'+str(customer),customer,'email','fixture'+str(customer)+'@example.invalid','proofhash'+str(customer),9999999999,1,None))
            self.db.execute('INSERT INTO stage6_notification_preferences VALUES (?,?,?,?,?)',(customer,'invoice_draft','email',1,1))
            self.db.execute('''INSERT INTO stage6_notification_outbox(delivery_id,customer_id,destination_id,proof_id,channel,private_payload_json,state,due_at,created_at,test_only)
                VALUES (?,?,?,?,'email',?,'queued',1,1,1)''',('delivery'+str(customer),customer,'dest'+str(customer),'proof'+str(customer),'containing fictional private proof'))
        self.db.commit()

    def seed_home_preferences(self):
        from stage6_common import migration_statements as home_schema
        for sql in home_schema():
            self.db.execute(sql)
        self.db.executemany('INSERT INTO stage6_home_preferences VALUES(?,?,?)',
            [(1, '["inventory"]', 'fixture-one'), (2, '["sales"]', 'fixture-two')])
        self.db.commit()

    def test_home_preferences_erasure_requires_review_and_preserves_other_customer(self):
        self.seed_home_preferences()
        before_other = self.store.query('SELECT * FROM stage6_home_preferences WHERE customer_id=2')
        plan = self.prepare()
        self.error('erasure_evidence_required', lambda: self.execute(plan))
        self.assertEqual(2, len(self.store.query('SELECT * FROM stage6_home_preferences')))
        plan = self.decide(plan, 'profile')
        self.error('erasure_evidence_required', lambda: self.execute(plan))
        self.assertEqual(2, len(self.store.query('SELECT * FROM stage6_home_preferences')))
        result = self.execute(self.decide(plan, 'notifications'))
        self.assertEqual(1, result['counts']['stage6_home_preferences'])
        self.assertFalse(self.store.query('SELECT * FROM stage6_home_preferences WHERE customer_id=1'))
        self.assertEqual(before_other, self.store.query('SELECT * FROM stage6_home_preferences WHERE customer_id=2'))
        self.assertEqual(1155, self.store.query('SELECT amount FROM invoices')[0]['amount'])
        self.assertFalse(result['complete'])

    def test_home_preferences_rollback_with_fixture_erasure_failure(self):
        self.seed_home_preferences()
        before = self.store.query('SELECT * FROM stage6_home_preferences ORDER BY customer_id')
        plan = self.decide(self.decide(self.prepare(), 'profile'), 'notifications')
        original = self.erasure._scrub_notification_fields

        def fail_after_settings_scrub(customer_id):
            original(customer_id)
            raise RuntimeError('fictional-post-scrub-failure')

        with patch.object(self.erasure, '_scrub_notification_fields', side_effect=fail_after_settings_scrub):
            with self.assertRaises(RuntimeError):
                self.execute(plan)
        self.assertEqual(before, self.store.query('SELECT * FROM stage6_home_preferences ORDER BY customer_id'))
        self.assertTrue(access_allowed_in_store(self.store, 1))
        self.assertFalse(self.erasure.get(self.staff, 1, self.request['request_id'])['fixture_result'])

    def test_fixture_scrubs_local_secrets_preserves_money_other_customer_and_audit(self):
        self.seed_notifications()
        plan=self.decide(self.decide(self.prepare(),'profile'),'notifications')
        before_other=self.store.query('SELECT * FROM stage6_destinations WHERE customer_id=2')
        result=self.execute(plan)
        self.assertTrue(result['fixture_only']);self.assertFalse(result['complete'])
        self.assertEqual('erasure_pending',self.api.get(self.staff,1)['state'])
        self.assertFalse(access_allowed_in_store(self.store,1))
        self.assertEqual('',self.store.query('SELECT email FROM users WHERE id=1')[0]['email'])
        self.assertEqual(30000,self.store.query('SELECT proxy_service_budget FROM users WHERE id=1')[0]['proxy_service_budget'])
        self.assertEqual(1155,self.store.query('SELECT amount FROM invoices')[0]['amount'])
        self.assertFalse(self.store.query('SELECT * FROM mobile_sessions WHERE user_id=1'))
        self.assertFalse(self.store.query('SELECT * FROM push_devices WHERE user_id=1'))
        self.assertFalse(self.store.query('SELECT * FROM stage6_notification_preferences WHERE customer_id=1'))
        self.assertEqual('',self.store.query('SELECT address FROM stage6_destinations WHERE customer_id=1')[0]['address'])
        proof=self.store.query('SELECT * FROM stage6_destination_proofs WHERE customer_id=1')[0]
        self.assertEqual('',proof['address']);self.assertEqual(0,proof['expires_at'])
        self.assertNotEqual('proofhash1',proof['token_hash'])
        self.assertEqual('{}',self.store.query('SELECT private_payload_json FROM stage6_notification_outbox WHERE customer_id=1')[0]['private_payload_json'])
        self.assertEqual(before_other,self.store.query('SELECT * FROM stage6_destinations WHERE customer_id=2'))
        self.assertTrue(self.store.query('SELECT * FROM stage6_delivery_audit'))
        self.assertEqual(result,self.execute(plan))
        self.assertEqual(1,self.store.query("SELECT COUNT(*) AS n FROM account_erasure_events WHERE kind='local_fixture_erasure'")[0]['n'])

    def test_failure_rolls_back_closure_scrubbing_and_audit(self):
        plan=self.decide(self.decide(self.prepare(),'profile'),'notifications')
        with patch.object(self.erasure,'_scrub_notification_fields',side_effect=RuntimeError('fixture failure')):
            with self.assertRaises(RuntimeError):self.execute(plan)
        self.assertTrue(access_allowed_in_store(self.store,1))
        self.assertEqual('fixture1@example.invalid',self.store.query('SELECT email FROM users WHERE id=1')[0]['email'])
        self.assertFalse(self.erasure.get(self.staff,1,self.request['request_id'])['fixture_result'])
        self.assertFalse(self.store.query("SELECT * FROM account_deletion_events WHERE kind='fixture_access_closed'"))

    def test_wrong_database_and_missing_marker_are_denied(self):
        plan=self.decide(self.decide(self.prepare(),'profile'),'notifications')
        self.error('fixture_only',lambda:self.erasure.rehearse_local(self.staff,1,self.request['request_id'],fixture_database=self.path.parent/'account-fixture-other.sqlite3',revision=plan['revision']))
        self.path.with_suffix('.fixture.json').unlink()
        self.error('fixture_only',lambda:self.execute(plan))
        self.assertTrue(access_allowed_in_store(self.store,1))

    def test_plan_audit_append_only(self):
        self.prepare()
        with self.assertRaises(sqlite3.DatabaseError):self.db.execute('DELETE FROM account_erasure_events')
        with self.assertRaises(sqlite3.DatabaseError):self.db.execute("UPDATE account_erasure_events SET kind='changed'")

    def test_finished_local_scope_cannot_be_relabelled(self):
        plan=self.decide(self.decide(self.prepare(),'profile'),'notifications')
        self.execute(plan)
        latest=self.erasure.get(self.staff,1,self.request['request_id'])
        self.error('erasure_rehearsal_recorded',lambda:self.decide(latest,'profile','not_applicable'))

    def test_other_request_cannot_be_read_by_customer(self):
        self.prepare()
        self.error('account_access_denied',lambda:self.erasure.get(self.other,1,self.request['request_id']))

    def test_attached_database_and_temp_shadow_rejected(self):
        plan=self.decide(self.decide(self.prepare(),'profile'),'notifications')
        self.db.execute("ATTACH DATABASE ':memory:' AS other")
        self.error('fixture_only',lambda:self.execute(plan))
        self.db.execute('DETACH DATABASE other')
        self.db.execute('CREATE TEMP TABLE shadow_placeholder(value TEXT)')
        self.error('fixture_only',lambda:self.execute(plan))
        self.assertTrue(access_allowed_in_store(self.store,1))

    def test_hardlinked_database_is_not_disposable(self):
        plan=self.decide(self.decide(self.prepare(),'profile'),'notifications')
        alias=self.path.with_name('account-fixture-hardlink.sqlite3')
        os.link(self.path,alias)
        self.error('fixture_only',lambda:self.execute(plan))
        self.assertTrue(access_allowed_in_store(self.store,1))

    def test_one_completion_contact_is_held_but_normal_delivery_revoked(self):
        from account_erasure_notice import NoticeService, protected_destination
        address='completion@example.invalid'
        self.db.execute('INSERT INTO stage6_destinations VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('completion-destination',1,'email',address,hashlib.sha256(address.encode()).hexdigest(),None,None,None,1,None))
        self.db.commit()
        notices=NoticeService(self.store)
        notices.set_consent(self.customer,self.request['request_id'],True,source_key='fixture-completion-yes')
        plan=self.decide(self.decide(self.prepare(),'profile'),'notifications')
        result=self.execute(plan)
        self.assertTrue(result['completion_contact_held'])
        destination=self.store.query('SELECT * FROM stage6_destinations WHERE destination_id=?',('completion-destination',))[0]
        self.assertEqual(address,destination['address'])
        self.assertIsNotNone(destination['revoked_at'])
        self.assertEqual('completion-destination',protected_destination(self.store,1))
        self.assertEqual('',self.store.query('SELECT email FROM users WHERE id=1')[0]['email'])
        self.assertFalse(result['complete'])
