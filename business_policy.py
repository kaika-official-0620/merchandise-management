"""Stage 1 business vocabulary and review-only policy; no I/O or payment calls.

Evidence objects must come from authorized, verified records, never unchecked
request JSON. Eligibility is a review result, not proof that a PSP or contract
has authorized a transaction. All catalog entries below remain inactive drafts.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Tuple
from zoneinfo import ZoneInfo


CATALOG_VERSION = 'proposal-2026-10-22-v3'
CURRENCY = 'JPY'
PROPOSED_BILLING_TIMEZONE = 'Asia/Tokyo'  # Operator chose JST; prices remain drafts.


class CountKind(str, Enum):
    SELF_ACTIVE_ITEMS = 'self_active_items'
    KAIKA_MONTHLY_REGISTRATIONS = 'kaika_monthly_registrations'
    ACTUAL_WORK_EVENTS = 'actual_work_events'
    MONTHLY_SHIPPED_ITEMS = 'monthly_shipped_items'
    INVOICE_LINES = 'invoice_lines'


class FundBucket(str, Enum):
    PURCHASE_FUNDS = 'purchase_funds'
    PURCHASE_RESERVATIONS = 'purchase_reservations'
    SALES_UNSETTLED = 'sales_unsettled'
    FEES_PAYABLE = 'fees_payable'


class FundEventKind(str, Enum):
    DEPOSIT = 'deposit'
    PURCHASE = 'purchase'
    RESERVE = 'reserve'
    RELEASE = 'release'
    REFUND = 'refund'
    SALES_SETTLEMENT = 'sales_settlement'
    INVOICE = 'invoice'
    PAYMENT = 'payment'
    CORRECTION = 'correction'


def integer_yen(value, *, allow_negative=False):
    """Unknown is None; bool, float and numeric strings are not yen values."""
    if value is None:
        return None
    if type(value) is not int or (value < 0 and not allow_negative):
        raise ValueError('金額は整数円で指定してください。不明はNoneです。')
    return value


def aware_time(value, label='日時'):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(label + 'にはタイムゾーン付き日時が必要です。')
    return value


def period_key(occurred_at, timezone_name):
    """Explicit zone for simulations. This does not approve a closing policy."""
    aware_time(occurred_at)
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ValueError('締めのタイムゾーンを明示してください。')
    return occurred_at.astimezone(ZoneInfo(timezone_name)).strftime('%Y-%m')


def draft_catalog():
    """Fresh JSON-compatible data; editing a response cannot alter the draft."""
    plans = []
    for code, label, net, limit in (
        ('light', 'ライト', 2980, 20), ('standard', 'スタンダード', 5980, 50),
        ('pro', 'プロ', 9800, 100), ('business', 'ビジネス', 19800, 300),
        ('enterprise', '個別協議', None, None),
    ):
        plans.append(dict(code=code, label=label, status='draft', enabled=False,
                          monthly_net_yen=net, monthly_gross_example_yen=None if net is None else net * 110 // 100,
                          kaika_monthly_registration_limit=limit, self_active_item_limit=None,
                          self_monthly_registration_limit=None,
                          self_limit_status='undecided', work_fees_included=False))
    return dict(version=CATALOG_VERSION, currency=CURRENCY, status='draft', enabled=False,
                chargeable=False, tax_example_percent=10,
                free=dict(status='draft', enabled=False, active_individual_items=100,
                          monthly_registration_limit=None, monthly_limit_kind='no_limit_proposed',
                          exclude_sold_items=True, keep_sales_history=True,
                          photo_limits=None, photo_retention_policy=None),
                paid_plans=plans, period_timezone_proposal=PROPOSED_BILLING_TIMEZONE,
                period_timezone_approved=True,
                rounding_policy={'method': 'floor', 'scope': 'invoice_per_rate'},
                calculation_policy_source='operator_answer_stage3_2026-10-22',
                sales_fee_tax_treatment='exclusive',
                sales_fee_tax_policy_source='operator_answer_2026-10-22',
                customer_contract_fee_approval=False, overflow_policy=None,
                work_bundle=dict(trigger_proposal='sold_and_shipped',
                                 scope_confirmed=False, unsold_charge_proposal=False,
                                 pre_sale_cancel_charge_proposal=False,
                                 mixed_pre_start_work_policy='post_start_work_only',
                                 mixed_post_start_allocation_yen=None,
                                 mixed_post_start_allocation_status='undecided', return_policy=None),
                live_payment_enabled=False)


def draft_sale_quote(sale_gross_yen, channel):
    """Faithful source bands for review, never a final invoice calculator."""
    amount = integer_yen(sale_gross_yen)
    if amount is None or amount < 1:
        raise ValueError('販売価格は1円以上の整数円で指定してください。')
    channels = ('wholesale', 'multichannel', 'dealer_auction', 'self_sale_shipping', 'live')
    if channel not in channels:
        raise ValueError('販売方法を確認してください。')
    work = 500 if amount < 20000 else 650 if amount < 50000 else 800 if amount < 100000 else 1000
    tier = 0 if amount < 20000 else 1 if amount < 50000 else 2 if amount < 100000 else 3 if amount < 200000 else 4
    sales = {'wholesale': (500, 1000, 1500, 2000, 3000),
             'multichannel': (300, 400, 500, 500, 500),
             'dealer_auction': (300, 400, 500, 700, 700)}
    sales_net = sales[channel][tier] if channel in sales else 0 if channel == 'self_sale_shipping' else None
    return dict(version=CATALOG_VERSION, status='draft', chargeable=False, currency=CURRENCY,
                sale_price_gross_yen=amount, work_bundle_net_yen=work,
                work_bundle_gross_example_yen=work * 110 // 100,
                sales_fee_source_yen=sales_net, sales_fee_net_yen=sales_net,
                sales_fee_gross_example_yen=None if sales_net is None else sales_net * 110 // 100,
                sales_fee_tax_treatment='exclusive', tax_example_percent=10,
                customer_contract_fee_approval=False, total_charge_yen=None,
                total_charge_gross_example_yen=None if sales_net is None else (work + sales_net) * 110 // 100,
                example_assumes_full_post_start_bundle=True,
                live_rate_range_percent=(10, 20) if channel == 'live' else None,
                channel_authorization_verified=False,
                wholesale_shipping_payer_proposal=(('customer' if amount <= 30000 else 'kaika')
                                                    if channel == 'wholesale' else None))


@dataclass(frozen=True)
class WorkEvidence:
    work_id: str
    physical_item_id: str
    kind: str
    performed_at: datetime
    duration_seconds: Optional[int] = None
    source_reference: Optional[str] = None

    def __post_init__(self):
        aware_time(self.performed_at, '作業日時')
        if self.duration_seconds is not None and (type(self.duration_seconds) is not int or self.duration_seconds < 0):
            raise ValueError('実測作業秒数は0以上の整数またはNoneです。経験値を補完しません。')


@dataclass(frozen=True)
class ContractEvidence:
    customer_id: int
    contract_version_id: Optional[str] = None
    fee_version_id: Optional[str] = None
    signed_document_reference: Optional[str] = None
    consent_reference: Optional[str] = None
    consent_at: Optional[datetime] = None
    charge_start_at: Optional[datetime] = None
    exception_status: str = 'undecided'  # none / exempt / adjusted need a decision.
    exception_reference: Optional[str] = None


@dataclass(frozen=True)
class FeePolicyEvidence:
    version_id: str
    status: str = 'draft'
    approval_reference: Optional[str] = None
    approved_at: Optional[datetime] = None
    effective_from: Optional[datetime] = None
    effective_until: Optional[datetime] = None
    amount_yen: Optional[int] = None
    currency: str = CURRENCY
    tax_treatment: Optional[str] = None  # exclusive / inclusive / exempt / outside
    tax_rate_percent: Optional[int] = None
    rounding_method: Optional[str] = None  # floor / ceil / half_up
    rounding_scope: Optional[str] = None  # invoice_per_rate / per_line
    billing_timezone: Optional[str] = None
    billing_timezone_approved: bool = False
    work_scope_confirmed: bool = False


@dataclass(frozen=True)
class InvoiceContext:
    customer_id: int
    physical_item_id: str
    fee_kind: str
    contract: Optional[ContractEvidence] = None
    fee_policy: Optional[FeePolicyEvidence] = None
    event_id: Optional[str] = None
    source_reference: Optional[str] = None
    charge_event_at: Optional[datetime] = None
    sold_at: Optional[datetime] = None
    shipped_at: Optional[datetime] = None
    works: Tuple[WorkEvidence, ...] = field(default_factory=tuple)
    return_state: str = 'undecided'  # no_return / pending / resolved
    return_resolution_reference: Optional[str] = None
    adjustment_review_complete: bool = False
    cancelled: bool = False
    already_billed_event: bool = False
    channel_authorization_reference: Optional[str] = None


REASONS = {
    'contract_missing': '適用する顧客契約が未確認です。',
    'customer_mismatch': '契約の顧客と請求先が一致しません。',
    'contract_version_missing': '署名済み契約版・原本が未確認です。',
    'consent_missing': '料金への同意記録・日時が未確認です。',
    'charge_start_missing': '請求開始日時が未確定です。',
    'fee_policy_missing': '適用する料金表版が未確認です。',
    'fee_policy_draft': '料金表は承認済み・有効な条件ではありません。',
    'fee_version_mismatch': '契約が同意する料金表版と計算対象が一致しません。',
    'unknown_amount': '請求額が未確定です。0円として扱えません。',
    'invalid_amount': '金額の単位・形式を確認してください。',
    'tax_unknown': '税区分または税率が未確定です。',
    'rounding_unknown': '税端数の方法・集計単位が未確定です。',
    'period_unknown': '締めのタイムゾーンが承認されていません。',
    'invalid_time': '時刻にはタイムゾーン付きの実日時が必要です。',
    'policy_not_effective': '料金表の適用期間外、または適用開始が未確認です。',
    'event_missing': '課金イベント・個品・原資料との対応が未確認です。',
    'unknown_fee_kind': 'この料金種別の確定条件は未実装です。',
    'before_consent': '料金への同意前のイベントです。',
    'before_charge_start': '請求開始前のイベントは請求できません。',
    'exception_unresolved': '免除・例外の有無または扱いが未確定です。',
    'exempt_not_chargeable': '免除対象です。実請求に進めません。',
    'adjustment_requires_review': '調整額・原請求との対応を確認する必要があります。',
    'not_sold_and_shipped': '売却と発送の両方が成立していません。',
    'invalid_shipping_sequence': '売却・発送・課金イベントの時刻の対応が不正です。',
    'work_scope_unknown': '一式料金に含まれる作業範囲が未確定です。',
    'work_evidence_missing': '実作業・原資料との対応が不足しています。',
    'work_item_mismatch': '作業の個品が請求対象と一致しません。',
    'work_after_shipping': '発送後の作業が発送一式に含まれています。',
    'pre_start_work_not_billable': '請求開始前の作業は請求対象外です。',
    'mixed_start_bundle_unresolved': '開始後に実施した作業分だけが対象ですが、その金額・配分が未確定です。',
    'pre_consent_work_unresolved': '料金への同意前に行った作業について、請求する明示合意が未確認です。',
    'sale_before_consent_unresolved': '料金への同意前に売却した商品について、請求する明示合意が未確認です。',
    'return_unresolved': '返品の有無・理由・料金調整が未確認です。',
    'cancelled_requires_correction': '取消は元記録に対応する訂正として扱う必要があります。',
    'already_billed': '同じイベントを請求済みです。二重計上はできません。',
    'channel_not_authorized': 'この販路・運用の許可が未確認です。',
    'sale_before_start_unresolved': '請求開始前に売却した商品の販売料は適用確認が必要です。',
    'event_state_unknown': '取消・二重計上の確認状態が未確定です。',
}


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    reason_codes: Tuple[str, ...]
    messages: Tuple[str, ...]
    can_record_work: bool = True
    can_save_draft: bool = True
    real_payment_enabled: bool = False

    def as_dict(self):
        return dict(allowed=self.allowed, reason_codes=list(self.reason_codes), messages=list(self.messages),
                    can_record_work=self.can_record_work, can_save_draft=self.can_save_draft,
                    real_payment_enabled=self.real_payment_enabled)


def evaluate_invoice_eligibility(context):
    """Validate one prospective line without persistence or external effects.

    A successful result only means supplied evidence is internally consistent.
    Stage 1 stores drafts only; finalization and payment executors do not exist.
    """
    if not isinstance(context, InvoiceContext):
        raise TypeError('InvoiceContextが必要です。')
    reasons = []
    add = lambda code: reasons.append(code) if code not in reasons else None
    def timestamp(value):
        if value is None:
            return None
        try:
            return aware_time(value)
        except ValueError:
            add('invalid_time')
            return None
    event = timestamp(context.charge_event_at)
    contract, policy = context.contract, context.fee_policy
    if not context.event_id or not context.source_reference or not context.physical_item_id or not event:
        add('event_missing')
    # Monthly fees/expenses require period/receipt-specific conditions in stage 3.
    if context.fee_kind not in ('work_bundle', 'sales_fee'):
        add('unknown_fee_kind')
    start = consent = None
    if contract is None:
        add('contract_missing')
    else:
        if contract.customer_id != context.customer_id:
            add('customer_mismatch')
        if not contract.contract_version_id or not contract.signed_document_reference:
            add('contract_version_missing')
        consent = timestamp(contract.consent_at)
        if not contract.consent_reference or not consent:
            add('consent_missing')
        start = timestamp(contract.charge_start_at)
        if not start:
            add('charge_start_missing')
        if contract.exception_status not in ('none', 'exempt', 'adjusted'):
            add('exception_unresolved')
        elif contract.exception_status == 'exempt':
            add('exempt_not_chargeable')
        elif contract.exception_status == 'adjusted':
            # Adjustments require their own evidenced lines, not a flag bypass.
            add('adjustment_requires_review')
        if event and consent and event < consent:
            add('before_consent')
        if event and start and event < start:
            add('before_charge_start')
    if policy is None:
        add('fee_policy_missing')
    else:
        approved = timestamp(policy.approved_at)
        effective = timestamp(policy.effective_from)
        until = timestamp(policy.effective_until)
        if policy.status != 'approved' or not policy.approval_reference or not approved or (event and approved > event):
            add('fee_policy_draft')
        if not contract or not contract.fee_version_id or contract.fee_version_id != policy.version_id:
            add('fee_version_mismatch')
        if not effective or (event and (event < effective or (until and event >= until))) or (until and effective and until <= effective):
            add('policy_not_effective')
        try:
            if integer_yen(policy.amount_yen) is None:
                add('unknown_amount')
            if policy.currency != CURRENCY:
                add('invalid_amount')
        except ValueError:
            add('invalid_amount')
        if policy.tax_treatment not in ('exclusive', 'inclusive', 'exempt', 'outside'):
            add('tax_unknown')
        elif policy.tax_treatment in ('exclusive', 'inclusive'):
            if type(policy.tax_rate_percent) is not int or policy.tax_rate_percent not in (8, 10):
                add('tax_unknown')
        elif policy.tax_rate_percent is not None:
            add('tax_unknown')
        if policy.rounding_method not in ('floor', 'ceil', 'half_up') or policy.rounding_scope not in ('invoice_per_rate', 'per_line'):
            add('rounding_unknown')
        try:
            if policy.billing_timezone_approved is not True:
                raise ValueError()
            if not isinstance(policy.billing_timezone, str) or not policy.billing_timezone:
                raise ValueError()
            ZoneInfo(policy.billing_timezone)
        except (ValueError, KeyError):
            add('period_unknown')
    sold, shipped = timestamp(context.sold_at), timestamp(context.shipped_at)
    if consent and sold and sold < consent:
        add('sale_before_consent_unresolved')
    if consent and any(work.performed_at < consent for work in context.works):
        add('pre_consent_work_unresolved')
    if context.fee_kind == 'work_bundle':
        if not sold or not shipped:
            add('not_sold_and_shipped')
        elif shipped < sold or (event and event != shipped):
            add('invalid_shipping_sequence')
        if not policy or policy.work_scope_confirmed is not True:
            add('work_scope_unknown')
        if not context.works or any(not w.work_id or not w.source_reference for w in context.works):
            add('work_evidence_missing')
        if any(w.physical_item_id != context.physical_item_id for w in context.works):
            add('work_item_mismatch')
        if shipped and any(w.performed_at > shipped for w in context.works):
            add('work_after_shipping')
        if start and context.works:
            earlier = [w.performed_at < start for w in context.works]
            if all(earlier):
                add('pre_start_work_not_billable')
            elif any(earlier):
                add('mixed_start_bundle_unresolved')
    if context.fee_kind == 'sales_fee':
        if not sold or not shipped:
            add('not_sold_and_shipped')
        elif shipped < sold or (event and event != shipped):
            add('invalid_shipping_sequence')
        if not context.channel_authorization_reference:
            add('channel_not_authorized')
        if sold and start and sold < start:
            add('sale_before_start_unresolved')
    if context.return_state == 'resolved':
        if not context.return_resolution_reference or context.adjustment_review_complete is not True:
            add('return_unresolved')
    elif context.return_state != 'no_return':
        add('return_unresolved')
    if context.cancelled:
        add('cancelled_requires_correction')
    if context.already_billed_event:
        add('already_billed')
    if type(context.cancelled) is not bool or type(context.already_billed_event) is not bool:
        add('event_state_unknown')
    return Eligibility(not reasons, tuple(reasons), tuple(REASONS[reason] for reason in reasons))


@dataclass(frozen=True)
class FundingApproval:
    capability: str
    regulatory_reference: Optional[str] = None
    psp_reference: Optional[str] = None
    contract_reference: Optional[str] = None
    operator_approval_reference: Optional[str] = None
    approved_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


def funding_capability_status(capability, approval=None, *, now=None, enabled=False):
    """Evidence review only. No runtime endpoint consumes this opt-in flag yet."""
    reasons = []
    permitted = ('bank_purchase_deposit', 'card_purchase_deposit', 'unused_purchase_refund', 'sales_settlement')
    if capability not in permitted:
        reasons.append('unsupported_funding_capability')
    if enabled is not True:
        reasons.append('funding_disabled')
    if not isinstance(approval, FundingApproval) or approval.capability != capability:
        reasons.append('funding_approval_missing')
    else:
        for name in ('regulatory_reference', 'psp_reference', 'contract_reference', 'operator_approval_reference'):
            if not getattr(approval, name):
                reasons.append(name + '_missing')
        try:
            current = aware_time(now, '確認日時')
            approved = aware_time(approval.approved_at, '承認日時')
            if approved > current or (approval.expires_at is not None and aware_time(approval.expires_at) <= current):
                reasons.append('funding_approval_not_current')
        except ValueError:
            reasons.append('funding_approval_time_missing')
    return Eligibility(not reasons, tuple(reasons), tuple('資金機能の確認が必要です: ' + reason for reason in reasons))


def example_invoice_context(case_name):
    """Fixed fictional examples for a read-only review page, always draft.

    Negative customer ID deliberately cannot match normal production customer
    IDs. This function accepts no customer, price, consent or approval inputs.
    """
    cases = ('normal', 'before_start', 'mixed_start', 'return_unresolved')
    if case_name not in cases:
        raise ValueError('確認用ケースを選択してください。')
    moment = lambda day: datetime(2026, 11, day, 3, 0, tzinfo=timezone.utc)
    contract = ContractEvidence(customer_id=-2200001, contract_version_id='example-contract',
        fee_version_id='example-draft-fee', signed_document_reference='example-only-not-signed',
        consent_reference='example-only-not-consent', consent_at=moment(1), charge_start_at=moment(10),
        exception_status='none')
    policy = FeePolicyEvidence(version_id='example-draft-fee', amount_yen=650,
        effective_from=moment(10), tax_treatment='exclusive', tax_rate_percent=10,
        billing_timezone=PROPOSED_BILLING_TIMEZONE)
    days = (11, 12) if case_name not in ('before_start', 'mixed_start') else (8, 9) if case_name == 'before_start' else (9, 11)
    works = tuple(WorkEvidence('example-work-' + str(n), 'example-item', kind, moment(day),
                              source_reference='example-only-work')
                  for n, (kind, day) in enumerate(zip(('photography', 'registration'), days)))
    return InvoiceContext(customer_id=-2200001, physical_item_id='example-item', fee_kind='work_bundle',
        contract=contract, fee_policy=policy, event_id='example-shipment', source_reference='example-only-shipment',
        charge_event_at=moment(12), sold_at=moment(11), shipped_at=moment(12), works=works,
        return_state='pending' if case_name == 'return_unresolved' else 'no_return')
