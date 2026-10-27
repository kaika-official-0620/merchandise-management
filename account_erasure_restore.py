"""Signed deletion-state replay for quarantined, synthetic recovery fixtures only.

No import I/O, runtime registration, production switch, network or quarantine
release. A manifest's expected digest must come from a separate trusted current
registry; a valid signature alone cannot establish freshness.
"""
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import tempfile
import uuid

from werkzeug.security import generate_password_hash
from account_lifecycle import LifecycleService, CLOSED, columns, tables
from item_registry import RegistryError, _now

PURPOSE = 'account-deletion-recovery-v1'
MARKER = {'synthetic_only': True, 'purpose': 'account-deletion-rehearsal'}
_HEX = re.compile(r'^[a-f0-9]{32}$')
_SHA = re.compile(r'^[a-f0-9]{64}$')
_STATES = {'requested', 'reviewing', 'withdrawn', *CLOSED}


def _fail(code='deletion_restore_verification_failed'):
    raise RegistryError(code, '削除状態の復旧確認が未完了のため、復旧データの利用を停止しています。', 409)


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False)


def manifest_digest(envelope):
    return hashlib.sha256(_json(envelope).encode()).hexdigest()


def _key(key):
    if not isinstance(key, bytes) or len(key) < 32:
        _fail('deletion_restore_signing_key_required')


def _fixture(store, fixture_database):
    if store.dialect != 'sqlite':
        _fail('fixture_only')
    path = Path(fixture_database).absolute()
    temp = Path(tempfile.gettempdir()).resolve()
    resolved = path.resolve()
    if temp not in resolved.parents or not resolved.name.startswith('account-fixture-'):
        _fail('fixture_only')
    # Do not accept symlinks/junctions even when the final target is under temp.
    for part in (path, *path.parents):
        if part.is_symlink():
            _fail('fixture_only')
        if part.exists() and getattr(part.lstat(), 'st_file_attributes', 0) & 0x400:
            _fail('fixture_only')
        if part == temp:
            break
    databases = store.query('PRAGMA database_list')
    if any(row['name'] not in ('main','temp') for row in databases) or store.query('SELECT 1 FROM sqlite_temp_master'):
        _fail('fixture_only')
    main = [row for row in databases if row['name']=='main']
    if len(main)!=1:_fail('fixture_only')
    actual = main[0]['file']
    if not actual or Path(actual).resolve() != resolved:
        _fail('fixture_only')
    if resolved.lstat().st_nlink != 1:
        _fail('fixture_only')
    marker = path.with_suffix('.fixture.json')
    try:
        if marker.is_symlink() or getattr(marker.lstat(), 'st_file_attributes', 0) & 0x400 or marker.lstat().st_nlink != 1:
            _fail('fixture_only')
        if json.loads(marker.read_text(encoding='utf-8')) != MARKER:
            _fail('fixture_only')
    except (OSError, ValueError, TypeError):
        _fail('fixture_only')
    return resolved


def prepare_fixture_bindings(store, actor, *, fixture_database, environment_id):
    """Explicitly seed stable random customer bindings BEFORE taking a backup.

    Never run this on the restored copy to invent missing bindings. Original and
    backup must share these identifiers; numeric user IDs alone are insufficient.
    """
    _fixture(store, fixture_database)
    LifecycleService(store).authorize(actor, staff_only=True)
    if not isinstance(environment_id, str) or not _HEX.fullmatch(environment_id):
        _fail('deletion_restore_environment_required')
    with store.atomic():
        store.execute('''CREATE TABLE IF NOT EXISTS account_restore_environment(
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), environment_id TEXT NOT NULL UNIQUE)''')
        existing = store.query('SELECT environment_id FROM account_restore_environment WHERE singleton=1')
        if existing and existing[0]['environment_id'] != environment_id:
            _fail('deletion_restore_environment_mismatch')
        store.execute('INSERT INTO account_restore_environment VALUES(1,?) ON CONFLICT(singleton) DO NOTHING', (environment_id,))
        store.execute('''CREATE TABLE IF NOT EXISTS account_restore_bindings(
            customer_id INTEGER PRIMARY KEY REFERENCES users(id), binding TEXT UNIQUE NOT NULL)''')
        for row in store.query("SELECT id FROM users WHERE role='user'"):
            store.execute('INSERT INTO account_restore_bindings VALUES(?,?) ON CONFLICT(customer_id) DO NOTHING', (row['id'], uuid.uuid4().hex))


def _bindings(store, environment_id):
    if not {'account_restore_environment', 'account_restore_bindings'} <= tables(store):
        _fail('deletion_restore_bindings_missing')
    rows = store.query('SELECT environment_id FROM account_restore_environment WHERE singleton=1')
    if len(rows) != 1 or rows[0]['environment_id'] != environment_id:
        _fail('deletion_restore_environment_mismatch')
    bound = {row['customer_id']: row['binding'] for row in store.query('SELECT * FROM account_restore_bindings')}
    customers = {row['id'] for row in store.query("SELECT id FROM users WHERE role='user'")}
    if set(bound) != customers or any(not isinstance(v, str) or not _HEX.fullmatch(v) for v in bound.values()):
        _fail('deletion_restore_bindings_incomplete')
    return bound


def export_fixture_manifest(store, actor, *, fixture_database, environment_id, generation, signing_key):
    """Read the current authoritative deletion state into a signed PII-free plan."""
    _fixture(store, fixture_database)
    service = LifecycleService(store)
    service.authorize(actor, staff_only=True)
    _key(signing_key)
    if type(generation) is not int or generation < 1:
        _fail('deletion_restore_generation_required')
    with store.atomic():
        service.lock(actor.user_id)
        bound = _bindings(store, environment_id)
        entries = []
        for customer_id, binding in sorted(bound.items()):
            rows = store.query('SELECT request_id,state,requested_at,version FROM account_deletion_requests WHERE customer_id=?', (customer_id,))
            entries.append({'customer_id': customer_id, 'binding': binding, 'request': rows[0] if rows else None})
        payload = {'purpose': PURPOSE, 'environment_id': environment_id, 'generation': generation,
                   'entries': entries}
        envelope = {'payload': payload, 'signature': hmac.new(signing_key, _json(payload).encode(), hashlib.sha256).hexdigest()}
        return envelope


def _verify(store, envelope, environment_id, expected_manifest_sha256, signing_key):
    _key(signing_key)
    try:
        if not isinstance(expected_manifest_sha256, str) or not _SHA.fullmatch(expected_manifest_sha256):
            _fail('deletion_restore_latest_digest_required')
        if not isinstance(envelope, dict) or set(envelope) != {'payload', 'signature'}:
            _fail()
        if not hmac.compare_digest(manifest_digest(envelope), expected_manifest_sha256):
            _fail('deletion_restore_latest_manifest_mismatch')
        payload, signature = envelope['payload'], envelope['signature']
        if not isinstance(signature, str) or not _SHA.fullmatch(signature) or not hmac.compare_digest(
                signature, hmac.new(signing_key, _json(payload).encode(), hashlib.sha256).hexdigest()):
            _fail('deletion_restore_signature_invalid')
        if not isinstance(payload, dict) or set(payload) != {'purpose', 'environment_id', 'generation', 'entries'}:
            _fail()
        if payload['purpose'] != PURPOSE or payload['environment_id'] != environment_id:
            _fail('deletion_restore_environment_mismatch')
        if type(payload['generation']) is not int or payload['generation'] < 1 or not isinstance(payload['entries'], list):
            _fail()
        bound = _bindings(store, environment_id)
        seen = set()
        for entry in payload['entries']:
            if not isinstance(entry, dict) or set(entry) != {'customer_id', 'binding', 'request'}:
                _fail()
            customer_id = entry['customer_id']
            if type(customer_id) is not int or customer_id in seen or bound.get(customer_id) != entry['binding']:
                _fail('deletion_restore_customer_mismatch')
            seen.add(customer_id)
            request = entry['request']
            if request is not None:
                if not isinstance(request, dict) or set(request) != {'request_id', 'state', 'requested_at', 'version'}:
                    _fail()
                if not isinstance(request['request_id'], str) or not _HEX.fullmatch(request['request_id']):
                    _fail()
                if request['state'] not in _STATES or type(request['version']) is not int or request['version'] < 1:
                    _fail()
                if not isinstance(request['requested_at'], str) or not 1 <= len(request['requested_at']) <= 64:
                    _fail()
            local = store.query('SELECT request_id,state,version FROM account_deletion_requests WHERE customer_id=?', (customer_id,))
            if local:
                old = local[0]
                if request is None or old['version'] > request['version']:
                    _fail('deletion_restore_state_conflict')
                if old['state'] in CLOSED and request['state'] not in CLOSED:
                    _fail('deletion_restore_state_conflict')
                if old['version'] == request['version'] and (old['request_id'] != request['request_id'] or
                        old['state'] != request['state'] and not (old['state'] in CLOSED and request['state'] in CLOSED)):
                    _fail('deletion_restore_state_conflict')
        if seen != set(bound):
            _fail('deletion_restore_bindings_incomplete')
        return payload
    except RegistryError:
        raise
    except (TypeError, ValueError, KeyError, OverflowError):
        _fail()


def replay_fixture_manifest(store, actor, *, fixture_database, envelope, environment_id,
                            expected_manifest_sha256, signing_key, clock=_now):
    """Revoke restored access and suppress work, leaving the restore quarantined.

    No files, amounts, ownership or external records are deleted. Even a prior
    completed request becomes erasure_pending until all restored copies have
    been independently assessed and erased. No activation method is supplied.
    """
    _fixture(store, fixture_database)
    service = LifecycleService(store, clock)
    service.authorize(actor, staff_only=True)
    if 'kaika_recovery_quarantine' not in tables(store):
        _fail('deletion_restore_quarantine_required')
    with store.atomic():
        service.lock(actor.user_id)
        payload = _verify(store, envelope, environment_id, expected_manifest_sha256, signing_key)
        store.execute('''CREATE TABLE IF NOT EXISTS account_restore_replays(
            manifest_sha256 TEXT PRIMARY KEY, generation INTEGER NOT NULL, recorded_at TEXT NOT NULL)''')
        already = bool(store.query('SELECT 1 FROM account_restore_replays WHERE manifest_sha256=?', (expected_manifest_sha256,)))
        newer = store.query('SELECT MAX(generation) AS n FROM account_restore_replays')[0]['n']
        if newer is not None and newer > payload['generation']:
            _fail('deletion_restore_generation_stale')
        if store.query('SELECT 1 FROM account_restore_replays WHERE generation=? AND manifest_sha256<>?',
                       (payload['generation'], expected_manifest_sha256)):
            _fail('deletion_restore_generation_conflict')
        stopped, suppressed = [], []
        for entry in payload['entries']:
            request, customer_id = entry['request'], entry['customer_id']
            if request is None:
                continue
            closed = request['state'] in CLOSED
            state = 'erasure_pending' if closed else request['state']
            service.lock(customer_id)
            store.execute('''INSERT INTO account_deletion_requests(customer_id,request_id,state,requested_at,updated_at,execution_scope,version)
                VALUES(?,?,?,?,?,'fixture_only',?) ON CONFLICT(customer_id) DO UPDATE SET
                request_id=excluded.request_id,state=excluded.state,requested_at=excluded.requested_at,updated_at=excluded.updated_at,
                review_json='{}',customer_notice='',execution_scope='fixture_only',version=excluded.version''',
                (customer_id, request['request_id'], state, request['requested_at'], clock(), request['version']))
            # A withdrawn request does not revive jobs cancelled during its
            # lifetime. The old backup cannot prove which jobs are safe to send.
            service.suppress(customer_id, closed=closed)
            suppressed.append(customer_id)
            if closed:
                names = tables(store)
                if 'mobile_sessions' in names:
                    store.execute('UPDATE mobile_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL', (clock(), customer_id))
                if 'push_devices' in names:
                    store.execute('UPDATE push_devices SET enabled=0 WHERE user_id=?', (customer_id,))
                fields = {k: '' for k in ('email', 'display_name', 'phone', 'postal_code', 'address', 'shipping_addresses', 'last_name', 'first_name') if k in columns(store, 'users')}
                if 'username' in columns(store, 'users'):
                    fields['username'] = 'closed-' + entry['binding']
                if 'password_hash' in columns(store, 'users'):
                    fields['password_hash'] = generate_password_hash(secrets.token_urlsafe(64))
                if fields:
                    store.execute('UPDATE users SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?', (*fields.values(), customer_id))
                stopped.append(customer_id)
            if not already:
                service.event(actor, {'customer_id': customer_id, 'request_id': request['request_id']},
                              'fixture_restore_reapplied', {'manifest_sha256': expected_manifest_sha256,
                                                           'erasure_complete': False, 'quarantine_retained': True})
        store.execute('INSERT INTO account_restore_replays VALUES(?,?,?) ON CONFLICT(manifest_sha256) DO NOTHING',
                      (expected_manifest_sha256, payload['generation'], clock()))
        return {'fixture_only': True, 'already_recorded': already, 'closed_accounts': len(stopped),
                'suppressed_accounts': len(suppressed), 'quarantine_retained': True,
                'erasure_complete': False, 'remaining_review': ['photos', 'indirect_personal_data', 'providers', 'backups', 'retention'],
                'manifest_sha256': expected_manifest_sha256}
