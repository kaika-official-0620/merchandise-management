"""StoreKit / Play Billing verification, using the existing website session.

Disabled until explicit store configuration is provided. No store clients, keys,
database, or network are accessed at import/registration time. Raw Google purchase
tokens and signed Apple transactions are never stored or logged by this module.
"""
import base64
from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
import uuid
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, g, jsonify, request
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException

from feature_plans import PLAN_FEATURES


class VerificationError(ValueError):
    """Malformed or mismatched store evidence. Messages must not contain tokens."""


def public_https_url(url):
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password and
                    parsed.port in (None, 443) and not any(ord(c) < 33 for c in url))
    except (TypeError, ValueError):
        return False


def value(obj, field, default=None):
    result = obj.get(field, default) if isinstance(obj, dict) else getattr(obj, field, default)
    return getattr(result, 'value', result)


def google_key(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class StoreEvidence:
    provider: str
    subscription_id: str
    account_token: str
    product_id: str
    base_plan_id: str
    environment: str
    status: str
    period_end: int
    cancel_at_period_end: bool
    finish_transaction: bool = True
    acknowledge: bool = False
    linked_subscription_id: str = ''
    purchased_at: int = 0
    acknowledged: bool = False
    order_id: str = ''


@dataclass(frozen=True)
class StoreNotification:
    event_id: str
    reference: str | None = None
    voided_order_id: str | None = None


class AppleVerifier:
    def __init__(self):
        from appstoreserverlibrary.api_client import AppStoreServerAPIClient
        from appstoreserverlibrary.models.Environment import Environment
        from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier
        self.environment = os.environ['FEATURE_STORE_ENVIRONMENT']
        self.bundle = os.environ['FEATURE_APPLE_BUNDLE_ID']
        self.group = os.environ['FEATURE_APPLE_SUBSCRIPTION_GROUP_ID']
        environment = Environment.PRODUCTION if self.environment == 'production' else Environment.SANDBOX
        roots = [Path(path).read_bytes() for path in json.loads(os.environ['FEATURE_APPLE_ROOT_CERTIFICATES'])]
        self.verifier = SignedDataVerifier(roots, True, environment, self.bundle,
            int(os.environ['FEATURE_APPLE_APP_ID']) if self.environment == 'production' else None)
        self.client = AppStoreServerAPIClient(Path(os.environ['FEATURE_APPLE_PRIVATE_KEY_PATH']).read_bytes(),
            os.environ['FEATURE_APPLE_KEY_ID'], os.environ['FEATURE_APPLE_ISSUER_ID'], self.bundle, environment)

    def decode(self, method, signed):
        from appstoreserverlibrary.signed_data_verifier import VerificationException
        try:
            return getattr(self.verifier, method)(signed)
        except VerificationException as error:
            raise VerificationError('invalid_apple_signature') from error

    def verify(self, reference):
        if not isinstance(reference, str) or not re.fullmatch(r'[0-9]{5,80}', reference):
            raise VerificationError('invalid_transaction')
        initial = self.decode('verify_and_decode_signed_transaction', self.client.get_transaction_info(reference).signedTransactionInfo)
        original = value(initial, 'originalTransactionId')
        if not original:
            raise VerificationError('missing_original_transaction')
        response = self.client.get_all_subscription_statuses(original)
        candidates = []
        for group in value(response, 'data', []) or []:
            for item in value(group, 'lastTransactions', []) or []:
                if value(item, 'originalTransactionId') != original:
                    continue
                txn = self.decode('verify_and_decode_signed_transaction', value(item, 'signedTransactionInfo'))
                renewal = self.decode('verify_and_decode_renewal_info', value(item, 'signedRenewalInfo'))
                if not (value(txn, 'originalTransactionId') == original and value(renewal, 'originalTransactionId') == original and
                        value(txn, 'bundleId') == self.bundle and value(txn, 'subscriptionGroupIdentifier') == self.group and
                        value(txn, 'type') == 'Auto-Renewable Subscription' and value(txn, 'quantity') == 1 and
                        value(txn, 'environment') == ('Production' if self.environment == 'production' else 'Sandbox') and
                        value(txn, 'inAppOwnershipType') == 'PURCHASED' and not value(txn, 'isUpgraded')):
                    raise VerificationError('transaction_scope_mismatch')
                end = int(value(txn, 'expiresDate', 0) or 0) // 1000
                status = 'active' if value(item, 'status') == 1 and end > int(time.time()) else 'expired'
                if value(item, 'status') in (3, 4):
                    status = 'past_due'
                if value(item, 'status') == 4:
                    grace_end = int(value(renewal, 'gracePeriodExpiresDate', 0) or 0) // 1000
                    if grace_end > int(time.time()):
                        status, end = 'grace_period', grace_end
                if value(txn, 'revocationDate') is not None or value(item, 'status') == 5:
                    status = 'canceled'
                candidates.append((int(value(txn, 'signedDate', 0) or 0), StoreEvidence(
                    'apple', original, str(value(txn, 'appAccountToken', '') or '').lower(),
                    value(txn, 'productId', ''), '', self.environment, status, end,
                    value(renewal, 'autoRenewStatus') == 0, purchased_at=int(value(txn, 'purchaseDate', 0) or 0) // 1000)))
        if len(candidates) != 1:
            raise VerificationError('ambiguous_subscription')
        return candidates[0][1]

    def notification(self, body, authorization):
        signed = body.get('signedPayload')
        if not isinstance(signed, str) or len(signed) > 512 * 1024:
            raise VerificationError('invalid_notification')
        notification = self.decode('verify_and_decode_notification', signed)
        event_id = str(value(notification, 'notificationUUID', '') or '')
        if not event_id:
            raise VerificationError('missing_event_id')
        if value(notification, 'notificationType') == 'TEST':
            return StoreNotification(event_id)
        data = value(notification, 'data')
        if not data or not value(data, 'signedTransactionInfo'):
            return StoreNotification(event_id)
        txn = self.decode('verify_and_decode_signed_transaction', value(data, 'signedTransactionInfo'))
        return StoreNotification(event_id, value(txn, 'transactionId'))

    def acknowledge(self, reference, evidence):
        return None  # StoreKit finish is performed on the device after persistence.


class GoogleVerifier:
    def __init__(self):
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        self.environment = os.environ['FEATURE_STORE_ENVIRONMENT']
        self.package = os.environ['FEATURE_GOOGLE_PACKAGE_NAME']
        credentials = service_account.Credentials.from_service_account_file(
            os.environ['FEATURE_GOOGLE_SERVICE_ACCOUNT_PATH'], scopes=['https://www.googleapis.com/auth/androidpublisher'])
        self.client = build('androidpublisher', 'v3', credentials=credentials, cache_discovery=False)

    def verify(self, reference):
        if not isinstance(reference, str) or not 10 <= len(reference) <= 8192 or any(ord(c) < 33 for c in reference):
            raise VerificationError('invalid_purchase_token')
        purchase = self.client.purchases().subscriptionsv2().get(packageName=self.package, token=reference).execute()
        environment = 'test' if purchase.get('testPurchase') is not None else 'production'
        if environment != self.environment:
            raise VerificationError('environment_mismatch')
        items = purchase.get('lineItems') or []
        if len(items) != 1 or not items[0].get('autoRenewingPlan') or items[0].get('prepaidPlan'):
            raise VerificationError('unsupported_subscription_items')
        item = items[0]
        end = int(datetime.fromisoformat(item.get('expiryTime', '').replace('Z', '+00:00')).timestamp())
        state = purchase.get('subscriptionState')
        active = state in ('SUBSCRIPTION_STATE_ACTIVE', 'SUBSCRIPTION_STATE_CANCELED') and end > int(time.time())
        status = 'active' if active else 'expired'
        if state in ('SUBSCRIPTION_STATE_PENDING', 'SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED'):
            status = 'incomplete'
        elif state in ('SUBSCRIPTION_STATE_IN_GRACE_PERIOD', 'SUBSCRIPTION_STATE_ON_HOLD'):
            status = 'past_due'
            if state == 'SUBSCRIPTION_STATE_IN_GRACE_PERIOD' and end > int(time.time()):
                status = 'grace_period'
        elif state == 'SUBSCRIPTION_STATE_PAUSED':
            status = 'paused'
        if (purchase.get('canceledStateContext') or {}).get('replacementCancellation') is not None:
            status = 'canceled'
        if status in ('active', 'grace_period') and purchase.get('acknowledgementState') not in ('ACKNOWLEDGEMENT_STATE_PENDING', 'ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED'):
            raise VerificationError('unknown_acknowledgement_state')
        account = (purchase.get('externalAccountIdentifiers') or {}).get('obfuscatedExternalAccountId', '')
        linked = purchase.get('linkedPurchaseToken')
        return StoreEvidence('google', google_key(reference), account, item.get('productId', ''),
            (item.get('offerDetails') or {}).get('basePlanId', ''), environment, status, end,
            not item['autoRenewingPlan'].get('autoRenewEnabled'), status != 'incomplete',
            status in ('active', 'grace_period') and purchase.get('acknowledgementState') == 'ACKNOWLEDGEMENT_STATE_PENDING',
            google_key(linked) if linked else '',
            int(datetime.fromisoformat(purchase['startTime'].replace('Z', '+00:00')).timestamp()) if purchase.get('startTime') else 0,
            purchase.get('acknowledgementState') == 'ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED',
            item.get('latestSuccessfulOrderId') or purchase.get('latestOrderId') or '')

    def acknowledge(self, reference, evidence):
        if evidence.acknowledge:
            self.client.purchases().subscriptions().acknowledge(packageName=self.package,
                subscriptionId=evidence.product_id, token=reference, body={}).execute()

    def notification(self, body, authorization):
        from google.auth.transport.requests import Request
        from google.oauth2.id_token import verify_oauth2_token
        if not authorization.startswith('Bearer ') or len(authorization) > 16384:
            raise VerificationError('missing_push_identity')
        try:
            claims = verify_oauth2_token(authorization[7:], Request(), os.environ['FEATURE_GOOGLE_PUBSUB_AUDIENCE'])
        except ValueError as error:
            raise VerificationError('invalid_push_identity') from error
        if claims.get('email_verified') is not True or claims.get('email') != os.environ['FEATURE_GOOGLE_PUBSUB_EMAIL']:
            raise VerificationError('push_identity_mismatch')
        if body.get('subscription') != os.environ['FEATURE_GOOGLE_PUBSUB_SUBSCRIPTION']:
            raise VerificationError('push_subscription_mismatch')
        message = body.get('message') or {}
        event_id = str(message.get('messageId') or '')
        try:
            notification = json.loads(base64.b64decode(message.get('data', ''), validate=True))
        except (ValueError, TypeError) as error:
            raise VerificationError('invalid_push_payload') from error
        if not event_id or not isinstance(notification, dict) or notification.get('packageName') != self.package:
            raise VerificationError('push_package_mismatch')
        voided = notification.get('voidedPurchaseNotification') or {}
        if voided and voided.get('productType') != 1:
            return StoreNotification(event_id)
        details = notification.get('subscriptionNotification') or voided
        order_id = voided.get('orderId') if voided.get('refundType') == 1 else None
        if order_id is not None and (not isinstance(order_id, str) or not 1 <= len(order_id) <= 200):
            raise VerificationError('invalid_voided_order')
        return StoreNotification(event_id, details.get('purchaseToken'), order_id)


class StoreBilling:
    def __init__(self, runtime, plans):
        self.runtime, self.plans = runtime, plans
        self.clients = {}

    def schema(self, cur):
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_store_accounts (
            user_id INTEGER PRIMARY KEY, account_token TEXT NOT NULL UNIQUE,
            pending_provider TEXT, pending_until BIGINT NOT NULL DEFAULT 0, pending_key TEXT,
            pending_product TEXT, pending_base_plan TEXT, pending_started BIGINT NOT NULL DEFAULT 0,
            pending_environment TEXT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_store_products (
            provider TEXT NOT NULL, product_id TEXT NOT NULL, base_plan_id TEXT NOT NULL,
            plan_code TEXT NOT NULL, PRIMARY KEY (provider, product_id, base_plan_id))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_store_receipts (
            provider TEXT NOT NULL, subscription_id TEXT NOT NULL, account_token TEXT NOT NULL,
            product_id TEXT NOT NULL, base_plan_id TEXT NOT NULL, environment TEXT NOT NULL,
            PRIMARY KEY (provider, subscription_id))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_store_replacements (
            provider TEXT NOT NULL, old_subscription_id TEXT NOT NULL,
            new_subscription_id TEXT NOT NULL, user_id INTEGER NOT NULL,
            PRIMARY KEY (provider, old_subscription_id))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_store_refunds (
            provider TEXT NOT NULL, subscription_id TEXT NOT NULL, order_id TEXT NOT NULL,
            received_at BIGINT NOT NULL, PRIMARY KEY (provider,subscription_id,order_id))''')

    def products(self, provider):
        try:
            products = json.loads(os.environ.get('FEATURE_STORE_PRODUCTS', '{}')).get(provider, [])
            if not isinstance(products, list) or len(products) != 2:
                return []
            codes, keys = set(), set()
            for product in products:
                if not isinstance(product, dict) or product.get('plan_code') not in PLAN_FEATURES:
                    return []
                product_id, base = product.get('product_id', ''), product.get('base_plan_id', '')
                if not isinstance(product_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,150}', product_id):
                    return []
                if provider == 'google' and (not isinstance(base, str) or not re.fullmatch(r'[a-z0-9-]{1,63}', base)):
                    return []
                if provider == 'apple' and base:
                    return []
                codes.add(product['plan_code'])
                keys.add((product_id, base))
            return products if codes == set(PLAN_FEATURES) and len(keys) == 2 else []
        except (ValueError, TypeError, AttributeError):
            return []

    def configured(self, provider):
        if provider not in ('apple', 'google') or not self.plans.enabled or os.environ.get('FEATURE_' + provider.upper() + '_BILLING_ENABLED') != '1':
            return False
        if os.environ.get('FEATURE_STORE_ENVIRONMENT') not in ('production', 'test') or not self.products(provider):
            return False
        fields = ('FEATURE_APPLE_BUNDLE_ID', 'FEATURE_APPLE_SUBSCRIPTION_GROUP_ID', 'FEATURE_APPLE_ROOT_CERTIFICATES',
            'FEATURE_APPLE_PRIVATE_KEY_PATH', 'FEATURE_APPLE_KEY_ID', 'FEATURE_APPLE_ISSUER_ID') if provider == 'apple' else (
            'FEATURE_GOOGLE_PACKAGE_NAME', 'FEATURE_GOOGLE_SERVICE_ACCOUNT_PATH', 'FEATURE_GOOGLE_PUBSUB_AUDIENCE',
            'FEATURE_GOOGLE_PUBSUB_EMAIL', 'FEATURE_GOOGLE_PUBSUB_SUBSCRIPTION')
        if provider == 'apple' and os.environ.get('FEATURE_STORE_ENVIRONMENT') == 'production':
            fields += ('FEATURE_APPLE_APP_ID',)
        return all(os.environ.get(field) for field in fields)

    def legal_ready(self):
        return all(public_https_url(os.environ.get(field, '')) for field in ('FEATURE_STORE_TERMS_URL', 'FEATURE_STORE_PRIVACY_URL'))

    def purchases_ready(self, provider):
        return self.configured(provider) and self.legal_ready() and os.environ.get('FEATURE_STORE_NEW_PURCHASES_ENABLED', '1') != '0'

    def client(self, provider):
        if not self.configured(provider):
            abort(503, description='アプリ内購入の準備中です。')
        if provider in self.clients:
            return self.clients[provider]
        # google-api-python-client's httplib2 transport is not thread-safe.
        # Keep provider transports request-local rather than sharing across users.
        clients = getattr(g, '_kaika_store_clients', {})
        if provider not in clients:
            clients[provider] = AppleVerifier() if provider == 'apple' else GoogleVerifier()
            g._kaika_store_clients = clients
        return clients[provider]

    def account(self, cur, user_id):
        self.schema(cur)
        self.plans.execute(cur, '''INSERT INTO feature_store_accounts (user_id, account_token) VALUES (?, ?)
            ON CONFLICT (user_id) DO NOTHING''', (user_id, str(uuid.uuid4())))
        self.plans.execute(cur, 'SELECT * FROM feature_store_accounts WHERE user_id=?' + (' FOR UPDATE' if self.plans.postgres else ''), (user_id,))
        return self.plans.one(cur)

    def map_products(self, cur, provider):
        for product in self.products(provider):
            key = (provider, product['product_id'], product.get('base_plan_id', ''))
            self.plans.execute(cur, 'SELECT plan_code FROM feature_store_products WHERE provider=? AND product_id=? AND base_plan_id=?', key)
            old = self.plans.one(cur)
            if old and old['plan_code'] != product['plan_code']:
                raise VerificationError('product_mapping_changed')
            self.plans.execute(cur, '''INSERT INTO feature_store_products VALUES (?, ?, ?, ?)
                ON CONFLICT (provider, product_id, base_plan_id) DO NOTHING''', (*key, product['plan_code']))

    def process(self, provider, reference, expected_user=None, event_id=None, voided_order_id=None):
        client = self.client(provider)
        first = client.verify(reference)
        with self.plans.db(write=True) as cur:
            self.schema(cur)
            self.map_products(cur, provider)
            self.plans.execute(cur, 'SELECT user_id FROM feature_store_accounts WHERE account_token=?', (first.account_token,))
            owner = self.plans.one(cur)
            if not owner or (expected_user is not None and owner['user_id'] != expected_user):
                raise VerificationError('purchase_account_mismatch')
            # Same lock order as checkout/intents: billing account then store
            # account. Re-fetch after locking so delayed notifications cannot
            # overwrite a newer state with an earlier network response.
            self.plans.lock_account(cur, owner['user_id'])
            account = self.account(cur, owner['user_id'])
            evidence = client.verify(reference)
            if not (evidence.provider == provider and evidence.environment == os.environ.get('FEATURE_STORE_ENVIRONMENT') and
                    hmac.compare_digest(evidence.account_token, account['account_token']) and evidence.subscription_id == first.subscription_id):
                raise VerificationError('purchase_scope_changed')
            self.plans.execute(cur, 'SELECT * FROM feature_store_receipts WHERE provider=? AND subscription_id=?', (provider, evidence.subscription_id))
            receipt = self.plans.one(cur)
            if receipt and (receipt['account_token'] != account['account_token'] or receipt['environment'] != evidence.environment):
                raise VerificationError('purchase_already_bound')
            self.plans.execute(cur, 'SELECT user_id FROM feature_subscriptions WHERE provider=? AND subscription_id=?', (provider, evidence.subscription_id))
            existing = self.plans.one(cur)
            if existing and existing['user_id'] != owner['user_id']:
                raise VerificationError('subscription_already_bound')
            self.plans.execute(cur, 'SELECT plan_code FROM feature_store_products WHERE provider=? AND product_id=? AND base_plan_id=?',
                (provider, evidence.product_id, evidence.base_plan_id))
            product = self.plans.one(cur)
            if not product:
                raise VerificationError('unknown_store_product')
            if voided_order_id and provider == 'google':
                self.plans.execute(cur, '''INSERT INTO feature_store_refunds VALUES (?,?,?,?)
                    ON CONFLICT (provider,subscription_id,order_id) DO NOTHING''',
                    (provider, evidence.subscription_id, voided_order_id, int(time.time())))
            if provider == 'google' and evidence.order_id:
                self.plans.execute(cur, 'SELECT order_id FROM feature_store_refunds WHERE provider=? AND subscription_id=? AND order_id=?',
                                   (provider, evidence.subscription_id, evidence.order_id))
                if self.plans.one(cur):
                    evidence = replace(evidence, status='canceled', period_end=0, acknowledge=False)
            self.plans.execute(cur, 'SELECT user_id FROM feature_store_replacements WHERE provider=? AND old_subscription_id=?',
                               (provider, evidence.subscription_id))
            replaced = self.plans.one(cur)
            if replaced:
                if replaced['user_id'] != owner['user_id']:
                    raise VerificationError('replacement_account_mismatch')
                evidence = replace(evidence, status='canceled', period_end=0, acknowledge=False)
            if evidence.linked_subscription_id:
                if evidence.linked_subscription_id == evidence.subscription_id:
                    raise VerificationError('invalid_replacement')
                self.plans.execute(cur, 'SELECT user_id FROM feature_subscriptions WHERE provider=? AND subscription_id=?', (provider, evidence.linked_subscription_id))
                linked = self.plans.one(cur)
                if linked and linked['user_id'] != owner['user_id']:
                    raise VerificationError('replacement_account_mismatch')
                self.plans.execute(cur, '''INSERT INTO feature_store_replacements VALUES (?,?,?,?)
                    ON CONFLICT (provider,old_subscription_id) DO NOTHING''',
                    (provider, evidence.linked_subscription_id, evidence.subscription_id, owner['user_id']))
                self.plans.execute(cur, "UPDATE feature_subscriptions SET status='canceled',period_end=0 WHERE provider=? AND subscription_id=?",
                                   (provider, evidence.linked_subscription_id))
            self.plans.execute(cur, '''INSERT INTO feature_store_receipts VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, subscription_id) DO UPDATE SET product_id=excluded.product_id, base_plan_id=excluded.base_plan_id''',
                (provider, evidence.subscription_id, evidence.account_token, evidence.product_id, evidence.base_plan_id, evidence.environment))
            self.plans.execute(cur, '''INSERT INTO feature_subscriptions
                (provider,subscription_id,user_id,plan_code,status,period_end,cancel_at_period_end,verified_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (provider,subscription_id) DO UPDATE SET
                plan_code=excluded.plan_code,status=excluded.status,period_end=excluded.period_end,
                cancel_at_period_end=excluded.cancel_at_period_end,verified_at=excluded.verified_at''',
                (provider, evidence.subscription_id, owner['user_id'], product['plan_code'], evidence.status,
                 evidence.period_end, int(evidence.cancel_at_period_end), int(time.time())))
            if (evidence.provider == account['pending_provider'] and evidence.product_id == account['pending_product'] and
                    evidence.base_plan_id == account['pending_base_plan'] and evidence.status in ('active', 'grace_period') and
                    evidence.environment == account['pending_environment'] and
                    evidence.purchased_at >= account['pending_started'] and evidence.period_end > int(time.time())):
                self.plans.execute(cur, 'UPDATE feature_store_accounts SET pending_provider=NULL,pending_until=0,pending_key=NULL WHERE user_id=?', (owner['user_id'],))
        # Acknowledge after durable entitlement storage. A retry safely re-fetches
        # current state and repeats only an outstanding Google acknowledgement.
        client.acknowledge(reference, evidence)
        if evidence.provider == 'google' and evidence.acknowledge:
            evidence = replace(evidence, acknowledged=True)
        if event_id:
            with self.plans.db(write=True) as cur:
                self.plans.execute(cur, 'INSERT INTO feature_billing_events VALUES (?,?) ON CONFLICT (event_id) DO NOTHING',
                                   ('store:' + provider + ':' + event_id, int(time.time())))
        g._feature_plans_snapshot = {}
        return evidence


def register_store_billing(runtime):
    app = runtime.app
    if 'kaika_store_billing' in app.extensions:
        return app.extensions['kaika_store_billing']
    plans = app.extensions['kaika_feature_plans']
    service = StoreBilling(runtime, plans)
    app.extensions['kaika_store_billing'] = service
    if not plans.enabled:
        return service
    bp = Blueprint('store_billing', __name__)

    @bp.errorhandler(Exception)
    def error_response(error):
        if isinstance(error, HTTPException):
            return jsonify(error='request_rejected', message=error.description), error.code
        if isinstance(error, VerificationError):
            return jsonify(error='purchase_verification_failed', message='購入内容とログイン中のアカウントを確認できません。'), 400
        # Exception strings from provider SDKs can contain raw purchase tokens.
        current_app.logger.error('Store billing operation unavailable (%s)', type(error).__name__)
        return jsonify(error='store_unavailable', message='購入状況を確認できませんでした。時間をおいて復元をお試しください。'), 503

    @bp.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    def body_json():
        if request.content_length and request.content_length > 1024 * 1024:
            abort(413)
        raw = request.stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            abort(413)
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            abort(400)
        if not isinstance(body, dict):
            abort(400)
        return body

    @bp.get('/api/plans/store-context')
    @login_required
    def context():
        provider = request.args.get('provider', '')
        if provider not in ('apple', 'google'):
            abort(400)
        management = plans.contract_management(device=provider)
        if not service.configured(provider) or not service.legal_ready():
            return jsonify(available=False, provider=provider, products=[], account_token=None, csrf_token=plans.csrf_token(),
                           purchases_available=False, contract_management=management)
        service.client(provider)  # Validate installed SDK / key files before offering purchase UI.
        with plans.db(write=True) as cur:
            plans.lock_account(cur, int(current_user.id))
            account = service.account(cur, int(current_user.id))
            service.map_products(cur, provider)
        return jsonify(available=True, provider=provider, products=service.products(provider),
                       purchases_available=service.purchases_ready(provider) and management['can_purchase'],
                       contract_management=management,
                       account_token=account['account_token'], csrf_token=plans.csrf_token(),
                       package_name=os.environ.get('FEATURE_GOOGLE_PACKAGE_NAME') if provider == 'google' else None,
                       terms_url=os.environ['FEATURE_STORE_TERMS_URL'], privacy_url=os.environ['FEATURE_STORE_PRIVACY_URL'])

    @bp.post('/api/plans/store-intent')
    @login_required
    def intent():
        plans.check_csrf()
        body = body_json()
        provider = body.get('provider')
        service.client(provider)
        if body.get('action') == 'cancel':
            with plans.db(write=True) as cur:
                plans.lock_account(cur, int(current_user.id))
                account = service.account(cur, int(current_user.id))
                if (account['pending_key'] and isinstance(body.get('intent_id'), str) and
                        hmac.compare_digest(account['pending_key'], body['intent_id']) and account['pending_provider'] == provider):
                    plans.execute(cur, 'UPDATE feature_store_accounts SET pending_provider=NULL,pending_until=0,pending_key=NULL WHERE user_id=?', (int(current_user.id),))
            return jsonify(canceled=True)
        if not service.purchases_ready(provider):
            abort(503, description='現在、新規のお申し込みを停止しています。購入の復元か契約管理をご利用ください。')
        product = next((p for p in service.products(provider) if p['product_id'] == body.get('product_id') and
                       p.get('base_plan_id', '') == body.get('base_plan_id', '')), None)
        if not product:
            abort(409, description='この商品は現在お申し込みできません。')
        user_id = int(current_user.id)
        with plans.db(write=True) as cur:
            billing = plans.lock_account(cur, user_id)
            account = service.account(cur, user_id)
            service.map_products(cur, provider)
            plans.execute(cur, 'SELECT stripe_subscription_id FROM users WHERE id=?', (user_id,))
            legacy = plans.one(cur)
            plans.execute(cur, '''SELECT s.* FROM feature_subscriptions s LEFT JOIN feature_store_receipts r
                ON r.provider=s.provider AND r.subscription_id=s.subscription_id WHERE s.user_id=? AND
                s.status NOT IN ('canceled','expired','incomplete_expired')
                AND (s.provider='stripe' OR r.environment IS NULL OR r.environment=?)''',
                (user_id, os.environ['FEATURE_STORE_ENVIRONMENT']))
            contracts = plans.rows(cur)
            if (legacy and legacy['stripe_subscription_id']) or contracts or (billing['pending_until'] or 0) > int(time.time()):
                abort(409, description='現在の契約または手続きがあります。購入の復元・契約管理からご確認ください。')
            if billing['customer_id']:
                if not plans.billing_ready():
                    abort(503, description='現在のご契約を確認中です。')
                remote = plans.stripe.Subscription.list(customer=billing['customer_id'], status='all', limit=100, api_key=plans.key())
                if remote.get('has_more') or any(s.get('status') not in ('canceled', 'incomplete_expired') for s in remote.get('data', [])):
                    abort(409, description='現在のご契約をご確認ください。')
            if account['pending_environment'] == os.environ['FEATURE_STORE_ENVIRONMENT'] and account['pending_until'] > int(time.time()):
                abort(409, description='購入手続き中です。先の手続きか購入の復元をお試しください。')
            intent_id = str(uuid.uuid4())
            plans.execute(cur, '''UPDATE feature_store_accounts SET pending_provider=?,pending_until=?,pending_key=?,
                pending_product=?,pending_base_plan=?,pending_started=?,pending_environment=? WHERE user_id=?''',
                (provider, int(time.time()) + 900, intent_id, product['product_id'], product.get('base_plan_id', ''), int(time.time()), os.environ['FEATURE_STORE_ENVIRONMENT'], user_id))
        return jsonify(account_token=account['account_token'], intent_id=intent_id, **product)

    @bp.post('/api/plans/store-verify')
    @login_required
    def verify():
        plans.check_csrf()
        body = body_json()
        provider = body.get('provider')
        reference = body.get('transaction_id') if provider == 'apple' else body.get('purchase_token')
        evidence = service.process(provider, reference, expected_user=int(current_user.id))
        state = plans.snapshot()
        return jsonify(verified=True, finish_transaction=evidence.provider == 'apple' and evidence.finish_transaction,
                       store_acknowledged=evidence.provider == 'google' and evidence.acknowledged,
                       purchase_status=evidence.status,
                       plan_code=state['plan_code'], status=state['status'], features=state['features'])

    @bp.post('/billing/<provider>/notifications')
    def notification(provider):
        client = service.client(provider)
        event = client.notification(body_json(), request.headers.get('Authorization', ''))
        if event.reference:
            # Verification and current-store re-fetch also apply to retries. This
            # avoids trusting old notification states and recovers lost responses.
            service.process(provider, event.reference, event_id=event.event_id, voided_order_id=event.voided_order_id)
        return jsonify(received=True)

    app.register_blueprint(bp)
    return service
