"""Isolated store adapter/HTTP tests; no real store credentials or network."""
from dataclasses import replace
from datetime import datetime, timezone
import base64
import hashlib
import hmac
import json
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import test_feature_plans as plan_tests
from feature_store_billing import AppleVerifier, GoogleVerifier, StoreEvidence, StoreNotification, VerificationError, google_key, register_store_billing


class FakeStore:
    def __init__(self):
        self.evidence = {}
        self.acknowledged = []
        self.fail_once = False

    def verify(self, reference):
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError('temporary fixture failure')
        if reference not in self.evidence:
            raise VerificationError('fixture_invalid_evidence')
        return self.evidence[reference]

    def acknowledge(self, reference, evidence):
        if evidence.acknowledge:
            self.acknowledged.append(reference)

    def notification(self, body, authorization):
        reference = body.get('reference', '')
        signature = hmac.new(b'fixture-notification', reference.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(authorization, signature):
            raise VerificationError('fixture_invalid_signature')
        return StoreNotification(body['event_id'], reference)


class StoreBillingTest(unittest.TestCase):
    db = plan_tests.FeaturePlansTest.db
    close_connections = plan_tests.FeaturePlansTest.close_connections
    login = plan_tests.FeaturePlansTest.login
    state = plan_tests.FeaturePlansTest.state

    def setUp(self):
        plan_tests.FeaturePlansTest.setUp(self)
        self.env_store = patch.dict(os.environ, {
            'FEATURE_STORE_ENVIRONMENT': 'test', 'FEATURE_APPLE_BILLING_ENABLED': '1', 'FEATURE_GOOGLE_BILLING_ENABLED': '1',
            'FEATURE_STORE_TERMS_URL': 'https://fixture.invalid/terms', 'FEATURE_STORE_PRIVACY_URL': 'https://fixture.invalid/privacy',
            'FEATURE_APPLE_BUNDLE_ID': 'fixture.apple', 'FEATURE_APPLE_SUBSCRIPTION_GROUP_ID': 'fixture-group',
            'FEATURE_APPLE_ROOT_CERTIFICATES': '["fixture-unused.der"]', 'FEATURE_APPLE_PRIVATE_KEY_PATH': 'fixture-unused.p8',
            'FEATURE_APPLE_KEY_ID': 'fixture-key', 'FEATURE_APPLE_ISSUER_ID': 'fixture-issuer',
            'FEATURE_GOOGLE_PACKAGE_NAME': 'fixture.google', 'FEATURE_GOOGLE_SERVICE_ACCOUNT_PATH': 'fixture-unused.json',
            'FEATURE_GOOGLE_PUBSUB_AUDIENCE': 'https://fixture.invalid/billing/google/notifications',
            'FEATURE_GOOGLE_PUBSUB_EMAIL': 'fixture@example.invalid',
            'FEATURE_GOOGLE_PUBSUB_SUBSCRIPTION': 'projects/fixture/subscriptions/fixture',
            'FEATURE_STORE_PRODUCTS': json.dumps({p: [dict(plan_code=code, product_id='fixture_' + code,
                **({'base_plan_id': 'monthly'} if p == 'google' else {})) for code in ('normal', 'business')] for p in ('apple', 'google')}),
        })
        self.env_store.start(); self.addCleanup(self.env_store.stop)
        self.store = register_store_billing(self.runtime)
        self.providers = {p: FakeStore() for p in ('apple', 'google')}
        self.store.clients = dict(self.providers)

    def context(self, provider='apple'):
        response = self.client.get('/api/plans/store-context?provider=' + provider)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def post(self, path, body, csrf=True):
        token = self.context(body.get('provider', 'apple'))['csrf_token'] if csrf else ''
        return self.client.post(path, json=body, headers={'X-CSRF-Token': token})

    def evidence(self, provider='apple', code='normal', reference='1000000000000001', **changes):
        context = self.context(provider)
        proof = StoreEvidence(provider, reference if provider == 'apple' else google_key(reference),
            context['account_token'], 'fixture_' + code, 'monthly' if provider == 'google' else '', 'test', 'active',
            int(time.time()) + 86400, False, acknowledge=provider == 'google')
        self.providers[provider].evidence[reference] = replace(proof, **changes)
        return reference

    def verify(self, provider='apple', reference='1000000000000001', **extra):
        return self.post('/api/plans/store-verify', dict(provider=provider,
            **({'transaction_id': reference} if provider == 'apple' else {'purchase_token': reference}), **extra))

    def intent(self, provider='apple', **extra):
        return self.post('/api/plans/store-intent', dict(provider=provider, product_id='fixture_normal',
            **({'base_plan_id': 'monthly'} if provider == 'google' else {}), **extra))

    def notification(self, provider, reference, event_id='fixture_event', signature=True):
        authorization = hmac.new(b'fixture-notification', reference.encode(), hashlib.sha256).hexdigest() if signature else ''
        return self.client.post('/billing/' + provider + '/notifications', json={'reference': reference, 'event_id': event_id},
                                headers={'Authorization': authorization})

    def test_disabled_configuration_does_not_offer_or_initialize_store(self):
        with patch.dict(os.environ, {'FEATURE_APPLE_BILLING_ENABLED': '0'}):
            self.store.clients = {}
            with patch('feature_store_billing.AppleVerifier', side_effect=AssertionError('must not load key')):
                value = self.context()
                self.assertFalse(value['available']); self.assertIsNone(value['account_token'])
                self.assertEqual(value['products'], [])
                self.assertEqual(self.verify().status_code, 503)

    def test_existing_contract_hides_new_purchase_and_manages_only_purchase_store(self):
        self.evidence(code='business'); self.assertEqual(self.verify().status_code, 200)
        apple = self.context('apple')
        self.assertFalse(apple['purchases_available'])
        management = apple['contract_management']
        self.assertTrue(management['has_contract']); self.assertTrue(management['can_manage_here'])
        self.assertTrue(management['can_restore_here']); self.assertFalse(management['can_purchase'])
        self.assertEqual(management['contracts'][0]['provider_name'], 'App Store')
        self.assertIn('日本時間', management['contracts'][0]['next_billing'])
        google = self.context('google')['contract_management']
        self.assertFalse(google['can_manage_here']); self.assertFalse(google['can_restore_here'])
        self.assertIn('iPhone', ' '.join(google['instructions']))
        self.assertEqual(self.intent('google').status_code, 409)

    def test_web_and_legacy_contracts_give_web_guidance_without_store_actions(self):
        with self.service.db(write=True) as cur:
            cur.execute('INSERT INTO feature_subscriptions VALUES (?,?,?,?,?,?,?,?)',
                ('stripe', 'private-subscription-id', 1, 'normal', 'active', int(time.time()) + 86400, 0, int(time.time())))
        result = self.context()
        self.assertFalse(result['purchases_available'])
        management = result['contract_management']
        self.assertFalse(management['can_manage_here']); self.assertFalse(management['can_restore_here'])
        self.assertIn('ブラウザー', ' '.join(management['instructions']))
        self.assertNotIn('private-subscription-id', json.dumps(management))
        self.login(2)
        self.assertFalse(self.context()['contract_management']['has_contract'])
        with self.service.db(write=True) as cur:
            cur.execute('UPDATE users SET stripe_subscription_id=? WHERE id=2', ('legacy-private',))
        management = self.context()['contract_management']
        self.assertTrue(management['legacy_contract']); self.assertFalse(management['can_purchase'])
        self.assertIn('以前からの在庫料金', ' '.join(management['instructions']))
        self.assertNotIn('legacy-private', json.dumps(management))

    def test_cancel_reserved_contract_retains_end_date_but_does_not_claim_next_charge(self):
        self.evidence(cancel_at_period_end=True); self.assertEqual(self.verify().status_code, 200)
        contract = self.context()['contract_management']['contracts'][0]
        self.assertTrue(contract['active']); self.assertIsNone(contract['next_billing'])
        self.assertIn('日本時間', contract['access_until'])
        self.evidence(status='canceled', cancel_at_period_end=False); self.assertEqual(self.verify().status_code, 200)
        contract = self.context()['contract_management']['contracts'][0]
        self.assertFalse(contract['active']); self.assertIsNone(contract['access_until'])
        self.assertIsNone(contract['next_billing'])

    def test_management_remains_private_and_available_when_store_configuration_missing(self):
        self.evidence(); self.assertEqual(self.verify().status_code, 200)
        with patch.dict(os.environ, {'FEATURE_APPLE_BILLING_ENABLED': '0'}):
            with patch('feature_store_billing.AppleVerifier', side_effect=AssertionError('must not load SDK')):
                response = self.client.get('/api/plans/store-context?provider=apple')
                self.assertEqual(response.status_code, 200)
                self.assertIn('no-store', response.headers['Cache-Control'])
                value = response.get_json(); self.assertFalse(value['available'])
                self.assertTrue(value['contract_management']['has_contract'])
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/api/plans/store-context?provider=apple').status_code, 401)

    def test_account_context_is_private_stable_and_bound_to_signed_in_user(self):
        first = self.context(); self.assertTrue(first['available'])
        self.assertEqual(self.context()['account_token'], first['account_token'])
        self.assertEqual(self.context('google')['account_token'], first['account_token'])
        self.login(2)
        self.assertNotEqual(self.context()['account_token'], first['account_token'])
        with self.client.session_transaction() as session: session.clear()
        self.assertEqual(self.client.get('/api/plans/store-context?provider=apple').status_code, 401)

    def test_csrf_and_unrecognized_provider_are_rejected(self):
        self.assertEqual(self.post('/api/plans/store-intent', {'provider': 'apple'}, csrf=False).status_code, 400)
        self.assertEqual(self.post('/api/plans/store-verify', {'provider': 'apple'}, csrf=False).status_code, 400)
        self.assertEqual(self.client.get('/api/plans/store-context?provider=other').status_code, 400)
        self.assertEqual(self.client.post('/billing/other/notifications', json={}).status_code, 503)

    def test_product_mapping_cannot_be_reassigned(self):
        self.context()
        products = json.loads(os.environ['FEATURE_STORE_PRODUCTS'])
        products['apple'][0]['plan_code'], products['apple'][1]['plan_code'] = 'business', 'normal'
        with patch.dict(os.environ, {'FEATURE_STORE_PRODUCTS': json.dumps(products)}):
            self.assertEqual(self.client.get('/api/plans/store-context?provider=apple').status_code, 400)

    def test_verified_purchase_grants_shared_entitlement_and_repeat_is_idempotent(self):
        self.evidence(code='business')
        for _ in range(2):
            result = self.verify(); self.assertEqual(result.status_code, 200)
            self.assertTrue(result.get_json()['finish_transaction'])
        self.assertEqual(self.state()['features'], ['inventory_manage', 'dealer_sales'])
        with self.service.db() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM feature_subscriptions WHERE provider='apple'")
            self.assertEqual(self.service.one(cur)['n'], 1)

    def test_client_plan_price_status_claims_cannot_grant_access(self):
        self.evidence()
        response = self.verify(plan_code='business', status='active', period_end=9999999999)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        self.assertEqual(self.verify(reference='999999999999').status_code, 400)

    def test_purchase_token_cannot_be_claimed_by_another_user(self):
        self.evidence()
        self.assertEqual(self.verify().status_code, 200)
        self.login(2)
        self.assertEqual(self.verify().status_code, 400)
        self.assertEqual(self.state()['features'], [])

    def test_wrong_app_environment_product_and_account_fail_closed(self):
        for changed in ({'environment': 'production'}, {'product_id': 'fixture_unknown'}, {'account_token': 'someone_else'}, {'provider': 'google'}):
            self.evidence(**changed)
            self.assertEqual(self.verify().status_code, 400)
            self.assertEqual(self.state()['features'], [])

    def test_test_entitlements_do_not_survive_switch_to_production_mode(self):
        self.evidence(); self.assertEqual(self.verify().status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        with patch.dict(os.environ, {'FEATURE_STORE_ENVIRONMENT': 'production'}):
            self.assertEqual(self.state()['features'], [])

    def test_pending_unpaid_expired_paused_and_refunded_do_not_unlock(self):
        for status in ('incomplete', 'past_due', 'expired', 'paused', 'canceled'):
            self.evidence(status=status, finish_transaction=status != 'incomplete')
            result = self.verify(); self.assertEqual(result.status_code, 200)
            self.assertEqual(result.get_json()['finish_transaction'], status != 'incomplete')
            self.assertEqual(self.state()['features'], [])
        self.evidence(period_end=int(time.time()) - 1)
        self.assertEqual(self.verify().status_code, 200)
        self.assertEqual(self.state()['features'], [])

    def test_renewal_cancellation_refund_and_reversed_event_order_use_current_state(self):
        reference = self.evidence(code='business', cancel_at_period_end=True)
        self.assertEqual(self.notification('apple', reference).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'business')
        self.evidence(code='normal')  # Store has completed a scheduled downgrade.
        self.assertEqual(self.notification('apple', reference, 'older_notification').status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        self.evidence(status='canceled')
        self.assertEqual(self.notification('apple', reference, 'refunded_notification').status_code, 200)
        self.assertEqual(self.state()['features'], [])

    def test_notification_requires_signature_and_failure_is_retryable(self):
        reference = self.evidence()
        self.assertEqual(self.notification('apple', reference, signature=False).status_code, 400)
        self.assertEqual(self.state()['features'], [])
        self.providers['apple'].fail_once = True
        with self.assertLogs(self.app.logger.name, level='ERROR'):
            self.assertEqual(self.notification('apple', reference).status_code, 503)
        self.assertEqual(self.notification('apple', reference).status_code, 200)
        self.assertEqual(self.notification('apple', reference).status_code, 200)
        with self.service.db() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM feature_billing_events WHERE event_id LIKE 'store:%'")
            self.assertEqual(self.service.one(cur)['n'], 1)

    def test_google_acknowledges_after_persistence_and_does_not_store_raw_token(self):
        reference = self.evidence('google', reference='fixture-secret-purchase-token', acknowledge=True)
        provider = self.providers['google']
        def acknowledge(token, evidence):
            with self.db() as conn:
                self.assertEqual(conn.execute("SELECT status FROM feature_subscriptions WHERE provider='google'").fetchone()[0], 'active')
            provider.acknowledged.append(token)
        provider.acknowledge = acknowledge
        self.assertEqual(self.verify('google', reference).status_code, 200)
        self.assertEqual(provider.acknowledged, [reference])
        with self.db() as conn:
            self.assertNotIn(reference, '\n'.join(conn.iterdump()))

    def test_google_ack_failure_keeps_durable_entitlement_and_retries(self):
        reference = self.evidence('google', reference='fixture-purchase-token', acknowledge=True)
        with patch.object(self.providers['google'], 'acknowledge', side_effect=RuntimeError('temporary')):
            with self.assertLogs(self.app.logger.name, level='ERROR'):
                self.assertEqual(self.verify('google', reference).status_code, 503)
        self.assertEqual(self.state()['plan_code'], 'normal')
        self.assertEqual(self.verify('google', reference).status_code, 200)

    def test_google_replacement_revokes_previous_token_and_rejects_other_owner(self):
        old = self.evidence('google', code='business', reference='fixture-old-token')
        self.assertEqual(self.verify('google', old).status_code, 200)
        new = self.evidence('google', reference='fixture-new-token', linked_subscription_id=google_key(old))
        self.assertEqual(self.verify('google', new).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        # An old token notification must never restore the superseded higher plan.
        self.assertEqual(self.notification('google', old, 'old_token_notification').status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        self.login(2)
        other = self.evidence('google', reference='fixture-other-token', linked_subscription_id=google_key(new))
        self.assertEqual(self.verify('google', other).status_code, 400)

    def test_intent_rechecks_existing_contract_and_blocks_parallel_checkouts(self):
        first = self.intent(); self.assertEqual(first.status_code, 200)
        self.assertEqual(self.intent('google').status_code, 409)
        with self.service.db(write=True) as cur:
            with self.assertRaises(HTTPException):
                self.service.reject_legacy_contract(cur, 1)
        self.assertEqual(self.post('/api/plans/store-intent', {'provider': 'apple', 'action': 'cancel', 'intent_id': first.get_json()['intent_id']}).status_code, 200)
        self.assertEqual(self.intent('google').status_code, 200)

    def test_expired_but_unreconciled_contract_blocks_repurchase_until_verified_expired(self):
        self.evidence(period_end=int(time.time()) - 1)
        self.assertEqual(self.verify().status_code, 200)
        self.assertEqual(self.intent().status_code, 409)
        self.evidence(status='expired', period_end=int(time.time()) - 1)
        self.assertEqual(self.verify().status_code, 200)
        self.assertEqual(self.intent().status_code, 200)

    def test_legacy_and_web_pending_checkout_block_store_intents(self):
        with self.db() as conn:
            conn.execute("UPDATE users SET stripe_subscription_id='legacy' WHERE id=1")
        self.assertEqual(self.intent().status_code, 409)
        with self.db() as conn:
            conn.execute('UPDATE users SET stripe_subscription_id=NULL WHERE id=1')
        with self.service.db(write=True) as cur:
            self.service.lock_account(cur, 1)
            cur.execute('UPDATE feature_billing_accounts SET pending_until=? WHERE user_id=1', (int(time.time()) + 3600,))
        self.assertEqual(self.intent().status_code, 409)

    def test_terms_and_privacy_configuration_require_https(self):
        for field in ('FEATURE_STORE_TERMS_URL', 'FEATURE_STORE_PRIVACY_URL'):
            for bad in ('', 'http://fixture.invalid/legal', 'javascript:alert(1)', 'https://user:password@fixture.invalid/legal'):
                with patch.dict(os.environ, {field: bad}):
                    self.assertFalse(self.context()['available'])

    def test_unrelated_or_old_purchase_does_not_clear_in_flight_store_intent(self):
        google = self.context('google')
        self.assertEqual(self.intent('google').status_code, 200)
        for provider, changed in [('apple', {'status': 'expired'}), ('google', {'purchased_at': int(time.time()) - 3600}),
                                  ('google', {'product_id': 'fixture_business', 'purchased_at': int(time.time())})]:
            reference = self.evidence(provider, reference='fixture-pending-other-proof', **changed)
            self.assertEqual(self.notification(provider, reference, provider + str(changed)).status_code, 200)
            with self.service.db() as cur:
                cur.execute('SELECT pending_provider,pending_until FROM feature_store_accounts WHERE user_id=1')
                pending = self.service.one(cur)
                self.assertEqual(pending['pending_provider'], 'google')
                self.assertGreater(pending['pending_until'], int(time.time()))
        reference = self.evidence('google', reference='fixture-pending-current-proof', purchased_at=int(time.time()))
        self.assertEqual(self.verify('google', reference).status_code, 200)
        with self.service.db() as cur:
            cur.execute('SELECT pending_until FROM feature_store_accounts WHERE user_id=1')
            self.assertEqual(self.service.one(cur)['pending_until'], 0)

    def test_verified_store_grace_period_keeps_access_only_until_store_deadline(self):
        self.evidence(status='grace_period')
        self.assertEqual(self.verify().status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')
        self.evidence(status='grace_period', period_end=int(time.time()) - 1)
        self.assertEqual(self.verify().status_code, 200)
        self.assertEqual(self.state()['features'], [])

    def test_google_full_refund_revokes_current_order_and_old_refund_cannot_revoke_new_renewal(self):
        reference = self.evidence('google', reference='fixture-refund-token', order_id='fixture-order-1')
        self.assertEqual(self.verify('google', reference).status_code, 200)
        with patch.object(self.providers['google'], 'notification', return_value=StoreNotification('refund-1', reference, 'fixture-order-1')):
            self.assertEqual(self.notification('google', reference).status_code, 200)
        self.assertEqual(self.state()['features'], [])
        self.assertEqual(self.verify('google', reference).status_code, 200)
        self.assertEqual(self.state()['features'], [])
        self.evidence('google', reference=reference, order_id='fixture-order-2')
        self.assertEqual(self.verify('google', reference).status_code, 200)
        with patch.object(self.providers['google'], 'notification', return_value=StoreNotification('old-refund-retry', reference, 'fixture-order-1')):
            self.assertEqual(self.notification('google', reference).status_code, 200)
        self.assertEqual(self.state()['plan_code'], 'normal')

    def test_sandbox_contract_and_intent_do_not_block_new_production_purchase(self):
        self.evidence(); self.assertEqual(self.verify().status_code, 200)
        with patch.dict(os.environ, {'FEATURE_STORE_ENVIRONMENT': 'production', 'FEATURE_APPLE_APP_ID': '12345'}):
            self.assertEqual(self.state()['features'], [])
            self.assertEqual(self.intent().status_code, 200)

    def test_pausing_new_purchases_keeps_restore_and_notifications_available(self):
        reference = self.evidence()
        with patch.dict(os.environ, {'FEATURE_STORE_NEW_PURCHASES_ENABLED': '0'}):
            context = self.context()
            self.assertTrue(context['available']); self.assertFalse(context['purchases_available'])
            self.assertEqual(self.intent().status_code, 503)
            self.assertEqual(self.verify().status_code, 200)
            self.assertEqual(self.notification('apple', reference).status_code, 200)
        with patch.dict(os.environ, {'FEATURE_STORE_TERMS_URL': ''}):
            self.assertFalse(self.context()['available'])
            self.assertEqual(self.notification('apple', reference).status_code, 200)


from werkzeug.exceptions import HTTPException


class StoreAdapterTest(unittest.TestCase):
    def apple(self):
        adapter = AppleVerifier.__new__(AppleVerifier)
        adapter.environment, adapter.bundle, adapter.group = 'test', 'fixture.apple', 'fixture-group'
        self.txn = dict(originalTransactionId='1000000000000001', transactionId='1000000000000002',
            bundleId='fixture.apple', subscriptionGroupIdentifier='fixture-group', type='Auto-Renewable Subscription',
            quantity=1, environment='Sandbox', inAppOwnershipType='PURCHASED', productId='fixture_normal',
            appAccountToken='fixture-account', expiresDate=(int(time.time()) + 3600) * 1000)
        self.item = dict(originalTransactionId='1000000000000001', signedTransactionInfo='fixture-current-jws',
                         signedRenewalInfo='fixture-renewal-jws', status=1)
        adapter.verifier = SimpleNamespace(verify_and_decode_signed_transaction=lambda signed: self.txn,
            verify_and_decode_renewal_info=lambda signed: dict(originalTransactionId='1000000000000001', autoRenewStatus=0))
        adapter.client = SimpleNamespace(get_transaction_info=lambda ref: SimpleNamespace(signedTransactionInfo='fixture-initial-jws'),
            get_all_subscription_statuses=lambda ref: dict(data=[dict(lastTransactions=[self.item])]))
        return adapter

    def google(self):
        adapter = GoogleVerifier.__new__(GoogleVerifier)
        adapter.environment, adapter.package = 'test', 'fixture.google'
        self.purchase = {'testPurchase': {}, 'subscriptionState': 'SUBSCRIPTION_STATE_ACTIVE',
            'acknowledgementState': 'ACKNOWLEDGEMENT_STATE_PENDING', 'externalAccountIdentifiers': {'obfuscatedExternalAccountId': 'fixture-account'},
            'lineItems': [{'productId': 'fixture_normal', 'offerDetails': {'basePlanId': 'monthly'},
                          'autoRenewingPlan': {'autoRenewEnabled': True},
                          'expiryTime': datetime.fromtimestamp(time.time() + 3600, timezone.utc).isoformat()}]}
        adapter.client = MagicMock()
        adapter.client.purchases().subscriptionsv2().get().execute.side_effect = lambda: self.purchase
        return adapter

    def test_apple_validates_scope_and_revocation_from_latest_verified_transaction(self):
        adapter = self.apple()
        result = adapter.verify('1000000000000002')
        self.assertEqual(result.status, 'active'); self.assertTrue(result.cancel_at_period_end)
        self.txn['revocationDate'] = int(time.time()) * 1000
        self.assertEqual(adapter.verify('1000000000000002').status, 'canceled')
        self.txn['bundleId'] = 'foreign.app'
        with self.assertRaises(VerificationError): adapter.verify('1000000000000002')

    def test_apple_rejects_test_production_family_sharing_quantity_type_and_group_mismatch(self):
        for field, invalid in [('environment', 'Production'), ('inAppOwnershipType', 'FAMILY_SHARED'), ('quantity', 2),
                               ('type', 'Consumable'), ('subscriptionGroupIdentifier', 'other'), ('isUpgraded', True)]:
            adapter = self.apple(); self.txn[field] = invalid
            with self.subTest(field=field), self.assertRaises(VerificationError): adapter.verify('1000000000000002')

    def test_apple_does_not_accept_unsigned_receipt_or_grace_retry_as_paid(self):
        adapter = self.apple()
        from appstoreserverlibrary.signed_data_verifier import VerificationException, VerificationStatus
        adapter.verifier.verify_and_decode_signed_transaction = MagicMock(side_effect=VerificationException(VerificationStatus.VERIFICATION_FAILURE))
        with self.assertRaises(VerificationError): adapter.verify('1000000000000002')
        for status in (2, 3, 4, 5):
            adapter = self.apple(); self.item['status'] = status
            self.assertNotEqual(adapter.verify('1000000000000002').status, 'active')

    def test_apple_grace_uses_only_signed_renewal_deadline_and_stops_at_expiry(self):
        adapter = self.apple(); self.item['status'] = 4
        self.txn['expiresDate'] = (int(time.time()) - 1) * 1000
        renewal = dict(originalTransactionId='1000000000000001', autoRenewStatus=1,
                       gracePeriodExpiresDate=(int(time.time()) + 1800) * 1000)
        adapter.verifier.verify_and_decode_renewal_info = lambda signed: renewal
        result = adapter.verify('1000000000000002')
        self.assertEqual(result.status, 'grace_period')
        self.assertEqual(result.period_end, renewal['gracePeriodExpiresDate'] // 1000)
        renewal['gracePeriodExpiresDate'] = (int(time.time()) - 1) * 1000
        self.assertEqual(adapter.verify('1000000000000002').status, 'past_due')

    def test_google_validates_environment_and_current_status_and_ack_requirement(self):
        adapter = self.google(); result = adapter.verify('fixture-google-token')
        self.assertEqual(result.status, 'active'); self.assertTrue(result.acknowledge)
        self.purchase['subscriptionState'] = 'SUBSCRIPTION_STATE_CANCELED'
        self.assertEqual(adapter.verify('fixture-google-token').status, 'active')
        self.purchase['canceledStateContext'] = {'replacementCancellation': {}}
        self.assertEqual(adapter.verify('fixture-google-token').status, 'canceled')
        self.purchase.pop('canceledStateContext')
        for state in ('PENDING', 'EXPIRED', 'ON_HOLD', 'PAUSED', 'IN_GRACE_PERIOD'):
            self.purchase['subscriptionState'] = 'SUBSCRIPTION_STATE_' + state
            self.assertNotEqual(adapter.verify('fixture-google-token').status, 'active')
            if state == 'IN_GRACE_PERIOD': self.assertEqual(adapter.verify('fixture-google-token').status, 'grace_period')
        self.purchase.pop('testPurchase')
        with self.assertRaises(VerificationError): adapter.verify('fixture-google-token')

    def test_google_rejects_multiple_prepaid_or_missing_auto_renewing_items(self):
        for kind in ('multiple', 'prepaid', 'no_plan'):
            adapter = self.google()
            if kind == 'multiple': self.purchase['lineItems'] *= 2
            elif kind == 'prepaid': self.purchase['lineItems'][0]['prepaidPlan'] = {'allowExtendAfterTime': 'fixture'}
            else: self.purchase['lineItems'][0].pop('autoRenewingPlan')
            with self.subTest(kind=kind), self.assertRaises(VerificationError): adapter.verify('fixture-google-token')

    def test_google_notification_checks_jwt_email_audience_subscription_and_package(self):
        adapter = self.google()
        env = {'FEATURE_GOOGLE_PUBSUB_AUDIENCE': 'https://fixture.invalid/push', 'FEATURE_GOOGLE_PUBSUB_EMAIL': 'fixture@example.invalid',
               'FEATURE_GOOGLE_PUBSUB_SUBSCRIPTION': 'projects/fixture/subscriptions/fixture'}
        notification = {'packageName': 'fixture.google', 'subscriptionNotification': {'purchaseToken': 'fixture-token'}}
        body = {'subscription': env['FEATURE_GOOGLE_PUBSUB_SUBSCRIPTION'], 'message': {'messageId': 'message-1',
                'data': base64.b64encode(json.dumps(notification).encode()).decode()}}
        claims = {'email': env['FEATURE_GOOGLE_PUBSUB_EMAIL'], 'email_verified': True}
        with patch.dict(os.environ, env), patch('google.oauth2.id_token.verify_oauth2_token', return_value=claims) as verify:
            self.assertEqual(adapter.notification(body, 'Bearer fixture-jwt'), StoreNotification('message-1', 'fixture-token'))
            self.assertEqual(verify.call_args.args[2], env['FEATURE_GOOGLE_PUBSUB_AUDIENCE'])
            with self.assertRaises(VerificationError): adapter.notification(body, '')
            claims['email'] = 'foreign@example.invalid'
            with self.assertRaises(VerificationError): adapter.notification(body, 'Bearer fixture-jwt')
            claims['email'] = env['FEATURE_GOOGLE_PUBSUB_EMAIL']; claims['email_verified'] = False
            with self.assertRaises(VerificationError): adapter.notification(body, 'Bearer fixture-jwt')
            claims['email_verified'] = True; body['subscription'] = 'foreign-subscription'
            with self.assertRaises(VerificationError): adapter.notification(body, 'Bearer fixture-jwt')
            body['subscription'] = env['FEATURE_GOOGLE_PUBSUB_SUBSCRIPTION']
            notification['packageName'] = 'foreign.package'
            body['message']['data'] = base64.b64encode(json.dumps(notification).encode()).decode()
            with self.assertRaises(VerificationError): adapter.notification(body, 'Bearer fixture-jwt')


if __name__ == '__main__':
    unittest.main()
