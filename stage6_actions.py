"""Customer requests to discuss return/another channel, never an authorization
to charge, sell, dispose of, or physically ship the item. Existing term required.
"""
import hashlib
import uuid

from item_registry import RegistryError, _json, _now, _text
from stage6_events import current_items, effective_deadline


def deadline_version(store, item):
    rows = store.query('''SELECT review_id FROM business_item_reviews WHERE physical_item_id=? AND customer_id=?
        AND kind='deadline' ORDER BY review_id''', (item['physical_item_id'], item['owner_user_id']))
    # All immutable IDs make A -> B -> A a new version even when two review
    # timestamps have the same resolution; a random UUID is not a chronology.
    return hashlib.sha256(_json([item['ownership_assignment_id'], effective_deadline(item),
        item['deadline_evidence'], [row['review_id'] for row in rows]]).encode()).hexdigest()


class CustodyActionService:
    def __init__(self, store):
        self.store = store

    def _item(self, actor, item_id):
        self.store.authorize(actor)
        # The registry performs 404-style tenant access checks before any choice.
        self.store.registry._profile(actor, item_id)
        rows = [item for item in current_items(self.store, actor) if item['physical_item_id'] == item_id]
        if not rows:
            raise RegistryError('item_not_found', status=404)
        return rows[0]

    def _valid(self, item):
        return (effective_deadline(item) is not None and item['custodian_kind'] == 'kaika'
            and item['physical_state'] == 'received' and item['sales_state'] not in ('sold', 'shipped'))

    def _status(self, request_id):
        rows = self.store.query('SELECT * FROM stage6_custody_responses WHERE request_id=? ORDER BY revision DESC LIMIT 1', (request_id,))
        return rows[0] if rows else None

    def latest_for_item(self, item):
        rows = self.store.query('''SELECT * FROM stage6_custody_requests WHERE ownership_assignment_id=?
            ORDER BY revision DESC LIMIT 1''', (item['ownership_assignment_id'],))
        if not rows:
            return None
        row = rows[0]
        state = self._status(row['request_id'])
        return dict(row, status=state['status'] if state else 'pending',
            current_source=self._valid(item) and row['deadline_version'] == deadline_version(self.store, item),
            response_id=state['response_id'] if state else None)

    def get(self, actor, request_id):
        self.store.authorize(actor)
        rows = self.store.query('SELECT * FROM stage6_custody_requests WHERE request_id=?', (request_id,))
        if not rows:
            raise RegistryError('custody_request_not_found', status=404)
        row = rows[0]
        staff = self.store.authorize(actor, row['customer_id'])
        item = self._item(actor, row['physical_item_id'])
        if not staff and item['ownership_assignment_id'] != row['ownership_assignment_id']:
            raise RegistryError('custody_request_not_found', status=404)
        responses = self.store.query('SELECT * FROM stage6_custody_responses WHERE request_id=? ORDER BY revision', (request_id,))
        safe = {key: row[key] for key in ('request_id', 'physical_item_id', 'customer_id', 'ownership_assignment_id',
            'revision', 'previous_request_id', 'choice', 'note', 'deadline_at', 'recorded_at')}
        safe.update(status=responses[-1]['status'] if responses else 'pending',
            responses=[{key: response[key] for key in ('status','note','recorded_at')} for response in responses],
            current_source=(item['ownership_assignment_id'] == row['ownership_assignment_id'] and self._valid(item)
                and row['deadline_version'] == deadline_version(self.store, item)),
            physical_shipping_enabled=False, charge_created=False,
            path='/inventory/items/' + row['physical_item_id'])
        return safe

    def list_for_item(self, actor, item_id):
        item = self._item(actor, item_id)
        return [self.get(actor, row['request_id']) for row in self.store.query('''SELECT request_id FROM stage6_custody_requests
            WHERE ownership_assignment_id=? ORDER BY revision DESC''', (item['ownership_assignment_id'],))]

    def overview(self, actor):
        self.store.authorize(actor, staff_required=True)
        result = []
        for item in current_items(self.store, actor):
            latest = self.latest_for_item(item)
            if latest:
                result.append(self.get(actor, latest['request_id']))
        return result

    def request(self, actor, item_id, *, choice, note, source_key):
        staff = self.store.authorize(actor)
        if staff:
            raise RegistryError('custody_customer_choice_required', 'この申請は商品を所有するお客様が行います。', 403)
        if choice not in ('return', 'alternate_channel'):
            raise RegistryError('invalid_custody_choice')
        if note is None:
            note = ''
        if not isinstance(note, str) or len(note) > 1000 or '\x00' in note:
            raise RegistryError('invalid_custody_note')
        note = note.strip()
        item = self._item(actor, item_id)
        payload = dict(action='custody_choice', item_id=item_id, choice=choice, note=note,
            ownership_assignment_id=item['ownership_assignment_id'], deadline_version=deadline_version(self.store, item))

        def create():
            self.store.lock_item(item_id)
            fresh = self._item(actor, item_id)
            if fresh['ownership_assignment_id'] != item['ownership_assignment_id']:
                raise RegistryError('custody_owner_changed', status=409)
            if not self._valid(fresh):
                raise RegistryError('custody_current_terms_required', '開花で預かり中の商品と、確認済みの契約上の期限が必要です。', 409)
            latest = self.latest_for_item(fresh)
            if latest and latest['current_source'] and latest['status'] != 'resolved':
                raise RegistryError('custody_request_already_open', 'この商品について受付中の申請があります。', 409)
            row = dict(request_id='cr6_' + uuid.uuid4().hex, physical_item_id=item_id, customer_id=actor.user_id,
                ownership_assignment_id=fresh['ownership_assignment_id'], revision=latest['revision'] + 1 if latest else 1,
                previous_request_id=latest['request_id'] if latest else None, choice=choice, note=note,
                deadline_at=effective_deadline(fresh), deadline_version=deadline_version(self.store, fresh),
                recorded_at=_now(), source_key=source_key)
            self.store.execute('INSERT INTO stage6_custody_requests(' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
            return self.get(actor, row['request_id'])
        return self.store.operation(actor, source_key, payload, create, customer_id=actor.user_id)

    def review(self, actor, request_id, *, status, note, source_key):
        self.store.authorize(actor, staff_required=True)
        if status not in ('acknowledged', 'resolved'):
            raise RegistryError('invalid_custody_response')
        note = _text(note, 'custody_response_note', 1000)
        request = self.get(actor, request_id)
        payload = dict(action='custody_response', request_id=request_id, status=status, note=note)

        def respond():
            self.store.lock_item(request['physical_item_id'])
            current = self.get(actor, request_id)
            if not current['current_source']:
                raise RegistryError('custody_source_changed', '所有者・預かり状態・期限が変わっています。最新の申請を確認してください。', 409)
            if current['status'] == 'resolved' or current['status'] == status:
                raise RegistryError('custody_response_transition_invalid', status=409)
            old = self._status(request_id)
            row = dict(response_id='cs6_' + uuid.uuid4().hex, request_id=request_id, revision=old['revision'] + 1 if old else 1,
                status=status, note=note, actor_user_id=actor.user_id, recorded_at=_now(), source_key=source_key)
            self.store.execute('INSERT INTO stage6_custody_responses(' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))
            return self.get(actor, request_id)
        return self.store.operation(actor, source_key, payload, respond, customer_id=request['customer_id'], staff_required=True)
