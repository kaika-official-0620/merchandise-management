"""Injected SDK contract tests; no Stripe package, key, API request or real card."""
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
import hashlib
import hmac
import json
import time
import unittest

from item_registry import RegistryError
from stage5_stripe_sandbox import (PaymentBinding, ConsentBinding, StripeSandboxCandidate,
    checkout_plan, off_session_plan, setup_checkout_plan, refund_plan)


class FakeResource:
    def __init__(self, owner, kind):
        self.owner, self.kind = owner, kind

    def retrieve(self, identifier):
        self.owner.reads.append((self.kind, identifier))
        if self.owner.error:
            raise RuntimeError('sk_test_do_not_log_client_secret_payload')
        return deepcopy(self.owner.objects[identifier])


class FakeSDK:
    def __init__(self):
        self.objects, self.reads, self.constructed, self.webhooks = {}, [], [], []
        self.error = False
        self.Webhook = SimpleNamespace(construct_event=self.verify)

    def StripeClient(self, secret_key, max_network_retries):
        self.constructed.append({'is_test': secret_key.startswith('sk_test_'), 'max_network_retries': max_network_retries})
        return SimpleNamespace(v1=SimpleNamespace(
            payment_intents=FakeResource(self, 'payment_intent'), refunds=FakeResource(self, 'refund'),
            setup_intents=FakeResource(self, 'setup_intent'), payment_methods=FakeResource(self, 'payment_method'),
            checkout=SimpleNamespace(sessions=FakeResource(self, 'checkout.session'))))

    def verify(self, raw, signature, secret, tolerance):
        # Test double verifies a Stripe-shaped HMAC only to exercise the caller
        # boundary. This does not claim an actual Stripe SDK integration test.
        self.webhooks.append((raw, signature, tolerance))
        fields = dict(entry.split('=', 1) for entry in signature.split(','))
        timestamp = int(fields['t'])
        wanted = hmac.new(secret.encode(), str(timestamp).encode() + b'.' + raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(wanted, fields['v1']) or abs(int(time.time()) - timestamp) > tolerance:
            raise ValueError('signature failed whsec_do_not_log')
        return json.loads(raw)


class StripeSandboxCandidateTests(unittest.TestCase):
    def setUp(self):
        self.sdk = FakeSDK()
        self.candidate = StripeSandboxCandidate(secret_key='sk_test_fictional12345', sdk=self.sdk,
            webhook_secret='whsec_fictional12345', read_enabled=True)
        self.binding = PaymentBinding('pb5_fictional', 'pa5_fictional', 1, 'cus_fictional', 1100)
        self.consent = ConsentBinding(1, 'cus_fictional', 'cc5_fictional', 'fixture-terms-v1', True)

    def assertCode(self, code, action):
        with self.assertRaises(RegistryError) as error:
            action()
        self.assertEqual(error.exception.code, code)

    def payment(self, status='succeeded', **changes):
        record = {'id': 'pi_fictional', 'object': 'payment_intent', 'livemode': False, 'customer': 'cus_fictional',
            'metadata': self.binding.metadata(), 'amount': 1100, 'amount_received': 1100 if status == 'succeeded' else 0,
            'amount_capturable': 0, 'currency': 'jpy', 'status': status, 'client_secret': 'never_expose_me', **changes}
        self.sdk.objects['pi_fictional'] = record
        return record

    def checkout(self, **changes):
        self.payment()
        record = {'id': 'cs_test_fictional', 'object': 'checkout.session', 'livemode': False,
            'customer': 'cus_fictional', 'metadata': self.binding.metadata(), 'amount_total': 1100, 'currency': 'jpy',
            'mode': 'payment', 'client_reference_id': 'pa5_fictional', 'status': 'complete', 'payment_status': 'paid',
            'payment_intent': 'pi_fictional', 'url': 'https://checkout.stripe.com/private', **changes}
        self.sdk.objects['cs_test_fictional'] = record
        return record

    def setup(self, **changes):
        self.sdk.objects['seti_fictional'] = {'id': 'seti_fictional', 'object': 'setup_intent', 'livemode': False,
            'customer': 'cus_fictional', 'metadata': self.consent.metadata(), 'usage': 'off_session',
            'status': 'succeeded', 'payment_method': 'pm_fictional', 'client_secret': 'never_expose_me', **changes}
        self.sdk.objects['pm_fictional'] = {'id': 'pm_fictional', 'object': 'payment_method', 'livemode': False,
            'customer': 'cus_fictional', 'type': 'card', 'card': {'last4': '4242'}, 'billing_details': {'name': 'private'}}

    def signed(self, event, timestamp=None):
        raw = json.dumps(event, separators=(',', ':')).encode()
        timestamp = int(time.time()) if timestamp is None else timestamp
        signature = hmac.new(b'whsec_fictional12345', str(timestamp).encode() + b'.' + raw, hashlib.sha256).hexdigest()
        return raw, 't=' + str(timestamp) + ',v1=' + signature

    def event(self, **changes):
        return {'id': 'evt_fictional', 'type': 'payment_intent.succeeded', 'livemode': False,
            'created': 1, 'data': {'object': {'id': 'pi_fictional', 'object': 'payment_intent', 'status': 'succeeded', 'amount': 1}}, **changes}

    def test_live_and_unknown_keys_rejected_before_sdk(self):
        for key in ('sk_live_secret12345', 'pk_test_public', '', None):
            self.assertCode('stripe_sandbox_key_required', lambda: StripeSandboxCandidate(secret_key=key, sdk=self.sdk))
        self.assertEqual(len(self.sdk.constructed), 1)

    def test_reads_disabled_by_default(self):
        self.payment()
        candidate = StripeSandboxCandidate(secret_key='sk_test_fictional12345', sdk=self.sdk)
        self.assertCode('stripe_sandbox_reads_disabled', lambda: candidate.retrieve_payment('pi_fictional', self.binding))
        self.assertEqual(self.sdk.reads, [])

    def test_writes_always_disabled_even_with_valid_plan(self):
        plan = off_session_plan(self.binding, self.consent, payment_method_id='pm_fictional')
        self.assertCode('stripe_durable_outbox_not_connected', lambda: self.candidate.execute_write(plan))
        self.assertEqual(self.sdk.reads, [])

    def test_checkout_is_single_physical_payment_not_subscription(self):
        plan = checkout_plan(self.binding, success_url='https://example.test/ok', cancel_url='https://example.test/back', allowed_origin='https://example.test')
        self.assertEqual(plan.service, 'v1.checkout.sessions.create')
        self.assertEqual(plan.params['mode'], 'payment')
        self.assertEqual(plan.params['allowed_payment_method_types'], ['card'])
        self.assertNotIn('payment_method_types', plan.params)
        self.assertEqual(plan.params['line_items'][0]['price_data']['unit_amount'], 1100)
        self.assertFalse(plan.params['automatic_tax']['enabled'])
        self.assertNotIn('setup_future_usage', plan.params['payment_intent_data'])
        self.assertFalse(plan.execution_enabled)
        self.assertEqual(plan.params['payment_intent_data']['metadata'], self.binding.metadata())

    def test_off_session_uses_saved_method_and_separate_consent(self):
        plan = off_session_plan(self.binding, self.consent, payment_method_id='pm_fictional')
        self.assertTrue(plan.params['off_session'])
        self.assertEqual(plan.params['allowed_payment_method_types'], ['card'])
        self.assertNotIn('payment_method_types', plan.params)
        self.assertTrue(plan.params['confirm'])
        self.assertEqual(plan.params['currency'], 'jpy')
        self.assertCode('stripe_off_session_consent_required', lambda: off_session_plan(self.binding, replace(self.consent, off_session=False), payment_method_id='pm_fictional'))

    def test_foreign_consent_rejected_and_raw_card_number_never_accepted(self):
        self.assertCode('stripe_consent_customer_mismatch', lambda: off_session_plan(self.binding, replace(self.consent, customer_id=2), payment_method_id='pm_fictional'))
        self.assertCode('stripe_identifier_invalid', lambda: off_session_plan(self.binding, self.consent, payment_method_id='4242424242424242'))

    def test_save_future_usage_requires_explicit_agreement(self):
        plan = checkout_plan(self.binding, success_url='https://example.test/ok', cancel_url='https://example.test/back', allowed_origin='https://example.test', save_consent=self.consent)
        self.assertEqual(plan.params['payment_intent_data']['setup_future_usage'], 'off_session')

    def test_setup_checkout_stores_terms_hash_without_charge(self):
        plan = setup_checkout_plan(self.consent, success_url='https://example.test/ok', cancel_url='https://example.test/back', allowed_origin='https://example.test')
        self.assertEqual(plan.params['mode'], 'setup')
        self.assertEqual(plan.params['allowed_payment_method_types'], ['card'])
        self.assertNotIn('payment_method_types', plan.params)
        self.assertNotIn('amount', plan.params)
        self.assertNotIn(self.consent.terms_reference, json.dumps(plan.params))
        self.assertEqual(plan.params['setup_intent_data']['metadata'], self.consent.metadata())

    def test_provider_operation_keys_are_stable_and_separated(self):
        first = off_session_plan(self.binding, self.consent, payment_method_id='pm_fictional')
        repeat = off_session_plan(self.binding, self.consent, payment_method_id='pm_fictional')
        checkout = checkout_plan(self.binding, success_url='https://example.test/ok', cancel_url='https://example.test/back', allowed_origin='https://example.test')
        self.assertEqual(first.options, repeat.options)
        self.assertNotEqual(first.options, checkout.options)

    def test_return_url_origin_and_https_enforced(self):
        for url in ('https://evil.test/ok', 'http://example.test/ok', 'https://user:secret@example.test/ok', 'https://example.test/ok#secret'):
            self.assertCode('stripe_return_url_invalid', lambda: checkout_plan(self.binding, success_url=url, cancel_url='https://example.test/back', allowed_origin='https://example.test'))

    def test_amount_integer_positive_and_limits(self):
        for amount in (0, -1, True, 1.2, 100000000):
            with self.assertRaises(RegistryError):
                replace(self.binding, amount_yen=amount)

    def test_success_freshly_retrieved_without_client_secret(self):
        self.payment()
        result = self.candidate.retrieve_payment('pi_fictional', self.binding)
        self.assertTrue(result.payment_verified)
        self.assertEqual(self.sdk.reads, [('payment_intent', 'pi_fictional')])
        self.assertNotIn('client_secret', json.dumps(asdict(result)))

    def test_pending_action_failure_and_manual_capture_are_not_payment(self):
        for status in ('requires_payment_method', 'requires_confirmation', 'requires_action', 'processing', 'requires_capture', 'canceled'):
            self.payment(status)
            self.assertFalse(self.candidate.retrieve_payment('pi_fictional', self.binding).payment_verified)

    def test_live_missing_environment_and_customer_amount_metadata_mismatch(self):
        for changes in ({'livemode': True}, {'livemode': None}, {'customer': 'cus_other'}, {'amount': 1101}, {'amount': True},
                        {'currency': 'usd'}, {'metadata': {}}, {'id': 'pi_other'}):
            self.payment(**changes)
            with self.assertRaises(RegistryError):
                self.candidate.retrieve_payment('pi_fictional', self.binding)

    def test_success_requires_full_received_amount_and_no_pending_capture(self):
        for changes in ({'amount_received': 0}, {'amount_received': 1099}, {'amount_capturable': 1}):
            self.payment(**changes)
            self.assertCode('stripe_received_amount_mismatch', lambda: self.candidate.retrieve_payment('pi_fictional', self.binding))

    def test_checkout_requires_fresh_payment_intent_verification(self):
        self.checkout()
        result = self.candidate.retrieve_checkout('cs_test_fictional', self.binding)
        self.assertTrue(result['payment_verified'])
        self.assertFalse(result['client_redirect_is_evidence'])
        self.assertNotIn('url', result)
        self.assertEqual(self.sdk.reads, [('checkout.session', 'cs_test_fictional'), ('payment_intent', 'pi_fictional')])

    def test_paid_checkout_without_payment_is_rejected(self):
        self.checkout(payment_intent=None)
        self.assertCode('stripe_checkout_payment_not_confirmed', lambda: self.candidate.retrieve_checkout('cs_test_fictional', self.binding))

    def test_checkout_completed_is_not_sufficient_for_paid(self):
        self.checkout(payment_status='unpaid')
        self.payment('requires_action')
        self.assertFalse(self.candidate.retrieve_checkout('cs_test_fictional', self.binding)['payment_verified'])

    def test_setup_verifies_saved_method_belongs_to_same_customer(self):
        self.setup()
        result = self.candidate.retrieve_setup('seti_fictional', self.consent)
        self.assertTrue(result['setup_verified'])
        self.assertFalse(result['payment_verified'])
        self.assertNotIn('billing_details', json.dumps(result))
        self.sdk.objects['pm_fictional']['customer'] = 'cus_other'
        self.assertCode('stripe_saved_card_customer_mismatch', lambda: self.candidate.retrieve_setup('seti_fictional', self.consent))

    def test_setup_checkout_completion_validates_setup_intent(self):
        self.setup()
        self.sdk.objects['cs_test_setup'] = {'id': 'cs_test_setup', 'object': 'checkout.session', 'livemode': False,
            'mode': 'setup', 'customer': 'cus_fictional', 'metadata': self.consent.metadata(),
            'client_reference_id': 'cc5_fictional', 'status': 'complete', 'setup_intent': 'seti_fictional'}
        result = self.candidate.retrieve_setup_checkout('cs_test_setup', self.consent)
        self.assertTrue(result['setup_verified'])
        self.assertFalse(result['payment_verified'])

    def test_refund_request_links_original_verified_payment(self):
        self.payment()
        verified = self.candidate.retrieve_payment('pi_fictional', self.binding)
        plan = refund_plan(verified, refund_id='rf5_fictional', amount_yen=700)
        self.assertEqual(plan.params['payment_intent'], 'pi_fictional')
        self.assertEqual(plan.params['amount'], 700)
        self.assertCode('stripe_refund_exceeds_original', lambda: refund_plan(verified, refund_id='rf5_fictional', amount_yen=1101))

    def test_refund_without_livemode_uses_fresh_original_test_payment(self):
        self.payment()
        self.sdk.objects['re_fictional'] = {'id': 're_fictional', 'object': 'refund', 'payment_intent': 'pi_fictional',
            'amount': 700, 'currency': 'jpy', 'metadata': self.binding.metadata() | {'kaika_refund_id': 'rf5_fictional'}, 'status': 'succeeded'}
        result = self.candidate.retrieve_refund('re_fictional', binding=self.binding, original_payment_id='pi_fictional', refund_id='rf5_fictional', amount_yen=700)
        self.assertTrue(result['refund_verified'])
        self.sdk.objects['re_fictional']['payment_intent'] = 'pi_other'
        self.assertCode('stripe_refund_binding_mismatch', lambda: self.candidate.retrieve_refund('re_fictional', binding=self.binding,
            original_payment_id='pi_fictional', refund_id='rf5_fictional', amount_yen=700))

    def test_refund_requires_action_not_success(self):
        self.payment()
        self.sdk.objects['re_fictional'] = {'id': 're_fictional', 'object': 'refund', 'payment_intent': 'pi_fictional',
            'amount': 700, 'currency': 'jpy', 'metadata': self.binding.metadata() | {'kaika_refund_id': 'rf5_fictional'}, 'status': 'requires_action'}
        self.assertFalse(self.candidate.retrieve_refund('re_fictional', binding=self.binding,
            original_payment_id='pi_fictional', refund_id='rf5_fictional', amount_yen=700)['refund_verified'])

    def test_signature_verified_hint_contains_no_payment_authority_or_created_order(self):
        body, signature = self.signed(self.event())
        hint = self.candidate.verify_webhook_hint(body, signature)
        self.assertEqual(hint.object_id, 'pi_fictional')
        self.assertFalse(hint.payment_verified)
        self.assertNotIn('created', asdict(hint))
        self.assertNotIn('amount', asdict(hint))
        self.assertEqual(self.sdk.webhooks[-1], (body, signature, 300))
        self.assertEqual(self.sdk.reads, [])

    def test_bad_modified_and_old_webhook_signatures_rejected(self):
        body, signature = self.signed(self.event())
        for payload, header in ((body + b' ', signature), (body, 't=1,v1=bad'), self.signed(self.event(), timestamp=1)):
            self.assertCode('stripe_webhook_signature_invalid', lambda: self.candidate.verify_webhook_hint(payload, header))

    def test_verified_live_connected_account_or_unsupported_events_rejected(self):
        for changes in ({'livemode': True}, {'account': 'acct_other'}, {'context': 'acct_other'}, {'type': 'customer.created'}):
            payload, signature = self.signed(self.event(**changes))
            with self.assertRaises(RegistryError):
                self.candidate.verify_webhook_hint(payload, signature)

    def test_read_errors_do_not_leak_provider_keys_or_client_secrets(self):
        self.sdk.error = True
        with self.assertRaises(RegistryError) as result:
            self.candidate.retrieve_payment('pi_fictional', self.binding)
        self.assertNotIn('sk_test_', str(result.exception))
        self.assertNotIn('client_secret', str(result.exception))
        self.assertIsNone(result.exception.__cause__)

    def test_official_sdk_object_converter_without_mapping_protocol(self):
        record = self.payment()
        class SDKObject:
            def to_dict(self, recursive=False):
                if not recursive:
                    raise AssertionError('recursive conversion required')
                return deepcopy(record)
        self.sdk.objects['pi_fictional'] = SDKObject()
        self.assertTrue(self.candidate.retrieve_payment('pi_fictional', self.binding).payment_verified)


if __name__ == '__main__':
    unittest.main()
