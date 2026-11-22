"""Persist the single wallet payment rehearsal on the dedicated staging site."""

from item_registry import RegistryError, _now
from stage3_billing import BillingService, provisional_fee_preview


DEMO_INVOICE_ID = 'in3_dc0ab67a7a07451e82119e2d09092eae'
DEMO_AMOUNT_YEN = 880


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
    )''',)


def enabled(app):
    return (app.extensions.get('kaika_staging') is True
            and isinstance(app.extensions.get('kaika_staging_review'), dict))


def _saved_demo(store, customer_id):
    rows = store.query('SELECT * FROM staging_invoice_payment_demo WHERE invoice_id=? AND customer_id=?',
                       (DEMO_INVOICE_ID, customer_id))
    return rows[0] if rows else None


def describe(store, month, fee_preview):
    if (month['invoice_id'] != DEMO_INVOICE_ID or month['is_current_snapshot'] is not True
            or not fee_preview or fee_preview['gross_total_yen'] != DEMO_AMOUNT_YEN):
        return None
    record = _saved_demo(store, month['customer_id'])
    if record and (record['content_hash'] != month['content_hash'] or record['amount_yen'] != DEMO_AMOUNT_YEN):
        return None
    paid_at = record['paid_at'] if record else None
    return {
        'invoice_id': DEMO_INVOICE_ID, 'amount_yen': DEMO_AMOUNT_YEN,
        'status': 'paid' if paid_at else ('sent' if record else 'unsent'),
        'sent_at': record['sent_at'] if record else None, 'paid_at': paid_at,
    }


def member_notice(store, actor):
    if actor.staff or not _saved_demo(store, actor.user_id):
        return None
    month = BillingService(store).get_month(actor, DEMO_INVOICE_ID)
    return describe(store, month, provisional_fee_preview(month))


def transition(store, actor, invoice_id, action):
    if invoice_id != DEMO_INVOICE_ID or action not in ('send', 'pay'):
        raise RegistryError('staging_invoice_payment_not_found', status=404)
    rows = store.query('SELECT customer_id FROM stage3_invoice_revisions WHERE invoice_id=?', (invoice_id,))
    if not rows:
        raise RegistryError('staging_invoice_payment_not_found', status=404)
    customer_id = rows[0]['customer_id']
    staff = store.authorize(actor, customer_id, write=action == 'send')
    if action == 'pay' and (staff or actor.user_id != customer_id):
        raise RegistryError('staging_invoice_payment_owner_required',
                            'この検証用請求の会員ご本人が支払いを試せます。', 403)

    with store.atomic():
        store.customer_lock(customer_id)
        month = BillingService(store).get_month(actor, invoice_id)
        demo = describe(store, month, provisional_fee_preview(month))
        if not demo:
            raise RegistryError('staging_invoice_payment_unavailable',
                                '対象の検証用下書きを開き直してください。', 409)
        if action == 'send' and demo['status'] == 'unsent':
            store.execute('''INSERT INTO staging_invoice_payment_demo
                (invoice_id, customer_id, content_hash, amount_yen, sent_by, sent_at)
                VALUES (?,?,?,?,?,?)''',
                (invoice_id, customer_id, month['content_hash'], DEMO_AMOUNT_YEN, actor.user_id, _now()))
        elif action == 'pay':
            if demo['status'] == 'unsent':
                raise RegistryError('staging_invoice_payment_not_sent',
                                    '開花から検証用の請求が届くと、支払いを試せます。', 409)
            if demo['status'] == 'sent':
                store.execute('''UPDATE staging_invoice_payment_demo SET paid_by=?,paid_at=?
                    WHERE invoice_id=? AND customer_id=? AND paid_at IS NULL''',
                    (actor.user_id, _now(), invoice_id, customer_id))
