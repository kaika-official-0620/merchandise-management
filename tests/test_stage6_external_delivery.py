"""Controlled wire capture only: no real provider, recipient or credential."""
import hashlib
import io
import json
import smtplib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import test_stage6_notifications as fixture
from item_registry import Registry, RegistryError
from stage6_common import Stage6Store
from stage6_notifications import NotificationService, suppress_customer_notification_jobs
from stage6_transports import DeliveryResult
from stage6_external_transports import (TestDeliveryPolicy,ExpoTestTransport,LineTestTransport,SmtpTestTransport,
    transports_from_config,official_https)

PUSH='ExpoPushToken[fixturePushToken12345]'
LINE='U'+'a'*32
EMAIL='customer@example.invalid'


def policy(channel,recipient,customer=1):
    return TestDeliveryPolicy(True,'test',({'channel':channel,'customer_id':customer,
        'recipient_sha256':hashlib.sha256(recipient.encode()).hexdigest(),'approval_reference':'fictional-explicit-approval'},))


def request(recipient):
    return {'delivery_id':'a'*32,'notification_id':'b'*32,'customer_id':1,'test_only':True,
        'recipient':recipient,'title':'【検証】確認があります','body':'ログインしてご確認ください。'}


class WireTests(unittest.TestCase):
    def test_default_flag_cannot_construct_network_transport(self):
        self.assertEqual(transports_from_config({}),{})
        with self.assertRaises(RegistryError):
            transports_from_config({'STAGE6_TEST_DELIVERY_ENABLED':True,'KAIKA_RUNTIME_ENV':'production'})

    def test_unapproved_and_wrong_customer_block_before_http(self):
        sent=[];adapter=ExpoTestTransport(policy('push',PUSH),http=lambda *args:sent.append(args))
        for changes in ({'recipient':'ExpoPushToken[otherFixtureToken12345]'},{'customer_id':2},{'test_only':False}):
            with self.assertRaises(RegistryError):adapter.send({**request(PUSH),**changes})
        self.assertEqual(sent,[])

    def test_ambiguous_or_missing_approval_reference_is_rejected(self):
        row=policy('push',PUSH).approvals[0]
        for approvals in ((row,row),({**row,'approval_reference':''},)):
            with self.assertRaises(RegistryError):TestDeliveryPolicy(True,'test',approvals).require('push',request(PUSH))

    def test_expo_ticket_receipt_and_invalid_destination(self):
        replies=[(200,{}, {'data':{'status':'ok','id':'fictional-ticket'}}),
            (200,{}, {'data':{'fictional-ticket':{'status':'ok'}}}),
            (200,{}, {'data':{'fictional-ticket':{'status':'error','details':{'error':'DeviceNotRegistered'}}}})]
        captured=[]
        def http(plan,token):captured.append(plan);return replies.pop(0)
        adapter=ExpoTestTransport(policy('push',PUSH),http=http)
        self.assertEqual(adapter.send(request(PUSH)).state,'accepted')
        self.assertEqual(adapter.retrieve(request(PUSH),'fictional-ticket',60).state,'provider_accepted')
        self.assertEqual(adapter.retrieve(request(PUSH),'fictional-ticket',60).code,'invalid_destination')
        self.assertEqual(captured[1]['json'],{'ids':['fictional-ticket']})
        self.assertNotIn('customer_id',captured[0]['json']['data'])

    def test_provider_5xx_is_unknown_not_safe_retry(self):
        for cls,recipient in ((ExpoTestTransport,PUSH),(LineTestTransport,LINE)):
            kwargs={'access_token':'fictional-token'} if cls is LineTestTransport else {}
            adapter=cls(policy('line' if cls is LineTestTransport else 'push',recipient),http=lambda *a:(503,{},{}),**kwargs)
            self.assertEqual(adapter.send(request(recipient)).state,'unknown')

    def test_line_reuses_uuid_and_conflict_is_acceptance(self):
        captured=[]
        def http(plan,token):captured.append(plan);return 409,{'X-Line-Accepted-Request-Id':'fictional-request'},{}
        adapter=LineTestTransport(policy('line',LINE),access_token='fictional-token',http=http)
        self.assertEqual(adapter.send(request(LINE)).state,'accepted')
        self.assertEqual(adapter.send(request(LINE)).reference,'fictional-request')
        self.assertEqual(captured[0]['headers'],captured[1]['headers'])

    def test_line_link_token_official_request_and_allowlist(self):
        captured=[]
        def http(plan,token):captured.append(plan);return 200,{},{'linkToken':'fictional-link-token-1234567'}
        adapter=LineTestTransport(policy('line',LINE),access_token='fictional-token',http=http)
        self.assertEqual(adapter.link_token(LINE),'fictional-link-token-1234567')
        self.assertTrue(captured[0]['url'].endswith('/'+LINE+'/linkToken'))
        with self.assertRaises(RegistryError):adapter.link_token('U'+'b'*32)
        self.assertEqual(len(captured),1)

    def test_https_capture_uses_fixed_endpoint_no_proxy_redirect_and_bounded_response(self):
        class Response(io.BytesIO):
            status=200;headers={}
        captured=[]
        class Opener:
            def open(self,req,timeout):captured.append((req,timeout));return Response(b'{"data":{}}')
        with patch('stage6_external_transports.urllib.request.build_opener',return_value=Opener()) as built:
            self.assertEqual(official_https({'url':'https://exp.host/--/api/v2/push/getReceipts','method':'POST','json':{'ids':['fake']}},'fixture-token')[0],200)
            with self.assertRaises(RegistryError):official_https({'url':'https://other.invalid/','method':'POST'},'fixture-token')
        self.assertEqual(len(captured),1)
        self.assertEqual(captured[0][1],15)
        self.assertEqual(captured[0][0].get_header('Authorization'),'Bearer fixture-token')
        self.assertEqual(json.loads(captured[0][0].data),{'ids':['fake']})
        self.assertEqual(len(built.call_args.args),3)

    def smtp(self,send=None,quit_error=False):
        captured=[]
        class SMTP:
            def __init__(self,**kwargs):captured.append(('connect',kwargs))
            def ehlo(self):captured.append(('ehlo',))
            def starttls(self,context):captured.append(('tls',context.verify_mode))
            def login(self,user,password):captured.append(('login',))
            def send_message(self,message,**kwargs):
                captured.append(('message',message,kwargs))
                return send() if send else {}
            def quit(self):
                if quit_error:raise TimeoutError('fixture private detail')
            def close(self):pass
        return SmtpTestTransport(policy('email',EMAIL),host='smtp.example.invalid',port=587,
            sender='kaika@example.invalid',username='fixture',password='fixture',smtp_factory=SMTP),captured

    def test_smtp_tls_and_single_envelope_and_quit_cannot_erase_acceptance(self):
        adapter,captured=self.smtp(quit_error=True)
        result=adapter.send(request(EMAIL))
        self.assertEqual(result.state,'accepted')
        self.assertEqual([row[0] for row in captured],['connect','ehlo','tls','ehlo','login','message'])
        self.assertEqual(captured[-1][2],{'from_addr':'kaika@example.invalid','to_addrs':[EMAIL]})
        self.assertIn('@example.invalid>',result.reference)

    def test_smtp_explicit_temporary_refusal_safe_retry(self):
        def rejected():raise smtplib.SMTPRecipientsRefused({EMAIL:(450,b'fictional temporary refusal')})
        adapter,_=self.smtp(rejected)
        self.assertEqual(adapter.send(request(EMAIL)).code,'retryable_rejection')

    def test_smtp_no_plaintext_or_bad_port(self):
        adapter,captured=self.smtp();adapter.port=25
        with self.assertRaises(RegistryError):adapter.send(request(EMAIL))
        self.assertEqual(captured,[])

    def test_smtp_unknown_has_no_automatic_retrieve_or_resend(self):
        adapter,captured=self.smtp()
        self.assertEqual(adapter.retrieve(request(EMAIL),'',900).state,'unknown')
        self.assertEqual(captured,[])


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.NotificationTests('test_inapp_survives_all_external_channels_unconfigured')
        self.f.setUp();self.addCleanup(self.f.doCleanups)

    def external(self,http):
        adapter=ExpoTestTransport(policy('push',PUSH),http=http)
        self.f.service.transports={'push':adapter}
        self.f.service.customer_active=lambda customer:True
        return adapter

    def test_controlled_adapter_requires_lifecycle_hook(self):
        delivery=self.f.queued();claim=self.f.claim(delivery)
        self.external(lambda *a:None);self.f.service.customer_active=None
        with self.assertRaises(RegistryError):self.f.service.dispatch_claim(fixture.STAFF,delivery,claim['claim_token'])

    def test_external_call_observes_committed_sending_from_separate_connection(self):
        delivery=self.f.queued();claim=self.f.claim(delivery)
        with tempfile.TemporaryDirectory(prefix='kaika-notice-wire-') as root:
            path=Path(root)/'fixture.sqlite';db=sqlite3.connect(path);self.f.db.backup(db)
            def http(plan,token):
                self.assertFalse(db.in_transaction)
                with closing(sqlite3.connect(path)) as other:
                    self.assertEqual(other.execute('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(delivery,)).fetchone()[0],'sending')
                return 200,{},{'data':{'status':'ok','id':'fixture-ticket'}}
            api=NotificationService(Stage6Store(Registry(db)),transports={'push':ExpoTestTransport(policy('push',PUSH),http=http)},
                clock=lambda:self.f.now,validator=lambda *a:True,destination_validator=lambda *a:True,customer_active=lambda c:True)
            try:self.assertEqual(api.dispatch_claim(fixture.STAFF,delivery,claim['claim_token'])['state'],'accepted')
            finally:db.close()

    def test_account_pause_after_claim_suppresses_without_network(self):
        delivery=self.f.queued();claim=self.f.claim(delivery);calls=[]
        self.external(lambda *a:calls.append(a));self.f.service.customer_active=lambda customer:False
        self.assertEqual(self.f.service.dispatch_claim(fixture.STAFF,delivery,claim['claim_token'])['state'],'suppressed')
        self.assertEqual(calls,[])

    def test_unknown_receipt_keeps_ticket_and_cannot_retry(self):
        delivery=self.f.queued();claim=self.f.claim(delivery)
        self.external(lambda *a:(200,{}, {'data':{'status':'ok','id':'fixture-ticket'}}))
        self.f.service.dispatch_claim(fixture.STAFF,delivery,claim['claim_token'])
        self.f.now+=901
        self.external(lambda *a:(200,{}, {'data':{}}))
        result=self.f.service.reconcile(fixture.STAFF,delivery)
        self.assertEqual((result['state'],result['outcome_code']),('unknown','receipt_unavailable'))
        self.assertEqual(self.f.db.execute('SELECT provider_reference FROM stage6_notification_outbox WHERE delivery_id=?',(delivery,)).fetchone()[0],'fixture-ticket')
        with self.assertRaises(RegistryError):self.f.service.retry_rejected(fixture.STAFF,delivery,source_key=self.f.key())

    def test_transport_timeout_holds_unknown_and_no_second_call(self):
        delivery=self.f.queued();claim=self.f.claim(delivery);calls=[]
        def timeout(*args):calls.append(1);raise TimeoutError('fixture secret not logged')
        self.external(timeout)
        self.assertEqual(self.f.service.dispatch_claim(fixture.STAFF,delivery,claim['claim_token'])['state'],'unknown')
        with self.assertRaises(RegistryError):self.f.service.dispatch_claim(fixture.STAFF,delivery,claim['claim_token'])
        self.assertEqual(calls,[1])

    def test_lifecycle_hook_preserves_audit_and_revokes_capabilities_only_on_closure(self):
        self.f.email(confirm=False);delivery=self.f.queued();self.f.claim(delivery)
        result=suppress_customer_notification_jobs(self.f.service.store,1,'account_deletion_pending',now=self.f.now)
        self.assertEqual(result['paused_deliveries'],2)
        self.assertIsNone(self.f.db.execute('SELECT revoked_at FROM stage6_destinations').fetchone()[0])
        suppress_customer_notification_jobs(self.f.service.store,1,'account_closed',revoke=True,now=self.f.now)
        self.assertIsNotNone(self.f.db.execute('SELECT revoked_at FROM stage6_destinations').fetchone()[0])
        self.assertIsNotNone(self.f.db.execute('SELECT consumed_at FROM stage6_destination_proofs').fetchone()[0])
        self.assertGreater(self.f.db.execute('SELECT count(*) FROM stage6_delivery_audit').fetchone()[0],2)

    def test_real_lifecycle_request_and_withdrawal_do_not_replay_queue(self):
        from account_lifecycle import LifecycleService, migration_statements, outbound_allowed_in_store
        for sql in migration_statements():self.f.db.execute(sql)
        self.f.db.commit()
        delivery=self.f.queued();claim=self.f.claim(delivery);calls=[]
        self.external(lambda *a:calls.append(a))
        self.f.service.customer_active=lambda customer:outbound_allowed_in_store(self.f.service.store,customer)
        lifecycle=LifecycleService(self.f.service.store,clock=lambda:'2026-10-25T01:00:00+00:00')
        result=lifecycle.request(fixture.CUSTOMER);self.f.db.commit()
        self.assertFalse(outbound_allowed_in_store(self.f.service.store,1))
        self.assertEqual(self.f.db.execute('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(delivery,)).fetchone()[0],'suppressed')
        lifecycle.withdraw(fixture.CUSTOMER,result['request_id']);self.f.db.commit()
        self.assertTrue(outbound_allowed_in_store(self.f.service.store,1))
        with self.assertRaises(RegistryError):self.f.service.dispatch_claim(fixture.STAFF,delivery,claim['claim_token'])
        self.assertEqual(calls,[])
        self.assertEqual(self.f.db.execute('SELECT state FROM stage6_notification_outbox WHERE delivery_id=?',(delivery,)).fetchone()[0],'suppressed')

    def test_line_issued_token_is_bound_and_secret_not_in_operation_audit(self):
        invitation=self.f.line_invitation();token='fixture-line-token-123456'
        prepared=self.f.service.prepare_line_token(fixture.STAFF,invitation,LINE,token,source_key=self.f.key())
        self.f.service.record_line_invitation_result(fixture.STAFF,prepared['invitation_id'],DeliveryResult('accepted'),source_key=self.f.key())
        with self.assertRaises(RegistryError):self.f.service.begin_line_link(fixture.OTHER,invitation,link_token=token,expected_customer_id=1,source_key=self.f.key())
        with self.assertRaises(RegistryError):self.f.service.begin_line_link(fixture.CUSTOMER,invitation,link_token='wrong-token',expected_customer_id=1,source_key=self.f.key())
        proof=self.f.service.begin_line_link(fixture.CUSTOMER,invitation,link_token=token,expected_customer_id=1,source_key=self.f.key())
        self.assertTrue(proof['nonce'])
        raw=str(self.f.db.execute('SELECT result_json FROM stage4_operations').fetchall())
        self.assertNotIn(token,raw);self.assertNotIn(proof['nonce'],raw);self.assertNotIn(invitation,raw)

    def test_line_invitation_reserves_shared_monthly_budget(self):
        invitation=self.f.line_invitation()
        self.f.service.monthly_limits['line']=0
        with self.assertRaises(RegistryError):self.f.service.prepare_line_token(fixture.STAFF,invitation,LINE,'fixture-line-token',source_key=self.f.key())
        self.f.service.monthly_limits['line']=1
        self.f.service.prepare_line_token(fixture.STAFF,invitation,LINE,'fixture-line-token',source_key=self.f.key())
        self.assertEqual(self.f.service.usage(fixture.STAFF)['line']['reserved_or_attempted'],1)

    def test_line_pause_between_nonce_and_callback_does_not_link(self):
        invitation=self.f.line_invitation();proof=self.f.service.begin_line_link(fixture.CUSTOMER,invitation,source_key=self.f.key())
        self.f.service.customer_active=lambda customer:False
        event={'type':'accountLink','webhookEventId':'paused-fixture','source':{'userId':LINE},'link':{'result':'ok','nonce':proof['nonce']}}
        with self.assertRaises(RegistryError):self.f.service.receive_line_webhook(fixture.STAFF,*self.f.signed([event]),source_key=self.f.key())
        self.assertEqual(self.f.db.execute("SELECT count(*) FROM stage6_destinations WHERE channel='line'").fetchone()[0],0)


if __name__=='__main__':unittest.main()
