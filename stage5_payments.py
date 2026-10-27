"""Reviewed physical bills and an intentionally offline test PSP state machine.

Neither this module nor its memory adapter accepts card numbers, contacts a PSP,
finalizes real invoices, changes purchase balances, or approves draft contracts.
Provider callbacks are hints only: reconciliation retrieves the object from a
trusted injected adapter and binds every identity/amount before accepting state.
An adapter for a real PSP needs a durable outbox/commit boundary and separate
sandbox/live readiness work; the only executable adapter here is memory-only.
"""
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
from threading import RLock
import uuid

from item_registry import RegistryError, _json, _now, _text
from stage3_billing import BillingService, invoice_totals
from stage3_common import yen


def _digest(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _id(prefix):
    return prefix + uuid.uuid4().hex


@dataclass(frozen=True)
class RetrievedTestObject:
    """Only a trusted adapter may construct this value; never deserialize HTTP into it."""
    provider_id: str
    object_kind: str
    revision: int
    status: str
    amount_yen: int
    currency: str
    customer_reference: str
    metadata: dict
    idempotency_key: str
    payment_id: str = ''
    livemode: bool = False
    provider: str = 'memory_test'


class MemoryTestPSP:
    """Explicit test harness, not Stripe sandbox or proof of real card behavior."""
    mode = 'test'
    provider = 'memory_test'
    network_enabled = False

    def __init__(self):
        self.objects = {}
        self._lock = RLock()
        self.create_calls = 0
        self.next_payment_status = 'requires_action'
        self.next_refund_status = 'succeeded'
        self.timeout_after_create = False

    def _create(self, kind, request):
        with self._lock:
            return self._create_locked(kind, request)

    def _create_locked(self, kind, request):
        key = request['idempotency_key']
        if key not in self.objects:
            self.create_calls += 1
            self.objects[key] = RetrievedTestObject(
                provider_id=('pi_test_' if kind == 'payment' else 're_test_') + uuid.uuid4().hex,
                object_kind=kind, revision=1,
                status=self.next_payment_status if kind == 'payment' else self.next_refund_status,
                amount_yen=request['amount_yen'], currency=request['currency'],
                customer_reference=request['customer_reference'], metadata=deepcopy(request['metadata']),
                idempotency_key=key, payment_id=request.get('payment_id', ''))
        if self.timeout_after_create:
            raise TimeoutError('simulated timeout after provider accepted the request')
        return self.retrieve(key)

    def create_payment(self, request):
        return self._create('payment', request)

    def create_refund(self, request):
        return self._create('refund', request)

    def retrieve(self, idempotency_key):
        with self._lock:
            return deepcopy(self.objects.get(idempotency_key))

    def advance(self, idempotency_key, status, **changes):
        """Fixture-only external action, e.g. customer completes test authentication."""
        with self._lock:
            old = self.objects[idempotency_key]
            values = {**old.__dict__, 'revision': old.revision + 1, 'status': status, **changes}
            self.objects[idempotency_key] = RetrievedTestObject(**values)


class PhysicalPaymentService:
    def __init__(self, store, adapter=None):
        self.store = store
        self.billing = BillingService(store.finance)
        # No production adapter is wired. Subclasses are also rejected so a
        # network implementation cannot silently inherit the memory-only label.
        if adapter is not None and type(adapter) is not MemoryTestPSP:
            raise RegistryError('physical_test_adapter_required', status=409)
        self.adapter = adapter

    def _operation(self, actor, customer_id, source_key, payload, callback, staff=True):
        return self.store.operation(actor, source_key, payload, callback,
                                    customer_id=customer_id, staff_required=staff, finance=True)

    def _row(self, actor, table, identifier, key):
        self.store.authorize(actor, finance=True)
        rows = self.store.query('SELECT * FROM ' + table + ' WHERE ' + key + '=?', (identifier,))
        if not rows:
            raise RegistryError('payment_record_not_found', status=404)
        self.store.authorize(actor, rows[0]['customer_id'], finance=True)
        return rows[0]

    def inspect_invoice(self, actor, invoice_id):
        invoice = self.billing.get_month(actor, invoice_id)
        excluded = {line['draft_id'] for line in invoice['snapshot']['lines'] if line['fee_kind'] == 'digital_monthly'}
        # A credit may be recorded in a later month than its digital original.
        # Its original ID still determines the same digital collection route.
        for line in invoice['snapshot']['lines']:
            if line.get('original_draft_id'):
                original = self.store.query('SELECT fee_kind FROM stage3_charge_drafts WHERE draft_id=? AND customer_id=?',
                    (line['original_draft_id'], invoice['customer_id']))
                if original and original[0]['fee_kind'] == 'digital_monthly':
                    excluded.add(line['draft_id'])
        lines = [line for line in invoice['snapshot']['lines']
                 if line['draft_id'] not in excluded and line.get('original_draft_id') not in excluded]
        reasons = []
        if invoice['timezone_name'] != 'Asia/Tokyo':
            reasons.append('physical_period_not_agreed')
        if not invoice['is_current_snapshot']:
            reasons.append('physical_invoice_stale')
        # The digital subscription is collected separately. Its missing/draft
        # line must never create a second monthly physical charge.
        wrappers = {'monthly_component_missing', 'unresolved_adjustments', 'no_agreed_period', 'conflicting_rounding'}
        reasons.extend(reason for reason in invoice['current_review_reasons'] if reason not in wrappers)
        methods = set()
        for line in lines:
            reasons.extend(invoice['current_line_review_reasons'].get(line['draft_id'], ['physical_line_unknown']))
            agreement = line['snapshot'].get('agreement_snapshot') or {}
            evidence = agreement.get('evidence') or {}
            if agreement.get('status') != 'fixture_approved' or agreement.get('customer_id') != invoice['customer_id']:
                reasons.append('physical_agreement_not_approved')
            if not evidence.get('consent_reference') or not evidence.get('signed_document_reference'):
                reasons.append('physical_contract_evidence_missing')
            if evidence.get('billing_timezone_approved') is not True or evidence.get('billing_timezone') != invoice['timezone_name']:
                reasons.append('physical_period_not_agreed')
            method = line['snapshot'].get('calculation', {}).get('rounding_method')
            methods.add(method)
            if evidence.get('rounding_scope') != 'invoice_per_rate':
                reasons.append('physical_rounding_scope_unconfirmed')
        method = next(iter(methods)) if len(methods) == 1 else None
        if method != 'floor':
            reasons.append('physical_rounding_not_agreed')
        totals = invoice_totals(lines, method)
        amount = totals['reference_gross_total_yen']
        if not lines or amount is None or amount <= 0:
            reasons.append('physical_positive_known_total_required')
        if totals['unknown_lines'] or totals['review_reasons']:
            reasons.append('physical_tax_or_amount_unresolved')
        return {'invoice_id': invoice_id, 'customer_id': invoice['customer_id'], 'period': invoice['period'],
                'timezone_name': invoice['timezone_name'], 'source_hash': invoice['content_hash'],
                'source_revision': invoice['revision'], 'lines': lines, 'totals': totals,
                'amount_yen': amount, 'currency': 'JPY', 'excluded_digital_draft_ids': sorted(excluded),
                'review_reasons': sorted(set(reasons)), 'execution_mode': 'test', 'real_payment_enabled': False}

    def prepare_invoice(self, actor, invoice_id, *, review_reference, source_key):
        inspected = self.inspect_invoice(actor, invoice_id)
        customer = inspected['customer_id']
        reference = _text(review_reference, 'review_reference', 1000)
        def create():
            current = self.inspect_invoice(actor, invoice_id)
            if current['review_reasons']:
                raise RegistryError('physical_invoice_requires_review', '未確定の料金・契約・明細はカード決済の検証へ進められません。', 409)
            existing = self.store.query('SELECT * FROM stage5_physical_bills WHERE customer_id=? AND period=? AND timezone_name=?',
                                        (customer, current['period'], current['timezone_name']))
            if existing:
                if existing[0]['invoice_id'] != invoice_id:
                    raise RegistryError('physical_period_already_prepared', '同じ月の別請求は自動再請求せず、訂正の確認が必要です。', 409)
                return self.get_bill(actor, existing[0]['bill_id'])
            bill = _id('pb5_')
            year, month = map(int, current['period'].split('-'))
            due = date(year + (month == 12), 1 if month == 12 else month + 1, 10).isoformat()
            self.store.execute('''INSERT INTO stage5_physical_bills(bill_id,invoice_id,customer_id,period,timezone_name,due_date,
                source_hash,snapshot_json,amount_yen,currency,review_reference,actor_user_id,recorded_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (bill, invoice_id, customer, current['period'], current['timezone_name'], due, current['source_hash'],
                 _json(current), current['amount_yen'], 'JPY', reference, actor.user_id, _now()))
            return self.get_bill(actor, bill)
        return self._operation(actor, customer, source_key,
            dict(action='physical_bill', invoice_id=invoice_id, review_reference=reference), create)

    def record_consent(self, actor, customer_id, *, terms_reference, payment_method_reference,
                       provider_customer_reference, off_session=False, source_key):
        reference = _text(terms_reference, 'terms_reference', 1000)
        if type(off_session) is not bool:
            raise RegistryError('consent_boolean_required')
        # Test placeholders, never card numbers or live provider credentials.
        for value, prefix in ((payment_method_reference, 'pm_test_'), (provider_customer_reference, 'cus_test_')):
            if not isinstance(value, str) or not re.fullmatch(prefix + '[A-Za-z0-9_-]{1,100}', value):
                raise RegistryError('test_payment_reference_required')
        payload = dict(action='physical_consent', terms_reference=reference, payment_method_reference=payment_method_reference,
                       provider_customer_reference=provider_customer_reference, off_session=off_session)
        def create():
            existing = self.store.query('SELECT * FROM stage5_card_consents WHERE customer_id=? AND terms_reference=?', (customer_id, reference))
            if existing:
                row = existing[0]
                if (row['payment_method_reference'], row['provider_customer_reference'], bool(row['off_session'])) != (payment_method_reference, provider_customer_reference, off_session):
                    raise RegistryError('consent_reference_conflict', status=409)
                return self._consent_projection(row)
            identifier = _id('cc5_')
            self.store.execute('''INSERT INTO stage5_card_consents(consent_id,customer_id,terms_reference,
                provider_customer_reference,payment_method_reference,off_session,actor_user_id,recorded_at) VALUES (?,?,?,?,?,?,?,?)''',
                (identifier, customer_id, reference, provider_customer_reference, payment_method_reference, int(off_session), actor.user_id, _now()))
            return self._consent_projection(self._row(actor, 'stage5_card_consents', identifier, 'consent_id'))
        return self._operation(actor, customer_id, source_key, payload, create, staff=False)

    def _consent_projection(self, row):
        return {k: row[k] for k in ('consent_id', 'customer_id', 'terms_reference', 'off_session', 'recorded_at')} | {
            'execution_mode': 'test', 'real_payment_enabled': False,
            'revoked': bool(self.store.query('SELECT consent_id FROM stage5_card_consent_revocations WHERE consent_id=?', (row['consent_id'],)))}

    def revoke_consent(self, actor, consent_id, *, reason, source_key):
        row = self._row(actor, 'stage5_card_consents', consent_id, 'consent_id')
        reason = _text(reason, 'reason', 1000)
        def create():
            self.store.execute('''INSERT INTO stage5_card_consent_revocations(consent_id,reason,actor_user_id,recorded_at)
                VALUES (?,?,?,?) ON CONFLICT(consent_id) DO NOTHING''', (consent_id, reason, actor.user_id, _now()))
            return self._consent_projection(row)
        return self._operation(actor, row['customer_id'], source_key,
            dict(action='physical_revoke_consent', consent_id=consent_id, reason=reason), create, staff=False)

    def _valid_consent(self, actor, consent_id, customer, off_session):
        consent = self._row(actor, 'stage5_card_consents', consent_id, 'consent_id')
        if consent['customer_id'] != customer:
            raise RegistryError('consent_customer_mismatch', status=409)
        if self._consent_projection(consent)['revoked']:
            raise RegistryError('payment_consent_revoked', status=409)
        if off_session and not consent['off_session']:
            raise RegistryError('off_session_consent_required', status=409)
        return consent

    def _fresh_bill(self, actor, bill):
        current = self.inspect_invoice(actor, bill['invoice_id'])
        if current['review_reasons'] or current['source_hash'] != bill['source_hash'] or current['amount_yen'] != bill['amount_yen']:
            raise RegistryError('physical_invoice_stale', status=409)

    def _state(self, kind, identifier):
        table, key = ('stage5_payment_events', 'attempt_id') if kind == 'payment' else ('stage5_refund_events', 'refund_id')
        rows = self.store.query('SELECT * FROM ' + table + ' WHERE ' + key + '=? ORDER BY sequence DESC', (identifier,))
        return rows[0] if rows else {'state': 'prepared', 'sequence': 0, 'provider_revision': None, 'snapshot_json': '{}'}

    def _event(self, kind, identifier, state, snapshot=None, provider_revision=None):
        table, key = ('stage5_payment_events', 'attempt_id') if kind == 'payment' else ('stage5_refund_events', 'refund_id')
        last = self._state(kind, identifier)
        self.store.execute('INSERT INTO ' + table + '(event_id,' + key + ',sequence,state,provider_revision,snapshot_json,recorded_at) VALUES (?,?,?,?,?,?,?)',
            (_id('pe5_'), identifier, last['sequence'] + 1, state, provider_revision, _json(snapshot or {}), _now()))

    def _require_payment_initiator(self, actor, customer_id, off_session):
        staff = self.store.authorize(actor, customer_id, finance=True)
        if off_session:
            self.store.authorize(actor, customer_id, staff_required=True, finance=True)
        elif staff or actor.user_id != customer_id:
            raise RegistryError('customer_payment_action_required',
                '毎回確認して支払う方式は、お客様本人が操作してください。スタッフによる請求には自動請求の同意が必要です。', 403)

    def create_attempt(self, actor, bill_id, consent_id, *, off_session=False, flow=None, source_key):
        bill = self._row(actor, 'stage5_physical_bills', bill_id, 'bill_id')
        if flow is not None:
            if flow not in ('customer_initiated', 'off_session'):
                raise RegistryError('payment_flow_invalid')
            if off_session and flow != 'off_session':
                raise RegistryError('payment_flow_conflict')
            off_session = flow == 'off_session'
        if type(off_session) is not bool:
            raise RegistryError('consent_boolean_required')
        self._require_payment_initiator(actor, bill['customer_id'], off_session)
        def create():
            self._fresh_bill(actor, bill)
            self._valid_consent(actor, consent_id, bill['customer_id'], off_session)
            # An explicitly connected Stripe sandbox uses a separate durable
            # ledger. Do not open a second memory-provider path for its bill.
            if self.store.dialect == 'postgres':
                present = self.store.query("SELECT to_regclass('stage9_payment_jobs') AS name")
            else:
                present = self.store.query("SELECT name FROM sqlite_master WHERE type='table' AND name='stage9_payment_jobs'")
            if present and present[0]['name'] and self.store.query("SELECT job_id FROM stage9_payment_jobs WHERE bill_id=? AND kind IN ('checkout','off_session')", (bill_id,)):
                raise RegistryError('payment_attempt_exists_reconcile_first', status=409)
            previous = self.store.query('SELECT * FROM stage5_payment_attempts WHERE bill_id=?', (bill_id,))
            # A failed payment intent is not proof it can never succeed later.
            # Only a retrieved terminal cancellation permits a new intent.
            if any(self._state('payment', row['attempt_id'])['state'] != 'canceled' for row in previous):
                raise RegistryError('payment_attempt_exists_reconcile_first', status=409)
            identifier = _id('pa5_')
            self.store.execute('''INSERT INTO stage5_payment_attempts(attempt_id,bill_id,consent_id,customer_id,amount_yen,
                currency,off_session,provider_idempotency_key,actor_user_id,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                (identifier, bill_id, consent_id, bill['customer_id'], bill['amount_yen'], 'JPY', int(off_session),
                 'test:physical:' + identifier, actor.user_id, _now()))
            self._event('payment', identifier, 'prepared')
            return self.get_attempt(actor, identifier)
        return self._operation(actor, bill['customer_id'], source_key,
            dict(action='physical_attempt', bill_id=bill_id, consent_id=consent_id, off_session=off_session), create, staff=off_session)

    def _adapter(self):
        if self.adapter is None:
            raise RegistryError('physical_test_adapter_unavailable', '決済は未接続です。実際のカードへ請求しません。', 409)
        return self.adapter

    def _request(self, actor, row, kind):
        attempt = row if kind == 'payment' else self._row(actor, 'stage5_payment_attempts', row['attempt_id'], 'attempt_id')
        consent = self._row(actor, 'stage5_card_consents', attempt['consent_id'], 'consent_id')
        request = dict(amount_yen=row['amount_yen'], currency=row['currency'],
            customer_reference=consent['provider_customer_reference'], idempotency_key=row['provider_idempotency_key'],
            metadata={'bill_id': attempt['bill_id'], 'customer_id': row['customer_id'], 'attempt_id': attempt['attempt_id'], 'execution_mode': 'test'})
        if kind == 'payment':
            request.update(payment_method_reference=consent['payment_method_reference'], off_session=bool(row['off_session']))
        else:
            paid = json.loads(self._state('payment', attempt['attempt_id'])['snapshot_json'])
            request['payment_id'] = paid['provider_id']
            request['metadata']['refund_id'] = row['refund_id']
        return request

    def _dispatch(self, actor, identifier, kind, source_key):
        adapter = self._adapter()
        table, key = ('stage5_payment_attempts', 'attempt_id') if kind == 'payment' else ('stage5_payment_refunds', 'refund_id')
        row = self._row(actor, table, identifier, key)
        if kind == 'payment':
            self._require_payment_initiator(actor, row['customer_id'], bool(row['off_session']))
        def mark():
            state = self._state(kind, identifier)['state']
            if state != 'prepared':
                return {'dispatch': False}
            if kind == 'payment':
                self._fresh_bill(actor, self._row(actor, 'stage5_physical_bills', row['bill_id'], 'bill_id'))
                self._valid_consent(actor, row['consent_id'], row['customer_id'], bool(row['off_session']))
            self._event(kind, identifier, 'processing')
            return {'dispatch': True}
        decision = self._operation(actor, row['customer_id'], source_key + ':mark',
            dict(action='physical_dispatch', identifier=identifier, kind=kind), mark,
            staff=kind == 'refund' or bool(row['off_session']))
        # Replays of the same source key may replay the saved decision. Check
        # the event stream too. A crash after marking may retry only the SAME
        # provider idempotency key; an unknown outcome never starts a new charge.
        current = self._state(kind, identifier)
        if decision['dispatch'] and current['state'] == 'processing' and current['sequence'] == 2:
            try:
                request = self._request(actor, row, kind)
                (adapter.create_payment if kind == 'payment' else adapter.create_refund)(request)
            except Exception:
                self._operation(actor, row['customer_id'], source_key + ':unknown',
                    dict(action='physical_dispatch_unknown', identifier=identifier, kind=kind),
                    lambda: self._unknown(kind, identifier), staff=kind == 'refund' or bool(row['off_session']))
                return self.get_attempt(actor, identifier) if kind == 'payment' else self.get_refund(actor, identifier)
        return self._reconcile(actor, identifier, kind, source_key + ':retrieve')

    def _unknown(self, kind, identifier):
        if self._state(kind, identifier)['state'] == 'processing':
            self._event(kind, identifier, 'unknown')
        return {'state': self._state(kind, identifier)['state']}

    def dispatch_attempt(self, actor, attempt_id, *, source_key):
        return self._dispatch(actor, attempt_id, 'payment', source_key)

    def dispatch_refund(self, actor, refund_id, *, source_key):
        return self._dispatch(actor, refund_id, 'refund', source_key)

    def _validate_snapshot(self, actor, row, kind, snapshot):
        if type(snapshot) is not RetrievedTestObject or snapshot.livemode is not False or snapshot.provider != 'memory_test':
            raise RegistryError('untrusted_payment_snapshot', status=409)
        expected = self._request(actor, row, kind)
        if (snapshot.object_kind != kind or snapshot.amount_yen != expected['amount_yen']
                or snapshot.currency != expected['currency'] or snapshot.customer_reference != expected['customer_reference']
                or snapshot.metadata != expected['metadata'] or snapshot.idempotency_key != expected['idempotency_key']
                or snapshot.payment_id != expected.get('payment_id', '') or not snapshot.provider_id
                or type(snapshot.revision) is not int or snapshot.revision < 1):
            raise RegistryError('payment_snapshot_mismatch', status=409)
        yen(snapshot.amount_yen)
        allowed = ('processing', 'requires_action', 'failed', 'succeeded', 'canceled') if kind == 'payment' else ('processing', 'failed', 'succeeded', 'canceled')
        if snapshot.status not in allowed:
            raise RegistryError('payment_snapshot_status_unknown', status=409)
        return deepcopy(snapshot.__dict__)

    def _reconcile(self, actor, identifier, kind, source_key):
        adapter = self._adapter()
        table, key = ('stage5_payment_attempts', 'attempt_id') if kind == 'payment' else ('stage5_payment_refunds', 'refund_id')
        row = self._row(actor, table, identifier, key)
        self.store.authorize(actor, row['customer_id'], staff_required=kind == 'refund' or bool(row['off_session']), finance=True)
        retrieved = adapter.retrieve(row['provider_idempotency_key'])
        if retrieved is None:
            raise RegistryError('payment_outcome_unknown_reconcile_required', status=409)
        snapshot = self._validate_snapshot(actor, row, kind, retrieved)
        def apply():
            previous = self._state(kind, identifier)
            rows = self.store.query('SELECT provider_revision,snapshot_json FROM ' + ('stage5_payment_events' if kind == 'payment' else 'stage5_refund_events') +
                ' WHERE ' + key + '=? AND provider_revision IS NOT NULL ORDER BY provider_revision DESC', (identifier,))
            if rows:
                latest = rows[0]
                old_snapshot = json.loads(latest['snapshot_json'])
                if old_snapshot['provider_id'] != retrieved.provider_id:
                    raise RegistryError('payment_provider_identity_changed', status=409)
                if retrieved.revision < latest['provider_revision']:
                    return self.get_attempt(actor, identifier) if kind == 'payment' else self.get_refund(actor, identifier)
                if retrieved.revision == latest['provider_revision']:
                    if snapshot != old_snapshot:
                        raise RegistryError('payment_provider_revision_conflict', status=409)
                    return self.get_attempt(actor, identifier) if kind == 'payment' else self.get_refund(actor, identifier)
            terminal = ('succeeded', 'canceled', 'failed') if kind == 'refund' else ('succeeded', 'canceled')
            if previous['state'] in terminal and previous['state'] != retrieved.status:
                raise RegistryError('payment_terminal_state_conflict', status=409)
            self._event(kind, identifier, retrieved.status, snapshot, retrieved.revision)
            return self.get_attempt(actor, identifier) if kind == 'payment' else self.get_refund(actor, identifier)
        # Bind the fetched revision into dedup payload. A callback ID alone is
        # neither payment evidence nor permission to ignore a later correction.
        return self._operation(actor, row['customer_id'], source_key,
            dict(action='physical_reconcile', identifier=identifier, kind=kind, snapshot_hash=_digest(snapshot)), apply,
            staff=kind == 'refund' or bool(row['off_session']))

    def reconcile_attempt(self, actor, attempt_id, *, source_key):
        return self._reconcile(actor, attempt_id, 'payment', source_key)

    def reconcile_refund(self, actor, refund_id, *, source_key):
        return self._reconcile(actor, refund_id, 'refund', source_key)

    def request_refund(self, actor, attempt_id, amount_yen, *, reason, source_key):
        payment = self._row(actor, 'stage5_payment_attempts', attempt_id, 'attempt_id')
        amount = yen(amount_yen)
        if amount <= 0:
            raise RegistryError('positive_refund_required')
        reason = _text(reason, 'reason', 1000)
        def create():
            if self._state('payment', attempt_id)['state'] != 'succeeded':
                raise RegistryError('refund_requires_verified_payment', status=409)
            refunds = self.store.query('SELECT * FROM stage5_payment_refunds WHERE attempt_id=?', (attempt_id,))
            reserved = sum(row['amount_yen'] for row in refunds if self._state('refund', row['refund_id'])['state'] not in ('failed', 'canceled'))
            if reserved + amount > payment['amount_yen']:
                raise RegistryError('refund_exceeds_original_payment', status=409)
            identifier = _id('rf5_')
            self.store.execute('''INSERT INTO stage5_payment_refunds(refund_id,attempt_id,customer_id,amount_yen,currency,
                reason,provider_idempotency_key,actor_user_id,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                (identifier, attempt_id, payment['customer_id'], amount, 'JPY', reason, 'test:refund:' + identifier, actor.user_id, _now()))
            self._event('refund', identifier, 'prepared')
            return self.get_refund(actor, identifier)
        return self._operation(actor, payment['customer_id'], source_key,
            dict(action='physical_refund', attempt_id=attempt_id, amount_yen=amount, reason=reason), create)

    def get_attempt(self, actor, attempt_id):
        row = self._row(actor, 'stage5_payment_attempts', attempt_id, 'attempt_id')
        state = self._state('payment', attempt_id)
        result = {k: row[k] for k in ('attempt_id', 'bill_id', 'customer_id', 'amount_yen', 'currency', 'off_session', 'recorded_at')}
        result.update(state=state['state'], execution_mode='test', real_payment_enabled=False,
                      next_action='test_authentication_required' if state['state'] == 'requires_action' else None,
                      flow='off_session' if row['off_session'] else 'customer_initiated')
        result['refunds'] = [self.get_refund(actor, item['refund_id']) for item in self.store.query('SELECT refund_id FROM stage5_payment_refunds WHERE attempt_id=? ORDER BY recorded_at,refund_id', (attempt_id,))]
        result['refunded_yen'] = sum(item['amount_yen'] for item in result['refunds'] if item['state'] == 'succeeded')
        result['refund_reserved_yen'] = sum(item['amount_yen'] for item in result['refunds'] if item['state'] not in ('succeeded', 'failed', 'canceled'))
        return result

    def get_refund(self, actor, refund_id):
        row = self._row(actor, 'stage5_payment_refunds', refund_id, 'refund_id')
        return {k: row[k] for k in ('refund_id', 'attempt_id', 'customer_id', 'amount_yen', 'currency', 'reason', 'recorded_at')} | {
            'state': self._state('refund', refund_id)['state'], 'execution_mode': 'test', 'real_payment_enabled': False}

    def get_bill(self, actor, bill_id):
        row = self._row(actor, 'stage5_physical_bills', bill_id, 'bill_id')
        inspected = self.inspect_invoice(actor, row['invoice_id'])
        attempts = [self.get_attempt(actor, attempt['attempt_id']) for attempt in self.store.query('SELECT attempt_id FROM stage5_payment_attempts WHERE bill_id=? ORDER BY recorded_at,attempt_id', (bill_id,))]
        paid = sum(attempt['amount_yen'] for attempt in attempts if attempt['state'] == 'succeeded')
        refunded = sum(attempt['refunded_yen'] for attempt in attempts)
        snapshot = json.loads(row['snapshot_json'])
        safe_lines = [{key: line.get(key) for key in ('draft_id', 'physical_item_id', 'fee_kind', 'original_draft_id',
            'amount_net_yen', 'tax_treatment', 'tax_rate_percent', 'occurred_at')} for line in snapshot['lines']]
        return {key: row[key] for key in ('bill_id', 'invoice_id', 'customer_id', 'period', 'timezone_name', 'due_date', 'amount_yen', 'currency', 'recorded_at')} | {
            'lines': safe_lines, 'totals': snapshot['totals'], 'attempts': attempts, 'test_paid_yen': paid,
            'test_refunded_yen': refunded, 'test_net_received_yen': paid - refunded,
            'current_review_reasons': inspected['review_reasons'], 'execution_mode': 'test', 'real_payment_enabled': False,
            'receipt_status': 'test_payment_record' if paid else 'not_paid', 'real_receipt_issued': False,
            'due_timezone': 'Asia/Tokyo', 'due_cutoff_time': None, 'automatic_collection_enabled': False}

    def overview(self, actor, customer_id=None):
        staff = self.store.authorize(actor, customer_id, finance=True)
        if not staff:
            customer_id = actor.user_id
        clause, args = (' WHERE customer_id=?', (customer_id,)) if customer_id is not None else ('', ())
        bills = self.store.query('SELECT bill_id FROM stage5_physical_bills' + clause + ' ORDER BY recorded_at DESC,bill_id', args)
        consents = self.store.query('SELECT * FROM stage5_card_consents' + clause + ' ORDER BY recorded_at DESC,consent_id', args)
        return {'bills': [self.get_bill(actor, row['bill_id']) for row in bills],
                'consents': [self._consent_projection(row) for row in consents],
                'execution_mode': 'test', 'adapter_connected': self.adapter is not None,
                'real_payment_enabled': False, 'automatic_collection_enabled': False}
