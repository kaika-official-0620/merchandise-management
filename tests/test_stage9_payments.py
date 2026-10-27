"""Isolated file-backed sandbox ledger; fake SDK transport, never Stripe I/O."""
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import hashlib
import hmac
import json
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit,parse_qs

from item_registry import Actor, Registry, RegistryError
from stage3_billing import BillingService
from stage5_common import Stage5Store
from stage5_payments import PhysicalPaymentService, MemoryTestPSP
from stage9_payment_schema import migrate_test_connection
from stage9_payments import SandboxPayments, suppress_customer_payment_jobs
from stage9_stripe import SandboxScope, StripeSandboxAdapter
from test_stage5_payments import fixture, approved_component, STAFF, CUSTOMER, OTHER


class FakeSDK:
    """Create/read realistic Stripe objects; assert no DB scope surrounds I/O."""
    def __init__(self,scope,assert_outside):
        self.scope,self.assert_outside=scope,assert_outside
        self.objects={}; self.requests=[]; self.reads=[]; self.keys={}
        self.on_create=None; self.raise_after=False; self.fail_reads=False; self.payment_status='succeeded'
        for customer,provider in scope.customer_map:
            self.objects[provider]=dict(id=provider,object='customer',livemode=False,metadata={
                'kaika_sandbox_fixture':scope.sandbox_id,'kaika_fixture_customer_id':str(customer)})
        def service(kind):
            return SimpleNamespace(create=lambda params,options:self.create(kind,params,options),retrieve=self.retrieve)
        self.client=SimpleNamespace(v1=SimpleNamespace(customers=service('customer'),
            checkout=SimpleNamespace(sessions=service('checkout.session')),
            payment_intents=service('payment_intent'),setup_intents=service('setup_intent'),
            payment_methods=service('payment_method'),refunds=service('refund')))
        self.Webhook=SimpleNamespace(construct_event=self.webhook)

    def StripeClient(self,key,**kwargs): return self.client

    def retrieve(self,identifier):
        self.assert_outside(); self.reads.append(identifier)
        if self.fail_reads: raise OSError('fake transport unavailable')
        return deepcopy(self.objects[identifier])

    def create(self,kind,params,options):
        self.assert_outside(); self.requests.append((kind,deepcopy(params),deepcopy(options)))
        if self.on_create: self.on_create()
        key=options['idempotency_key']
        if key in self.keys:
            identifier,old=self.keys[key]
            if params!=old: raise ValueError('fake idempotency mismatch')
            return deepcopy(self.objects[identifier])
        prefix={'checkout.session':'cs_test_','payment_intent':'pi_','refund':'re_'}[kind]
        identifier=prefix+str(len(self.keys)+1)
        record=dict(id=identifier,object=kind,livemode=False,metadata=params['metadata'])
        if kind=='checkout.session':
            record.update(mode=params['mode'],customer=params['customer'],client_reference_id=params['client_reference_id'],
                status='open',url='https://checkout.stripe.com/c/pay/'+identifier,setup_intent=None,
                payment_intent=None,payment_status='unpaid',currency='jpy')
            if params['mode']=='payment': record['amount_total']=params['line_items'][0]['price_data']['unit_amount']
        elif kind=='payment_intent':
            record.update(customer=params['customer'],currency='jpy',amount=params['amount'],amount_capturable=0,
                amount_received=params['amount'] if self.payment_status=='succeeded' else 0,status=self.payment_status,
                client_secret=identifier+'_secret_fixture')
        elif kind=='refund':
            record.update(payment_intent=params['payment_intent'],amount=params['amount'],currency='jpy',status='succeeded')
        self.objects[identifier]=record; self.keys[key]=(identifier,deepcopy(params))
        if self.raise_after: raise TimeoutError('fake response lost')
        return deepcopy(record)

    @staticmethod
    def webhook(raw,signature,secret,tolerance):
        if signature!='fixture-valid': raise ValueError('fake bad signature')
        return json.loads(raw)

    def finish(self,session):
        record=self.objects[session]; record['status']='complete'
        if record['mode']=='setup':
            setup='seti_'+session; method='pm_'+session
            self.objects[method]=dict(id=method,object='payment_method',livemode=False,type='card',customer=record['customer'])
            self.objects[setup]=dict(id=setup,object='setup_intent',livemode=False,customer=record['customer'],
                metadata=record['metadata'],usage='off_session',status='succeeded',payment_method=method)
            record['setup_intent']=setup
        else:
            pi='pi_'+session; record.update(payment_intent=pi,payment_status='paid')
            self.objects[pi]=dict(id=pi,object='payment_intent',livemode=False,customer=record['customer'],
                metadata=record['metadata'],currency='jpy',amount=record['amount_total'],amount_received=record['amount_total'],
                amount_capturable=0,status='succeeded')


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='kaika-stage9-payments-'); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'fixture.sqlite'; db=fixture(self.path)
        self.scope=SandboxScope('fixture-stage9-20261025',((1,'cus_fixture1'),(2,'cus_fixture2')),'https://sandbox.example.test')
        migrate_test_connection(db,sandbox_id=self.scope.sandbox_id,customer_ids=(1,2),allowed_test_path=self.path)
        approved_component(db,'one')
        store=Stage5Store(Registry(db)); invoice=BillingService(store.finance).create_month(STAFF,1,'2026-10',source_key='invoice')
        self.bill=PhysicalPaymentService(store).prepare_invoice(STAFF,invoice['invoice_id'],review_reference='fixture',source_key='bill')['bill_id']
        db.commit(); db.close(); self.local=threading.local(); self.now=1000000
        self.sdk=FakeSDK(self.scope,lambda:self.assertFalse(getattr(self.local,'inside',False),'Network inside DB transaction'))
        self.adapter=StripeSandboxAdapter(scope=self.scope,secret_key='sk_test_fixture123',webhook_secret='whsec_fixture123',
            sdk=self.sdk,allow_network=True,allow_writes=True,publishable_key='pk_test_fixture123')
        self.api=SandboxPayments(self.db,self.scope,self.adapter,clock=lambda:self.now)

    @contextmanager
    def db(self,write=False):
        conn=sqlite3.connect(self.path,timeout=5); conn.execute('PRAGMA foreign_keys=ON')
        self.local.inside=True
        try:
            if write: conn.execute('BEGIN IMMEDIATE')
            yield Stage5Store(Registry(conn))
            if write: conn.commit()
        except Exception: conn.rollback(); raise
        finally: conn.close(); self.local.inside=False

    def row(self,job):
        with self.db() as store: return store.query('SELECT * FROM stage9_payment_jobs WHERE job_id=?',(job,))[0]

    def checkout(self): return self.api.queue(CUSTOMER,'checkout',bill_id=self.bill,source_key='checkout')['job_id']

    def setup_card(self):
        job=self.api.queue(CUSTOMER,'setup',terms_reference='fixture:each-10th:approved-bill:revocable',source_key='setup')['job_id']
        self.api.dispatch(CUSTOMER,job); self.sdk.finish(self.row(job)['provider_id']); self.api.reconcile(CUSTOMER,job)
        return job

    def paid(self):
        job=self.checkout(); self.api.dispatch(CUSTOMER,job); self.sdk.finish(self.row(job)['provider_id'])
        self.assertEqual(self.api.reconcile(CUSTOMER,job)['state'],'succeeded'); return job

    def assertCode(self,code,fn):
        with self.assertRaises(RegistryError) as error: fn()
        self.assertEqual(code,error.exception.code)

    def event(self,job,event_id='evt_fixture',identifier=None,kind=None):
        row=self.row(job); identifier=identifier or row['provider_id']; obj=deepcopy(self.sdk.objects[identifier])
        if kind: obj['object']=kind
        return json.dumps(dict(id=event_id,object='event',livemode=False,data={'object':obj})).encode()

    def test_checkout_persist_dispatch_continuation_and_read_authority(self):
        job=self.checkout(); self.assertEqual(len(self.sdk.requests),0)
        self.assertEqual(self.api.dispatch(CUSTOMER,job)['state'],'open')
        self.assertEqual(self.api.continuation(CUSTOMER,job)['kind'],'redirect')
        self.assertCode('customer_payment_action_required',lambda:self.api.continuation(STAFF,job))
        self.assertCode('sandbox_reconciliation_required',lambda:self.api.dispatch(CUSTOMER,job))
        self.sdk.finish(self.row(job)['provider_id']); self.assertEqual(self.api.reconcile(CUSTOMER,job)['state'],'succeeded')
        self.assertNotIn('secret',json.dumps(self.api.get(CUSTOMER,job)))
        self.assertEqual(len(self.sdk.requests),1)

    def test_cross_customer_and_finance_permissions(self):
        job=self.checkout()
        for actor in (OTHER,Actor(8,True),Actor(7,True)):
            with self.assertRaises(RegistryError): self.api.get(actor,job)
        with self.assertRaises(RegistryError): self.api.queue(STAFF,'checkout',bill_id=self.bill,source_key='staff')
        with self.assertRaises(RegistryError): self.api.queue(CUSTOMER,'off_session',bill_id=self.bill,source_key='off')

    def test_queue_idempotency_and_single_bill(self):
        job=self.checkout(); self.assertEqual(self.checkout(),job)
        self.assertCode('source_key_conflict',lambda:self.api.queue(CUSTOMER,'setup',terms_reference='x',source_key='checkout'))
        self.assertCode('sandbox_payment_exists_reconcile_first',lambda:self.api.queue(CUSTOMER,'checkout',bill_id=self.bill,source_key='second'))

    def test_signature_duplicate_and_out_of_order_are_only_hints(self):
        job=self.checkout(); self.api.dispatch(CUSTOMER,job); body=self.event(job)
        self.assertCode('stripe_webhook_signature_invalid',lambda:self.api.accept_webhook(body,'invalid'))
        self.assertTrue(self.api.accept_webhook(body,'fixture-valid')['queued'])
        self.assertTrue(self.api.accept_webhook(body,'fixture-valid')['duplicate'])
        self.assertEqual(self.api.get(CUSTOMER,job)['state'],'open')
        changed=json.loads(body); changed['livemode']=True
        self.assertCode('stripe_webhook_environment_mismatch',lambda:self.api.accept_webhook(json.dumps(changed).encode(),'fixture-valid'))
        self.sdk.finish(self.row(job)['provider_id']); self.api.reconcile(CUSTOMER,job)
        self.api.accept_webhook(self.event(job,'evt_late'),'fixture-valid'); self.api.reconcile(CUSTOMER,job)
        self.assertEqual(self.api.get(CUSTOMER,job)['state'],'succeeded')
        changed=json.loads(body); changed['type']='changed'
        self.assertCode('stripe_event_content_changed',lambda:self.api.accept_webhook(json.dumps(changed).encode(),'fixture-valid'))

    def test_lost_create_response_recovery_uses_identical_key_and_payload(self):
        job=self.checkout(); self.sdk.raise_after=True
        self.assertEqual(self.api.dispatch(CUSTOMER,job)['state'],'unknown')
        self.sdk.raise_after=False
        self.assertEqual(self.api.dispatch(STAFF,job,recover=True)['state'],'open')
        self.assertEqual(self.sdk.requests[0],self.sdk.requests[1]); self.assertEqual(len(self.sdk.keys),1)

    def test_lost_response_webhook_recovers_without_new_create(self):
        job=self.checkout(); self.sdk.raise_after=True; self.api.dispatch(CUSTOMER,job)
        body=self.event(job,identifier='cs_test_1'); self.api.accept_webhook(body,'fixture-valid')
        self.assertEqual(self.api.reconcile(CUSTOMER,job)['state'],'open'); self.assertEqual(len(self.sdk.requests),1)

    def test_recovery_window_expired_does_not_reissue(self):
        job=self.checkout(); self.sdk.raise_after=True; self.api.dispatch(CUSTOMER,job); self.now+=23*3600
        self.assertCode('sandbox_idempotency_window_expired',lambda:self.api.dispatch(STAFF,job,recover=True))
        self.assertEqual(len(self.sdk.requests),1)

    def test_failure_after_create_retains_provider_reference_for_read(self):
        job=self.checkout()
        self.sdk.on_create=lambda:setattr(self.sdk,'fail_reads',True)
        self.assertEqual(self.api.dispatch(CUSTOMER,job)['state'],'unknown'); self.assertEqual(self.row(job)['provider_id'],'cs_test_1')
        self.sdk.fail_reads=False; self.assertEqual(self.api.reconcile(CUSTOMER,job)['state'],'open')
        self.assertEqual(len(self.sdk.requests),1)

    def test_running_lease_blocks_second_worker_outside_transactions(self):
        job=self.checkout(); entered=threading.Event(); release=threading.Event(); outputs=[]
        def hook(): entered.set(); release.wait(5)
        self.sdk.on_create=hook
        worker=threading.Thread(target=lambda:outputs.append(self.api.dispatch(CUSTOMER,job)))
        worker.start(); self.assertTrue(entered.wait(5))
        try: self.assertCode('sandbox_job_busy',lambda:self.api.dispatch(STAFF,job,recover=True))
        finally: release.set(); worker.join(6)
        self.assertEqual(outputs[0]['state'],'open'); self.assertEqual(len(self.sdk.requests),1)

    def test_consent_setup_off_session_and_customer_3ds(self):
        consent=self.setup_card(); self.sdk.payment_status='requires_action'
        job=self.api.queue(STAFF,'off_session',bill_id=self.bill,consent_id=consent,source_key='off')['job_id']
        self.assertEqual(self.api.dispatch(STAFF,job)['state'],'requires_action')
        secret=self.api.continuation(CUSTOMER,job); self.assertEqual(secret['kind'],'authentication')
        self.assertNotIn('client_secret',self.row(job)['payload_json'])
        with self.assertRaises(RegistryError): self.api.continuation(OTHER,job)
        self.assertCode('customer_payment_action_required',lambda:self.api.continuation(STAFF,job))

    def test_consent_revocation_suppresses_queued_payment(self):
        consent=self.setup_card()
        job=self.api.queue(STAFF,'off_session',bill_id=self.bill,consent_id=consent,source_key='off')['job_id']
        self.api.revoke_consent(CUSTOMER,consent)
        self.assertEqual(self.api.get(CUSTOMER,job)['state'],'suppressed')
        self.assertCode('sandbox_consent_revoked',lambda:self.api.dispatch(STAFF,job))
        self.assertEqual(len(self.sdk.requests),1)

    def test_refunds_reserve_capacity_and_match_parent(self):
        parent=self.paid()
        job=self.api.queue(STAFF,'refund',parent_job_id=parent,amount_yen=700,reason='fixture return',source_key='refund')['job_id']
        self.assertCode('refund_exceeds_original_payment',lambda:self.api.queue(STAFF,'refund',parent_job_id=parent,amount_yen=401,reason='other',source_key='excess'))
        self.assertEqual(self.api.dispatch(STAFF,job)['state'],'succeeded')
        with self.assertRaises(RegistryError): self.api.queue(CUSTOMER,'refund',parent_job_id=parent,amount_yen=1,reason='x',source_key='forbidden')

    def test_lifecycle_suppresses_queued_without_external_io(self):
        job=self.checkout()
        with self.db(True) as store: counts=suppress_customer_payment_jobs(store,1)
        self.assertEqual(counts,{'suppressed':1,'unknown':0})
        self.assertCode('sandbox_customer_not_allowed',lambda:self.api.dispatch(CUSTOMER,job))
        self.assertEqual(len(self.sdk.requests),0)

    def test_lifecycle_during_network_keeps_unknown_until_reconciliation(self):
        job=self.checkout()
        def deleted():
            with self.db(True) as store: suppress_customer_payment_jobs(store,1)
        self.sdk.on_create=deleted
        self.assertEqual(self.api.dispatch(CUSTOMER,job)['state'],'unknown')
        self.api.accept_webhook(self.event(job,identifier='cs_test_1'),'fixture-valid')
        self.assertEqual(self.api.reconcile(STAFF,job)['state'],'open')
        self.assertCode('sandbox_customer_not_allowed',lambda:self.api.continuation(CUSTOMER,job))

    def test_lifecycle_predicate_is_called_after_commit(self):
        job=self.checkout()
        def denied(customer):
            self.assertFalse(self.local.inside); return False
        self.api.outbound_allowed=denied
        self.assertEqual(self.api.dispatch(CUSTOMER,job)['state'],'unknown'); self.assertEqual(len(self.sdk.requests),0)

    def test_revoke_consent_during_provider_customer_read_prevents_payment_write(self):
        consent=self.setup_card()
        job=self.api.queue(STAFF,'off_session',bill_id=self.bill,consent_id=consent,source_key='off')['job_id']
        original=self.sdk.client.v1.customers.retrieve
        def revoke(identifier):
            result=original(identifier); self.api.revoke_consent(CUSTOMER,consent); return result
        self.sdk.client.v1.customers.retrieve=revoke
        self.assertEqual(self.api.dispatch(STAFF,job)['state'],'unknown')
        self.assertEqual(len(self.sdk.requests),1) # only the earlier setup

    def test_revoke_consent_during_provider_card_read_prevents_payment_write(self):
        consent=self.setup_card()
        job=self.api.queue(STAFF,'off_session',bill_id=self.bill,consent_id=consent,source_key='off')['job_id']
        original=self.sdk.client.v1.payment_methods.retrieve
        def revoke(identifier):
            result=original(identifier); self.api.revoke_consent(CUSTOMER,consent); return result
        self.sdk.client.v1.payment_methods.retrieve=revoke
        self.api.dispatch(STAFF,job); self.assertEqual(len(self.sdk.requests),1)

    def test_actual_deletion_request_during_customer_read_prevents_checkout_write(self):
        from account_lifecycle import LifecycleService,migration_statements
        job=self.checkout()
        with self.db(True) as store:
            for sql in migration_statements(): store.execute(sql)
        original=self.sdk.client.v1.customers.retrieve
        def request_deletion(identifier):
            result=original(identifier)
            with self.db(True) as store: LifecycleService(store.registry).request(CUSTOMER)
            return result
        self.sdk.client.v1.customers.retrieve=request_deletion
        self.assertEqual(self.api.dispatch(CUSTOMER,job)['state'],'unknown'); self.assertEqual(self.sdk.requests,[])

    def test_read_only_adapter_cannot_return_checkout_or_authentication_continuation(self):
        job=self.checkout(); self.api.dispatch(CUSTOMER,job); self.adapter.allow_writes=False
        self.assertFalse(self.api.get(CUSTOMER,job)['can_continue'])
        self.assertCode('stripe_sandbox_network_disabled',lambda:self.api.continuation(CUSTOMER,job))

    def test_read_only_adapter_cannot_return_3ds_secret(self):
        consent=self.setup_card(); self.sdk.payment_status='requires_action'
        job=self.api.queue(STAFF,'off_session',bill_id=self.bill,consent_id=consent,source_key='off')['job_id']
        self.api.dispatch(STAFF,job); self.adapter.allow_writes=False
        self.assertFalse(self.api.get(CUSTOMER,job)['can_continue'])
        self.assertCode('stripe_sandbox_network_disabled',lambda:self.api.continuation(CUSTOMER,job))

    def test_consent_revoked_during_continuation_read_prevents_secret_exposure(self):
        consent=self.setup_card(); self.sdk.payment_status='requires_action'
        job=self.api.queue(STAFF,'off_session',bill_id=self.bill,consent_id=consent,source_key='off')['job_id']
        self.api.dispatch(STAFF,job)
        original=self.sdk.client.v1.payment_intents.retrieve
        def revoke(identifier):
            result=original(identifier); self.api.revoke_consent(CUSTOMER,consent); return result
        self.sdk.client.v1.payment_intents.retrieve=revoke
        self.assertCode('sandbox_consent_revoked',lambda:self.api.continuation(CUSTOMER,job))
        self.adapter.allow_writes=False
        self.assertFalse(self.api.get(CUSTOMER,job)['can_continue'])

    def test_mode_customer_metadata_amount_and_url_fail_closed(self):
        job=self.checkout(); self.sdk.objects['cus_fixture1']['livemode']=True
        self.api.dispatch(CUSTOMER,job); self.assertEqual(len(self.sdk.requests),0)
        self.sdk.objects['cus_fixture1']['livemode']=False
        self.api.dispatch(STAFF,job,recover=True)
        self.sdk.objects['cs_test_1']['amount_total']=1
        self.assertEqual(self.api.reconcile(CUSTOMER,job)['state'],'unknown')
        self.sdk.objects['cs_test_1']['amount_total']=1100; self.sdk.objects['cs_test_1']['url']='https://attacker.invalid/'
        self.assertCode('stripe_checkout_url_invalid',lambda:self.api.continuation(CUSTOMER,job))

    def test_memory_path_cannot_double_open_same_bill(self):
        self.checkout()
        with self.db(True) as store:
            memory=PhysicalPaymentService(store,MemoryTestPSP())
            consent=memory.record_consent(CUSTOMER,1,terms_reference='fixture',payment_method_reference='pm_test_1',provider_customer_reference='cus_test_1',off_session=False,source_key='mc')['consent_id']
            self.assertCode('payment_attempt_exists_reconcile_first',lambda:memory.create_attempt(CUSTOMER,self.bill,consent,source_key='ma'))

    def test_unknown_terms_cannot_become_bill(self):
        with self.db(True) as store:
            approved_component(store.registry.connection,'unknown',customer=2,reasons=['bundle_scope_unconfirmed'])
            invoice=BillingService(store.finance).create_month(STAFF,2,'2026-10',source_key='unknown-i')
            self.assertCode('physical_invoice_requires_review',lambda:PhysicalPaymentService(store).prepare_invoice(STAFF,invoice['invoice_id'],review_reference='x',source_key='unknown-b'))

    def test_reconcile_unstarted_preserves_queued(self):
        job=self.checkout(); self.assertCode('sandbox_provider_reference_missing',lambda:self.api.reconcile(CUSTOMER,job))
        self.assertEqual(self.api.get(CUSTOMER,job)['state'],'queued')

    def test_invoice_changed_after_preparation_blocks_external_request(self):
        job=self.checkout()
        with self.db(True) as store: approved_component(store.registry.connection,'late',100)
        self.assertCode('physical_invoice_stale',lambda:self.api.dispatch(CUSTOMER,job))
        self.assertEqual(self.api.get(CUSTOMER,job)['state'],'queued'); self.assertEqual(self.sdk.requests,[])

    def test_schema_enforces_integer_amount_and_non_rewritable_consent(self):
        job=self.checkout(); row=self.row(job)
        with self.db(True) as store:
            for amount in (1.5,'not-a-yen',-1):
                copy={**row,'job_id':'p9_'+('a'*32),'source_key':'bad-amount','idempotency_key':'bad-key',
                    'kind':'refund','amount_yen':amount}
                columns=','.join(copy); placeholders=','.join('?' for _ in copy)
                with self.assertRaises(sqlite3.IntegrityError): store.execute(f'INSERT INTO stage9_payment_jobs({columns}) VALUES ({placeholders})',tuple(copy.values()))
        consent=self.setup_card(); self.api.revoke_consent(CUSTOMER,consent)
        with self.db(True) as store:
            with self.assertRaises(sqlite3.IntegrityError): store.execute('UPDATE stage9_payment_consents SET revoked_at=NULL WHERE consent_id=?',(consent,))
            with self.assertRaises(sqlite3.IntegrityError): store.execute("UPDATE stage9_payment_consents SET terms_reference='changed' WHERE consent_id=?",(consent,))

    def test_sdk_customer_marker_required_before_create(self):
        job=self.checkout(); self.sdk.objects['cus_fixture1']['metadata']['kaika_sandbox_fixture']='another-sandbox'
        self.assertEqual(self.api.dispatch(CUSTOMER,job)['state'],'unknown'); self.assertEqual(self.sdk.requests,[])

    def test_migration_cannot_repoint_existing_scope(self):
        conn=sqlite3.connect(self.path); conn.execute('PRAGMA foreign_keys=ON')
        try:
            with self.assertRaises(ValueError): migrate_test_connection(conn,sandbox_id='other-sandbox-fixture',customer_ids=(1,),allowed_test_path=self.path)
        finally: conn.close()

    def test_append_only_events_and_no_stage5_payment_side_effect(self):
        job=self.paid()
        with self.db(True) as store:
            self.assertEqual(store.query('SELECT COUNT(*) AS n FROM stage5_payment_attempts')[0]['n'],0)
            with self.assertRaises(sqlite3.IntegrityError): store.execute('DELETE FROM stage9_payment_events')
            with self.assertRaises(sqlite3.IntegrityError): store.execute('UPDATE stage9_payment_jobs SET amount_yen=1 WHERE job_id=?',(job,))
            with self.assertRaises(sqlite3.IntegrityError): store.execute('DELETE FROM stage9_payment_jobs WHERE job_id=?',(job,))

    def test_explicit_gate_keys_and_scope(self):
        for changes in ({'secret_key':'sk_live_disallowed123'},{'webhook_secret':None},{'publishable_key':'pk_live_disallowed123'}):
            args=dict(scope=self.scope,secret_key='sk_test_fixture123',webhook_secret='whsec_fixture123',sdk=self.sdk)
            args.update(changes)
            with self.assertRaises((ValueError,RegistryError)): StripeSandboxAdapter(**args)
        disabled=StripeSandboxAdapter(scope=self.scope,secret_key='sk_test_fixture123',webhook_secret='whsec_fixture123',sdk=self.sdk)
        api=SandboxPayments(self.db,self.scope,disabled); job=self.checkout()
        api.dispatch(CUSTOMER,job); self.assertEqual(self.sdk.requests,[]); self.assertEqual(self.sdk.reads,[])

    def test_official_sdk_signature_verifier(self):
        try: import stripe
        except ImportError: self.skipTest('Stripe SDK not installed')
        adapter=StripeSandboxAdapter(scope=self.scope,secret_key='sk_test_fixture123',webhook_secret='whsec_fixture123',sdk=stripe)
        job=self.checkout(); self.api.dispatch(CUSTOMER,job); raw=self.event(job)
        timestamp=int(time.time()); digest=hmac.new(b'whsec_fixture123',str(timestamp).encode()+b'.'+raw,hashlib.sha256).hexdigest()
        hint=adapter.webhook_hint(raw,f't={timestamp},v1={digest}')
        self.assertEqual(hint['job_id'],job)
        self.assertCode('stripe_webhook_signature_invalid',lambda:adapter.webhook_hint(raw+b' ',f't={timestamp},v1={digest}'))

    def test_official_sdk_serializes_create_read_and_refund_with_local_http_transport(self):
        try: import stripe
        except ImportError: self.skipTest('Stripe SDK not installed')
        current=[None]; test=self; requests=[]
        class LocalHTTP(stripe.HTTPClient):
            name='fixture'
            def request(inner,method,url,headers,post_data=None):
                test.assertFalse(getattr(test.local,'inside',False))
                requests.append((method,url,headers,post_data))
                path=urlsplit(url).path
                if method.lower()=='get': record=test.sdk.retrieve(path.rsplit('/',1)[1])
                else:
                    row=test.row(current[0]); payload=json.loads(row['payload_json'])
                    encoded=parse_qs(post_data)
                    test.assertIn('metadata[execution_mode]',encoded)
                    key=headers.get('Idempotency-Key') or headers.get('idempotency-key')
                    test.assertEqual(key,row['idempotency_key'])
                    kind={'/v1/checkout/sessions':'checkout.session','/v1/payment_intents':'payment_intent','/v1/refunds':'refund'}[path]
                    record=test.sdk.create(kind,payload,{'idempotency_key':key})
                return json.dumps(record),200,{'request-id':'req_fixture'}
        transport=LocalHTTP()
        sdk=SimpleNamespace(StripeClient=stripe.StripeClient,RequestsClient=lambda **kwargs:transport,Webhook=stripe.Webhook)
        adapter=StripeSandboxAdapter(scope=self.scope,secret_key='sk_test_fixture123',webhook_secret='whsec_fixture123',sdk=sdk,allow_network=True,allow_writes=True)
        api=SandboxPayments(self.db,self.scope,adapter)
        consent=api.queue(CUSTOMER,'setup',terms_reference='fixture-test-consent',source_key='sdk-setup')['job_id']; current[0]=consent
        self.assertEqual(api.dispatch(CUSTOMER,consent)['state'],'open')
        self.sdk.finish(self.row(consent)['provider_id']); self.assertEqual(api.reconcile(CUSTOMER,consent)['state'],'succeeded')
        job=api.queue(STAFF,'off_session',bill_id=self.bill,consent_id=consent,source_key='sdk-pay')['job_id']; current[0]=job
        self.assertEqual(api.dispatch(STAFF,job)['state'],'succeeded')
        refund=api.queue(STAFF,'refund',parent_job_id=job,amount_yen=100,reason='SDK fixture',source_key='sdk-refund')['job_id']; current[0]=refund
        self.assertEqual(api.dispatch(STAFF,refund)['state'],'succeeded')
        self.assertEqual(sum(method.lower()=='post' for method,_,_,_ in requests),3)

    def test_actual_lifecycle_request_suppresses_and_withdraw_does_not_replay(self):
        from account_lifecycle import LifecycleService,migration_statements
        job=self.checkout()
        with self.db(True) as store:
            for sql in migration_statements(): store.execute(sql)
            request=LifecycleService(store.registry).request(CUSTOMER)
        self.assertEqual(self.api.get(CUSTOMER,job)['state'],'suppressed')
        with self.db(True) as store: LifecycleService(store.registry).withdraw(CUSTOMER,request['request_id'])
        self.assertCode('sandbox_customer_not_allowed',lambda:self.api.dispatch(CUSTOMER,job))
        self.assertEqual(len(self.sdk.requests),0)

    def web(self,user=1):
        from flask import Flask
        from flask_login import LoginManager,UserMixin
        from stage9_payment_web import register_stage9_payments
        app=Flask(__name__,template_folder=str(Path(__file__).resolve().parents[1]/'templates'))
        app.secret_key='fixture-stage9-web-secret'
        for flag in ('STAGE2_INVENTORY_ENABLED','STAGE3_FINANCE_ENABLED','STAGE4_COMMERCE_ENABLED','STAGE5_PAYMENTS_ENABLED','STAGE9_PAYMENTS_ENABLED'): app.config[flag]=True
        app.extensions['stage9_payment_scope']=self.scope; app.extensions['stage9_payment_adapter']=self.adapter
        login=LoginManager(app)
        class User(UserMixin):
            def __init__(self,identifier): self.id=int(identifier); self.role='owner' if self.id==9 else 'user'
            def is_admin(self): return self.id==9
            def has_permission(self,name): return self.id==9
        @login.user_loader
        def load(identifier): return User(identifier)
        runtime=SimpleNamespace(app=app,DATABASE=str(self.path),DATABASE_URL=None,get_db=lambda:sqlite3.connect(self.path))
        register_stage9_payments(runtime)
        client=app.test_client()
        with client.session_transaction() as session: session['_user_id']=str(user); session['_fresh']=True
        return app,client

    def form(self,client,path,action,extra=None):
        response=client.get(path); self.assertEqual(response.status_code,200)
        match=re.search(r'<form[^>]*action="'+re.escape(action)+r'"[^>]*>(.*?)</form>',response.get_data(as_text=True),re.S)
        self.assertIsNotNone(match,action)
        fields=dict(re.findall(r'name="([^"]+)" value="([^"]*)"',match[1])); fields.update(extra or {})
        return fields

    def test_http_customer_checkout_has_csrf_and_private_redirect(self):
        app,client=self.web(); index='/payments/sandbox'; queue=index+'/bills/'+self.bill
        self.assertEqual(client.post(queue).status_code,400)
        response=client.post(queue,data=self.form(client,index,queue)); self.assertEqual(response.status_code,302)
        path=response.headers['Location']; job=path.rsplit('/',1)[1]
        self.assertEqual(client.get(path).status_code,200); self.assertEqual(len(self.sdk.requests),0)
        dispatch=path+'/dispatch'; client.post(dispatch,data=self.form(client,path,dispatch))
        continuation=path+'/continue'; response=client.post(continuation,data=self.form(client,path,continuation))
        self.assertEqual(response.status_code,303); self.assertTrue(response.headers['Location'].startswith('https://checkout.stripe.com/'))
        self.assertIn('no-store',response.headers['Cache-Control'])
        self.assertEqual(self.api.get(CUSTOMER,job)['state'],'open')
        self.assertEqual(client.get(path+'?success=true').status_code,200)
        self.assertEqual(self.api.get(CUSTOMER,job)['state'],'open')

    def test_http_cross_customer_scope_and_disabled_registration(self):
        job=self.checkout(); app,client=self.web(2)
        self.assertEqual(client.get('/payments/sandbox/'+job).status_code,403)
        app.config['STAGE9_PAYMENTS_ENABLED']=False
        self.assertEqual(client.get('/payments/sandbox').status_code,404)
        self.assertEqual(client.post('/webhooks/stripe/physical-sandbox').status_code,404)
        self.assertEqual(self.sdk.requests,[])

    def test_http_3ds_is_customer_only_no_cache_and_no_secret_in_job(self):
        consent=self.setup_card(); self.sdk.payment_status='requires_action'
        job=self.api.queue(STAFF,'off_session',bill_id=self.bill,consent_id=consent,source_key='off')['job_id']
        self.api.dispatch(STAFF,job); app,client=self.web(); path='/payments/sandbox/'+job; action=path+'/continue'
        page=client.get(path).get_data(as_text=True); self.assertNotIn('_secret_',page)
        response=client.post(action,data=self.form(client,path,action))
        self.assertEqual(response.status_code,200); self.assertIn('confirmCardPayment',response.get_data(as_text=True))
        self.assertIn('no-store',response.headers['Cache-Control'])
        with client.session_transaction() as session: session['_user_id']='2'
        self.assertEqual(client.post(action,data={}).status_code,400)
        self.assertEqual(client.get(path).status_code,403)

    def test_http_signed_webhook_queues_without_network(self):
        job=self.checkout(); self.api.dispatch(CUSTOMER,job); app,client=self.web()
        calls=len(self.sdk.reads); body=self.event(job)
        response=client.post('/webhooks/stripe/physical-sandbox',data=body,headers={'Stripe-Signature':'fixture-valid'},content_type='application/json')
        self.assertEqual(response.status_code,200); self.assertTrue(response.json['queued'])
        self.assertEqual(len(self.sdk.reads),calls); self.assertEqual(self.api.get(CUSTOMER,job)['state'],'open')

    def test_http_lifecycle_automatically_blocks_write_outside_db(self):
        job=self.checkout(); app,client=self.web()
        class Lifecycle:
            def outbound_allowed(inner,customer):
                self.assertFalse(getattr(self.local,'inside',False)); return False
            def access_allowed(inner,customer): return True
        app.extensions['account_lifecycle']=Lifecycle()
        path='/payments/sandbox/'+job; action=path+'/dispatch'
        response=client.post(action,data=self.form(client,path,action))
        self.assertEqual(response.status_code,302); self.assertEqual(self.sdk.requests,[])
        app.extensions['account_lifecycle'].access_allowed=lambda customer:False
        self.assertEqual(client.get(path).status_code,403)

    def test_cli_default_inspection_no_network_and_write_requires_switch(self):
        job=self.checkout(); app,_=self.web(); runner=app.test_cli_runner()
        args=['stage9-payments','--staff-user-id','9','--job-id',job]
        self.assertEqual(runner.invoke(args=args).exit_code,0); self.assertEqual(self.sdk.requests,[])
        self.assertNotEqual(runner.invoke(args=args+['--action','dispatch']).exit_code,0)
        self.assertEqual(runner.invoke(args=args+['--action','dispatch','--send-approved-test']).exit_code,0)
        self.assertEqual(len(self.sdk.requests),1)


if __name__=='__main__': unittest.main()
