"""Deletion requests and auditable closure rehearsal; never a production eraser.

No I/O at import. Explicit migrations only. Financial/ownership records, photos,
third-party records and backups are not silently deleted or considered erased.
"""
import json
import re
import secrets
import uuid
from pathlib import Path
import tempfile

from item_registry import RegistryError, _now
from werkzeug.security import generate_password_hash

SCHEMA_VERSION = 'account-lifecycle-2026-10-25-v1'
CLOSED = ('access_closed', 'erasure_pending', 'completed')
REVIEW_CATEGORIES = ('goods', 'funds', 'subscriptions', 'retention', 'providers', 'photos', 'backups')
REVIEW_LABELS = dict(zip(REVIEW_CATEGORIES, (
    '預かり商品・未発送・返送', '未精算売上・残高・請求', '継続課金と購入元',
    '契約・取引の保持理由と期限', '決済・通知など外部サービスのデータ',
    '商品写真・添付ファイル', 'バックアップと復旧時の削除反映')))


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    statements = [
        'CREATE TABLE IF NOT EXISTS account_lifecycle_schema(version TEXT PRIMARY KEY NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS account_deletion_requests(
            customer_id INTEGER PRIMARY KEY REFERENCES users(id), request_id TEXT UNIQUE NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('requested','reviewing','withdrawn','access_closed','erasure_pending','completed')),
            requested_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            review_json TEXT NOT NULL DEFAULT '{}', customer_notice TEXT NOT NULL DEFAULT '',
            execution_scope TEXT NOT NULL DEFAULT 'request_only' CHECK(execution_scope IN ('request_only','fixture_only')),
            version INTEGER NOT NULL DEFAULT 1)''',
        '''CREATE TABLE IF NOT EXISTS account_deletion_events(
            event_id TEXT PRIMARY KEY NOT NULL, request_id TEXT NOT NULL, customer_id INTEGER NOT NULL REFERENCES users(id),
            actor_user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL,
            details_json TEXT NOT NULL, recorded_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS account_deletion_reauth(
            customer_id INTEGER PRIMARY KEY REFERENCES users(id), failures INTEGER NOT NULL DEFAULT 0,
            window_started BIGINT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS account_deletion_operations(
            source_key TEXT PRIMARY KEY NOT NULL, customer_id INTEGER NOT NULL REFERENCES users(id),
            request_id TEXT NOT NULL, recorded_at TEXT NOT NULL)''',
        "INSERT INTO account_lifecycle_schema(version) VALUES ('" + SCHEMA_VERSION + "') ON CONFLICT(version) DO NOTHING",
    ]
    if dialect == 'sqlite':
        statements += [f"CREATE TRIGGER IF NOT EXISTS account_deletion_events_no_{op.lower()} BEFORE {op} ON account_deletion_events BEGIN SELECT RAISE(ABORT,'append_only'); END" for op in ('UPDATE','DELETE')]
    else:
        statements += ["CREATE OR REPLACE FUNCTION account_deletion_reject_event_change() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql",
            'DROP TRIGGER IF EXISTS account_deletion_events_immutable ON account_deletion_events',
            'CREATE TRIGGER account_deletion_events_immutable BEFORE UPDATE OR DELETE ON account_deletion_events FOR EACH ROW EXECUTE FUNCTION account_deletion_reject_event_change()']
    return statements


def tables(store):
    if store.dialect == 'postgres':
        rows = store.query("SELECT table_name AS name FROM information_schema.tables WHERE table_schema=current_schema() AND table_type='BASE TABLE'")
    else:
        rows = store.query("SELECT name FROM sqlite_master WHERE type='table'")
    return {row['name'] for row in rows}


def columns(store, table):
    if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', table):
        raise ValueError('Unsafe identifier')
    if store.dialect == 'postgres':
        return {r['name'] for r in store.query('SELECT column_name AS name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=?', (table,))}
    return {r['name'] for r in store.query('PRAGMA table_info("' + table + '")')}


def state_in_store(store, customer_id):
    if 'account_deletion_requests' not in tables(store):
        return None
    rows = store.query('SELECT state FROM account_deletion_requests WHERE customer_id=?', (customer_id,))
    return rows[0]['state'] if rows else None


def outbound_allowed_in_store(store, customer_id):
    return state_in_store(store, customer_id) in (None, 'withdrawn')


def access_allowed_in_store(store, customer_id):
    return state_in_store(store, customer_id) not in CLOSED


class LifecycleService:
    def __init__(self, store, clock=_now):
        self.store, self.clock = store, clock

    def ready(self):
        if 'account_lifecycle_schema' not in tables(self.store) or not self.store.query(
                'SELECT version FROM account_lifecycle_schema WHERE version=?', (SCHEMA_VERSION,)):
            raise RegistryError('account_lifecycle_migration_required', '削除申請の受付準備が未完了です。', 503)

    def authorize(self, actor, customer_id=None, *, staff_only=False):
        self.ready()
        users = self.store.query('SELECT * FROM users WHERE id=?', (actor.user_id,))
        if not users:
            raise RegistryError('account_access_denied', status=403)
        user = users[0]
        permissions = user.get('admin_permissions')
        try:
            permissions = json.loads(permissions) if permissions else None
        except (ValueError, TypeError):
            permissions = ['invalid']
        staff = user['role'] == 'owner' or user['role'] == 'admin' and (permissions is None or isinstance(permissions, list) and 'users' in permissions)
        if staff_only and not staff or not staff and (user['role'] != 'user' or customer_id not in (None, actor.user_id)):
            raise RegistryError('account_access_denied', status=403)
        if customer_id is not None:
            target = self.store.query('SELECT role FROM users WHERE id=?', (customer_id,))
            if not target or target[0]['role'] != 'user':
                raise RegistryError('customer_required', status=404)
        return staff

    def lock(self, customer_id):
        # Same order as the payment/notification workflow. No network in this TX.
        if 'stage4_locks' in tables(self.store):
            self.store.execute("INSERT INTO stage4_locks(lock_key) VALUES ('workflow') ON CONFLICT(lock_key) DO NOTHING")
            self.store.execute("UPDATE stage4_locks SET lock_version=lock_version WHERE lock_key='workflow'")
        self.store.execute('UPDATE users SET id=id WHERE id=?', (customer_id,))

    def get(self, actor, customer_id):
        self.authorize(actor, customer_id)
        rows = self.store.query('SELECT * FROM account_deletion_requests WHERE customer_id=?', (customer_id,))
        if not rows:
            return None
        row = rows[0]
        row['review'] = json.loads(row.pop('review_json'))
        return row

    def event(self, actor, row, kind, details=None):
        self.store.execute('INSERT INTO account_deletion_events VALUES (?,?,?,?,?,?,?)', (
            uuid.uuid4().hex, row['request_id'], row['customer_id'], actor.user_id, kind,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True), self.clock()))

    def suppress(self, customer_id, closed=False):
        names = tables(self.store)
        if 'push_outbox' in names:
            self.store.execute("UPDATE push_outbox SET state='cancelled' WHERE user_id=? AND state='queued'", (customer_id,))
            self.store.execute("UPDATE push_outbox SET state='unknown' WHERE user_id=? AND state IN ('sending','receipt')", (customer_id,))
        if 'stage6_notification_outbox' in names:
            from stage6_notifications import suppress_customer_notification_jobs
            suppress_customer_notification_jobs(self.store, customer_id,
                'account_closed' if closed else 'account_deletion_pending', revoke=closed)
        if 'stage9_payment_jobs' in names:
            from stage9_payments import suppress_customer_payment_jobs
            suppress_customer_payment_jobs(self.store, customer_id,
                'account_closed' if closed else 'account_deletion_pending')

    def request(self, actor, *, source_key=None):
        # Always accept the customer's request even when goods/debts remain.
        self.authorize(actor, actor.user_id)
        if self.store.query('SELECT role FROM users WHERE id=?', (actor.user_id,))[0]['role'] != 'user':
            raise RegistryError('customer_required', status=403)
        with self.store.atomic():
            self.lock(actor.user_id)
            old = self.get(actor, actor.user_id)
            if source_key is not None:
                if not isinstance(source_key,str) or not 1 <= len(source_key) <= 256:
                    raise RegistryError('invalid_source_key', status=400)
                prior=self.store.query('SELECT * FROM account_deletion_operations WHERE source_key=?',(source_key,))
                if prior:
                    if prior[0]['customer_id'] != actor.user_id or not old or old['request_id'] != prior[0]['request_id']:
                        raise RegistryError('deletion_state_conflict', status=409)
                    return old  # A delayed request cannot undo a later withdrawal.
            if old and old['state'] != 'withdrawn':
                if source_key is not None:
                    self.store.execute('INSERT INTO account_deletion_operations VALUES (?,?,?,?)',
                        (source_key,actor.user_id,old['request_id'],self.clock()))
                return old
            request_id, now = uuid.uuid4().hex, self.clock()
            self.store.execute('''INSERT INTO account_deletion_requests(customer_id,request_id,state,requested_at,updated_at)
                VALUES (?,?,'requested',?,?) ON CONFLICT(customer_id) DO UPDATE SET request_id=excluded.request_id,
                state='requested',requested_at=excluded.requested_at,updated_at=excluded.updated_at,
                review_json='{}',customer_notice='',execution_scope='request_only',version=account_deletion_requests.version+1''',
                (actor.user_id, request_id, now, now))
            row = self.get(actor, actor.user_id)
            if source_key is not None:
                self.store.execute('INSERT INTO account_deletion_operations VALUES (?,?,?,?)',
                    (source_key,actor.user_id,row['request_id'],now))
            self.suppress(actor.user_id)
            self.event(actor, row, 'requested')
            return row

    def withdraw(self, actor, request_id):
        self.authorize(actor, actor.user_id)
        with self.store.atomic():
            self.lock(actor.user_id)
            row = self.get(actor, actor.user_id)
            if not row or row['request_id'] != request_id or row['state'] not in ('requested', 'reviewing', 'withdrawn'):
                raise RegistryError('deletion_state_conflict', status=409)
            if row['state'] != 'withdrawn':
                self.store.execute("UPDATE account_deletion_requests SET state='withdrawn',updated_at=?,version=version+1 WHERE customer_id=?", (self.clock(), actor.user_id))
                self.event(actor, row, 'withdrawn')
            # Suppressed messages/jobs are deliberately not automatically replayed.

    def review(self, actor, customer_id, request_id, category, reference, notice=''):
        self.authorize(actor, customer_id, staff_only=True)
        if category not in REVIEW_CATEGORIES or not reference.strip() or len(reference) > 200 or len(notice) > 1000:
            raise RegistryError('review_evidence_required', status=400)
        with self.store.atomic():
            self.lock(customer_id)
            row = self.get(actor, customer_id)
            if not row or row['request_id'] != request_id or row['state'] not in ('requested','reviewing'):
                raise RegistryError('deletion_state_conflict', status=409)
            review = row['review']
            review[category] = {'reference': reference, 'reviewed_at': self.clock(), 'actor_id': actor.user_id}
            self.store.execute("UPDATE account_deletion_requests SET state='reviewing',review_json=?,customer_notice=?,updated_at=?,version=version+1 WHERE customer_id=?",
                (json.dumps(review, ensure_ascii=False), notice, self.clock(), customer_id))
            self.event(actor, row, 'review_recorded', {'category': category, 'reference': reference})

    def inventory(self, actor, customer_id):
        self.authorize(actor, customer_id, staff_only=True)
        results = []
        for name in sorted(tables(self.store)):
            if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name) or name.startswith('sqlite_'):
                continue
            keys = sorted(columns(self.store, name) & {'user_id','customer_id','owner_user_id','initial_customer_id','actor_user_id'})
            if name == 'users':
                keys = ['id']
            count = self.store.query('SELECT COUNT(*) AS n FROM "' + name + '" WHERE ' + ' OR '.join('"'+k+'"=?' for k in keys), tuple(customer_id for _ in keys))[0]['n'] if keys else None
            results.append({'table': name, 'direct_records': count, 'classification': 'unreviewed'})
        # This is discovery, not a legal retention policy or complete FK graph.
        return results

    def close_fixture(self, actor, customer_id, request_id, *, fixture_database, review_reference):
        """Rehearse auth revocation on an explicit disposable SQLite fixture only.

        Remains erasure_pending: it is NOT completion of the deletion request.
        No public HTTP route exposes this executor. No production-enable flag.
        """
        self.authorize(actor, customer_id, staff_only=True)
        if self.store.dialect != 'sqlite' or not review_reference.strip():
            raise RegistryError('fixture_only', status=403)
        from account_erasure_guard import assert_fixture_database
        assert_fixture_database(self.store, fixture_database)
        with self.store.atomic():
            self.lock(customer_id)
            row = self.get(actor, customer_id)
            if not row or row['request_id'] != request_id or row['state'] not in ('requested','reviewing','erasure_pending'):
                raise RegistryError('deletion_state_conflict', status=409)
            if row['state'] == 'erasure_pending':
                return row
            names = tables(self.store)
            if 'mobile_sessions' in names:
                self.store.execute('UPDATE mobile_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', (self.clock(), customer_id))
            if 'push_devices' in names:
                self.store.execute('UPDATE push_devices SET enabled=0 WHERE user_id=?', (customer_id,))
            self.suppress(customer_id, closed=True)
            usercols = columns(self.store, 'users')
            # Only reviewed authentication/profile fields. Never financial fields.
            fields = {k: '' for k in ('email','display_name','phone','postal_code','address','shipping_addresses','last_name','first_name') if k in usercols}
            if 'username' in usercols:
                fields['username'] = 'closed-' + uuid.uuid4().hex
            if 'password_hash' in usercols:
                fields['password_hash'] = generate_password_hash(secrets.token_urlsafe(64))
            if fields:
                self.store.execute('UPDATE users SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', (*fields.values(), customer_id))
            self.store.execute("UPDATE account_deletion_requests SET state='erasure_pending',execution_scope='fixture_only',updated_at=?,version=version+1 WHERE customer_id=?", (self.clock(), customer_id))
            self.event(actor, row, 'fixture_access_closed', {'reference': review_reference, 'erasure_complete': False})
            return self.get(actor, customer_id)
