"""Opt-in home, private inbox and notification settings. External sends disabled."""
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import os
import secrets
import hashlib
import json
import uuid
from urllib.parse import urlencode
import click

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException

from inventory_registry_web import RegistryWeb, _text
from item_registry import Actor, RegistryError, _now
from stage6_common import Stage6Store
from stage6_home import HomeService
from stage6_home_preferences import HomePreferences, DEFAULT_SECTIONS
from stage6_presentation import home_view, local_time
from stage6_events import EventScanner
from stage6_notifications import NotificationService
from stage6_transports import MemoryNotificationTransport
from stage6_transports import verified_line_events, DeliveryResult
from stage6_external_transports import transports_from_config, EXTERNAL_TRANSPORT_TYPES, LineTestTransport
from stage6_actions import CustodyActionService


def staff():
    return current_user.is_authenticated and current_user.is_admin() and current_user.has_permission('merchandise')


class HomeWeb(RegistryWeb):
    def actor(self):
        return Actor(int(current_user.get_id()), staff=staff())

    @contextmanager
    def db(self, write=False):
        with super().db(write) as registry:
            store = Stage6Store(registry)
            store.require_ready()
            yield store

    def clock(self):
        return self.app.extensions.get('stage6_clock', _now)()

    def epoch(self):
        value = datetime.fromisoformat(self.clock())
        if value.tzinfo is None:
            raise ValueError('Aware notification clock required')
        return value.timestamp()

    def sandbox_payments_enabled(self):
        adapter = self.app.extensions.get('stage9_payment_adapter')
        scope = self.app.extensions.get('stage9_payment_scope') or getattr(adapter, 'scope', None)
        return self.app.config.get('STAGE9_PAYMENTS_ENABLED') is True and scope is not None

    def scanner(self, store):
        return EventScanner(store, clock=self.clock, pre_due_days=7,
            sandbox_payments_enabled=self.sandbox_payments_enabled())

    def notifications(self, store):
        bridge = self.app.extensions.get('stage6_push_bridge')
        active=self.app.extensions.get('stage6_customer_active')
        return NotificationService(store, clock=self.epoch,
            validator=self.scanner(store).is_current,
            destination_validator=bridge.destination_valid if bridge else None,
            transports=self.app.extensions.get('stage6_test_transports'),
            customer_active=(lambda customer_id:active(store,customer_id)) if active else None,
            monthly_limits=self.app.config.get('STAGE6_TEST_MONTHLY_LIMITS', {}),
            origin=self.app.config.get('STAGE6_CONFIRMATION_ORIGIN', 'https://notification-preview.invalid'))

    def refresh(self, actor, source_key):
        with self.db(True) as store:
            api = self.notifications(store)
            events = self.scanner(store).scan(actor)
            created = 0
            for event in events:
                result = api.publish_event(actor, event, source_key=source_key + ':' + event['dedupe_key'])
                created += int(result['created'])
            return {'created': created, 'current': len(events)}


def register_stage6_web(runtime):
    app = runtime.app
    if 'workspace_home' in app.blueprints:
        return app.extensions['stage6_web']
    app.config.setdefault('STAGE6_ENABLED', os.environ.get('STAGE6_ENABLED') == '1')
    for name in ('STAGE6_TEST_DELIVERY_ENABLED','STAGE6_EXPO_TEST_ENABLED','STAGE6_LINE_TEST_ENABLED',
                 'STAGE6_SMTP_TEST_ENABLED','STAGE6_LINE_LINK_ENABLED'):
        app.config.setdefault(name,os.environ.get(name)=='1')
    for name,default in {'KAIKA_RUNTIME_ENV':'','STAGE6_TEST_RECIPIENT_APPROVALS':'[]','STAGE6_TEST_MONTHLY_LIMITS':'{}',
            'STAGE6_CONFIRMATION_ORIGIN':'https://notification-preview.invalid','STAGE6_EXPO_ACCESS_TOKEN':'',
            'STAGE6_LINE_CHANNEL_ACCESS_TOKEN':'','STAGE6_LINE_CHANNEL_SECRET':'','STAGE6_LINE_STAFF_USER_ID':'',
            'STAGE6_SMTP_HOST':'','STAGE6_SMTP_PORT':'587','STAGE6_SMTP_SENDER':'','STAGE6_SMTP_USERNAME':'',
            'STAGE6_SMTP_PASSWORD':'','STAGE6_SMTP_TLS_MODE':'starttls'}.items():
        app.config.setdefault(name,os.environ.get(name,default))
    if isinstance(app.config['STAGE6_TEST_MONTHLY_LIMITS'],str):
        try:app.config['STAGE6_TEST_MONTHLY_LIMITS']=json.loads(app.config['STAGE6_TEST_MONTHLY_LIMITS'])
        except ValueError:raise RuntimeError('Invalid notification test limits') from None
    external=transports_from_config(app.config)
    if external:
        app.extensions['stage6_test_transports']=external
    service = HomeWeb(runtime)
    bp = Blueprint('workspace_home', __name__)

    def enabled():
        return app.config.get('STAGE6_ENABLED') is True and all(app.config.get(name) is True for name in
            ('STAGE2_INVENTORY_ENABLED', 'STAGE3_FINANCE_ENABLED', 'STAGE4_COMMERCE_ENABLED', 'STAGE5_PAYMENTS_ENABLED'))

    def allowed(staff_only=False, customer_only=False):
        def decorate(view):
            @wraps(view)
            @login_required
            def inner(*args, **kwargs):
                if not enabled():
                    abort(404)
                if staff_only and not staff() or customer_only and staff() or not staff() and current_user.role != 'user':
                    abort(403)
                try:
                    return view(*args, **kwargs)
                except HTTPException:
                    raise
                except RegistryError as error:
                    return render_template('workspace_home/error.html', staff=staff(), message={
                        'notification_source_no_longer_current': '対象の状態が変わっています。ホームで最新の内容をご確認ください。',
                        'notification_budget_unconfirmed': '配信数の検証上限が未設定です。外部配信は行いません。',
                        'external_notification_delivery_disabled': '実際の通知配信は無効です。検証用の送信処理も未接続です。',
                        'notification_monthly_limit': '通知の検証用上限に達しています。アプリ内のお知らせは引き続き確認できます。',
                        'verification_rate_limit': '確認の依頼回数が上限に達しました。時間をおいてお試しください。',
                        'verification_invalid_or_expired': '確認情報が失効したか、このアカウントの確認情報ではありません。',
                        'notification_destination_in_use': 'この通知先は別のアカウントに連携済みです。',
                        'home_preferences_invalid': '表示する項目を選び直してください。',
                        'home_preferences_not_ready': '表示設定の保存を準備しています。ホームは引き続き確認できます。',
                    }.get(error.code, '現在の状態では操作できません。画面を開き直して内容をご確認ください。')), error.status
            return inner
        return decorate

    @bp.get('/workspace-home')
    @allowed()
    def home():
        with service.db() as store:
            api = HomeService(store, clock=service.clock, pre_due_days=7,
                sandbox_payments_enabled=service.sandbox_payments_enabled())
            dashboard = api.staff(service.actor()) if staff() else api.customer(service.actor())
            inbox = [] if staff() else service.notifications(store).inbox(service.actor())
            preferences = None if staff() else HomePreferences(store).get(service.actor())
        return render_template('workspace_home/home.html', dashboard=dashboard, inbox=inbox, staff=staff(),
            home_view=home_view(dashboard, request.args, staff()), local_time=local_time,
            home_preferences=preferences)

    @bp.post('/workspace-home/display')
    @allowed(customer_only=True)
    def display_preferences():
        key = service.verify('stage6_home_display')
        selected = list(DEFAULT_SECTIONS) if request.form.get('reset') == 'default' else request.form.getlist('sections')
        with service.db(True) as store:
            HomePreferences(store, clock=service.clock).save(service.actor(), selected, source_key=key)
        flash('ホームの表示を保存しました。他の端末でも同じ表示になります。', 'success')
        return redirect(url_for('workspace_home.home'))

    @bp.get('/notifications')
    @allowed()
    def inbox():
        with service.db() as store:
            api = service.notifications(store)
            notes = [] if staff() else api.inbox(service.actor())
            deliveries = api.deliveries(service.actor())
            usage = api.usage(service.actor()) if staff() else None
        return render_template('workspace_home/inbox.html', notes=notes, deliveries=deliveries, usage=usage, staff=staff())

    @bp.post('/notifications/refresh')
    @allowed()
    def refresh():
        service.refresh(service.actor(), service.verify('stage6_refresh'))
        flash('最新の業務記録を確認しました。実際の通知は配信していません。', 'success')
        return redirect(url_for('workspace_home.inbox'))

    @bp.get('/notifications/<notification_id>')
    @allowed()
    def notification(notification_id):
        with service.db() as store:
            note = service.notifications(store).get_notification(service.actor(), notification_id)
        return render_template('workspace_home/notification.html', note=note, staff=staff())

    @bp.post('/notifications/<notification_id>/read')
    @allowed(customer_only=True)
    def read(notification_id):
        key = service.verify('stage6_read', notification_id)
        with service.db(True) as store:
            service.notifications(store).mark_read(service.actor(), notification_id, source_key=key)
        return redirect(url_for('workspace_home.notification', notification_id=notification_id))

    @bp.get('/notifications/settings')
    @allowed(customer_only=True)
    def settings():
        with service.db() as store:
            result = service.notifications(store).settings(service.actor())
        result['line_connection_ready']=result['line_connection_ready'] and app.config.get('STAGE6_LINE_LINK_ENABLED') is True
        return render_template('workspace_home/settings.html', settings=result, staff=False)

    @bp.post('/notifications/settings/<event_kind>')
    @allowed(customer_only=True)
    def preference(event_kind):
        key = service.verify('stage6_preference', event_kind)
        with service.db(True) as store:
            service.notifications(store).set_preferences(service.actor(), event_kind, request.form.getlist('channels'), source_key=key)
        return redirect(url_for('workspace_home.settings'))

    @bp.post('/notifications/email')
    @allowed(customer_only=True)
    def email():
        key = service.verify('stage6_email')
        with service.db(True) as store:
            service.notifications(store).request_email_verification(service.actor(), _text('email', 254, True), source_key=key)
        flash('確認依頼を検証用キューに保存しました。許可された検証先への送信はスタッフが実行します。', 'info')
        return redirect(url_for('workspace_home.settings'))

    @bp.get('/notifications/email/confirm')
    def email_confirm_landing():
        if not enabled():abort(404)
        return render_template('workspace_home/email_confirm.html', staff=False,
            authenticated=current_user.is_authenticated and current_user.role=='user')

    @bp.post('/notifications/email/confirm')
    @allowed(customer_only=True)
    def email_confirm():
        # GET shows a confirmation form; mail scanners cannot consume a proof.
        if request.method == 'POST':
            key = service.verify('stage6_confirm_email')
            with service.db(True) as store:
                service.notifications(store).confirm_email(service.actor(), _text('token', 200, True), source_key=key)
            return redirect(url_for('workspace_home.settings'))

    def line_transport():
        adapter=app.extensions.get('stage6_test_transports',{}).get('line')
        if not enabled() or app.config.get('STAGE6_LINE_LINK_ENABLED') is not True or type(adapter) is not LineTestTransport:
            abort(404)
        return adapter

    @bp.get('/notifications/line/link')
    def line_link_landing():
        line_transport()
        authenticated=current_user.is_authenticated and current_user.role=='user'
        return render_template('workspace_home/line_link.html',authenticated=authenticated,staff=False)

    @bp.post('/notifications/line/link')
    @allowed(customer_only=True)
    def line_link():
        adapter=line_transport()
        key=service.verify('stage6_line_link')
        invitation,link_token=_text('invitation',200,True),_text('link_token',1024,True)
        with service.db(True) as store:
            api=service.notifications(store)
            expected=api.line_invitation_customer(service.actor(),invitation,adapter.policy)
            result=api.begin_line_link(service.actor(),invitation,source_key=key,link_token=link_token,expected_customer_id=expected)
        if not result.get('nonce'):
            raise RegistryError('line_invitation_invalid_or_expired',status=409)
        return redirect('https://access.line.me/dialog/bot/accountLink?'+urlencode({'linkToken':link_token,'nonce':result['nonce']}))

    @bp.post('/notifications/line/webhook')
    def line_webhook():
        adapter=line_transport()
        if request.content_length is not None and request.content_length>256000:abort(413)
        raw=request.get_data(cache=False)
        signature=request.headers.get('x-line-signature','')
        secret=app.config.get('STAGE6_LINE_CHANNEL_SECRET','')
        try:
            events=verified_line_events(raw,signature,secret)
            staff_id=int(app.config.get('STAGE6_LINE_STAFF_USER_ID',''))
            actor=Actor(staff_id,staff=True)
            # Do not activate unknown recipients via a public callback.
            for event in events:
                if event.get('type')=='accountLink':
                    adapter.policy.customer_for('line',event.get('source',{}).get('userId',''))
            def invitation_allowed(event):
                try:
                    adapter.policy.customer_for('line',event.get('source',{}).get('userId',''))
                except RegistryError:return False
                return event.get('type')=='follow' or (event.get('message',{}).get('type')=='text' and event['message'].get('text')=='開花連携')
            with service.db(True) as store:
                result=service.notifications(store).receive_line_webhook(actor,raw,signature,secret,
                    source_key='webhook:'+hashlib.sha256(raw).hexdigest(),invitation_allowed=invitation_allowed,
                    line_customer_for=lambda user:adapter.policy.customer_for('line',user))
            # All transactions are closed before either LINE API request.
            for invitation in result['internal_invitations']:
                recipient=invitation['line_user_id']
                try:
                    customer=adapter.policy.customer_for('line',recipient)
                    with service.db() as store:
                        if not service.notifications(store)._active(customer):continue
                    link_token=adapter.link_token(recipient)
                    with service.db(True) as store:
                        if not service.notifications(store)._active(customer):continue
                        prepared=service.notifications(store).prepare_line_token(actor,invitation['invitation_token'],recipient,
                            link_token,source_key='line-token:'+invitation['webhook_event_id'])
                    url=app.config['STAGE6_CONFIRMATION_ORIGIN']+'/notifications/line/link#'+urlencode({
                        'invitation':invitation['invitation_token'],'linkToken':link_token})
                    message=dict(delivery_id=uuid.uuid5(uuid.NAMESPACE_URL,'kaika-line-link:'+invitation['webhook_event_id']).hex,
                        customer_id=customer,test_only=True,recipient=recipient,title='【開花・検証】LINE連携',
                        body='ご自身が依頼した場合だけ、開花にログインして連携を確認してください。',link_invitation_url=url)
                    try:outcome=adapter.send(message)
                    except Exception:outcome=DeliveryResult('unknown',code='transport_result_unknown')
                    with service.db(True) as store:
                        service.notifications(store).record_line_invitation_result(actor,prepared['invitation_id'],outcome,
                            source_key='line-invitation-result:'+invitation['webhook_event_id'])
                except Exception:
                    # No secret/linkToken/body or recipient enters logs. A lost
                    # response needs a new invitation, not an automatic resend.
                    app.logger.warning('LINE test invitation deferred; request a new link.')
            return {'accepted':True}
        except RegistryError as error:
            return {'accepted':False},403 if error.status==403 else 400
        except (ValueError,TypeError,AttributeError):
            return {'accepted':False},400
        except Exception:
            app.logger.warning('LINE test webhook deferred; no private request details logged.')
            return {'accepted':False},503

    @bp.post('/notifications/destinations/<destination_id>/revoke')
    @allowed(customer_only=True)
    def revoke(destination_id):
        key = service.verify('stage6_revoke_destination', destination_id)
        with service.db(True) as store:
            service.notifications(store).revoke_destination(service.actor(), destination_id, source_key=key)
            bridge = app.extensions.get('stage6_push_bridge')
            if bridge is not None:
                bridge.deactivate_legacy_destination(store, service.actor(), destination_id)
        return redirect(url_for('workspace_home.settings'))

    @bp.route('/notifications/items/<item_id>/choice', methods=['GET', 'POST'])
    @allowed()
    def choice(item_id):
        if request.method == 'POST':
            if staff():
                abort(403)
            key = service.verify('stage6_custody_choice', item_id)
            with service.db(True) as store:
                CustodyActionService(store).request(service.actor(), item_id, choice=_text('choice', 30, True),
                    note=_text('note', 1000), source_key=key)
            return redirect(url_for('workspace_home.choice', item_id=item_id))
        with service.db() as store:
            item = store.registry.detail(service.actor(), item_id)
            choices = CustodyActionService(store).list_for_item(service.actor(), item_id)
        return render_template('workspace_home/choice.html', item=item, choices=choices, staff=staff())

    @bp.post('/notifications/choices/<request_id>/review')
    @allowed(staff_only=True)
    def choice_review(request_id):
        key = service.verify('stage6_choice_review', request_id)
        with service.db(True) as store:
            result = CustodyActionService(store).review(service.actor(), request_id, status=_text('status', 30, True),
                note=_text('note', 1000, True), source_key=key)
        return redirect(url_for('workspace_home.choice', item_id=result['physical_item_id']))

    @bp.post('/notifications/deliveries/<delivery_id>/<action>')
    @allowed(staff_only=True)
    def delivery_action(delivery_id, action):
        if action not in ('simulate', 'send_test', 'reconcile', 'retry'):
            abort(404)
        key = service.verify('stage6_delivery_' + action, delivery_id)
        if action in ('simulate','send_test'):
            with service.db(True) as store:
                api = service.notifications(store)
                rows = [row for row in api.deliveries(service.actor()) if row['delivery_id'] == delivery_id]
                if not rows:
                    abort(404)
                adapter_type=type(api.transports.get(rows[0]['channel']))
                if (action=='simulate' and adapter_type is not MemoryNotificationTransport
                        or action=='send_test' and adapter_type not in EXTERNAL_TRANSPORT_TYPES):
                    raise RegistryError('external_notification_delivery_disabled', status=409)
                claim = api.claim(service.actor(), delivery_id, source_key=key)
            # This is a different connection after the claim was committed.
            if claim.get('claim_token'):
                with service.db() as store:
                    store.connection.rollback()  # End readiness-only reads on the fresh worker connection.
                    service.notifications(store).dispatch_claim(service.actor(), delivery_id, claim['claim_token'])
        elif action == 'reconcile':
            with service.db() as store:
                store.connection.rollback()
                service.notifications(store).reconcile(service.actor(), delivery_id)
        else:
            with service.db(True) as store:
                service.notifications(store).retry_rejected(service.actor(), delivery_id, source_key=key)
        return redirect(url_for('workspace_home.inbox'))

    @bp.post('/notifications/recover')
    @allowed(staff_only=True)
    def recover():
        key = service.verify('stage6_recover')
        with service.db(True) as store:
            service.notifications(store).recover_stale_claims(service.actor(), source_key=key)
        flash('中断した処理を確認しました。送信結果が不明な通知は再送せず、照合待ちにしています。', 'info')
        return redirect(url_for('workspace_home.inbox'))

    @bp.after_request
    def private(response):
        response.headers.update({'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'no-referrer'})
        return response

    @app.before_request
    def home_entry():
        if enabled() and request.endpoint == 'user_home' and request.method == 'GET' and current_user.is_authenticated:
            return redirect(url_for('workspace_home.home'))

    @app.after_request
    def collect_changed_business(response):
        # Persist an inbox after the business transaction commits, never send.
        # Re-scanning is idempotent, so a failed projection can recover on refresh.
        if (enabled() and current_user.is_authenticated and request.method == 'POST'
                and response.status_code < 400 and request.blueprint in
                ('inventory_registry', 'finance_review', 'commerce', 'payment_review')):
            try:
                service.refresh(service.actor(), 'after-business:' + secrets.token_hex(16))
            except Exception:
                app.logger.warning('Notification projection deferred; business response retained.')
        return response

    @app.context_processor
    def context():
        return dict(stage6_available=enabled(), stage6_csrf=service.csrf, stage6_operation=service.operation,
                    stage6_external_test_channels=tuple(channel for channel,adapter in app.extensions.get('stage6_test_transports',{}).items()
                        if type(adapter) in EXTERNAL_TRANSPORT_TYPES),
                    notification_channels={'push': 'プッシュ', 'email': 'メール', 'line': 'LINE'},
                    notification_states={'queued': '待機中', 'claimed': '送信準備済み', 'sending': '結果を確認中',
                        'accepted': '配信サービスが受付', 'provider_accepted': '配信先サービスが受付',
                        'failed': '失敗', 'unknown': '結果不明・再照合が必要', 'suppressed': '送信を停止'})

    app.register_blueprint(bp)
    app.extensions['stage6_web'] = service

    @app.cli.command('stage6-deliver-test')
    @click.option('--staff-user-id',type=int,required=True)
    @click.option('--delivery-id',required=True)
    @click.option('--send-approved-test',is_flag=True,help='Actually send to an explicitly approved test recipient.')
    @click.option('--reconcile',is_flag=True)
    def deliver_command(staff_user_id,delivery_id,send_approved_test,reconcile):
        if not enabled() or send_approved_test and reconcile:
            raise click.ClickException('Notification test action is disabled or ambiguous.')
        actor=Actor(staff_user_id,staff=True)
        try:
            if send_approved_test:
                with service.db(True) as store:
                    api=service.notifications(store)
                    row=api._delivery(actor,delivery_id)
                    api._adapter(row,api._request(row))
                    claim=api.claim(actor,delivery_id,source_key='cli-claim:'+secrets.token_hex(16))
                if claim.get('claim_token'):
                    with service.db() as store:
                        store.connection.rollback()
                        result=service.notifications(store).dispatch_claim(actor,delivery_id,claim['claim_token'])
                else:result=claim
            elif reconcile:
                with service.db() as store:
                    store.connection.rollback()
                    result=service.notifications(store).reconcile(actor,delivery_id)
            else:
                with service.db() as store:
                    store.authorize(actor,staff_required=True)
                    result=service.notifications(store)._project_delivery(service.notifications(store)._delivery(actor,delivery_id))
            click.echo('Notification state: '+result['state']+'; recipient delivery/read is not confirmed.')
        except RegistryError as error:
            raise click.ClickException(error.code) from None

    @app.cli.command('stage6-notifications')
    @click.option('--staff-user-id', type=int, required=True)
    @click.option('--record', is_flag=True, help='Record the current scoped inbox events. Never sends externally.')
    def refresh_command(staff_user_id, record):
        """Explicit future scheduler entry. Default is a read-only event count."""
        if not enabled():
            raise click.ClickException('Stage 6 is disabled; no data accessed.')
        actor = Actor(staff_user_id, staff=True)
        if record:
            result = service.refresh(actor, 'scheduled-refresh:' + secrets.token_hex(16))
        else:
            with service.db() as store:
                store.authorize(actor, staff_required=True)
                result = {'current': len(service.scanner(store).scan(actor)), 'created': 0}
        click.echo('Current events: ' + str(result['current']) + '; created: ' + str(result['created']) + '; external deliveries: 0')

    return service
