"""Recorded item finances and transparent profit; never a payment or balance.

All amounts are explicit integer JPY evidence, including zero. Legacy numeric
columns are deliberately not imported: their zero can mean 'never entered'.
Corrections append an entire version and preserve the exact earlier values.
"""
from decimal import Decimal, ROUND_HALF_UP
import json
import uuid

from item_registry import RegistryError, _json, _now, _text, _time
from stage3_common import financial_event_order_ambiguous


MONEY_FIELDS = ('purchase_cost_yen', 'sale_gross_yen', 'channel_fee_yen', 'materials_yen',
                'kaika_fee_yen', 'return_adjustment_yen', 'monthly_allocation_yen')
CHANNELS = frozenset(('wholesale', 'multichannel', 'dealer_auction', 'self_sale_shipping', 'live', 'other'))
_MAX_YEN = 9000000000000


def _money(value, name):
    if value is None:
        return None
    if type(value) is not int or abs(value) > _MAX_YEN or (name != 'return_adjustment_yen' and value < 0):
        raise RegistryError('invalid_' + name, '金額は円単位の整数、未確認は空欄で記録してください。')
    return value


def _shipping(entries):
    if entries is None:
        return None
    if not isinstance(entries, list) or len(entries) > 100:
        raise RegistryError('invalid_shipping_entries')
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {'source_id', 'amount_yen', 'origin', 'source_reference'}:
            raise RegistryError('invalid_shipping_entry')
        origin = entry.get('origin')
        if origin not in ('external', 'staff', 'customer'):
            raise RegistryError('invalid_shipping_origin')
        result.append({'source_id': _text(entry.get('source_id'), 'shipping_source_id', 200),
                       'amount_yen': _money(entry.get('amount_yen'), 'shipping_cost_yen'),
                       'origin': origin, 'source_reference': _text(entry.get('source_reference'), 'shipping_source_reference', 2000)})
    return result


def shipping_summary(entries):
    """A source is an economic expense, not the path by which it arrived."""
    if entries is None:
        return {'total_yen': None, 'review_reasons': ['shipping_amount_unknown'], 'duplicate_source_ids': []}
    sources, duplicates, reasons = {}, set(), []
    for entry in entries:
        key, amount = entry['source_id'], entry['amount_yen']
        if key in sources:
            duplicates.add(key)
            if sources[key] != amount:
                reasons.append('shipping_source_amount_conflict')
        else:
            sources[key] = amount
        if amount is None:
            reasons.append('shipping_amount_unknown')
    return {'total_yen': None if reasons else sum(sources.values()),
            'review_reasons': sorted(set(reasons)), 'duplicate_source_ids': sorted(duplicates)}


def _ratio(profit, denominator):
    if profit is None or denominator in (None, 0):
        return None
    return str((Decimal(profit) * 100 / Decimal(denominator)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


class ProfitService:
    def __init__(self, store):
        self.store = store

    def _context(self, actor, item_id, write=False):
        self.store.require_ready()
        item = self.store.item(actor, item_id, write=write)
        customer_id = item['owner_user_id']
        self.store.authorize(actor, customer_id, write=write)
        rows = self.store.query("SELECT assignment_id,started_at FROM business_item_assignments WHERE physical_item_id=? AND role='owner' AND ended_at IS NULL", (item_id,))
        if len(rows) != 1:
            raise RegistryError('financial_owner_unknown', status=409)
        return item, customer_id, rows[0]

    def _latest(self, assignment_id):
        rows = self.store.query('SELECT * FROM stage3_item_financial_facts WHERE ownership_assignment_id=? ORDER BY revision_number DESC LIMIT 1', (assignment_id,))
        return rows[0] if rows else None

    def _events(self, item_id, customer_id, assignment):
        return self.store.query('''SELECT * FROM business_item_observations WHERE physical_item_id=? AND customer_id=? AND occurred_at>=?
            ORDER BY occurred_at,recorded_at,observation_id''', (item_id, customer_id, _time(assignment['started_at'])))

    def _decode(self, row, item_id, customer_id):
        if row is None:
            return dict({name: None for name in MONEY_FIELDS}, version_id=None, physical_item_id=item_id,
                        customer_id=customer_id, revision_number=0, channel=None, source_reference=None,
                        source_event_key=None, basis=None, sale_event_id=None, shipping_entries=None,
                        shipping_cost_yen=None, channel_fee_includes_shipping=None, currency='JPY',
                        review_reasons=['financial_facts_not_recorded'], real_payment_enabled=False,
                        purchase_funds_credit_yen=0)
        result = dict(row)
        result['shipping_entries'] = json.loads(result.pop('shipping_entries_json')) if row['shipping_entries_json'] is not None else None
        result['channel_fee_includes_shipping'] = None if row['channel_fee_includes_shipping'] is None else bool(row['channel_fee_includes_shipping'])
        shipping = shipping_summary(result['shipping_entries'])
        result['shipping_cost_yen'] = shipping['total_yen']
        result['shipping_duplicate_source_ids'] = shipping['duplicate_source_ids']
        reasons = list(shipping['review_reasons'])
        if result['channel_fee_yen'] not in (None, 0) and result['shipping_cost_yen'] not in (None, 0):
            if result['channel_fee_includes_shipping'] is True:
                reasons.append('channel_fee_shipping_double_count')
            elif result['channel_fee_includes_shipping'] is None:
                reasons.append('channel_fee_shipping_scope_unknown')
        result['review_reasons'] = sorted(set(reasons))
        result['real_payment_enabled'] = False
        result['purchase_funds_credit_yen'] = 0
        return result

    def snapshot(self, actor, item_id):
        _, customer_id, assignment = self._context(actor, item_id)
        return self._decode(self._latest(assignment['assignment_id']), item_id, customer_id)

    def history(self, actor, item_id):
        _, customer_id, assignment = self._context(actor, item_id)
        return [self._decode(row, item_id, customer_id) for row in self.store.query(
            'SELECT * FROM stage3_item_financial_facts WHERE ownership_assignment_id=? ORDER BY revision_number', (assignment['assignment_id'],))]

    def record_facts(self, actor, item_id, *, amounts, channel, source_reference, occurred_at, source_key,
                     previous_version_id=None, shipping_entries=None, sale_event_id=None, basis='actual',
                     channel_fee_includes_shipping=None):
        _, customer_id, initial_assignment = self._context(actor, item_id, write=True)
        if not isinstance(amounts, dict) or set(amounts) - set(MONEY_FIELDS):
            raise RegistryError('invalid_financial_amounts')
        amounts = {name: _money(amounts.get(name), name) for name in MONEY_FIELDS}
        if channel is not None and channel not in CHANNELS:
            raise RegistryError('invalid_financial_channel')
        if basis not in ('actual', 'estimate'):
            raise RegistryError('invalid_financial_basis')
        if channel_fee_includes_shipping is not None and type(channel_fee_includes_shipping) is not bool:
            raise RegistryError('invalid_channel_fee_shipping_scope')
        shipping_entries = _shipping(shipping_entries)
        when, evidence = _time(occurred_at), _text(source_reference, 'financial_source_reference')
        payload = dict(action='financial_facts', item_id=item_id, amounts=amounts, channel=channel, source_reference=evidence,
                       occurred_at=when, previous_version_id=previous_version_id, shipping_entries=shipping_entries,
                       sale_event_id=sale_event_id, basis=basis, channel_fee_includes_shipping=channel_fee_includes_shipping,
                       ownership_assignment_id=initial_assignment['assignment_id'])

        def write():
            self.store.registry._profile(actor, item_id, lock=True)
            _, current_customer, assignment = self._context(actor, item_id, write=True)
            if current_customer != customer_id or assignment['assignment_id'] != initial_assignment['assignment_id']:
                raise RegistryError('financial_owner_changed', status=409)
            if when < _time(assignment['started_at']):
                raise RegistryError('financial_evidence_before_ownership')
            latest = self._latest(assignment['assignment_id'])
            if previous_version_id != (latest['version_id'] if latest else None):
                raise RegistryError('financial_revision_conflict', status=409)
            if latest and when < latest['occurred_at']:
                raise RegistryError('financial_revision_time_reversed')
            events = self._events(item_id, customer_id, assignment)
            if financial_event_order_ambiguous(events):
                raise RegistryError('financial_event_order_unconfirmed',
                    '旧記録の売却・発送・返品の順序を、原資料と照合してください。', 409)
            sales = [event for event in events if event['kind'] == 'sold' and event['occurred_at'] <= when]
            linked_sale = sale_event_id
            if linked_sale is None and basis == 'actual' and sales:
                linked_sale = sales[-1]['observation_id']
            if linked_sale is not None and not any(event['observation_id'] == linked_sale for event in sales):
                raise RegistryError('financial_sale_event_mismatch', status=409)
            if linked_sale is not None and sales[-1]['observation_id'] != linked_sale:
                raise RegistryError('financial_sale_event_stale', status=409)
            version_id = 'ff_' + uuid.uuid4().hex
            values = dict(version_id=version_id, physical_item_id=item_id, customer_id=customer_id,
                          ownership_assignment_id=assignment['assignment_id'], revision_number=latest['revision_number'] + 1 if latest else 1,
                          previous_version_id=previous_version_id, actor_user_id=actor.user_id, occurred_at=when, recorded_at=_now(),
                          source_reference=evidence, source_event_key=source_key, currency='JPY', basis=basis, channel=channel,
                          sale_event_id=linked_sale, channel_fee_includes_shipping=None if channel_fee_includes_shipping is None else int(channel_fee_includes_shipping),
                          shipping_entries_json=None if shipping_entries is None else _json(shipping_entries), **amounts)
            self.store.execute('INSERT INTO stage3_item_financial_facts(' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            return self._decode(values, item_id, customer_id)
        return self.store.operation(actor, customer_id, source_key, payload, write)

    def _status(self, assignment_id, sale_event_id):
        if not sale_event_id:
            return None
        rows = self.store.query('''SELECT s.* FROM stage3_sale_status_records s WHERE s.ownership_assignment_id=? AND s.sale_event_id=?
            AND NOT EXISTS (SELECT 1 FROM stage3_sale_status_records newer WHERE newer.previous_status_id=s.status_record_id)''', (assignment_id, sale_event_id))
        if len(rows) > 1:
            raise RegistryError('sale_status_chain_conflict', status=409)
        return rows[0] if rows else None

    def _billing_reconciliation(self, item_id, customer_id):
        """Drafts cannot validate actual Kaika fees, including an entered zero.

        Check the catalog before querying the additive billing table. A missing
        table in a focused component fixture must not abort a PG transaction.
        No dependency on BillingService (which reads financial snapshots).
        """
        if self.store.dialect == 'postgres':
            available = self.store.query("SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() AND table_name='stage3_charge_drafts'")
        else:
            available = self.store.query("SELECT name FROM sqlite_master WHERE type='table' AND name='stage3_charge_drafts'")
        if not available:
            return {'available': False, 'status': 'not_available', 'pending_draft_ids': [], 'pending_draft_count': 0}
        rows = self.store.query('''SELECT draft_id,fee_kind,original_draft_id FROM stage3_charge_drafts
            WHERE physical_item_id=? AND customer_id=? AND fee_kind IN ('work_bundle','sales_fee','expense','adjustment')
            ORDER BY occurred_at,recorded_at,draft_id''', (item_id, customer_id))
        return {'available': True, 'status': 'needs_review' if rows else 'no_product_drafts',
                'pending_draft_ids': [row['draft_id'] for row in rows], 'pending_draft_count': len(rows)}

    def record_sales_status(self, actor, item_id, *, status, occurred_at, source_key, source_reference, resolution_reference=None):
        _, customer_id, initial_assignment = self._context(actor, item_id, write=True)
        if status not in ('unpaid', 'paid', 'return_hold', 'settlement_ready'):
            raise RegistryError('invalid_sale_payment_status')
        when, evidence = _time(occurred_at), _text(source_reference, 'sale_status_source')
        resolution = _text(resolution_reference, 'return_resolution_reference') if resolution_reference is not None else None
        payload = dict(action='sale_status', item_id=item_id, status=status, occurred_at=when, source_reference=evidence,
                       resolution_reference=resolution, ownership_assignment_id=initial_assignment['assignment_id'])

        def write():
            self.store.registry._profile(actor, item_id, lock=True)
            item, current_customer, assignment = self._context(actor, item_id, write=True)
            if current_customer != customer_id or assignment['assignment_id'] != initial_assignment['assignment_id']:
                raise RegistryError('financial_owner_changed', status=409)
            fact = self._latest(assignment['assignment_id'])
            if not fact or not fact['sale_event_id'] or fact['basis'] != 'actual':
                raise RegistryError('actual_sale_evidence_required', status=409)
            events = self._events(item_id, customer_id, assignment)
            if financial_event_order_ambiguous(events):
                raise RegistryError('financial_event_order_unconfirmed',
                    '旧記録の売却・発送・返品の順序を、原資料と照合してください。', 409)
            sale_index = next((i for i, event in enumerate(events) if event['observation_id'] == fact['sale_event_id']), None)
            if sale_index is None or any(event['kind'] == 'sold' for event in events[sale_index+1:]):
                raise RegistryError('financial_sale_event_stale', status=409)
            if when < fact['occurred_at']:
                raise RegistryError('sale_status_before_financial_evidence')
            after_sale = events[sale_index+1:]
            returned = any(event['kind'] in ('returned', 'sale_cancelled') for event in after_sale)
            previous = self._status(assignment['assignment_id'], fact['sale_event_id'])
            previous_status = previous['status'] if previous else None
            transitions = {None: ('unpaid', 'return_hold'), 'unpaid': ('paid', 'return_hold'), 'paid': ('settlement_ready', 'return_hold'),
                           'return_hold': ('unpaid', 'paid'), 'settlement_ready': ('return_hold',)}
            revised = bool(previous and previous['fact_version_id'] != fact['version_id'])
            # A corrected sale amount needs fresh payment evidence. It cannot
            # inherit a prior ready status merely because the item is the same.
            allowed = transitions[previous_status]
            if revised and previous_status in ('unpaid', 'paid', 'settlement_ready'):
                allowed = tuple(set(allowed) | {'unpaid', 'paid'})
            if status not in allowed:
                raise RegistryError('invalid_sale_payment_transition', status=409)
            if previous and when < previous['occurred_at']:
                raise RegistryError('sale_status_time_reversed')
            if previous_status == 'return_hold' and not resolution:
                raise RegistryError('return_resolution_required', status=409)
            if returned and status not in ('return_hold', 'unpaid'):
                raise RegistryError('returned_sale_cannot_settle', status=409)
            if status == 'settlement_ready':
                if revised:
                    raise RegistryError('revised_sale_payment_review_required', status=409)
                if fact['sale_gross_yen'] is None or not any(event['kind'] == 'shipped' and event['occurred_at'] <= when for event in after_sale):
                    raise RegistryError('shipped_sale_amount_required', status=409)
            record = dict(status_record_id='ss_' + uuid.uuid4().hex, physical_item_id=item_id, customer_id=customer_id,
                          ownership_assignment_id=assignment['assignment_id'], sale_event_id=fact['sale_event_id'], fact_version_id=fact['version_id'],
                          previous_status_id=previous['status_record_id'] if previous else None, actor_user_id=actor.user_id,
                          status=status, occurred_at=when, recorded_at=_now(), source_reference=evidence,
                          resolution_reference=resolution, source_event_key=source_key, purchase_funds_credit_yen=0)
            self.store.execute('INSERT INTO stage3_sale_status_records(' + ','.join(record) + ') VALUES (' + ','.join('?' for _ in record) + ')', tuple(record.values()))
            return dict(record, real_payment_enabled=False)
        return self.store.operation(actor, customer_id, source_key, payload, write)

    def profit(self, actor, item_id):
        item, customer_id, assignment = self._context(actor, item_id)
        facts = self._decode(self._latest(assignment['assignment_id']), item_id, customer_id)
        missing = [name for name in MONEY_FIELDS if name != 'monthly_allocation_yen' and facts[name] is None]
        if facts['shipping_cost_yen'] is None:
            missing.append('shipping_cost_yen')
        reasons = list(facts['review_reasons']) + ['unknown_' + name for name in missing]
        subtotal = None
        if not reasons:
            subtotal = facts['sale_gross_yen'] - facts['purchase_cost_yen'] - facts['channel_fee_yen'] - facts['shipping_cost_yen'] - facts['materials_yen'] - facts['kaika_fee_yen'] + facts['return_adjustment_yen']
        including = None if subtotal is None or facts['monthly_allocation_yen'] is None else subtotal - facts['monthly_allocation_yen']
        billing = self._billing_reconciliation(item_id, customer_id)
        if billing['pending_draft_count']:
            # Preserve entered arithmetic, but do not call it realized while
            # the fees or corrections await review. Invoice-level tax has no
            # approved per-item allocation, so no draft total is substituted.
            reasons.append('kaika_fee_draft_pending')
        events = self._events(item_id, customer_id, assignment)
        order_unconfirmed = financial_event_order_ambiguous(events)
        if order_unconfirmed:
            reasons.append('financial_event_order_unconfirmed')
        sale_index = next((i for i, event in enumerate(events) if event['observation_id'] == facts['sale_event_id']), None)
        after_sale = events[sale_index+1:] if sale_index is not None else []
        cancelled = any(event['kind'] in ('returned', 'sale_cancelled') for event in after_sale)
        stale = any(event['kind'] == 'sold' for event in after_sale)
        shipped = any(event['kind'] == 'shipped' for event in after_sale)
        status = self._status(assignment['assignment_id'], facts['sale_event_id'])
        status_value = status['status'] if status else 'unpaid'
        status_stale = bool(status and status['fact_version_id'] != facts['version_id'])
        if status_stale:
            reasons.append('sale_status_fact_version_outdated')
        if order_unconfirmed:
            recognition = 'needs_review'
        elif cancelled or status_value == 'return_hold':
            recognition = 'return_hold'
            reasons.append('return_or_cancel_requires_reconciliation')
        elif stale:
            recognition = 'needs_review'
            reasons.append('financial_sale_event_stale')
        elif facts['basis'] != 'actual' or sale_index is None or not shipped:
            recognition = 'estimate'
        elif status_value != 'settlement_ready' or status_stale:
            recognition = 'awaiting_payment_review'
        elif reasons:
            recognition = 'needs_review'
        else:
            recognition = 'realized'
        # Staff item detail intentionally contains full operational history.
        # Current-owner cost measurement must use only this ownership period.
        work_durations = self.store.query('''SELECT w.duration_seconds FROM business_work_records w
            JOIN business_work_context c ON c.work_id=w.work_id
            WHERE w.physical_item_id=? AND c.customer_id=? AND w.performed_at>=?''',
            (item_id, customer_id, _time(assignment['started_at'])))
        # Display arithmetic separately from recognition; neither is available funds.
        return {'physical_item_id': item_id, 'customer_id': customer_id, 'facts': facts,
                'profit_excluding_monthly_yen': subtotal, 'profit_including_monthly_yen': including,
                'realized_profit_yen': subtotal if recognition == 'realized' else None,
                'estimated_profit_yen': subtotal if recognition == 'estimate' else None,
                'sales_margin_percent': _ratio(subtotal, facts['sale_gross_yen']),
                'cost_return_percent': _ratio(subtotal, facts['purchase_cost_yen']),
                'monthly_sales_margin_percent': _ratio(including, facts['sale_gross_yen']),
                'monthly_cost_return_percent': _ratio(including, facts['purchase_cost_yen']),
                'recognition': recognition, 'sale_payment_status': status_value, 'status_record': status,
                'review_reasons': sorted(set(reasons)), 'missing_amounts': missing,
                'monthly_allocation_unknown': facts['monthly_allocation_yen'] is None,
                'billing_reconciliation': billing, 'kaika_fee_basis': 'entered_record',
                'known_work_seconds': sum(work['duration_seconds'] for work in work_durations if work['duration_seconds'] is not None),
                'unknown_work_duration_count': sum(work['duration_seconds'] is None for work in work_durations),
                'work_cost_yen': None, 'purchase_funds_credit_yen': 0, 'real_payment_enabled': False}
