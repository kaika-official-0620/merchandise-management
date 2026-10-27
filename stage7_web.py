"""Staff-only pilot preparation and economics. No activation or payment routes."""
from contextlib import contextmanager
from datetime import date, datetime, time
from functools import wraps
import os
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from werkzeug.exceptions import HTTPException

from inventory_registry_web import RegistryWeb, _text
from item_registry import Actor, RegistryError, _now
from stage7_common import Stage7Store
from stage7_measurement import MeasurementService, FINANCIAL_EVENTS
from stage7_economics import EconomicsService, calculate_scenario

JST = ZoneInfo('Asia/Tokyo')
INPUT_LABELS = {
    'free_accounts': '無料利用人数', 'paid_accounts': '有料利用人数',
    'paid_monthly_price_yen': '有料1人の月額（円）', 'receipts': '月の受付点数',
    'sellthrough_bps': '最終売却率（10000で100％、5000で50％）',
    'physical_fee_per_sale_yen': '売却・発送1点あたりの開花収入（円）',
    'free_digital_per_account_yen': '無料1人のデジタル費用（円）',
    'paid_digital_per_account_yen': '有料1人のデジタル費用（円）',
    'physical_other_per_received_yen': '受付1点の人件費以外の費用（円）',
    'physical_other_per_sold_yen': '売却1点の人件費以外の費用（円）',
    'fixed_expenses_yen': '本サービスの固定費・給与除く（円）',
    'hourly_labor_yen': '時間配賦する人件費（円／人時）',
    'fixed_salary_yen': '固定計上する本サービスの給与等（円）',
    'work_minutes_per_received': '受付1点の作業人分・未売却も含む',
    'work_minutes_per_sold': '売却後1点の追加作業人分',
    'digital_payment_fee_bps': 'デジタル決済料率（100で1％）',
    'physical_payment_fee_bps': '現物決済料率（100で1％）',
    'available_staff_minutes': '本サービスに使える月の合計人分',
    'other_staff_minutes': '仕入れ・移動・問い合わせ・返品等の追加人分',
    'opening_inventory': '期首の預かり個品数', 'shelf_capacity': '利用可能な保管点数',
    'working_capital_yen': '会社が使える運転資金（顧客資金を除く・円）',
    'purchase_cash_per_received_yen': '受付1点あたり必要な会社の先払い資金（円）',
    'operating_cash_outflow_yen': '期間中に必要な会社の運営支払い（円）',
}
SCOPE_LABELS = {'free_digital':'無料利用者のデジタル費用','paid_digital':'有料利用者のデジタル費用','physical':'現物作業の費用','fixed':'固定費'}
BASIS_LABELS = {'measured':'実測','observed':'観察記録','assumption':'仮定','reference':'参考値','test':'模擬検証'}
TAX_LABELS = {'exclusive':'税抜','inclusive':'税込','unknown':'未確認'}
EVENT_LABELS = {'registration_started':'登録開始','registration_completed':'登録完了','retention_7d':'7日継続の確認','retention_30d':'30日継続の確認',
    'service_requested':'開花サービス申込','item_received':'商品受付','item_registered':'開花の登録完了','item_shipped':'発送','item_returned':'返品','unsold_observed':'未売却の確認',
    'support':'問い合わせ対応','major_task_attempt':'主要操作の観察','paid_offer':'有料プランの提示','paid_consent':'有料利用の同意記録','payment_success':'支払成功の申告記録','renewal':'有料更新の申告記録','cancellation':'解約の申告記録'}
EVENT_LABELS.update(billing_error='誤請求の発見',cross_customer_access='顧客間データ閲覧の発見',duplicate_allocation='同一個品の二重引当の発見',duplicate_payment='重複決済の発見')
COST_LABELS = {'image_storage':'画像保管','database':'DB','logs':'ログ','backup':'バックアップ','notifications':'通知','payments':'決済','support':'問い合わせ','hosting':'サーバー',
    'materials':'資材','shipping':'送料','staff_salary_fixed':'給与等の固定計上','staff_time_allocated':'人件費の時間配賦','other':'その他'}
RESOURCE_LABELS = {'image_count':'画像枚数','image_bytes':'画像容量（bytes）','database_bytes':'DB容量（bytes）','log_bytes':'ログ容量（bytes）','backup_bytes':'バックアップ容量（bytes）','notification_attempts':'通知の送信試行数','payment_transactions':'決済取引数','support_minutes':'問い合わせ対応分数'}
METRIC_LABELS = {'expected_sold':'受付分の最終売却見込み点数','expected_unsold':'受付分の未売却見込み点数','digital_revenue_yen':'月額収入（円）','physical_revenue_yen':'受付集団の最終的な現物料金収入（円）','total_revenue_yen':'安定運用を仮定した月の収入計（円）','digital_cost_yen':'デジタル費用（円）','physical_nonlabor_cost_yen':'現物費用・人件費を除く（円）','labor_cost_yen':'人件費（円）','payment_fees_yen':'決済手数料（円）','total_cost_yen':'安定運用を仮定した月の費用計（円）','expected_profit_yen':'安定運用を仮定した月の利益（円）','receipt_work_minutes':'受付分の作業人分','sale_work_minutes':'売却分の追加作業人分','other_staff_minutes':'その他の作業人分','required_staff_minutes':'安定運用を仮定した月の必要人分','projected_inventory':'受付集団が最終売却後に残る点数＋期首在庫','peak_inventory':'全受付が出庫前に重なった最大保管点数','purchase_cash_required_yen':'会社の仕入先払い資金（円）','required_working_capital_yen':'必要な会社資金（円）'}


def integer(name, required=False):
    raw = _text(name, 30, required)
    if not raw:
        return None
    if not raw.isascii() or not raw.isdecimal():
        abort(400, description='件数・金額・時間は0以上の整数で入力してください。')
    return int(raw)


def timestamp(name, required=True):
    raw = _text(name, 50, required)
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
        return (value.replace(tzinfo=JST) if value.tzinfo is None else value).isoformat()
    except ValueError:
        abort(400, description='日時を確認してください。')


class PilotWeb(RegistryWeb):
    def actor(self):
        return Actor(int(current_user.get_id()), staff=True)

    @contextmanager
    def db(self, write=False):
        with super().db(write) as registry:
            store = Stage7Store(registry)
            store.require_ready()
            yield store

    def clock(self):
        return self.app.extensions.get('stage7_clock', _now)()

    def period(self):
        return datetime.fromisoformat(self.clock()).astimezone(JST).strftime('%Y-%m')

    def measurement(self, store):
        return MeasurementService(store, clock=self.clock)


def register_stage7_web(runtime):
    app = runtime.app
    if 'pilot_review' in app.blueprints:
        return app.extensions['stage7_web']
    app.config.setdefault('STAGE7_ENABLED', os.environ.get('STAGE7_ENABLED') == '1')
    service = PilotWeb(runtime)
    bp = Blueprint('pilot_review', __name__)

    def enabled():
        return all(app.config.get(name) is True for name in (
            'STAGE2_INVENTORY_ENABLED','STAGE3_FINANCE_ENABLED','STAGE4_COMMERCE_ENABLED',
            'STAGE5_PAYMENTS_ENABLED','STAGE6_ENABLED','STAGE7_ENABLED'))

    def finance():
        return current_user.is_admin() and current_user.has_permission('invoices')

    def allowed(financial=False):
        def decorate(view):
            @wraps(view)
            @login_required
            def wrapped(*args, **kwargs):
                if not enabled():
                    abort(404)
                if not current_user.is_admin() or not current_user.has_permission('merchandise'):
                    abort(403)
                if financial and not finance():
                    abort(403)
                try:
                    return view(*args, **kwargs)
                except HTTPException:
                    raise
                except RegistryError as error:
                    return render_template('pilot_review/error.html', staff=True,
                        message='入力内容、権限、前の記録との関係を確認してください。未確認の条件では確定できません。',
                        code=error.code), error.status
            return wrapped
        return decorate

    @bp.get('/pilot-review')
    @allowed()
    def index():
        with service.db() as store:
            campaigns = service.measurement(store).overview(service.actor())
        return render_template('pilot_review/index.html', campaigns=campaigns, staff=True)

    @bp.post('/pilot-review/campaigns')
    @allowed()
    def create_campaign():
        key = service.verify('stage7_campaign')
        with service.db(True) as store:
            campaign = service.measurement(store).create_campaign(service.actor(),
                title=_text('title',150,True), phase=_text('phase',30,True),
                planned_start_at=timestamp('planned_start_at'), planned_end_at=timestamp('planned_end_at'),
                paid=request.form.get('paid') == 'yes', prerequisite_references={}, source_key=key)
        flash('実証計画を下書きとして保存しました。募集・課金・通知は開始していません。','success')
        return redirect(url_for('pilot_review.campaign', campaign_id=campaign['campaign_id']))

    @bp.get('/pilot-review/campaigns/<campaign_id>')
    @allowed()
    def campaign(campaign_id):
        with service.db() as store:
            api = service.measurement(store)
            detail = api.detail(service.actor(), campaign_id)
            report = api.report(service.actor(), campaign_id)
            customers = service.customers(store.registry)
        return render_template('pilot_review/campaign.html', detail=detail, report=report, customers=customers, staff=True)

    @bp.post('/pilot-review/campaigns/<campaign_id>/participants')
    @allowed()
    def enroll(campaign_id):
        key = service.verify('stage7_enroll',campaign_id)
        with service.db(True) as store:
            service.measurement(store).enroll(service.actor(), campaign_id, integer('customer_id',True),
                experience=_text('experience',40,True), source_key=key)
        return redirect(url_for('pilot_review.campaign',campaign_id=campaign_id))

    @bp.post('/pilot-review/campaigns/<campaign_id>/observations')
    @allowed()
    def observe(campaign_id):
        key = service.verify('stage7_observe',campaign_id)
        with service.db(True) as store:
            service.measurement(store).observe(service.actor(),campaign_id,integer('customer_id',True),
                event_type=_text('event_type',60,True), occurred_at=timestamp('occurred_at'),
                basis=_text('basis',30,True), execution_mode=_text('execution_mode',30,True),
                evidence_reference=_text('evidence_reference',500,True),source_key=key,
                outcome={'yes':True,'no':False,'':None}.get(_text('outcome',40),'invalid'),duration_seconds=integer('duration_seconds'),
                correction_of=_text('correction_of',100) or None,
                correction_reason=_text('correction_reason',500) or None)
        flash('観察記録を保存しました。実際の業務・支払いの状態は変更していません。','success')
        return redirect(url_for('pilot_review.campaign',campaign_id=campaign_id))

    @bp.get('/pilot-review/cohorts')
    @allowed()
    def cohorts():
        result = None
        start,end = request.args.get('start',''),request.args.get('end','')
        if start or end:
            try:
                begin = datetime.combine(date.fromisoformat(start),time.min,JST).isoformat()
                finish = datetime.combine(date.fromisoformat(end),time.min,JST).isoformat()
            except (ValueError,TypeError):
                abort(400)
            with service.db() as store:
                result = service.measurement(store).receipt_cohort(service.actor(),start_at=begin,end_at=finish)
        return render_template('pilot_review/cohorts.html',result=result,start=start,end=end,staff=True)

    @bp.get('/pilot-review/costs')
    @allowed(financial=True)
    def costs():
        period=request.args.get('period') or service.period()
        denominators={}
        for key in ('free_digital','paid_digital','physical'):
            raw=request.args.get(key,'')
            if raw:
                if not raw.isascii() or not raw.isdecimal() or len(raw)>12:
                    abort(400)
                denominators[key]=int(raw)
        with service.db() as store:
            result=EconomicsService(store).cost_summary(service.actor(),period,denominators=denominators)
        return render_template('pilot_review/costs.html',result=result,period=period,denominators=denominators,staff=True)

    @bp.post('/pilot-review/costs')
    @allowed(financial=True)
    def record_cost():
        key=service.verify('stage7_cost')
        period=_text('period',7,True)
        with service.db(True) as store:
            EconomicsService(store).record_cost(service.actor(),period=period,scope=_text('scope',40,True),
                category=_text('category',50,True),amount_yen=integer('amount_yen'),basis=_text('basis',30,True),
                tax_basis=_text('tax_basis',30,True),reference=_text('reference',500,True),
                source_key=key,note=_text('note',1000))
        return redirect(url_for('pilot_review.costs',period=period))

    @bp.get('/pilot-review/costs/<entry_id>')
    @allowed(financial=True)
    def cost_history(entry_id):
        with service.db() as store:
            entry=EconomicsService(store).history(service.actor(),entry_id)
        return render_template('pilot_review/cost_history.html',entry=entry,staff=True)

    @bp.post('/pilot-review/costs/<entry_id>/corrections')
    @allowed(financial=True)
    def correct_cost(entry_id):
        key=service.verify('stage7_correct_cost',entry_id)
        _text('period',7,True)
        with service.db(True) as store:
            result=EconomicsService(store).correct_cost(service.actor(),entry_id,amount_yen=integer('amount_yen'),
                basis=_text('basis',30,True),tax_basis=_text('tax_basis',30,True),reference=_text('reference',500,True),
                reason=_text('reason',500,True),source_key=key)
        return redirect(url_for('pilot_review.costs',period=result['period']))

    @bp.post('/pilot-review/resources')
    @allowed(financial=True)
    def record_resource():
        key=service.verify('stage7_resource')
        period=_text('period',7,True)
        with service.db(True) as store:
            EconomicsService(store).record_resource(service.actor(),period=period,scope=_text('scope',40,True),
                metric=_text('metric',50,True),quantity=integer('quantity'),basis=_text('basis',30,True),
                reference=_text('reference',500,True),source_key=key)
        return redirect(url_for('pilot_review.costs',period=period))

    @bp.post('/pilot-review/resources/<entry_id>/corrections')
    @allowed(financial=True)
    def correct_resource(entry_id):
        key=service.verify('stage7_correct_resource',entry_id)
        _text('period',7,True)
        with service.db(True) as store:
            result=EconomicsService(store).correct_resource(service.actor(),entry_id,quantity=integer('quantity'),
                basis=_text('basis',30,True),reference=_text('reference',500,True),reason=_text('reason',500,True),source_key=key)
        return redirect(url_for('pilot_review.costs',period=result['period']))

    @bp.route('/pilot-review/scenarios',methods=['GET','POST'])
    @allowed(financial=True)
    def scenarios():
        result=None
        inputs={}
        period=request.args.get('period') or service.period()
        if request.method=='POST':
            key=service.verify('stage7_scenario')
            inputs={name:integer(name) for name in INPUT_LABELS}
            inputs.update(labor_mode=_text('labor_mode',40,True),tax_basis=_text('tax_basis',30,True),
                timing_basis=_text('timing_basis',40) or 'unknown',basis=_text('basis',30,True),reference=_text('reference',500,True))
            result=calculate_scenario(inputs)
            period=_text('period',7,True)
            if request.form.get('save')=='yes':
                with service.db(True) as store:
                    EconomicsService(store).save_scenario(service.actor(),name=_text('name',150,True),
                        period=period,inputs=inputs,source_key=key)
                flash('比較案を保存しました。料金表や実請求には反映していません。','success')
        with service.db() as store:
            saved=EconomicsService(store).scenarios(service.actor(),period=period)
        return render_template('pilot_review/scenarios.html',result=result,inputs=inputs,saved=saved,period=period,staff=True)

    @bp.after_request
    def private(response):
        response.headers.update({'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'})
        return response

    @app.context_processor
    def context():
        return dict(stage7_available=enabled(),stage7_csrf=service.csrf,stage7_operation=service.operation,
            stage7_finance=current_user.is_authenticated and finance(),stage7_inputs=INPUT_LABELS,
            stage7_scopes=SCOPE_LABELS,stage7_basis=BASIS_LABELS,stage7_tax=TAX_LABELS,
            stage7_events={key:label for key,label in EVENT_LABELS.items() if key not in FINANCIAL_EVENTS or (current_user.is_authenticated and finance())},
            stage7_categories=COST_LABELS,stage7_resources=RESOURCE_LABELS,metric_labels=METRIC_LABELS,
            check_labels={'profit_positive':'利益が正か','staff_capacity':'作業時間に収まるか','shelf_capacity':'棚容量に収まるか','funding_capacity':'会社の支払資金に収まるか'})

    app.register_blueprint(bp)
    app.extensions['stage7_web']=service
    return service
