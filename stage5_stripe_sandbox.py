"""UNWIRED Stripe sandbox request/retrieval candidate, never a live executor.

Official references checked 2026-10-22:
https://docs.stripe.com/api/checkout/sessions/create
https://docs.stripe.com/api/payment_intents/retrieve
https://docs.stripe.com/api/refunds/create
https://docs.stripe.com/payments/save-and-reuse?payment-ui=elements
https://docs.stripe.com/webhooks
https://docs.stripe.com/api/idempotent_requests

Request plans are reviewable data. execute_write ALWAYS refuses. Before wiring,
a worker must durably commit the bill/attempt, exact payload hash and provider
idempotency key, then call Stripe outside every DB transaction; persist object
IDs and reconcile timeouts instead of blindly creating new objects. Stripe may
prune keys after >=24 hours: an old key is NOT an indefinite duplicate guard.
Use fresh resource retrieval, not event.created ordering, for webhook hints.

No imports of Stripe, env discovery, default connection, webhook registration,
card collection, schema changes, or domain entitlement/payment mutation occur.
Injected SDK must be a current official StripeClient with .v1 services. The
repo's broad stripe>=7 requirement has not established that runtime capability.
"""
from dataclasses import dataclass
from copy import deepcopy
import hashlib
import re
from urllib.parse import urlsplit

from item_registry import RegistryError
from stage3_common import yen


def _sdk_dict(value):
    if isinstance(value, dict):
        return deepcopy(value)
    convert = getattr(value, 'to_dict', None)
    if callable(convert):
        converted = convert(recursive=True)
        if isinstance(converted, dict):
            return converted
    raise RegistryError('stripe_sdk_response_invalid', status=409)


def _identifier(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + r'[A-Za-z0-9_]{1,180}', value):
        raise RegistryError('stripe_identifier_invalid')
    return value


def _local_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,150}', value):
        raise RegistryError('stripe_local_reference_invalid')
    return value


def _amount(value):
    yen(value)
    if value < 1 or value > 99_999_999:
        raise RegistryError('stripe_amount_outside_candidate_range')
    return value


def _https(value, allowed_origin):
    if not isinstance(value, str):
        raise RegistryError('stripe_return_url_invalid')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment or \
            parsed.scheme + '://' + parsed.netloc != allowed_origin:
        raise RegistryError('stripe_return_url_invalid')
    return value


@dataclass(frozen=True)
class PaymentBinding:
    """Construct only from a reviewed server bill, never from client amounts."""
    bill_id: str
    attempt_id: str
    customer_id: int
    provider_customer_id: str
    amount_yen: int

    def __post_init__(self):
        _local_id(self.bill_id)
        _local_id(self.attempt_id)
        if type(self.customer_id) is not int or self.customer_id < 1:
            raise RegistryError('stripe_customer_invalid')
        _identifier(self.provider_customer_id, 'cus_')
        _amount(self.amount_yen)

    def metadata(self):
        return {'kaika_bill_id': self.bill_id, 'kaika_attempt_id': self.attempt_id,
                'kaika_customer_id': str(self.customer_id), 'execution_mode': 'test', 'record_kind': 'physical_services'}


@dataclass(frozen=True)
class ConsentBinding:
    customer_id: int
    provider_customer_id: str
    consent_id: str
    terms_reference: str
    off_session: bool

    def __post_init__(self):
        if type(self.customer_id) is not int or self.customer_id < 1 or type(self.off_session) is not bool:
            raise RegistryError('stripe_consent_invalid')
        _identifier(self.provider_customer_id, 'cus_')
        _local_id(self.consent_id)
        if not isinstance(self.terms_reference, str) or not self.terms_reference.strip() or len(self.terms_reference) > 2000:
            raise RegistryError('stripe_consent_terms_required')

    def metadata(self):
        return {'kaika_customer_id': str(self.customer_id), 'kaika_consent_id': self.consent_id,
                'terms_sha256': hashlib.sha256(self.terms_reference.encode()).hexdigest(),
                'execution_mode': 'test', 'record_kind': 'physical_card_consent'}


@dataclass(frozen=True)
class RequestPlan:
    service: str
    params: dict
    options: dict
    execution_enabled: bool = False


@dataclass(frozen=True)
class VerifiedPayment:
    provider_id: str
    binding: PaymentBinding
    status: str
    amount_received_yen: int
    payment_verified: bool
    environment: str = 'sandbox'


@dataclass(frozen=True)
class VerifiedWebhookHint:
    event_id: str
    event_type: str
    object_id: str
    object_kind: str
    environment: str = 'sandbox'
    payment_verified: bool = False


def _plan(service, params, operation, local_id):
    # Namespace separates Checkout, SetupIntent, PaymentIntent and Refund calls.
    # The future outbox must persist this key AND immutable params before send.
    key = 'kaika:sandbox:' + operation + ':' + _local_id(local_id)
    return RequestPlan(service, deepcopy(params), {'idempotency_key': key})


def checkout_plan(binding, *, success_url, cancel_url, allowed_origin, save_consent=None):
    if type(binding) is not PaymentBinding:
        raise RegistryError('stripe_binding_required')
    params = {'mode': 'payment', 'customer': binding.provider_customer_id,
        'client_reference_id': binding.attempt_id, 'allowed_payment_method_types': ['card'],
        'success_url': _https(success_url, allowed_origin), 'cancel_url': _https(cancel_url, allowed_origin),
        'metadata': binding.metadata(), 'payment_intent_data': {'metadata': binding.metadata()},
        'automatic_tax': {'enabled': False}, 'allow_promotion_codes': False,
        'line_items': [{'quantity': 1, 'price_data': {'currency': 'jpy', 'unit_amount': binding.amount_yen,
            'product_data': {'name': '開花 現物サービス確認済み請求（テスト）'}}}]}
    # Omit ui_mode to retain Stripe's hosted default across supported API
    # versions; no embedded card form or raw card data enters this application.
    if save_consent is not None:
        _matching_consent(binding, save_consent)
        if not save_consent.off_session:
            raise RegistryError('stripe_off_session_consent_required')
        params['payment_intent_data']['setup_future_usage'] = 'off_session'
    return _plan('v1.checkout.sessions.create', params, 'checkout', binding.attempt_id)


def _matching_consent(binding, consent):
    if type(consent) is not ConsentBinding or consent.customer_id != binding.customer_id or consent.provider_customer_id != binding.provider_customer_id:
        raise RegistryError('stripe_consent_customer_mismatch')


def off_session_plan(binding, consent, *, payment_method_id):
    if type(binding) is not PaymentBinding:
        raise RegistryError('stripe_binding_required')
    _matching_consent(binding, consent)
    if not consent.off_session:
        raise RegistryError('stripe_off_session_consent_required')
    params = {'amount': binding.amount_yen, 'currency': 'jpy', 'customer': binding.provider_customer_id,
        'payment_method': _identifier(payment_method_id, 'pm_'), 'allowed_payment_method_types': ['card'],
        'off_session': True, 'confirm': True, 'capture_method': 'automatic', 'metadata': binding.metadata()}
    return _plan('v1.payment_intents.create', params, 'payment', binding.attempt_id)


def setup_checkout_plan(consent, *, success_url, cancel_url, allowed_origin):
    if type(consent) is not ConsentBinding or not consent.off_session:
        raise RegistryError('stripe_off_session_consent_required')
    params = {'mode': 'setup', 'customer': consent.provider_customer_id, 'currency': 'jpy',
        'allowed_payment_method_types': ['card'], 'client_reference_id': consent.consent_id,
        'metadata': consent.metadata(), 'setup_intent_data': {'metadata': consent.metadata()},
        'success_url': _https(success_url, allowed_origin), 'cancel_url': _https(cancel_url, allowed_origin)}
    return _plan('v1.checkout.sessions.create', params, 'setup-checkout', consent.consent_id)


def refund_plan(payment, *, refund_id, amount_yen):
    if type(payment) is not VerifiedPayment or not payment.payment_verified or payment.status != 'succeeded' or payment.environment != 'sandbox':
        raise RegistryError('stripe_verified_original_payment_required')
    amount = _amount(amount_yen)
    if amount > payment.amount_received_yen:
        raise RegistryError('stripe_refund_exceeds_original')
    # Aggregate remaining capacity still belongs to the durable local refund
    # ledger; this pure builder cannot check concurrent requests or past refunds.
    metadata = payment.binding.metadata() | {'kaika_refund_id': _local_id(refund_id)}
    return _plan('v1.refunds.create', {'payment_intent': payment.provider_id, 'amount': amount,
        'metadata': metadata, 'reason': 'requested_by_customer'}, 'refund', refund_id)


class StripeSandboxCandidate:
    """Explicitly constructed, read-only SDK candidate. No write executor exists."""
    def __init__(self, *, secret_key, sdk, webhook_secret=None, read_enabled=False, http_client=None):
        if not isinstance(secret_key, str) or not re.fullmatch(r'sk_test_[A-Za-z0-9]{8,}', secret_key):
            raise RegistryError('stripe_sandbox_key_required')
        if webhook_secret is not None and (not isinstance(webhook_secret, str) or not re.fullmatch(r'whsec_[A-Za-z0-9]{8,}', webhook_secret)):
            raise RegistryError('stripe_webhook_secret_invalid')
        if type(read_enabled) is not bool:
            raise RegistryError('stripe_read_flag_invalid')
        self._sdk, self._webhook_secret, self.read_enabled = sdk, webhook_secret, read_enabled
        try:
            self._client = sdk.StripeClient(secret_key, max_network_retries=0,
                **({'http_client': http_client} if http_client is not None else {}))
            self._client.v1.payment_intents
            self._client.v1.checkout.sessions
            self._client.v1.refunds
            self._client.v1.setup_intents
            self._client.v1.payment_methods
        except Exception:
            raise RegistryError('stripe_sdk_candidate_incompatible', status=409) from None

    def execute_write(self, plan):
        raise RegistryError('stripe_durable_outbox_not_connected', 'Stripe検証決済は未接続です。送信前の永続化・結果照合を接続する必要があります。', 409)

    def _retrieve(self, service, identifier):
        if not self.read_enabled:
            raise RegistryError('stripe_sandbox_reads_disabled', status=409)
        try:
            result = service.retrieve(identifier)
            # Stripe 16 StripeObject uses .to_dict(recursive=True), not Mapping.
            # Whitelist projections below never
            # expose client_secret, hosted URLs, full billing details or errors.
            return _sdk_dict(result)
        except Exception:
            raise RegistryError('stripe_sandbox_retrieval_failed', 'Stripe検証結果を取得できません。結果未確認として再照合が必要です。', 409) from None

    @staticmethod
    def _base(record, identifier, object_kind, *, require_mode=True):
        if record.get('id') != identifier or record.get('object') != object_kind:
            raise RegistryError('stripe_object_identity_mismatch', status=409)
        if require_mode and record.get('livemode') is not False or not require_mode and record.get('livemode') not in (None, False):
            raise RegistryError('stripe_live_or_unknown_environment', status=409)

    @staticmethod
    def _binding(record, binding, amount_key='amount'):
        if record.get('customer') != binding.provider_customer_id or record.get('metadata') != binding.metadata() or \
                record.get('currency') != 'jpy' or type(record.get(amount_key)) is not int or record.get(amount_key) != binding.amount_yen:
            raise RegistryError('stripe_payment_binding_mismatch', status=409)

    def retrieve_payment(self, provider_id, binding):
        if type(binding) is not PaymentBinding:
            raise RegistryError('stripe_binding_required')
        _identifier(provider_id, 'pi_')
        record = self._retrieve(self._client.v1.payment_intents, provider_id)
        self._base(record, provider_id, 'payment_intent')
        self._binding(record, binding)
        status = record.get('status')
        if status not in ('requires_payment_method', 'requires_confirmation', 'requires_action', 'processing', 'requires_capture', 'canceled', 'succeeded'):
            raise RegistryError('stripe_payment_status_unknown', status=409)
        received = record.get('amount_received')
        if type(received) is not int or received < 0 or received > binding.amount_yen:
            raise RegistryError('stripe_received_amount_mismatch', status=409)
        paid = status == 'succeeded'
        if paid and (received != binding.amount_yen or record.get('amount_capturable') != 0):
            raise RegistryError('stripe_received_amount_mismatch', status=409)
        return VerifiedPayment(provider_id, binding, status, received, paid)

    def retrieve_checkout(self, session_id, binding):
        _identifier(session_id, 'cs_test_')
        record = self._retrieve(self._client.v1.checkout.sessions, session_id)
        self._base(record, session_id, 'checkout.session')
        self._binding(record, binding, 'amount_total')
        if record.get('mode') != 'payment' or record.get('client_reference_id') != binding.attempt_id or \
                record.get('status') not in ('open', 'complete', 'expired') or record.get('payment_status') not in ('unpaid', 'paid', 'no_payment_required'):
            raise RegistryError('stripe_checkout_binding_mismatch', status=409)
        payment_id = record.get('payment_intent')
        payment = self.retrieve_payment(payment_id, binding) if payment_id else None
        paid = record['status'] == 'complete' and record['payment_status'] == 'paid'
        if paid and (payment is None or not payment.payment_verified):
            raise RegistryError('stripe_checkout_payment_not_confirmed', status=409)
        return {'session_id': session_id, 'status': record['status'], 'payment_status': record['payment_status'],
                'payment_id': payment_id, 'payment_verified': bool(paid), 'environment': 'sandbox',
                'client_redirect_is_evidence': False}

    def retrieve_setup(self, setup_id, consent):
        if type(consent) is not ConsentBinding or not consent.off_session:
            raise RegistryError('stripe_off_session_consent_required')
        _identifier(setup_id, 'seti_')
        record = self._retrieve(self._client.v1.setup_intents, setup_id)
        self._base(record, setup_id, 'setup_intent')
        if record.get('customer') != consent.provider_customer_id or record.get('metadata') != consent.metadata() or record.get('usage') != 'off_session':
            raise RegistryError('stripe_setup_binding_mismatch', status=409)
        status = record.get('status')
        if status not in ('requires_payment_method', 'requires_confirmation', 'requires_action', 'processing', 'canceled', 'succeeded'):
            raise RegistryError('stripe_setup_status_unknown', status=409)
        method_id = None
        if status == 'succeeded':
            method_id = _identifier(record.get('payment_method'), 'pm_')
            method = self._retrieve(self._client.v1.payment_methods, method_id)
            self._base(method, method_id, 'payment_method')
            if method.get('customer') != consent.provider_customer_id or method.get('type') != 'card':
                raise RegistryError('stripe_saved_card_customer_mismatch', status=409)
        return {'setup_id': setup_id, 'consent_id': consent.consent_id, 'status': status, 'payment_method_id': method_id,
                'setup_verified': status == 'succeeded', 'environment': 'sandbox', 'payment_verified': False}

    def retrieve_setup_checkout(self, session_id, consent):
        if type(consent) is not ConsentBinding or not consent.off_session:
            raise RegistryError('stripe_off_session_consent_required')
        _identifier(session_id, 'cs_test_')
        record = self._retrieve(self._client.v1.checkout.sessions, session_id)
        self._base(record, session_id, 'checkout.session')
        if record.get('mode') != 'setup' or record.get('customer') != consent.provider_customer_id or \
                record.get('metadata') != consent.metadata() or record.get('client_reference_id') != consent.consent_id or \
                record.get('status') not in ('open', 'complete', 'expired'):
            raise RegistryError('stripe_setup_binding_mismatch', status=409)
        setup_id = record.get('setup_intent')
        setup = self.retrieve_setup(setup_id, consent) if setup_id else None
        if record['status'] == 'complete' and (setup is None or not setup['setup_verified']):
            raise RegistryError('stripe_setup_not_confirmed', status=409)
        return {'session_id': session_id, 'status': record['status'], 'setup': setup,
                'setup_verified': bool(record['status'] == 'complete' and setup and setup['setup_verified']),
                'environment': 'sandbox', 'payment_verified': False}

    def retrieve_refund(self, provider_refund_id, *, binding, original_payment_id, refund_id, amount_yen):
        _identifier(provider_refund_id, 're_')
        amount = _amount(amount_yen)
        original = self.retrieve_payment(original_payment_id, binding)
        if not original.payment_verified or amount > original.amount_received_yen:
            raise RegistryError('stripe_verified_original_payment_required', status=409)
        record = self._retrieve(self._client.v1.refunds, provider_refund_id)
        # Refund objects do not consistently contain livemode. Parent PI is
        # freshly retrieved with the test client and MUST have livemode=false.
        self._base(record, provider_refund_id, 'refund', require_mode=False)
        if record.get('payment_intent') != original_payment_id or record.get('amount') != amount or type(record.get('amount')) is not int or \
                record.get('currency') != 'jpy' or record.get('metadata') != binding.metadata() | {'kaika_refund_id': _local_id(refund_id)} or \
                record.get('customer') not in (None, binding.provider_customer_id):
            raise RegistryError('stripe_refund_binding_mismatch', status=409)
        if record.get('status') not in ('pending', 'requires_action', 'succeeded', 'failed', 'canceled'):
            raise RegistryError('stripe_refund_status_unknown', status=409)
        return {'refund_id': provider_refund_id, 'original_payment_id': original_payment_id,
                'amount_yen': amount, 'currency': 'JPY', 'status': record['status'],
                'refund_verified': record['status'] == 'succeeded', 'environment': 'sandbox'}

    def verify_webhook_hint(self, raw_body, signature_header):
        if self._webhook_secret is None:
            raise RegistryError('stripe_webhook_unconfigured', status=409)
        if type(raw_body) is not bytes or not raw_body or len(raw_body) > 1_048_576 or not isinstance(signature_header, str) or len(signature_header) > 8192:
            raise RegistryError('stripe_webhook_input_invalid')
        try:
            event = self._sdk.Webhook.construct_event(raw_body, signature_header, self._webhook_secret, tolerance=300)
            event = _sdk_dict(event)
        except Exception:
            raise RegistryError('stripe_webhook_signature_invalid', status=400) from None
        if event.get('livemode') is not False or event.get('account') or event.get('context'):
            raise RegistryError('stripe_webhook_environment_mismatch', status=409)
        event_id = _identifier(event.get('id'), 'evt_')
        event_type = event.get('type')
        supported = {'payment_intent.succeeded': ('payment_intent', 'pi_'),
            'payment_intent.payment_failed': ('payment_intent', 'pi_'), 'payment_intent.processing': ('payment_intent', 'pi_'),
            'payment_intent.requires_action': ('payment_intent', 'pi_'), 'payment_intent.canceled': ('payment_intent', 'pi_'),
            'checkout.session.completed': ('checkout.session', 'cs_test_'),
            'checkout.session.async_payment_succeeded': ('checkout.session', 'cs_test_'),
            'checkout.session.async_payment_failed': ('checkout.session', 'cs_test_'),
            'setup_intent.succeeded': ('setup_intent', 'seti_'), 'setup_intent.setup_failed': ('setup_intent', 'seti_'),
            'refund.created': ('refund', 're_'), 'refund.updated': ('refund', 're_'), 'refund.failed': ('refund', 're_')}
        if event_type not in supported:
            raise RegistryError('stripe_webhook_type_unsupported', status=400)
        obj = event.get('data', {}).get('object', {})
        kind, prefix = supported[event_type]
        if not isinstance(obj, dict) or obj.get('object') != kind:
            raise RegistryError('stripe_webhook_object_invalid')
        object_id = _identifier(obj.get('id'), prefix)
        # No event timestamp or payload status is a revision/paid authority.
        # Persist/deduplicate this event ID, enqueue known-object retrieval, and
        # match its local customer/bill bindings before changing any domain row.
        return VerifiedWebhookHint(event_id, event_type, object_id, kind)
