"""Read explicit self-sale reports without importing legacy zeros into finance.

No payment, fee creation, profit recognition or financial ledger writes.
Current ownership, a current sale event and a matching self-report audit are
required. Full Stage3 financial facts take precedence to avoid double counting.
"""
from decimal import Decimal, InvalidOperation
import json


def _yen(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > 9000000000000 or number != number.to_integral_value():
            return None
        return int(number)
    except InvalidOperation:
        return None


def self_sale_reports(store, items, *, excluded_item_ids=()):
    """items is HomeService's already authorized current-owner projection."""
    if store.dialect == 'postgres':
        columns = {r['column_name'] for r in store.query("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='merchandise'")}
        tables = {r['table_name'] for r in store.query('SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()')}
    else:
        columns = {r['name'] for r in store.query('PRAGMA table_info(merchandise)')}
        tables = {r['name'] for r in store.query("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {'id','user_id','scope','custody_location','sale_date','sale_price','is_shipped'}
    if not required <= columns or not {'self_inventory_items','inventory_custody_events'} <= tables:
        return []
    result = []
    for item in items:
        item_id = item['physical_item_id']
        if item_id in excluded_item_ids or item['custodian'] != 'customer' or item['sales_state'] not in ('sold','shipped') or item['held']:
            continue
        owner = item['owner_user_id']
        rows = store.query('''SELECT m.id,m.sale_date,m.sale_price,m.is_shipped FROM merchandise m
            JOIN self_inventory_items s ON s.merchandise_id=m.id AND s.user_id=m.user_id
            JOIN business_item_links l ON l.merchandise_id=m.id
            WHERE l.physical_item_id=? AND m.user_id=? AND m.scope='user' AND m.custody_location='self' ''', (item_id,owner))
        if len(rows) != 1:
            continue
        row = rows[0]
        amount = _yen(row['sale_price'])
        shipped = row['is_shipped']
        if type(shipped) not in (bool, int) or shipped not in (0, 1):
            continue
        if bool(shipped) != (item['sales_state'] == 'shipped'):
            continue
        if not row['sale_date'] or amount is None:
            continue
        events = store.query('''SELECT o.* FROM business_item_observations o
            JOIN business_item_assignments a ON a.assignment_id=? AND a.physical_item_id=o.physical_item_id
            WHERE o.physical_item_id=? AND o.customer_id=? AND o.kind='sold'
            AND o.occurred_at>=a.started_at ORDER BY o.occurred_at DESC,o.recorded_at DESC,o.observation_id DESC''',
            (item['ownership_assignment_id'],item_id,owner))
        if not events or events[0]['evidence_reference'] != 'customer-self-sale-report:' + str(row['id']):
            continue
        audit = store.query('''SELECT id,event_type,details FROM inventory_custody_events WHERE merchandise_id=?
            AND user_id=? AND actor_id=? AND event_type IN ('sale','cancel_sale') ORDER BY id DESC LIMIT 1''', (row['id'],owner,owner))
        if not audit:
            continue
        try:
            details = json.loads(audit[0]['details'])
            after = details['after']
            if audit[0]['event_type'] != 'sale' or details.get('registry_sale_observation_id') != events[0]['observation_id']:
                continue
            if type(after['is_shipped']) is not bool or str(after['sale_date']) != str(row['sale_date']) or _yen(after['sale_price']) != amount or after['is_shipped'] != bool(shipped):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        result.append(dict(physical_item_id=item_id,title=item['title'],sale_gross_yen=amount,
            sold_on=str(row['sale_date']),is_shipped=bool(row['is_shipped']),source_event_id=audit[0]['id'],
            source='customer_self_report',provider_verified=False,profit_yen=None,
            path='/view/' + str(row['id']),financial_review_path='/finance/items/' + item_id))
    return result
