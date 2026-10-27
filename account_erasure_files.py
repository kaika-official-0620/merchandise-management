"""File erasure rehearsal, confined to a marked disposable SQLite fixture.

No production enable flag, HTTP route, network, database mutation or import I/O.
This deletes only explicitly reviewed fictional photos. It does not complete an
account deletion; attachments/evidence, providers and backups remain separate.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import uuid

from item_registry import RegistryError

MARKER = {'synthetic_only': True, 'purpose': 'account-file-erasure-rehearsal'}
MARKER_NAME = 'account-erasure-files.fixture.json'
VERSION = 'account-file-erasure-2026-10-26-v1'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def _reject(code='file_cleanup_fixture_only'):
    raise RegistryError(code, '専用の架空ファイル削除検証環境、所有関係、確認済み計画を確認してください。', 409)


def _plain(path, *, missing=False):
    """Do not traverse symlinks, Windows junctions/reparse points or hardlinks."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        _reject('file_cleanup_path_missing')
    if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
        _reject('file_cleanup_unsafe_path')
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        _reject('file_cleanup_shared_file')
    if not stat.S_ISREG(info.st_mode) and not stat.S_ISDIR(info.st_mode):
        _reject('file_cleanup_unsafe_path')
    return info


def _fixture(store, fixture_root):
    if store.dialect != 'sqlite':
        _reject()
    root = Path(os.path.abspath(fixture_root))
    temp = Path(tempfile.gettempdir()).resolve()
    if temp not in root.parents or not root.name.startswith('account-erasure-files-'):
        _reject()
    current = temp
    for part in root.relative_to(temp).parts:
        current /= part
        if not stat.S_ISDIR(_plain(current).st_mode):
            _reject()
    marker = root / MARKER_NAME
    _plain(marker)
    try:
        if json.loads(marker.read_text(encoding='utf-8')) != MARKER:
            _reject()
    except (ValueError, OSError):
        _reject()
    databases = store.query('PRAGMA database_list')
    main = [row for row in databases if row['name'] == 'main']
    if len(main) != 1 or not main[0]['file'] or any(row['name'] not in ('main', 'temp') for row in databases):
        _reject()
    if store.query('SELECT name FROM sqlite_temp_master'):
        _reject('file_cleanup_temporary_schema_unverified')
    database = Path(os.path.abspath(main[0]['file']))
    if database.parent != root or not database.name.startswith('account-fixture-'):
        _reject()
    _plain(database)
    return root


def _path(root, reference, *, missing=False):
    if not isinstance(reference, str) or '\\' in reference or '%' in reference or ':' in reference or '\x00' in reference:
        _reject('file_cleanup_unsafe_path')
    parts = PurePosixPath(reference).parts
    if len(parts) < 2 or parts[0] != 'uploads' or '/'.join(parts) != reference or any(part.startswith('.') for part in parts):
        _reject('file_cleanup_unsafe_path')
    if PurePosixPath(reference).suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
        _reject('file_cleanup_attachment_unreviewed')
    # Same public-reference/private-storage mapping as stage2_self_management.
    actual = ('static', 'uploads', '.mobile-private', parts[2]) if len(parts) == 3 and parts[1] == 'mobile' else ('static', *parts)
    current = root
    for index, part in enumerate(actual):
        current /= part
        info = _plain(current, missing=missing and index == len(actual)-1)
        if index < len(actual)-1 and not stat.S_ISDIR(info.st_mode):
            _reject('file_cleanup_unsafe_path')
        if index == len(actual)-1 and info is not None and not stat.S_ISREG(info.st_mode):
            _reject('file_cleanup_unsafe_path')
    return current


def _hash_file(path):
    _plain(path)
    hasher = hashlib.sha256()
    size = 0
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            hasher.update(block)
            size += len(block)
    return hasher.hexdigest(), size


def _schema(store):
    names = [row['name'] for row in store.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'")]
    if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name) for name in names):
        _reject('file_cleanup_schema_unreviewed')
    return {name: {row['name'] for row in store.query('PRAGMA table_info("'+name+'")')} for name in names}


def _references(store, customer_id, root):
    """Read all fixture reference holders, including retired photos and JSON.

    Unknown reference locations are holds, never permission to remove a file.
    Actual values are used only to identify refs and are not returned in reports.
    """
    schema = _schema(store)
    required = {
        'merchandise': {'id', 'user_id', 'photo_path', 'additional_photos'},
        'stage2_photo_records': {'merchandise_id', 'owner_user_id', 'photo_path'},
        'business_item_links': {'merchandise_id', 'physical_item_id', 'link_status'},
        'business_item_assignments': {'physical_item_id', 'role', 'party_id', 'ended_at'},
        'business_parties': {'party_id', 'kind', 'user_id'},
    }
    if any(not fields <= schema.get(name, set()) for name, fields in required.items()):
        _reject('file_cleanup_ownership_schema_unverified')
    owners = {}
    for row in store.query("SELECT l.merchandise_id, p.kind, p.user_id FROM business_item_links l LEFT JOIN business_item_assignments a ON a.physical_item_id=l.physical_item_id AND a.role='owner' AND a.ended_at IS NULL LEFT JOIN business_parties p ON p.party_id=a.party_id WHERE l.link_status='verified'"):
        owners.setdefault(row['merchandise_id'], []).append(row['user_id'] if row['kind'] == 'customer' else None)
    refs, candidates, scan_issues = {}, set(), []

    def add(reference, owner, item_id, source):
        if not isinstance(reference, str) or not reference:
            return
        record = {'source': source, 'item': item_id, 'record_owner': owner,
                  'current_owners': sorted(owners.get(item_id, []), key=lambda x: str(x))}
        refs.setdefault(reference, []).append(record)
        if owner == customer_id or customer_id in owners.get(item_id, []):
            candidates.add(reference)

    for row in store.query('SELECT id,user_id,photo_path,additional_photos FROM merchandise'):
        add(row['photo_path'], row['user_id'], row['id'], 'merchandise.photo_path')
        try:
            extras = json.loads(row['additional_photos'] or '[]')
            if not isinstance(extras, list) or any(not isinstance(value, str) for value in extras):
                raise ValueError()
        except (ValueError, TypeError):
            scan_issues.append('malformed_additional_photos')
            extras = []
        for reference in extras:
            add(reference, row['user_id'], row['id'], 'merchandise.additional_photos')
    for row in store.query('SELECT merchandise_id,owner_user_id,photo_path FROM stage2_photo_records'):
        add(row['photo_path'], row['owner_user_id'], row['merchandise_id'], 'stage2_photo_records.photo_path')

    # Retained snapshots, listing/catalog JSON and unknown attachment columns
    # can contain photo references. A textual reference anywhere else is a hold.
    handled = {('merchandise', 'photo_path'), ('merchandise', 'additional_photos'), ('stage2_photo_records', 'photo_path')}
    for table, fields in schema.items():
        scan_fields = sorted(fields - {field for name, field in handled if name == table})
        if not scan_fields:
            continue
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', field) for field in scan_fields):
            _reject('file_cleanup_schema_unreviewed')
        for row in store.query('SELECT ' + ','.join('"'+field+'"' for field in scan_fields) + ' FROM "'+table+'"'):
            for field, value in row.items():
                if isinstance(value, (bytes, bytearray)):
                    scan_issues.append('opaque_binary_reference_holder')
                    continue
                if not isinstance(value, str):
                    continue
                comparison = value.casefold() if os.name == 'nt' else value
                for reference in candidates:
                    needle = reference.casefold() if os.name == 'nt' else reference
                    if needle in comparison or needle.replace('/', '\\/') in comparison:
                        refs[reference].append({'source':table+'.'+field, 'unreviewed_retained_reference':True})
    # Distinct textual references may name one Windows file (case or 8.3 alias).
    # Combine their owners before permission checks; never rewrite the path to
    # a normalized spelling that might name another file on a different FS.
    parents = {reference:reference for reference in refs}
    def group(reference):
        while parents[reference] != reference:
            reference = parents[reference]
        return reference
    buckets = {}
    for reference in refs:
        keys = [('spelling', os.path.normcase(reference))]
        try:
            info = _path(root, reference).stat()
            keys.append(('file', info.st_dev, info.st_ino))
        except (OSError, RegistryError):
            pass
        for key in keys:
            if key in buckets:
                parents[group(reference)] = group(buckets[key])
            else:
                buckets[key] = reference
    groups = {}
    for reference in refs:
        groups.setdefault(group(reference), []).append(reference)
    combined, selected = {}, []
    for aliases in groups.values():
        records = []
        for alias in sorted(aliases):
            records.extend({**record, 'reference_key_sha256':hashlib.sha256(alias.encode()).hexdigest()}
                           for record in refs[alias])
        for alias in aliases:
            combined[alias] = records
        eligible = sorted(set(aliases) & candidates)
        if eligible:
            selected.append(eligible[0])
    return combined, sorted(selected), sorted(set(scan_issues))


def _journal_dir(root):
    directory = root / '.file-cleanup'
    if not directory.exists():
        directory.mkdir()
    if not stat.S_ISDIR(_plain(directory).st_mode):
        _reject('file_cleanup_unsafe_path')
    return directory


def _save(path, value):
    _plain(path.parent)
    _plain(path, missing=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            _plain(temporary)
            temporary.unlink()


@contextmanager
def _execution_lock(root):
    path = root / '.file-cleanup.lock'
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _reject('file_cleanup_busy_or_interrupted')
    os.close(descriptor)
    try:
        yield
    finally:
        _plain(path)
        path.unlink()


@contextmanager
def _database_read_lock(store):
    # Fixture SQLite only: prevent another writer transferring the item between
    # the ownership check and file removal. There are no database writes here.
    if store.connection.in_transaction:
        _reject('file_cleanup_open_transaction')
    store.execute('BEGIN IMMEDIATE')
    try:
        yield
    finally:
        store.execute('ROLLBACK')


def plan_file_cleanup(store, customer_id, *, fixture_root, decisions=None):
    """Prepare and persist a reviewable *fictional-only* file plan.

    decisions[path] = {'action':'delete', 'synthetic_only':True,
                       'reference':'fictional-policy-case-1'}
    No decision means a retention-policy hold; no retention period is invented.
    """
    root = _fixture(store, fixture_root)
    if isinstance(customer_id, bool) or not isinstance(customer_id, int) or customer_id <= 0:
        _reject('file_cleanup_customer_required')
    decisions = decisions or {}
    if not isinstance(decisions, dict):
        _reject('file_cleanup_decision_required')
    refs, candidates, issues = _references(store, customer_id, root)
    entries = []
    for reference in candidates:
        records = refs[reference]
        entry = {'entry_id': hashlib.sha256(reference.encode()).hexdigest(), 'relative_path':reference,
                 'reference_sha256':_digest(records), 'reasons':list(issues), 'sha256':None, 'byte_size':None}
        if any(record.get('unreviewed_retained_reference') for record in records):
            entry['reasons'].append('retained_or_unknown_reference')
        if any(record.get('record_owner') != customer_id or record.get('current_owners') != [customer_id]
               for record in records if not record.get('unreviewed_retained_reference')):
            entry['reasons'].append('shared_or_unverified_owner')
        # One file reused by two different goods is retained even for one owner.
        if len({record.get('item') for record in records if 'item' in record}) != 1:
            entry['reasons'].append('shared_between_items')
        decision = decisions.get(reference)
        if (not isinstance(decision, dict) or decision.get('action') != 'delete'
                or decision.get('synthetic_only') is not True or not isinstance(decision.get('reference'), str)
                or not 1 <= len(decision['reference'].strip()) <= 200):
            entry['reasons'].append('retention_policy_pending')
        entry['decision_sha256'] = _digest(decision)
        try:
            path = _path(root, reference)
            entry['sha256'], entry['byte_size'] = _hash_file(path)
        except RegistryError as error:
            entry['reasons'].append(error.code)
        entry['reasons'] = sorted(set(entry['reasons']))
        entry['status'] = 'blocked' if entry['reasons'] else 'ready'
        entries.append(entry)
    plan = {'version':VERSION, 'plan_id':uuid.uuid4().hex, 'customer_id':customer_id,
            'synthetic_only':True, 'entries':entries, 'reference_scan_issues':issues,
            'scope':'reviewed fictional local photos only', 'account_deletion_complete':False,
            'outside_scope':['cloud storage', 'external providers', 'backups', 'unreviewed attachments', 'database records']}
    _save(_journal_dir(root) / (plan['plan_id']+'.plan.json'), plan)
    return plan


def execute_fixture_cleanup(store, plan, *, fixture_root):
    """Verify a persisted plan, then remove only its unchanged fixture photos.

    Intent is durable before unlink. After interruption an absent file is
    recorded as absent-after-intent, not as a witnessed deletion. Reappeared or
    changed files are never deleted by retrying an old plan. A stale lock after
    process death requires explicit fixture-only operator inspection/removal.
    """
    root = _fixture(store, fixture_root)
    if not isinstance(plan, dict) or not re.fullmatch(r'[0-9a-f]{32}', str(plan.get('plan_id', ''))):
        _reject('file_cleanup_plan_invalid')
    with _execution_lock(root), _database_read_lock(store):
        journal = _journal_dir(root)
        plan_path = journal / (plan['plan_id']+'.plan.json')
        _plain(plan_path)
        try:
            saved = json.loads(plan_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            _reject('file_cleanup_plan_invalid')
        if _digest(saved) != _digest(plan) or plan.get('version') != VERSION or plan.get('synthetic_only') is not True:
            _reject('file_cleanup_plan_changed')
        state_path = journal / (plan['plan_id']+'.result.json')
        _plain(state_path, missing=True)
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {'plan_sha256':_digest(plan), 'entries':{}}
        if state.get('plan_sha256') != _digest(plan):
            _reject('file_cleanup_result_changed')
        refs, _, issues = _references(store, plan['customer_id'], root)
        for entry in plan['entries']:
            key, reference = entry['entry_id'], entry['relative_path']
            prior = state['entries'].get(key, {})
            status = 'blocked'
            reason = None
            if entry['status'] != 'ready':
                reason = 'plan_entry_held'
            elif issues or _digest(refs.get(reference, [])) != entry['reference_sha256']:
                reason = 'references_changed'
            else:
                try:
                    path = _path(root, reference, missing=True)
                    exists = path.exists()
                    if prior.get('removed_once') or prior.get('status') in ('deleted', 'absent_after_intent'):
                        if exists:
                            reason = 'file_reappeared'
                        else:
                            status = prior.get('removal_status', prior['status'])
                    elif not exists:
                        if prior.get('status') == 'intent':
                            status = 'absent_after_intent'
                        else:
                            reason = 'missing_before_execution'
                    elif _hash_file(path) != (entry['sha256'], entry['byte_size']):
                        reason = 'file_changed'
                    else:
                        state['entries'][key] = {'status':'intent'}
                        _save(state_path, state)
                        # Validate the full path again immediately before unlink.
                        _fixture(store, fixture_root)
                        _path(root, reference).unlink()
                        status = 'deleted'
                except (OSError, RegistryError) as error:
                    reason = error.code if isinstance(error, RegistryError) else 'filesystem_error'
            removal_status = status if status in ('deleted', 'absent_after_intent') else prior.get('removal_status')
            state['entries'][key] = {'status':status, 'reason':reason,
                                    'removed_once':bool(removal_status), 'removal_status':removal_status}
            _save(state_path, state)
        report = {'plan_id':plan['plan_id'], 'synthetic_only':True,
                  'deleted':sum(row['status']=='deleted' for row in state['entries'].values()),
                  'absent_after_intent':sum(row['status']=='absent_after_intent' for row in state['entries'].values()),
                  'blocked':sum(row['status']=='blocked' for row in state['entries'].values()),
                  'entries':state['entries'], 'database_records_deleted':0,
                  'account_deletion_complete':False, 'real_provider_calls':0}
        return report
