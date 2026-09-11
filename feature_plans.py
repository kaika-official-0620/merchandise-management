"""Opt-in feature subscriptions, separate from the existing inventory tuition.

No database or payment API is accessed during registration. SQLite fixtures can
exercise this module without importing the production app or its scheduler.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
import hmac
import os
import re
import secrets
import time
from urllib.parse import urlsplit

from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException


FEATURES = {
    'inventory_manage': '自分の在庫を登録・管理',
    'dealer_sales': '業者卸・オークション・併売の申請',
}
PLAN_FEATURES = {
    'normal': ('inventory_manage',),
    'business': ('inventory_manage', 'dealer_sales'),
}
PLAN_NAMES = {'normal': 'ノーマル', 'business': 'ビジネス'}
STATUS_NAMES = {'active': '利用中', 'past_due': 'お支払いの確認待ち',
                'grace_period': 'お支払い方法をご確認ください',
                'unpaid': 'お支払いの確認待ち', 'canceled': '解約済み',
                'incomplete': '決済手続き中', 'incomplete_expired': '手続き期限切れ',
                'paused': '停止中', 'trialing': '試用中', 'expired': '利用期間終了', 'none': '未契約'}
TERMINAL_STATUSES = ('canceled', 'incomplete_expired')
PROVIDER_NAMES = {'stripe': 'Webで契約（クレジットカード）', 'apple': 'App Store', 'google': 'Google Play'}


def contract_date(value):
    try:
        return datetime.fromtimestamp(int(value), timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H:%M（日本時間）') if value else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def stripe_id(value):
    return value.get('id') if isinstance(value, dict) else value


class FeaturePlans:
    def __init__(self, runtime):
        self.runtime = runtime
        self.enabled = os.environ.get('FEATURE_PLANS_ENABLED') == '1'
        self.postgres = bool(getattr(runtime, 'DATABASE_URL', None))
        self.stripe = getattr(runtime, 'stripe', None)

    def execute(self, cursor, sql, args=()):
        cursor.execute(sql.replace('?', '%s') if self.postgres else sql, args)

    def rows(self, cursor):
        names = [col[0] for col in cursor.description]
        return [dict(row) if hasattr(row, 'keys') else dict(zip(names, row)) for row in cursor.fetchall()]

    def one(self, cursor):
        rows = self.rows(cursor)
        return rows[0] if rows else None

    @contextmanager
    def db(self, write=False):
        conn = self.runtime.get_db()
        cursor = conn.cursor()
        try:
            if write and not self.postgres:
                cursor.execute('BEGIN IMMEDIATE')
            self.schema(cursor)
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()
            conn.close()

    def schema(self, cur):
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_plan_catalog (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, monthly_amount INTEGER,
            stripe_price_id TEXT NOT NULL DEFAULT '', selling INTEGER NOT NULL DEFAULT 0)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_plan_prices (
            price_id TEXT PRIMARY KEY, plan_code TEXT NOT NULL, monthly_amount INTEGER NOT NULL)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_billing_accounts (
            user_id INTEGER PRIMARY KEY, customer_id TEXT UNIQUE, pending_key TEXT,
            pending_plan TEXT, pending_price TEXT, pending_session TEXT, pending_until BIGINT)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_subscriptions (
            provider TEXT NOT NULL, subscription_id TEXT NOT NULL, user_id INTEGER NOT NULL,
            plan_code TEXT, status TEXT NOT NULL, period_end BIGINT NOT NULL DEFAULT 0,
            cancel_at_period_end INTEGER NOT NULL DEFAULT 0, verified_at BIGINT NOT NULL,
            PRIMARY KEY (provider, subscription_id))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_manual_grants (
            user_id INTEGER PRIMARY KEY, plan_code TEXT NOT NULL, expires_at BIGINT NOT NULL)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_billing_events (
            event_id TEXT PRIMARY KEY, processed_at BIGINT NOT NULL)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_plan_changes (
            subscription_id TEXT PRIMARY KEY, plan_code TEXT NOT NULL,
            effective_at BIGINT NOT NULL, verified_at BIGINT NOT NULL)''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_store_receipts (
            provider TEXT NOT NULL, subscription_id TEXT NOT NULL, account_token TEXT NOT NULL,
            product_id TEXT NOT NULL, base_plan_id TEXT NOT NULL, environment TEXT NOT NULL,
            PRIMARY KEY (provider, subscription_id))''')
        cur.execute('''CREATE TABLE IF NOT EXISTS feature_plan_audit (
            id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, subject TEXT NOT NULL,
            action TEXT NOT NULL, detail TEXT NOT NULL, created_at BIGINT NOT NULL)''')
        for code, name in PLAN_NAMES.items():
            self.execute(cur, '''INSERT INTO feature_plan_catalog (code, name) VALUES (?, ?)
                ON CONFLICT (code) DO NOTHING''', (code, name))

    def native(self):
        return 'KaikaApp/' in request.headers.get('User-Agent', '')

    def csrf_token(self):
        if '_feature_csrf' not in session:
            session['_feature_csrf'] = secrets.token_urlsafe(32)
        return session['_feature_csrf']

    def check_csrf(self):
        supplied = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
        expected = session.get('_feature_csrf', '')
        if not expected or not hmac.compare_digest(str(supplied), str(expected)):
            abort(400, description='画面を再読み込みしてから、もう一度お試しください。')

    def catalog(self, cur=None):
        if cur is None:
            with self.db() as cursor:
                return self.catalog(cursor)
        cur.execute('SELECT * FROM feature_plan_catalog ORDER BY CASE code WHEN \'normal\' THEN 0 ELSE 1 END')
        return [dict(row, features=PLAN_FEATURES.get(row['code'], ())) for row in self.rows(cur)]

    def snapshot(self, user=None):
        user = user if user is not None else current_user
        if not user.is_authenticated:
            return {'plan_code': None, 'status': 'none', 'features': [], 'subscriptions': [], 'manual': None}
        cache = getattr(g, '_feature_plans_snapshot', {})
        key = int(user.id)
        if key in cache:
            return cache[key]
        with self.db() as cur:
            self.execute(cur, '''SELECT s.*, r.environment AS store_environment FROM feature_subscriptions s
                LEFT JOIN feature_store_receipts r ON r.provider=s.provider AND r.subscription_id=s.subscription_id
                WHERE s.user_id = ?''', (key,))
            subscriptions = self.rows(cur)
            subscriptions = [sub for sub in subscriptions if sub['provider'] not in ('apple', 'google') or
                             sub['store_environment'] == os.environ.get('FEATURE_STORE_ENVIRONMENT')]
            self.execute(cur, '''SELECT c.* FROM feature_plan_changes c JOIN feature_subscriptions s
                ON s.provider='stripe' AND s.subscription_id=c.subscription_id WHERE s.user_id=?''', (key,))
            changes = self.rows(cur)
            self.execute(cur, 'SELECT * FROM feature_manual_grants WHERE user_id = ?', (key,))
            manual = self.one(cur)
        now = int(time.time())
        codes = [sub['plan_code'] for sub in subscriptions if sub['status'] in ('active', 'grace_period') and sub['period_end'] > now and
                 (sub['provider'] not in ('apple', 'google') or sub['store_environment'] == os.environ.get('FEATURE_STORE_ENVIRONMENT'))]
        if manual and manual['expires_at'] > now:
            codes.append(manual['plan_code'])
        else:
            manual = None
        code = 'business' if 'business' in codes else ('normal' if 'normal' in codes else None)
        last_status = max(subscriptions, key=lambda s: s['verified_at'])['status'] if subscriptions else 'none'
        if not code and last_status == 'active':
            last_status = 'expired'
        value = {'plan_code': code, 'status': 'active' if code else last_status,
                 'features': list(PLAN_FEATURES.get(code, ())), 'subscriptions': subscriptions, 'manual': manual,
                 'changes': [change for change in changes if change['effective_at'] > now]}
        cache[key] = value
        g._feature_plans_snapshot = cache
        return value

    def has_feature(self, feature, user=None):
        user = user if user is not None else current_user
        if feature not in FEATURES or not user.is_authenticated:
            return False
        if user.is_admin():
            return True  # Existing admin permissions still apply in each business handler.
        if not self.enabled:
            return feature == 'dealer_sales'  # Preserve old workflows, no new self-registration.
        return feature in self.snapshot(user)['features']

    def contract_management(self, state=None, device=None):
        """Private display data only; never expose customer/transaction/token identifiers.

        Dates come from verified periods, not an inferred charge success. This
        does not contact a payment provider or change any entitlement.
        """
        state = state if state is not None else self.snapshot()
        with self.db() as cur:
            self.execute(cur, 'SELECT stripe_subscription_id FROM users WHERE id=?', (int(current_user.id),))
            legacy = bool((self.one(cur) or {}).get('stripe_subscription_id'))
        names = {plan['code']: plan['name'] for plan in self.catalog()}
        now = int(time.time())
        contracts, active_providers = [], set()
        for sub in sorted(state['subscriptions'], key=lambda item: item['verified_at'], reverse=True):
            open_contract = sub['status'] not in ('canceled', 'expired', 'incomplete_expired')
            if open_contract:
                active_providers.add(sub['provider'])
            current = sub['status'] in ('active', 'grace_period') and sub['period_end'] > now
            renewal = sub['period_end'] if current and sub['status'] == 'active' and not sub['cancel_at_period_end'] else None
            changes = [change for change in state.get('changes', []) if
                       sub['provider'] == 'stripe' and change['subscription_id'] == sub['subscription_id']]
            contracts.append(dict(provider=sub['provider'], provider_name=PROVIDER_NAMES.get(sub['provider'], '契約元を確認中'),
                plan_name=names.get(sub['plan_code'], 'プランを確認中'), status=STATUS_NAMES.get(sub['status'], '確認中'),
                active=current, cancel_at_period_end=bool(sub['cancel_at_period_end']),
                next_billing=contract_date(renewal), access_until=contract_date(sub['period_end']) if current else None,
                verified_at=contract_date(sub['verified_at']),
                changes=[dict(plan_name=names.get(change['plan_code'], '確認中'), effective_at=contract_date(change['effective_at'])) for change in changes]))
        has_contract = legacy or bool(active_providers)
        instructions = []
        if legacy:
            instructions.append('以前からの在庫料金の契約があります。追加契約を始める前に、既存の契約内容と切替方法をお問い合わせからご相談ください。')
        if 'stripe' in active_providers:
            instructions.append('Webで購入した契約です。PCまたはスマートフォンのブラウザーで同じアカウントにログインし、利用プランの「支払い方法・解約の管理」からお手続きください。App Store・Google Playでは管理できません。')
        if 'apple' in active_providers:
            instructions.append('App Storeで購入した契約です。購入時のApple Accountを使い、iPhoneの「設定」→自分の名前→「サブスクリプション」で変更・解約を確認できます。')
        if 'google' in active_providers:
            instructions.append('Google Playで購入した契約です。購入時のGoogleアカウントを使い、Google Playの「お支払いと定期購入」→「定期購入」で変更・解約を確認できます。別の端末ではブラウザーのGoogle Playから同じアカウントで確認してください。')
        return dict(contracts=contracts, has_contract=has_contract, legacy_contract=legacy,
            can_purchase=not has_contract, can_manage_here=device in active_providers,
            can_restore_here=bool(device in active_providers or not has_contract),
            instructions=instructions, manual_until=contract_date((state.get('manual') or {}).get('expires_at')),
            current_plan=names.get(state['plan_code'], '有効な追加プランなし'))

    def require(self, feature):
        def decorator(view):
            @wraps(view)
            @login_required
            def wrapped(*args, **kwargs):
                if not self.has_feature(feature):
                    if request.is_json or request.path.startswith('/api/'):
                        return jsonify(error='plan_required', feature=feature), 403
                    return render_template('feature_plan_required.html', feature_label=FEATURES[feature]), 403
                if self.enabled and feature == 'dealer_sales' and request.method == 'POST':
                    self.check_csrf()
                return view(*args, **kwargs)
            return wrapped
        return decorator

    def administrator(self):
        if not current_user.is_authenticated:
            abort(401)
        if not current_user.is_admin() or not current_user.has_permission('users'):
            abort(403)

    def audit(self, cur, subject, action, detail):
        self.execute(cur, '''INSERT INTO feature_plan_audit VALUES (?, ?, ?, ?, ?, ?)''',
                     (secrets.token_hex(16), int(current_user.id), str(subject), action, detail, int(time.time())))

    def key(self):
        return os.environ.get('FEATURE_STRIPE_SECRET_KEY') or getattr(self.runtime, 'STRIPE_SECRET_KEY', '')

    def billing_origin(self):
        origin = os.environ.get('FEATURE_BILLING_ORIGIN', '').rstrip('/')
        parsed = urlsplit(origin)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            abort(503, description='決済の準備中です。')
        return origin

    def billing_ready(self):
        return bool(self.stripe and self.key() and os.environ.get('FEATURE_STRIPE_WEBHOOK_SECRET') and
                    os.environ.get('FEATURE_BILLING_ORIGIN') and os.environ.get('FEATURE_STRIPE_PORTAL_CONFIG_ID'))

    def web_billing_only(self):
        self.check_csrf()
        if self.native():
            abort(403, description='このアプリでは決済手続きを利用できません。')
        if not self.billing_ready():
            abort(503, description='決済の準備中です。')
        self.billing_origin()

    def lock_account(self, cur, user_id):
        self.execute(cur, 'INSERT INTO feature_billing_accounts (user_id) VALUES (?) ON CONFLICT (user_id) DO NOTHING', (user_id,))
        self.execute(cur, 'SELECT * FROM feature_billing_accounts WHERE user_id = ?' + (' FOR UPDATE' if self.postgres else ''), (user_id,))
        return self.one(cur)

    def validate_price(self, plan):
        if not plan or not plan['selling'] or not plan['monthly_amount'] or not plan['stripe_price_id']:
            abort(409, description='このプランは受付準備中です。')
        price = self.stripe.Price.retrieve(plan['stripe_price_id'], api_key=self.key())
        recurring = price.get('recurring') or {}
        if not (price.get('active') and price.get('currency') == 'jpy' and
                price.get('unit_amount') == plan['monthly_amount'] and price.get('tax_behavior') == 'inclusive' and
                recurring.get('interval') == 'month' and recurring.get('interval_count') == 1 and
                recurring.get('usage_type') == 'licensed'):
            abort(409, description='プランの料金設定を確認中です。お手続きは行われていません。')
        return price

    def reject_legacy_contract(self, cur, user_id):
        self.execute(cur, 'SELECT stripe_subscription_id FROM users WHERE id=?', (user_id,))
        legacy = self.one(cur)
        if legacy and legacy.get('stripe_subscription_id'):
            abort(409, description='現在のご契約を確認する必要があります。お問い合わせからご連絡ください。')
        self.execute(cur, '''SELECT s.subscription_id FROM feature_subscriptions s LEFT JOIN feature_store_receipts r
            ON r.provider=s.provider AND r.subscription_id=s.subscription_id WHERE s.user_id=?
            AND s.provider!='stripe' AND s.status NOT IN ('canceled','incomplete_expired','expired')
            AND (r.environment IS NULL OR r.environment=?)''', (user_id, os.environ.get('FEATURE_STORE_ENVIRONMENT', '')))
        if self.one(cur):
            abort(409, description='別のお支払い方法でご契約があります。現在の契約をご確認ください。')
        store = self.runtime.app.extensions.get('kaika_store_billing')
        if store:
            store.schema(cur)
            self.execute(cur, 'SELECT pending_until,pending_environment FROM feature_store_accounts WHERE user_id=?', (user_id,))
            pending = self.one(cur)
            if pending and pending['pending_environment'] == os.environ.get('FEATURE_STORE_ENVIRONMENT') and pending['pending_until'] > int(time.time()):
                abort(409, description='アプリで購入手続き中です。手続き状況をご確認ください。')

    def validate_change_configuration(self, cur):
        config_id = os.environ.get('FEATURE_STRIPE_CHANGE_PORTAL_CONFIG_ID')
        if not config_id:
            abort(503, description='プラン変更の準備中です。')
        config = self.stripe.billing_portal.Configuration.retrieve(config_id, api_key=self.key())
        features = config.get('features') or {}
        update = features.get('subscription_update') or {}
        conditions = (update.get('schedule_at_period_end') or {}).get('conditions') or []
        cancel = features.get('subscription_cancel') or {}
        if not (config.get('active') and update.get('enabled') and
                update.get('default_allowed_updates') == ['price'] and
                update.get('proration_behavior') == 'always_invoice' and
                update.get('billing_cycle_anchor', 'unchanged') == 'unchanged' and
                conditions == [{'type': 'decreasing_item_amount'}] and
                (not cancel.get('enabled') or cancel.get('mode') == 'at_period_end')):
            abort(503, description='プラン変更の設定を確認中です。')
        # The portal may still offer navigation into its allowed products. Keep
        # its whole catalogue restricted, not just the requested target price.
        plans = self.catalog(cur)
        prices = {plan['code']: self.validate_price(plan) for plan in plans}
        products = {stripe_id(price.get('product')) for price in prices.values()}
        configured = update.get('products') or []
        if not (len(products) == 1 and None not in products and '' not in products and
                len(configured) == 1 and stripe_id(configured[0].get('product')) in products and
                set(configured[0].get('prices') or []) == {price['id'] for price in prices.values()} and
                not (configured[0].get('adjustable_quantity') or {}).get('enabled') and
                prices['normal']['unit_amount'] < prices['business']['unit_amount']):
            abort(503, description='プラン変更の料金設定を確認中です。')
        return config_id, prices

    def validate_portal_configuration(self):
        config_id = os.environ.get('FEATURE_STRIPE_PORTAL_CONFIG_ID')
        if not config_id:
            abort(503, description='契約管理の準備中です。')
        config = self.stripe.billing_portal.Configuration.retrieve(config_id, api_key=self.key())
        portal_features = config.get('features') or {}
        cancel = portal_features.get('subscription_cancel') or {}
        if not config.get('active') or (portal_features.get('subscription_update') or {}).get('enabled') or not cancel.get('enabled') or cancel.get('mode') != 'at_period_end':
            abort(503, description='契約管理の設定を確認中です。')
        return config_id

    def sync_subscription(self, cur, user_id, customer_id, subscription_id):
        self.execute(cur, "SELECT * FROM feature_subscriptions WHERE provider='stripe' AND subscription_id=?", (subscription_id,))
        existing = self.one(cur)
        if existing and existing['user_id'] != user_id:
            raise ValueError('Subscription already belongs to another user')
        sub = self.stripe.Subscription.retrieve(subscription_id, expand=['latest_invoice'], api_key=self.key())
        if stripe_id(sub.get('customer')) != customer_id:
            raise ValueError('Subscription customer mismatch')
        if (sub.get('metadata') or {}).get('kaika_feature_billing') != '1' and not existing:
            return
        items = (sub.get('items') or {}).get('data') or []
        plan_code = None
        period_end = 0
        if len(items) == 1 and items[0].get('quantity') == 1:
            price = items[0].get('price') or {}
            self.execute(cur, 'SELECT * FROM feature_plan_prices WHERE price_id = ?', (stripe_id(price),))
            mapping = self.one(cur)
            if mapping and isinstance(price, dict) and price.get('currency') == 'jpy' and price.get('unit_amount') == mapping['monthly_amount']:
                plan_code = mapping['plan_code']
                period_end = int(items[0].get('current_period_end') or sub.get('current_period_end') or 0)
        status = sub.get('status') or 'incomplete'
        invoice = sub.get('latest_invoice')
        if status == 'active' and (not isinstance(invoice, dict) or invoice.get('status') != 'paid'):
            status = 'past_due'  # Never unlock a new billing period from an unpaid invoice.
            # Stripe keeps the old subscription items while a pending upgrade
            # awaits payment. Preserve only the already verified paid period.
            if (sub.get('pending_update') and existing and existing['status'] == 'active' and
                    existing['plan_code'] == plan_code and existing['period_end'] > int(time.time())):
                status = 'active'
                period_end = min(period_end, existing['period_end'])
        if (sub.get('metadata') or {}).get('kaika_feature_billing') != '1':
            plan_code, status = None, 'paused'
        if sub.get('pause_collection'):
            status = 'paused'
        self.execute(cur, '''INSERT INTO feature_subscriptions
            (provider, subscription_id, user_id, plan_code, status, period_end, cancel_at_period_end, verified_at)
            VALUES ('stripe', ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (provider, subscription_id) DO UPDATE SET
            plan_code=excluded.plan_code, status=excluded.status, period_end=excluded.period_end,
            cancel_at_period_end=excluded.cancel_at_period_end, verified_at=excluded.verified_at''',
            (subscription_id, user_id, plan_code, status, period_end, int(bool(sub.get('cancel_at_period_end'))), int(time.time())))
        self.execute(cur, 'DELETE FROM feature_plan_changes WHERE subscription_id=?', (subscription_id,))
        if sub.get('schedule') and status == 'active':
            schedule = self.stripe.SubscriptionSchedule.retrieve(stripe_id(sub['schedule']), api_key=self.key())
            if stripe_id(schedule.get('subscription')) != subscription_id or stripe_id(schedule.get('customer')) != customer_id:
                raise ValueError('Subscription schedule ownership mismatch')
            phases = [phase for phase in schedule.get('phases', []) if phase.get('start_date', 0) > int(time.time())]
            if schedule.get('status') == 'active' and phases:
                phase = min(phases, key=lambda p: p['start_date'])
                next_items = phase.get('items') or []
                if len(next_items) == 1 and next_items[0].get('quantity') == 1:
                    self.execute(cur, 'SELECT plan_code FROM feature_plan_prices WHERE price_id=?', (stripe_id(next_items[0].get('price')),))
                    mapping = self.one(cur)
                    if mapping and mapping['plan_code'] != plan_code:
                        self.execute(cur, 'INSERT INTO feature_plan_changes VALUES (?, ?, ?, ?)',
                                     (subscription_id, mapping['plan_code'], int(phase['start_date']), int(time.time())))


def register_feature_plans(runtime):
    app = runtime.app
    if 'kaika_feature_plans' in app.extensions:
        return app.extensions['kaika_feature_plans']
    service = FeaturePlans(runtime)
    app.extensions['kaika_feature_plans'] = service

    @app.context_processor
    def feature_plan_context():
        return dict(feature_plans_enabled=service.enabled, has_plan_feature=service.has_feature,
                    feature_csrf_token=service.csrf_token, feature_native_app=service.native(),
                    feature_labels=FEATURES)

    @app.before_request
    def native_billing_guard():
        if service.native() and (request.path.startswith('/admin/stripe') or request.path.startswith('/api/stripe/') or
                                 (request.endpoint == 'profile' and request.method == 'POST' and 'requested_monthly_plan' in request.form)):
            abort(403, description='このアプリでは決済・プラン変更手続きを利用できません。')

    if not service.enabled:
        return service
    bp = Blueprint('feature_plans', __name__)

    @bp.errorhandler(Exception)
    def billing_error(error):
        if isinstance(error, HTTPException):
            return error
        current_app.logger.exception('Feature plan operation failed')
        return '現在お手続きを完了できません。時間をおいてからもう一度お試しください。', 503

    @bp.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @bp.get('/plans')
    @login_required
    def plans():
        state = service.snapshot()
        management = service.contract_management(state)
        web_contracts = [s for s in state['subscriptions'] if s['provider'] == 'stripe' and
                         s['status'] == 'active' and s['period_end'] > int(time.time())]
        return render_template('feature_plans.html', plans=service.catalog(), state=state,
                               management=management,
                               status_names=STATUS_NAMES, billing_ready=service.billing_ready(),
                               portal_ready=service.billing_ready(),
                               change_ready=bool(service.billing_ready() and os.environ.get('FEATURE_STRIPE_CHANGE_PORTAL_CONFIG_ID') and
                                   len(web_contracts) == 1 and not web_contracts[0]['cancel_at_period_end'] and not state['changes']),
                               open_contract=management['has_contract'] or any(s['status'] not in TERMINAL_STATUSES for s in state['subscriptions']),
                               billing_return=request.args.get('result', '') == 'pending')

    @bp.get('/api/plans/me')
    @login_required
    def my_plan():
        value = service.snapshot()
        return jsonify(plan_code=value['plan_code'], status=value['status'], features=value['features'])

    @bp.post('/billing/checkout')
    @login_required
    def checkout():
        service.web_billing_only()
        service.validate_portal_configuration()
        user_id = int(current_user.id)
        code = request.form.get('plan_code', '')
        with service.db(write=True) as cur:
            account = service.lock_account(cur, user_id)
            # Old fees must be reconciled before starting a second subscription.
            service.reject_legacy_contract(cur, user_id)
            plan = next((p for p in service.catalog(cur) if p['code'] == code), None)
            service.validate_price(plan)
            if not account['customer_id']:
                customer = service.stripe.Customer.create(metadata={'kaika_feature_user_id': str(user_id)},
                    idempotency_key='kaika-feature-customer-' + str(user_id), api_key=service.key())
                service.execute(cur, 'UPDATE feature_billing_accounts SET customer_id = ? WHERE user_id = ?', (customer['id'], user_id))
                account['customer_id'] = customer['id']
            # Query Stripe too: the webhook for a completed checkout can arrive later.
            remote = service.stripe.Subscription.list(customer=account['customer_id'], status='all', limit=100, api_key=service.key())
            if remote.get('has_more') or any(s.get('status') not in TERMINAL_STATUSES for s in remote.get('data', [])):
                abort(409, description='すでに契約または決済手続きがあります。契約内容をご確認ください。')
            if account['pending_key'] and (account['pending_until'] or 0) > int(time.time()):
                if account['pending_plan'] != code or account['pending_price'] != plan['stripe_price_id']:
                    abort(409, description='別のプランのお手続き中です。先のお手続きを完了するか、有効期限後にやり直してください。')
            else:
                service.execute(cur, '''UPDATE feature_billing_accounts SET pending_key=?, pending_plan=?,
                    pending_price=?, pending_session=NULL, pending_until=? WHERE user_id=?''',
                    (secrets.token_hex(24), code, plan['stripe_price_id'], int(time.time()) + 90000, user_id))
        # Persist the idempotency key before issuing a billable operation.
        with service.db(write=True) as cur:
            account = service.lock_account(cur, user_id)
            if account['pending_session']:
                checkout_session = service.stripe.checkout.Session.retrieve(account['pending_session'], api_key=service.key())
                if checkout_session.get('status') != 'open':
                    abort(409, description='決済結果を確認中です。利用プラン画面を更新してください。')
            else:
                checkout_session = service.stripe.checkout.Session.create(
                    mode='subscription', customer=account['customer_id'], client_reference_id=str(user_id),
                    line_items=[{'price': account['pending_price'], 'quantity': 1}],
                    subscription_data={'metadata': {'kaika_feature_billing': '1'}},
                    success_url=service.billing_origin() + '/plans?result=pending',
                    cancel_url=service.billing_origin() + '/plans', locale='ja',
                    idempotency_key=account['pending_key'], api_key=service.key())
                service.execute(cur, 'UPDATE feature_billing_accounts SET pending_session=?, pending_until=? WHERE user_id=?',
                    (checkout_session['id'], int(checkout_session['expires_at']) + 3600, user_id))
        return redirect(checkout_session['url'], code=303)

    @bp.post('/billing/change-plan')
    @login_required
    def change_plan():
        service.web_billing_only()
        user_id = int(current_user.id)
        target = request.form.get('plan_code', '')
        if target not in PLAN_FEATURES:
            abort(400)
        with service.db(write=True) as cur:
            account = service.lock_account(cur, user_id)
            service.reject_legacy_contract(cur, user_id)
            config_id, prices = service.validate_change_configuration(cur)
            if not account['customer_id']:
                abort(409, description='変更できるWeb決済のご契約がありません。')
            remote = service.stripe.Subscription.list(customer=account['customer_id'], status='all', limit=100, api_key=service.key())
            contracts = [s for s in remote.get('data', []) if s.get('status') not in TERMINAL_STATUSES]
            if remote.get('has_more') or len(contracts) != 1:
                abort(409, description='契約内容の確認が必要です。お問い合わせからご連絡ください。')
            sub = service.stripe.Subscription.retrieve(contracts[0]['id'], expand=['latest_invoice'], api_key=service.key())
            items = (sub.get('items') or {}).get('data') or []
            if not (stripe_id(sub.get('customer')) == account['customer_id'] and
                    (sub.get('metadata') or {}).get('kaika_feature_billing') == '1' and
                    sub.get('status') == 'active' and not sub.get('cancel_at_period_end') and
                    not sub.get('cancel_at') and not sub.get('pause_collection') and
                    not sub.get('pending_update') and not sub.get('schedule') and
                    isinstance(sub.get('latest_invoice'), dict) and sub['latest_invoice'].get('status') == 'paid' and
                    len(items) == 1 and items[0].get('quantity') == 1 and items[0].get('id') and
                    int(items[0].get('current_period_end') or sub.get('current_period_end') or 0) > int(time.time())):
                abort(409, description='お支払いや変更・解約予約の状況をご確認ください。')
            item = items[0]
            current_price = item.get('price') or {}
            service.execute(cur, 'SELECT * FROM feature_plan_prices WHERE price_id=?', (stripe_id(current_price),))
            mapping = service.one(cur)
            recurring = current_price.get('recurring') or {}
            if not (mapping and current_price.get('unit_amount') == mapping['monthly_amount'] and
                    current_price.get('currency') == 'jpy' and current_price.get('tax_behavior') == 'inclusive' and
                    recurring.get('interval') == 'month' and recurring.get('interval_count') == 1 and
                    recurring.get('usage_type') == 'licensed' and
                    stripe_id(current_price.get('product')) == stripe_id(prices[target].get('product'))):
                abort(409, description='現在の料金設定を確認する必要があります。')
            source = mapping['plan_code']
            if not ((source == 'normal' and target == 'business' and mapping['monthly_amount'] < prices[target]['unit_amount']) or
                    (source == 'business' and target == 'normal' and mapping['monthly_amount'] > prices[target]['unit_amount'])):
                abort(409, description='現在の契約からこのプランへの変更は個別の確認が必要です。')
            service.execute(cur, "SELECT user_id FROM feature_subscriptions WHERE provider='stripe' AND subscription_id=?", (sub['id'],))
            known = service.one(cur)
            if known and known['user_id'] != user_id:
                abort(409, description='契約内容の確認が必要です。')
            # This only opens Stripe's price/proration confirmation. It neither
            # changes the subscription nor grants a plan from the browser return.
            portal_session = service.stripe.billing_portal.Session.create(customer=account['customer_id'],
                configuration=config_id, return_url=service.billing_origin() + '/plans', locale='ja',
                flow_data={'type': 'subscription_update_confirm', 'subscription_update_confirm': {
                    'subscription': sub['id'], 'items': [{'id': item['id'], 'price': prices[target]['id'], 'quantity': 1}]},
                    'after_completion': {'type': 'redirect', 'redirect': {'return_url': service.billing_origin() + '/plans?result=pending'}}},
                api_key=service.key())
            service.audit(cur, sub['id'], 'change_confirmation', 'from=' + source + '; to=' + target)
        return redirect(portal_session['url'], code=303)

    @bp.post('/billing/portal')
    @login_required
    def portal():
        service.web_billing_only()
        config_id = service.validate_portal_configuration()
        with service.db() as cur:
            service.execute(cur, 'SELECT customer_id FROM feature_billing_accounts WHERE user_id=?', (int(current_user.id),))
            account = service.one(cur)
        if not account or not account['customer_id']:
            abort(409, description='Web決済のご契約がありません。')
        portal_session = service.stripe.billing_portal.Session.create(customer=account['customer_id'],
            configuration=config_id, return_url=service.billing_origin() + '/plans', api_key=service.key())
        return redirect(portal_session['url'], code=303)

    @bp.post('/billing/webhook')
    def webhook():
        secret = os.environ.get('FEATURE_STRIPE_WEBHOOK_SECRET')
        if not secret or not service.key() or not service.stripe:
            return jsonify(error='billing_not_configured'), 503
        if request.content_length and request.content_length > 1024 * 1024:
            abort(413)
        try:
            payload = request.stream.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                abort(413)
            event = service.stripe.Webhook.construct_event(payload, request.headers.get('Stripe-Signature', ''), secret)
        except HTTPException:
            raise
        except Exception:
            return jsonify(error='invalid_signature'), 400
        event_type = event.get('type', '')
        obj = (event.get('data') or {}).get('object') or {}
        if event_type.startswith('customer.subscription.'):
            subscription_id = obj.get('id')
        elif event_type in ('checkout.session.completed', 'checkout.session.async_payment_succeeded',
                            'invoice.paid', 'invoice.payment_succeeded', 'invoice.payment_failed'):
            subscription_id = stripe_id(obj.get('subscription')) or stripe_id(((obj.get('parent') or {}).get('subscription_details') or {}).get('subscription'))
        else:
            return jsonify(received=True)
        customer_id = stripe_id(obj.get('customer'))
        if not subscription_id or not customer_id or not event.get('id'):
            return jsonify(received=True)
        # Serialize events per customer and fetch current Stripe state; don't trust
        # event arrival order, browser return parameters, or user-supplied plan IDs.
        try:
            with service.db(write=True) as cur:
                service.execute(cur, 'SELECT * FROM feature_billing_accounts WHERE customer_id=?' + (' FOR UPDATE' if service.postgres else ''), (customer_id,))
                account = service.one(cur)
                if not account:
                    return jsonify(received=True)
                service.execute(cur, 'SELECT event_id FROM feature_billing_events WHERE event_id=?', (event['id'],))
                if service.one(cur):
                    return jsonify(received=True)
                service.sync_subscription(cur, account['user_id'], customer_id, subscription_id)
                service.execute(cur, 'INSERT INTO feature_billing_events VALUES (?, ?) ON CONFLICT (event_id) DO NOTHING', (event['id'], int(time.time())))
        except Exception:
            current_app.logger.exception('Feature billing synchronization failed')
            return jsonify(error='retry_later'), 503
        return jsonify(received=True)

    @bp.route('/admin/feature-plans', methods=['GET', 'POST'])
    @login_required
    def admin():
        service.administrator()
        if service.native():
            abort(403)
        if request.method == 'POST':
            service.check_csrf()
            action = request.form.get('action')
            with service.db(write=True) as cur:
                code = request.form.get('plan_code', '')
                if action == 'catalog':
                    name = request.form.get('name', '').strip()
                    price_id = request.form.get('stripe_price_id', '').strip()
                    amount = request.form.get('monthly_amount', '').strip()
                    selling = int(request.form.get('selling') == '1')
                    if code not in PLAN_FEATURES or not 1 <= len(name) <= 60:
                        abort(400)
                    if amount and (not amount.isascii() or not amount.isdigit() or not 50 <= int(amount) <= 1000000):
                        abort(400, description='月額料金を正しく入力してください。')
                    if price_id and not re.fullmatch(r'price_[A-Za-z0-9]+', price_id):
                        abort(400)
                    if (price_id and not amount) or (selling and (not price_id or not amount)):
                        abort(400, description='受付には月額料金と決済の設定が必要です。')
                    if price_id:
                        service.execute(cur, 'SELECT * FROM feature_plan_prices WHERE price_id=?', (price_id,))
                        old = service.one(cur)
                        if old and (old['plan_code'] != code or old['monthly_amount'] != int(amount)):
                            abort(409, description='使用済みのPrice IDは別のプランや料金に転用できません。')
                        service.execute(cur, 'INSERT INTO feature_plan_prices VALUES (?, ?, ?) ON CONFLICT (price_id) DO NOTHING', (price_id, code, int(amount)))
                    service.execute(cur, 'UPDATE feature_plan_catalog SET name=?, monthly_amount=?, stripe_price_id=?, selling=? WHERE code=?',
                        (name, int(amount) if amount else None, price_id, selling, code))
                    service.audit(cur, code, action, f'name={name}; amount={amount}; price={price_id}; selling={selling}')
                elif action in ('grant', 'revoke'):
                    try:
                        user_id = int(request.form.get('user_id', ''))
                        days = int(request.form.get('days', '0'))
                    except ValueError:
                        abort(400)
                    reason = request.form.get('reason', '').strip()
                    if not 3 <= len(reason) <= 300:
                        abort(400, description='付与・解除の理由を入力してください。')
                    service.execute(cur, 'SELECT id FROM users WHERE id=?', (user_id,))
                    if not service.one(cur):
                        abort(404)
                    if action == 'grant':
                        if code not in PLAN_FEATURES or not 1 <= days <= 90:
                            abort(400)
                        service.execute(cur, '''INSERT INTO feature_manual_grants VALUES (?, ?, ?) ON CONFLICT (user_id)
                            DO UPDATE SET plan_code=excluded.plan_code, expires_at=excluded.expires_at''', (user_id, code, int(time.time()) + days * 86400))
                    else:
                        service.execute(cur, 'DELETE FROM feature_manual_grants WHERE user_id=?', (user_id,))
                    service.audit(cur, user_id, action, f'plan={code}; days={days}; reason={reason}')
                else:
                    abort(400)
            return redirect(url_for('feature_plans.admin'), code=303)
        with service.db() as cur:
            plans = service.catalog(cur)
            cur.execute('SELECT id, username, display_name FROM users ORDER BY id')
            users = service.rows(cur)
            cur.execute('SELECT * FROM feature_plan_audit ORDER BY created_at DESC LIMIT 30')
            audit = service.rows(cur)
            cur.execute('SELECT * FROM feature_manual_grants ORDER BY user_id')
            grants = service.rows(cur)
        return render_template('feature_plans_admin.html', plans=plans, users=users, audit=audit, grants=grants,
                               billing_ready=service.billing_ready())

    app.register_blueprint(bp)
    # Wrap the actual runtime route, including patches, without altering its role,
    # item ownership checks, transaction logic, or cancellation/history routes.
    for endpoint in ('sales_agency_apply',):
        if endpoint in app.view_functions:
            app.view_functions[endpoint] = service.require('dealer_sales')(app.view_functions[endpoint])
    return service
