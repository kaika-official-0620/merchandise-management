"""Separate one-time completion-email consent, with an offline fixture sender.

Normal notifications remain revoked. Addresses are never copied into this
outbox; only the already verified destination reference is held temporarily.
No external provider, environment switch, scheduler or import-time I/O.
"""
import hashlib
import re
import uuid

from account_lifecycle import LifecycleService, tables
from account_erasure_restore import _fixture
from item_registry import RegistryError, _now

VERSION = 'account-erasure-notice-2026-10-26-v1'
_REFERENCE = re.compile(r'^[A-Za-z0-9_.:/-]{1,200}$')


def migration_statements(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    sql = [
        'CREATE TABLE IF NOT EXISTS account_erasure_notice_schema(version TEXT PRIMARY KEY)',
        '''CREATE TABLE IF NOT EXISTS account_erasure_notices(
            request_id TEXT PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES users(id),
            destination_id TEXT, address_hash TEXT, verified_at BIGINT,
            consented_at TEXT, consent_revoked_at TEXT, held_at TEXT,
            state TEXT NOT NULL CHECK(state IN ('no_consent','consented','held','blocked','sending','accepted','unknown','failed','cancelled')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts IN (0,1)),
            evidence_reference TEXT, outcome_code TEXT, updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS account_erasure_notice_events(
            event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, customer_id INTEGER NOT NULL,
            kind TEXT NOT NULL, recorded_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS account_erasure_notice_operations(
            source_key TEXT PRIMARY KEY, customer_id INTEGER NOT NULL, request_id TEXT NOT NULL,
            enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), recorded_at TEXT NOT NULL)''',
        "INSERT INTO account_erasure_notice_schema VALUES ('"+VERSION+"') ON CONFLICT(version) DO NOTHING",
    ]
    if dialect == 'sqlite':
        sql += [f"CREATE TRIGGER IF NOT EXISTS account_erasure_notice_events_no_{op.lower()} BEFORE {op} ON account_erasure_notice_events BEGIN SELECT RAISE(ABORT,'append_only'); END" for op in ('UPDATE','DELETE')]
        sql += [f"CREATE TRIGGER IF NOT EXISTS account_erasure_notice_operations_no_{op.lower()} BEFORE {op} ON account_erasure_notice_operations BEGIN SELECT RAISE(ABORT,'append_only'); END" for op in ('UPDATE','DELETE')]
    else:
        sql += ["CREATE OR REPLACE FUNCTION account_erasure_notice_immutable() RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'append_only'; END; $$ LANGUAGE plpgsql",
            'DROP TRIGGER IF EXISTS account_erasure_notice_immutable ON account_erasure_notice_events',
            'CREATE TRIGGER account_erasure_notice_immutable BEFORE UPDATE OR DELETE ON account_erasure_notice_events FOR EACH ROW EXECUTE FUNCTION account_erasure_notice_immutable()']
        sql += ['DROP TRIGGER IF EXISTS account_erasure_notice_operations_immutable ON account_erasure_notice_operations',
            'CREATE TRIGGER account_erasure_notice_operations_immutable BEFORE UPDATE OR DELETE ON account_erasure_notice_operations FOR EACH ROW EXECUTE FUNCTION account_erasure_notice_immutable()']
    return sql


def _active_request(store, customer_id, request_id):
    rows=store.query('SELECT * FROM account_deletion_requests WHERE customer_id=? AND request_id=?',(customer_id,request_id))
    if not rows or rows[0]['state']=='withdrawn':
        raise RegistryError('deletion_state_conflict',status=409)
    return rows[0]


def _destination(store, notice):
    if 'stage6_destinations' not in tables(store) or not notice.get('destination_id'):
        return None
    rows=store.query("SELECT * FROM stage6_destinations WHERE destination_id=? AND customer_id=? AND channel='email'",
        (notice['destination_id'],notice['customer_id']))
    if not rows:return None
    dest=rows[0]
    if not dest['address'] or dest['address_hash'] != notice['address_hash'] or dest['verified_at'] != notice['verified_at']:
        return None
    if hashlib.sha256(dest['address'].encode()).hexdigest() != notice['address_hash']:
        return None
    return dest


def protected_destination(store, customer_id):
    """Return the single held ID; never return or copy its actual email address."""
    if not {'account_erasure_notices','account_deletion_requests'} <= tables(store):return None
    rows=store.query('''SELECT n.* FROM account_erasure_notices n JOIN account_deletion_requests r
        ON r.request_id=n.request_id AND r.customer_id=n.customer_id
        WHERE n.customer_id=? AND r.state<>'withdrawn' AND n.consent_revoked_at IS NULL
        AND n.consented_at IS NOT NULL AND n.state IN ('consented','held','blocked','sending','unknown','failed')''',(customer_id,))
    if len(rows)!=1:return None
    notice=rows[0];dest=_destination(store,notice)
    if dest is None or notice['held_at'] is None and dest['revoked_at'] is not None:return None
    return dest['destination_id']


class CapturedCompletionTransport:
    """In-memory capture only. This exact class is required by fixture dispatch."""
    def __init__(self, outcome='accepted'):
        if outcome not in ('accepted','unknown','failed'):raise ValueError('Invalid fixture outcome')
        self.outcome=outcome
        self.messages=[]

    def deliver(self, *, recipient, sender, request_id):
        self.messages.append({'recipient':recipient,'sender':sender,'request_id':request_id,
            'subject':'【架空データ検証】削除完了のお知らせ',
            'body':'これはローカル検証用の削除完了通知です。実際のお客様への送信は行っていません。'})
        return self.outcome


class NoticeService:
    def __init__(self,store,clock=_now):self.store,self.clock=store,clock

    def ready(self):
        if 'account_erasure_notice_schema' not in tables(self.store) or not self.store.query('SELECT version FROM account_erasure_notice_schema WHERE version=?',(VERSION,)):
            raise RegistryError('account_erasure_notice_migration_required','完了連絡の確認準備が未完了です。',503)
        return True

    def _row(self,customer_id,request_id):
        rows=self.store.query('SELECT * FROM account_erasure_notices WHERE customer_id=? AND request_id=?',(customer_id,request_id))
        return rows[0] if rows else None

    def _event(self,customer_id,request_id,kind):
        self.store.execute('INSERT INTO account_erasure_notice_events VALUES(?,?,?,?,?)',
            (uuid.uuid4().hex,request_id,customer_id,kind,self.clock()))

    def get(self,actor,customer_id,request_id):
        self.ready();LifecycleService(self.store).authorize(actor,customer_id)
        _active_request(self.store,customer_id,request_id)
        row=self._row(customer_id,request_id)
        if not row:return {'request_id':request_id,'state':'no_consent','consented':False,'held':False,'attempts':0,'blockers':['no_completion_email_consent']}
        # No address, address hash or destination binding is exposed to the UI.
        return {'request_id':request_id,'state':row['state'],'consented':bool(row['consented_at'] and not row['consent_revoked_at']),
            'held':bool(row['held_at']),'attempts':row['attempts'],'outcome_code':row['outcome_code'],
            'blockers':(['no_completion_email_consent'] if row['consent_revoked_at'] else [])}

    def set_consent(self,actor,request_id,enabled,*,source_key=None):
        self.ready();api=LifecycleService(self.store);api.authorize(actor,actor.user_id)
        if type(enabled) is not bool or self.store.query('SELECT role FROM users WHERE id=?',(actor.user_id,))[0]['role']!='user':
            raise RegistryError('customer_required',status=403)
        if source_key is not None and (not isinstance(source_key,str) or not 1<=len(source_key)<=256):
            raise RegistryError('invalid_source_key',status=400)
        with self.store.atomic():
            api.lock(actor.user_id)
            request=_active_request(self.store,actor.user_id,request_id)
            if request['state'] not in ('requested','reviewing'):raise RegistryError('deletion_state_conflict',status=409)
            if source_key is not None:
                prior=self.store.query('SELECT * FROM account_erasure_notice_operations WHERE source_key=?',(source_key,))
                if prior:
                    if prior[0]['customer_id']!=actor.user_id or prior[0]['request_id']!=request_id or prior[0]['enabled']!=int(enabled):
                        raise RegistryError('completion_notice_operation_conflict',status=409)
                    return self.get(actor,actor.user_id,request_id)
            def remember():
                if source_key is not None:self.store.execute('INSERT INTO account_erasure_notice_operations VALUES(?,?,?,?,?)',
                    (source_key,actor.user_id,request_id,int(enabled),self.clock()))
            old=self._row(actor.user_id,request_id)
            if old and old['attempts']:raise RegistryError('completion_notice_already_attempted',status=409)
            if not enabled:
                if old:
                    self.store.execute("UPDATE account_erasure_notices SET state='cancelled',consent_revoked_at=?,updated_at=? WHERE request_id=?",(self.clock(),self.clock(),request_id))
                    if old['consent_revoked_at'] is None:self._event(actor.user_id,request_id,'consent_revoked')
                remember()
                return {'request_id':request_id,'state':'no_consent','consented':False}
            if 'stage6_destinations' not in tables(self.store):
                raise RegistryError('completion_email_not_verified','通知設定でメールアドレスを確認してから同意してください。',409)
            destinations=self.store.query("SELECT * FROM stage6_destinations WHERE customer_id=? AND channel='email' AND verified_at IS NOT NULL AND revoked_at IS NULL",(actor.user_id,))
            if len(destinations)!=1:
                raise RegistryError('completion_email_not_verified' if not destinations else 'completion_email_selection_required',
                    '本人確認済みのメール通知先を1件にしてください。',409)
            dest=destinations[0]
            if not dest['address'] or hashlib.sha256(dest['address'].encode()).hexdigest()!=dest['address_hash']:
                raise RegistryError('completion_email_not_verified',status=409)
            if old and old['consent_revoked_at'] is None and old['destination_id']==dest['destination_id'] and old['address_hash']==dest['address_hash'] and old['verified_at']==dest['verified_at']:
                remember()
                return self.get(actor,actor.user_id,request_id)
            self.store.execute('''INSERT INTO account_erasure_notices(request_id,customer_id,destination_id,address_hash,verified_at,consented_at,state,updated_at)
                VALUES(?,?,?,?,?,?,'consented',?) ON CONFLICT(request_id) DO UPDATE SET
                destination_id=excluded.destination_id,address_hash=excluded.address_hash,verified_at=excluded.verified_at,
                consented_at=excluded.consented_at,consent_revoked_at=NULL,held_at=NULL,state='consented',updated_at=excluded.updated_at''',
                (request_id,actor.user_id,dest['destination_id'],dest['address_hash'],dest['verified_at'],self.clock(),self.clock()))
            self._event(actor.user_id,request_id,'consented_once')
            remember()
            return self.get(actor,actor.user_id,request_id)

    def hold_for_erasure(self,actor,customer_id,request_id):
        self.ready();api=LifecycleService(self.store);api.authorize(actor,customer_id,staff_only=True)
        with self.store.atomic():
            api.lock(customer_id)
            _active_request(self.store,customer_id,request_id)
            row=self._row(customer_id,request_id)
            if not row or row['consent_revoked_at'] is not None:return None
            if row['state'] in ('accepted','cancelled'):return None
            if row['held_at'] is not None:return protected_destination(self.store,customer_id)
            dest=_destination(self.store,row)
            if dest is None or dest['revoked_at'] is not None:
                self.store.execute("UPDATE account_erasure_notices SET state='blocked',outcome_code='verified_destination_unavailable',updated_at=? WHERE request_id=?",(self.clock(),request_id))
                return None
            self.store.execute("UPDATE account_erasure_notices SET state='held',held_at=?,updated_at=? WHERE request_id=?",(self.clock(),self.clock(),request_id))
            self._event(customer_id,request_id,'held_for_completion_only')
            return dest['destination_id']

    def dispatch_fixture(self,actor,customer_id,request_id,*,fixture_database,completion_evidence,sender,transport):
        self.ready();_fixture(self.store,fixture_database)
        api=LifecycleService(self.store);api.authorize(actor,customer_id,staff_only=True)
        if type(transport) is not CapturedCompletionTransport:
            raise RegistryError('completion_notice_capture_only',status=403)
        if self.store.connection.in_transaction:
            raise RegistryError('completion_notice_committed_connection_required',status=409)
        with self.store.atomic():
            api.lock(customer_id)
            request=_active_request(self.store,customer_id,request_id)
            row=self._row(customer_id,request_id)
            if not row or row['consent_revoked_at'] is not None:
                raise RegistryError('completion_notice_consent_required',status=409)
            if row['state']=='accepted':return self.get(actor,customer_id,request_id)
            if row['attempts'] or row['state'] in ('sending','unknown','failed'):
                raise RegistryError('completion_notice_no_automatic_retry',status=409)
            if request['state']!='completed' or request['execution_scope']!='fixture_only':
                raise RegistryError('completion_notice_erasure_not_complete',status=409)
            if not isinstance(completion_evidence,str) or not _REFERENCE.fullmatch(completion_evidence):
                raise RegistryError('completion_notice_evidence_required',status=409)
            if not isinstance(sender,str) or not sender.endswith('@example.invalid') or '\n' in sender or '\r' in sender:
                raise RegistryError('completion_notice_fixture_sender_required',status=409)
            dest=_destination(self.store,row)
            if row['held_at'] is None or dest is None or dest['revoked_at'] is None:
                raise RegistryError('completion_notice_destination_unavailable',status=409)
            self.store.execute("UPDATE account_erasure_notices SET state='sending',attempts=1,evidence_reference=?,updated_at=? WHERE request_id=?",(completion_evidence,self.clock(),request_id))
            self._event(customer_id,request_id,'fixture_send_claimed')
            recipient=dest['address']
        # The exact in-memory transport never opens a socket. No provider adapters.
        try:
            outcome=transport.deliver(recipient=recipient,sender=sender,request_id=request_id)
        except Exception:
            outcome='unknown'
        if outcome not in ('accepted','unknown','failed'):outcome='unknown'
        with self.store.atomic():
            api.lock(customer_id)
            self.store.execute('UPDATE account_erasure_notices SET state=?,outcome_code=?,updated_at=? WHERE request_id=?',
                (outcome,'captured_'+outcome,self.clock(),request_id))
            self._event(customer_id,request_id,'fixture_'+outcome)
            if outcome=='accepted':
                # Release the temporary address only after capture acceptance.
                self.store.execute("UPDATE stage6_destinations SET address='',address_hash=? WHERE destination_id=? AND customer_id=? AND address_hash=?",
                    ('erased-notice-'+uuid.uuid4().hex,row['destination_id'],customer_id,row['address_hash']))
            return self.get(actor,customer_id,request_id)
