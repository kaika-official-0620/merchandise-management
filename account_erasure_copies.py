"""Offline evidence for disposable backup files and fictional provider records.

The catalogue belongs only to a marked SQLite rehearsal. It inventories the
copies created here. It does not discover real backups, connect to a provider,
release a restore from quarantine, or register an application route.
"""
from contextlib import contextmanager
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from account_erasure_guard import assert_fixture_database
from account_lifecycle import CLOSED, LifecycleService
from item_registry import RegistryError, _now


TASKS = frozenset(('backups', 'providers'))
SERVICES = frozenset(('email', 'line', 'push', 'payments'))
IDENTIFIER = re.compile(r'[0-9a-f]{32}')
MAX_BACKUP_BYTES = 64 * 1024 * 1024


def _reject(code='erasure_copy_evidence_required', status=409):
    raise RegistryError(code, '検証用コピーの範囲、処理結果、保持方針を確認してください。', status)


def _plain(path, *, directory=False, missing=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        _reject()
    matches_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1
    if not matches_type or getattr(info, 'st_file_attributes', 0) & 0x400:
        _reject('erasure_copy_scope_mismatch')
    return info


def _digest(value):
    return hashlib.sha256(value).hexdigest()


class FixtureCopies:
    def __init__(self, store, fixture_database, clock=_now):
        self.store = store
        self.clock = clock
        self.fixture_database = assert_fixture_database(store, fixture_database)
        self.directory = self.fixture_database.with_suffix('.copies')
        self.catalogue = self.directory / 'catalogue.sqlite3'

    @contextmanager
    def _catalogue(self, *, initialize=False, read_only=False):
        assert_fixture_database(self.store, self.fixture_database)
        if initialize:
            self.directory.mkdir(exist_ok=True, mode=0o700)
        _plain(self.directory, directory=True)
        existing = _plain(self.catalogue, missing=initialize)
        for suffix in ('-journal', '-wal', '-shm'):
            _plain(self.catalogue.with_name(self.catalogue.name + suffix), missing=True)
        if existing is None:
            descriptor = os.open(self.catalogue, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(descriptor)
        if read_only:
            db = sqlite3.connect(self.catalogue.as_uri() + '?mode=ro', uri=True, timeout=3)
        else:
            db = sqlite3.connect(self.catalogue, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            if not read_only:
                db.execute('PRAGMA secure_delete=ON')
            db.execute('BEGIN' if read_only else 'BEGIN IMMEDIATE')
            if initialize and existing is None:
                db.execute('''CREATE TABLE copies(copy_id TEXT PRIMARY KEY, task TEXT NOT NULL,
                    customer_id INTEGER NOT NULL, service TEXT NOT NULL, checksum TEXT NOT NULL,
                    shared INTEGER NOT NULL, payload TEXT, state TEXT NOT NULL, erasing_request TEXT)''')
                db.execute('''CREATE TABLE receipts(request_id TEXT NOT NULL, customer_id INTEGER NOT NULL,
                    task TEXT NOT NULL, evidence_digest TEXT NOT NULL, PRIMARY KEY(request_id, customer_id, task))''')
                info = self.fixture_database.stat()
                db.execute('CREATE TABLE binding(source_device INTEGER NOT NULL, source_inode TEXT NOT NULL)')
                db.execute('INSERT INTO binding VALUES (?,?)', (info.st_dev, str(info.st_ino)))
            info = self.fixture_database.stat()
            bindings = [tuple(row) for row in db.execute('SELECT source_device,source_inode FROM binding')]
            if bindings != [(info.st_dev, str(info.st_ino))]:
                _reject('erasure_copy_scope_mismatch')
            self._verify_catalogue_paths(db)
            yield db
            db.commit()
        except sqlite3.DatabaseError:
            _reject()
        finally:
            db.close()

    def _archive(self, copy_id):
        if not isinstance(copy_id, str) or not IDENTIFIER.fullmatch(copy_id):
            _reject('erasure_copy_scope_mismatch')
        return self.directory / ('backup-' + copy_id + '.copy')

    def _verify_catalogue_paths(self, db):
        expected = {self._archive(row['copy_id']).name for row in db.execute("SELECT copy_id FROM copies WHERE task='backups'")}
        expected.update((self.catalogue.name, self.catalogue.name + '-journal'))
        if any(entry.name not in expected for entry in self.directory.iterdir()):
            _reject()

    def initialize(self, actor):
        """Start an explicitly empty inventory before taking synthetic snapshots."""
        LifecycleService(self.store, self.clock).authorize(actor, staff_only=True)
        with self._catalogue(initialize=True):
            pass

    def _seed(self, actor, customer_id, task, service, payload, shared):
        lifecycle = LifecycleService(self.store, self.clock)
        lifecycle.authorize(actor, customer_id, staff_only=True)
        with self.store.atomic():
            lifecycle.lock(customer_id)
            row = lifecycle.get(actor, customer_id)
            if row and row['state'] in CLOSED:
                _reject('erasure_copy_scope_mismatch')
            copy_id = uuid.uuid4().hex
            with self._catalogue(initialize=True) as db:
                checksum = _digest(payload if task == 'backups' else payload.encode('utf-8'))
                db.execute('INSERT INTO copies VALUES (?,?,?,?,?,?,?,\'active\',NULL)',
                           (copy_id, task, customer_id, service, checksum, int(shared), payload if task == 'providers' else None))
                if task == 'backups':
                    # Exclusive creation in an owned temporary directory. An interrupted
                    # registration leaves an unknown artifact and blocks reconciliation.
                    descriptor = os.open(self._archive(copy_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(payload)
            return copy_id

    def seed_backup(self, actor, customer_id, *, payload, shared=False):
        """Store a real snapshot of supplied fictional bytes in the fixture."""
        if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_BACKUP_BYTES or type(shared) is not bool:
            _reject('erasure_copy_scope_mismatch')
        return self._seed(actor, customer_id, 'backups', '', payload, shared)

    def seed_provider(self, actor, customer_id, *, service, payload):
        """Create provider-side data only in the offline fixture catalogue."""
        if not isinstance(service, str) or service not in SERVICES or not isinstance(payload, str) or not 0 < len(payload) <= 4000:
            _reject('erasure_copy_scope_mismatch')
        return self._seed(actor, customer_id, 'providers', service, payload, False)

    def _policy(self, actor, customer_id, request_id, task, action, reference, retain_until):
        lifecycle = LifecycleService(self.store, self.clock)
        lifecycle.authorize(actor, customer_id, staff_only=True)
        request = lifecycle.get(actor, customer_id)
        if (not request or request['request_id'] != request_id or request['execution_scope'] != 'fixture_only'
                or request['state'] not in ('erasure_pending', 'completed')):
            _reject('erasure_copy_scope_mismatch')
        if (not isinstance(task, str) or task not in TASKS or action not in ('erase', 'retain', 'not_applicable')
                or not isinstance(reference, str) or not 0 < len(reference) <= 200):
            _reject('erasure_copy_policy_invalid')
        if action == 'retain':
            try:
                expiry = date.fromisoformat(retain_until)
                if expiry.isoformat() != retain_until or expiry <= date.fromisoformat(self.clock()[:10]):
                    raise ValueError()
            except (ValueError, TypeError):
                _reject('erasure_copy_policy_invalid')
        elif retain_until is not None:
            _reject('erasure_copy_policy_invalid')
        decisions = self.store.query('''SELECT d.action,d.reference,d.retain_until
            FROM account_erasure_plans p JOIN account_erasure_decisions d ON d.plan_id=p.plan_id
            WHERE p.customer_id=? AND p.request_id=? AND d.task_key=?''', (customer_id, request_id, task))
        if decisions != [{'action': action, 'reference': reference, 'retain_until': retain_until}]:
            _reject('erasure_copy_policy_invalid')
        return request['state']

    def _copies(self, db, customer_id, task):
        return list(db.execute('SELECT * FROM copies WHERE customer_id=? AND task=? ORDER BY copy_id', (customer_id, task)))

    def _check_copy(self, row, *, expect_present):
        if row['task'] == 'providers':
            payload = row['payload']
            if expect_present:
                if not isinstance(payload, str) or _digest(payload.encode('utf-8')) != row['checksum']:
                    _reject()
            elif payload is not None:
                _reject()
            return
        archive = self._archive(row['copy_id'])
        info = _plain(archive, missing=not expect_present)
        if expect_present:
            if not 0 < info.st_size <= MAX_BACKUP_BYTES or _digest(archive.read_bytes()) != row['checksum']:
                _reject()
        elif info is not None:
            _reject()

    def _evidence(self, rows, customer_id, request_id, task, action, reference, retain_until):
        if (action == 'not_applicable' and rows) or (action in ('erase', 'retain') and not rows):
            _reject()
        for row in rows:
            if action == 'erase' and (row['state'] != 'erased' or row['erasing_request'] != request_id):
                _reject()
            if action == 'retain' and row['state'] != 'active':
                _reject()
            self._check_copy(row, expect_present=action == 'retain')
        proof = {'action': action, 'reference': reference, 'retain_until': retain_until}
        facts = (customer_id, request_id, task, proof, [(row['copy_id'], row['checksum'], row['service'], row['shared']) for row in rows])
        return {**proof, 'digest': _digest(json.dumps(facts, sort_keys=True, separators=(',', ':')).encode('utf-8'))}

    def reconcile(self, actor, customer_id, request_id, *, task, action, reference, retain_until=None):
        """Carry out an explicitly recorded fixture decision, with repeatable evidence."""
        lifecycle = LifecycleService(self.store, self.clock)
        lifecycle.authorize(actor, customer_id, staff_only=True)
        with self.store.atomic():
            lifecycle.lock(customer_id)
            state = self._policy(actor, customer_id, request_id, task, action, reference, retain_until)
            if state == 'completed':
                return self.verify(actor, customer_id, request_id, task=task, action=action,
                                   reference=reference, retain_until=retain_until)
            if action == 'erase':
                self._record_erasure_intent(customer_id, request_id, task)
            with self._catalogue() as db:
                if action == 'erase':
                    self._erase_intended(db, customer_id, request_id, task)
                evidence = self._evidence(self._copies(db, customer_id, task), customer_id, request_id, task,
                                          action, reference, retain_until)
                db.execute('''INSERT INTO receipts VALUES(?,?,?,?) ON CONFLICT(request_id,customer_id,task)
                    DO UPDATE SET evidence_digest=excluded.evidence_digest''', (request_id, customer_id, task, evidence['digest']))
                return evidence

    def _record_erasure_intent(self, customer_id, request_id, task):
        with self._catalogue() as db:
            rows = self._copies(db, customer_id, task)
            if not rows or any(row['shared'] for row in rows):
                _reject()
            for row in rows:
                if row['state'] == 'active':
                    self._check_copy(row, expect_present=True)
                elif row['erasing_request'] != request_id or row['state'] not in ('erasing', 'erased'):
                    _reject()
            # Commit intent before unlinking. A retry may find a missing artifact
            # only when this same request had already started the removal.
            db.execute('''UPDATE copies SET state='erasing',erasing_request=?
                WHERE customer_id=? AND task=? AND state='active' ''', (request_id, customer_id, task))

    def _erase_intended(self, db, customer_id, request_id, task):
        for row in self._copies(db, customer_id, task):
            if row['state'] == 'erased':
                continue
            if row['state'] != 'erasing' or row['erasing_request'] != request_id or row['shared']:
                _reject()
            if task == 'backups':
                archive = self._archive(row['copy_id'])
                if _plain(archive, missing=True) is not None:
                    self._check_copy(row, expect_present=True)
                    archive.unlink()
            else:
                self._check_copy(row, expect_present=True)
            db.execute("UPDATE copies SET payload=NULL,state='erased' WHERE copy_id=?", (row['copy_id'],))

    def verify(self, actor, customer_id, request_id, *, task, action, reference, retain_until=None):
        self._policy(actor, customer_id, request_id, task, action, reference, retain_until)
        with self._catalogue(read_only=True) as db:
            evidence = self._evidence(self._copies(db, customer_id, task), customer_id, request_id, task,
                                      action, reference, retain_until)
            receipt = db.execute('SELECT evidence_digest FROM receipts WHERE request_id=? AND customer_id=? AND task=?',
                                 (request_id, customer_id, task)).fetchone()
            if receipt is None or receipt['evidence_digest'] != evidence['digest']:
                _reject()
            return evidence


def verify_fixture_copies(store, actor, customer_id, request_id, *, fixture_database, backup, providers):
    """Verify backed-up and provider copies using the current saved decisions."""
    bound = assert_fixture_database(store, fixture_database)
    LifecycleService(store).authorize(actor, customer_id, staff_only=True)
    decisions = store.query('''SELECT d.task_key,d.action,d.reference,d.retain_until
        FROM account_erasure_plans p JOIN account_erasure_decisions d ON d.plan_id=p.plan_id
        WHERE p.customer_id=? AND p.request_id=? AND d.task_key IN ('backups','providers')''', (customer_id, request_id))
    sources = {'backups': backup, 'providers': providers}
    result = {}
    for decision in decisions:
        task = decision.pop('task_key')
        source = sources[task]
        if source is None:
            _reject()
        if type(source) is not FixtureCopies or source.fixture_database != bound or source.store is not store:
            _reject('erasure_copy_scope_mismatch')
        result[task] = source.verify(actor, customer_id, request_id, task=task, **decision)
    if set(result) != TASKS:
        _reject()
    return result
