"""Append-only test or manually entered money records; no money is executed.

Balances are calculated from immutable movements, separated by execution mode.
No sale is ever automatically credited to purchase funds. A record-only entry is
an unverified bookkeeping report, not bank/provider confirmation or permission
to accept funds. Physical-item reservations are test accounting allocations;
they do not transfer ownership, create live orders, or approve the funding model.
"""
from datetime import datetime, timezone
import uuid

from item_registry import RegistryError, _text, _time
from stage3_funds_schema import BUCKETS, KINDS, MAX_YEN


MODE_LABELS = {'test': 'テスト資金（実際のお金ではありません）',
               'record_only': '入力記録（銀行・決済事業者との照合未確認）'}


def _amount(value):
    if type(value) is not int or not 1 <= value <= MAX_YEN:
        raise RegistryError('positive_integer_yen_required', '金額は確認済みの正の整数円で入力してください。')
    return value


def _mode(value):
    if value not in MODE_LABELS:
        raise RegistryError('funds_mode_invalid', 'テストまたは入力記録だけを利用できます。')
    return value


class FundsService:
    def __init__(self, store):
        self.store = store

    def _raw(self, record_id):
        rows = self.store.query('SELECT * FROM stage3_fund_records WHERE record_id=?', (record_id,))
        if not rows:
            raise RegistryError('fund_record_not_found', status=404)
        return rows[0]

    @staticmethod
    def _public(row):
        result = dict(row)
        result['mode'] = result['execution_mode']
        result['mode_label'] = MODE_LABELS[result['execution_mode']]
        result['deltas_yen'] = {bucket: result.pop('delta_' + bucket) for bucket in BUCKETS}
        result['provider_verified'] = False
        result['live_execution_enabled'] = False
        return result

    def get(self, actor, record_id):
        self.store.require_ready()
        row = self._raw(record_id)
        self.store.authorize(actor, customer_id=row['customer_id'])
        return self._public(row)

    def summary(self, actor, customer_id, mode='test'):
        mode = _mode(mode)
        self.store.require_ready()
        self.store.authorize(actor, customer_id=customer_id)
        row = self.store.query('SELECT ' + ','.join('COALESCE(SUM(delta_' + b + '),0) AS ' + b for b in BUCKETS) +
                               ',COUNT(*) AS record_count FROM stage3_fund_records WHERE customer_id=? AND execution_mode=?',
                               (customer_id, mode))[0]
        count = int(row.pop('record_count'))
        # PostgreSQL SUM(BIGINT) returns numeric/Decimal. These are already
        # integer database values, not user input or rounded monetary floats.
        row = {bucket: int(row[bucket]) for bucket in BUCKETS}
        return {'customer_id': customer_id, 'mode': mode, 'mode_label': MODE_LABELS[mode], 'currency': 'JPY',
                'balances_yen': row, 'record_count': count, 'provider_verified': False,
                'live_execution_enabled': False, 'sales_to_purchase_credit_enabled': False}

    def history(self, actor, customer_id, mode='test'):
        mode = _mode(mode)
        self.store.require_ready()
        self.store.authorize(actor, customer_id=customer_id)
        return [self._public(row) for row in self.store.query(
            'SELECT * FROM stage3_fund_records WHERE customer_id=? AND execution_mode=? ORDER BY recorded_at,record_id',
            (customer_id, mode))]

    def _children(self, record_id):
        return self.store.query('SELECT * FROM stage3_fund_records WHERE original_record_id=?', (record_id,))

    def _available_origin(self, origin, children):
        # Every child consumes its parent's remaining amount. Reversal is a
        # full, terminal reversal; it cannot replenish an earlier allocation.
        used = 0
        for row in children:
            consumed = row['amount_yen']
            # A returned/reversed payment reopens its invoice. Reversing an
            # unconsumed receipt makes the original sale unpaid again. A
            # purchase refund does NOT reopen an already consumed reservation.
            if (origin['kind'], row['kind']) in (('invoice', 'payment'), ('sales_record', 'sales_receipt')):
                reversals = self._children(row['record_id'])
                consumed -= sum(child['amount_yen'] for child in reversals
                                if child['kind'] == 'correction' or
                                (row['kind'] == 'payment' and child['kind'] == 'refund'))
            used += consumed
        return origin['amount_yen'] - used

    def _lock_item(self, actor, physical_item_id):
        # The PostgreSQL item lock covers different customers contending for
        # the same object. SQLite's preceding customer write lock serializes
        # writers. No state or owner is updated here.
        self.store.item(actor, physical_item_id, write=True)
        suffix = ' FOR UPDATE' if self.store.registry.dialect == 'postgres' else ''
        self.store.query('SELECT physical_item_id FROM business_items WHERE physical_item_id=?' + suffix,
                         (physical_item_id,))

    def _assert_item_unclaimed(self, physical_item_id, mode):
        reservations = self.store.query('SELECT * FROM stage3_fund_records WHERE physical_item_id=? AND execution_mode=? AND kind=?',
                                         (physical_item_id, mode, 'reserve'))
        for reservation in reservations:
            children = self._children(reservation['record_id'])
            held = self._available_origin(reservation, children)
            committed = sum(row['amount_yen'] - sum(child['amount_yen'] for child in self._children(row['record_id']))
                            for row in children if row['kind'] == 'purchase')
            if held > 0 or committed > 0:
                raise RegistryError('fund_item_already_reserved', '同じ個品には予約・購入の記録が残っています。', 409)

    def post(self, actor, customer_id, *, kind, amount_yen, mode='test', occurred_at,
             source_key, evidence_reference, original_record_id=None, physical_item_id=None, note=None):
        """Append one movement under a customer lock; caller owns commit.

        For linked movements supply the originating record. Purchase/release
        consume a reservation, payment consumes an invoice, and every sales
        movement consumes a preceding state record. Each source is capped.
        Corrections fully reverse unconsumed deposits/invoices/payments/sales
        entries only; purchases are refunded and reservations released instead.
        """
        if kind not in KINDS:
            raise RegistryError('fund_kind_invalid')
        amount_yen, mode = _amount(amount_yen), _mode(mode)
        occurred_at = _time(occurred_at)
        source_key = _text(source_key, 'source_key', 180)
        evidence_reference = _text(evidence_reference, 'evidence_reference', 2000)
        if note is not None:
            note = _text(note, 'note', 1000)
        if physical_item_id is not None:
            physical_item_id = _text(physical_item_id, 'physical_item_id', 200)
        if original_record_id is not None:
            original_record_id = _text(original_record_id, 'original_record_id', 200)
        payload = dict(kind=kind, amount_yen=amount_yen, mode=mode, occurred_at=occurred_at,
                       evidence_reference=evidence_reference, original_record_id=original_record_id,
                       physical_item_id=physical_item_id, note=note)

        def create():
            origin = None
            children = []
            item_id = physical_item_id
            standalone = kind in ('deposit', 'reserve', 'invoice', 'sales_record')
            if standalone != (original_record_id is None):
                raise RegistryError('fund_origin_required_or_unexpected')
            if original_record_id:
                origin = self._raw(original_record_id)
                if origin['customer_id'] != customer_id or origin['execution_mode'] != mode:
                    raise RegistryError('fund_origin_scope_mismatch', status=409)
                if occurred_at < origin['occurred_at']:
                    raise RegistryError('fund_event_precedes_origin', status=409)
                if item_id and item_id != origin['physical_item_id']:
                    raise RegistryError('fund_origin_item_mismatch', status=409)
                item_id = origin['physical_item_id']
                children = self._children(original_record_id)
                if any(row['kind'] == 'correction' for row in children):
                    raise RegistryError('fund_origin_reversed', status=409)
            if item_id:
                self._lock_item(actor, item_id)
            if kind == 'reserve':
                if not item_id:
                    raise RegistryError('fund_reservation_item_required')
                self._assert_item_unclaimed(item_id, mode)
            if kind == 'sales_record' and item_id:
                item = self.store.item(actor, item_id, write=True)
                if item['owner_user_id'] != customer_id:
                    raise RegistryError('fund_sale_customer_mismatch', status=409)
            deltas = {bucket: 0 for bucket in BUCKETS}

            def move(bucket, sign):
                deltas[bucket] += sign * amount_yen

            def consume(allowed):
                if origin['kind'] not in allowed:
                    raise RegistryError('fund_origin_kind_invalid', status=409)
                if amount_yen > self._available_origin(origin, children):
                    raise RegistryError('fund_origin_amount_exceeded', '元記録の未処理額を超えています。', 409)

            if kind == 'deposit':
                move('purchase_available', 1)
            elif kind == 'reserve':
                move('purchase_available', -1)
                move('purchase_reserved', 1)
            elif kind in ('release', 'purchase'):
                consume(('reserve',))
                move('purchase_reserved', -1)
                if kind == 'release':
                    move('purchase_available', 1)
            elif kind == 'invoice':
                move('fees_payable', 1)
            elif kind == 'payment':
                consume(('invoice',))
                move('fees_payable', -1)
            elif kind == 'sales_record':
                move('sales_unpaid', 1)
            elif kind == 'sales_receipt':
                consume(('sales_record',))
                move('sales_unpaid', -1)
                move('sales_unsettled', 1)
            elif kind == 'sales_hold':
                consume(('sales_receipt', 'sales_release', 'sales_ready'))
                move('sales_settlement_ready' if origin['kind'] == 'sales_ready' else 'sales_unsettled', -1)
                move('sales_return_hold', 1)
            elif kind == 'sales_release':
                consume(('sales_hold',))
                move('sales_return_hold', -1)
                move('sales_unsettled', 1)
            elif kind == 'sales_ready':
                consume(('sales_receipt', 'sales_release'))
                move('sales_unsettled', -1)
                move('sales_settlement_ready', 1)
            elif kind == 'sales_settlement':
                consume(('sales_ready',))
                move('sales_settlement_ready', -1)
            elif kind == 'refund':
                consume(('deposit', 'purchase', 'payment', 'sales_hold'))
                bucket, sign = {'deposit': ('purchase_available', -1), 'purchase': ('purchase_available', 1),
                                'payment': ('fees_payable', 1), 'sales_hold': ('sales_return_hold', -1)}[origin['kind']]
                move(bucket, sign)
            elif kind == 'correction':
                if origin['kind'] not in ('deposit', 'invoice', 'payment', 'sales_record', 'sales_receipt'):
                    raise RegistryError('fund_correction_requires_specific_reversal', status=409)
                if children or amount_yen != origin['amount_yen']:
                    raise RegistryError('fund_correction_requires_unconsumed_full_origin', status=409)
                deltas = {bucket: -origin['delta_' + bucket] for bucket in BUCKETS}

            before = self.summary(actor, customer_id, mode)['balances_yen']
            for bucket in BUCKETS:
                after = before[bucket] + deltas[bucket]
                if after < 0:
                    raise RegistryError('fund_insufficient_' + bucket, '記録上の利用可能額を超えています。', 409)
                if after > MAX_YEN:
                    raise RegistryError('fund_balance_limit_exceeded', status=409)
            record_id = 'fr_' + uuid.uuid4().hex
            columns = ('record_id', 'customer_id', 'actor_user_id', 'execution_mode', 'kind', 'amount_yen',
                       'occurred_at', 'recorded_at', 'evidence_reference', 'source_key', 'original_record_id',
                       'physical_item_id', 'note') + tuple('delta_' + bucket for bucket in BUCKETS)
            values = (record_id, customer_id, actor.user_id, mode, kind, amount_yen, occurred_at,
                      datetime.now(timezone.utc).isoformat(timespec='microseconds'), evidence_reference, source_key,
                      original_record_id, item_id, note) + tuple(deltas[bucket] for bucket in BUCKETS)
            self.store.execute('INSERT INTO stage3_fund_records (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in values) + ')', values)
            return {'record': self._public(self._raw(record_id)), 'summary': self.summary(actor, customer_id, mode),
                    'live_execution_enabled': False}

        return self.store.operation(actor, customer_id, 'funds:' + source_key, payload, create)
