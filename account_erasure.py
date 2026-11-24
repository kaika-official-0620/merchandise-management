"""Reviewable erasure plans and evidence-gated synthetic-only completion.

No startup migration, network, real provider deletion or production switch.
Unreviewed records and copies block completion in the disposable fixture.
"""
from datetime import date
import hashlib
import json
import re
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
COMPLETION_ACTIONS = {
    'profile': {'erase'}, 'notifications': {'erase'},
    'goods': {'retain'}, 'money': {'retain'}, 'records': {'retain'},
    'photos': {'erase', 'not_applicable'}, 'providers': {'erase', 'retain', 'not_applicable'},
    'backups': {'erase', 'retain', 'not_applicable'},
    'completion_notice': {'retain', 'not_applicable'}, 'unclassified': {'not_applicable'},
}
_REMOVED_PERSONAL_ROWS = {'mobile_sessions': 'user_id', 'push_devices': 'user_id',
                          'stage6_notification_preferences': 'customer_id', 'stage6_home_preferences': 'customer_id'}
_SCRUBBED_PERSONAL_FIELDS = {
    'push_outbox': ('user_id', {'token': '', 'binding': '', 'path': ''}),
    'stage6_notifications': ('customer_id', {'source_json': '{}', 'title': '削除済み', 'body': '', 'path': ''}),
    'stage6_notification_outbox': ('customer_id', {'private_payload_json': '{}', 'claim_hash': None}),
    'stage6_destinations': ('customer_id', {'address': '', 'address_hash': '', 'binding_hash': None, 'installation': None}),
    'stage6_destination_proofs': ('customer_id', {'address': '', 'expires_at': 0}),
}
_ACTIVE_NOTICE_STATES = {'push_outbox': {'queued', 'sending', 'receipt'},
                         'stage6_notification_outbox': {'queued', 'claimed', 'failed', 'sending'}}
_REVIEWED_FIXTURE_TABLES = {
    'users', 'merchandise', 'stage2_photo_records', 'business_item_links', 'business_parties',
    'business_item_assignments', 'invoices', 'stage6_delivery_audit',
    'account_restore_environment', 'account_restore_bindings', 'account_restore_replays',
    'account_lifecycle_schema', 'account_deletion_requests', 'account_deletion_events',
    'account_deletion_reauth', 'account_deletion_operations', 'account_erasure_schema', 'account_erasure_plans',
    'account_erasure_decisions', 'account_erasure_events', 'account_erasure_notice_schema',
    'account_erasure_notices', 'account_erasure_notice_events', 'account_erasure_notice_operations',
    *_REMOVED_PERSONAL_ROWS, *_SCRUBBED_PERSONAL_FIELDS,
}
_SHA256 = re.compile(r'[a-f0-9]{64}')


def _completion_digest(evidence):
    value = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(value.encode()).hexdigest()


def _event_matches(serialized, expected):
    try:
        return serialized == json.dumps(expected, sort_keys=True, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return False


def _retention_in_future(value, now):
    try:
        expiry = date.fromisoformat(value)
        return expiry.isoformat() == value and expiry > date.fromisoformat(now[:10])
    except (TypeError, ValueError):
        return False


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
        plan['complete'] = self._recorded_completion(plan, row) is not None
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
            if not _retention_in_future(retain_until, self.clock()):
                raise RegistryError('retention_expiry_required', '保持理由と将来の保持期限を確認してください。未定なら未確定を選びます。', 400)
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

    def _recorded_completion(self, plan, row):
        if (not row or row['state'] != 'completed' or row['execution_scope'] != 'fixture_only'
                or 'kaika_recovery_quarantine' in tables(self.store)):
            return None
        result = plan['fixture_result']
        completion = result.get('completion') if isinstance(result, dict) else None
        if (not isinstance(completion, dict) or completion.get('complete') is not True
                or completion.get('state') != 'completed' or completion.get('request_id') != row['request_id']
                or type(completion.get('customer_id')) is not int or completion['customer_id'] != row['customer_id']
                or completion.get('plan_id') != plan['plan_id'] or not isinstance(completion.get('evidence_sha256'), str)
                or not _SHA256.fullmatch(completion['evidence_sha256'])):
            return None
        events = self.store.query("SELECT details_json FROM account_erasure_events WHERE plan_id=? AND kind='fixture_completed'",
                                  (plan['plan_id'],))
        if len(events) == 1 and _event_matches(events[0]['details_json'], completion):
            return completion
        return None

    def fixture_completion(self, actor, customer_id, request_id):
        """Read the paired plan/journal record for the synthetic completion mail."""
        plan = self.get(actor, customer_id, request_id)
        if plan and plan['complete']:
            return plan['fixture_result']['completion']
        return None

    def complete_fixture(self, actor, customer_id, request_id, *, fixture_database, revision,
                         file_plan_id=None, fixture_root=None, backup=None, providers=None):
        """Finalize only the marked synthetic case after independently checking its evidence."""
        from account_erasure_notice import NoticeService, protected_destination
        from account_erasure_files import reconcile_fixture_photo_references
        from account_erasure_copies import verify_fixture_copies

        self._authorize(actor, customer_id)
        assert_fixture_database(self.store, fixture_database)
        if type(revision) is not int:
            raise RegistryError('erasure_plan_stale', status=409)
        with self.store.atomic():
            self.lifecycle.lock(customer_id)
            if 'kaika_recovery_quarantine' in tables(self.store):
                raise RegistryError('erasure_fixture_restore_quarantined', status=409)
            row = self.lifecycle.get(actor, customer_id)
            if not row or row['request_id'] != request_id or row['state'] not in ('requested', 'reviewing', 'erasure_pending', 'completed'):
                raise RegistryError('deletion_state_conflict', status=409)
            plan = self.get(actor, customer_id, request_id)
            if row['state'] == 'completed':
                if plan and plan['complete']:
                    return plan['fixture_result']['completion']
                raise RegistryError('erasure_completion_evidence_required', status=409)
            if not plan or plan['revision'] != revision:
                raise RegistryError('erasure_plan_stale', status=409)

            def pending(blockers):
                return {'state': row['state'], 'complete': False, 'blockers': sorted(set(blockers))}

            if row['state'] != 'erasure_pending' or row['execution_scope'] != 'fixture_only':
                return pending(['local_erasure_required'])
            now = self.clock()
            decisions = plan['decisions']
            blockers = self._completion_decision_blockers(decisions, now)
            held = protected_destination(self.store, customer_id)
            if not self._local_erasure_confirmed(plan, customer_id, held):
                blockers.append('local_erasure_required')
            if self._unreviewed_fixture_data():
                blockers.append('unclassified')
            notice = NoticeService(self.store, self.clock).get(actor, customer_id, request_id)
            notice_action = decisions.get('completion_notice', {}).get('action')
            if (held and (notice_action != 'retain' or notice['state'] != 'held' or not notice['consented'] or not notice['held'])
                    or not held and (notice_action != 'not_applicable' or notice['consented'])):
                blockers.append('completion_notice')
            if blockers:
                return pending(blockers)
            try:
                copies = verify_fixture_copies(self.store, actor, customer_id, request_id, fixture_database=fixture_database,
                                               backup=backup, providers=providers)
            except RegistryError as error:
                if error.code in {'erasure_copy_evidence_required', 'erasure_copy_scope_mismatch', 'erasure_copy_policy_invalid'}:
                    return pending(['backups', 'providers'])
                raise
            for task in ('backups', 'providers'):
                proof = copies.get(task, {})
                decision = decisions[task]
                if (any(proof.get(name) != decision[name] for name in ('action', 'reference', 'retain_until'))
                        or not _SHA256.fullmatch(str(proof.get('digest', '')))):
                    blockers.append(task)
            if blockers:
                return pending(blockers)
            if fixture_root is None:
                return pending(['photos'])
            try:
                photos = reconcile_fixture_photo_references(self.store, customer_id, fixture_root=fixture_root,
                                                            plan_id=file_plan_id, action=decisions['photos']['action'])
            except RegistryError as error:
                if error.code.startswith('file_cleanup_'):
                    return pending(['photos'])
                raise
            task_proofs = {'photos': photos, **copies}
            evidence_sha256 = _completion_digest({'customer_id': customer_id, 'request_id': request_id, 'plan_id': plan['plan_id'],
                                                  'revision': revision, 'checks': task_proofs, 'decisions': decisions})
            result = {'state': 'completed', 'complete': True, 'blockers': [], 'fixture_only': True, 'customer_id': customer_id,
                      'request_id': request_id, 'plan_id': plan['plan_id'], 'recorded_at': now, 'evidence_sha256': evidence_sha256,
                      'deleted_photos': photos['deleted'], 'photo_references_removed': photos['references_removed']}
            fixture_result = {**plan['fixture_result'], 'account_state': 'completed', 'complete': True, 'remaining': [], 'completion': result}
            self.store.execute('UPDATE account_erasure_plans SET fixture_result_json=?,revision=revision+1,updated_at=? WHERE plan_id=?',
                               (json.dumps(fixture_result, sort_keys=True), now, plan['plan_id']))
            self.store.execute("UPDATE account_deletion_requests SET state='completed',updated_at=?,version=version+1 WHERE customer_id=? AND request_id=?",
                               (now, customer_id, request_id))
            self._event(actor, plan['plan_id'], 'fixture_completed', result)
            self.lifecycle.event(actor, row, 'fixture_completed', {'evidence_sha256': evidence_sha256})
            return result

    @staticmethod
    def _completion_decision_blockers(decisions, now):
        blockers = []
        for task, allowed in COMPLETION_ACTIONS.items():
            decision = decisions.get(task, {})
            action = decision.get('action')
            if (action not in allowed or not decision.get('reason') or not decision.get('reference')
                    or action == 'retain' and not _retention_in_future(decision.get('retain_until'), now)):
                blockers.append(task)
        return blockers

    def _local_erasure_confirmed(self, plan, customer_id, held):
        previous = plan['fixture_result']
        if (not isinstance(previous, dict) or previous.get('fixture_only') is not True
                or previous.get('selected_profile_fields_scrubbed') is not True):
            return False
        events = self.store.query("SELECT details_json FROM account_erasure_events WHERE plan_id=? AND kind='local_fixture_erasure'",
                                  (plan['plan_id'],))
        if len(events) != 1 or not _event_matches(events[0]['details_json'], previous):
            return False
        user = self.store.query('SELECT * FROM users WHERE id=?', (customer_id,))[0]
        if (not str(user.get('username', '')).startswith('closed-') or not user.get('password_hash')
                or any(user.get(field) for field in ('email', 'display_name', 'phone', 'postal_code', 'address',
                                                     'shipping_addresses', 'last_name', 'first_name'))):
            return False
        names = tables(self.store)
        for table, key in _REMOVED_PERSONAL_ROWS.items():
            if table in names and self.store.query(f'SELECT 1 FROM {table} WHERE {key}=? LIMIT 1', (customer_id,)):
                return False
        for table, (key, candidates) in _SCRUBBED_PERSONAL_FIELDS.items():
            if table not in names:
                continue
            for current in self.store.query(f'SELECT * FROM {table} WHERE {key}=?', (customer_id,)):
                if (current.get('state') in _ACTIVE_NOTICE_STATES.get(table, ())
                        or table == 'stage6_destinations' and current.get('revoked_at') is None):
                    return False
                if table == 'stage6_destinations' and current['destination_id'] == held:
                    continue
                if any(current[field] != expected for field, expected in candidates.items() if field in current):
                    return False
        return True

    def _unreviewed_fixture_data(self):
        """The synthetic executor knows only the data stores used in its rehearsal."""
        for table in tables(self.store) - _REVIEWED_FIXTURE_TABLES:
            if table.startswith('sqlite_'):
                continue
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', table):
                return True
            if self.store.query('SELECT 1 FROM "' + table + '" LIMIT 1'):
                return True
        return False

    def _scrub_notification_fields(self, customer_id):
        from account_erasure_notice import protected_destination
        protected=protected_destination(self.store,customer_id)
        names, counts = tables(self.store), {}
        # Display choices are personal settings too. This helper is reached
        # only by the existing guarded fixture rehearsal after both profile and
        # notification erasure decisions; it does not enable live deletion.
        for table, key in _REMOVED_PERSONAL_ROWS.items():
            if table in names:
                counts[table] = self.store.execute(f'DELETE FROM {table} WHERE {key}=?', (customer_id,)).rowcount
        for table, (key, candidates) in _SCRUBBED_PERSONAL_FIELDS.items():
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
