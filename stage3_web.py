"""Opt-in financial review UI. No real payment, final invoice, or live balance."""
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
import os

from flask import Blueprint, abort, flash, redirect, render_template, request, Response, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException

from inventory_registry_web import RegistryWeb, JST, _time, _text
from item_registry import Actor, RegistryError
from stage3_common import FinanceStore, yen

AMOUNTS = {
    'purchase_cost_yen': '仕入原価', 'sale_gross_yen': '税込販売価格',
    'channel_fee_yen': '販売先の手数料', 'materials_yen': '資材費',
    'kaika_fee_yen': '開花料金（作業・販売料など、月額を除く）',
    'return_adjustment_yen': '返品等の利益調整（利益が減る場合はマイナス）',
    'monthly_allocation_yen': 'この商品に配分する月額利用料',
}
CHANNELS = {'wholesale':'業者卸', 'multichannel':'同時出品', 'dealer_auction':'業者競売',
            'self_sale_shipping':'自己販売・開花が発送', 'live':'LIVE（条件未確定）'}
BUCKETS = {'purchase_available':'購入用・未使用分', 'purchase_reserved':'購入予約分',
    'sales_unpaid':'売上・入金待ち', 'sales_unsettled':'売上・精算未確認',
    'sales_return_hold':'売上・返品保留', 'sales_settlement_ready':'売上・精算可能の記録', 'fees_payable':'料金・未払いの記録'}
KINDS = {'deposit':'入金', 'reserve':'購入予約', 'release':'予約解除', 'purchase':'購入', 'refund':'返金',
    'invoice':'請求額記録', 'payment':'支払', 'correction':'訂正（元記録の全額反転）',
    'sales_record':'売上記録', 'sales_receipt':'売上入金', 'sales_hold':'売上の返品保留',
    'sales_release':'返品保留の解除', 'sales_ready':'精算可能への振替', 'sales_settlement':'売上精算'}
STATUS = {'unpaid':'入金待ち', 'paid':'入金の記録あり', 'return_hold':'返品・取消の確認待ち',
    'settlement_ready':'精算可能の確認済み', 'estimate':'見込', 'needs_review':'要確認',
    'awaiting_payment_review':'入金・精算の確認待ち', 'realized':'記録に基づく確定利益'}
FEE_KINDS = {'work_bundle':'一式作業料', 'sales_fee':'販売料', 'expense':'実費',
    'digital_monthly':'デジタル月額', 'physical_monthly':'現物サービス月額', 'adjustment':'訂正・調整'}


def reason_label(code):
    from business_policy import REASONS
    from stage3_billing import EXTRA_REASONS
    labels = {
        'financial_facts_not_recorded':'金額と原資料が未登録です。',
        'kaika_fee_draft_pending':'開花料金の下書きが未確定です。手入力の料金だけでは利益を確定できません。',
        'shipping_amount_unknown':'送料が未確認です。',
        'shipping_source_amount_conflict':'同じ送料の原資料に異なる金額があります。',
        'channel_fee_shipping_double_count':'販売先手数料と送料が重複しています。内訳の確認が必要です。',
        'channel_fee_shipping_scope_unknown':'販売先手数料に送料が含まれるか未確認です。',
        'return_or_cancel_requires_reconciliation':'返品・取消後の費用と売上の照合が必要です。',
        'financial_sale_event_stale':'現在の売却記録と金額の対応を再確認してください。',
        'revised_sale_payment_review_required':'金額訂正後の入金・精算を再確認してください。',
        'sale_status_fact_version_outdated':'金額訂正後の入金・精算を再確認してください。',
        'financial_revision_conflict':'別の記録更新があります。画面を開き直して確認してください。',
        'invalid_sale_payment_transition':'入金待ち・入金確認・精算可能の順に根拠を確認してください。',
        'actual_sale_evidence_required':'対応する売却記録と実績金額が必要です。',
        'returned_sale_cannot_settle':'返品した売上は精算可能へ変更できません。',
        'component_evidence_conflict':'同じ原資料に異なる請求候補があります。訂正元を指定してください。',
        'original_draft_required':'訂正元の明細IDを指定してください。',
        'component_owner_period_mismatch':'この所有期間より前の費用です。元のお客様の明細を確認してください。',
    }
    if code.startswith('unknown_') and code[8:] in AMOUNTS:
        return AMOUNTS[code[8:]] + 'が未入力です。'
    return labels.get(code, EXTRA_REASONS.get(code, REASONS.get(code, code)))


def _staff():
    return (current_user.is_authenticated and current_user.is_admin()
            and current_user.has_permission('merchandise') and current_user.has_permission('invoices'))


def _amount(name, *, signed=False):
    raw = request.form.get(name, '').strip()
    if not raw:
        return None
    if not raw.lstrip('-').isdigit() or raw.startswith('--'):
        raise RegistryError('integer_yen_required', '金額は円単位の整数で入力してください。')
    return yen(int(raw), signed=signed)


class FinanceWeb(RegistryWeb):
    def actor(self):
        return Actor(int(current_user.get_id()), staff=_staff())

    @contextmanager
    def db(self, write=False):
        with super().db(write=write) as registry:
            store = FinanceStore(registry)
            store.require_ready()
            yield store


def register_stage3_finance(runtime):
    app = runtime.app
    if 'finance_review' in app.blueprints:
        return app.extensions['stage3_finance']
    app.config.setdefault('STAGE3_FINANCE_ENABLED', os.environ.get('STAGE3_FINANCE_ENABLED') == '1')
    app.config.setdefault('STAGE3_TEST_LEDGER_ENABLED', False)
    service = FinanceWeb(runtime)
    bp = Blueprint('finance_review', __name__)

    def allowed(staff=False):
        def decorate(view):
            @wraps(view)
            @login_required
            def inner(*args, **kwargs):
                if app.config.get('STAGE3_FINANCE_ENABLED') is not True or app.config.get('STAGE2_INVENTORY_ENABLED') is not True:
                    abort(404)
                if staff and not _staff() or not _staff() and current_user.role != 'user':
                    abort(403)
                try:
                    return view(*args, **kwargs)
                except HTTPException:
                    raise
                except RegistryError as exc:
                    return render_template('finance_review/error.html', message=reason_label(str(exc))), exc.status
                except ValueError:
                    return render_template('finance_review/error.html', message='入力値と根拠を確認してください。'), 400
            return inner
        return decorate

    @bp.get('/finance')
    @allowed()
    def index():
        with service.db() as store:
            store.authorize(service.actor())
            items = store.registry.list_items(service.actor())
            customers = service.customers(store.registry) if _staff() else []
        return render_template('finance_review/index.html', items=items, customers=customers, staff=_staff())

    @bp.get('/finance/items/<item_id>')
    @allowed()
    def item(item_id):
        from stage3_profit import ProfitService
        from stage3_billing import BillingService
        with service.db() as store:
            detail = store.item(service.actor(), item_id)
            profit = ProfitService(store).profit(service.actor(), item_id)
            history = ProfitService(store).history(service.actor(), item_id)
            billing = BillingService(store)
            preview = billing.preview_item(service.actor(), item_id)
            drafts = billing.list_item_drafts(service.actor(), item_id)
            agreements = billing.list_agreements(service.actor(), detail['owner_user_id'])
        return render_template('finance_review/item.html', item=detail, profit=profit, facts=profit['facts'],
            history=history, preview=preview, drafts=drafts, agreements=agreements, staff=_staff())

    @bp.post('/finance/items/<item_id>/facts')
    @allowed(staff=True)
    def facts(item_id):
        from stage3_profit import ProfitService
        key = service.verify('finance_facts', item_id)
        amounts = {name: _amount(name, signed=name == 'return_adjustment_yen') for name in AMOUNTS}
        shipping = _amount('shipping_cost_yen')
        source = _text('source_reference', 1000, True)
        entries = None if shipping is None else [] if shipping == 0 else [{
            'source_id': _text('shipping_source_id', 200, True), 'amount_yen': shipping,
            'origin':'staff', 'source_reference':source}]
        with service.db(write=True) as store:
            previous = ProfitService(store).snapshot(service.actor(), item_id)
            if len(previous.get('shipping_entries') or []) > 1:
                if (shipping != previous['shipping_cost_yen'] or
                        _text('shipping_source_id', 200) != previous['shipping_entries'][0]['source_id']):
                    raise RegistryError('shipping_detail_review_required', '送料に複数の原資料があります。内訳ごとの確認が必要なため、この画面から送料は変更できません。', 409)
                entries = previous['shipping_entries']
            ProfitService(store).record_facts(service.actor(), item_id, amounts=amounts,
                channel=_text('channel', 30) or None, source_reference=source,
                occurred_at=_time(request.form.get('occurred_at')), source_key=key,
                previous_version_id=_text('previous_version_id', 100) or None,
                shipping_entries=entries, sale_event_id=_text('sale_event_id', 100) or None,
                basis=_text('basis', 20, True),
                channel_fee_includes_shipping={'separate':False, 'included':True}.get(request.form.get('shipping_scope')))
        flash('金額と根拠を記録しました。以前の版は履歴に残ります。', 'success')
        return redirect(url_for('finance_review.item', item_id=item_id))

    @bp.post('/finance/items/<item_id>/status')
    @allowed(staff=True)
    def status(item_id):
        from stage3_profit import ProfitService
        key = service.verify('finance_status', item_id)
        with service.db(write=True) as store:
            ProfitService(store).record_sales_status(service.actor(), item_id, status=_text('status', 30, True),
                occurred_at=_time(request.form.get('occurred_at')), source_key=key,
                source_reference=_text('source_reference', 1000, True),
                resolution_reference=_text('resolution_reference', 1000) or None)
        flash('売上の確認状況を記録しました。購入用資金への振替はありません。', 'success')
        return redirect(url_for('finance_review.item', item_id=item_id))

    @bp.post('/finance/items/<item_id>/drafts')
    @allowed(staff=True)
    def drafts(item_id):
        from stage3_billing import BillingService
        key = service.verify('finance_drafts', item_id)
        with service.db(write=True) as store:
            BillingService(store).create_item_drafts(service.actor(), item_id, source_key=key,
                agreement_id=_text('agreement_id', 100) or None)
        flash('発送・作業記録を確認し、請求候補を保存しました。確定・決済はしていません。', 'success')
        return redirect(url_for('finance_review.item', item_id=item_id))

    @bp.get('/finance/customers/<int:customer_id>')
    @allowed()
    def customer(customer_id):
        from stage3_billing import BillingService
        from stage3_funds import FundsService
        with service.db() as store:
            store.authorize(service.actor(), customer_id)
            billing, funds = BillingService(store), FundsService(store)
            months = billing.list_months(service.actor(), customer_id)
            agreements = billing.list_agreements(service.actor(), customer_id)
            summaries = [funds.summary(service.actor(), customer_id, mode) for mode in ('record_only', 'test')]
            records = {mode: funds.history(service.actor(), customer_id, mode) for mode in ('record_only','test')}
            items = [x for x in store.registry.list_items(service.actor()) if x['owner_user_id'] == customer_id]
        return render_template('finance_review/customer.html', customer_id=customer_id, months=months,
            summaries=summaries, records=records, items=items, agreements=agreements, staff=_staff())

    @bp.post('/finance/customers/<int:customer_id>/month')
    @allowed(staff=True)
    def month(customer_id):
        from stage3_billing import BillingService
        key = service.verify('finance_month', customer_id)
        with service.db(write=True) as store:
            BillingService(store).create_month(service.actor(), customer_id, _text('period', 7, True),
                                               source_key=key, timezone_name='Asia/Tokyo')
        flash('月次の請求下書きを保存しました。前の版は保持しています。', 'success')
        return redirect(url_for('finance_review.customer', customer_id=customer_id))

    @bp.post('/finance/customers/<int:customer_id>/component')
    @allowed(staff=True)
    def component(customer_id):
        from stage3_billing import BillingService
        key = service.verify('finance_component', customer_id)
        kind = _text('fee_kind', 30, True)
        if kind not in ('expense','digital_monthly','physical_monthly','adjustment'):
            abort(400)
        with service.db(write=True) as store:
            BillingService(store).record_component(service.actor(), customer_id, fee_kind=kind,
                amount_net_yen=_amount('amount_net_yen', signed=kind == 'adjustment'),
                tax_rate_percent=_amount('tax_rate_percent'), occurred_at=_time(request.form.get('occurred_at')),
                evidence_reference=_text('evidence_reference', 1000, True), source_key=key,
                agreement_id=_text('agreement_id', 100) or None, item_id=_text('item_id', 100) or None,
                original_draft_id=_text('original_draft_id', 100) or None)
        flash('請求候補を記録しました。月額料金の契約成立・承認を意味しません。', 'success')
        return redirect(url_for('finance_review.customer', customer_id=customer_id))

    @bp.post('/finance/customers/<int:customer_id>/agreement')
    @allowed(staff=True)
    def agreement(customer_id):
        from stage3_billing import BillingService
        from business_policy import CATALOG_VERSION
        key = service.verify('finance_agreement', customer_id)
        evidence = {name: _text(name, 1000) or None for name in (
            'contract_version_id', 'signed_document_reference', 'consent_reference', 'exception_reference', 'note')}
        for name in ('charge_start_at','consent_at'):
            evidence[name] = _time(request.form[name]).isoformat() if request.form.get(name) else None
        evidence.update(fee_version_id=CATALOG_VERSION, tax_rate_percent=10,
            rounding_method='floor', rounding_scope='invoice_per_rate', billing_timezone='Asia/Tokyo',
            billing_timezone_approved=True, work_scope_confirmed=False,
            exception_status=_text('exception_status', 30) or 'undecided')
        with service.db(write=True) as store:
            BillingService(store).save_agreement(service.actor(), customer_id, evidence=evidence, source_key=key)
        flash('契約の参照情報を下書きとして保存しました。料金の承認・課金開始はしていません。', 'success')
        return redirect(url_for('finance_review.customer', customer_id=customer_id))

    @bp.post('/finance/customers/<int:customer_id>/test-funds')
    @allowed(staff=True)
    def test_funds(customer_id):
        from stage3_funds import FundsService
        if app.config.get('STAGE3_TEST_LEDGER_ENABLED') is not True:
            abort(404)
        key = service.verify('finance_funds', customer_id)
        with service.db(write=True) as store:
            FundsService(store).post(service.actor(), customer_id, kind=_text('kind', 30, True),
                amount_yen=_amount('amount_yen'), mode='test', occurred_at=_time(request.form.get('occurred_at')),
                source_key=key, evidence_reference=_text('evidence_reference', 1000, True),
                original_record_id=_text('original_record_id', 100) or None,
                physical_item_id=_text('physical_item_id', 100) or None)
        flash('架空の資金記録を保存しました。実際のお金は動いていません。', 'success')
        return redirect(url_for('finance_review.customer', customer_id=customer_id))

    @bp.get('/finance/months/<month_id>')
    @allowed()
    def invoice(month_id):
        from stage3_billing import BillingService
        with service.db() as store:
            month = BillingService(store).get_month(service.actor(), month_id)
        return render_template('finance_review/invoice.html', month=month, staff=_staff())

    @bp.get('/finance/months/<month_id>/export.csv')
    @allowed()
    def export(month_id):
        from stage3_billing import BillingService
        with service.db() as store:
            content = BillingService(store).csv_month(service.actor(), month_id)
        return Response(content, content_type='text/csv; charset=utf-8', headers={
            'Content-Disposition':'attachment; filename="invoice-draft.csv"', 'Cache-Control':'no-store'})

    @bp.after_request
    def no_cache(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.context_processor
    def finance_context():
        return dict(stage3_finance_available=app.config.get('STAGE3_FINANCE_ENABLED') is True
                    and app.config.get('STAGE2_INVENTORY_ENABLED') is True,
                    finance_csrf=service.csrf, finance_operation=service.operation,
                    finance_amounts=AMOUNTS, finance_channels=CHANNELS, finance_buckets=BUCKETS,
                    finance_kinds=KINDS, finance_status=STATUS, finance_fee_kinds=FEE_KINDS, finance_reason=reason_label,
                    finance_now=datetime.now(JST).strftime('%Y-%m-%dT%H:%M:%S.%f'),
                    finance_test_ledger=app.config.get('STAGE3_TEST_LEDGER_ENABLED') is True)

    app.register_blueprint(bp)
    app.extensions['stage3_finance'] = service
    return service
