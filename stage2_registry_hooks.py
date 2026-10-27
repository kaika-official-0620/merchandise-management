"""Atomic adapters from existing inventory handlers to the opt-in registry.

The caller owns the connection and commits. These functions never infer work
minutes, create a fee, replace a historical deadline or duplicate a physical good.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from flask import abort
from flask_login import current_user

from item_registry import Actor, Registry, RegistryError


def _context(conn, runtime):
    return Registry(conn, 'postgres' if runtime.DATABASE_URL else 'sqlite'), Actor(int(current_user.id), bool(current_user.is_admin()))


def _when(runtime, value=None):
    value = value if value is not None else getattr(runtime, 'get_jst_now', lambda: datetime.now(timezone.utc))()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone(timedelta(hours=9)))
    return value


def _key(token, prefix):
    return prefix + ':' + hashlib.sha256(str(token['nonce']).encode()).hexdigest()


def _failure(error):
    messages = {
        'staff_work_required': '保管場所の記録が一致していません。開花へ確認を依頼してください。',
        'invalid_workflow_transition': '登録完了の前に撮影の実施・写真の再利用条件を確認してください。',
        'item_on_hold': 'この個品は確認待ちです。確認待ちの理由を解決してから再開してください。',
        'invalid_state_transition': '個品の状態と今回の操作が一致していません。記録を確認してください。',
        'unshipped_sale_required': '発送済みの商品は売却取消ではなく返品として記録してください。',
        'registry_migration_required': '段階2の検証用移行が未準備です。確認後に再開してください。',
    }
    status = 409 if error.code in ('invalid_workflow_transition', 'invalid_state_transition') else error.status
    abort(status if status in (400,403,404,409) else 409,
          description=messages.get(error.code, '個品記録を更新できませんでした。所有者・保管場所・操作順を確認してください。'))


def _linked(registry, actor, merchandise_id):
    rows = registry.query('SELECT physical_item_id FROM business_item_links WHERE merchandise_id=?', (merchandise_id,))
    return registry.detail(actor, rows[0]['physical_item_id']) if rows else None


def self_saved(conn, runtime, merchandise_id, values, token, is_new=False):
    registry, actor = _context(conn, runtime)
    when, key = _when(runtime), _key(token, 'self-record')
    try:
        if not is_new:
            # Legacy records remain editable while a staff member reconciles
            # identity. The existing custody audit records the actual edit.
            linked = _linked(registry, actor, merchandise_id)
            if linked and linked['custodian'] != 'unknown' and linked['physical_state'] != 'unknown':
                if linked['title'] != values['product_name']:
                    return registry.update_title(actor, linked['physical_item_id'], values['product_name'],
                        occurred_at=when, evidence_reference='customer-self-edit:' + str(merchandise_id), source_key=key + ':title')
            return linked
        item = registry.create_item(actor, values['product_name'], custodian='self',
            occurred_at=when, evidence_reference='customer-self-registration:' + str(merchandise_id), source_key=key)
        return registry.attach_legacy(actor, merchandise_id, physical_item_id=item['physical_item_id'],
            occurred_at=when, evidence_reference='same-transaction-self-registration:' + str(merchandise_id), source_key=key + ':link')
    except RegistryError as error:
        _failure(error)


def sale_recorded(conn, runtime, item, updates, action, token, reason='', returned_confirmed=False):
    registry, actor = _context(conn, runtime)
    when, key = _when(runtime), _key(token, 'self-sale')
    try:
        linked = _linked(registry, actor, item['id'])
        if not linked or linked['custodian'] == 'unknown' or linked['physical_state'] == 'unknown':
            # Do not block existing basic records over incomplete migration.
            # Existing sale/custody audit remains, no fabricated registry event.
            return None
        item_id = linked['physical_item_id']
        reference = 'customer-self-sale-report:' + str(item['id'])
        if action == 'listing':
            # A personal listing toggle is not evidence of completed Kaika
            # photography/registration or external channel authorization.
            return linked
        if action == 'cancel_sale':
            if linked['sales_state'] == 'shipped':
                if not returned_confirmed:
                    abort(409, description='返品の現物を受領したことを確認してから記録してください。')
                return registry.record_event(actor, item_id, 'returned', when, key + ':returned', reference,
                                             details={'return_reason': reason})
            return registry.record_event(actor, item_id, 'sale_cancelled', when, key + ':cancelled', reference,
                                         details={'note': reason})
        if action == 'sale':
            if linked['sales_state'] not in ('sold', 'shipped'):
                # This is the time the user records the sale state. The exact
                # sale date, without a guessed clock time, remains in merchandise.
                linked = registry.record_event(actor, item_id, 'sold', when, key + ':sold', reference,
                    details={'note': '自己販売の記録。入力された売却日: ' + str(updates.get('sale_date') or '')})
            if updates.get('is_shipped') and linked['sales_state'] != 'shipped':
                linked = registry.record_event(actor, item_id, 'shipped', when, key + ':shipped', reference)
            if not updates.get('is_shipped') and linked['sales_state'] == 'shipped':
                abort(409, description='発送済みの記録は取り消せません。返品の受領を記録してください。')
            # Bind the custody audit to this sale cycle, including corrections.
            # A previous sale's audit must not become evidence after a return.
            sold = registry.query('''SELECT observation_id FROM business_item_observations
                WHERE physical_item_id=? AND customer_id=? AND kind='sold'
                ORDER BY occurred_at DESC,recorded_at DESC,observation_id DESC LIMIT 1''', (item_id, actor.user_id))
            return {**linked, 'self_sale_observation_id': sold[0]['observation_id'] if sold else None}
    except RegistryError as error:
        _failure(error)


def intake_changed(conn, runtime, item, intake, action, token):
    if action not in ('receive', 'complete'):
        return None
    registry, actor = _context(conn, runtime)
    when, key = _when(runtime), _key(token, 'intake-' + action + ':' + str(item['id']))
    reference = 'kaika-intake:' + str(intake['id'])
    try:
        linked = registry.attach_legacy(actor, item['id'], occurred_at=when,
            evidence_reference=reference, source_key=key + ':link')
        kind = 'received' if action == 'receive' else 'registration_completed'
        return registry.record_event(actor, linked['physical_item_id'], kind, when, key, reference)
    except RegistryError as error:
        _failure(error)


def received_item_created(conn, runtime, merchandise_id, values, intake, token):
    registry, actor = _context(conn, runtime)
    when, key = _when(runtime, intake['received_at']), _key(token, 'kaika-register')
    reference = 'kaika-intake:' + str(intake['id'])
    try:
        item = registry.create_item(actor, values['product_name'], owner_user_id=int(intake['user_id']),
            custodian='unknown', occurred_at=when, evidence_reference=reference, source_key=key)
        registry.attach_legacy(actor, merchandise_id, physical_item_id=item['physical_item_id'],
            occurred_at=when, evidence_reference=reference, source_key=key + ':link')
        # Receipt is explicit in the intake; uploaded files alone are not proof
        # of when photography happened and never invent a photography work log.
        return registry.record_event(actor, item['physical_item_id'], 'received', when, key + ':received', reference)
    except RegistryError as error:
        _failure(error)


def record_return_received(conn, runtime, item_id, *, occurred_at, source_key, evidence_reference, details=None):
    """Confirm delivery to the owner and restore self-management atomically.

    A dispatch alone never changes legacy custody to self. All linked rows must
    still belong to the current customer and be held by Kaika. Monetary columns,
    original intake links and historical work remain unchanged.
    """
    registry, actor = _context(conn, runtime)
    when = _when(runtime, occurred_at)
    adapter_key = source_key + ':legacy-return'
    payload = dict(action='return_received_legacy', physical_item_id=item_id,
        occurred_at=when.isoformat(), evidence=evidence_reference, details=details or {})
    try:
        registry.actor(actor, staff_required=True)
        with registry.atomic():
            digest, prior = registry._request(actor, adapter_key, payload)
            if prior:
                return registry.record_event(actor, item_id, 'return_received', when, source_key,
                    evidence_reference, details=details)
            # Existing inventory handlers lock legacy rows before touching the
            # registry. Keep that lock order for PostgreSQL as well.
            suffix = ' FOR UPDATE OF m' if registry.dialect == 'postgres' else ''
            rows = registry.query('''SELECT m.* FROM merchandise m JOIN business_item_links l
                ON l.merchandise_id=m.id WHERE l.physical_item_id=? ORDER BY m.id''' + suffix, (item_id,))
            if not rows:
                raise RegistryError('return_link_required', '既存の商品記録との対応を確認してください。', 409)
            profile, roles, _ = registry._profile(actor, item_id, lock=True)
            owner_id = registry._owner(roles)
            if profile['physical_state'] != 'return_in_transit' or roles.get('custodian', {}).get('kind') != 'kaika':
                raise RegistryError('return_state_changed', '返送中の商品と保管者の記録を確認してください。', 409)
            plans = []
            for row in rows:
                if row.get('user_id') != owner_id or row.get('scope') != 'user' or row.get('custody_location') != 'kaika':
                    raise RegistryError('return_legacy_mismatch', '所有者・顧客在庫区分・開花保管の記録が一致しません。', 409)
                version = row.get('custody_version')
                if type(version) is not int or version < 1:
                    raise RegistryError('return_legacy_version_required', '既存在庫の更新状態を確認してください。', 409)
                self_rows = registry.query('SELECT * FROM self_inventory_items WHERE merchandise_id=?', (row['id'],))
                if self_rows and self_rows[0]['user_id'] != owner_id:
                    raise RegistryError('return_self_owner_mismatch', '自己管理記録の所有者が一致しません。', 409)
                intakes = registry.query('''SELECT i.id,i.user_id,i.status FROM inventory_intakes i
                    JOIN inventory_intake_items ii ON ii.intake_id=i.id
                    WHERE ii.merchandise_id=? AND i.status IN ('requested','approved','shipped','received')''', (row['id'],))
                if any(intake['user_id'] != owner_id or intake['status'] != 'received' for intake in intakes):
                    raise RegistryError('return_intake_mismatch', '別の受付が進行中です。受付と返送の対応を確認してください。', 409)
                plans.append((row, self_rows, intakes))
            result = registry.record_event(actor, item_id, 'return_received', when, source_key, evidence_reference, details=details)
            for row, self_rows, intakes in plans:
                cursor = registry.execute('''UPDATE merchandise SET custody_location='self',custody_version=custody_version+1,
                    updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=? AND scope='user'
                    AND custody_location='kaika' AND custody_version=?''', (actor.user_id, row['id'], owner_id, row['custody_version']))
                if cursor.rowcount != 1:
                    raise RegistryError('return_legacy_changed', '確認中に在庫が変更されました。再確認してください。', 409)
                if self_rows:
                    registry.execute('''UPDATE self_inventory_items SET version=version+1,updated_at=CURRENT_TIMESTAMP
                        WHERE merchandise_id=? AND user_id=?''', (row['id'], owner_id))
                else:
                    submission = hashlib.sha256(('return-receipt:' + item_id + ':' + str(row['id'])).encode()).hexdigest()
                    registry.execute('INSERT INTO self_inventory_items(merchandise_id,user_id,submission_hash) VALUES(?,?,?)',
                        (row['id'], owner_id, submission))
                audit = json.dumps({'physical_item_id': item_id, 'source_key': source_key,
                    'before': {'custody_location': row['custody_location'], 'custody_version': row['custody_version']},
                    'after': {'custody_location': 'self', 'custody_version': row['custody_version'] + 1},
                    'evidence_reference': evidence_reference, 'received_at': when.isoformat(), 'financial_records_unchanged': True}, ensure_ascii=False)
                for intake_id in ([intake['id'] for intake in intakes] or [None]):
                    registry.execute('''INSERT INTO inventory_custody_events(merchandise_id,intake_id,user_id,actor_id,event_type,details)
                        VALUES(?,?,?,?,?,?)''', (row['id'], intake_id, owner_id, actor.user_id, 'return_received', audit))
            registry._remember(actor, adapter_key, digest, {'physical_item_id': item_id, 'owner_user_id': owner_id,
                'legacy_ids': [row['id'] for row in rows]})
            return result
    except RegistryError as error:
        _failure(error)
