"""Integer-yen calculations and immutable, review-only financial documents.

No real invoice finalization or payment endpoint exists. Customer agreements are
saved as drafts; approved evidence is supported only in explicitly seeded test
fixtures. A proposal total is never a payable balance.
"""
import csv
from datetime import datetime
import hashlib
import io
import json
import re
import uuid
from zoneinfo import ZoneInfo

from business_policy import (CATALOG_VERSION, ContractEvidence, FeePolicyEvidence,
    InvoiceContext, WorkEvidence, draft_catalog, draft_sale_quote,
    evaluate_invoice_eligibility, period_key)
from item_registry import RegistryError, _json, _now, _text, _time
from stage3_common import yen, financial_event_order_ambiguous


FEE_KINDS = ('work_bundle', 'sales_fee', 'expense', 'digital_monthly', 'physical_monthly', 'adjustment')
EXTRA_REASONS = {
    'sale_amount_unknown': '販売額の確定記録がありません。',
    'sale_fact_not_actual': '販売額は見込値です。',
    'sale_event_mismatch': '販売額と今回の売却イベントの対応が未確認です。',
    'sale_basis_unconfirmed': '料金帯に使う税込商品価格の範囲が未確認です。',
    'channel_unknown': '販売方法が未確認です。',
    'custody_unknown': '発送時の保管者が未確認です。',
    'self_managed_no_kaika_fee': '自己管理のみの商品へ開花作業料を付与しません。',
    'source_changed_after_snapshot': '金額・作業・返品の記録が変わりました。元の明細を残して訂正を確認してください。',
    'component_terms_unconfirmed': '月額・実費・調整の適用条件と原資料の確認が必要です。',
    'monthly_terms_unconfirmed': '途中月・0件月・超過の月額条件は未確定です。',
    'correction_requires_review': '元明細・返品理由・調整額の確認が必要です。',
    'uncollected_shipments': '発送済み商品の料金下書きに未作成のものがあります。',
    'no_agreed_period': '表示上の暦月です。顧客が同意した締め条件は未確認です。',
    'conflicting_rounding': '税端数の方法が未確定または明細間で一致していません。',
    'rounding_scope_invalid': '消費税は請求書全体で税率ごとに1回計算する必要があります。',
    'return_cycle_requires_review': '返品後の再販売です。以前の作業との重複を確認してください。',
    'unresolved_adjustments': '未確定の明細・調整があるため請求合計を確定できません。',
    'monthly_component_missing': '月額の明細または月額対象外の適用確認がありません。表示済み作業料だけでは月の全額になりません。',
    'invoice_new_lines_available': 'この版の作成後に明細が追加されています。新しい版を作成して確認してください。',
    'invoice_superseded': 'この下書きには新しい版があります。履歴として表示しています。',
    'credit_tax_rule_unconfirmed': '税率別の合計がマイナスです。元請求の税額配分と返金時の端数規則を確認してください。',
    'financial_event_order_unconfirmed': '旧記録の売却・発送・返品の順序を特定できません。原資料との照合が必要です。',
}


def _dt(value):
    return datetime.fromisoformat(_time(value)) if value is not None else None


def _rate(value):
    if value is not None and (type(value) is not int or value not in (8, 10)):
        raise RegistryError('tax_rate_invalid')
    return value


def round_fraction(numerator, denominator, method):
    """No binary floats. half_up rounds ties away from zero, including credits."""
    if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
        raise ValueError('Integer numerator and positive denominator required')
    if method == 'floor':
        return numerator // denominator
    if method == 'ceil':
        return -(-numerator // denominator)
    if method == 'half_up':
        sign = -1 if numerator < 0 else 1
        quotient, remainder = divmod(abs(numerator), denominator)
        return sign * (quotient + int(remainder * 2 >= denominator))
    raise RegistryError('rounding_unknown')


def invoice_totals(lines, rounding_method=None):
    """Aggregate tax once per rate across the invoice, not once per product.

    Unknown amounts/tax/method keep the total unknown; known subtotals are named
    explicitly so they cannot accidentally be presented as the full invoice.
    Input amounts are tax-exclusive JPY, or exempt/outside amounts.
    """
    groups = {}
    unknown = []
    known_net = 0
    for position, line in enumerate(lines):
        amount = yen(line.get('amount_net_yen'), signed=True, optional=True)
        treatment = line.get('tax_treatment')
        rate = _rate(line.get('tax_rate_percent'))
        if amount is None:
            unknown.append({'position': position, 'reason': 'unknown_amount'})
            continue
        known_net += amount
        if treatment == 'exclusive' and rate is not None:
            key = ('exclusive', rate)
        elif treatment in ('exempt', 'outside') and rate is None:
            key = (treatment, None)
        else:
            unknown.append({'position': position, 'reason': 'tax_unknown'})
            continue
        groups[key] = groups.get(key, 0) + amount
    calculated = []
    review_reasons = []
    known_tax = 0
    method_known = rounding_method in ('floor', 'ceil', 'half_up')
    for (treatment, rate), subtotal in sorted(groups.items(), key=lambda x: str(x[0])):
        # The operator approved flooring ordinary invoice tax, not rounding a
        # standalone credit's negative fraction down to a larger refund. Credit
        # notes need original-tax allocation and a separate return policy.
        credit_unconfirmed = rate is not None and subtotal < 0
        if credit_unconfirmed:
            tax = None
            review_reasons.append('credit_tax_rule_unconfirmed')
        else:
            tax = (round_fraction(subtotal * rate, 100, rounding_method) if method_known else None) if rate else 0
        if tax is not None:
            known_tax += tax
        calculated.append({'tax_treatment': treatment, 'tax_rate_percent': rate,
                           'amount_net_yen': subtotal, 'tax_yen': tax,
                           'amount_gross_yen': subtotal + tax if tax is not None else None,
                           'review_reasons': ['credit_tax_rule_unconfirmed'] if credit_unconfirmed else []})
    complete = not unknown and all(group['tax_yen'] is not None for group in calculated)
    return {'currency': 'JPY', 'rounding_method': rounding_method, 'rounding_scope': 'invoice_per_rate',
            'groups': calculated, 'unknown_lines': unknown, 'review_reasons': list(dict.fromkeys(review_reasons)),
            'known_net_subtotal_yen': known_net,
            'known_tax_subtotal_yen': known_tax, 'net_total_yen': known_net if complete else None,
            'tax_total_yen': known_tax if complete else None,
            'reference_gross_total_yen': known_net + known_tax if complete else None,
            'payable_total_yen': None, 'real_payment_enabled': False}


def fee_proposal(sale_gross_yen, channel):
    """Keep the reviewed source's exact price/shipping boundaries in one place."""
    yen(sale_gross_yen)
    return draft_sale_quote(sale_gross_yen, channel)


def _catalog():
    result = draft_catalog()
    # These are snapshots, not active or subsequently editable rate settings.
    result['fee_bands'] = [dict(minimum=minimum, **draft_sale_quote(minimum, 'wholesale'))
                          for minimum in (1, 20000, 50000, 100000, 200000)]
    result['channel_bands'] = {channel: [draft_sale_quote(minimum, channel)['sales_fee_net_yen']
                              for minimum in (1, 20000, 50000, 100000, 200000)]
                              for channel in ('wholesale', 'multichannel', 'dealer_auction', 'self_sale_shipping', 'live')}
    return result


def _quote_from_catalog(catalog, amount, channel):
    yen(amount)
    if amount < 1 or channel not in catalog['channel_bands']:
        raise RegistryError('sale_basis_invalid')
    index = max(i for i, band in enumerate(catalog['fee_bands']) if amount >= band['minimum'])
    return {'version': catalog['version'], 'sale_price_gross_yen': amount,
            'work_bundle_net_yen': catalog['fee_bands'][index]['work_bundle_net_yen'],
            'sales_fee_net_yen': catalog['channel_bands'][channel][index],
            'wholesale_shipping_payer_proposal': ('customer' if amount <= 30000 else 'kaika') if channel == 'wholesale' else None,
            'chargeable': False}


class BillingService:
    def __init__(self, store):
        self.store = store

    def save_agreement(self, actor, customer_id, *, evidence, source_key):
        """Save an unapproved evidence proposal; this method cannot approve it."""
        self.store.authorize(actor, customer_id, write=True)
        if not isinstance(evidence, dict):
            raise RegistryError('agreement_evidence_invalid')
        allowed = {'contract_version_id', 'fee_version_id', 'signed_document_reference', 'consent_reference',
                   'consent_at', 'charge_start_at', 'exception_status', 'exception_reference',
                   'tax_rate_percent', 'rounding_method', 'rounding_scope', 'billing_timezone',
                   'billing_timezone_approved', 'work_scope_confirmed', 'work_kinds',
                   'effective_from', 'effective_until', 'approval_reference', 'approved_at',
                   'channel_authorization_references', 'sale_price_basis_confirmed',
                   'return_policy_reference', 'monthly_net_yen', 'monthly_terms_reference', 'note'}
        if set(evidence) - allowed:
            raise RegistryError('agreement_fields_not_allowed')
        evidence = json.loads(_json(evidence))
        # Operator choices recorded 2026-10-22. These are calculation defaults,
        # not evidence of an individual customer's contract or fee approval.
        evidence.setdefault('rounding_method', 'floor')
        evidence.setdefault('rounding_scope', 'invoice_per_rate')
        evidence.setdefault('billing_timezone', 'Asia/Tokyo')
        evidence.setdefault('billing_timezone_approved', True)
        for key in ('contract_version_id', 'fee_version_id', 'signed_document_reference', 'consent_reference',
                    'exception_reference', 'approval_reference', 'return_policy_reference', 'monthly_terms_reference', 'note'):
            if evidence.get(key) is not None:
                evidence[key] = _text(evidence[key], key, 2000)
        if evidence.get('exception_status') not in (None, 'undecided', 'none', 'exempt', 'adjusted'):
            raise RegistryError('exception_status_invalid')
        for key in ('consent_at', 'charge_start_at', 'effective_from', 'effective_until', 'approved_at'):
            if evidence.get(key) is not None:
                evidence[key] = _time(evidence[key])
        _rate(evidence.get('tax_rate_percent'))
        yen(evidence.get('monthly_net_yen'), optional=True)
        for key in ('billing_timezone_approved', 'work_scope_confirmed', 'sale_price_basis_confirmed'):
            if key in evidence and type(evidence[key]) is not bool:
                raise RegistryError('agreement_boolean_invalid')
        if evidence.get('rounding_method') not in (None, 'floor', 'ceil', 'half_up'):
            raise RegistryError('rounding_unknown')
        if evidence.get('rounding_scope') not in (None, 'invoice_per_rate'):
            raise RegistryError('rounding_scope_invalid')
        if evidence.get('billing_timezone'):
            try:
                ZoneInfo(evidence['billing_timezone'])
            except (KeyError, ValueError, TypeError):
                raise RegistryError('billing_timezone_invalid') from None
        refs = evidence.get('channel_authorization_references', {})
        if not isinstance(refs, dict) or any(k not in _catalog()['channel_bands'] or not isinstance(v, str) for k, v in refs.items()):
            raise RegistryError('channel_references_invalid')
        kinds = evidence.get('work_kinds')
        from item_registry import WORK_KINDS
        if kinds is not None and (not isinstance(kinds, list) or not kinds or any(not isinstance(k, str) or k not in WORK_KINDS for k in kinds)):
            raise RegistryError('work_scope_invalid')
        if len(_json(evidence)) > 30000:
            raise RegistryError('agreement_too_large')
        def create():
            agreement_id = 'ag3_' + uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage3_agreements(agreement_id,customer_id,actor_user_id,recorded_at,
                evidence_json,catalog_json,status,source_key) VALUES (?,?,?,?,?,?,'draft',?)''',
                (agreement_id, customer_id, actor.user_id, _now(), _json(evidence), _json(_catalog()), source_key))
            return {'agreement_id': agreement_id, 'customer_id': customer_id, 'status': 'draft',
                    'real_payment_enabled': False}
        return self.store.operation(actor, customer_id, source_key, {'action': 'billing_agreement', 'evidence': evidence}, create)

    def list_agreements(self, actor, customer_id):
        self.store.authorize(actor, customer_id)
        return [self._agreement(row) for row in self.store.query(
            'SELECT * FROM stage3_agreements WHERE customer_id=? ORDER BY recorded_at,agreement_id', (customer_id,))]

    @staticmethod
    def _agreement(row):
        return {**row, 'evidence': json.loads(row['evidence_json']), 'catalog': json.loads(row['catalog_json']),
                'real_payment_enabled': False}

    def _load_agreement(self, customer_id, agreement_id):
        if not agreement_id:
            return None
        rows = self.store.query('SELECT * FROM stage3_agreements WHERE agreement_id=? AND customer_id=?',
                                (agreement_id, customer_id))
        if not rows:
            raise RegistryError('agreement_not_found', status=404)
        return self._agreement(rows[0])

    def _facts(self, actor, item_id):
        from stage3_profit import ProfitService
        return ProfitService(self.store).snapshot(actor, item_id)

    def _evidence(self, actor, item_id):
        item = self.store.item(actor, item_id)
        customer = item['owner_user_id']
        # Staff detail includes former owners' histories. Never use those as the
        # present customer's financial evidence, even though staff may view them.
        events = self.store.query('''SELECT * FROM business_item_observations
            WHERE physical_item_id=? AND customer_id=? ORDER BY occurred_at,recorded_at,observation_id''', (item_id, customer))
        owner = self.store.query('''SELECT a.* FROM business_item_assignments a JOIN business_parties p ON p.party_id=a.party_id
            WHERE a.physical_item_id=? AND a.role='owner' AND a.ended_at IS NULL AND p.user_id=?''', (item_id, customer))
        start = _time(owner[0]['started_at']) if owner else None
        events = [e for e in events if not start or _time(e['occurred_at']) >= start]
        sale_index = next((i for i in range(len(events) - 1, -1, -1) if events[i]['kind'] == 'sold'), None)
        sale = events[sale_index] if sale_index is not None else None
        # Same timestamps are valid for sequential user operations. Sequence
        # comes from the append log, not random UUID order or timestamp equality.
        shipment = next((e for e in reversed(events[sale_index + 1:]) if e['kind'] == 'shipped'), None) if sale_index is not None else None
        # Legacy rows with equal event AND recording times cannot be ordered by
        # UUID. Do not reuse a previous shipment for an uncertain sale cycle.
        if financial_event_order_ambiguous(events):
            shipment = None
        works = self.store.query('''SELECT w.*,p.kind AS performer_kind FROM business_work_records w
            JOIN business_work_context c ON c.work_id=w.work_id JOIN business_parties p ON p.party_id=w.actor_party_id
            WHERE w.physical_item_id=? AND c.customer_id=? AND p.kind='staff' ORDER BY performed_at,work_id''', (item_id, customer))
        works = [w for w in works if not start or _time(w['performed_at']) >= start]
        custodian = None
        if shipment:
            rows = self.store.query('''SELECT a.*,p.kind FROM business_item_assignments a JOIN business_parties p ON p.party_id=a.party_id
                WHERE a.physical_item_id=? AND a.role='custodian' ORDER BY a.started_at''', (item_id,))
            at = _time(shipment['occurred_at'])
            eligible = [row for row in rows if _time(row['started_at']) <= at and
                        (not row['ended_at'] or _time(row['ended_at']) > at)]
            custodian = eligible[-1]['kind'] if eligible else None
        facts = self._facts(actor, item_id)
        fingerprint = hashlib.sha256(_json({'events': [(e['observation_id'], e['kind']) for e in events],
            'works': [w['work_id'] for w in works], 'fact_version': facts.get('version_id'), 'custodian': custodian}).encode('utf-8')).hexdigest()
        return item, events, works, sale, shipment, custodian, facts, fingerprint

    def preview_item(self, actor, item_id, agreement_id=None):
        item, events, works, sale, shipment, custody, facts, fingerprint = self._evidence(actor, item_id)
        customer = item['owner_user_id']
        agreement = self._load_agreement(customer, agreement_id)
        evidence = agreement['evidence'] if agreement else {'rounding_method': 'floor',
            'rounding_scope': 'invoice_per_rate', 'billing_timezone': 'Asia/Tokyo',
            'billing_timezone_approved': True}
        common = []
        if financial_event_order_ambiguous(events):
            common.append('financial_event_order_unconfirmed')
        if not shipment or not sale:
            common.append('not_sold_and_shipped')
        if custody == 'customer' and not works:
            return {'physical_item_id': item_id, 'customer_id': customer, 'applicable': False, 'lines': [],
                    'reason_codes': ['self_managed_no_kaika_fee'], 'real_payment_enabled': False,
                    'source_fingerprint': fingerprint, 'shipment_event_id': shipment['observation_id'] if shipment else None}
        if custody != 'kaika':
            common.append('custody_unknown')
        amount = facts.get('sale_gross_yen')
        channel = facts.get('channel')
        if amount is None or type(amount) is not int or amount < 1:
            common.append('sale_amount_unknown')
        if facts.get('basis') != 'actual':
            common.append('sale_fact_not_actual')
        if not sale or facts.get('sale_event_id') != sale['observation_id']:
            common.append('sale_event_mismatch')
        if evidence.get('sale_price_basis_confirmed') is not True:
            common.append('sale_basis_unconfirmed')
        catalog = agreement['catalog'] if agreement else _catalog()
        if channel not in catalog['channel_bands']:
            common.append('channel_unknown')
        quote = _quote_from_catalog(catalog, amount, channel) if 'sale_amount_unknown' not in common and 'channel_unknown' not in common else None
        contract = ContractEvidence(customer_id=customer, **{key: (_dt(evidence.get(key)) if key.endswith('_at') else evidence.get(key))
            for key in ('contract_version_id', 'fee_version_id', 'signed_document_reference', 'consent_reference',
                        'consent_at', 'charge_start_at', 'exception_status', 'exception_reference')}) if agreement else None
        sale_index = next((i for i, event in enumerate(events) if sale and event['observation_id'] == sale['observation_id']), None)
        after_sale = events[sale_index + 1:] if sale_index is not None else []
        returned = any(e['kind'] == 'returned' for e in after_sale)
        cancelled = any(e['kind'] == 'sale_cancelled' for e in after_sale)
        if any(e['kind'] == 'returned' for e in events):
            common.append('return_cycle_requires_review')
        selected_works = [w for w in works if not evidence.get('work_kinds') or w['kind'] in evidence['work_kinds']]
        lines = []
        for fee_kind, quote_key in (('work_bundle', 'work_bundle_net_yen'), ('sales_fee', 'sales_fee_net_yen')):
            proposed = quote[quote_key] if quote else None
            policy = FeePolicyEvidence(version_id=evidence.get('fee_version_id') or catalog['version'],
                status='approved' if agreement and agreement['status'] == 'fixture_approved' else 'draft',
                approval_reference=evidence.get('approval_reference'), approved_at=_dt(evidence.get('approved_at')),
                effective_from=_dt(evidence.get('effective_from')), effective_until=_dt(evidence.get('effective_until')),
                amount_yen=proposed, tax_treatment='exclusive', tax_rate_percent=evidence.get('tax_rate_percent'),
                rounding_method=evidence.get('rounding_method'), rounding_scope=evidence.get('rounding_scope'),
                billing_timezone=evidence.get('billing_timezone'), billing_timezone_approved=evidence.get('billing_timezone_approved') is True,
                work_scope_confirmed=evidence.get('work_scope_confirmed') is True and bool(evidence.get('work_kinds')))
            context = InvoiceContext(customer, item_id, fee_kind, contract, policy,
                event_id=shipment['observation_id'] if shipment else None,
                source_reference=shipment['evidence_reference'] if shipment else None,
                charge_event_at=_dt(shipment['occurred_at']) if shipment else None,
                sold_at=_dt(sale['occurred_at']) if sale else None,
                shipped_at=_dt(shipment['occurred_at']) if shipment else None,
                works=tuple(WorkEvidence(w['work_id'], item_id, w['kind'], _dt(w['performed_at']), w['duration_seconds'], w['source_reference']) for w in selected_works),
                return_state='pending' if returned else 'no_return', cancelled=cancelled,
                channel_authorization_reference=evidence.get('channel_authorization_references', {}).get(channel))
            reasons = list(dict.fromkeys(common + list(evaluate_invoice_eligibility(context).reason_codes)))
            if evidence.get('rounding_scope') not in (None, 'invoice_per_rate'):
                reasons.append('rounding_scope_invalid')
            # Do not put a full bundle into totals when only post-start work may
            # be charged, or treat an unapproved self-shipping channel as free.
            withheld = {'pre_start_work_not_billable', 'mixed_start_bundle_unresolved', 'before_charge_start',
                        'custody_unknown', 'sale_event_mismatch', 'sale_fact_not_actual', 'return_cycle_requires_review',
                        'financial_event_order_unconfirmed'}
            if fee_kind == 'sales_fee' and channel == 'self_sale_shipping' and 'channel_not_authorized' in reasons:
                withheld.add('channel_not_authorized')
            line_amount = None if withheld.intersection(reasons) else proposed
            lines.append({'fee_kind': fee_kind, 'amount_net_yen': line_amount, 'proposed_full_amount_net_yen': proposed,
                'tax_treatment': 'exclusive', 'tax_rate_percent': evidence.get('tax_rate_percent'),
                'reason_codes': reasons, 'status': 'needs_review' if reasons else 'draft',
                'rounding_method': evidence.get('rounding_method'), 'rounding_scope': 'invoice_per_rate',
                'billing_timezone': evidence.get('billing_timezone'), 'chargeable': False})
        return {'physical_item_id': item_id, 'customer_id': customer, 'title': item['title'], 'applicable': True,
                'agreement_id': agreement_id, 'agreement_snapshot': agreement,
                'shipment_event_id': shipment['observation_id'] if shipment else None,
                'occurred_at': shipment['occurred_at'] if shipment else None,
                'source_reference': shipment['evidence_reference'] if shipment else None,
                'sale_event_id': sale['observation_id'] if sale else None,
                'sale_fact_snapshot': facts, 'work_snapshot': selected_works,
                'quote_snapshot': quote, 'source_fingerprint': fingerprint,
                'lines': lines, 'real_payment_enabled': False}

    def create_item_drafts(self, actor, item_id, *, source_key, agreement_id=None):
        item = self.store.item(actor, item_id, write=True)
        customer = item['owner_user_id']
        def create():
            # Customer serialization does not serialize an ownership change.
            # Lock the same item profile as the registry assignment writer and
            # recheck the owner before reading evidence or writing any charge.
            _, roles, _ = self.store.registry._profile(actor, item_id, lock=True)
            if self.store.registry._owner(roles) != customer:
                raise RegistryError('financial_owner_changed', '所有者が変わりました。最新の商品情報で確認してください。', 409)
            preview = self.preview_item(actor, item_id, agreement_id)
            if not preview['applicable']:
                return {'physical_item_id': item_id, 'draft_ids': [], 'reason_codes': preview['reason_codes'], 'real_payment_enabled': False}
            if not preview['shipment_event_id'] or not preview['sale_event_id']:
                raise RegistryError('not_sold_and_shipped', '売却と発送の記録がそろってから下書きを作成してください。', 409)
            ids = []
            for line in preview['lines']:
                existing = self.store.query('''SELECT draft_id FROM stage3_charge_drafts
                    WHERE customer_id=? AND source_event_id=? AND fee_kind=?''',
                    (customer, preview['shipment_event_id'], line['fee_kind']))
                if existing:
                    ids.append(existing[0]['draft_id'])
                    continue
                draft_id = 'ch3_' + uuid.uuid4().hex
                snapshot = {key: value for key, value in preview.items() if key != 'lines'}
                snapshot['calculation'] = line
                self.store.execute('''INSERT INTO stage3_charge_drafts(draft_id,customer_id,physical_item_id,agreement_id,
                    source_event_id,fee_kind,amount_net_yen,tax_rate_percent,tax_treatment,occurred_at,recorded_at,
                    evidence_reference,snapshot_json,reasons_json,source_key,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (draft_id, customer, item_id, agreement_id, preview['shipment_event_id'], line['fee_kind'],
                     line['amount_net_yen'], line['tax_rate_percent'], line['tax_treatment'], preview['occurred_at'], _now(),
                     preview['source_reference'], _json(snapshot), _json(line['reason_codes']),
                     source_key + ':' + line['fee_kind'], line['status']))
                ids.append(draft_id)
            return {'physical_item_id': item_id, 'draft_ids': ids, 'real_payment_enabled': False}
        return self.store.operation(actor, customer, source_key,
            {'action': 'item_fee_drafts', 'item_id': item_id, 'agreement_id': agreement_id}, create)

    def _line(self, actor, row, *, current=True):
        self.store.authorize(actor, row['customer_id'])
        snapshot = json.loads(row['snapshot_json'])
        reasons = json.loads(row['reasons_json'])
        current_reasons = list(reasons)
        if current and row['physical_item_id'] and snapshot.get('source_fingerprint'):
            # Old-owner invoices remain readable after transfer. Only inspect
            # that invoice customer's event identities; no new owner is joined.
            historic_events = self.store.query('''SELECT kind,occurred_at,recorded_at FROM business_item_observations
                WHERE physical_item_id=? AND customer_id=?''', (row['physical_item_id'], row['customer_id']))
            if financial_event_order_ambiguous(historic_events):
                current_reasons.append('financial_event_order_unconfirmed')
            try:
                item = self.store.item(actor, row['physical_item_id'])
                # An old owner's own historical invoice remains readable, but
                # never join that customer's document to the new owner's data.
                if item['owner_user_id'] == row['customer_id']:
                    fingerprint = self._evidence(actor, row['physical_item_id'])[-1]
                    if fingerprint != snapshot['source_fingerprint']:
                        current_reasons.append('source_changed_after_snapshot')
            except RegistryError as exc:
                if exc.status not in (403, 404):
                    raise
        return {**row, 'snapshot': snapshot, 'reason_codes': reasons,
                'current_review_reasons': list(dict.fromkeys(current_reasons)),
                'current_status': 'needs_review' if current_reasons else 'draft',
                'real_payment_enabled': False, 'chargeable': False}

    def list_item_drafts(self, actor, item_id):
        item = self.store.item(actor, item_id)
        # Current ownership is explicit; staff history comes from customer bills.
        return [self._line(actor, row) for row in self.store.query('''SELECT * FROM stage3_charge_drafts
            WHERE physical_item_id=? AND customer_id=? ORDER BY occurred_at,recorded_at,draft_id''',
            (item_id, item['owner_user_id']))]

    item_lines = list_item_drafts

    def record_component(self, actor, customer_id, *, fee_kind, amount_net_yen, tax_rate_percent,
                         occurred_at, evidence_reference, source_key, agreement_id=None,
                         item_id=None, original_draft_id=None, tax_treatment='exclusive', note=None):
        if fee_kind not in ('expense', 'digital_monthly', 'physical_monthly', 'adjustment'):
            raise RegistryError('manual_fee_kind_invalid')
        amount = yen(amount_net_yen, signed=fee_kind == 'adjustment', optional=True)
        rate = _rate(tax_rate_percent)
        if tax_treatment not in (None, 'exclusive', 'exempt', 'outside'):
            raise RegistryError('tax_treatment_invalid')
        if tax_treatment in ('exempt', 'outside') and rate is not None:
            raise RegistryError('tax_rate_invalid')
        when = _time(occurred_at)
        reference = _text(evidence_reference, 'evidence_reference', 2000)
        self.store.authorize(actor, customer_id, write=True)
        # Corrections belong to the original customer's immutable charge, even
        # after the physical item changes owner. New expenses belong to the
        # current ownership period and are rechecked under the item lock below.
        ownership_assignment_id = None
        if item_id and fee_kind != 'adjustment':
            _, initial_roles, _ = self.store.registry._profile(actor, item_id)
            if self.store.registry._owner(initial_roles) != customer_id:
                raise RegistryError('component_customer_mismatch', status=409)
            ownership_assignment_id = initial_roles['owner']['assignment_id']
        agreement = self._load_agreement(customer_id, agreement_id)
        if fee_kind == 'adjustment' and not original_draft_id:
            raise RegistryError('original_draft_required')
        if fee_kind != 'adjustment' and original_draft_id:
            raise RegistryError('original_only_for_adjustment')
        payload = dict(action='billing_component', fee_kind=fee_kind, amount_net_yen=amount, tax_rate_percent=rate,
            occurred_at=when, evidence_reference=reference, agreement_id=agreement_id, item_id=item_id,
            original_draft_id=original_draft_id, tax_treatment=tax_treatment, note=note,
            ownership_assignment_id=ownership_assignment_id)
        def create():
            reasons = ['component_terms_unconfirmed']
            original = None
            if item_id and fee_kind != 'adjustment':
                _, roles, _ = self.store.registry._profile(actor, item_id, lock=True)
                if (self.store.registry._owner(roles) != customer_id
                        or roles['owner']['assignment_id'] != ownership_assignment_id):
                    raise RegistryError('financial_owner_changed',
                        '所有者が変わりました。最新の商品情報で確認してください。', 409)
                if when < _time(roles['owner']['started_at']):
                    raise RegistryError('component_owner_period_mismatch',
                        'この所有期間より前の費用です。元のお客様の明細を確認してください。', 409)
            if not original_draft_id:
                duplicates = self.store.query('''SELECT * FROM stage3_charge_drafts
                    WHERE customer_id=? AND fee_kind=? AND evidence_reference=?''', (customer_id, fee_kind, reference))
                duplicates = [row for row in duplicates if row['physical_item_id'] == item_id]
                if duplicates:
                    row = duplicates[0]
                    if (row['amount_net_yen'], row['tax_rate_percent'], row['tax_treatment'], row['occurred_at'], row['agreement_id']) != (amount, rate, tax_treatment, when, agreement_id):
                        raise RegistryError('component_evidence_conflict', status=409)
                    return {'draft_id': row['draft_id'], 'status': row['status'], 'real_payment_enabled': False}
            if original_draft_id:
                originals = self.store.query('SELECT * FROM stage3_charge_drafts WHERE draft_id=? AND customer_id=?',
                                             (original_draft_id, customer_id))
                if not originals or originals[0]['physical_item_id'] != item_id:
                    raise RegistryError('original_draft_not_found', status=404)
                original = originals[0]
                if original['fee_kind'] == 'adjustment':
                    raise RegistryError('correction_must_reference_original')
                if when < _time(original['occurred_at']):
                    raise RegistryError('correction_before_original')
                if original['tax_treatment'] != tax_treatment or original['tax_rate_percent'] != rate:
                    raise RegistryError('correction_tax_mismatch')
                same = self.store.query('''SELECT * FROM stage3_charge_drafts WHERE original_draft_id=?
                    AND evidence_reference=?''', (original_draft_id, reference))
                if same:
                    if len(same) == 1 and same[0]['amount_net_yen'] == amount and same[0]['occurred_at'] == when:
                        return {'draft_id': same[0]['draft_id'], 'status': 'needs_review', 'real_payment_enabled': False}
                    raise RegistryError('correction_evidence_conflict', status=409)
                adjustments = self.store.query('SELECT amount_net_yen FROM stage3_charge_drafts WHERE original_draft_id=?', (original_draft_id,))
                if amount is not None and original['amount_net_yen'] is not None and all(r['amount_net_yen'] is not None for r in adjustments):
                    if original['amount_net_yen'] + sum(r['amount_net_yen'] for r in adjustments) + amount < 0:
                        raise RegistryError('correction_exceeds_original')
                reasons.append('correction_requires_review')
            if fee_kind in ('digital_monthly', 'physical_monthly'):
                reasons.append('monthly_terms_unconfirmed')
            if amount is None:
                reasons.append('unknown_amount')
            if tax_treatment is None or tax_treatment == 'exclusive' and rate is None:
                reasons.append('tax_unknown')
            if not agreement:
                reasons.append('contract_missing')
            elif agreement['status'] != 'fixture_approved':
                reasons.append('fee_policy_draft')
            snapshot = {'component': payload, 'agreement_snapshot': agreement,
                        'original_snapshot': dict(original) if original else None, 'calculation': {
                            'rounding_method': agreement['evidence'].get('rounding_method') if agreement else 'floor',
                            'billing_timezone': agreement['evidence'].get('billing_timezone') if agreement else 'Asia/Tokyo'}}
            draft_id = 'ch3_' + uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage3_charge_drafts(draft_id,customer_id,physical_item_id,agreement_id,
                original_draft_id,fee_kind,amount_net_yen,tax_rate_percent,tax_treatment,occurred_at,recorded_at,
                evidence_reference,snapshot_json,reasons_json,source_key,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'needs_review')''',
                (draft_id, customer_id, item_id, agreement_id, original_draft_id, fee_kind, amount, rate,
                 tax_treatment, when, _now(), reference, _json(snapshot), _json(reasons), source_key))
            return {'draft_id': draft_id, 'status': 'needs_review', 'real_payment_enabled': False}
        return self.store.operation(actor, customer_id, source_key, payload, create)

    def create_month(self, actor, customer_id, period, *, source_key, timezone_name='Asia/Tokyo'):
        if not isinstance(period, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', period):
            raise RegistryError('invoice_period_invalid')
        try:
            ZoneInfo(timezone_name)
        except (KeyError, ValueError, TypeError):
            raise RegistryError('billing_timezone_invalid') from None
        self.store.authorize(actor, customer_id, write=True)
        def create():
            rows = self.store.query('SELECT * FROM stage3_charge_drafts WHERE customer_id=? ORDER BY occurred_at,recorded_at,draft_id', (customer_id,))
            lines = [self._line(actor, row) for row in rows if period_key(_dt(row['occurred_at']), timezone_name) == period]
            methods = {line['snapshot'].get('calculation', {}).get('rounding_method') for line in lines}
            method = next(iter(methods)) if len(methods) == 1 else None
            reasons = []
            if method not in ('floor', 'ceil', 'half_up'):
                reasons.append('conflicting_rounding')
            agreed_period = bool(lines) and all(line['snapshot'].get('agreement_snapshot') and
                line['snapshot']['agreement_snapshot']['status'] == 'fixture_approved' and
                line['snapshot']['agreement_snapshot']['evidence'].get('billing_timezone_approved') is True and
                line['snapshot']['agreement_snapshot']['evidence'].get('billing_timezone') == timezone_name for line in lines)
            if not agreed_period:
                reasons.append('no_agreed_period')
            if any(line['current_review_reasons'] for line in lines):
                reasons.append('unresolved_adjustments')
            # A complete monthly burden cannot be inferred from just the two
            # per-shipment fees. Do not auto-create a subscription or infer a
            # free month from a missing subscription line.
            has_monthly_component = any(line['fee_kind'] in ('digital_monthly', 'physical_monthly') for line in lines)
            if not has_monthly_component:
                reasons.append('monthly_component_missing')
            # A monthly bill must disclose omissions, not quietly report a zero.
            shipped = self.store.query("SELECT * FROM business_item_observations WHERE customer_id=? AND kind='shipped'", (customer_id,))
            uncollected = []
            for event in shipped:
                if period_key(_dt(event['occurred_at']), timezone_name) != period:
                    continue
                recorded = {line['fee_kind'] for line in rows if line['source_event_id'] == event['observation_id']}
                custodians = self.store.query('''SELECT a.*,p.kind FROM business_item_assignments a JOIN business_parties p ON p.party_id=a.party_id
                    WHERE a.physical_item_id=? AND a.role='custodian' ''', (event['physical_item_id'],))
                at = _time(event['occurred_at'])
                kaika = any(c['kind'] == 'kaika' and _time(c['started_at']) <= at and
                            (not c['ended_at'] or _time(c['ended_at']) > at) for c in custodians)
                if kaika and not {'work_bundle', 'sales_fee'}.issubset(recorded):
                    uncollected.append({'physical_item_id': event['physical_item_id'], 'shipment_event_id': event['observation_id']})
            if uncollected:
                reasons.append('uncollected_shipments')
            totals = invoice_totals(lines, method)
            reasons.extend(totals['review_reasons'])
            totals['known_component_reference_gross_yen'] = totals['reference_gross_total_yen']
            if uncollected or not has_monthly_component:
                totals['reference_gross_total_yen'] = None
            previous = self.store.query('''SELECT * FROM stage3_invoice_revisions
                WHERE customer_id=? AND period=? AND timezone_name=? ORDER BY revision DESC''', (customer_id, period, timezone_name))
            snapshot = {'customer_id': customer_id, 'period': period, 'timezone_name': timezone_name,
                'lines': lines, 'totals': totals, 'reason_codes': reasons, 'uncollected_shipments': uncollected,
                'chargeable': False, 'real_payment_enabled': False}
            digest = hashlib.sha256(_json(snapshot).encode('utf-8')).hexdigest()
            if previous and previous[0]['content_hash'] == digest:
                return {'invoice_id': previous[0]['invoice_id'], 'revision': previous[0]['revision'], 'unchanged': True, 'real_payment_enabled': False}
            invoice_id = 'in3_' + uuid.uuid4().hex
            revision = previous[0]['revision'] + 1 if previous else 1
            old_ids = {line['draft_id'] for line in json.loads(previous[0]['snapshot_json'])['lines']} if previous else set()
            snapshot['added_draft_ids'] = [line['draft_id'] for line in lines if line['draft_id'] not in old_ids]
            self.store.execute('''INSERT INTO stage3_invoice_revisions(invoice_id,customer_id,period,timezone_name,
                revision,previous_invoice_id,content_hash,snapshot_json,recorded_at,source_key,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (invoice_id, customer_id, period, timezone_name, revision, previous[0]['invoice_id'] if previous else None,
                 digest, _json(snapshot), _now(), source_key, 'needs_review' if reasons else 'draft'))
            return {'invoice_id': invoice_id, 'revision': revision, 'unchanged': False, 'real_payment_enabled': False}
        return self.store.operation(actor, customer_id, source_key,
            {'action': 'invoice_month', 'period': period, 'timezone_name': timezone_name}, create)

    def _current_month_shipments(self, customer_id, period, timezone_name):
        events = self.store.query("SELECT * FROM business_item_observations WHERE customer_id=? AND kind='shipped'", (customer_id,))
        result = set()
        for event in events:
            if period_key(_dt(event['occurred_at']), timezone_name) != period:
                continue
            custodians = self.store.query('''SELECT a.*,p.kind FROM business_item_assignments a
                JOIN business_parties p ON p.party_id=a.party_id WHERE a.physical_item_id=? AND a.role='custodian' ''',
                (event['physical_item_id'],))
            at = _time(event['occurred_at'])
            if any(c['kind'] == 'kaika' and _time(c['started_at']) <= at and
                   (not c['ended_at'] or _time(c['ended_at']) > at) for c in custodians):
                result.add(event['observation_id'])
        return result

    def list_months(self, actor, customer_id):
        self.store.authorize(actor, customer_id)
        return [{key: row[key] for key in ('invoice_id', 'customer_id', 'period', 'timezone_name', 'revision',
            'previous_invoice_id', 'recorded_at', 'status')} for row in self.store.query('''SELECT * FROM stage3_invoice_revisions
            WHERE customer_id=? ORDER BY period DESC,revision DESC''', (customer_id,))]

    def get_month(self, actor, invoice_id):
        self.store.authorize(actor)
        rows = self.store.query('SELECT * FROM stage3_invoice_revisions WHERE invoice_id=?', (invoice_id,))
        if not rows:
            raise RegistryError('invoice_not_found', status=404)
        self.store.authorize(actor, rows[0]['customer_id'])
        row = rows[0]
        snapshot = json.loads(row['snapshot_json'])
        current_reasons = list(snapshot['reason_codes'])
        line_reasons = {}
        changed = False
        for original in snapshot['lines']:
            current = self._line(actor, original)
            line_reasons[original['draft_id']] = current['current_review_reasons']
            if current['current_review_reasons'] != original['current_review_reasons']:
                changed = True
                current_reasons.append('source_changed_after_snapshot')
        current_lines = self.store.query('SELECT draft_id,occurred_at FROM stage3_charge_drafts WHERE customer_id=?', (row['customer_id'],))
        current_ids = {line['draft_id'] for line in current_lines if period_key(_dt(line['occurred_at']), row['timezone_name']) == row['period']}
        if current_ids != {line['draft_id'] for line in snapshot['lines']}:
            changed = True
            current_reasons.append('invoice_new_lines_available')
        known_shipments = {line['source_event_id'] for line in snapshot['lines'] if line['source_event_id']}
        known_shipments.update(event['shipment_event_id'] for event in snapshot.get('uncollected_shipments', []))
        if self._current_month_shipments(row['customer_id'], row['period'], row['timezone_name']) - known_shipments:
            changed = True
            current_reasons.append('uncollected_shipments')
        newer = self.store.query('''SELECT invoice_id FROM stage3_invoice_revisions
            WHERE customer_id=? AND period=? AND timezone_name=? AND revision>?''',
            (row['customer_id'], row['period'], row['timezone_name'], row['revision']))
        if newer:
            changed = True
            current_reasons.append('invoice_superseded')
        return {**row, 'snapshot': snapshot, 'real_payment_enabled': False,
                'current_review_reasons': list(dict.fromkeys(current_reasons)),
                'current_line_review_reasons': line_reasons,
                'current_status': 'needs_review' if current_reasons else 'draft',
                'is_current_snapshot': not changed}

    def csv_month(self, actor, invoice_id):
        invoice = self.get_month(actor, invoice_id)
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['請求下書きID', '版', '対象月', '個品ID', '明細ID', '訂正元', '種類', '税別円', '税率', '税区分', '発生日', '原資料', '要確認事項'])
        def safe(value):
            if type(value) is int:
                return value
            text = '' if value is None else str(value)
            return "'" + text if text.lstrip().startswith(('=', '+', '-', '@', '\t', '\r')) else text
        for line in invoice['snapshot']['lines']:
            writer.writerow([safe(value) for value in (invoice_id, invoice['revision'], invoice['period'],
                line['physical_item_id'], line['draft_id'], line['original_draft_id'], line['fee_kind'],
                line['amount_net_yen'], line['tax_rate_percent'], line['tax_treatment'], line['occurred_at'],
                line['evidence_reference'], ';'.join(invoice['current_line_review_reasons'][line['draft_id']]))])
        writer.writerow(['現在の確認事項', ';'.join(invoice['current_review_reasons'])])
        writer.writerow(['記録済み明細の参考額（未記録の月額等を含みません）', invoice['snapshot']['totals'].get('known_component_reference_gross_yen')])
        writer.writerow(['参考合計（税込・請求額ではありません）', invoice['snapshot']['totals']['reference_gross_total_yen']])
        writer.writerow(['実請求', '無効'])
        return '\ufeff' + output.getvalue()
