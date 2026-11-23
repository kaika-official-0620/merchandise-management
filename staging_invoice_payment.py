"""Persist two separate wallet payment rehearsals on the dedicated staging site."""

import csv
import io
from contextlib import contextmanager

from item_registry import RegistryError, _now
from stage3_billing import BillingService, invoice_totals, provisional_fee_preview
from stage3_common import yen


DEMO_INVOICE_ID = 'in3_dc0ab67a7a07451e82119e2d09092eae'
DEMO_AMOUNT_YEN = 880
RESALE_INVOICE_ID = 'in3_13d28ed903074e4886b1e9ff7666fa43'
RESALE_SALES_FEE_NET_YEN = 300
RESALE_AMOUNT_YEN = 330
RESALE_PLANNED_SETTLEMENT_YEN = 7200
RESALE_COST_FIELDS = ('purchase_cost_yen', 'shipping_cost_yen', 'channel_fee_yen')
ACTIONS = ('send', 'pay', 'return', 'refund')


def migration_statements(dialect='postgres'):
    return ('''CREATE TABLE IF NOT EXISTS staging_invoice_payment_demo (
        invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES stage3_invoice_revisions(invoice_id),
        customer_id INTEGER NOT NULL REFERENCES users(id),
        content_hash TEXT NOT NULL,
        amount_yen INTEGER NOT NULL CHECK (amount_yen = 880),
        sent_by INTEGER NOT NULL REFERENCES users(id),
        sent_at TEXT NOT NULL,
        paid_by INTEGER REFERENCES users(id),
        paid_at TEXT,
        CHECK (sent_by <> customer_id),
        CHECK ((paid_by IS NULL) = (paid_at IS NULL)),
        CHECK (paid_by IS NULL OR paid_by = customer_id)
    )''', '''CREATE TABLE IF NOT EXISTS staging_invoice_payment_return_demo (
        invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES staging_invoice_payment_demo(invoice_id),
        returned_by INTEGER NOT NULL REFERENCES users(id),
        returned_at TEXT NOT NULL,
        refunded_by INTEGER REFERENCES users(id),
        refunded_at TEXT,
        CHECK ((refunded_by IS NULL) = (refunded_at IS NULL))
    )''', '''CREATE TABLE IF NOT EXISTS staging_wallet_resale_payment_demo (
        invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES stage3_invoice_revisions(invoice_id),
        customer_id INTEGER NOT NULL REFERENCES users(id),
        content_hash TEXT NOT NULL,
        amount_yen INTEGER NOT NULL CHECK (amount_yen = 330),
        sent_by INTEGER NOT NULL REFERENCES users(id),
        sent_at TEXT NOT NULL,
        paid_by INTEGER REFERENCES users(id),
        paid_at TEXT,
        CHECK (sent_by <> customer_id),
        CHECK ((paid_by IS NULL) = (paid_at IS NULL)),
        CHECK (paid_by IS NULL OR paid_by = customer_id)
    )''', '''CREATE TABLE IF NOT EXISTS staging_wallet_resale_test_costs (
        invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES staging_wallet_resale_payment_demo(invoice_id),
        sale_event_id TEXT NOT NULL REFERENCES business_item_observations(observation_id),
        purchase_cost_yen BIGINT NOT NULL CHECK (purchase_cost_yen BETWEEN 0 AND 9000000000000),
        shipping_cost_yen BIGINT NOT NULL CHECK (shipping_cost_yen BETWEEN 0 AND 9000000000000),
        channel_fee_yen BIGINT NOT NULL CHECK (channel_fee_yen BETWEEN 0 AND 9000000000000),
        recorded_by INTEGER NOT NULL REFERENCES users(id),
        recorded_at TEXT NOT NULL
    )''', '''CREATE TABLE IF NOT EXISTS staging_wallet_resale_receipt_demo (
        invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES staging_wallet_resale_payment_demo(invoice_id),
        sale_event_id TEXT NOT NULL REFERENCES business_item_observations(observation_id),
        sale_gross_yen BIGINT NOT NULL CHECK (sale_gross_yen = 8500),
        planned_settlement_yen BIGINT NOT NULL CHECK (planned_settlement_yen = 7200),
        confirmed_by INTEGER NOT NULL REFERENCES users(id),
        confirmed_at TEXT NOT NULL
    )''', '''CREATE TABLE IF NOT EXISTS staging_wallet_resale_settlement_demo (
        invoice_id TEXT PRIMARY KEY NOT NULL REFERENCES staging_wallet_resale_receipt_demo(invoice_id),
        sale_event_id TEXT NOT NULL REFERENCES business_item_observations(observation_id),
        amount_yen BIGINT NOT NULL CHECK (amount_yen = 7200),
        settled_by INTEGER NOT NULL REFERENCES users(id),
        settled_at TEXT NOT NULL
    )''')


def enabled(app):
    return (app.extensions.get('kaika_staging') is True
            and isinstance(app.extensions.get('kaika_staging_review'), dict))


def _saved_demo(store, customer_id):
    rows = store.query('''SELECT payment.*, reversal.returned_at, reversal.refunded_at
        FROM staging_invoice_payment_demo AS payment
        LEFT JOIN staging_invoice_payment_return_demo AS reversal ON reversal.invoice_id=payment.invoice_id
        WHERE payment.invoice_id=? AND payment.customer_id=?''', (DEMO_INVOICE_ID, customer_id))
    return rows[0] if rows else None


def describe(store, month, *, include_refunded_history=False):
    if month['invoice_id'] != DEMO_INVOICE_ID:
        return None
    historical = month['is_current_snapshot'] is not True
    if historical and not include_refunded_history:
        return None
    record = _saved_demo(store, month['customer_id'])
    if record and (record['content_hash'] != month['content_hash'] or record['amount_yen'] != DEMO_AMOUNT_YEN):
        return None
    # Operational changes invalidate the draft for new actions but cannot erase a refund.
    if historical and not (record and record['refunded_at']):
        return None
    fee_preview = provisional_fee_preview(month, include_historical=historical)
    if not fee_preview or fee_preview['gross_total_yen'] != DEMO_AMOUNT_YEN:
        return None
    status = 'unsent'
    if record:
        status = 'sent'
        for timestamp, phase in (('paid_at', 'paid'), ('returned_at', 'returned'), ('refunded_at', 'refunded')):
            if record[timestamp]:
                status = phase
    return {
        'invoice_id': DEMO_INVOICE_ID, 'amount_yen': DEMO_AMOUNT_YEN,
        'status': status, 'historical': historical, 'fee_preview': fee_preview,
        'sent_at': record['sent_at'] if record else None,
        'paid_at': record['paid_at'] if record else None,
        'returned_at': record['returned_at'] if record else None,
        'refunded_at': record['refunded_at'] if record else None,
    }


def member_notices(store, actor):
    if actor.staff:
        return []
    billing = BillingService(store)
    notices = []
    if _saved_resale(store, actor.user_id):
        resale = describe_resale(store, billing.get_month(actor, RESALE_INVOICE_ID), actor, include_paid_history=True)
        if resale:
            notices.append(resale)
    if _saved_demo(store, actor.user_id):
        original = describe(store, billing.get_month(actor, DEMO_INVOICE_ID), include_refunded_history=True)
        if original:
            notices.append(original)
    return notices


def refunded_history(store, actor, customer_id, *, period=None, item_id=None):
    record = _saved_demo(store, customer_id)
    if not record or not record['refunded_at']:
        return None
    original = BillingService(store).get_month(actor, DEMO_INVOICE_ID)
    demo = describe(store, original, include_refunded_history=True)
    if not demo or demo['status'] != 'refunded':
        return None
    if period is not None and (original['period'] != period or original['timezone_name'] != 'Asia/Tokyo'):
        return None
    if item_id is not None and demo['fee_preview']['physical_item_id'] != item_id:
        return None
    lines = original['snapshot']['lines']
    return {
        'invoice_id': DEMO_INVOICE_ID,
        'draft_ids': sorted(line['draft_id'] for line in lines),
        'shipment_event_ids': sorted({line['source_event_id'] for line in lines if line['source_event_id']}),
    }


def _saved_resale(store, customer_id):
    rows = store.query('''SELECT * FROM staging_wallet_resale_payment_demo WHERE invoice_id=? AND customer_id=?''',
                       (RESALE_INVOICE_ID, customer_id))
    return rows[0] if rows else None


def describe_resale(store, month, actor, *, include_paid_history=False):
    if month['invoice_id'] != RESALE_INVOICE_ID:
        return None
    historical = month['is_current_snapshot'] is not True
    if historical and not include_paid_history:
        return None
    customer_id = month['customer_id']
    record = _saved_resale(store, customer_id)
    if record and (record['content_hash'] != month['content_hash'] or record['amount_yen'] != RESALE_AMOUNT_YEN):
        return None
    if historical and not (record and record['paid_at']):
        return None

    snapshot = month['snapshot']
    lines = snapshot['lines']
    if (snapshot.get('chargeable') is not False or snapshot.get('uncollected_shipments') or len(lines) != 2
            or {line['fee_kind'] for line in lines} != {'work_bundle', 'sales_fee'}):
        return None
    item_id, shipment_id = lines[0]['physical_item_id'], lines[0]['source_event_id']
    archived = refunded_history(store, actor, customer_id, period=month['period'], item_id=item_id)
    if (not item_id or not shipment_id or not archived or snapshot.get('staging_refund_history') != archived
            or shipment_id in archived['shipment_event_ids']):
        return None
    for line in lines:
        evidence = line.get('evidence_reference') or ''
        sale_facts = line['snapshot'].get('sale_fact_snapshot') or {}
        if (line['customer_id'] != customer_id or line['physical_item_id'] != item_id
                or line['source_event_id'] != shipment_id or line['amount_net_yen'] is not None
                or line['original_draft_id'] is not None or evidence.split(maxsplit=1)[:1] != ['DEMO-WALLET-S02']
                or sale_facts.get('sale_gross_yen') != 8500):
            return None

    # The amount approved for this fixture is only the new selling fee.
    sale_fee = {'fee_kind': 'sales_fee', 'amount_net_yen': RESALE_SALES_FEE_NET_YEN}
    tax_rate = 10
    totals = invoice_totals([dict(sale_fee, tax_treatment='exclusive', tax_rate_percent=tax_rate)], 'floor')
    if totals['reference_gross_total_yen'] != RESALE_AMOUNT_YEN:
        return None
    preview = {'title': lines[0]['snapshot']['title'], 'physical_item_id': item_id,
               'lines': [sale_fee], 'tax_rate_percent': tax_rate,
               'net_total_yen': totals['net_total_yen'], 'tax_total_yen': totals['tax_total_yen'],
               'gross_total_yen': totals['reference_gross_total_yen']}
    paid_at = record['paid_at'] if record else None
    return {'invoice_id': RESALE_INVOICE_ID, 'amount_yen': RESALE_AMOUNT_YEN, 'resale': True,
            'status': 'paid' if paid_at else ('sent' if record else 'unsent'),
            'historical': historical, 'fee_preview': preview,
            'sent_at': record['sent_at'] if record else None, 'paid_at': paid_at,
            'returned_at': None, 'refunded_at': None}


def _resale_costs(store):
    rows = store.query('SELECT * FROM staging_wallet_resale_test_costs WHERE invoice_id=?', (RESALE_INVOICE_ID,))
    return rows[0] if rows else None


def _resale_receipt(store, sale_id, sale_gross, costs):
    if not costs:
        return None
    planned = sale_gross - costs['channel_fee_yen'] - costs['shipping_cost_yen']
    if planned != RESALE_PLANNED_SETTLEMENT_YEN:
        return None
    rows = store.query('''SELECT receipt.sale_event_id, receipt.sale_gross_yen, receipt.planned_settlement_yen,
                               receipt.confirmed_at, settlement.sale_event_id AS settled_sale_event_id,
                               settlement.amount_yen AS settled_amount_yen, settlement.settled_at
        FROM staging_wallet_resale_receipt_demo AS receipt
        LEFT JOIN staging_wallet_resale_settlement_demo AS settlement ON settlement.invoice_id=receipt.invoice_id
        WHERE receipt.invoice_id=?''', (RESALE_INVOICE_ID,))
    saved = rows[0] if rows else None
    if saved and (saved['sale_event_id'], saved['sale_gross_yen'], saved['planned_settlement_yen']) != (sale_id, sale_gross, planned):
        return None
    settled_at = saved['settled_at'] if saved else None
    if settled_at and (saved['settled_sale_event_id'], saved['settled_amount_yen']) != (sale_id, planned):
        return None
    status = 'settled' if settled_at else ('confirmed' if saved else 'pending')
    return {'status': status, 'planned_settlement_yen': planned,
            'confirmed_at': saved['confirmed_at'] if saved else None, 'settled_at': settled_at}


def resale_reconciliation(store, month, demo):
    if not demo or not demo.get('resale') or demo['status'] != 'paid':
        return None
    lines = month['snapshot']['lines']
    sale_ids = {line['snapshot'].get('sale_event_id') for line in lines}
    if len(sale_ids) != 1 or None in sale_ids:
        return None
    sale_id = next(iter(sale_ids))
    sale_facts = [line['snapshot']['sale_fact_snapshot'] for line in lines]
    if any(facts.get('sale_event_id') != sale_id for facts in sale_facts):
        return None

    sale_gross = sale_facts[0]['sale_gross_yen']
    paid_fee = demo['amount_yen']
    difference = sale_gross - paid_fee
    saved = _resale_costs(store)
    costs = None
    if saved and saved['sale_event_id'] == sale_id:
        costs = {field: saved[field] for field in RESALE_COST_FIELDS}
        costs.update(remaining_yen=difference - sum(costs.values()), recorded_at=saved['recorded_at'])
    return {'invoice_id': month['invoice_id'], 'item_id': demo['fee_preview']['physical_item_id'],
            'title': demo['fee_preview']['title'], 'period': month['period'], 'sale_event_id': sale_id,
            'sale_gross_yen': sale_gross, 'paid_fee_yen': paid_fee,
            'reference_difference_yen': difference, 'paid_at': demo['paid_at'], 'costs': costs,
            'sale_receipt': _resale_receipt(store, sale_id, sale_gross, costs)}


def current_resale_reconciliation(store, actor, customer_id, *, item_id=None, profit=None):
    record = _saved_resale(store, customer_id)
    if not record or not record['paid_at']:
        return None
    month = BillingService(store).get_month(actor, RESALE_INVOICE_ID)
    demo = describe_resale(store, month, actor, include_paid_history=True)
    result = resale_reconciliation(store, month, demo)
    if not result or (item_id is not None and item_id != result['item_id']):
        return None
    return result if _matches_current_resale(store, actor, customer_id, result, profit) else None


def _matches_current_resale(store, actor, customer_id, result, profit=None):
    try:
        current = store.item(actor, result['item_id'])
    except RegistryError as exc:
        if exc.status in (403, 404):
            return False
        raise
    if current['owner_user_id'] != customer_id or current['sales_state'] != 'shipped':
        return False
    if profit is None:
        from stage3_profit import ProfitService
        profit = ProfitService(store).profit(actor, result['item_id'])
    facts = profit['facts']
    return (profit['recognition'] != 'return_hold' and facts['sale_event_id'] == result['sale_event_id']
            and facts['sale_gross_yen'] == result['sale_gross_yen'])


@contextmanager
def _writable_resale_reconciliation(store, actor, unavailable_code):
    billing = BillingService(store)
    draft = billing.get_month(actor, RESALE_INVOICE_ID)
    customer_id = draft['customer_id']
    store.authorize(actor, customer_id, write=True)
    with store.atomic():
        store.customer_lock(customer_id)
        month = billing.get_month(actor, RESALE_INVOICE_ID)
        result = resale_reconciliation(store, month, describe_resale(store, month, actor))
        if result:
            # Use the registry lock shared by sale / return writers.
            store.registry._profile(actor, result['item_id'], lock=True)
        if not result or not _matches_current_resale(store, actor, customer_id, result):
            raise RegistryError(unavailable_code,
                                '支払い済みの再販売明細を開き直して確認してください。', 409)
        yield result


def record_resale_costs(store, actor, *, purchase_cost_yen, shipping_cost_yen, channel_fee_yen):
    amounts = (yen(purchase_cost_yen), yen(shipping_cost_yen), yen(channel_fee_yen))
    with _writable_resale_reconciliation(store, actor, 'staging_resale_costs_unavailable') as result:
        saved = _resale_costs(store)
        if saved:
            if saved['sale_event_id'] != result['sale_event_id'] or tuple(saved[field] for field in RESALE_COST_FIELDS) != amounts:
                raise RegistryError('staging_resale_costs_conflict', 'この検証明細には費用がすでに記録されています。', 409)
            return False
        store.execute('''INSERT INTO staging_wallet_resale_test_costs
            (invoice_id, sale_event_id, purchase_cost_yen, shipping_cost_yen, channel_fee_yen, recorded_by, recorded_at)
            VALUES (?,?,?,?,?,?,?)''', (RESALE_INVOICE_ID, result['sale_event_id'], *amounts, actor.user_id, _now()))
        return True


def confirm_resale_receipt(store, actor):
    with _writable_resale_reconciliation(store, actor, 'staging_resale_receipt_unavailable') as result:
        receipt = result['sale_receipt']
        if not receipt:
            raise RegistryError('staging_resale_receipt_unavailable',
                                '再販売分の費用とテスト精算予定額を確認してください。', 409)
        if receipt['status'] != 'pending':
            return False
        store.execute('''INSERT INTO staging_wallet_resale_receipt_demo
            (invoice_id, sale_event_id, sale_gross_yen, planned_settlement_yen, confirmed_by, confirmed_at)
            VALUES (?,?,?,?,?,?)''', (RESALE_INVOICE_ID, result['sale_event_id'], result['sale_gross_yen'],
                                      receipt['planned_settlement_yen'], actor.user_id, _now()))
        return True


def record_resale_settlement(store, actor):
    """Record the approved mock amount without touching funds, payment or profit records."""
    with _writable_resale_reconciliation(store, actor, 'staging_resale_settlement_unavailable') as result:
        receipt = result['sale_receipt']
        if not receipt or receipt['status'] == 'pending':
            raise RegistryError('staging_resale_settlement_unavailable',
                                '売上のテスト入金を確認してから、テスト精算を記録してください。', 409)
        if receipt['status'] == 'settled':
            return False
        store.execute('''INSERT INTO staging_wallet_resale_settlement_demo
            (invoice_id, sale_event_id, amount_yen, settled_by, settled_at)
            VALUES (?,?,?,?,?)''', (RESALE_INVOICE_ID, result['sale_event_id'],
                                   receipt['planned_settlement_yen'], actor.user_id, _now()))
        return True


def append_resale_csv(content, reconciliation):
    if not reconciliation:
        return content
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    section = '検証集計（再販売分）'
    writer.writerows((
        (section, '売上（税込・円）', reconciliation['sale_gross_yen']),
        (section, 'テスト販売料（税込・円）', reconciliation['paid_fee_yen']),
        (section, '販売料だけを差し引いた参考額（円）', reconciliation['reference_difference_yen']),
    ))
    costs = reconciliation['costs']
    if costs:
        writer.writerows((
            (section, '仕入原価（円）', costs['purchase_cost_yen']),
            (section, '送料（円）', costs['shipping_cost_yen']),
            (section, '販路手数料（円）', costs['channel_fee_yen']),
            (section, '最終的な手残り（検証用・円）', costs['remaining_yen']),
        ))
    else:
        writer.writerow((section, '最終的な手残り', '未確認'))
    return content + output.getvalue()


def supports_action(invoice_id, action):
    return ((invoice_id == DEMO_INVOICE_ID and action in ACTIONS)
            or (invoice_id == RESALE_INVOICE_ID and action in ('send', 'pay')))


def transition(store, actor, invoice_id, action):
    if not supports_action(invoice_id, action):
        raise RegistryError('staging_invoice_payment_not_found', status=404)
    rows = store.query('SELECT customer_id FROM stage3_invoice_revisions WHERE invoice_id=?', (invoice_id,))
    if not rows:
        raise RegistryError('staging_invoice_payment_not_found', status=404)
    customer_id = rows[0]['customer_id']
    staff = store.authorize(actor, customer_id, write=action != 'pay')
    if action == 'pay' and (staff or actor.user_id != customer_id):
        raise RegistryError('staging_invoice_payment_owner_required',
                            'この検証用請求の会員ご本人が支払いを試せます。', 403)

    with store.atomic():
        store.customer_lock(customer_id)
        month = BillingService(store).get_month(actor, invoice_id)
        resale = invoice_id == RESALE_INVOICE_ID
        demo = describe_resale(store, month, actor) if resale else describe(store, month)
        if not demo:
            raise RegistryError('staging_invoice_payment_unavailable',
                                '対象の検証用下書きを開き直してください。', 409)
        status = demo['status']
        # Both SQL identifiers are selected from this closed set, never from form data.
        table = 'staging_wallet_resale_payment_demo' if resale else 'staging_invoice_payment_demo'
        if action == 'send':
            if status == 'unsent':
                store.execute(f'''INSERT INTO {table}
                    (invoice_id, customer_id, content_hash, amount_yen, sent_by, sent_at)
                    VALUES (?,?,?,?,?,?)''',
                    (invoice_id, customer_id, month['content_hash'], demo['amount_yen'], actor.user_id, _now()))
        elif action == 'pay':
            if status == 'unsent':
                raise RegistryError('staging_invoice_payment_not_sent',
                                    '開花から検証用の請求が届くと、支払いを試せます。', 409)
            if status == 'sent':
                store.execute(f'''UPDATE {table} SET paid_by=?,paid_at=?
                    WHERE invoice_id=? AND customer_id=? AND paid_at IS NULL''',
                    (actor.user_id, _now(), invoice_id, customer_id))
        elif action == 'return':
            if status in ('unsent', 'sent'):
                raise RegistryError('staging_invoice_payment_not_paid',
                                    '検証用の支払いを記録してから、返品を受け付けてください。', 409)
            if status == 'paid':
                store.execute('''INSERT INTO staging_invoice_payment_return_demo
                    (invoice_id, returned_by, returned_at) VALUES (?,?,?)''',
                    (invoice_id, actor.user_id, _now()))
        elif status == 'returned':
            store.execute('''UPDATE staging_invoice_payment_return_demo SET refunded_by=?,refunded_at=?
                WHERE invoice_id=? AND refunded_at IS NULL''', (actor.user_id, _now(), invoice_id))
        elif status != 'refunded':
            raise RegistryError('staging_invoice_payment_not_returned',
                                '検証用の返品を受け付けてから、返金を試してください。', 409)
