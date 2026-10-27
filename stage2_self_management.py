"""Opt-in basic inventory policy and non-destructive photo measurements.

There is no database discovery, automatic migration, billing or file removal.
Only an explicitly enabled, migrated local/staging runtime calls these helpers.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath

from flask import abort


SELF_INVENTORY_DDL = ('''CREATE TABLE IF NOT EXISTS self_inventory_items (
    merchandise_id INTEGER PRIMARY KEY REFERENCES merchandise(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id),
    submission_hash VARCHAR(64) NOT NULL UNIQUE,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
)''',)


def custody_ddl(dialect='sqlite'):
    if dialect not in ('sqlite', 'postgres'):
        raise ValueError('Unsupported dialect')
    pk = 'SERIAL PRIMARY KEY' if dialect == 'postgres' else 'INTEGER PRIMARY KEY AUTOINCREMENT'
    return (f'''CREATE TABLE IF NOT EXISTS inventory_intakes (
        id {pk}, user_id INTEGER NOT NULL REFERENCES users(id),
        kind VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'requested',
        expected_count INTEGER NOT NULL, received_count INTEGER,
        client_note TEXT NOT NULL DEFAULT '', shipping_instructions TEXT NOT NULL DEFAULT '',
        carrier VARCHAR(100) NOT NULL DEFAULT '', tracking_number VARCHAR(100) NOT NULL DEFAULT '',
        submission_hash VARCHAR(64) NOT NULL UNIQUE, version INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        received_at TIMESTAMP, received_by INTEGER REFERENCES users(id))''',
        '''CREATE TABLE IF NOT EXISTS inventory_intake_items (
        intake_id INTEGER NOT NULL REFERENCES inventory_intakes(id),
        merchandise_id INTEGER NOT NULL REFERENCES merchandise(id),
        registration_key VARCHAR(64) UNIQUE,
        PRIMARY KEY (intake_id, merchandise_id))''',
        f'''CREATE TABLE IF NOT EXISTS inventory_custody_events (
        id {pk}, merchandise_id INTEGER REFERENCES merchandise(id),
        intake_id INTEGER REFERENCES inventory_intakes(id), user_id INTEGER NOT NULL REFERENCES users(id),
        actor_id INTEGER NOT NULL REFERENCES users(id), event_type VARCHAR(40) NOT NULL,
        details TEXT NOT NULL DEFAULT '', created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)''',
        'CREATE INDEX IF NOT EXISTS idx_inventory_intake_user_status ON inventory_intakes(user_id,status)',
        'CREATE INDEX IF NOT EXISTS idx_inventory_intake_item ON inventory_intake_items(merchandise_id)')


PHOTO_RECORDS_DDL = '''CREATE TABLE IF NOT EXISTS stage2_photo_records (
    merchandise_id INTEGER NOT NULL REFERENCES merchandise(id) ON DELETE RESTRICT,
    path_hash VARCHAR(64) NOT NULL,
    photo_path TEXT NOT NULL,
    owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    byte_size BIGINT CHECK(byte_size IS NULL OR byte_size >= 0),
    measurement_status VARCHAR(16) NOT NULL CHECK(measurement_status IN ('measured','unknown')),
    first_recorded_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    removed_from_item_at TEXT,
    PRIMARY KEY(merchandise_id,path_hash),
    CHECK((measurement_status='measured' AND byte_size IS NOT NULL) OR
          (measurement_status='unknown' AND byte_size IS NULL))
)'''


def enabled(app):
    return app.config.get('STAGE2_INVENTORY_ENABLED') is True


def basic_self_management_allowed(user, app):
    return enabled(app) and bool(getattr(user, 'is_authenticated', False)) and user.role == 'user'


def _execute(cur, sql, args=(), postgres=False):
    cur.execute(sql.replace('?', '%s') if postgres else sql, args)


def _rows(cur):
    columns = [entry[0] for entry in cur.description]
    return [dict(row) if hasattr(row, 'keys') else dict(zip(columns, row)) for row in cur.fetchall()]


def require_schema(cur, postgres=False):
    required = {'business_items', 'business_item_links', 'business_item_profiles', 'business_registry_schema', 'stage2_photo_records'}
    if postgres:
        _execute(cur, 'SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema()')
    else:
        cur.execute("SELECT name AS table_name FROM sqlite_master WHERE type='table'")
    if not required <= {row['table_name'] for row in _rows(cur)}:
        abort(409, description='段階2の検証用データ構造が未準備です。移行確認後に再開してください。')


def registration_quota(cur, user_id, app, postgres=False, plan_code=None):
    """Separate active physical goods from monthly Kaika registrations.

    Unknown legacy links never get silently merged or counted as zero. The
    candidate total conservatively counts each unlinked row and marks it for
    review; explicitly recorded links count as one physical item.
    """
    require_schema(cur, postgres)
    _execute(cur, '''SELECT m.*, l.physical_item_id, l.link_status,
        p.physical_state AS registry_physical_state, p.sales_state AS registry_sales_state
        FROM merchandise m LEFT JOIN business_item_links l ON l.merchandise_id=m.id
        LEFT JOIN business_item_profiles p ON p.physical_item_id=l.physical_item_id
        WHERE m.user_id=? AND m.scope='user' AND m.custody_location='self' ''', (int(user_id),), postgres)
    candidates = _rows(cur)
    def active_item(row):
        # A recorded return reactivates the same physical item even while the
        # original sale fields remain as evidence. A legacy unknown profile,
        # however, cannot overwrite the historical sale's meaning by default.
        if row.get('registry_physical_state') not in (None, 'unknown'):
            return row.get('registry_sales_state') not in ('sold', 'shipped')
        return not row.get('sale_date') and str(row.get('item_status') or '').lower() != 'sold'
    active = [row for row in candidates if active_item(row)]
    identities = {('physical', row['physical_item_id']) if row.get('physical_item_id')
                  else ('legacy', row['id']) for row in active}
    unknown = sum(1 for row in active if not row.get('physical_item_id') or row.get('link_status') != 'verified')
    proposal = app.config.get('STAGE2_FREE_LIMIT_PROPOSAL_ENABLED') is True
    # Paid self-stock capacity is not decided. Everyone retains the proposed
    # free base in this experiment; payment never silently grants infinity.
    limit = 100 if proposal else None
    return {'active_items': len(identities), 'active_limit': limit,
            'unreconciled_items': unknown, 'needs_reconciliation': bool(unknown),
            'proposal_enabled': proposal, 'monthly_registration_limit': None,
            'can_add': bool(proposal and len(identities) < limit),
            'paid_extension_pending': bool(plan_code),
            'needs_confirmation': not proposal or bool(plan_code),
            'real_billing_enabled': False}


def enforce_new_registration(cur, user_id, app, postgres=False, plan_code=None):
    # SQLite caller already holds BEGIN IMMEDIATE. PostgreSQL locks by customer
    # before reading the total so two different submissions cannot both add 100.
    if postgres:
        digest = hashlib.sha256(('stage2-self-capacity:' + str(int(user_id))).encode()).digest()
        cur.execute('SELECT pg_advisory_xact_lock(%s)', (int.from_bytes(digest[:8], 'big', signed=True),))
    value = registration_quota(cur, user_id, app, postgres, plan_code)
    if not value['can_add']:
        message = ('検証中の自己管理枠は管理中100点です。過去の記録の閲覧・編集・販売記録は続けられます。'
                   if value['proposal_enabled'] else '自己管理の新規登録枠は未確定です。既存の記録は引き続き利用できます。')
        abort(409, description=message)
    return value


def reference_paths(item):
    paths = [item.get('photo_path')]
    additional = item.get('additional_photos') or []
    if isinstance(additional, str):
        try:
            additional = json.loads(additional)
        except (TypeError, ValueError):
            additional = []
    if isinstance(additional, list):
        paths.extend(path for path in additional if isinstance(path, str))
    return list(dict.fromkeys(path for path in paths if isinstance(path, str) and path))


def _measurement(app, reference):
    """Measure only a safe existing item reference; this grants no read access."""
    if '\\' in reference or '\x00' in reference:
        return None
    parts = PurePosixPath(reference).parts
    if len(parts) < 2 or parts[0] != 'uploads' or any(part.startswith('.') for part in parts):
        return None
    static = Path(app.static_folder).resolve()
    raw_uploads = static / 'uploads'
    if raw_uploads.is_symlink() or (hasattr(raw_uploads, 'is_junction') and raw_uploads.is_junction()):
        return None
    uploads = raw_uploads.resolve()
    if len(parts) == 3 and parts[1] == 'mobile':
        base = (uploads / '.mobile-private').resolve()
        candidate = base / parts[2]
    else:
        base = uploads
        candidate = static.joinpath(*parts)
    try:
        # Reject symlinks/junctions rather than following a private reference to
        # an arbitrary file, even when the resulting path happens to be inside.
        relative_parts = candidate.relative_to(uploads).parts
        current = uploads
        for part in relative_parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                return None
        resolved = candidate.resolve()
        if base not in resolved.parents or not resolved.is_file():
            return None
        if resolved.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp', '.gif'):
            return None
        return resolved.stat().st_size
    except (OSError, ValueError):
        return None


def record_item_photos(cur, merchandise_id, user_id, item, app, postgres=False):
    """Persist measurements in the caller's transaction, never remove originals."""
    if not enabled(app):
        return None
    require_schema(cur, postgres)
    now = datetime.now(timezone.utc).isoformat()
    paths = reference_paths(item)
    _execute(cur, '''UPDATE stage2_photo_records SET removed_from_item_at=?
        WHERE merchandise_id=? AND removed_from_item_at IS NULL''', (now, merchandise_id), postgres)
    known_bytes, unknown = 0, 0
    for path in paths:
        measured = _measurement(app, path)
        status = 'measured' if measured is not None else 'unknown'
        if measured is None:
            unknown += 1
        else:
            known_bytes += measured
        _execute(cur, '''INSERT INTO stage2_photo_records
            (merchandise_id,path_hash,photo_path,owner_user_id,byte_size,measurement_status,
             first_recorded_at,last_seen_at,removed_from_item_at)
            VALUES (?,?,?,?,?,?,?,?,NULL)
            ON CONFLICT(merchandise_id,path_hash) DO UPDATE SET
                owner_user_id=excluded.owner_user_id,byte_size=excluded.byte_size,
                measurement_status=excluded.measurement_status,last_seen_at=excluded.last_seen_at,
                removed_from_item_at=NULL''',
            (merchandise_id, hashlib.sha256(path.encode()).hexdigest(), path, user_id, measured, status, now, now), postgres)
    return {'photo_count': len(paths), 'known_bytes': known_bytes, 'unknown_size_count': unknown}


def photo_summary(cur, merchandise_id, user_id, postgres=False):
    _execute(cur, '''SELECT byte_size,measurement_status FROM stage2_photo_records
        WHERE merchandise_id=? AND owner_user_id=? AND removed_from_item_at IS NULL''',
        (merchandise_id, user_id), postgres)
    rows = _rows(cur)
    return {'photo_count': len(rows), 'known_bytes': sum(row['byte_size'] or 0 for row in rows),
            'unknown_size_count': sum(row['measurement_status'] != 'measured' for row in rows)}
