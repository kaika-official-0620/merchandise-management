"""Shared authorization/transactions for opt-in financial review records.

No connector, payment execution, startup migration or production configuration.
The caller owns the final transaction. Staff need inventory AND invoice rights.
"""
import hashlib
import json

from item_registry import Actor, RegistryError, _json, _now, _text

SCHEMA_VERSION = 'stage3-finance-2026-10-22-v1'


def financial_event_order_ambiguous(events):
    """Legacy equal-time sale-cycle records have no trustworthy UUID order.

    New registry writes serialize their recorded times. Preserve old evidence
    and require review instead of guessing whether a shipment preceded resale.
    """
    seen = set()
    for event in events:
        if event['kind'] not in ('sold', 'shipped', 'returned', 'sale_cancelled'):
            continue
        key = (event['occurred_at'], event['recorded_at'])
        if key in seen:
            return True
        seen.add(key)
    return False


def yen(value, *, signed=False, optional=False):
    if value is None and optional:
        return None
    if type(value) is not int or abs(value) > 9_000_000_000_000 or (value < 0 and not signed):
        raise RegistryError('integer_yen_required', '金額は円単位の整数で入力してください。')
    return value


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    statements = [
        'CREATE TABLE IF NOT EXISTS stage3_schema(version TEXT PRIMARY KEY NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS stage3_customer_locks(
            customer_id INTEGER PRIMARY KEY REFERENCES users(id), lock_version INTEGER NOT NULL DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS stage3_operations(
            source_key TEXT PRIMARY KEY NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            customer_id INTEGER NOT NULL REFERENCES users(id), request_hash TEXT NOT NULL,
            result_json TEXT NOT NULL, recorded_at TEXT NOT NULL)''',
        "INSERT INTO stage3_schema(version) VALUES ('" + SCHEMA_VERSION + "') ON CONFLICT(version) DO NOTHING",
    ]
    statements += immutable_statements('stage3_operations', dialect)
    return statements


def immutable_statements(table, dialect='sqlite'):
    if not table.startswith('stage3_') or not table.replace('_', '').isalnum():
        raise ValueError('Invalid table')
    if dialect == 'sqlite':
        return [f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()}
            BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END'''
                for action in ('UPDATE', 'DELETE')]
    return ["""CREATE OR REPLACE FUNCTION stage3_reject_change() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql""",
        f'DROP TRIGGER IF EXISTS {table}_immutable ON {table}',
        f'''CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION stage3_reject_change()''']


class FinanceStore:
    def __init__(self, registry):
        self.registry = registry
        self.connection = registry.connection
        self.dialect = registry.dialect

    def execute(self, sql, params=()):
        return self.registry.execute(sql, params)

    def query(self, sql, params=()):
        return self.registry.query(sql, params)

    def atomic(self):
        return self.registry.atomic()

    def require_ready(self):
        self.registry._require_ready()
        try:
            ready = self.query('SELECT version FROM stage3_schema WHERE version=?', (SCHEMA_VERSION,))
        except Exception:
            ready = False
        if not ready:
            raise RegistryError('stage3_migration_required', '段階3の検証用データ構造が未準備です。', 409)

    def authorize(self, actor, customer_id=None, write=False):
        self.require_ready()
        staff = self.registry.actor(actor)
        rows = self.query('SELECT * FROM users WHERE id=?', (actor.user_id,))
        user = rows[0]
        finance = user.get('role') == 'owner'
        if user.get('role') == 'admin':
            raw = user.get('admin_permissions')
            if not raw:
                finance = True
            else:
                try:
                    permissions = json.loads(raw) if isinstance(raw, str) else raw
                    finance = isinstance(permissions, list) and 'invoices' in permissions
                except (ValueError, TypeError):
                    finance = False
        if actor.staff and not finance or write and not (staff and finance):
            raise RegistryError('finance_permission_required', '商品と請求を扱うスタッフ権限が必要です。', 403)
        if not staff and user.get('role') != 'user':
            raise RegistryError('customer_required', status=403)
        if customer_id is not None:
            if not staff and actor.user_id != customer_id:
                raise RegistryError('customer_access_denied', status=403)
            self.registry._customer_exists(customer_id)
        return bool(staff and finance)

    def item(self, actor, item_id, write=False):
        self.authorize(actor, write=write)
        item = self.registry.detail(actor, item_id)
        self.authorize(actor, item['owner_user_id'], write=write)
        return item

    def customer_lock(self, customer_id):
        self.registry._customer_exists(customer_id)
        # Serialize before reading monetary balances. SQLite's first write takes
        # the database write lock; PostgreSQL locks this customer's shared row.
        self.execute('INSERT INTO stage3_customer_locks(customer_id) VALUES (?) ON CONFLICT(customer_id) DO NOTHING', (customer_id,))
        self.execute('UPDATE stage3_customer_locks SET lock_version=lock_version WHERE customer_id=?', (customer_id,))

    def operation(self, actor, customer_id, source_key, payload, callback):
        self.authorize(actor, customer_id, write=True)
        source_key = _text(source_key, 'source_key', 500)
        digest = hashlib.sha256(_json(payload).encode('utf-8')).hexdigest()
        with self.atomic():
            self.customer_lock(customer_id)
            rows = self.query('SELECT * FROM stage3_operations WHERE source_key=?', (source_key,))
            if rows:
                old = rows[0]
                if (old['actor_user_id'] != actor.user_id or old['customer_id'] != customer_id
                        or old['request_hash'] != digest):
                    raise RegistryError('source_key_conflict', '同じ操作番号に異なる内容が指定されました。', 409)
                return json.loads(old['result_json'])
            result = callback()
            self.execute('''INSERT INTO stage3_operations(source_key,actor_user_id,customer_id,
                request_hash,result_json,recorded_at) VALUES (?,?,?,?,?,?)''',
                (source_key, actor.user_id, customer_id, digest, _json(result), _now()))
            return result
