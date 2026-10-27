"""Authorization-aware individual-item repository. Never creates fees or funds.

The caller owns an explicitly migrated connection and its final transaction.
There are no startup hooks, environment discovery, HTTP calls or automatic DDL.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import uuid

from item_registry_schema import SCHEMA_VERSION


class RegistryError(ValueError):
    def __init__(self, code, message=None, status=400):
        self.code = code
        self.status = status
        super().__init__(message or code)


@dataclass(frozen=True)
class Actor:
    user_id: int
    staff: bool = False


EVENT_KINDS = frozenset(('expected', 'received', 'inspection_completed', 'authenticity_checked',
    'photographed', 'registration_completed', 'approval_pending', 'listed', 'sold', 'shipped',
    'returned', 'return_shipped', 'return_received', 'hold', 'release_hold', 'sale_cancelled'))
WORK_KINDS = frozenset(('receipt', 'inspection', 'authenticity_check', 'photography', 'registration',
    'repricing', 'packing', 'dispatch', 'support', 'return', 'other'))
IMPORTED_FIELDS = frozenset(('purchase_date', 'store_name', 'purchase_price', 'payment_method',
    'listing_price', 'is_listed', 'listing_date', 'sale_date', 'sale_price', 'shipping_cost',
    'sales_destination', 'commission', 'is_shipped', 'custody_location', 'received_at',
    'registration_completed_at', 'shipped_at', 'legacy_id', 'external_key', 'brand_name',
    'item_condition', 'storage_start_date'))


def _text(value, name, maximum=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise RegistryError('invalid_' + name)
    return value.strip()


def _time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            raise RegistryError('timezone_required') from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise RegistryError('timezone_required')
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _json(value):
    def historic_value(item):
        # PostgreSQL DATE/TIMESTAMP values are retained as ISO strings. Do not
        # infer a timezone for a legacy naive datetime or convert unknown to 0.
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError('Unsupported historical value type')
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False, default=historic_value)


class Registry:
    def __init__(self, connection, dialect='sqlite', *, clock=None):
        if dialect not in ('sqlite', 'postgres'):
            raise ValueError('Unsupported dialect')
        self.connection = connection
        self.dialect = dialect
        self.clock = clock or _now

    def _actual_time(self, value):
        when = _time(value)
        if when > _time(self.clock()):
            raise RegistryError('registry_time_in_future',
                '実際に行った日時を記録してください。未来の予定は実績として保存できません。')
        return when

    def _observation_recorded_at(self, item_id):
        """Preserve append order when the system clock repeats or moves back.

        record_event holds the individual-item row lock before this is called.
        Business occurred_at remains the user's actual timestamp. The separate
        recording timestamp is a stable ordering token, never work duration.
        Existing ambiguous historical records are not rewritten here.
        """
        now = _time(_now())
        rows = self.query('SELECT MAX(recorded_at) AS latest FROM business_item_observations WHERE physical_item_id=?', (item_id,))
        latest = rows[0]['latest'] if rows else None
        if latest and _time(latest) >= now:
            return _time(datetime.fromisoformat(_time(latest)) + timedelta(microseconds=1))
        return now

    def execute(self, sql, params=()):
        cursor = self.connection.cursor()
        cursor.execute(sql.replace('?', '%s') if self.dialect == 'postgres' else sql, params)
        return cursor

    def query(self, sql, params=()):
        cursor = self.execute(sql, params)
        columns = [col[0] for col in cursor.description]
        return [dict(row) if hasattr(row, 'keys') else dict(zip(columns, row)) for row in cursor.fetchall()]

    @contextmanager
    def atomic(self):
        name = 'registry_' + uuid.uuid4().hex
        self.execute('SAVEPOINT ' + name)
        try:
            yield
            self.execute('RELEASE SAVEPOINT ' + name)
        except Exception:
            self.execute('ROLLBACK TO SAVEPOINT ' + name)
            self.execute('RELEASE SAVEPOINT ' + name)
            raise

    def ready(self):
        try:
            return bool(self.query('SELECT version FROM business_registry_schema WHERE version=?', (SCHEMA_VERSION,)))
        except Exception:
            return False

    def _require_ready(self):
        if not self.ready():
            raise RegistryError('registry_migration_required', '検証用の明示移行が必要です。', 409)

    def actor(self, actor, staff_required=False):
        if not isinstance(actor, Actor) or type(actor.user_id) is not int:
            raise RegistryError('invalid_actor', status=403)
        rows = self.query('SELECT * FROM users WHERE id=?', (actor.user_id,))
        if not rows:
            raise RegistryError('unknown_actor', status=403)
        user = rows[0]
        permitted = user.get('role') == 'owner'
        if user.get('role') == 'admin':
            permissions = user.get('admin_permissions')
            if not permissions:
                permitted = True
            else:
                try:
                    decoded = json.loads(permissions) if isinstance(permissions, str) else permissions
                    permitted = isinstance(decoded, list) and 'merchandise' in decoded
                except (ValueError, TypeError):
                    permitted = False
        # A staff flag can narrow an authenticated actor's powers, never grant them.
        if actor.staff and not permitted:
            raise RegistryError('staff_permission_required', status=403)
        if staff_required and not (actor.staff and permitted):
            raise RegistryError('staff_permission_required', status=403)
        return actor.staff and permitted

    def _user_exists(self, user_id):
        if type(user_id) is not int or not self.query('SELECT id FROM users WHERE id=?', (user_id,)):
            raise RegistryError('unknown_customer')

    def _customer_exists(self, user_id):
        if type(user_id) is not int:
            raise RegistryError('customer_owner_required', status=409)
        rows = self.query('SELECT role FROM users WHERE id=?', (user_id,))
        if not rows or rows[0]['role'] != 'user':
            raise RegistryError('customer_owner_required',
                'この段階では顧客所有の個品だけを照合できます。開花自己在庫は別の売買仕様で扱います。', 409)

    def _party(self, kind, user_id=None):
        if kind not in ('customer', 'kaika', 'staff', 'external'):
            raise RegistryError('invalid_party_kind')
        if kind == 'customer':
            self._customer_exists(user_id)
        elif kind != 'kaika':
            self._user_exists(user_id)
        party_id = 'kaika' if kind == 'kaika' else kind + ':' + str(user_id)
        self.execute('INSERT INTO business_parties(party_id,kind,user_id,reference) VALUES (?,?,?,?) ON CONFLICT(party_id) DO NOTHING',
            (party_id, kind, None if kind == 'kaika' else user_id, kind + ':recorded'))
        return party_id

    def _assign(self, item_id, role, party_id, when, evidence, event_key):
        self.execute('INSERT INTO business_item_assignments(assignment_id,physical_item_id,role,party_id,started_at,evidence_reference,source_event_id) VALUES (?,?,?,?,?,?,?)',
            ('as_' + uuid.uuid4().hex, item_id, role, party_id, when, evidence, event_key))

    def _roles(self, item_id):
        return {row['role']: row for row in self.query('''SELECT a.*,p.kind,p.user_id FROM business_item_assignments a
            JOIN business_parties p ON p.party_id=a.party_id WHERE a.physical_item_id=? AND a.ended_at IS NULL''', (item_id,))}

    def _profile(self, actor, item_id, lock=False):
        self._require_ready()
        staff = self.actor(actor)
        suffix = ' FOR UPDATE' if lock and self.dialect == 'postgres' else ''
        rows = self.query('SELECT p.*,i.label_code,i.identity_status,i.identity_evidence FROM business_item_profiles p JOIN business_items i ON i.physical_item_id=p.physical_item_id WHERE p.physical_item_id=?' + suffix, (item_id,))
        if not rows:
            raise RegistryError('item_not_found', status=404)
        profile = rows[0]
        roles = self._roles(item_id)
        if not staff and roles.get('owner', {}).get('user_id') != actor.user_id:
            raise RegistryError('item_not_found', status=404)
        return profile, roles, staff

    def _owner(self, roles):
        owner = roles.get('owner', {})
        if owner.get('user_id') is None:
            raise RegistryError('customer_owner_required', status=409)
        return owner['user_id']

    def operation(self, source_key, actor):
        self._require_ready()
        self.actor(actor)
        rows = self.query('SELECT * FROM business_registry_operations WHERE source_key=?', (source_key,))
        if not rows:
            return None
        if rows[0]['actor_user_id'] != actor.user_id:
            raise RegistryError('operation_not_owned', status=403)
        return json.loads(rows[0]['result_json'])

    def _request(self, actor, source_key, payload):
        _text(source_key, 'source_key', 200)
        digest = hashlib.sha256(_json(payload).encode()).hexdigest()
        rows = self.query('SELECT * FROM business_registry_operations WHERE source_key=?', (source_key,))
        if rows:
            if rows[0]['actor_user_id'] != actor.user_id:
                raise RegistryError('operation_not_owned', status=403)
            if rows[0]['request_hash'] != digest:
                raise RegistryError('idempotency_conflict', status=409)
            return digest, json.loads(rows[0]['result_json'])
        return digest, None

    def _remember(self, actor, source_key, digest, result):
        self.execute('INSERT INTO business_registry_operations(source_key,actor_user_id,request_hash,result_json) VALUES (?,?,?,?)',
            (source_key, actor.user_id, digest, _json(result)))

    def create_item(self, actor, title, owner_user_id=None, custodian='self', *, occurred_at,
                    evidence_reference, source_key, origin='manual', imported_fields=None, external_label=None):
        self._require_ready()
        staff = self.actor(actor, staff_required=origin != 'manual')
        owner_user_id = actor.user_id if owner_user_id is None else owner_user_id
        self._customer_exists(owner_user_id)
        if not staff and owner_user_id != actor.user_id:
            raise RegistryError('cannot_assign_other_customer', status=403)
        if custodian not in ('self', 'kaika', 'unknown') or origin not in ('manual', 'legacy_import', 'legacy_link'):
            raise RegistryError('invalid_item_origin')
        # Customers cannot declare that Kaika has actually received goods.
        if not staff and custodian == 'kaika':
            raise RegistryError('staff_receipt_required', status=403)
        title = _text(title, 'title', 300)
        evidence_reference = _text(evidence_reference, 'evidence')
        when = self._actual_time(occurred_at)
        fields = dict(imported_fields or {})
        if set(fields) - IMPORTED_FIELDS:
            raise RegistryError('unsupported_import_field')
        if external_label is not None:
            external_label = _text(external_label, 'external_label', 200)
        payload = dict(action='create', title=title, owner=owner_user_id, custodian=custodian,
            when=when, evidence=evidence_reference, origin=origin, fields=fields, label=external_label)
        with self.atomic():
            digest, result = self._request(actor, source_key, payload)
            if result:
                return self.detail(actor, result['physical_item_id'])
            item_id = 'ki_' + uuid.uuid4().hex
            # The operator will decide physical labels later. Only an internal
            # UUID is issued; do not assign or replace a warehouse label.
            label = None
            self.execute('INSERT INTO business_items(physical_item_id,label_code,identity_status) VALUES (?,?,?)', (item_id, label, 'unresolved'))
            physical = 'with_customer' if custodian == 'self' else 'unknown'
            self.execute('''INSERT INTO business_item_profiles(physical_item_id,title,origin,created_at,initial_customer_id,
                external_label,imported_fields_json,physical_state) VALUES (?,?,?,?,?,?,?,?)''',
                (item_id, title, origin, when, owner_user_id, external_label, _json(fields), physical))
            owner_party = self._party('customer', owner_user_id)
            self._assign(item_id, 'owner', owner_party, when, evidence_reference, source_key + ':owner')
            if custodian != 'unknown':
                party = owner_party if custodian == 'self' else self._party('kaika')
                self._assign(item_id, 'custodian', party, when, evidence_reference, source_key + ':custodian')
            result = {'physical_item_id': item_id}
            self._remember(actor, source_key, digest, result)
            return self.detail(actor, item_id)

    def attach_legacy(self, actor, merchandise_id, *, physical_item_id=None, evidence_reference, occurred_at, source_key):
        """Link explicitly selected rows; never infer duplicates or create work."""
        self._require_ready()
        staff = self.actor(actor)
        if type(merchandise_id) is not int:
            raise RegistryError('invalid_legacy_id')
        legacy_rows = self.query('SELECT * FROM merchandise WHERE id=?', (merchandise_id,))
        if not legacy_rows:
            raise RegistryError('legacy_not_found', status=404)
        legacy = legacy_rows[0]
        if not staff and legacy.get('user_id') != actor.user_id:
            raise RegistryError('legacy_not_found', status=404)
        self._customer_exists(legacy.get('user_id'))
        if 'scope' in legacy and legacy['scope'] != 'user':
            raise RegistryError('customer_inventory_scope_required',
                '開花自己在庫または区分未確認の記録は、この顧客在庫の照合へ追加できません。', 409)
        when = self._actual_time(occurred_at)
        evidence_reference = _text(evidence_reference, 'evidence')
        with self.atomic():
            digest, prior = self._request(actor, source_key, dict(action='attach', legacy_id=merchandise_id, item=physical_item_id, when=when, evidence=evidence_reference))
            if prior:
                return self.detail(actor, prior['physical_item_id'])
            links = self.query('SELECT * FROM business_item_links WHERE merchandise_id=?', (merchandise_id,))
            if links:
                if physical_item_id and physical_item_id != links[0]['physical_item_id']:
                    raise RegistryError('legacy_already_linked', status=409)
                item_id = links[0]['physical_item_id']
                self._profile(actor, item_id)
            else:
                if physical_item_id is None:
                    # New legacy enrollment is allowed for the row's customer;
                    # origin cannot be used to bypass create_item staff checks.
                    if not staff:
                        raise RegistryError('staff_reconciliation_required', status=403)
                    safe_fields = {key: legacy.get(key) for key in IMPORTED_FIELDS if key in legacy}
                    item = self.create_item(actor, legacy.get('product_name') or '名称未確認', owner_user_id=legacy['user_id'],
                        custodian='unknown', occurred_at=when, evidence_reference=evidence_reference,
                        source_key=source_key + ':create', origin='legacy_link', imported_fields=safe_fields)
                    item_id = item['physical_item_id']
                else:
                    item_id = physical_item_id
                    _, roles, _ = self._profile(actor, item_id, lock=True)
                    if self._owner(roles) != legacy.get('user_id'):
                        raise RegistryError('legacy_owner_mismatch', status=409)
                self.execute('INSERT INTO business_item_links(merchandise_id,physical_item_id,link_status,review_reference) VALUES (?,?,?,?)',
                    (merchandise_id, item_id, 'unresolved', evidence_reference))
                snapshot = {key: legacy.get(key) for key in ('id', 'user_id', 'product_name', *sorted(IMPORTED_FIELDS)) if key in legacy}
                self.execute('INSERT INTO business_legacy_snapshots(snapshot_id,merchandise_id,physical_item_id,customer_id,captured_at,snapshot_json,source_key) VALUES (?,?,?,?,?,?,?)',
                    ('ls_' + uuid.uuid4().hex, merchandise_id, item_id, legacy['user_id'], when, _json(snapshot), source_key))
            self._remember(actor, source_key, digest, {'physical_item_id': item_id})
            return self.detail(actor, item_id)

    def verify_identity(self, actor, item_id, *, evidence_reference, occurred_at, source_key, external_label=None):
        self.actor(actor, staff_required=True)
        evidence_reference = _text(evidence_reference, 'evidence')
        when = self._actual_time(occurred_at)
        if external_label is not None:
            external_label = _text(external_label, 'external_label', 200)
        with self.atomic():
            profile, roles, _ = self._profile(actor, item_id, lock=True)
            digest, prior = self._request(actor, source_key, dict(action='verify', item=item_id, evidence=evidence_reference, when=when, label=external_label))
            if not prior:
                self.execute('''INSERT INTO business_item_reviews(review_id,physical_item_id,customer_id,actor_user_id,kind,
                    recorded_at,evidence_reference,before_json,after_json,source_key) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    ('rv_' + uuid.uuid4().hex, item_id, self._owner(roles), actor.user_id, 'identity', _now(), evidence_reference,
                     _json({'identity_status': profile['identity_status'], 'identity_evidence': profile['identity_evidence'], 'external_label': profile['external_label']}),
                     _json({'identity_status': 'verified', 'identity_evidence': evidence_reference, 'external_label': external_label}), source_key))
                self.execute("UPDATE business_items SET identity_status='verified',identity_evidence=? WHERE physical_item_id=?", (evidence_reference, item_id))
                self.execute("UPDATE business_item_links SET link_status='verified',review_reference=? WHERE physical_item_id=?", (evidence_reference, item_id))
                if external_label is not None:
                    self.execute('UPDATE business_item_profiles SET external_label=? WHERE physical_item_id=?', (external_label, item_id))
                self._remember(actor, source_key, digest, {'physical_item_id': item_id})
            return self.detail(actor, item_id)

    def change_assignment(self, actor, item_id, role, party_kind, user_id=None, *, occurred_at, evidence_reference, source_key):
        self.actor(actor, staff_required=True)
        if role not in ('owner', 'custodian', 'sales_operator', 'settlement_party'):
            raise RegistryError('invalid_role')
        if role == 'owner' and party_kind != 'customer':
            raise RegistryError('customer_owner_required')
        when = self._actual_time(occurred_at)
        evidence_reference = _text(evidence_reference, 'evidence')
        with self.atomic():
            profile, roles, _ = self._profile(actor, item_id, lock=True)
            digest, prior = self._request(actor, source_key, dict(action='assignment', item=item_id, role=role, kind=party_kind, user=user_id, when=when, evidence=evidence_reference))
            if prior:
                return self.detail(actor, item_id)
            current = roles.get(role)
            if current and _time(current['started_at']) > when:
                raise RegistryError('assignment_time_before_current')
            if profile['last_event_at'] and when < _time(profile['last_event_at']):
                raise RegistryError('assignment_before_last_event')
            party = self._party(party_kind, user_id)
            if current and current['party_id'] == party:
                raise RegistryError('assignment_unchanged')
            if current:
                self.execute('UPDATE business_item_assignments SET ended_at=? WHERE assignment_id=?', (when, current['assignment_id']))
            self._assign(item_id, role, party, when, evidence_reference, source_key)
            if role == 'owner':
                # Contract/deadline/private history do not silently follow a sale.
                self.execute("UPDATE business_item_profiles SET deadline_status='unknown',deadline_at=NULL,deadline_evidence=NULL,deadline_customer_id=NULL WHERE physical_item_id=?", (item_id,))
            self._remember(actor, source_key, digest, {'physical_item_id': item_id})
            return self.detail(actor, item_id)

    def set_deadline(self, actor, item_id, *, deadline_at, evidence_reference, source_key):
        self.actor(actor, staff_required=True)
        evidence_reference = _text(evidence_reference, 'evidence')
        deadline = _time(deadline_at)
        with self.atomic():
            profile, roles, _ = self._profile(actor, item_id, lock=True)
            digest, prior = self._request(actor, source_key, dict(action='deadline', item=item_id, deadline=deadline, evidence=evidence_reference, customer=self._owner(roles)))
            if not prior:
                self.execute('''INSERT INTO business_item_reviews(review_id,physical_item_id,customer_id,actor_user_id,kind,
                    recorded_at,evidence_reference,before_json,after_json,source_key) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    ('rv_' + uuid.uuid4().hex, item_id, self._owner(roles), actor.user_id, 'deadline', _now(), evidence_reference,
                     _json({'deadline_at': profile['deadline_at'], 'deadline_status': profile['deadline_status'], 'deadline_evidence': profile['deadline_evidence']}),
                     _json({'deadline_at': deadline, 'deadline_status': 'confirmed', 'deadline_evidence': evidence_reference}), source_key))
                self.execute("UPDATE business_item_profiles SET deadline_at=?,deadline_status='confirmed',deadline_evidence=?,deadline_customer_id=? WHERE physical_item_id=?", (deadline, evidence_reference, self._owner(roles), item_id))
                self._remember(actor, source_key, digest, {'physical_item_id': item_id})
            return self.detail(actor, item_id)

    def _operable(self, actor, item_id):
        profile, roles, staff = self._profile(actor, item_id, lock=True)
        custodian = roles.get('custodian', {})
        if not staff and not (custodian.get('kind') == 'customer' and custodian.get('user_id') == actor.user_id):
            raise RegistryError('staff_work_required', status=403)
        return profile, roles, staff

    def update_title(self, actor, item_id, title, *, occurred_at, evidence_reference, source_key):
        title = _text(title, 'title', 300)
        evidence_reference = _text(evidence_reference, 'evidence')
        when = self._actual_time(occurred_at)
        with self.atomic():
            profile, roles, _ = self._operable(actor, item_id)
            digest, prior = self._request(actor, source_key, dict(action='title', item=item_id, title=title,
                when=when, evidence=evidence_reference))
            if not prior:
                if profile['held']:
                    raise RegistryError('item_on_hold', status=409)
                self.execute('''INSERT INTO business_item_reviews(review_id,physical_item_id,customer_id,actor_user_id,kind,
                    recorded_at,evidence_reference,before_json,after_json,source_key) VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    ('rv_' + uuid.uuid4().hex, item_id, self._owner(roles), actor.user_id, 'title', _now(), evidence_reference,
                     _json({'title': profile['title']}), _json({'title': title, 'occurred_at': when}), source_key))
                self.execute('UPDATE business_item_profiles SET title=? WHERE physical_item_id=?', (title, item_id))
                self._remember(actor, source_key, digest, {'physical_item_id': item_id})
            return self.detail(actor, item_id)

    def record_event(self, actor, item_id, kind, occurred_at, source_key, evidence_reference, details=None):
        if kind not in EVENT_KINDS:
            raise RegistryError('invalid_event_kind')
        when = self._actual_time(occurred_at)
        evidence_reference = _text(evidence_reference, 'evidence')
        details = dict(details or {})
        if set(details) - {'note', 'discrepancy', 'authenticity_result', 'return_reason', 'tracking_reference', 'target_custodian'}:
            raise RegistryError('unsupported_event_detail')
        if any(value is not None and (not isinstance(value, str) or len(value) > 2000) for value in details.values()):
            raise RegistryError('invalid_event_detail')
        with self.atomic():
            profile, roles, staff = self._operable(actor, item_id)
            digest, prior = self._request(actor, source_key, dict(action='event', item=item_id, kind=kind, when=when, evidence=evidence_reference, details=details))
            if prior:
                return self.detail(actor, item_id)
            if when < _time(profile['created_at']) or (profile['last_event_at'] and when < _time(profile['last_event_at'])):
                raise RegistryError('event_time_before_current')
            if profile['held'] and kind not in ('release_hold', 'hold', 'return_shipped', 'return_received'):
                raise RegistryError('item_on_hold', status=409)
            changes = {'last_event_at': when}
            physical, sale, workflow = profile['physical_state'], profile['sales_state'], profile['workflow_state']
            if kind == 'expected':
                if physical not in ('unknown', 'with_customer', 'returned_to_customer'):
                    raise RegistryError('invalid_state_transition')
                changes.update(physical_state='expected')
            elif kind == 'received':
                if not staff:
                    raise RegistryError('staff_receipt_required', status=403)
                if physical not in ('unknown', 'expected', 'with_customer', 'returned_to_customer') or sale in ('sold', 'shipped'):
                    raise RegistryError('invalid_state_transition')
                current = roles.get('custodian')
                if current and current['party_id'] != 'kaika':
                    self.execute('UPDATE business_item_assignments SET ended_at=? WHERE assignment_id=?', (when, current['assignment_id']))
                if not current or current['party_id'] != 'kaika':
                    self._assign(item_id, 'custodian', self._party('kaika'), when, evidence_reference, source_key + ':custodian')
                changes.update(physical_state='received', received_at=when, workflow_state='received')
            elif kind in ('inspection_completed', 'authenticity_checked', 'photographed', 'registration_completed', 'approval_pending', 'listed'):
                if physical not in ('received', 'with_customer') or sale in ('sold', 'shipped'):
                    raise RegistryError('invalid_state_transition')
                expected = {'inspection_completed': ('received', 'unreviewed'),
                    'authenticity_checked': ('inspected', 'authenticity_checked'),
                    'photographed': ('received', 'unreviewed', 'inspected', 'authenticity_checked', 'photographed'),
                    'registration_completed': ('photographed',),
                    'approval_pending': ('registered', 'approval_pending'),
                    'listed': ('registered', 'approval_pending', 'listed')}
                if workflow not in expected[kind]:
                    raise RegistryError('invalid_workflow_transition')
                next_state = {'inspection_completed': 'inspected', 'authenticity_checked': 'authenticity_checked',
                    'photographed': 'photographed', 'registration_completed': 'registered',
                    'approval_pending': 'approval_pending', 'listed': 'listed'}[kind]
                changes['workflow_state'] = next_state
                if kind == 'authenticity_checked' and details.get('authenticity_result') not in ('confirmed', 'uncertain', 'rejected'):
                    raise RegistryError('authenticity_result_required')
                if kind == 'authenticity_checked' and details['authenticity_result'] != 'confirmed':
                    changes['held'] = 1
                if kind == 'registration_completed':
                    changes['registration_completed_at'] = when
                if kind == 'listed':
                    # This records a human report, not marketplace/dual approval authorization.
                    changes['sales_state'] = 'listed'
            elif kind == 'sold':
                if sale in ('sold', 'shipped') or physical not in ('received', 'with_customer'):
                    raise RegistryError('invalid_state_transition')
                changes.update(sales_state='sold', sold_at=when)
            elif kind == 'shipped':
                if sale != 'sold' or physical not in ('received', 'with_customer'):
                    raise RegistryError('sale_before_shipping_required')
                changes.update(sales_state='shipped', physical_state='dispatched', shipped_at=when)
            elif kind == 'returned':
                if sale != 'shipped' or physical != 'dispatched' or not details.get('return_reason'):
                    raise RegistryError('return_reason_or_shipment_required')
                current_custodian = roles.get('custodian', {})
                changes.update(sales_state='unlisted', physical_state='received' if current_custodian.get('kind') == 'kaika' else 'with_customer',
                    workflow_state='received' if current_custodian.get('kind') == 'kaika' else 'unreviewed', returned_at=when, held=1)
            elif kind == 'return_shipped':
                if not staff or physical != 'received' or sale in ('sold', 'shipped'):
                    raise RegistryError('invalid_state_transition')
                from stage2_dispatch_guard import assert_return_listings_stopped
                assert_return_listings_stopped(self, item_id, when)
                changes.update(physical_state='return_in_transit', return_shipped_at=when)
            elif kind == 'return_received':
                if not staff or physical != 'return_in_transit':
                    raise RegistryError('invalid_state_transition')
                current = roles.get('custodian')
                if current:
                    self.execute('UPDATE business_item_assignments SET ended_at=? WHERE assignment_id=?', (when, current['assignment_id']))
                self._assign(item_id, 'custodian', self._party('customer', self._owner(roles)), when, evidence_reference, source_key + ':custodian')
                changes.update(physical_state='with_customer')
            elif kind == 'sale_cancelled':
                if sale != 'sold':
                    raise RegistryError('unshipped_sale_required')
                changes.update(sales_state='unlisted', sold_at=None)
            elif kind == 'hold':
                if not details.get('note') and not details.get('discrepancy'):
                    raise RegistryError('hold_reason_required')
                changes['held'] = 1
            elif kind == 'release_hold':
                if not profile['held'] or not details.get('note'):
                    raise RegistryError('release_reason_required')
                changes['held'] = 0
            customer_id = self._owner(roles)
            actor_party = self._party('staff' if staff else 'customer', actor.user_id)
            base_id = None
            if kind in ('expected', 'received', 'registration_completed', 'sold', 'shipped', 'returned', 'return_shipped', 'hold'):
                base_id = 'be_' + uuid.uuid4().hex
                self.execute('INSERT INTO business_item_events(event_id,physical_item_id,kind,occurred_at,actor_party_id,source_reference,source_event_key,customer_id) VALUES (?,?,?,?,?,?,?,?)',
                    (base_id, item_id, kind, when, actor_party, evidence_reference, source_key, customer_id))
            self.execute('INSERT INTO business_item_observations(observation_id,physical_item_id,kind,occurred_at,recorded_at,actor_user_id,customer_id,evidence_reference,details_json,base_event_id,source_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                ('ob_' + uuid.uuid4().hex, item_id, kind, when, self._observation_recorded_at(item_id), actor.user_id, customer_id, evidence_reference, _json(details), base_id, source_key))
            self.execute('UPDATE business_item_profiles SET ' + ','.join(key + '=?' for key in changes) + ' WHERE physical_item_id=?', (*changes.values(), item_id))
            self._remember(actor, source_key, digest, {'physical_item_id': item_id})
            return self.detail(actor, item_id)

    def record_work(self, actor, item_id, kind, performed_at, source_key, evidence_reference,
                    duration_seconds=None, measurement_source='unknown', rework_of=None, rework_reason=None):
        if kind not in WORK_KINDS:
            raise RegistryError('invalid_work_kind')
        if duration_seconds is None:
            if measurement_source != 'unknown':
                raise RegistryError('measurement_source_requires_duration')
        elif type(duration_seconds) is not int or duration_seconds < 0 or measurement_source not in ('observed', 'timer'):
            raise RegistryError('actual_duration_required')
        when = self._actual_time(performed_at)
        evidence_reference = _text(evidence_reference, 'evidence')
        if rework_of:
            rework_reason = _text(rework_reason, 'rework_reason')
        with self.atomic():
            profile, roles, staff = self._operable(actor, item_id)
            customer_id = self._owner(roles)
            digest, prior = self._request(actor, source_key, dict(action='work', item=item_id, kind=kind, when=when, evidence=evidence_reference,
                duration=duration_seconds, measurement=measurement_source, rework=rework_of, reason=rework_reason))
            if prior:
                return prior
            if when < _time(roles['owner']['started_at']):
                raise RegistryError('work_owner_period_mismatch')
            if profile['physical_state'] in ('dispatched', 'return_in_transit') and kind not in ('support', 'other'):
                raise RegistryError('physical_work_requires_item_present')
            if rework_of:
                previous = self.query('SELECT w.*,c.customer_id FROM business_work_records w JOIN business_work_context c ON c.work_id=w.work_id WHERE w.work_id=?', (rework_of,))
                if not previous or previous[0]['physical_item_id'] != item_id or previous[0]['customer_id'] != customer_id:
                    raise RegistryError('rework_item_mismatch')
            work_id = 'wk_' + uuid.uuid4().hex
            self.execute('''INSERT INTO business_work_records(work_id,physical_item_id,actor_party_id,kind,performed_at,duration_seconds,
                measurement_source,rework_of,rework_reason,source_reference,source_event_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (work_id, item_id, self._party('staff' if staff else 'customer', actor.user_id), kind, when,
                 duration_seconds, measurement_source, rework_of, rework_reason, evidence_reference, source_key))
            self.execute('INSERT INTO business_work_context(work_id,customer_id,recorded_at) VALUES (?,?,?)', (work_id, customer_id, _now()))
            result = {'physical_item_id': item_id, 'work_id': work_id, 'duration_seconds': duration_seconds,
                'measurement_source': measurement_source, 'charge_created': False}
            self._remember(actor, source_key, digest, result)
            return result

    def list_items(self, actor):
        self._require_ready()
        staff = self.actor(actor)
        if staff:
            rows = self.query('SELECT physical_item_id FROM business_item_profiles ORDER BY created_at,physical_item_id')
        else:
            rows = self.query('''SELECT a.physical_item_id FROM business_item_assignments a JOIN business_parties p ON p.party_id=a.party_id
                WHERE a.role='owner' AND a.ended_at IS NULL AND p.kind='customer' AND p.user_id=? ORDER BY a.physical_item_id''', (actor.user_id,))
        return [self.detail(actor, row['physical_item_id']) for row in rows]

    def detail(self, actor, item_id):
        self._require_ready()
        profile, roles, staff = self._profile(actor, item_id)
        owner = self._owner(roles)
        safe_keys = ('physical_item_id', 'title', 'origin', 'created_at', 'label_code', 'external_label',
            'identity_status', 'physical_state', 'sales_state', 'workflow_state', 'held', 'received_at',
            'registration_completed_at', 'sold_at', 'shipped_at', 'returned_at', 'return_shipped_at',
            'deadline_at', 'deadline_status')
        result = {key: profile[key] for key in safe_keys}
        result['owner_user_id'] = owner
        result['assignments'] = {role: {'kind': value['kind'], 'user_id': value['user_id'] if staff or value['user_id'] == actor.user_id else None}
                                 for role, value in roles.items()}
        result['custodian'] = roles.get('custodian', {}).get('kind', 'unknown')
        result['imported_fields'] = json.loads(profile['imported_fields_json']) if staff or profile['initial_customer_id'] == actor.user_id else {}
        result['legacy_ids'] = [row['merchandise_id'] for row in self.query('''SELECT l.merchandise_id,m.user_id FROM business_item_links l
            JOIN merchandise m ON m.id=l.merchandise_id WHERE l.physical_item_id=?''', (item_id,)) if staff or row['user_id'] == actor.user_id]
        event_filter = '' if staff else ' AND customer_id=?'
        params = (item_id,) if staff else (item_id, actor.user_id)
        observations = self.query('SELECT * FROM business_item_observations WHERE physical_item_id=?' + event_filter + ' ORDER BY occurred_at,observation_id', params)
        result['events'] = [{'event_id': row['observation_id'], 'kind': row['kind'], 'occurred_at': row['occurred_at'],
            'evidence_reference': row['evidence_reference'], 'details': json.loads(row['details_json'])} for row in observations]
        works = self.query('''SELECT w.*,c.customer_id FROM business_work_records w JOIN business_work_context c ON c.work_id=w.work_id
            WHERE w.physical_item_id=?''' + ('' if staff else ' AND c.customer_id=?') + ' ORDER BY w.performed_at,w.work_id', params)
        result['works'] = [{key: row[key] for key in ('work_id', 'kind', 'performed_at', 'duration_seconds', 'measurement_source', 'rework_of', 'rework_reason', 'source_reference')} for row in works]
        result['charge_created'] = False
        result['fee_review_status'] = 'not_evaluated'
        result['identity_evidence'] = profile['identity_evidence'] if staff else None
        result['deadline_evidence'] = profile['deadline_evidence'] if staff or profile['deadline_customer_id'] == actor.user_id else None
        if staff:
            result['assignment_history'] = self.query('SELECT * FROM business_item_assignments WHERE physical_item_id=? ORDER BY started_at,assignment_id', (item_id,))
            result['legacy_snapshots'] = [{**row, 'snapshot': json.loads(row['snapshot_json'])} for row in self.query('SELECT * FROM business_legacy_snapshots WHERE physical_item_id=? ORDER BY captured_at', (item_id,))]
            result['review_history'] = self.query('SELECT * FROM business_item_reviews WHERE physical_item_id=? ORDER BY recorded_at,review_id', (item_id,))
        return result

    def reconcile(self, actor):
        self._require_ready()
        self.actor(actor, staff_required=True)
        if self.dialect == 'postgres':
            columns = {row['column_name'] for row in self.query("SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='merchandise'")}
        else:
            columns = {row['name'] for row in self.query('PRAGMA table_info(merchandise)')}
        customer_scope = " AND m.scope='user'" if 'scope' in columns else ''
        legacy = self.query("SELECT m.id,m.user_id,m.product_name FROM merchandise m JOIN users u ON u.id=m.user_id WHERE u.role='user'" + customer_scope + ' ORDER BY m.id')
        links = {row['merchandise_id']: row for row in self.query('SELECT * FROM business_item_links')}
        rows = [{**row, 'physical_item_id': links.get(row['id'], {}).get('physical_item_id'),
                 'link_status': links.get(row['id'], {}).get('link_status', 'unlinked')} for row in legacy]
        return {'rows': rows, 'legacy_row_count': len(rows), 'linked_row_count': sum(row['link_status'] != 'unlinked' for row in rows),
            'physical_item_count': len(self.query('SELECT physical_item_id FROM business_items')),
            'unresolved_count': sum(row['link_status'] != 'verified' for row in rows),
            'automatic_merge': False, 'work_created': False, 'charge_created': False}
