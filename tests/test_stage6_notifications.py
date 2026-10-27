"""Offline notification lifecycle, permissions, proof and durable worker boundaries."""
import base64
import hashlib
import hmac
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from item_registry import Actor, Registry, RegistryError
from stage6_common import Stage6Store, migration_statements as common_schema
from stage6_notifications_schema import migration_statements
from stage6_notifications import NotificationService, EVENT_TEXT
from stage6_transports import DeliveryResult, MemoryNotificationTransport
from stage3_funds_schema import migration_statements as funds_schema
from test_stage5_payments import fixture, STAFF, CUSTOMER, OTHER


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.db=fixture()
        self.addCleanup(self.db.close)
        for sql in list(funds_schema())+common_schema()+migration_statements():
            self.db.execute(sql)
        self.db.commit()
        self.now=1792580400
        self.serial=0
        self.current=True
        self.adapter=MemoryNotificationTransport()
        self.service=NotificationService(Stage6Store(Registry(self.db)),
            transports={x:self.adapter for x in ('email','push','line')},clock=lambda:self.now,
            validator=lambda actor,event:self.current,destination_validator=lambda dest:True,
            monthly_limits={'email':20,'line':20,'push':20})

    def key(self):
        self.serial+=1
        return 'notification-operation-'+str(self.serial)

    def error(self,code,call):
        with self.assertRaises(RegistryError) as caught:
            call()
        self.assertEqual(code,caught.exception.code)

    def event(self,**changes):
        return dict(customer_id=1,event_type='listing_approval',source_type='listing',source_id='listing-fixture',
            source_version='v1',dedupe_key='listing:fixture:v1',physical_item_id='item1',ownership_assignment_id='assignment1',
            path='/commerce/listings/abc',execution_mode='test',external_delivery_allowed=False,**changes)

    def publish(self,event=None):
        return self.service.publish_event(STAFF,event or self.event(),source_key=self.key())['notification_id']

    def preferences(self,kind='listing_approval',channels=('email','push','line')):
        self.service.set_preferences(CUSTOMER,kind,list(channels),source_key=self.key())

    def register_push(self,actor=CUSTOMER,token='ExpoPushToken[fixturePushToken12345]',installation='installation-fixture-1',binding='binding-fixture-123456789012345678901'):
        return self.service.register_push(actor,token,installation,binding,'ios',source_key=self.key())['destination_id']

    def email(self,address='customer@example.invalid',confirm=True):
        challenge=self.service.request_email_verification(CUSTOMER,address,source_key=self.key())
        row=self.db.execute('SELECT private_payload_json FROM stage6_notification_outbox WHERE proof_id=?',(challenge['challenge_id'],)).fetchone()
        token=parse_qs(urlparse(json.loads(row[0])['verification_url']).fragment)['token'][0]
        if confirm:
            self.service.confirm_email(CUSTOMER,token,source_key=self.key())
        return token

    def queued(self):
        self.register_push();self.preferences(channels=('push',));notice=self.publish()
        return self.service.get_notification(CUSTOMER,notice)['deliveries'][0]['delivery_id']

    def claim(self,delivery):
        claim=self.service.claim(STAFF,delivery,source_key=self.key());self.db.commit()
        return claim

    def send(self,delivery):
        claim=self.claim(delivery)
        return self.service.dispatch_claim(STAFF,delivery,claim['claim_token'])

    def signed(self,events):
        secret='fictional-channel-secret-12345'
        body=json.dumps({'events':events},separators=(',',':')).encode()
        signature=base64.b64encode(hmac.new(secret.encode(),body,hashlib.sha256).digest()).decode()
        return body,signature,secret

    def line_invitation(self):
        event=dict(type='follow',webhookEventId='line-fixture-follow',source={'userId':'U'+'a'*32})
        result=self.service.receive_line_webhook(STAFF,*self.signed([event]),source_key=self.key())
        return result['internal_invitations'][0]['invitation_token']

    def link_line(self):
        token=self.line_invitation()
        challenge=self.service.begin_line_link(CUSTOMER,token,source_key=self.key())
        event=dict(type='accountLink',webhookEventId='line-fixture-link',source={'userId':'U'+'a'*32},link={'result':'ok','nonce':challenge['nonce']})
        self.service.receive_line_webhook(STAFF,*self.signed([event]),source_key=self.key())
        return challenge,event

    def test_inapp_survives_all_external_channels_unconfigured(self):
        notice=self.publish()
        result=self.service.get_notification(CUSTOMER,notice)
        self.assertTrue(result['is_current']);self.assertEqual(result['deliveries'],[])
        self.assertEqual(len(self.service.inbox(CUSTOMER)),1)
        self.assertFalse(self.service.settings(CUSTOMER)['external_delivery_enabled'])

    def test_tenant_and_staff_read_does_not_mark_customer_read(self):
        notice=self.publish()
        self.error('customer_access_denied',lambda:self.service.get_notification(OTHER,notice))
        self.error('notification_owner_required',lambda:self.service.mark_read(STAFF,notice,source_key=self.key()))
        self.service.mark_read(CUSTOMER,notice,source_key=self.key())
        self.assertEqual(self.service.get_notification(CUSTOMER,notice)['read_at'],self.now)

    def test_source_validator_required_and_stale_link_removed(self):
        notice=self.publish();self.current=False
        self.assertIsNone(self.service.get_notification(CUSTOMER,notice)['path'])
        self.error('notification_source_not_current',lambda:self.publish())
        self.service.validator=None
        self.error('notification_source_not_current',lambda:self.publish())

    def test_event_and_operation_dedup(self):
        notice=self.publish()
        self.assertEqual(notice,self.publish())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage6_notifications').fetchone()[0],1)

    def test_source_changes_before_transaction_commit_refuses_publication(self):
        calls=[]
        def validator(actor,event):
            calls.append(True)
            return len(calls)==1
        self.service.validator=validator
        self.error('notification_source_not_current',lambda:self.publish())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage6_notifications').fetchone()[0],0)

    def test_finance_notifications_require_finance_staff_permission(self):
        self.register_push();self.preferences('invoice_draft',('push',))
        event=self.event();event.update(event_type='invoice_draft',dedupe_key='invoice-1',path='/finance/months/invoice-1')
        notice=self.publish(event)
        delivery=self.service.get_notification(CUSTOMER,notice)['deliveries'][0]['delivery_id']
        limited=Actor(8,True)
        self.error('finance_permission_required',lambda:self.service.get_notification(limited,notice))
        self.error('finance_permission_required',lambda:self.service.claim(limited,delivery,source_key=self.key()))
        self.error('finance_permission_required',lambda:self.service.reconcile(limited,delivery))
        self.assertEqual(self.service.inbox(limited,1),[])
        self.assertEqual(self.service.deliveries(limited),[])
        self.preferences('storage_due',('push',))
        event.update(event_type='storage_due',dedupe_key='deadline-1',path='/inventory/items/item1')
        normal=self.publish(event)
        self.assertEqual(self.service.get_notification(limited,normal)['event_kind'],'storage_due')
        self.assertEqual(len(self.service.deliveries(limited)),1)

    def test_stale_claim_recovery_respects_finance_permission(self):
        self.register_push();self.preferences('invoice_draft',('push',))
        event=self.event();event.update(event_type='invoice_draft',dedupe_key='invoice-1',path='/finance/months/invoice-1')
        notice=self.publish(event)
        financial=self.service.get_notification(CUSTOMER,notice)['deliveries'][0]['delivery_id']
        self.claim(financial)
        self.preferences('storage_due',('push',))
        event.update(event_type='storage_due',dedupe_key='deadline-1',path='/inventory/items/item1')
        normal=self.service.get_notification(CUSTOMER,self.publish(event))['deliveries'][0]['delivery_id']
        self.claim(normal);self.now+=901
        result=self.service.recover_stale_claims(Actor(8,True),source_key=self.key())
        self.assertEqual(result['recovered'],1)
        self.assertEqual(self.service._delivery(STAFF,financial)['state'],'claimed')
        self.assertEqual(self.service._delivery(STAFF,normal)['state'],'queued')

    def test_customer_choice_path_is_local_and_safe(self):
        event=self.event();event.update(event_type='return_selection',path='/notifications/items/item1/choice')
        self.assertEqual(self.service.get_notification(CUSTOMER,self.publish(event))['path'],event['path'])

    def test_source_version_change_creates_history(self):
        first=self.publish()
        event=self.event();event.update(source_version='v2',dedupe_key='listing:fixture:v2')
        second=self.publish(event)
        self.assertNotEqual(first,second)
        self.assertEqual(len(self.service.inbox(CUSTOMER)),2)

    def test_generic_body_ignores_source_sensitive_payload(self):
        event=self.event();event.update(title='PRIVATE CUSTOMER',body='CARD 4111111111111111',balance=50000)
        notice=self.service.get_notification(CUSTOMER,self.publish(event))
        serialized=json.dumps(notice)
        self.assertNotIn('PRIVATE',serialized);self.assertNotIn('4111',serialized);self.assertNotIn('50000',serialized)

    def test_unsafe_paths_rejected(self):
        for path in ('https://evil.invalid','//evil.invalid','/commerce/../admin','/payments/x?customer=2','/finance/%2e%2e/admin'):
            event=self.event();event['path']=path
            self.error('notification_path_invalid',lambda:self.publish(event))

    def test_preferences_start_opted_out_and_are_per_event(self):
        self.register_push();self.preferences(channels=('push',))
        event=self.event();event.update(event_type='storage_due',dedupe_key='storage:1')
        self.assertEqual(self.service.get_notification(CUSTOMER,self.publish(event))['deliveries'],[])
        self.assertEqual(len(self.service.get_notification(CUSTOMER,self.publish())['deliveries']),1)

    def test_invalid_preferences_rejected(self):
        for kind,channels in [('bad',['push']),('storage_due',['push','push']),('storage_due',['sms'])]:
            self.error('notification_preferences_invalid',lambda:self.service.set_preferences(CUSTOMER,kind,channels,source_key=self.key()))

    def test_email_verification_has_hashed_proof_no_projection_secret(self):
        token=self.email(confirm=False)
        projection=json.dumps(self.service.settings(CUSTOMER))+json.dumps(self.service.deliveries(CUSTOMER))
        self.assertNotIn(token,projection);self.assertNotIn('customer@example',projection)
        self.assertNotIn(token,str(self.db.execute('SELECT token_hash FROM stage6_destination_proofs').fetchall()))
        self.assertEqual(self.service.settings(CUSTOMER)['destinations'],[])

    def test_email_proof_bound_to_customer_one_use_and_expiry(self):
        token=self.email(confirm=False)
        self.error('verification_invalid_or_expired',lambda:self.service.confirm_email(OTHER,token,source_key=self.key()))
        self.service.confirm_email(CUSTOMER,token,source_key=self.key())
        self.error('verification_invalid_or_expired',lambda:self.service.confirm_email(CUSTOMER,token,source_key=self.key()))
        token=self.email('second@example.invalid',confirm=False);self.now+=1800
        self.error('verification_invalid_or_expired',lambda:self.service.confirm_email(CUSTOMER,token,source_key=self.key()))

    def test_email_old_challenge_invalidated_and_rate_limited(self):
        first=self.email(confirm=False);self.email('second@example.invalid',confirm=False)
        self.error('verification_invalid_or_expired',lambda:self.service.confirm_email(CUSTOMER,first,source_key=self.key()))
        self.email('third@example.invalid',confirm=False)
        self.error('verification_rate_limit',lambda:self.email('fourth@example.invalid',confirm=False))

    def test_email_header_injection_rejected(self):
        self.error('email_address_invalid',lambda:self.email('a@example.com\r\nBcc:evil@example.com'))

    def test_email_verification_has_its_own_queue_and_does_not_send_on_request(self):
        self.email(confirm=False)
        self.assertEqual(self.adapter.sent,[])
        delivery=self.service.deliveries(CUSTOMER)[0]['delivery_id']
        self.send(delivery)
        self.assertIn('verification_url',self.adapter.sent[-1])

    def test_verified_email_replacement_retires_old(self):
        self.email();self.email('new@example.invalid')
        destinations=self.service.settings(CUSTOMER)['destinations']
        self.assertEqual(sum(x['revoked_at'] is None for x in destinations),1)

    def test_same_push_refresh_keeps_queued_destination(self):
        first=self.register_push();second=self.register_push()
        self.assertEqual(first,second)
        self.assertEqual(len(self.service.settings(CUSTOMER)['destinations']),1)

    def test_push_account_switch_invalidates_old_destination(self):
        delivery=self.queued();self.register_push(OTHER)
        self.assertEqual(self.claim(delivery)['state'],'suppressed')

    def test_push_logout_invalidates_pending(self):
        delivery=self.queued()
        self.service.revoke_push_binding(CUSTOMER,'binding-fixture-123456789012345678901',source_key=self.key())
        self.assertEqual(self.claim(delivery)['state'],'suppressed')

    def test_push_missing_native_resolver_fails_closed(self):
        delivery=self.queued();self.service.destination_validator=None
        self.assertEqual(self.claim(delivery)['state'],'suppressed')

    def test_multiple_devices_each_get_one_delivery(self):
        self.register_push();self.register_push(installation='installation-fixture-2',token='ExpoPushToken[fixturePushToken67890]')
        self.preferences(channels=('push',))
        notice=self.publish()
        self.assertEqual(len(self.service.get_notification(CUSTOMER,notice)['deliveries']),2)
        self.publish();self.assertEqual(len(self.service.get_notification(CUSTOMER,notice)['deliveries']),2)

    def test_revoke_other_destination_denied(self):
        destination=self.register_push()
        self.error('notification_destination_not_found',lambda:self.service.revoke_destination(OTHER,destination,source_key=self.key()))

    def test_line_signature_verified_before_parse(self):
        self.error('line_signature_invalid',lambda:self.service.receive_line_webhook(STAFF,b'notjson','bad','fixture-secret-1234',source_key=self.key()))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage6_line_invitations').fetchone()[0],0)

    def test_line_flow_requires_signed_source_and_matching_nonce_user(self):
        token=self.line_invitation();proof=self.service.begin_line_link(CUSTOMER,token,source_key=self.key())
        event=dict(type='accountLink',webhookEventId='wrong-user',source={'userId':'U'+'b'*32},link={'result':'ok','nonce':proof['nonce']})
        self.error('line_link_proof_invalid',lambda:self.service.receive_line_webhook(STAFF,*self.signed([event]),source_key=self.key()))
        self.assertEqual(self.service.settings(CUSTOMER)['destinations'],[])

    def test_line_nonce_one_use_redelivery_dedup_and_no_id_projection(self):
        proof,event=self.link_line()
        self.service.receive_line_webhook(STAFF,*self.signed([event]),source_key=self.key())
        self.assertEqual(len(self.service.settings(CUSTOMER)['destinations']),1)
        self.assertNotIn('U'+'a'*32,json.dumps(self.service.settings(CUSTOMER)))
        event['webhookEventId']='new-event-same-nonce'
        self.error('line_link_proof_invalid',lambda:self.service.receive_line_webhook(STAFF,*self.signed([event]),source_key=self.key()))

    def test_line_repeated_provider_id_cannot_change_business_content(self):
        proof,event=self.link_line()
        event['deliveryContext']={'isRedelivery':True}
        self.service.receive_line_webhook(STAFF,*self.signed([event]),source_key=self.key())
        event['source']={'userId':'U'+'b'*32}
        self.error('line_event_conflict',lambda:self.service.receive_line_webhook(STAFF,*self.signed([event]),source_key=self.key()))

    def test_line_invitation_and_nonce_excluded_from_immutable_audit(self):
        event=dict(type='follow',webhookEventId='line-private-follow',source={'userId':'U'+'a'*32})
        args=self.signed([event])
        webhook_key=self.key()
        first=self.service.receive_line_webhook(STAFF,*args,source_key=webhook_key)
        invitation=first['internal_invitations'][0]['invitation_token']
        nonce_key=self.key()
        challenge=self.service.begin_line_link(CUSTOMER,invitation,source_key=nonce_key)
        nonce=challenge['nonce']
        audits='\n'.join(row[0] for row in self.db.execute('SELECT result_json FROM stage4_operations'))
        self.assertNotIn(invitation,audits)
        self.assertNotIn(nonce,audits)
        self.assertNotIn('internal_invitations',audits)
        repeated_hook=self.service.receive_line_webhook(STAFF,*args,source_key=webhook_key)
        self.assertEqual(repeated_hook['internal_invitations'],[])
        self.assertTrue(repeated_hook['invitation_replay_unavailable'])
        repeated_nonce=self.service.begin_line_link(CUSTOMER,invitation,source_key=nonce_key)
        self.assertEqual(challenge['proof_id'],repeated_nonce['proof_id'])
        self.assertNotIn('nonce',repeated_nonce)
        self.assertTrue(repeated_nonce['resume_requires_new_invitation'])

    def test_line_invitation_expiry_and_unfollow(self):
        token=self.line_invitation();self.now+=601
        self.error('line_invitation_invalid_or_expired',lambda:self.service.begin_line_link(CUSTOMER,token,source_key=self.key()))
        self.now-=601
        challenge=self.service.begin_line_link(CUSTOMER,token,source_key=self.key())
        event=dict(type='accountLink',webhookEventId='link',source={'userId':'U'+'a'*32},link={'result':'ok','nonce':challenge['nonce']})
        self.service.receive_line_webhook(STAFF,*self.signed([event]),source_key=self.key())
        event=dict(type='unfollow',webhookEventId='unfollow',source={'userId':'U'+'a'*32})
        self.service.receive_line_webhook(STAFF,*self.signed([event]),source_key=self.key())
        self.assertIsNotNone(self.service.settings(CUSTOMER)['destinations'][0]['revoked_at'])

    def test_dispatch_requires_committed_claim_and_staff(self):
        delivery=self.queued();self.db.commit();self.db.execute('BEGIN')
        claim=self.service.claim(STAFF,delivery,source_key=self.key())
        self.error('notification_claim_commit_required',lambda:self.service.dispatch_claim(STAFF,delivery,claim['claim_token']))
        self.assertEqual(self.adapter.sent,[])
        self.db.commit()
        self.service.dispatch_claim(STAFF,delivery,claim['claim_token'])
        self.assertEqual(len(self.adapter.sent),1)

    def test_default_transport_and_network_looking_adapter_rejected(self):
        delivery=self.queued();claim=self.claim(delivery)
        self.service.transports={}
        self.error('external_notification_delivery_disabled',lambda:self.service.dispatch_claim(STAFF,delivery,claim['claim_token']))
        class Unsafe(MemoryNotificationTransport):
            pass
        self.service.transports={'push':Unsafe()}
        self.error('external_notification_delivery_disabled',lambda:self.service.dispatch_claim(STAFF,delivery,claim['claim_token']))

    def test_claim_revalidated_after_settings_or_source_change(self):
        delivery=self.queued();claim=self.claim(delivery);self.current=False
        result=self.service.dispatch_claim(STAFF,delivery,claim['claim_token'])
        self.assertEqual(result['state'],'suppressed');self.assertEqual(self.adapter.sent,[])

    def test_preference_revoked_before_dispatch_suppressed(self):
        delivery=self.queued();claim=self.claim(delivery)
        self.preferences(channels=());self.db.commit()
        self.assertEqual(self.service.dispatch_claim(STAFF,delivery,claim['claim_token'])['state'],'suppressed')

    def test_success_is_provider_acceptance_not_delivered(self):
        delivery=self.queued();self.adapter.next_result=DeliveryResult('accepted','fixture-ticket')
        self.assertEqual(self.send(delivery)['state'],'accepted')
        self.adapter.results[delivery]=DeliveryResult('provider_accepted','fixture-ticket')
        self.assertEqual(self.service.reconcile(STAFF,delivery)['state'],'provider_accepted')
        self.assertNotIn('delivered',json.dumps(self.service.deliveries(CUSTOMER)))

    def test_timeout_unknown_not_blind_retry(self):
        delivery=self.queued();self.adapter.timeout_after_send=True
        self.assertEqual(self.send(delivery)['state'],'unknown')
        self.error('notification_retry_not_safe',lambda:self.service.retry_rejected(STAFF,delivery,source_key=self.key()))
        self.db.commit()
        self.assertEqual(self.service.reconcile(STAFF,delivery)['state'],'accepted')
        self.assertEqual(len(self.adapter.sent),1)

    def test_no_duplicate_dispatch_for_same_claim(self):
        delivery=self.queued();claim=self.claim(delivery)
        self.service.dispatch_claim(STAFF,delivery,claim['claim_token'])
        self.error('notification_claim_invalid',lambda:self.service.dispatch_claim(STAFF,delivery,claim['claim_token']))
        self.assertEqual(len(self.adapter.sent),1)

    def test_claim_capability_not_in_audit_or_replayed_operation(self):
        delivery=self.queued()
        source_key=self.key()
        initial=self.service.claim(STAFF,delivery,source_key=source_key)
        self.db.commit()
        token=initial['claim_token']
        audit='\n'.join(row[0] for row in self.db.execute('SELECT result_json FROM stage4_operations'))
        self.assertNotIn(token,audit)
        self.assertNotIn('claim_token',audit)
        replay=self.service.claim(STAFF,delivery,source_key=source_key)
        self.assertNotIn('claim_token',replay)
        self.assertTrue(replay['resume_requires_recovery'])
        self.assertEqual(self.adapter.sent,[])
        self.db.commit()
        self.service.dispatch_claim(STAFF,delivery,token)
        self.assertEqual(len(self.adapter.sent),1)

    def test_retry_only_explicit_rejection_with_backoff(self):
        delivery=self.queued();self.adapter.next_result=DeliveryResult('failed',code='retryable_rejection')
        self.send(delivery)
        result=self.service.retry_rejected(STAFF,delivery,source_key=self.key())
        self.assertGreater(result['due_at'],self.now)
        self.error('notification_not_claimable',lambda:self.claim(delivery))
        self.now=result['due_at'];self.db.commit();self.send(delivery)
        self.assertEqual(len(self.adapter.sent),2)

    def test_monthly_limit_account_wide_unknown_cost_not_zero(self):
        delivery=self.queued();self.service.monthly_limits['push']=1
        self.claim(delivery)
        event=self.event();event['dedupe_key']='second';notice=self.publish(event)
        second=self.service.get_notification(CUSTOMER,notice)['deliveries'][0]['delivery_id']
        self.error('notification_monthly_limit',lambda:self.claim(second))
        usage=self.service.usage(STAFF)['push']
        self.assertEqual(usage['reserved_or_attempted'],1);self.assertIsNone(usage['estimated_cost_yen'])

    def test_missing_budget_blocks_claim_without_losing_inbox(self):
        delivery=self.queued();self.service.monthly_limits={}
        self.error('notification_budget_unconfirmed',lambda:self.claim(delivery))
        self.assertEqual(len(self.service.inbox(CUSTOMER)),1)

    def test_device_not_registered_revokes_destination(self):
        delivery=self.queued();self.adapter.next_result=DeliveryResult('failed',code='invalid_destination')
        self.send(delivery)
        self.assertIsNotNone(self.service.settings(CUSTOMER)['destinations'][0]['revoked_at'])

    def test_worker_crash_claim_requeues_sending_stays_unknown(self):
        first=self.queued();old_claim=self.claim(first)
        self.now+=901
        self.service.recover_stale_claims(STAFF,source_key=self.key())
        self.assertEqual(self.service._delivery(STAFF,first)['state'],'queued')
        self.db.commit()
        self.error('notification_claim_invalid',lambda:self.service.dispatch_claim(STAFF,first,old_claim['claim_token']))
        self.claim(first)
        self.db.execute("UPDATE stage6_notification_outbox SET state='sending' WHERE delivery_id=?",(first,));self.db.commit()
        self.now+=901
        self.service.recover_stale_claims(STAFF,source_key=self.key())
        self.assertEqual(self.service._delivery(STAFF,first)['state'],'unknown')

    def test_audit_append_only_and_no_billing_mutation(self):
        delivery=self.queued();self.send(delivery)
        with self.assertRaises(Exception):
            self.db.execute('DELETE FROM stage6_delivery_audit')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_fund_records').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage5_payment_attempts').fetchone()[0],0)

    def test_sending_state_visible_to_independent_connection_before_adapter_call(self):
        with tempfile.TemporaryDirectory(prefix='kaika-stage6-worker-') as directory:
            path=Path(directory)/'fixture.sqlite'
            target=sqlite3.connect(path)
            self.db.backup(target);target.close()
            db=sqlite3.connect(path);db.execute('PRAGMA foreign_keys=ON')
            service=NotificationService(Stage6Store(Registry(db)),transports={'push':self.adapter},clock=lambda:self.now,
                validator=lambda a,e:True,destination_validator=lambda d:True,monthly_limits={'push':20})
            try:
                service.register_push(CUSTOMER,'ExpoPushToken[fixturePushToken12345]','installation-fixture-1','binding-fixture-123456789012345678901','ios',source_key='register')
                service.set_preferences(CUSTOMER,'listing_approval',['push'],source_key='pref')
                service.publish_event(STAFF,self.event(),source_key='publish')
                delivery=service.deliveries(STAFF)[0]['delivery_id']
                claim=service.claim(STAFF,delivery,source_key='claim');db.commit()
                def inspect(request):
                    with closing(sqlite3.connect(path)) as independent:
                        state=independent.execute('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(delivery,)).fetchone()[0]
                    self.assertEqual(state,'sending')
                    self.assertFalse(db.in_transaction)
                    return DeliveryResult('accepted','fixture-ticket')
                with patch.object(MemoryNotificationTransport,'send',side_effect=inspect):
                    service.dispatch_claim(STAFF,delivery,claim['claim_token'])
            finally:
                db.close()

    def test_two_workers_same_claim_send_at_most_once(self):
        delivery=self.queued();claim=self.claim(delivery)
        with tempfile.TemporaryDirectory(prefix='kaika-stage6-concurrency-') as directory:
            path=Path(directory)/'fixture.sqlite'
            target=sqlite3.connect(path);self.db.backup(target);target.close()
            barrier=threading.Barrier(2)
            def worker():
                db=sqlite3.connect(path,timeout=5);db.execute('PRAGMA foreign_keys=ON')
                service=NotificationService(Stage6Store(Registry(db)),transports={'push':self.adapter},clock=lambda:self.now,
                    validator=lambda a,e:True,destination_validator=lambda d:True,monthly_limits={'push':20})
                try:
                    barrier.wait()
                    return service.dispatch_claim(STAFF,delivery,claim['claim_token'])['state']
                except (RegistryError,sqlite3.OperationalError):
                    return 'rejected'
                finally:
                    db.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:worker(),range(2)))
            self.assertEqual(results.count('accepted'),1)
            self.assertEqual(len(self.adapter.sent),1)


if __name__=='__main__':
    unittest.main()
