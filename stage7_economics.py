"""Finance-only operating evidence and exact, non-billing comparisons.

No customer funds, sales settlement, provider calls, inferred zero costs or
automatic bill creation. All scenario quantities describe one declared period.
"""
import json
import re
import uuid
from decimal import Decimal, localcontext, ROUND_HALF_UP
from fractions import Fraction

from item_registry import RegistryError, _json, _now, _text
from stage3_common import yen

SCOPES = ('free_digital', 'paid_digital', 'physical', 'fixed')
COST_CATEGORIES = ('image_storage', 'database', 'logs', 'backup', 'notifications',
                   'payments', 'support', 'hosting', 'materials', 'shipping',
                   'staff_salary_fixed', 'staff_time_allocated', 'other')
RESOURCE_UNITS = {'image_count': 'count', 'image_bytes': 'bytes', 'database_bytes': 'bytes',
                  'log_bytes': 'bytes', 'backup_bytes': 'bytes', 'notification_attempts': 'count',
                  'payment_transactions': 'count', 'support_minutes': 'minutes'}
BASIS = ('measured', 'assumption', 'reference')
TAX_BASIS = ('exclusive', 'inclusive', 'unknown')
COUNT_FIELDS = ('free_accounts', 'paid_accounts', 'receipts', 'work_minutes_per_received',
                'work_minutes_per_sold', 'other_staff_minutes', 'available_staff_minutes', 'opening_inventory', 'shelf_capacity')
MONEY_FIELDS = ('paid_monthly_price_yen', 'physical_fee_per_sale_yen', 'free_digital_per_account_yen',
                'paid_digital_per_account_yen', 'physical_other_per_received_yen',
                'physical_other_per_sold_yen', 'fixed_expenses_yen', 'hourly_labor_yen',
                'fixed_salary_yen', 'working_capital_yen', 'purchase_cash_per_received_yen',
                'operating_cash_outflow_yen')
RATE_FIELDS = ('sellthrough_bps', 'digital_payment_fee_bps', 'physical_payment_fee_bps')
SCENARIO_FIELDS = set(COUNT_FIELDS + MONEY_FIELDS + RATE_FIELDS + ('labor_mode', 'tax_basis', 'basis', 'reference', 'timing_basis'))


def _period(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', value):
        raise RegistryError('economic_period_required', '対象月は YYYY-MM で指定してください。')
    return value


def _choice(value, allowed, code):
    if not isinstance(value, str) or value not in allowed:
        raise RegistryError(code)
    return value


def _quantity(value, optional=True):
    if value is None and optional:
        return None
    if type(value) is not int or value < 0 or value > 9_000_000_000_000:
        raise RegistryError('economic_integer_required', '未確認の値は空欄にし、確認済みの数量は非負の整数で指定してください。')
    return value


def _number(value):
    """Exact numerator/denominator plus display-only half-up, never invoice rounding."""
    if value is None:
        return {'value': None, 'numerator': None, 'denominator': None}
    value = Fraction(value)
    with localcontext() as context:
        context.prec = 60
        shown = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return {'value': int(value) if value.denominator == 1 else format(shown, '.2f'),
            'numerator': value.numerator, 'denominator': value.denominator}


def _mul(*values):
    # An explicit zero population entails zero variable cost even if its unit
    # price is not yet known. Never do this for a missing/nonzero population.
    if any(value == 0 for value in values if value is not None):
        return Fraction(0)
    return None if any(value is None for value in values) else _product(values)


def _product(values):
    result = Fraction(1)
    for value in values:
        result *= value
    return result


def _add(*values):
    return None if any(value is None for value in values) else sum(values, Fraction(0))


def calculate_scenario(inputs):
    """A sensitivity model, not real revenue, tax computation or charge approval.

    Every accepted item consumes receipt work and other receipt cost, including
    ultimately unsold items. Sale work/fees apply to the expected sold subset.
    Monthly profit requires an explicit steady-state throughput assumption.
    Shelf safety uses the peak before any outgoing shipment, not final stock.
    """
    if not isinstance(inputs, dict) or set(inputs) - SCENARIO_FIELDS:
        raise RegistryError('economic_scenario_fields_invalid')
    values = {}
    for key in COUNT_FIELDS:
        values[key] = _quantity(inputs.get(key))
    for key in MONEY_FIELDS:
        values[key] = yen(inputs.get(key), optional=True)
    for key in RATE_FIELDS:
        rate = _quantity(inputs.get(key))
        if rate is not None and rate > 10000:
            raise RegistryError('economic_rate_out_of_range')
        values[key] = None if rate is None else Fraction(rate, 10000)
    mode = _choice(inputs.get('labor_mode'), ('time_allocated', 'fixed_salary'), 'economic_labor_mode_required')
    tax = _choice(inputs.get('tax_basis'), TAX_BASIS, 'economic_tax_basis_required')
    timing = _choice(inputs.get('timing_basis', 'unknown'), ('unknown','steady_state_assumption'), 'economic_timing_basis_required')
    basis = _choice(inputs.get('basis'), ('assumption', 'reference'), 'economic_scenario_is_not_actual_profit')
    reference = _text(inputs.get('reference'), 'reference', 1000)
    unused_labor = 'fixed_salary_yen' if mode == 'time_allocated' else 'hourly_labor_yen'
    if values[unused_labor] is not None:
        raise RegistryError('economic_salary_double_count', '給与は固定費か時間配賦の一方だけを指定してください。')
    v = values
    sold = _mul(v['receipts'], v['sellthrough_bps'])
    unsold = None if sold is None or v['receipts'] is None else v['receipts'] - sold
    digital_revenue = _mul(v['paid_accounts'], v['paid_monthly_price_yen'])
    physical_revenue = _mul(sold, v['physical_fee_per_sale_yen'])
    revenue = _add(digital_revenue, physical_revenue)
    digital_cost = _add(_mul(v['free_accounts'], v['free_digital_per_account_yen']),
                        _mul(v['paid_accounts'], v['paid_digital_per_account_yen']))
    receipt_minutes = _mul(v['receipts'], v['work_minutes_per_received'])
    sale_minutes = _mul(sold, v['work_minutes_per_sold'])
    minutes = _add(receipt_minutes, sale_minutes, v['other_staff_minutes'])
    labor = _mul(minutes, v['hourly_labor_yen'], Fraction(1, 60)) if mode == 'time_allocated' else v['fixed_salary_yen']
    variable_physical = _add(_mul(v['receipts'], v['physical_other_per_received_yen']),
                             _mul(sold, v['physical_other_per_sold_yen']))
    payment_fees = _add(_mul(digital_revenue, v['digital_payment_fee_bps']),
                        _mul(physical_revenue, v['physical_payment_fee_bps']))
    total_cost = _add(digital_cost, variable_physical, labor, payment_fees, v['fixed_expenses_yen'])
    profit = None if revenue is None or total_cost is None else revenue - total_cost
    inventory = _add(v['opening_inventory'], unsold)
    peak_inventory = _add(v['opening_inventory'], v['receipts'])
    purchase_cash = _mul(v['receipts'], v['purchase_cash_per_received_yen'])
    capital = _add(purchase_cash, v['operating_cash_outflow_yen'])
    raw = dict(expected_sold=sold, expected_unsold=unsold, digital_revenue_yen=digital_revenue,
               physical_revenue_yen=physical_revenue, total_revenue_yen=revenue,
               digital_cost_yen=digital_cost, physical_nonlabor_cost_yen=variable_physical,
               labor_cost_yen=labor, payment_fees_yen=payment_fees, total_cost_yen=total_cost,
               expected_profit_yen=profit, receipt_work_minutes=receipt_minutes,
               sale_work_minutes=sale_minutes, other_staff_minutes=v['other_staff_minutes'], required_staff_minutes=minutes,
               projected_inventory=inventory, peak_inventory=peak_inventory, purchase_cash_required_yen=purchase_cash,
               required_working_capital_yen=capital)
    # Tax unknown: retain physical counts and known subtotals but no combined
    # monetary result/profit that could be mistaken for a comparable net result.
    if tax == 'unknown' or timing == 'unknown':
        for key in ('total_revenue_yen', 'total_cost_yen', 'expected_profit_yen'):
            raw[key] = None
        profit = None
    if timing == 'unknown':
        raw['required_staff_minutes'] = None
    def fits(required, available):
        return None if required is None or available is None else required <= available
    unknown = [key for key in COUNT_FIELDS + MONEY_FIELDS + RATE_FIELDS if inputs.get(key) is None and key != unused_labor]
    if tax == 'unknown':
        unknown.append('tax_basis')
    if timing == 'unknown':
        unknown.append('timing_basis')
    return {'model': 'comparison_only', 'basis': basis, 'reference': reference,
            'tax_basis': tax, 'timing_basis': timing, 'labor_mode': mode, 'metrics': {key: _number(value) for key, value in raw.items()},
            'checks': {'profit_positive': None if profit is None else profit > 0,
                       'staff_capacity': fits(None if timing=='unknown' else minutes, v['available_staff_minutes']),
                       'shelf_capacity': fits(peak_inventory, v['shelf_capacity']),
                       'funding_capacity': fits(capital, v['working_capital_yen'])},
            'unknown_fields': unknown,
            'excluded': ['customer_purchase_balances', 'customer_unsettled_sales', 'consulting_one_off_receipts'],
            'warnings': ['比較用の入力値です。実績利益・請求額・料金の承認ではありません。',
                         '価格・費用は同じ税区分で入力してください。税額の自動計算は行いません。',
                         '固定経費には給与・変動費を含めず、時間配賦と固定給与を重ねないでください。',
                         'その他作業時間には仕入れ・移動・問合せ・返品・再作業等を含めてください。未計測を0分に置き換えないでください。',
                         '最終売却率を当月売却率とは扱いません。月次の利益・人時は、受付と売却が毎月安定している仮定を明示した場合だけ比較します。初月や長期滞留の収支は別途追跡が必要です。',
                         '受付全件の作業費を含みます。棚容量は期首在庫と受付全件が同時に存在する最大点数で比較します。最終残数とは別です。',
                         '必要資金は受付数×仕入支払額と明示した運営支払額の合計です。入金前に必要な資金を保守的に比較し、顧客の預り資金を含めません。']}


class EconomicsService:
    def __init__(self, store):
        self.store = store

    def _authorize(self, actor):
        self.store.authorize(actor, staff_required=True, finance=True)

    def _latest(self, entry_id):
        rows = self.store.query('''SELECT e.*,r.revision_id,r.revision,r.previous_revision_id,
            r.amount_yen,r.quantity,r.basis,r.tax_basis,r.evidence_reference,r.reason,r.voided
            FROM stage7_economic_entries e JOIN stage7_economic_revisions r ON r.entry_id=e.entry_id
            WHERE e.entry_id=? ORDER BY r.revision DESC LIMIT 1''', (entry_id,))
        if not rows:
            raise RegistryError('economic_entry_not_found', status=404)
        return rows[0]

    def _validate_cost(self, scope, category, amount_yen, basis, tax_basis):
        _choice(scope, SCOPES, 'economic_scope_invalid')
        _choice(category, COST_CATEGORIES, 'economic_category_invalid')
        _choice(basis, BASIS, 'economic_basis_required')
        _choice(tax_basis, TAX_BASIS, 'economic_tax_basis_required')
        yen(amount_yen, optional=True)
        if category == 'staff_salary_fixed' and scope != 'fixed':
            raise RegistryError('economic_fixed_salary_scope')

    def _check_salary(self, period, category, entry_id=None, voided=False):
        opposite = {'staff_salary_fixed': 'staff_time_allocated', 'staff_time_allocated': 'staff_salary_fixed'}.get(category)
        if not opposite or voided:
            return
        for row in self._rows(period, 'cost'):
            if row['entry_id'] != entry_id and not row['voided'] and row['metric'] == opposite:
                raise RegistryError('economic_salary_double_count', '同じ月の給与を固定費と時間配賦の両方では記録できません。')

    def _revision(self, actor, entry_id, previous, *, amount_yen=None, quantity=None, basis,
                  tax_basis, reference, reason='', voided=False):
        revision_id = uuid.uuid4().hex
        revision = previous['revision'] + 1 if previous else 1
        self.store.execute('''INSERT INTO stage7_economic_revisions(revision_id,entry_id,revision,
            previous_revision_id,amount_yen,quantity,basis,tax_basis,evidence_reference,reason,
            voided,actor_user_id,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (revision_id, entry_id, revision, previous['revision_id'] if previous else None,
             amount_yen, quantity, basis, tax_basis, reference, reason, int(voided), actor.user_id, _now()))
        return self._latest(entry_id)

    def record_cost(self, actor, *, period, scope, category, amount_yen, basis, tax_basis,
                    reference, source_key, note=''):
        self._authorize(actor)
        period = _period(period)
        self._validate_cost(scope, category, amount_yen, basis, tax_basis)
        reference = _text(reference, 'reference', 1000)
        if not isinstance(note, str) or len(note) > 2000:
            raise RegistryError('economic_note_invalid')
        payload = dict(action='cost', period=period, scope=scope, category=category,
                       amount_yen=amount_yen, basis=basis, tax_basis=tax_basis, reference=reference, note=note)
        def write():
            self._check_salary(period, category)
            if self.store.query("SELECT entry_id FROM stage7_economic_entries WHERE kind='cost' AND period=? AND source_reference=?", (period, reference)):
                raise RegistryError('economic_duplicate_evidence', '同じ原価明細が既にあります。訂正を使用してください。', 409)
            entry_id = uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage7_economic_entries(entry_id,kind,period,scope,metric,
                source_reference,actor_user_id,recorded_at) VALUES(?,'cost',?,?,?,?,?,?)''',
                (entry_id, period, scope, category, reference, actor.user_id, _now()))
            return self._revision(actor, entry_id, None, amount_yen=amount_yen, basis=basis,
                                  tax_basis=tax_basis, reference=reference, reason=note)
        return self.store.operation(actor, source_key, payload, write, staff_required=True, finance=True)

    def record_resource(self, actor, *, period, scope, metric, quantity, basis, reference, source_key):
        self._authorize(actor)
        period = _period(period)
        _choice(scope, SCOPES, 'economic_scope_invalid')
        _choice(metric, RESOURCE_UNITS, 'economic_resource_invalid')
        _choice(basis, BASIS, 'economic_basis_required')
        _quantity(quantity)
        reference = _text(reference, 'reference', 1000)
        payload = dict(action='resource', period=period, scope=scope, metric=metric, quantity=quantity, basis=basis, reference=reference)
        def write():
            if self.store.query("SELECT entry_id FROM stage7_economic_entries WHERE kind='resource' AND period=? AND (source_reference=? OR (scope=? AND metric=?))", (period, reference, scope, metric)):
                raise RegistryError('economic_duplicate_resource', '同じ月・対象・数量の記録は訂正して更新してください。', 409)
            entry_id = uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage7_economic_entries(entry_id,kind,period,scope,metric,
                source_reference,actor_user_id,recorded_at) VALUES(?,'resource',?,?,?,?,?,?)''',
                (entry_id, period, scope, metric, reference, actor.user_id, _now()))
            return self._revision(actor, entry_id, None, quantity=quantity, basis=basis,
                                  tax_basis='not_applicable', reference=reference)
        return self.store.operation(actor, source_key, payload, write, staff_required=True, finance=True)

    def correct_cost(self, actor, entry_id, *, amount_yen, basis, tax_basis, reference, reason, source_key, voided=False):
        return self._correct(actor, entry_id, kind='cost', amount_yen=amount_yen, quantity=None,
                             basis=basis, tax_basis=tax_basis, reference=reference, reason=reason, source_key=source_key, voided=voided)

    def correct_resource(self, actor, entry_id, *, quantity, basis, reference, reason, source_key, voided=False):
        return self._correct(actor, entry_id, kind='resource', amount_yen=None, quantity=quantity,
                             basis=basis, tax_basis='not_applicable', reference=reference, reason=reason, source_key=source_key, voided=voided)

    def _correct(self, actor, entry_id, *, kind, amount_yen, quantity, basis, tax_basis, reference, reason, source_key, voided):
        self._authorize(actor)
        reference, reason = _text(reference, 'reference', 1000), _text(reason, 'reason', 2000)
        _choice(basis, BASIS, 'economic_basis_required')
        if type(voided) is not bool:
            raise RegistryError('economic_void_boolean_required')
        _quantity(quantity)
        yen(amount_yen, optional=True)
        payload = dict(action='correction', entry_id=entry_id, kind=kind, amount_yen=amount_yen,
                       quantity=quantity, basis=basis, tax_basis=tax_basis, reference=reference, reason=reason, voided=voided)
        def write():
            old = self._latest(entry_id)
            if old['kind'] != kind:
                raise RegistryError('economic_entry_kind_mismatch')
            if kind == 'cost':
                self._validate_cost(old['scope'], old['metric'], amount_yen, basis, tax_basis)
                self._check_salary(old['period'], old['metric'], entry_id, voided)
            return self._revision(actor, entry_id, old, amount_yen=amount_yen, quantity=quantity,
                                  basis=basis, tax_basis=tax_basis, reference=reference, reason=reason, voided=voided)
        return self.store.operation(actor, source_key, payload, write, staff_required=True, finance=True)

    def history(self, actor, entry_id):
        self._authorize(actor)
        entry = self._latest(entry_id)
        entry['history'] = self.store.query('SELECT * FROM stage7_economic_revisions WHERE entry_id=? ORDER BY revision', (entry_id,))
        return entry

    def _rows(self, period, kind):
        return self.store.query('''SELECT e.*,r.revision_id,r.revision,r.amount_yen,r.quantity,
            r.basis,r.tax_basis,r.evidence_reference,r.reason,r.voided
            FROM stage7_economic_entries e JOIN stage7_economic_revisions r ON r.entry_id=e.entry_id
            WHERE e.period=? AND e.kind=? AND NOT EXISTS (SELECT 1 FROM stage7_economic_revisions later
                WHERE later.entry_id=r.entry_id AND later.revision>r.revision)
            ORDER BY e.recorded_at,e.entry_id''', (period, kind))

    def cost_summary(self, actor, period, denominators=None):
        self._authorize(actor)
        period = _period(period)
        denominators = denominators or {}
        if not isinstance(denominators, dict) or set(denominators) - {'free_digital', 'paid_digital', 'physical'}:
            raise RegistryError('economic_denominators_invalid')
        for value in denominators.values():
            _quantity(value)
        rows = self._rows(period, 'cost')
        resources = self._rows(period, 'resource')
        scopes = {}
        for scope in SCOPES:
            active = [row for row in rows if row['scope'] == scope and not row['voided']]
            taxes = {row['tax_basis'] for row in active}
            bases = {row['basis'] for row in active}
            known = sum(row['amount_yen'] for row in active if row['amount_yen'] is not None)
            unknown = sum(row['amount_yen'] is None for row in active)
            comparable = len(taxes) == 1 and 'unknown' not in taxes and len(bases) == 1
            total = known if active and not unknown and comparable else None
            denominator = denominators.get(scope)
            unit = Fraction(total, denominator) if total is not None and denominator not in (None, 0) else None
            by_tax = {tax: sum(row['amount_yen'] for row in active if row['tax_basis'] == tax and row['amount_yen'] is not None) for tax in sorted(taxes)} if len(bases) == 1 else {}
            by_basis = {}
            for basis in sorted(bases):
                selected = [row for row in active if row['basis'] == basis]
                basis_taxes = {row['tax_basis'] for row in selected}
                basis_unknown = sum(row['amount_yen'] is None for row in selected)
                sums = {tax: sum(row['amount_yen'] for row in selected if row['tax_basis'] == tax and row['amount_yen'] is not None) for tax in sorted(basis_taxes)}
                by_basis[basis] = {'known_subtotal_by_tax': sums, 'unknown_count': basis_unknown,
                    'total_yen': sum(sums.values()) if len(basis_taxes) == 1 and 'unknown' not in basis_taxes and not basis_unknown else None}
            scopes[scope] = dict(total_yen=total, known_subtotal_by_tax=by_tax, unknown_count=unknown,
                                 entry_count=len(active), basis=sorted(bases), by_basis=by_basis,
                                 tax_basis=next(iter(taxes)) if len(taxes) == 1 else 'mixed' if taxes else 'unknown',
                                 denominator=denominator, per_unit_yen=_number(unit),
                                 complete_operating_cost=False)
        for row in resources:
            row['unit'] = RESOURCE_UNITS[row['metric']]
        return {'period': period, 'scopes': scopes, 'entries': rows, 'resources': resources,
                'basis': 'recorded_evidence_only', 'complete_operating_cost': False,
                'note': '未記録の原価は0円とみなしません。登録済み明細の小計です。数量の訂正は元記録を残し、税区分や実測・仮定・参考の異なる金額は合算しません。同じ原価の推定から実測への変更は訂正してください。'}

    def save_scenario(self, actor, *, name, period, inputs, source_key):
        self._authorize(actor)
        name, period = _text(name, 'name', 200), _period(period)
        result = calculate_scenario(inputs)
        payload = dict(action='scenario', name=name, period=period, inputs=inputs)
        def write():
            scenario_id = uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage7_economic_scenarios(scenario_id,period,name,
                inputs_json,result_json,actor_user_id,recorded_at) VALUES(?,?,?,?,?,?,?)''',
                (scenario_id, period, name, _json(inputs), _json(result), actor.user_id, _now()))
            return dict(scenario_id=scenario_id, period=period, name=name, inputs=inputs, result=result)
        return self.store.operation(actor, source_key, payload, write, staff_required=True, finance=True)

    def scenarios(self, actor, period=None):
        self._authorize(actor)
        where, args = (' WHERE period=?', (_period(period),)) if period is not None else ('', ())
        rows = self.store.query('SELECT * FROM stage7_economic_scenarios' + where + ' ORDER BY recorded_at,scenario_id', args)
        return [dict(scenario_id=row['scenario_id'], period=row['period'], name=row['name'],
                     inputs=json.loads(row['inputs_json']), result=json.loads(row['result_json'])) for row in rows]
