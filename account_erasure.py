"""Reviewable erasure plans and disposable-data rehearsals, never live erasure.

No startup migration, network, provider deletion or production enable switch.
Retained accounting/ownership records and unreviewed stores prevent completion.
"""
from datetime import date
import hashlib
import json
import secrets
import uuid

from account_lifecycle import LifecycleService, columns, tables
from item_registry import RegistryError, _now
from account_erasure_guard import assert_fixture_database

SCHEMA_VERSION = 'account-erasure-plan-2026-10-26-v1'
TASKS = {
    'profile': 'ログイン情報・プロフィール',
    'notifications': '通知先・端末・確認リンク・通知本文',
    'goods': '預かり商品・所有履歴・返送',
    'money': '残高・売上精算・請求・支払い',
    'records': '契約・取引記録・監査証跡',
    'photos': '商品写真・添付ファイル・共有参照',
    'providers': '外部サービスの削除・保持・処理中照合',
    'backups': 'バックアップ・復旧時の再適用',
    'completion_notice': '削除完了の連絡・本人への確認手段',
    'unclassified': '未分類・間接参照・JSON内の関連情報',
}
ACTIONS = {'unconfirmed': '未確定', 'erase': '削除する案', 'retain': '期限と理由を付けて保持する案',
           'not_applicable': '対象なしの案'}


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    sql = [
        'CREATE TABLE IF NOT EXISTS account_erasure_schema(version TEXT PRIMARY KEY NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS account_erasure_plans(
            plan_id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
            customer_id INTEGER NOT NULL REFERENCES users(id), revision INTEGER NOT NULL DEFAULT 1,
            inventory_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            fixture_result_json TEXT NOT NULL DEFAULT '{}')''',
        '''CREATE TABLE IF NOT EXISTS account_erasure_decisions(
            plan_id TEXT NOT NULL REFERENCES account_erasure_plans(plan_id), task_key TEXT NOT NULL,
            action TEXT NOT NULL CHECK(action IN ('unconfirmed','erase','retain','not_applicable')),
            reason TEXT NOT NULL, reference TEXT NOT NULL, retain_until TEXT,
            updated_at TEXT NOT NULL, actor_user_id INTEGER NOT NULL REFERENCES users(id),
            PRIMARY KEY(plan_id,task_key))''',
        '''CREATE TABLE IF NOT EXISTS account_erasure_events(
            event_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES account_erasure_plans(plan_id),
            actor_user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL,
            details_json TEXT NOT NULL, recorded_at TEXT NOT NULL)''',
        "INSERT INTO account_erasure_schema VALUES ('"+SCHEMA_VERSION+"') ON CONFLICT(version) DO NOTHING",
    ]
    if dialect == 'sqlite':
        sql += [f"CREATE TRIGGER IF NOT EXISTS account_erasure_events_no_{op.lower()} BEFORE {op} ON account_erasure_events BEGIN SELECT RAISE(ABORT,'append_only'); END" for op in ('UPDATE', 'DELETE')]
    else:
        sql += ["CREATE OR REPLACE FUNCTION account_erasure_event_immutable() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql",
                'DROP TRIGGER IF EXISTS account_erasure_events_immutable ON account_erasure_events',
                'CREATE TRIGGER account_erasure_events_immutable BEFORE UPDATE OR DELETE ON account_erasure_events FOR EACH ROW EXECUTE FUNCTION account_erasure_event_immutable()']
    from account_erasure_notice import migration_statements as notice_statements
    return sql + notice_statements(dialect)


class ErasureService:
    def __init__(self, store, clock=_now):
        self.store, self.clock = store, clock
        self.lifecycle = LifecycleService(store, clock)

    def ready(self):
        return 'account_erasure_schema' in tables(self.store) and bool(self.store.query(
            'SELECT version FROM account_erasure_schema WHERE version=?', (SCHEMA_VERSION,)))

    def _authorize(self, actor, customer_id):
        self.lifecycle.authorize(actor, customer_id, staff_only=True)
        if not self.ready():
            raise RegistryError('account_erasure_migration_required', '削除計画の準備が未完了です。', 503)

    def _request(self, actor, customer_id, request_id):
        row = self.lifecycle.get(actor, customer_id)
        if not row or row['request_id'] != request_id or row['state'] not in ('requested', 'reviewing', 'erasure_pending'):
            raise RegistryError('deletion_state_conflict', status=409)
        return row

    def _event(self, actor, plan_id, kind, details):
        self.store.execute('INSERT INTO account_erasure_events VALUES (?,?,?,?,?,?)', (
            uuid.uuid4().hex, plan_id, actor.user_id, kind, json.dumps(details, sort_keys=True, ensure_ascii=False), self.clock()))

    def get(self, actor, customer_id, request_id):
        self._authorize(actor, customer_id)
        plans = self.store.query('SELECT * FROM account_erasure_plans WHERE customer_id=? AND request_id=?', (customer_id, request_id))
        if not plans:
            return None
        plan = plans[0]
        plan['inventory'] = json.loads(plan.pop('inventory_json'))
        plan['fixture_result'] = json.loads(plan.pop('fixture_result_json'))
        plan['decisions'] = {d['task_key']: d for d in self.store.query('SELECT * FROM account_erasure_decisions WHERE plan_id=?', (plan['plan_id'],))}
        row = self.lifecycle.get(actor, customer_id)
        plan['editable'] = bool(row and row['request_id'] == request_id and row['state'] in ('requested', 'reviewing', 'erasure_pending'))
        plan['complete'] = False
        plan['unresolved'] = [k for k in TASKS if k not in plan['decisions'] or plan['decisions'][k]['action'] == 'unconfirmed']
        return plan

    def prepare(self, actor, customer_id, request_id):
        self._authorize(actor, customer_id)
        with self.store.atomic():
            self.lifecycle.lock(customer_id)
            self._request(actor, customer_id, request_id)
            old = self.get(actor, customer_id, request_id)
            if old:
                return old
            from account_data_inventory import inventory_data
            snapshot = inventory_data(self.store, customer_id)
            plan_id, now = uuid.uuid4().hex, self.clock()
            self.store.execute('''INSERT INTO account_erasure_plans(plan_id,request_id,customer_id,inventory_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?)''', (plan_id, request_id, customer_id, json.dumps(snapshot, ensure_ascii=False), now, now))
            self._event(actor, plan_id, 'plan_prepared', {'counts_only': True, 'policy_approved': False})
            return self.get(actor, customer_id, request_id)

    def decide(self, actor, customer_id, request_id, *, revision, task_key, action, reason='', reference='', retain_until=None):
        self._authorize(actor, customer_id)
        if task_key not in TASKS or action not in ACTIONS or type(revision) is not int:
            raise RegistryError('erasure_decision_invalid', status=400)
        if not isinstance(reason, str) or not isinstance(reference, str) or len(reason) > 600 or len(reference) > 200:
            raise RegistryError('erasure_decision_invalid', status=400)
        if action != 'unconfirmed' and (not reason.strip() or not reference.strip()):
            raise RegistryError('erasure_evidence_required', status=400)
        if action == 'erase' and task_key in ('goods', 'money', 'records', 'unclassified'):
            raise RegistryError('erasure_scope_unresolved', '所有・金銭・取引・未分類情報の一括消去はできません。', 409)
        if action == 'retain':
            try:
                parsed = date.fromisoformat(retain_until)
                if parsed.isoformat() != retain_until or parsed <= date.fromisoformat(self.clock()[:10]):
                    raise ValueError()
            except (TypeError, ValueError):
                raise RegistryError('retention_expiry_required', '保持理由と将来の保持期限を確認してください。未定なら未確定を選びます。', 400) from None
        elif retain_until:
            raise RegistryError('erasure_decision_invalid', status=400)
        with self.store.atomic():
            self.lifecycle.lock(customer_id)
            self._request(actor, customer_id, request_id)
            plan = self.get(actor, customer_id, request_id)
            if not plan or plan['revision'] != revision:
                raise RegistryError('erasure_plan_stale', '別の変更があります。画面を開き直してください。', 409)
            if plan['fixture_result'] and task_key in ('profile', 'notifications'):
                raise RegistryError('erasure_rehearsal_recorded', status=409)
            now = self.clock()
            self.store.execute('''INSERT INTO account_erasure_decisions VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(plan_id,task_key) DO UPDATE SET action=excluded.action,reason=excluded.reason,
                reference=excluded.reference,retain_until=excluded.retain_until,updated_at=excluded.updated_at,actor_user_id=excluded.actor_user_id''',
                (plan['plan_id'], task_key, action, reason.strip(), reference.strip(), retain_until, now, actor.user_id))
            self.store.execute('UPDATE account_erasure_plans SET revision=revision+1,updated_at=? WHERE plan_id=?', (now, plan['plan_id']))
            # Reasons stay in the bounded review record; never copy personal fields into the journal.
            self._event(actor, plan['plan_id'], 'draft_decision_recorded', {'task_key':task_key, 'action':action, 'revision':revision+1})
            return self.get(actor, customer_id, request_id)

    def rehearse_local(self, actor, customer_id, request_id, *, fixture_database, revision):
        """Erase selected fictional local fields; always remain erasure_pending."""
        self._authorize(actor, customer_id)
        assert_fixture_database(self.store, fixture_database)
        with self.store.atomic():
            self.lifecycle.lock(customer_id)
            self._request(actor, customer_id, request_id)
            plan = self.get(actor, customer_id, request_id)
            if not plan:
                raise RegistryError('erasure_plan_required', status=409)
            if plan['fixture_result']:
                return plan['fixture_result']
            if type(revision) is not int or revision != plan['revision']:
                raise RegistryError('erasure_plan_stale', status=409)
            if any(plan['decisions'].get(key, {}).get('action') != 'erase' for key in ('profile', 'notifications')):
                raise RegistryError('erasure_evidence_required', status=409)
            from account_erasure_notice import NoticeService, protected_destination
            NoticeService(self.store,self.clock).hold_for_erasure(actor,customer_id,request_id)
            self.lifecycle.close_fixture(actor, customer_id, request_id, fixture_database=fixture_database,
                                         review_reference='fixture-plan:'+plan['plan_id'])
            counts = self._scrub_notification_fields(customer_id)
            result = {'fixture_only':True, 'selected_profile_fields_scrubbed':True, 'counts':counts,
                      'completion_contact_held':protected_destination(self.store,customer_id) is not None,
                      'account_state':'erasure_pending', 'complete':False,
                      'remaining':['financial_and_ownership_records','photos_and_attachments','external_providers',
                                   'backups_and_restore','unclassified_and_indirect_data','retention_policy','completion_notice']}
            self.store.execute('UPDATE account_erasure_plans SET fixture_result_json=?,revision=revision+1,updated_at=? WHERE plan_id=?',
                               (json.dumps(result, sort_keys=True), self.clock(), plan['plan_id']))
            self._event(actor, plan['plan_id'], 'local_fixture_erasure', result)
            return result

    def _scrub_notification_fields(self, customer_id):
        from account_erasure_notice import protected_destination
        protected=protected_destination(self.store,customer_id)
        names, counts = tables(self.store), {}
        # Display choices are personal settings too. This helper is reached
        # only by the existing guarded fixture rehearsal after both profile and
        # notification erasure decisions; it does not enable live deletion.
        for table, key in (('mobile_sessions','user_id'), ('push_devices','user_id'),
                           ('stage6_notification_preferences','customer_id'),
                           ('stage6_home_preferences','customer_id')):
            if table in names:
                counts[table] = self.store.execute(f'DELETE FROM {table} WHERE {key}=?', (customer_id,)).rowcount
        fields_by_table = {
            'push_outbox': ('user_id', {'token':'', 'binding':'', 'path':''}),
            'stage6_notifications': ('customer_id', {'source_json':'{}', 'title':'削除済み', 'body':'', 'path':''}),
            'stage6_notification_outbox': ('customer_id', {'private_payload_json':'{}', 'claim_hash':None}),
            'stage6_destinations': ('customer_id', {'address':'', 'address_hash':'', 'binding_hash':None, 'installation':None}),
            'stage6_destination_proofs': ('customer_id', {'address':'', 'expires_at':0}),
        }
        for table, (key, candidates) in fields_by_table.items():
            if table not in names:
                continue
            available = columns(self.store, table)
            fields = {k:v for k,v in candidates.items() if k in available}
            if fields:
                exclusion=' AND destination_id<>?' if table=='stage6_destinations' and protected else ''
                params=(*fields.values(),customer_id,protected) if exclusion else (*fields.values(),customer_id)
                counts[table] = self.store.execute(f'UPDATE {table} SET '+','.join(k+'=?' for k in fields)+f' WHERE {key}=?'+exclusion,params).rowcount
        if 'stage6_destination_proofs' in names:
            for row in self.store.query('SELECT proof_id FROM stage6_destination_proofs WHERE customer_id=?', (customer_id,)):
                self.store.execute('UPDATE stage6_destination_proofs SET token_hash=? WHERE proof_id=?',
                    (hashlib.sha256(secrets.token_bytes(32)).hexdigest(),row['proof_id']))
        # Orphan LINE invitations, immutable journals and unknown payloads need their
        # own ownership/retention review. No LIKE matching or cross-customer cleanup.
        return counts
