"""Offline official Stripe SDK contract/signature checks; no API requests.

Install the pinned SDK into an isolated OS-temporary target and prepend it to
PYTHONPATH. A missing SDK is reported as skipped, never as a verified payment.
All messages, identifiers and HMAC material below are fabricated fixtures.
"""
import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch

try:
    import stripe
except ImportError:
    stripe = None


@unittest.skipIf(stripe is None, 'Official Stripe SDK is not installed in this isolated runtime')
class OfficialStripeSDKTest(unittest.TestCase):
    def setUp(self):
        self.secret = 'whsec_fabricated_signature_fixture_only'
        self.payload = json.dumps({'id': 'evt_offline_fixture', 'object': 'event',
            'type': 'invoice.paid', 'livemode': False,
            'data': {'object': {'id': 'in_offline_fixture', 'object': 'invoice',
                              'customer': 'cus_offline_fixture', 'status': 'paid'}}},
            ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    def header(self, payload=None, timestamp=None):
        timestamp = int(time.time()) if timestamp is None else timestamp
        raw = self.payload if payload is None else payload
        digest = hmac.new(self.secret.encode(), str(timestamp).encode() + b'.' + raw, hashlib.sha256).hexdigest()
        return f't={timestamp},v1={digest}'

    def test_construct_event_accepts_signed_raw_bytes_without_any_network(self):
        with patch('socket.socket', side_effect=AssertionError('Offline verification cannot open a socket')):
            event = stripe.Webhook.construct_event(self.payload, self.header(), self.secret)
        self.assertEqual(event.id, 'evt_offline_fixture')
        self.assertIs(event.livemode, False)
        from feature_plans import stripe_data, stripe_id
        normalized = stripe_data(event)
        self.assertIsInstance(normalized['data']['object'], dict)
        self.assertEqual(stripe_id(event.data.object), 'in_offline_fixture')
        self.assertEqual(event.data.object.status, 'paid')
        self.assertEqual(event['data']['object']['customer'], 'cus_offline_fixture')

    def test_bad_signature_and_wrong_secret_are_rejected(self):
        with self.assertRaises(stripe.SignatureVerificationError):
            stripe.Webhook.construct_event(self.payload, f't={int(time.time())},v1=invalid', self.secret)
        with self.assertRaises(stripe.SignatureVerificationError):
            stripe.Webhook.construct_event(self.payload, self.header(), 'whsec_wrong_fixture')

    def test_signature_is_over_exact_raw_bytes_not_reserialized_json(self):
        changed = self.payload + b'\n'
        self.assertEqual(json.loads(changed), json.loads(self.payload))
        with self.assertRaises(stripe.SignatureVerificationError):
            stripe.Webhook.construct_event(changed, self.header(), self.secret)
        self.assertFalse(stripe.Webhook.construct_event(changed, self.header(changed), self.secret).livemode)

    def test_old_valid_signature_is_rejected_by_timestamp_tolerance(self):
        with self.assertRaises(stripe.SignatureVerificationError):
            stripe.Webhook.construct_event(self.payload, self.header(timestamp=int(time.time()) - 600), self.secret)

    def test_offline_resource_surface_used_by_digital_billing_exists(self):
        for resource, method in ((stripe.Price, 'retrieve'), (stripe.Customer, 'create'),
                (stripe.Subscription, 'retrieve'), (stripe.Subscription, 'list'),
                (stripe.SubscriptionSchedule, 'retrieve'), (stripe.Charge, 'retrieve'),
                (stripe.Invoice, 'retrieve'), (stripe.InvoicePayment, 'list'),
                (stripe.checkout.Session, 'create'), (stripe.checkout.Session, 'retrieve'),
                (stripe.billing_portal.Session, 'create'), (stripe.billing_portal.Configuration, 'retrieve')):
            with self.subTest(resource=resource.__name__, method=method):
                self.assertTrue(callable(getattr(resource, method)))

    def test_checkout_portal_and_invoice_lookup_parameters_match_official_sdk(self):
        from stripe.params.checkout._session_create_params import SessionCreateParams as CheckoutParams
        from stripe.params.billing_portal._session_create_params import SessionCreateParams as PortalParams
        from stripe.params.billing_portal._session_create_params import SessionCreateParamsFlowData
        from stripe.params._invoice_payment_list_params import InvoicePaymentListParams, InvoicePaymentListParamsPayment
        self.assertTrue({'mode', 'customer', 'client_reference_id', 'line_items', 'subscription_data',
                         'success_url', 'cancel_url', 'locale', 'idempotency_key'} <= set(CheckoutParams.__annotations__))
        self.assertTrue({'customer', 'configuration', 'return_url', 'flow_data'} <= set(PortalParams.__annotations__))
        self.assertIn('subscription_update_confirm', SessionCreateParamsFlowData.__annotations__)
        self.assertTrue({'payment', 'limit'} <= set(InvoicePaymentListParams.__annotations__))
        self.assertTrue({'type', 'payment_intent'} <= set(InvoicePaymentListParamsPayment.__annotations__))

    def test_current_official_resources_work_through_checkout_subscription_and_refund_handlers(self):
        from test_feature_plans import FeaturePlansTest
        fixture = FeaturePlansTest('test_disabled_registration_never_touches_database_or_payment_api')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        api = fixture.stripe
        for resource, method, sdk_type in (
                (api.Price, 'retrieve', stripe.Price), (api.Customer, 'create', stripe.Customer),
                (api.Subscription, 'retrieve', stripe.Subscription), (api.Subscription, 'list', stripe.ListObject),
                (api.Charge, 'retrieve', stripe.Charge), (api.Invoice, 'retrieve', stripe.Invoice),
                (api.checkout.Session, 'create', stripe.checkout.Session),
                (api.billing_portal.Configuration, 'retrieve', stripe.billing_portal.Configuration),
                (api.billing_portal.Session, 'create', stripe.billing_portal.Session)):
            original = getattr(resource, method)
            setattr(resource, method, lambda *args, _original=original, _type=sdk_type, **kwargs:
                    _type.construct_from(_original(*args, **kwargs), None))
        api.Webhook.construct_event = stripe.Webhook.construct_event
        self.secret = 'whsec-fixture'
        def deliver(event):
            raw = json.dumps(dict(event, object='event', livemode=False)).encode()
            return fixture.client.post('/billing/webhook', data=raw,
                headers={'Stripe-Signature': self.header(raw)}, content_type='application/json')
        with patch('socket.socket', side_effect=AssertionError('No payment API call allowed')):
            fixture.configure()
            self.assertEqual(fixture.post('/billing/checkout', {'plan_code': 'normal'}).status_code, 303)
            fixture.subscription()
            self.assertEqual(deliver(fixture.event()).status_code, 200)
            self.assertEqual(fixture.state()['plan_code'], 'normal')
            self.assertEqual(fixture.post('/billing/portal').status_code, 303)
            self.assertEqual(deliver(fixture.refund_fixture()).status_code, 200)
            self.assertTrue(fixture.state()['payment_review_required'])

    def test_physical_candidate_services_exist_on_official_v1_client_without_requests(self):
        with patch('socket.socket', side_effect=AssertionError('Client inspection must stay offline')):
            client = stripe.StripeClient('sk_test_offline_fixture_not_a_real_key')
            for service, operation in ((client.v1.checkout.sessions, 'create'),
                    (client.v1.payment_intents, 'create'), (client.v1.payment_intents, 'retrieve'),
                    (client.v1.setup_intents, 'create'), (client.v1.setup_intents, 'retrieve'),
                    (client.v1.refunds, 'create'), (client.v1.refunds, 'retrieve'),
                    (client.v1.payment_methods, 'retrieve')):
                self.assertTrue(callable(getattr(service, operation)))

    def test_physical_request_plan_keys_match_current_official_parameter_schemas(self):
        from stripe.params.checkout._session_create_params import SessionCreateParams
        from stripe.params._payment_intent_create_params import PaymentIntentCreateParams
        from stripe.params._refund_create_params import RefundCreateParams
        from stage5_stripe_sandbox import (PaymentBinding, ConsentBinding, VerifiedPayment,
            checkout_plan, setup_checkout_plan, off_session_plan, refund_plan, StripeSandboxCandidate)
        binding = PaymentBinding('bill_fixture', 'attempt_fixture', 1, 'cus_fixture', 1155)
        consent = ConsentBinding(1, 'cus_fixture', 'consent_fixture', 'fixture-reviewed-terms', True)
        urls = {'success_url': 'https://fixture.invalid/success', 'cancel_url': 'https://fixture.invalid/cancel',
                'allowed_origin': 'https://fixture.invalid'}
        plans = [(checkout_plan(binding, save_consent=consent, **urls), SessionCreateParams),
                 (setup_checkout_plan(consent, **urls), SessionCreateParams),
                 (off_session_plan(binding, consent, payment_method_id='pm_fixture'), PaymentIntentCreateParams),
                 (refund_plan(VerifiedPayment('pi_fixture', binding, 'succeeded', 1155, True),
                              refund_id='refund_fixture', amount_yen=500), RefundCreateParams)]
        for plan, schema in plans:
            self.assertFalse(plan.execution_enabled)
            self.assertFalse(set(plan.params) - set(schema.__annotations__),
                             (plan.service, sorted(set(plan.params) - set(schema.__annotations__))))
        with patch('socket.socket', side_effect=AssertionError('No payment API call allowed')):
            adapter = StripeSandboxCandidate(secret_key='sk_test_offlinefixture000', sdk=stripe)
            self.assertFalse(adapter.read_enabled)


if __name__ == '__main__':
    unittest.main()
