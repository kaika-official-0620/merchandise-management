"""Persist the single wallet payment rehearsal on the dedicated staging site."""

from item_registry import RegistryError, _now
from stage3_billing import BillingService, provisional_fee_preview


DEMO_INVOICE_ID = 'in3_dc0ab67a7a07451e82119e2d09092eae'
DEMO_AMOUNT_YEN = 880
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


def member_notice(store, actor):
    if actor.staff or not _saved_demo(store, actor.user_id):
        return None
    month = BillingService(store).get_month(actor, DEMO_INVOICE_ID)
    return describe(store, month, include_refunded_history=True)


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


def transition(store, actor, invoice_id, action):
    if invoice_id != DEMO_INVOICE_ID or action not in ACTIONS:
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
        demo = describe(store, month)
        if not demo:
            raise RegistryError('staging_invoice_payment_unavailable',
                                '対象の検証用下書きを開き直してください。', 409)
        status = demo['status']
        if action == 'send':
            if status == 'unsent':
                store.execute('''INSERT INTO staging_invoice_payment_demo
                    (invoice_id, customer_id, content_hash, amount_yen, sent_by, sent_at)
                    VALUES (?,?,?,?,?,?)''',
                    (invoice_id, customer_id, month['content_hash'], DEMO_AMOUNT_YEN, actor.user_id, _now()))
        elif action == 'pay':
            if status == 'unsent':
                raise RegistryError('staging_invoice_payment_not_sent',
                                    '開花から検証用の請求が届くと、支払いを試せます。', 409)
            if status == 'sent':
                store.execute('''UPDATE staging_invoice_payment_demo SET paid_by=?,paid_at=?
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
