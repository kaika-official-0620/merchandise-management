"""Payment review and explicitly injected offline card simulations. Never live."""
from contextlib import contextmanager
from functools import wraps
import os

from flask import Blueprint,abort,redirect,render_template,request,url_for
from flask_login import current_user,login_required
from werkzeug.exceptions import HTTPException

from inventory_registry_web import RegistryWeb,_text
from item_registry import Actor,RegistryError
from stage3_web import _amount,reason_label,FEE_KINDS
from stage5_common import Stage5Store
from stage5_payments import PhysicalPaymentService


def staff():
    return current_user.is_authenticated and current_user.is_admin() and current_user.has_permission('merchandise') and current_user.has_permission('invoices')


class PaymentWeb(RegistryWeb):
    def actor(self):
        return Actor(int(current_user.get_id()),staff=staff())

    @contextmanager
    def db(self,write=False):
        with super().db(write) as registry:
            store=Stage5Store(registry)
            store.require_ready()
            yield store

    def payments(self,store):
        # Never discover provider secrets or create a network client from env.
        return PhysicalPaymentService(store,self.app.extensions.get('stage5_test_provider'))


def register_stage5_payments(runtime):
    app=runtime.app
    if 'payment_review' in app.blueprints:
        return app.extensions['stage5_payments']
    app.config.setdefault('STAGE5_PAYMENTS_ENABLED',os.environ.get('STAGE5_PAYMENTS_ENABLED')=='1')
    service=PaymentWeb(runtime)
    bp=Blueprint('payment_review',__name__)

    def enabled():
        return all(app.config.get(name) is True for name in ('STAGE2_INVENTORY_ENABLED','STAGE3_FINANCE_ENABLED','STAGE4_COMMERCE_ENABLED','STAGE5_PAYMENTS_ENABLED'))

    def allowed(staff_only=False):
        def decorate(view):
            @wraps(view)
            @login_required
            def inner(*args,**kwargs):
                if not enabled():
                    abort(404)
                if staff_only and not staff() or not staff() and current_user.role!='user':
                    abort(403)
                try:
                    return view(*args,**kwargs)
                except HTTPException:
                    raise
                except RegistryError as error:
                    messages={
                        'physical_invoice_stale':'明細が変更されています。元の請求下書きと訂正内容を確認してください。',
                        'payment_attempt_exists_reconcile_first':'この明細には支払い処理が残っています。新しく請求せず、既存の結果を照合してください。',
                        'off_session_consent_required':'カードへの自動請求について、お客様の同意記録が必要です。',
                        'payment_consent_revoked':'この支払い方法の同意は解除されています。',
                        'refund_exceeds_original_payment':'返金済み・返金処理中の金額を含めると、元のお支払い額を超えます。',
                        'refund_requires_verified_payment':'支払い成功の確認が完了していないため、返金を開始できません。',
                        'payment_outcome_unknown_reconcile_required':'支払い結果を確認できません。新しい決済を作らず、既存の処理を確認してください。',
                    }
                    message=messages.get(error.code,str(error))
                    if message==error.code:
                        message='対象の明細・同意・処理状況を再確認してください。この操作から実請求は行いません。'
                    return render_template('payment_review/error.html',message=message,staff=staff()),error.status
            return inner
        return decorate

    @bp.get('/payments')
    @allowed()
    def index():
        with service.db() as store:
            api=service.payments(store)
            overview=api.overview(service.actor())
            rows=store.query('SELECT invoice_id,customer_id,period,revision,status FROM stage3_invoice_revisions'+
                ('' if staff() else ' WHERE customer_id=?')+' ORDER BY period DESC,revision DESC',() if staff() else (service.actor().user_id,))
        plans=app.extensions.get('kaika_feature_plans')
        management=plans.contract_management() if plans and not staff() else None
        return render_template('payment_review/index.html',overview=overview,invoices=rows,management=management,staff=staff())

    @bp.route('/payments/invoices/<invoice_id>',methods=['GET','POST'])
    @allowed()
    def invoice(invoice_id):
        if request.method=='POST':
            if not staff():
                abort(403)
            key=service.verify('stage5_prepare',invoice_id)
            with service.db(True) as store:
                bill=service.payments(store).prepare_invoice(service.actor(),invoice_id,
                    review_reference=_text('review_reference',1000,True),source_key=key)
            return redirect(url_for('payment_review.bill',bill_id=bill['bill_id']))
        with service.db() as store:
            result=service.payments(store).inspect_invoice(service.actor(),invoice_id)
        return render_template('payment_review/invoice.html',invoice=result,invoice_id=invoice_id,staff=staff())

    @bp.post('/payments/consents')
    @allowed()
    def consent():
        if staff() or request.form.get('agree')!='yes':
            abort(400)
        key=service.verify('stage5_consent')
        customer=service.actor().user_id
        automatic=request.form.get('off_session')=='yes'
        with service.db(True) as store:
            service.payments(store).record_consent(service.actor(),customer,
                terms_reference='fixture-physical-v1:'+('automatic' if automatic else 'customer')+':'+key,
                payment_method_reference='pm_test_customer_'+str(customer),provider_customer_reference='cus_test_customer_'+str(customer),
                off_session=automatic,source_key=key)
        return redirect(url_for('payment_review.index'))

    @bp.post('/payments/consents/<consent_id>/revoke')
    @allowed()
    def revoke(consent_id):
        key=service.verify('stage5_revoke',consent_id)
        with service.db(True) as store:
            service.payments(store).revoke_consent(service.actor(),consent_id,reason=_text('reason',1000,True),source_key=key)
        return redirect(url_for('payment_review.index'))

    @bp.route('/payments/bills/<bill_id>',methods=['GET','POST'])
    @allowed()
    def bill(bill_id):
        if request.method=='POST':
            key=service.verify('stage5_attempt',bill_id)
            with service.db(True) as store:
                result=service.payments(store).create_attempt(service.actor(),bill_id,_text('consent_id',100,True),
                    off_session=request.form.get('off_session')=='yes',source_key=key)
            return redirect(url_for('payment_review.attempt',attempt_id=result['attempt_id']))
        with service.db() as store:
            api=service.payments(store)
            result=api.get_bill(service.actor(),bill_id)
            overview=api.overview(service.actor(),result['customer_id'])
        return render_template('payment_review/bill.html',bill=result,consents=overview['consents'],staff=staff())

    @bp.get('/payments/attempts/<attempt_id>')
    @allowed()
    def attempt(attempt_id):
        with service.db() as store:
            api=service.payments(store)
            result=api.get_attempt(service.actor(),attempt_id)
            connected=api.adapter is not None
        return render_template('payment_review/attempt.html',attempt=result,connected=connected,staff=staff())

    @bp.post('/payments/attempts/<attempt_id>/<action>')
    @allowed()
    def payment_action(attempt_id,action):
        if action not in ('dispatch','reconcile'):
            abort(404)
        key=service.verify('stage5_payment_'+action,attempt_id)
        with service.db(True) as store:
            api=service.payments(store)
            getattr(api,action+'_attempt')(service.actor(),attempt_id,source_key=key)
        return redirect(url_for('payment_review.attempt',attempt_id=attempt_id))

    @bp.post('/payments/attempts/<attempt_id>/refund')
    @allowed(True)
    def refund(attempt_id):
        key=service.verify('stage5_refund',attempt_id)
        with service.db(True) as store:
            service.payments(store).request_refund(service.actor(),attempt_id,_amount('amount_yen'),reason=_text('reason',1000,True),source_key=key)
        return redirect(url_for('payment_review.attempt',attempt_id=attempt_id))

    @bp.post('/payments/refunds/<refund_id>/<action>')
    @allowed(True)
    def refund_action(refund_id,action):
        if action not in ('dispatch','reconcile'):
            abort(404)
        key=service.verify('stage5_refund_'+action,refund_id)
        with service.db(True) as store:
            api=service.payments(store)
            result=getattr(api,action+'_refund')(service.actor(),refund_id,source_key=key)
        return redirect(url_for('payment_review.attempt',attempt_id=result['attempt_id']))

    @bp.after_request
    def private(response):
        response.headers.update({'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})
        return response

    @app.context_processor
    def context():
        return dict(stage5_payments_available=enabled(),stage5_csrf=service.csrf,stage5_operation=service.operation,
            payment_fee_kinds=FEE_KINDS,payment_reason=reason_label)

    app.register_blueprint(bp)
    app.extensions['stage5_payments']=service
    return service
