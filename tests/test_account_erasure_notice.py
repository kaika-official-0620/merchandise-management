import hashlib
import json
import unittest
from unittest.mock import patch

from account_erasure_notice import (NoticeService, CapturedCompletionTransport,
    migration_statements, protected_destination)
from account_erasure import migration_statements as erasure_statements
from account_lifecycle import outbound_allowed_in_store
from stage6_notifications_schema import migration_statements as notification_statements
from test_account_lifecycle import LifecycleFixture


class CompletionNoticeTests(LifecycleFixture):
    def setUp(self):
        super().setUp()
        for sql in notification_statements()+erasure_statements():self.db.execute(sql)
        self.email='completion-owner@example.invalid'
        self.db.execute('''INSERT INTO stage6_destinations(destination_id,customer_id,channel,address,address_hash,verified_at)
            VALUES('email-owner',1,'email',?,?,123)''',(self.email,hashlib.sha256(self.email.encode()).hexdigest()))
        self.db.commit()
        self.request=self.api.request(self.customer)
        self.notices=NoticeService(self.store)
        self.capture=CapturedCompletionTransport()
        self.completion_digest=hashlib.sha256(b'unit-only-not-an-integrated-completion').hexdigest()

    def consent_and_close(self):
        self.notices.set_consent(self.customer,self.request['request_id'],True)
        self.assertEqual('email-owner',self.notices.hold_for_erasure(self.staff,1,self.request['request_id']))
        self.close(self.request['request_id'])
        self.db.commit()

    def mark_fixture_complete(self, *, record_journal=True):
        # Explicit sender-unit-test evidence, never an integrated completion
        # claim. The workflow suite uses ErasureService.complete_fixture instead.
        self.db.execute("UPDATE account_deletion_requests SET state='completed',version=version+1 WHERE customer_id=1")
        completion={'request_id':self.request['request_id'],'customer_id':1,
            'plan_id':'unit-notice-plan','complete':True,'state':'completed',
            'evidence_sha256':self.completion_digest}
        self.db.execute('''INSERT INTO account_erasure_plans(plan_id,request_id,customer_id,
            inventory_json,created_at,updated_at,fixture_result_json) VALUES(?,?,1,'[]','unit','unit',?)''',
            ('unit-notice-plan',self.request['request_id'],json.dumps({'completion':completion})))
        if record_journal:
            self.db.execute('''INSERT INTO account_erasure_events VALUES(
                'unit-notice-completion','unit-notice-plan',3,'fixture_completed',?,'unit')''',
                (json.dumps(completion,sort_keys=True,ensure_ascii=False),))
        self.db.commit()

    def dispatch(self,**overrides):
        args=dict(fixture_database=self.path,completion_evidence=self.completion_digest,
            sender='completion@example.invalid',transport=self.capture)
        args.update(overrides)
        return self.notices.dispatch_fixture(self.staff,1,self.request['request_id'],**args)

    def test_consent_binds_verified_reference_without_copying_address(self):
        result=self.notices.set_consent(self.customer,self.request['request_id'],True)
        self.assertTrue(result['consented'])
        self.assertEqual('email-owner',protected_destination(self.store,1))
        stored=self.store.query('SELECT * FROM account_erasure_notices')[0]
        self.assertNotIn(self.email,json.dumps(stored))
        self.assertNotIn('address_hash',result)
        self.assertNotIn('destination_id',result)
        self.notices.set_consent(self.customer,self.request['request_id'],True)
        self.assertEqual(1,self.store.query('SELECT COUNT(*) AS n FROM account_erasure_notice_events')[0]['n'])

    def test_no_consent_never_prevents_account_closure(self):
        self.assertIsNone(self.notices.hold_for_erasure(self.staff,1,self.request['request_id']))
        self.close(self.request['request_id'])
        self.assertIsNone(protected_destination(self.store,1))
        self.assertEqual('erasure_pending',self.api.get(self.staff,1)['state'])

    def test_unverified_or_multiple_email_addresses_require_resolution(self):
        first = self.notices.get(self.customer, 1, self.request['request_id'])
        self.assertTrue(first['can_opt_in'])
        self.assertNotIn(self.email, json.dumps(first))
        self.db.execute('DELETE FROM stage6_destinations')
        empty = self.notices.get(self.customer, 1, self.request['request_id'])
        self.assertFalse(empty['can_opt_in'])
        self.assertEqual('completion_email_not_verified', empty['availability_error'])
        self.error('completion_email_not_verified',lambda:self.notices.set_consent(self.customer,self.request['request_id'],True))
        for index in range(2):
            address=f'fictional-{index}@example.invalid'
            self.db.execute("INSERT INTO stage6_destinations(destination_id,customer_id,channel,address,address_hash,verified_at) VALUES(?,1,'email',?,?,123)",
                ('email-'+str(index),address,hashlib.sha256(address.encode()).hexdigest()))
        multiple = self.notices.get(self.customer, 1, self.request['request_id'])
        self.assertFalse(multiple['can_opt_in'])
        self.assertEqual('completion_email_selection_required', multiple['availability_error'])
        self.error('completion_email_selection_required',lambda:self.notices.set_consent(self.customer,self.request['request_id'],True))

    def test_other_customer_cannot_consent_or_read(self):
        self.error('deletion_state_conflict',lambda:self.notices.set_consent(self.other,self.request['request_id'],True))
        self.error('account_access_denied',lambda:self.notices.get(self.other,1,self.request['request_id']))

    def test_consent_withdrawal_releases_protection(self):
        self.notices.set_consent(self.customer,self.request['request_id'],True)
        self.notices.set_consent(self.customer,self.request['request_id'],False)
        self.assertIsNone(protected_destination(self.store,1))
        self.assertIsNone(self.notices.hold_for_erasure(self.staff,1,self.request['request_id']))
        self.assertFalse(self.notices.get(self.customer,1,self.request['request_id'])['consented'])

    def test_revoked_destination_before_hold_is_not_reactivated(self):
        self.notices.set_consent(self.customer,self.request['request_id'],True)
        self.db.execute('UPDATE stage6_destinations SET revoked_at=124')
        self.assertIsNone(self.notices.hold_for_erasure(self.staff,1,self.request['request_id']))
        self.assertIsNone(protected_destination(self.store,1))
        self.assertEqual(124,self.store.query('SELECT revoked_at FROM stage6_destinations')[0]['revoked_at'])

    def test_changed_destination_address_cannot_inherit_old_consent(self):
        self.notices.set_consent(self.customer,self.request['request_id'],True)
        self.assertTrue(self.notices.get(self.customer, 1, self.request['request_id'])['consent_matches_destination'])
        changed='someone-else@example.invalid'
        self.db.execute('UPDATE stage6_destinations SET address=?,address_hash=?',(changed,hashlib.sha256(changed.encode()).hexdigest()))
        self.assertFalse(self.notices.get(self.customer, 1, self.request['request_id'])['consent_matches_destination'])
        self.assertIsNone(protected_destination(self.store,1))
        self.assertIsNone(self.notices.hold_for_erasure(self.staff,1,self.request['request_id']))

    def test_erasure_pending_never_sends_completion_mail(self):
        self.consent_and_close()
        self.error('completion_notice_erasure_not_complete',self.dispatch)
        self.assertEqual([],self.capture.messages)
        self.assertEqual('email-owner',protected_destination(self.store,1))

    def test_completed_fixture_sends_once_releases_address_and_preserves_normal_stop(self):
        self.consent_and_close();self.mark_fixture_complete()
        result=self.dispatch()
        self.assertEqual('accepted',result['state'])
        self.assertFalse(result['held'])
        self.assertEqual(self.email,self.capture.messages[0]['recipient'])
        self.assertEqual('accepted',self.dispatch()['state'])
        self.assertEqual(1,len(self.capture.messages))
        self.assertFalse(outbound_allowed_in_store(self.store,1))
        destination=self.store.query('SELECT * FROM stage6_destinations')[0]
        self.assertIsNotNone(destination['revoked_at'])
        self.assertEqual('',destination['address'])
        self.assertNotEqual(hashlib.sha256(self.email.encode()).hexdigest(),destination['address_hash'])
        notice=self.store.query('SELECT * FROM account_erasure_notices')[0]
        for field in ('destination_id','address_hash','verified_at','held_at'):
            self.assertIsNone(notice[field])
        self.assertEqual(1,notice['attempts'])
        self.assertTrue(notice['consented_at'])
        self.assertIsNone(protected_destination(self.store,1))

    def test_state_completed_without_matching_record_cannot_capture(self):
        self.consent_and_close()
        self.db.execute("UPDATE account_deletion_requests SET state='completed' WHERE customer_id=1")
        self.db.commit()
        self.error('completion_notice_evidence_required',self.dispatch)
        self.assertEqual([],self.capture.messages)
        self.assertEqual(0,self.store.query('SELECT attempts FROM account_erasure_notices')[0]['attempts'])

    def test_completion_state_and_plan_without_append_only_journal_cannot_capture(self):
        self.consent_and_close();self.mark_fixture_complete(record_journal=False)
        self.error('completion_notice_evidence_required',self.dispatch)
        self.assertEqual([],self.capture.messages)
        self.assertEqual(0,self.store.query('SELECT attempts FROM account_erasure_notices')[0]['attempts'])
        self.assertEqual(self.email,self.store.query('SELECT address FROM stage6_destinations')[0]['address'])

    def test_completion_record_must_match_customer_request_plan_state_and_digest(self):
        self.consent_and_close();self.mark_fixture_complete()
        original=json.loads(self.store.query('SELECT fixture_result_json FROM account_erasure_plans')[0]['fixture_result_json'])
        changes=(('customer_id',2),('customer_id',True),('request_id','other-request'),
            ('plan_id','other-plan'),('state','erasure_pending'),('complete',False),
            ('evidence_sha256','0'*64))
        for field,value in changes:
            with self.subTest(field=field,value=value):
                modified={'completion':dict(original['completion'],**{field:value})}
                self.db.execute('UPDATE account_erasure_plans SET fixture_result_json=?',(json.dumps(modified),))
                self.db.commit()
                self.error('completion_notice_evidence_required',self.dispatch)
        for invalid in ('[]','null','{"completion":[]}','invalid-json'):
            self.db.execute('UPDATE account_erasure_plans SET fixture_result_json=?',(invalid,))
            self.db.commit()
            self.error('completion_notice_evidence_required',self.dispatch)
        self.assertEqual([],self.capture.messages)
        self.assertEqual(0,self.store.query('SELECT attempts FROM account_erasure_notices')[0]['attempts'])

    def test_accepted_replay_cannot_substitute_a_different_completion_digest(self):
        self.consent_and_close();self.mark_fixture_complete();self.dispatch()
        self.error('completion_notice_evidence_required',lambda:self.dispatch(completion_evidence='0'*64))
        self.assertEqual(1,len(self.capture.messages))

    def test_changed_or_reverified_held_address_does_not_inherit_consent(self):
        self.consent_and_close();self.mark_fixture_complete()
        self.db.execute('UPDATE stage6_destinations SET verified_at=456')
        self.db.commit()
        self.error('completion_notice_destination_unavailable',self.dispatch)
        other='changed-after-hold@example.invalid'
        self.db.execute('UPDATE stage6_destinations SET verified_at=123,address=?,address_hash=?',
            (other,hashlib.sha256(other.encode()).hexdigest()))
        self.db.commit()
        self.error('completion_notice_destination_unavailable',self.dispatch)
        self.assertEqual([],self.capture.messages)
        self.assertEqual(0,self.store.query('SELECT attempts FROM account_erasure_notices')[0]['attempts'])

    def test_missing_consent_timestamp_cannot_capture_even_with_a_held_row(self):
        self.consent_and_close();self.mark_fixture_complete()
        self.db.execute('UPDATE account_erasure_notices SET consented_at=NULL')
        self.db.commit()
        self.error('completion_notice_consent_required',self.dispatch)
        self.assertEqual([],self.capture.messages)

    def test_dispatch_requires_staff_tenant_match_and_current_notice_migration(self):
        self.consent_and_close();self.mark_fixture_complete()
        arguments=dict(fixture_database=self.path,completion_evidence=self.completion_digest,
            sender='completion@example.invalid',transport=self.capture)
        self.error('account_access_denied',lambda:self.notices.dispatch_fixture(
            self.customer,1,self.request['request_id'],**arguments))
        self.error('deletion_state_conflict',lambda:self.notices.dispatch_fixture(
            self.staff,2,self.request['request_id'],**arguments))
        self.db.execute('DELETE FROM account_erasure_notice_schema')
        self.db.commit()
        self.error('account_erasure_notice_migration_required',self.dispatch)
        self.assertEqual([],self.capture.messages)
        self.assertEqual(0,self.store.query('SELECT attempts FROM account_erasure_notices')[0]['attempts'])

    def test_failed_capture_is_held_for_review_and_never_automatically_retried(self):
        self.consent_and_close();self.mark_fixture_complete()
        capture=CapturedCompletionTransport('failed')
        self.assertEqual('failed',self.dispatch(transport=capture)['state'])
        self.error('completion_notice_no_automatic_retry',lambda:self.dispatch(transport=capture))
        self.assertEqual(1,len(capture.messages))
        self.assertEqual('email-owner',protected_destination(self.store,1))

    def test_unknown_outcome_never_automatically_retries(self):
        self.consent_and_close();self.mark_fixture_complete()
        capture=CapturedCompletionTransport('unknown')
        self.assertEqual('unknown',self.dispatch(transport=capture)['state'])
        self.error('completion_notice_no_automatic_retry',lambda:self.dispatch(transport=capture))
        self.assertEqual(1,len(capture.messages))
        self.assertEqual('email-owner',protected_destination(self.store,1))

    def test_capture_exception_persists_unknown_without_error_details(self):
        self.consent_and_close();self.mark_fixture_complete()
        with patch.object(CapturedCompletionTransport,'deliver',side_effect=TimeoutError('private provider diagnostic')):
            self.assertEqual('unknown',self.dispatch()['state'])
        row=self.store.query('SELECT * FROM account_erasure_notices')[0]
        self.assertNotIn('private',json.dumps(row))
        self.error('completion_notice_no_automatic_retry',self.dispatch)

    def test_external_adapter_or_missing_sender_evidence_are_refused(self):
        self.consent_and_close();self.mark_fixture_complete()
        self.error('completion_notice_capture_only',lambda:self.dispatch(transport=object()))
        self.error('completion_notice_fixture_sender_required',lambda:self.dispatch(sender=None))
        self.error('completion_notice_fixture_sender_required',lambda:self.dispatch(sender='real@example.com'))
        self.error('completion_notice_evidence_required',lambda:self.dispatch(completion_evidence=''))
        self.assertEqual([],self.capture.messages)

    def test_sender_runs_only_after_committed_claim_and_rejects_caller_open_transaction(self):
        self.consent_and_close();self.mark_fixture_complete()
        self.db.execute("UPDATE account_deletion_requests SET updated_at='fixture'")
        self.error('completion_notice_committed_connection_required',self.dispatch)
        self.db.commit()
        original=CapturedCompletionTransport.deliver
        def checked(transport,**kwargs):
            self.assertFalse(self.db.in_transaction)
            self.assertEqual('sending',self.store.query('SELECT state FROM account_erasure_notices')[0]['state'])
            return original(transport,**kwargs)
        with patch.object(CapturedCompletionTransport,'deliver',checked):self.dispatch()

    def test_withdrawal_and_new_request_never_reuse_old_email_consent(self):
        self.notices.set_consent(self.customer,self.request['request_id'],True)
        self.api.withdraw(self.customer,self.request['request_id'])
        self.assertIsNone(protected_destination(self.store,1))
        new=self.api.request(self.customer)
        self.assertIsNone(protected_destination(self.store,1))
        self.assertFalse(self.notices.get(self.customer,1,new['request_id'])['consented'])

    def test_attached_database_cannot_use_fixture_sender(self):
        self.consent_and_close();self.mark_fixture_complete()
        self.db.execute("ATTACH DATABASE ':memory:' AS unrelated")
        self.error('fixture_only',self.dispatch)
        self.assertEqual([],self.capture.messages)

    def test_late_opt_in_replay_cannot_undo_later_consent_withdrawal(self):
        request_id=self.request['request_id']
        self.notices.set_consent(self.customer,request_id,True,source_key='first-opt-in')
        self.notices.set_consent(self.customer,request_id,False,source_key='later-opt-out')
        result=self.notices.set_consent(self.customer,request_id,True,source_key='first-opt-in')
        self.assertFalse(result['consented'])
        self.assertIsNone(protected_destination(self.store,1))

    def test_notice_operation_cannot_be_reused_for_changed_intent_or_customer(self):
        self.notices.set_consent(self.customer,self.request['request_id'],True,source_key='one-source')
        self.error('completion_notice_operation_conflict',lambda:self.notices.set_consent(self.customer,self.request['request_id'],False,source_key='one-source'))
        other_request=self.api.request(self.other)
        self.error('completion_notice_operation_conflict',lambda:self.notices.set_consent(self.other,other_request['request_id'],True,source_key='one-source'))


if __name__=='__main__':unittest.main()
