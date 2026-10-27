"""Explicit Stripe test-only connector. No environment discovery or startup I/O.

The coordinator must commit a durable request/lease before calling create().
No real card details, raw webhook bodies, hosted URLs or client secrets are logged.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from urllib.parse import urlsplit

from item_registry import RegistryError
from stage5_stripe_sandbox import (StripeSandboxCandidate, PaymentBinding, ConsentBinding,
    _sdk_dict, _identifier)


@dataclass(frozen=True)
class SandboxScope:
    sandbox_id: str
    customer_map: tuple
    origin: str

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',self.sandbox_id): raise ValueError('Invalid sandbox identifier')
        parsed=urlsplit(self.origin)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError('Explicit HTTPS sandbox origin required')
        if not self.customer_map or len(dict(self.customer_map))!=len(self.customer_map): raise ValueError('Fixture customer allowlist required')
        for user,provider in self.customer_map:
            if type(user) is not int or user<1: raise ValueError('Invalid fixture customer')
            _identifier(provider,'cus_')
        if len({value for _,value in self.customer_map})!=len(self.customer_map): raise ValueError('Provider customer must be unique')

    def provider_customer(self,customer):
        value=dict(self.customer_map).get(customer)
        if not value: raise RegistryError('sandbox_customer_not_allowed',status=403)
        return value


class SandboxOutcomeUnknown(Exception):
    def __init__(self,provider_hint=None):
        super().__init__('Sandbox result requires reconciliation')
        self.provider_hint=provider_hint


class StripeSandboxAdapter:
    mode='test'
    provider='stripe_sandbox'

    def __init__(self, *, scope, secret_key, webhook_secret, sdk, allow_network=False,
                 allow_writes=False, publishable_key=None):
        if type(scope) is not SandboxScope or type(allow_network) is not bool or type(allow_writes) is not bool:
            raise ValueError('Explicit sandbox configuration required')
        if allow_writes and not allow_network: raise ValueError('Writes require explicit network permission')
        if not isinstance(webhook_secret,str) or not re.fullmatch(r'whsec_[A-Za-z0-9]{8,}',webhook_secret):
            raise ValueError('Explicit sandbox webhook secret required')
        if publishable_key is not None and not re.fullmatch(r'pk_test_[A-Za-z0-9]{8,}',publishable_key):
            raise ValueError('Test publishable key required')
        self.scope,self.allow_network,self.allow_writes=scope,allow_network,allow_writes
        self._publishable_key=publishable_key
        http_client=sdk.RequestsClient(timeout=(5,15)) if hasattr(sdk,'RequestsClient') else None
        self._candidate=StripeSandboxCandidate(secret_key=secret_key,webhook_secret=webhook_secret,sdk=sdk,
            read_enabled=allow_network,http_client=http_client)
        self._client=self._candidate._client; self._sdk=sdk; self._webhook_secret=webhook_secret

    def _gate(self,job,write=False):
        if not self.allow_network or write and not self.allow_writes:
            raise RegistryError('stripe_sandbox_network_disabled',status=409)
        if job['sandbox_id']!=self.scope.sandbox_id or job['environment']!='test' or self.scope.provider_customer(job['customer_id'])!=job['provider_customer_id']:
            raise RegistryError('sandbox_scope_mismatch',status=409)

    def _read(self,service,identifier):
        return self._candidate._retrieve(service,identifier)

    def verify_customer(self,job):
        self._gate(job)
        record=self._read(self._client.v1.customers,job['provider_customer_id'])
        self._candidate._base(record,job['provider_customer_id'],'customer')
        if record.get('deleted') or record.get('metadata',{}).get('kaika_sandbox_fixture')!=self.scope.sandbox_id or \
                record.get('metadata',{}).get('kaika_fixture_customer_id')!=str(job['customer_id']):
            raise RegistryError('stripe_fixture_customer_unverified',status=409)

    @staticmethod
    def binding(job):
        return PaymentBinding(job['bill_id'],job['job_id'],job['customer_id'],job['provider_customer_id'],job['amount_yen'])

    @staticmethod
    def consent(job):
        return ConsentBinding(job['customer_id'],job['provider_customer_id'],job['consent_id'],job['terms_reference'],True)

    def create(self,job,*,before_write):
        if not callable(before_write): raise ValueError('Durable pre-write revalidation required')
        self._gate(job,write=True); self.verify_customer(job)
        payload=json.loads(job['payload_json'])
        if hashlib.sha256(job['payload_json'].encode()).hexdigest()!=job['payload_hash']:
            raise RegistryError('sandbox_payload_changed',status=409)
        services={'checkout':self._client.v1.checkout.sessions,'setup':self._client.v1.checkout.sessions,
            'off_session':self._client.v1.payment_intents,'refund':self._client.v1.refunds}
        if job['kind']=='off_session':
            method=self._read(self._client.v1.payment_methods,payload['payment_method'])
            self._candidate._base(method,payload['payment_method'],'payment_method')
            if method.get('customer')!=job['provider_customer_id'] or method.get('type')!='card':
                raise RegistryError('stripe_saved_card_customer_mismatch',status=409)
        # Customer/card reads can take time. A revocation or deletion request
        # committed during them must stop the pending write. The callback opens
        # and closes its own short DB scope; no network is held inside that scope.
        before_write()
        self._gate(job,write=True)
        try:
            record=_sdk_dict(services[job['kind']].create(params=payload,options={'idempotency_key':job['idempotency_key']}))
            prefix={'checkout':'cs_test_','setup':'cs_test_','off_session':'pi_','refund':'re_'}[job['kind']]
            identifier=_identifier(record.get('id'),prefix)
            if record.get('livemode') not in (False,None) or job['kind']!='refund' and record.get('livemode') is not False:
                raise ValueError('Environment mismatch')
            return identifier
        except Exception as error:
            # A card error may contain the already-created PI requiring 3DS.
            # Keep only an identifier hint; do not trust error status or amount.
            body=getattr(error,'json_body',None)
            detail=body.get('error') if isinstance(body,dict) else None
            obj=detail.get('payment_intent') if isinstance(detail,dict) else None
            hint=obj.get('id') if isinstance(obj,dict) else None
            if job['kind']!='off_session' or not isinstance(hint,str) or not re.fullmatch(r'pi_[A-Za-z0-9_]{1,180}',hint): hint=None
            raise SandboxOutcomeUnknown(hint) from None

    def retrieve(self,job):
        self._gate(job); self.verify_customer(job)
        identifier=job.get('provider_id')
        hint=job.get('hint_id'); hint_kind=job.get('hint_kind')
        if not identifier and hint_kind in ('checkout.session','payment_intent','refund'):
            identifier=hint
        if not identifier: raise RegistryError('sandbox_provider_reference_missing',status=409)
        base={'state':'unknown','provider_id':job.get('provider_id'),'payment_id':None,'payment_method_id':None}
        kind=job['kind']
        if kind in ('checkout','off_session') and identifier.startswith('pi_'):
            evidence=self._candidate.retrieve_payment(identifier,self.binding(job))
            return {**base,'state':evidence.status,'provider_id':identifier if kind=='off_session' else base['provider_id'],
                'payment_id':identifier,'verified':True}
        if kind=='checkout':
            evidence=self._candidate.retrieve_checkout(identifier,self.binding(job))
            return {**base,'state':'succeeded' if evidence['payment_verified'] else
                'canceled' if evidence['status']=='expired' and not evidence['payment_id'] else
                'open' if evidence['status']=='open' else 'processing',
                'provider_id':identifier,'payment_id':evidence['payment_id'],'verified':True}
        if kind=='setup':
            evidence=self._candidate.retrieve_setup_checkout(identifier,self.consent(job))
            setup=evidence.get('setup') or {}
            return {**base,'state':'succeeded' if evidence['setup_verified'] else
                'canceled' if evidence['status']=='expired' else 'open',
                'provider_id':identifier,'payment_method_id':setup.get('payment_method_id'),'verified':True}
        if kind=='refund':
            parent=job['parent']
            evidence=self._candidate.retrieve_refund(identifier,binding=self.binding(parent),
                original_payment_id=parent['payment_id'],refund_id=job['job_id'],amount_yen=job['amount_yen'])
            return {**base,'state':evidence['status'],'provider_id':identifier,'payment_id':parent['payment_id'],'verified':True}
        raise RegistryError('sandbox_kind_invalid',status=409)

    def continuation(self,job,*,before_continue):
        if not callable(before_continue): raise ValueError('Continuation revalidation required')
        # Redirecting to Checkout / returning a 3DS secret can authorize a new
        # provider write by the customer, so read-only access is insufficient.
        self._gate(job,write=True)
        result=self.retrieve(job)
        if job['kind'] in ('checkout','setup') and result['state']=='open' and result['provider_id']:
            raw=self._read(self._client.v1.checkout.sessions,result['provider_id'])
            self._candidate._base(raw,result['provider_id'],'checkout.session')
            if job['kind']=='checkout': self._candidate._binding(raw,self.binding(job),'amount_total')
            elif raw.get('customer')!=job['provider_customer_id'] or raw.get('metadata')!=self.consent(job).metadata():
                raise RegistryError('stripe_setup_binding_mismatch',status=409)
            if raw.get('status')!='open': raise RegistryError('sandbox_continuation_unavailable',status=409)
            url=raw.get('url'); parsed=urlsplit(url) if isinstance(url,str) else None
            if not parsed or parsed.scheme!='https' or parsed.hostname!='checkout.stripe.com' or parsed.port not in (None,443) or parsed.username or parsed.password:
                raise RegistryError('stripe_checkout_url_invalid',status=409)
            before_continue(); self._gate(job,write=True)
            return {'kind':'redirect','url':url}
        if job['kind']=='off_session' and result['state']=='requires_action' and self._publishable_key:
            raw=self._read(self._client.v1.payment_intents,result['payment_id'])
            self._candidate._base(raw,result['payment_id'],'payment_intent')
            self._candidate._binding(raw,self.binding(job))
            if raw.get('status')!='requires_action': raise RegistryError('sandbox_continuation_unavailable',status=409)
            secret=raw.get('client_secret')
            if not isinstance(secret,str) or not secret.startswith(result['payment_id']+'_secret_'):
                raise RegistryError('stripe_authentication_unavailable',status=409)
            before_continue(); self._gate(job,write=True)
            return {'kind':'authentication','client_secret':secret,'publishable_key':self._publishable_key}
        raise RegistryError('sandbox_continuation_unavailable',status=409)

    def webhook_hint(self,raw,signature):
        if type(raw) is not bytes or not raw or len(raw)>1048576 or not isinstance(signature,str) or len(signature)>8192:
            raise RegistryError('stripe_webhook_input_invalid')
        try:
            event=_sdk_dict(self._sdk.Webhook.construct_event(raw,signature,self._webhook_secret,tolerance=300))
        except Exception:
            raise RegistryError('stripe_webhook_signature_invalid',status=400) from None
        if event.get('livemode') is not False or event.get('account') or event.get('context'):
            raise RegistryError('stripe_webhook_environment_mismatch',status=400)
        identifier=_identifier(event.get('id'),'evt_')
        data=event.get('data')
        obj=data.get('object') if isinstance(data,dict) else None
        supported={'checkout.session':('cs_test_','kaika_attempt_id'), 'payment_intent':('pi_','kaika_attempt_id'),
            'refund':('re_','kaika_refund_id')}
        if not isinstance(obj,dict) or obj.get('object') not in supported:
            return None
        kind=obj['object']; prefix,field=supported[kind]
        metadata=obj.get('metadata')
        if not isinstance(metadata,dict) or metadata.get('execution_mode')!='test': return None
        job=metadata.get(field) or (metadata.get('kaika_consent_id') if kind=='checkout.session' else None)
        if not isinstance(job,str) or not re.fullmatch(r'p9_[a-f0-9]{32}',job): return None
        return {'event_id':identifier,'object_id':_identifier(obj.get('id'),prefix),'object_kind':kind,
            'job_id':job,'body_hash':hashlib.sha256(raw).hexdigest()}
