"""Scoped permissions and serializable local workflow records, no connectors."""
import hashlib
import json

from item_registry import RegistryError, _json, _now, _text
from stage3_common import FinanceStore

SCHEMA_VERSION = 'stage4-commerce-2026-10-22-v1'


def immutable_statements(table, dialect='sqlite'):
    if not table.startswith('stage4_') or not table.replace('_','').isalnum():
        raise ValueError('Invalid table')
    if dialect == 'sqlite':
        return [f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'append_only'); END" for action in ('UPDATE','DELETE')]
    return ["CREATE OR REPLACE FUNCTION stage4_reject_change() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql",
            f'DROP TRIGGER IF EXISTS {table}_immutable ON {table}',
            f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION stage4_reject_change()']


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite','postgres'):
        raise ValueError('Unsupported dialect')
    return [
        'CREATE TABLE IF NOT EXISTS stage4_schema(version TEXT PRIMARY KEY NOT NULL)',
        'CREATE TABLE IF NOT EXISTS stage4_locks(lock_key TEXT PRIMARY KEY NOT NULL,lock_version INTEGER NOT NULL DEFAULT 0)',
        '''CREATE TABLE IF NOT EXISTS stage4_operations(source_key TEXT PRIMARY KEY NOT NULL,
            actor_user_id INTEGER NOT NULL REFERENCES users(id), customer_id INTEGER REFERENCES users(id),
            request_hash TEXT NOT NULL,result_json TEXT NOT NULL,recorded_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS stage4_media(media_id TEXT PRIMARY KEY NOT NULL,
            physical_item_id TEXT NOT NULL REFERENCES business_items(physical_item_id),
            ownership_assignment_id TEXT NOT NULL REFERENCES business_item_assignments(assignment_id),
            storage_path TEXT NOT NULL UNIQUE,content_sha256 TEXT NOT NULL,byte_size INTEGER NOT NULL CHECK(byte_size>0),
            actor_user_id INTEGER NOT NULL REFERENCES users(id),recorded_at TEXT NOT NULL)''',
        "INSERT INTO stage4_schema(version) VALUES ('"+SCHEMA_VERSION+"') ON CONFLICT(version) DO NOTHING",
    ] + immutable_statements('stage4_operations',dialect) + immutable_statements('stage4_media',dialect)


class Stage4Store:
    def __init__(self, registry):
        self.registry = registry
        self.finance = FinanceStore(registry)
        self.connection, self.dialect = registry.connection, registry.dialect

    def execute(self, sql, params=()):
        return self.registry.execute(sql,params)

    def query(self, sql, params=()):
        return self.registry.query(sql,params)

    def atomic(self):
        return self.registry.atomic()

    def require_ready(self):
        self.finance.require_ready()
        try:
            ready = self.query('SELECT version FROM stage4_schema WHERE version=?',(SCHEMA_VERSION,))
        except Exception:
            ready = False
        if not ready:
            raise RegistryError('stage4_migration_required','段階4の検証用データ構造が未準備です。',409)

    def authorize(self, actor, customer_id=None, staff_required=False, finance=False):
        self.require_ready()
        staff = self.registry.actor(actor,staff_required=staff_required)
        user = self.query('SELECT role FROM users WHERE id=?',(actor.user_id,))[0]
        if not staff and user['role'] != 'user':
            raise RegistryError('customer_required',status=403)
        if customer_id is not None:
            if not staff and customer_id != actor.user_id:
                raise RegistryError('customer_access_denied',status=403)
            self.registry._customer_exists(customer_id)
        if finance:
            self.finance.authorize(actor,customer_id,write=staff_required)
        return staff

    def lock_item(self,item_id):
        suffix = ' FOR UPDATE' if self.dialect == 'postgres' else ''
        if not self.query('SELECT physical_item_id FROM business_items WHERE physical_item_id=?'+suffix,(item_id,)):
            raise RegistryError('item_not_found',status=404)
        self.query('SELECT physical_item_id FROM business_item_profiles WHERE physical_item_id=?'+suffix,(item_id,))

    def operation(self,actor,source_key,payload,callback,customer_id=None,staff_required=False,finance=False):
        staff = self.authorize(actor,customer_id,staff_required,finance)
        if not staff and customer_id is None:
            raise RegistryError('customer_scope_required',status=403)
        key = _text(source_key,'source_key',500)
        digest = hashlib.sha256(_json(payload).encode()).hexdigest()
        with self.atomic():
            # Serialize stage-4 transitions; acquire customer before item locks,
            # matching the stage-3 ledger order. No external call holds a lock.
            self.execute("INSERT INTO stage4_locks(lock_key) VALUES ('workflow') ON CONFLICT(lock_key) DO NOTHING")
            self.execute("UPDATE stage4_locks SET lock_version=lock_version WHERE lock_key='workflow'")
            if customer_id is not None:
                self.finance.customer_lock(customer_id)
            previous = self.query('SELECT * FROM stage4_operations WHERE source_key=?',(key,))
            if previous:
                row = previous[0]
                if row['actor_user_id'] != actor.user_id or row['customer_id'] != customer_id or row['request_hash'] != digest:
                    raise RegistryError('source_key_conflict','同じ操作番号の内容が異なります。',409)
                return json.loads(row['result_json'])
            result = callback()
            self.execute('INSERT INTO stage4_operations(source_key,actor_user_id,customer_id,request_hash,result_json,recorded_at) VALUES (?,?,?,?,?,?)',
                (key,actor.user_id,customer_id,digest,_json(result),_now()))
            return result

    def listing_photo_allowed(self,item_id,customer_id,reference):
        roles = self.registry._roles(item_id)
        owner = roles.get('owner',{})
        if owner.get('user_id') != customer_id:
            return False
        rows = self.query('''SELECT m.*,a.party_id FROM stage4_media m JOIN business_item_assignments a
            ON a.assignment_id=m.ownership_assignment_id WHERE m.media_id=? AND m.physical_item_id=?''',(reference,item_id))
        if rows:
            if rows[0]['ownership_assignment_id']==owner['assignment_id']:
                return True
            if rows[0]['party_id']=='kaika':
                exists = self.query("SELECT to_regclass('stage4_purchase_orders') AS name") if self.dialect == 'postgres' else self.query("SELECT name FROM sqlite_master WHERE type='table' AND name='stage4_purchase_orders'")
                if not exists or not exists[0]['name']:
                    return False
                proof = self.reusable_catalog_registration(item_id,customer_id)
                return bool(proof and reference in proof['photo_refs'])
            return False
        # The existing photo ACL remains authoritative for legacy photographs.
        exists = self.query("SELECT to_regclass('stage2_photo_records') AS name") if self.dialect == 'postgres' else self.query("SELECT name FROM sqlite_master WHERE type='table' AND name='stage2_photo_records'")
        if not exists or not exists[0]['name']:
            return False
        return bool(self.query('''SELECT p.photo_path FROM stage2_photo_records p JOIN business_item_links l
            ON l.merchandise_id=p.merchandise_id WHERE l.physical_item_id=? AND p.owner_user_id=?
            AND p.photo_path=? AND p.removed_from_item_at IS NULL''',(item_id,customer_id,reference)))

    def reusable_catalog_registration(self,item_id,customer_id):
        exists = self.query("SELECT to_regclass('stage4_purchase_orders') AS name") if self.dialect == 'postgres' else self.query("SELECT name FROM sqlite_master WHERE type='table' AND name='stage4_purchase_orders'")
        if not exists or not exists[0]['name']:
            return None
        from stage4_catalog import catalog_registration_proof
        return catalog_registration_proof(self,item_id,customer_id)
