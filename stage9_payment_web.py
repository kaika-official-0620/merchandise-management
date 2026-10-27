"""Opt-in sandbox screens/CLI; registration performs no DB/provider I/O."""
from functools import wraps
import json

import click
from flask import Blueprint,abort,jsonify,redirect,render_template,request,url_for
from flask_login import current_user,login_required
from werkzeug.exceptions import HTTPException

from inventory_registry_web import _text
from item_registry import Actor,RegistryError
from stage3_web import _amount
from stage5_web import PaymentWeb,staff
from stage9_payments import SandboxPayments

TERMS_REFERENCE='sandbox-physical-card-2026-10-25:v1'
STATES={'queued':'送信準備済み','sending':'送信処理中','unknown':'結果要確認','suppressed':'処理停止',
    'open':'お客様の操作待ち','requires_action':'本人認証待ち','requires_payment_method':'カード変更が必要',
    'requires_confirmation':'確認待ち','processing':'処理中','requires_capture':'確定待ち','pending':'処理中',
    'succeeded':'テスト成功','failed':'失敗','canceled':'取消済み'}


class SandboxPaymentWeb(PaymentWeb):
    def payments(self):
        adapter=self.app.extensions.get('stage9_payment_adapter')
        scope=self.app.extensions.get('stage9_payment_scope') or (adapter.scope if adapter else None)
        if scope is None: raise RegistryError('sandbox_scope_unconfigured',status=409)
        def outbound(customer):
            lifecycle=self.app.extensions.get('account_lifecycle')
            return lifecycle.outbound_allowed(customer) if lifecycle else True
        return SandboxPayments(self.db,scope,adapter,outbound_allowed=outbound)


def register_stage9_payments(runtime):
    app=runtime.app
    if 'sandbox_payments' in app.blueprints: return app.extensions['stage9_payments']
    app.config.setdefault('STAGE9_PAYMENTS_ENABLED',False)
    service=SandboxPaymentWeb(runtime); bp=Blueprint('sandbox_payments',__name__)

    def enabled():
        return all(app.config.get(key) is True for key in ('STAGE2_INVENTORY_ENABLED','STAGE3_FINANCE_ENABLED',
            'STAGE4_COMMERCE_ENABLED','STAGE5_PAYMENTS_ENABLED','STAGE9_PAYMENTS_ENABLED'))

    def allowed(staff_only=False):
        def decorate(view):
            @wraps(view)
            @login_required
            def inner(*args,**kwargs):
                if not enabled(): abort(404)
                if staff_only and not staff() or not staff() and current_user.role!='user': abort(403)
                lifecycle=app.extensions.get('account_lifecycle')
                if not staff() and lifecycle and not lifecycle.access_allowed(int(current_user.get_id())): abort(403)
                try: return view(*args,**kwargs)
                except HTTPException: raise
                except RegistryError as error:
                    # No raw provider exception or object payload in a response.
                    messages={
                        'sandbox_scope_unconfigured':'検証先はまだ接続されていません。担当者がテスト環境を設定してから利用できます。',
                        'stripe_sandbox_not_connected':'Stripeのテスト接続が未設定です。実際のカード請求は行いません。',
                        'sandbox_migration_required':'検証用データベースの準備が未完了です。',
                        'sandbox_job_busy':'同じ処理を確認中です。少し待ってから画面を開き直してください。',
                        'sandbox_provider_reference_missing':'送信先の結果をまだ特定できません。新しい支払いを作らず、担当者が確認します。',
                        'sandbox_payment_exists_reconcile_first':'この明細の支払い処理は既にあります。一覧から既存の処理を確認してください。',
                        'sandbox_idempotency_window_expired':'再送できる時間を過ぎています。Stripe側の結果を担当者が確認するまで停止します。',
                        'sandbox_account_paused':'削除申請などにより、新しい支払いの操作を停止しています。',
                        'sandbox_customer_not_allowed':'このアカウントではテスト決済を行えません。',
                        'sandbox_consent_revoked':'カード利用の同意は解除されています。',
                    }
                    return render_template('sandbox_payments/error.html',message=messages.get(error.code,
                        '明細・同意・決済結果を再確認してください。確認できない料金を支払い済みにはしません。')),error.status
            return inner
        return decorate

    @bp.get('/payments/sandbox')
    @allowed()
    def index():
        return render_template('sandbox_payments/index.html',overview=service.payments().overview(service.actor()),staff=staff())

    @bp.post('/payments/sandbox/bills/<bill_id>')
    @allowed()
    def queue_bill(bill_id):
        key=service.verify('stage9_bill',bill_id)
        api=service.payments()
        if staff(): job=api.queue(service.actor(),'off_session',bill_id=bill_id,consent_id=_text('consent_id',100,True),source_key=key)
        else: job=api.queue(service.actor(),'checkout',bill_id=bill_id,source_key=key)
        return redirect(url_for('sandbox_payments.job',job_id=job['job_id']))

    @bp.post('/payments/sandbox/consents')
    @allowed()
    def setup():
        if staff() or request.form.get('agree')!='yes': abort(400)
        key=service.verify('stage9_setup')
        job=service.payments().queue(service.actor(),'setup',terms_reference=TERMS_REFERENCE,source_key=key)
        return redirect(url_for('sandbox_payments.job',job_id=job['job_id']))

    @bp.post('/payments/sandbox/consents/<consent_id>/revoke')
    @allowed()
    def revoke(consent_id):
        service.verify('stage9_revoke',consent_id)
        service.payments().revoke_consent(service.actor(),consent_id)
        return redirect(url_for('sandbox_payments.index'))

    @bp.get('/payments/sandbox/<job_id>')
    @allowed()
    def job(job_id):
        # A success/cancel URL visit never changes financial state.
        return render_template('sandbox_payments/job.html',job=service.payments().get(service.actor(),job_id),staff=staff())

    @bp.post('/payments/sandbox/<job_id>/<action>')
    @allowed()
    def action(job_id,action):
        if action not in ('dispatch','reconcile','recover'): abort(404)
        service.verify('stage9_'+action,job_id)
        if action=='recover' and not staff(): abort(403)
        api=service.payments()
        if action=='reconcile': api.reconcile(service.actor(),job_id)
        else: api.dispatch(service.actor(),job_id,recover=action=='recover')
        return redirect(url_for('sandbox_payments.job',job_id=job_id))

    @bp.post('/payments/sandbox/<job_id>/refund')
    @allowed(True)
    def refund(job_id):
        key=service.verify('stage9_refund',job_id)
        job=service.payments().queue(service.actor(),'refund',parent_job_id=job_id,amount_yen=_amount('amount_yen'),
            reason=_text('reason',1000,True),source_key=key)
        return redirect(url_for('sandbox_payments.job',job_id=job['job_id']))

    @bp.post('/payments/sandbox/<job_id>/continue')
    @allowed()
    def continuation(job_id):
        service.verify('stage9_continue',job_id)
        result=service.payments().continuation(service.actor(),job_id)
        if result['kind']=='redirect': return redirect(result['url'],code=303)
        return render_template('sandbox_payments/authenticate.html',authentication=result,job_id=job_id)

    @bp.post('/webhooks/stripe/physical-sandbox')
    def webhook():
        if not enabled(): abort(404)
        if request.content_length and request.content_length>1048576: abort(413)
        try: return jsonify(service.payments().accept_webhook(request.get_data(cache=False),request.headers.get('Stripe-Signature','')))
        except RegistryError as error: return jsonify(received=False,error='sandbox_webhook_rejected'),error.status

    @bp.after_request
    def private(response):
        response.headers.update({'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})
        return response

    @app.context_processor
    def context():
        return dict(stage9_payments_available=enabled(),stage9_csrf=service.csrf,stage9_operation=service.operation,
            stage9_status=lambda value:STATES.get(value,'要確認'))

    @app.cli.command('stage9-payments')
    @click.option('--staff-user-id',required=True,type=int)
    @click.option('--job-id',required=True)
    @click.option('--action',type=click.Choice(['inspect','dispatch','reconcile','recover']),default='inspect')
    @click.option('--send-approved-test',is_flag=True,default=False)
    def worker(staff_user_id,job_id,action,send_approved_test):
        if not enabled(): raise click.ClickException('Sandbox payments are disabled')
        actor=Actor(staff_user_id,True)
        # Explicit authorization from stored finance role, even inspect-only.
        with service.db() as store: store.authorize(actor,staff_required=True,finance=True)
        api=service.payments()
        try:
            if action=='inspect': result=api.get(actor,job_id)
            elif action=='reconcile': result=api.reconcile(actor,job_id)
            elif not send_approved_test: raise click.ClickException('Explicit --send-approved-test required')
            else: result=api.dispatch(actor,job_id,recover=action=='recover')
            click.echo(json.dumps({key:result[key] for key in ('job_id','state','environment','needs_reconcile')},ensure_ascii=False))
        except RegistryError as error: raise click.ClickException(error.code) from None

    app.register_blueprint(bp); app.extensions['stage9_payments']=service
    return service
