"""Durable Stripe sandbox coordinator. Every network call is outside DB scopes.

Stage5 bills remain fixture-only, immutable sources. This ledger never writes
real invoices, payment balances, entitlements, or approved contract conditions.
"""
import hashlib
import json
import time
import uuid

from item_registry import RegistryError, _json, _text
from stage3_common import yen
from stage5_payments import PhysicalPaymentService
from stage5_stripe_sandbox import (PaymentBinding, ConsentBinding, checkout_plan,
    setup_checkout_plan, off_session_plan, refund_plan, VerifiedPayment)
from stage9_payment_schema import VERSION
from stage9_stripe import SandboxOutcomeUnknown, StripeSandboxAdapter, SandboxScope


def _hash(value): return hashlib.sha256(_json(value).encode()).hexdigest()
def _id(): return 'p9_'+uuid.uuid4().hex


def _exists(store,table):
    if store.dialect=='postgres':
        rows=store.query('SELECT to_regclass(?) AS name',(table,))
    else: rows=store.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,))
    return bool(rows and rows[0]['name'])


def suppress_customer_payment_jobs(store,customer_id,reason='account_lifecycle'):
    """Call within the lifecycle transaction with Stage5/6/7 Store or Registry.

No commits or provider calls. In-flight outcomes remain unknown; historical
success and audit records stay intact. Safe no-op before explicit migration.
"""
    if not _exists(store,'stage9_payment_jobs'): return {'suppressed':0,'unknown':0}
    jobs=store.query("SELECT * FROM stage9_payment_jobs WHERE customer_id=? AND state NOT IN ('succeeded','canceled','suppressed')",(customer_id,))
    counts={'suppressed':0,'unknown':0}
    store.execute('UPDATE stage9_payment_customers SET enabled=0 WHERE customer_id=?',(customer_id,))
    for job in jobs:
        state='unknown' if job['first_sent_at'] is not None else 'suppressed'
        store.execute('UPDATE stage9_payment_jobs SET state=?,lease_token=NULL,lease_until=NULL WHERE job_id=?',(state,job['job_id']))
        store.execute('INSERT INTO stage9_payment_events VALUES (?,?,?,?,?,?)',
            ('e9_'+uuid.uuid4().hex,job['job_id'],'account_suppressed',state,_json({'reason':'account_lifecycle'}),int(time.time())))
        counts[state]+=1
    return counts


class SandboxPayments:
    def __init__(self,db,scope,adapter=None,*,clock=None,outbound_allowed=None):
        if type(scope) is not SandboxScope: raise ValueError('Explicit sandbox scope required')
        if adapter is not None and type(adapter) is not StripeSandboxAdapter:
            raise ValueError('Explicit Stripe sandbox adapter required')
        if adapter and adapter.scope!=scope: raise ValueError('Sandbox scope mismatch')
        self.db,self.scope,self.adapter=db,scope,adapter
        self.clock=clock or (lambda:int(time.time()))
        self.outbound_allowed=outbound_allowed or (lambda customer:True)

    def _ready(self,store,customer=None,write=False):
        store.require_ready()
        if not _exists(store,'stage9_payment_scope'):
            raise RegistryError('sandbox_migration_required',status=409)
        scopes=store.query('SELECT * FROM stage9_payment_scope')
        if len(scopes)!=1 or scopes[0]!=dict(sandbox_id=self.scope.sandbox_id,schema_version=VERSION,environment='test'):
            raise RegistryError('sandbox_database_scope_mismatch',status=409)
        if customer is not None:
            self.scope.provider_customer(customer)
            rows=store.query('SELECT * FROM stage9_payment_customers WHERE customer_id=?',(customer,))
            if not rows or rows[0]['sandbox_id']!=self.scope.sandbox_id or write and rows[0]['enabled']!=1:
                raise RegistryError('sandbox_customer_not_allowed',status=403)

    @staticmethod
    def _lock(store):
        store.execute("INSERT INTO stage4_locks(lock_key) VALUES ('workflow') ON CONFLICT(lock_key) DO NOTHING")
        store.execute("UPDATE stage4_locks SET lock_version=lock_version WHERE lock_key='workflow'")

    def _row(self,store,actor,job_id):
        self._ready(store)
        rows=store.query('SELECT * FROM stage9_payment_jobs WHERE job_id=?',(job_id,))
        if not rows: raise RegistryError('sandbox_job_not_found',status=404)
        job=rows[0]; store.authorize(actor,job['customer_id'],finance=True)
        self._ready(store,job['customer_id'])
        return job

    def _event(self,store,job,kind,state,detail=None):
        store.execute('INSERT INTO stage9_payment_events VALUES (?,?,?,?,?,?)',
            ('e9_'+uuid.uuid4().hex,job['job_id'],kind,state,_json(detail or {}),self.clock()))

    def _projection(self,store,job):
        fields=('job_id','kind','bill_id','parent_job_id','consent_id','amount_yen','created_at','state','needs_reconcile')
        result={key:job[key] for key in fields}
        result.update(currency='JPY',environment='test',real_payment_enabled=False,
            can_continue=bool(job['state'] in ('open','requires_action') and self.adapter and self.adapter.allow_network and self.adapter.allow_writes),
            events=store.query('SELECT kind,state,detail_json,recorded_at FROM stage9_payment_events WHERE job_id=? ORDER BY recorded_at,event_id',(job['job_id'],)))
        return result

    def get(self,actor,job_id):
        with self.db() as store: return self._projection(store,self._row(store,actor,job_id))

    def overview(self,actor):
        with self.db() as store:
            self._ready(store); staff=store.authorize(actor,finance=True)
            jobs=store.query('SELECT * FROM stage9_payment_jobs'+('' if staff else ' WHERE customer_id=?')+' ORDER BY created_at DESC',() if staff else (actor.user_id,))
            customers=set(dict(self.scope.customer_map))
            bills=store.query('SELECT bill_id,customer_id,period,amount_yen FROM stage5_physical_bills'+('' if staff else ' WHERE customer_id=?'),() if staff else (actor.user_id,))
            consents=store.query('SELECT consent_id,customer_id,terms_reference,created_at,revoked_at,payment_method_id FROM stage9_payment_consents'+('' if staff else ' WHERE customer_id=?'),() if staff else (actor.user_id,))
            for consent in consents: consent['verified']=bool(consent.pop('payment_method_id'))
            return {'jobs':[self._projection(store,job) for job in jobs],
                'bills':[bill for bill in bills if bill['customer_id'] in customers],
                'consents':consents,'connected':bool(self.adapter and self.adapter.allow_network and self.adapter.allow_writes),
                'environment':'test','real_payment_enabled':False}

    def _fresh(self,store,actor,job):
        self._ready(store,job['customer_id'],write=True)
        if job['kind'] in ('checkout','off_session'):
            bill=PhysicalPaymentService(store)._row(actor,'stage5_physical_bills',job['bill_id'],'bill_id')
            PhysicalPaymentService(store)._fresh_bill(actor,bill)
            if bill['amount_yen']!=job['amount_yen']: raise RegistryError('sandbox_bill_mismatch',status=409)
        if job['consent_id']:
            consent=store.query('SELECT * FROM stage9_payment_consents WHERE consent_id=?',(job['consent_id'],))
            if not consent or consent[0]['customer_id']!=job['customer_id'] or consent[0]['revoked_at'] is not None:
                raise RegistryError('sandbox_consent_revoked',status=409)

    def _enrich(self,store,job):
        job=dict(job)
        if job['consent_id']:
            consent=store.query('SELECT * FROM stage9_payment_consents WHERE consent_id=?',(job['consent_id'],))[0]
            job['terms_reference']=consent['terms_reference']
        if job['parent_job_id']:
            job['parent']=store.query('SELECT * FROM stage9_payment_jobs WHERE job_id=?',(job['parent_job_id'],))[0]
        return job

    def queue(self,actor,kind,*,bill_id=None,consent_id=None,parent_job_id=None,amount_yen=None,
              terms_reference=None,reason=None,source_key):
        if kind not in ('checkout','setup','off_session','refund'): raise RegistryError('sandbox_kind_invalid')
        source_key=_text(source_key,'source_key',500)
        payload=dict(kind=kind,bill_id=bill_id,consent_id=consent_id,parent_job_id=parent_job_id,
            amount_yen=amount_yen,terms_reference=terms_reference,reason=reason)
        with self.db(True) as store:
            self._ready(store); self._lock(store)
            staff=store.authorize(actor,finance=True)
            previous=store.query('SELECT * FROM stage9_payment_jobs WHERE source_key=?',(source_key,))
            if previous:
                if previous[0]['actor_id']!=actor.user_id or previous[0]['source_hash']!=_hash(payload): raise RegistryError('source_key_conflict',status=409)
                return self._projection(store,self._row(store,actor,previous[0]['job_id']))
            identifier=_id(); parent=None; customer=actor.user_id; amount=0
            if kind in ('checkout','off_session'):
                bill=PhysicalPaymentService(store)._row(actor,'stage5_physical_bills',bill_id,'bill_id')
                customer,amount=bill['customer_id'],bill['amount_yen']
                PhysicalPaymentService(store)._fresh_bill(actor,bill)
                if kind=='checkout' and (staff or customer!=actor.user_id): raise RegistryError('customer_payment_action_required',status=403)
                if kind=='off_session' and not staff: raise RegistryError('finance_permission_required',status=403)
                old=store.query("SELECT * FROM stage9_payment_jobs WHERE bill_id=? AND kind IN ('checkout','off_session')",(bill_id,))
                if old: raise RegistryError('sandbox_payment_exists_reconcile_first',status=409)
                old=store.query('SELECT attempt_id FROM stage5_payment_attempts WHERE bill_id=?',(bill_id,))
                if old: raise RegistryError('sandbox_memory_attempt_exists',status=409)
            elif kind=='refund':
                if not staff: raise RegistryError('finance_permission_required',status=403)
                parent=self._row(store,actor,parent_job_id)
                if parent['kind'] not in ('checkout','off_session') or parent['state']!='succeeded' or not parent['payment_id']:
                    raise RegistryError('refund_requires_verified_payment',status=409)
                amount=yen(amount_yen)
                if amount<=0: raise RegistryError('positive_refund_required')
                reason=_text(reason,'reason',1000)
                customer=parent['customer_id']; bill_id=parent['bill_id']
                refunds=store.query("SELECT amount_yen,state FROM stage9_payment_jobs WHERE parent_job_id=? AND kind='refund'",(parent_job_id,))
                if sum(row['amount_yen'] for row in refunds if row['state'] not in ('failed','canceled','suppressed'))+amount>parent['amount_yen']:
                    raise RegistryError('refund_exceeds_original_payment',status=409)
            elif kind=='setup':
                if staff: raise RegistryError('customer_payment_action_required',status=403)
                terms_reference=_text(terms_reference,'terms_reference',1000)
                consent_id=identifier
                store.execute("INSERT INTO stage9_payment_consents(consent_id,customer_id,terms_reference,actor_id,created_at,setup_job_id) VALUES (?,?,?,?,?,?)",
                    (identifier,customer,terms_reference,actor.user_id,self.clock(),identifier))
            self._ready(store,customer,write=True)
            provider=self.scope.provider_customer(customer)
            binding=PaymentBinding(bill_id,identifier,customer,provider,amount) if amount else None
            return_url=self.scope.origin+'/payments/sandbox/'+identifier
            if kind=='checkout':
                plan=checkout_plan(binding,success_url=return_url,cancel_url=return_url,allowed_origin=self.scope.origin)
            elif kind=='setup':
                consent=ConsentBinding(customer,provider,consent_id,terms_reference,True)
                plan=setup_checkout_plan(consent,success_url=return_url,cancel_url=return_url,allowed_origin=self.scope.origin)
            elif kind=='off_session':
                rows=store.query('SELECT * FROM stage9_payment_consents WHERE consent_id=?',(consent_id,))
                if not rows or rows[0]['customer_id']!=customer or rows[0]['revoked_at'] is not None or not rows[0]['payment_method_id']:
                    raise RegistryError('sandbox_verified_consent_required',status=409)
                consent=ConsentBinding(customer,provider,consent_id,rows[0]['terms_reference'],True)
                plan=off_session_plan(binding,consent,payment_method_id=rows[0]['payment_method_id'])
            else:
                evidence=VerifiedPayment(parent['payment_id'],StripeSandboxAdapter.binding(parent),'succeeded',parent['amount_yen'],True)
                plan=refund_plan(evidence,refund_id=identifier,amount_yen=amount)
            serialized=_json(plan.params)
            store.execute('''INSERT INTO stage9_payment_jobs(job_id,sandbox_id,customer_id,kind,bill_id,parent_job_id,consent_id,amount_yen,
                provider_customer_id,payload_json,payload_hash,idempotency_key,source_key,source_hash,actor_id,created_at,state)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identifier,self.scope.sandbox_id,customer,kind,bill_id,parent_job_id,consent_id,amount,provider,serialized,
                 hashlib.sha256(serialized.encode()).hexdigest(),plan.options['idempotency_key'],source_key,_hash(payload),actor.user_id,self.clock(),'queued'))
            job=self._row(store,actor,identifier)
            self._event(store,job,'queued','queued',{'scope':'stripe_sandbox',**({'review_reason':reason} if reason else {})})
            return self._projection(store,job)

    def revoke_consent(self,actor,consent_id):
        with self.db(True) as store:
            self._ready(store); self._lock(store)
            rows=store.query('SELECT * FROM stage9_payment_consents WHERE consent_id=?',(consent_id,))
            if not rows: raise RegistryError('sandbox_consent_not_found',status=404)
            row=rows[0]; staff=store.authorize(actor,row['customer_id'],finance=True)
            if staff or actor.user_id!=row['customer_id']: raise RegistryError('customer_payment_action_required',status=403)
            store.execute('UPDATE stage9_payment_consents SET revoked_at=COALESCE(revoked_at,?) WHERE consent_id=?',(self.clock(),consent_id))
            for job in store.query("SELECT * FROM stage9_payment_jobs WHERE consent_id=? AND state NOT IN ('succeeded','canceled','suppressed')",(consent_id,)):
                state='unknown' if job['first_sent_at'] is not None else 'suppressed'
                store.execute('UPDATE stage9_payment_jobs SET state=?,lease_token=NULL,lease_until=NULL WHERE job_id=?',(state,job['job_id']))
                self._event(store,job,'consent_revoked',state)

    def _claim(self,actor,job_id,*,write,recover=False):
        with self.db(True) as store:
            self._ready(store); self._lock(store); job=self._row(store,actor,job_id)
            if job['lease_token'] and job['lease_until']>self.clock(): raise RegistryError('sandbox_job_busy',status=409)
            if write:
                self._fresh(store,actor,job)
                if job['state'] in ('sending','unknown') and recover:
                    if not actor.staff: raise RegistryError('finance_permission_required',status=403)
                    if job['provider_id'] or job['payment_id'] or job['hint_id']: raise RegistryError('sandbox_retrieve_existing_reference',status=409)
                    if job['first_sent_at'] is None or self.clock()-job['first_sent_at']>=23*3600:
                        raise RegistryError('sandbox_idempotency_window_expired',status=409)
                elif job['state']!='queued': raise RegistryError('sandbox_reconciliation_required',status=409)
            elif not (job['provider_id'] or job['payment_id'] or job['hint_id']):
                raise RegistryError('sandbox_provider_reference_missing',status=409)
            token=uuid.uuid4().hex
            store.execute('UPDATE stage9_payment_jobs SET lease_token=?,lease_until=?,state=?,first_sent_at=COALESCE(first_sent_at,?) WHERE job_id=?',
                (token,self.clock()+120,'sending' if write else job['state'],self.clock() if write else None,job_id))
            self._event(store,job,'dispatch_claim' if write else 'reconcile_claim','sending' if write else job['state'])
            return self._enrich(store,job),token

    def _finish(self,actor,job,token,result=None,unknown=False,hint=None):
        with self.db(True) as store:
            self._lock(store); current=self._row(store,actor,job['job_id'])
            if current['lease_token']!=token:
                self._event(store,current,'late_result_ignored',current['state'])
                return self._projection(store,current)
            state='unknown' if unknown else result['state']
            if current['state'] in ('succeeded','canceled') and state!=current['state']:
                store.execute('UPDATE stage9_payment_jobs SET lease_token=NULL,lease_until=NULL,needs_reconcile=1 WHERE job_id=?',(job['job_id'],))
                self._event(store,current,'terminal_conflict',current['state'])
                return self._projection(store,current)
            if result:
                for field in ('provider_id','payment_id'):
                    if current[field] and result.get(field) and current[field]!=result[field]: raise RegistryError('sandbox_provider_identity_changed',status=409)
            store.execute('''UPDATE stage9_payment_jobs SET state=?,lease_token=NULL,lease_until=NULL,
                provider_id=COALESCE(?,provider_id),payment_id=COALESCE(?,payment_id),payment_method_id=COALESCE(?,payment_method_id),
                hint_id=COALESCE(?,hint_id),hint_kind=CASE WHEN ? IS NOT NULL THEN 'payment_intent' ELSE hint_kind END,
                needs_reconcile=? WHERE job_id=?''',
                (state,(result or {}).get('provider_id'),(result or {}).get('payment_id'),(result or {}).get('payment_method_id'),hint,hint,
                 int(unknown or current['hint_version']!=job['hint_version']),job['job_id']))
            if result and job['kind']=='setup' and state=='succeeded':
                store.execute('UPDATE stage9_payment_consents SET payment_method_id=? WHERE consent_id=? AND revoked_at IS NULL',
                    (result['payment_method_id'],job['consent_id']))
            self._event(store,current,'outcome_unknown' if unknown else 'provider_verified',state)
            return self._projection(store,self._row(store,actor,job['job_id']))

    def dispatch(self,actor,job_id,*,recover=False):
        if self.adapter is None: raise RegistryError('stripe_sandbox_not_connected',status=409)
        job,token=self._claim(actor,job_id,write=True,recover=recover)
        # The DB scope has closed and committed. Lifecycle uses a separate read
        # connection and is rechecked immediately before external writes.
        if self.outbound_allowed(job['customer_id']) is not True:
            return self._finish(actor,job,token,unknown=True)
        try:
            def before_write():
                with self.db(True) as store:
                    self._lock(store); current=self._row(store,actor,job_id)
                    if current['lease_token']!=token or current['lease_until']<=self.clock() or current['state']!='sending':
                        raise RegistryError('sandbox_dispatch_lease_lost',status=409)
                    self._fresh(store,actor,current)
                if self.outbound_allowed(job['customer_id']) is not True:
                    raise RegistryError('sandbox_account_paused',status=409)
            identifier=self.adapter.create(job,before_write=before_write)
            # Persist the provider reference before the follow-up read. A crash
            # thereafter can reconcile without creating another provider object.
            with self.db(True) as store:
                self._lock(store); current=self._row(store,actor,job_id)
                if current['lease_token']!=token: return self._projection(store,current)
                store.execute('UPDATE stage9_payment_jobs SET provider_id=? WHERE job_id=?',(identifier,job_id))
            job['provider_id']=identifier
            result=self.adapter.retrieve(job)
            return self._finish(actor,job,token,result)
        except SandboxOutcomeUnknown as error:
            return self._finish(actor,job,token,unknown=True,hint=error.provider_hint)
        except Exception:
            return self._finish(actor,job,token,unknown=True)

    def reconcile(self,actor,job_id):
        if self.adapter is None: raise RegistryError('stripe_sandbox_not_connected',status=409)
        job,token=self._claim(actor,job_id,write=False)
        try: return self._finish(actor,job,token,self.adapter.retrieve(job))
        except Exception: return self._finish(actor,job,token,unknown=True)

    def continuation(self,actor,job_id):
        if self.adapter is None: raise RegistryError('stripe_sandbox_not_connected',status=409)
        with self.db() as store:
            job=self._row(store,actor,job_id)
            if actor.staff or actor.user_id!=job['customer_id']: raise RegistryError('customer_payment_action_required',status=403)
            self._fresh(store,actor,job); job=self._enrich(store,job)
        if self.outbound_allowed(job['customer_id']) is not True: raise RegistryError('sandbox_account_paused',status=409)
        def before_continue():
            with self.db() as store:
                current=self._row(store,actor,job_id); self._fresh(store,actor,current)
                if current['state'] in ('succeeded','canceled','suppressed'):
                    raise RegistryError('sandbox_continuation_unavailable',status=409)
            if self.outbound_allowed(job['customer_id']) is not True: raise RegistryError('sandbox_account_paused',status=409)
        return self.adapter.continuation(job,before_continue=before_continue)

    def accept_webhook(self,raw,signature):
        if self.adapter is None: raise RegistryError('stripe_sandbox_not_connected',status=409)
        hint=self.adapter.webhook_hint(raw,signature)
        if not hint: return {'received':True,'ignored':True}
        with self.db(True) as store:
            self._ready(store); self._lock(store)
            old=store.query('SELECT body_hash FROM stage9_payment_webhooks WHERE event_id=?',(hint['event_id'],))
            if old:
                if old[0]['body_hash']!=hint['body_hash']: raise RegistryError('stripe_event_content_changed',status=409)
                return {'received':True,'duplicate':True}
            jobs=store.query('SELECT * FROM stage9_payment_jobs WHERE job_id=?',(hint['job_id'],))
            job=jobs[0] if jobs else None
            if job:
                self._ready(store,job['customer_id'])
                allowed={'checkout':('checkout.session','payment_intent'),'off_session':('payment_intent',),
                    'setup':('checkout.session',),'refund':('refund',)}[job['kind']]
                if hint['object_kind'] not in allowed: raise RegistryError('stripe_webhook_kind_mismatch',status=409)
                store.execute('UPDATE stage9_payment_jobs SET hint_id=?,hint_kind=?,hint_version=hint_version+1,needs_reconcile=1 WHERE job_id=?',
                    (hint['object_id'],hint['object_kind'],job['job_id']))
            store.execute('INSERT INTO stage9_payment_webhooks VALUES (?,?,?,?,?,?)',
                (hint['event_id'],hint['body_hash'],job['job_id'] if job else None,hint['object_id'],hint['object_kind'],self.clock()))
            return {'received':True,'queued':bool(job)}
