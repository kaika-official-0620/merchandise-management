"""Customer and Kaika approve the same immutable listing version.

No external publishing or price automation. Approval validity is checked again
when the caller asks to publish; stored approval alone never means publishable.
"""
import hashlib
import json
import uuid

from item_registry import RegistryError, _json, _now, _text, _time
from stage3_common import yen

CHANNELS = frozenset(('mercari_shops', 'yahoo_store', 'rakuma_official', 'wholesale', 'dealer_auction', 'other'))
_ANCHOR_EVENTS = frozenset(('received', 'inspection_completed', 'authenticity_checked', 'photographed',
    'registration_completed', 'returned', 'return_shipped', 'return_received', 'hold', 'release_hold', 'sale_cancelled'))


class ListingService:
    def __init__(self, store, clock=None):
        self.store = store
        self.registry = store.registry
        self.clock = clock or _now

    def _current(self, actor, item_id):
        self.store.authorize(actor)
        profile, roles, staff = self.registry._profile(actor, item_id)
        owner = roles.get('owner', {})
        if owner.get('kind') != 'customer':
            raise RegistryError('customer_owner_required', status=409)
        self.store.authorize(actor, owner['user_id'])
        return profile, roles, staff

    def _row(self, actor, version_id):
        self.store.authorize(actor)
        rows = self.store.query('SELECT * FROM stage4_listing_versions WHERE version_id=?', (version_id,))
        if not rows:
            raise RegistryError('listing_not_found', status=404)
        row = rows[0]
        profile, roles, staff = self._current(actor, row['physical_item_id'])
        # A new owner must not see a previous customer's draft, evidence or terms.
        if not staff and roles['owner']['assignment_id'] != row['ownership_assignment_id']:
            raise RegistryError('listing_not_found', status=404)
        return row, profile, roles, staff

    def _anchor(self, profile, roles):
        observations = self.store.query('''SELECT observation_id,kind,occurred_at FROM business_item_observations
            WHERE physical_item_id=? ORDER BY occurred_at,observation_id''', (profile['physical_item_id'],))
        relevant = [row for row in observations if row['kind'] in _ANCHOR_EVENTS]
        return dict(ownership_assignment_id=roles['owner']['assignment_id'],
            custodian_assignment_id=roles.get('custodian', {}).get('assignment_id'),
            title=profile['title'], identity_status=profile['identity_status'],
            physical_state=profile['physical_state'], received_at=profile['received_at'],
            registration_completed_at=profile['registration_completed_at'],
            reused_catalog_registration=self._reused_registration(profile['physical_item_id'], roles['owner']['user_id']),
            relevant_event_ids=[row['observation_id'] for row in relevant])

    def _reused_registration(self, item_id, customer_id):
        resolver = getattr(self.store, 'reusable_catalog_registration', None)
        return resolver(item_id, customer_id) if resolver else None

    def _photo_allowed(self, item_id, customer_id, reference):
        # Only media already bound to this individual item can enter a proposal.
        resolver = getattr(self.store, 'listing_photo_allowed', None)
        if resolver is not None:
            return bool(resolver(item_id, customer_id, reference))
        try:
            return bool(self.store.query('''SELECT p.photo_path FROM stage2_photo_records p
                JOIN business_item_links l ON l.merchandise_id=p.merchandise_id
                WHERE l.physical_item_id=? AND p.owner_user_id=? AND p.photo_path=?
                AND p.removed_from_item_at IS NULL''', (item_id, customer_id, reference)))
        except Exception:
            return False

    def _photos(self, item_id, customer_id, references):
        if not isinstance(references, (tuple, list)) or not 1 <= len(references) <= 20:
            raise RegistryError('listing_photos_required')
        result = [_text(ref, 'photo_reference', 1000) for ref in references]
        if len(set(result)) != len(result):
            raise RegistryError('duplicate_photo_reference')
        for reference in result:
            if not self._photo_allowed(item_id, customer_id, reference):
                raise RegistryError('listing_photo_not_owned', status=409)
        return result

    def _channels(self, channels):
        if not isinstance(channels, (list, tuple)) or not 1 <= len(channels) <= 20:
            raise RegistryError('listing_channels_required')
        result = []
        for pair in channels:
            if (not isinstance(pair, dict) or set(pair) != {'channel', 'account_id'}
                    or not isinstance(pair['channel'], str) or pair['channel'] not in CHANNELS):
                raise RegistryError('invalid_listing_channel')
            result.append(dict(channel=pair['channel'], account_id=_text(pair['account_id'], 'account_id', 200)))
        if len(set((pair['channel'], pair['account_id']) for pair in result)) != len(result):
            raise RegistryError('duplicate_listing_channel')
        return sorted(result, key=lambda value: (value['channel'], value['account_id']))

    def propose(self, actor, item_id, *, title, description, condition, photo_references,
                price_gross_yen, minimum_price_gross_yen, channels, valid_until,
                source_key, evidence_reference, occurred_at, supersedes_version_id=None):
        profile, roles, _ = self._current(actor, item_id)
        customer_id = roles['owner']['user_id']
        price, minimum = yen(price_gross_yen), yen(minimum_price_gross_yen)
        if minimum <= 0 or price < minimum:
            raise RegistryError('listing_price_below_minimum')
        when, expiry = _time(occurred_at), _time(valid_until)
        if when > _time(self.clock()):
            raise RegistryError('listing_time_in_future')
        if expiry <= when or expiry <= _time(self.clock()):
            raise RegistryError('listing_expired', status=409)
        terms = dict(title=_text(title, 'title', 300), description=_text(description, 'description', 15000),
            condition=_text(condition, 'condition', 2000),
            photo_references=self._photos(item_id, customer_id, photo_references),
            price_gross_yen=price, minimum_price_gross_yen=minimum,
            channels=self._channels(channels), valid_until=expiry, price_change_policy='both_reapprove')
        evidence = _text(evidence_reference, 'evidence_reference', 2000)
        payload = dict(action='listing_propose', item_id=item_id, terms=terms,
            supersedes_version_id=supersedes_version_id, occurred_at=when, evidence_reference=evidence)

        def write():
            self.store.lock_item(item_id)
            profile, current_roles, _ = self._current(actor, item_id)
            if current_roles['owner']['assignment_id'] != roles['owner']['assignment_id']:
                raise RegistryError('listing_ownership_changed', status=409)
            if profile['held'] or profile['sales_state'] in ('sold', 'shipped'):
                raise RegistryError('listing_item_unavailable', status=409)
            if when < current_roles['owner']['started_at']:
                raise RegistryError('listing_before_ownership', status=409)
            if profile['last_event_at'] and when < _time(profile['last_event_at']):
                raise RegistryError('listing_before_item_event', status=409)
            self._photos(item_id, customer_id, terms['photo_references'])
            rows = self.store.query('SELECT * FROM stage4_listing_versions WHERE physical_item_id=? ORDER BY revision_number DESC', (item_id,))
            previous = rows[0] if rows else None
            effective_previous = supersedes_version_id
            if previous and previous['ownership_assignment_id'] != current_roles['owner']['assignment_id'] and effective_previous is None:
                # A new owner cannot inspect the old customer's version ID.
                effective_previous = previous['version_id']
            if effective_previous != (previous['version_id'] if previous else None):
                raise RegistryError('listing_version_conflict', status=409)
            if previous and when < previous['occurred_at']:
                raise RegistryError('listing_time_reversed', status=409)
            revision = previous['revision_number'] + 1 if previous else 1
            anchor = self._anchor(profile, current_roles)
            fingerprint = dict(physical_item_id=item_id, customer_id=customer_id,
                ownership_assignment_id=current_roles['owner']['assignment_id'], revision_number=revision,
                supersedes_version_id=effective_previous, terms=terms, item_anchor=anchor)
            digest = hashlib.sha256(_json(fingerprint).encode('utf-8')).hexdigest()
            version_id = 'lv_' + uuid.uuid4().hex
            self.store.execute('''INSERT INTO stage4_listing_versions(version_id,physical_item_id,customer_id,
                ownership_assignment_id,revision_number,supersedes_version_id,actor_user_id,title,description,
                item_condition,photo_references_json,channels_json,price_gross_yen,minimum_price_gross_yen,
                valid_until,price_change_policy,item_anchor_json,content_hash,occurred_at,recorded_at,evidence_reference,source_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (version_id,item_id,customer_id,current_roles['owner']['assignment_id'],revision,effective_previous,
                 actor.user_id,terms['title'],terms['description'],terms['condition'],_json(terms['photo_references']),
                 _json(terms['channels']),price,minimum,expiry,'both_reapprove',_json(anchor),digest,when,_now(),evidence,source_key))
            return self.detail(actor, version_id)
        return self.store.operation(actor, source_key, payload, write, customer_id=customer_id)

    def _reasons(self, row, profile, roles, at):
        reasons = []
        if roles['owner']['assignment_id'] != row['ownership_assignment_id']:
            reasons.append('listing_ownership_changed')
        if roles.get('custodian', {}).get('kind') != 'kaika' or not profile['received_at']:
            reasons.append('listing_kaika_receipt_required')
        reused = self._reused_registration(row['physical_item_id'], roles['owner']['user_id'])
        if not reused and (not profile['registration_completed_at'] or profile['workflow_state'] not in ('registered', 'approval_pending', 'listed')):
            reasons.append('listing_registration_required')
        if profile['held'] or profile['sales_state'] in ('sold', 'shipped') or profile['physical_state'] != 'received':
            reasons.append('listing_item_unavailable')
        if _json(self._anchor(profile, roles)) != row['item_anchor_json']:
            reasons.append('listing_item_changed')
        if _time(at) >= row['valid_until']:
            reasons.append('listing_expired')
        latest = self.store.query('SELECT version_id FROM stage4_listing_versions WHERE physical_item_id=? ORDER BY revision_number DESC', (row['physical_item_id'],))[0]
        if latest['version_id'] != row['version_id']:
            reasons.append('listing_superseded')
        if any(not self._photo_allowed(row['physical_item_id'], row['customer_id'], ref) for ref in json.loads(row['photo_references_json'])):
            reasons.append('listing_photo_not_owned')
        decisions = self.store.query('SELECT * FROM stage4_listing_decisions WHERE version_id=? ORDER BY recorded_at,decision_id', (row['version_id'],))
        if any(decision['content_hash'] != row['content_hash'] for decision in decisions):
            reasons.append('listing_approval_hash_mismatch')
        if any(decision['decision'] == 'rejected' for decision in decisions):
            reasons.append('listing_rejected')
        approved = {decision['party_role'] for decision in decisions if decision['decision'] == 'approved' and decision['content_hash'] == row['content_hash']}
        for role in ('customer', 'kaika'):
            if role not in approved:
                reasons.append('listing_' + role + '_approval_required')
        return reasons, decisions

    def _decision(self, actor, version_id, decision, *, source_key, evidence_reference, occurred_at, reason=None):
        row, _, _, staff = self._row(actor, version_id)
        party = 'kaika' if staff else 'customer'
        when = _time(occurred_at)
        if when > _time(self.clock()):
            raise RegistryError('listing_time_in_future')
        if when < row['occurred_at']:
            raise RegistryError('listing_time_reversed', status=409)
        evidence = _text(evidence_reference, 'evidence_reference', 2000)
        reason = _text(reason, 'rejection_reason', 2000) if decision == 'rejected' else None
        payload = dict(action='listing_decision', version_id=version_id, decision=decision,
            occurred_at=when, evidence_reference=evidence, reason=reason)

        def write():
            self.store.lock_item(row['physical_item_id'])
            fresh, profile, roles, _ = self._row(actor, version_id)
            reasons, existing = self._reasons(fresh, profile, roles, self.clock())
            if any(old['party_role'] == party and when < old['occurred_at'] for old in existing):
                raise RegistryError('listing_time_reversed', status=409)
            if decision == 'approved':
                blocked = [code for code in reasons if code not in ('listing_customer_approval_required', 'listing_kaika_approval_required')]
                if blocked:
                    raise RegistryError(blocked[0], status=409)
            else:
                if roles['owner']['assignment_id'] != fresh['ownership_assignment_id']:
                    raise RegistryError('listing_ownership_changed', status=409)
            if any(old['party_role'] == party and old['decision'] == decision for old in existing):
                return self.detail(actor, version_id)
            self.store.execute('''INSERT INTO stage4_listing_decisions(decision_id,version_id,party_role,decision,
                actor_user_id,content_hash,reason,occurred_at,recorded_at,evidence_reference,source_key)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''', ('ld_' + uuid.uuid4().hex, version_id, party, decision, actor.user_id,
                fresh['content_hash'], reason, when, _now(), evidence, source_key))
            return self.detail(actor, version_id)
        return self.store.operation(actor, source_key, payload, write, customer_id=row['customer_id'])

    def approve(self, actor, version_id, *, source_key, evidence_reference, occurred_at):
        return self._decision(actor, version_id, 'approved', source_key=source_key,
            evidence_reference=evidence_reference, occurred_at=occurred_at)

    def reject(self, actor, version_id, *, source_key, evidence_reference, occurred_at, reason):
        return self._decision(actor, version_id, 'rejected', source_key=source_key,
            evidence_reference=evidence_reference, occurred_at=occurred_at, reason=reason)

    def detail(self, actor, version_id, at=None):
        row, profile, roles, staff = self._row(actor, version_id)
        reasons, decisions = self._reasons(row, profile, roles, at or self.clock())
        result = {key: row[key] for key in ('version_id','physical_item_id','customer_id','ownership_assignment_id',
            'revision_number','supersedes_version_id','title','description','price_gross_yen','minimum_price_gross_yen',
            'valid_until','price_change_policy','content_hash','occurred_at')}
        result.update(condition=row['item_condition'], photo_references=json.loads(row['photo_references_json']),
            channels=json.loads(row['channels_json']), price_yen=row['price_gross_yen'],
            status='approved' if not reasons else ('rejected' if 'listing_rejected' in reasons else 'needs_review'),
            review_reasons=reasons, external_publish_enabled=False,
            approvals=[dict(party_role=d['party_role'], decision=d['decision'], occurred_at=d['occurred_at'],
                content_hash=d['content_hash'], reason=d['reason']) for d in decisions])
        if staff:
            result['evidence_reference'] = row['evidence_reference']
            result['decisions'] = decisions
        elif row['supersedes_version_id']:
            previous = self.store.query('SELECT ownership_assignment_id FROM stage4_listing_versions WHERE version_id=?', (row['supersedes_version_id'],))[0]
            if previous['ownership_assignment_id'] != row['ownership_assignment_id']:
                result['supersedes_version_id'] = None
        return result

    def list_versions(self, actor, item_id, at=None):
        _, roles, staff = self._current(actor, item_id)
        sql = 'SELECT version_id FROM stage4_listing_versions WHERE physical_item_id=?'
        params = (item_id,)
        if not staff:
            sql += ' AND ownership_assignment_id=?'
            params += (roles['owner']['assignment_id'],)
        return [self.detail(actor, row['version_id'], at) for row in self.store.query(sql + ' ORDER BY revision_number DESC', params)]

    def approved_version(self, actor, version_id, at=None):
        result = self.detail(actor, version_id, at)
        if result['review_reasons']:
            raise RegistryError(result['review_reasons'][0], status=409)
        return result

    def approved_for_publication(self, actor, version_id, channel, at, account_id=None):
        self.store.authorize(actor, staff_required=True)
        result = self.approved_version(actor, version_id, at)
        matches = [pair for pair in result['channels'] if pair['channel'] == channel and (account_id is None or pair['account_id'] == account_id)]
        if len(matches) != 1:
            raise RegistryError('listing_channel_not_approved', status=409)
        # Operational evidence is staff-only, and not external listing content.
        result.pop('decisions', None)
        result.pop('evidence_reference', None)
        return result
