"""Test-only/manual channel records and fulfillment; never calls a marketplace.

An external ID here is evidence entered by a staff member, not proof of API
permission or successful publication. Raw records and corrections are retained.
An item lock and one current claim prevent two locally accepted orders. A stop
queue records the unavoidable period before other marketplaces are confirmed off.
"""
import csv
import hashlib
import io
import json
import uuid
from datetime import datetime, timedelta, timezone

from item_registry import RegistryError, _json, _now, _text, _time
from stage3_common import yen
from stage3_profit import ProfitService, MONEY_FIELDS
from stage3_billing import BillingService


CHANNELS = ('mercari_shops', 'yahoo_store', 'rakuma_official', 'wholesale', 'dealer_auction', 'other')
CAPABILITIES = ('product_register', 'order_fetch', 'fee_fetch', 'shipping_fetch', 'cancel',
                'stock_update', 'manual_entry', 'csv', 'consignment', 'multi_listing', 'api')
CAPABILITY_STATES = ('unverified', 'documented', 'unavailable')
CSV_COLUMNS = ('listing_id', 'external_order_id', 'sale_gross_yen', 'occurred_at', 'evidence_reference')


def _hash(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _id(prefix):
    return prefix + '_' + uuid.uuid4().hex


class ChannelService:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or _now
        self.profit = ProfitService(store.finance)
        self.billing = BillingService(store.finance)

    def _insert(self, table, values):
        self.store.execute('INSERT INTO ' + table + '(' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))

    def _when(self, value):
        when = _time(value)
        if when > _time(self.clock()):
            raise RegistryError('channel_time_in_future', '実際に行った日時を記録してください。')
        return when

    def _one(self, table, column, value):
        rows = self.store.query('SELECT * FROM ' + table + ' WHERE ' + column + '=?', (value,))
        if not rows:
            raise RegistryError('channel_record_not_found', status=404)
        return rows[0]

    def _operation(self, actor, key, payload, callback, customer_id=None):
        return self.store.operation(actor, key, payload, callback, customer_id=customer_id,
                                    staff_required=True, finance=True)

    def _account(self, account_id):
        row = self._one('stage4_channel_accounts', 'account_id', account_id)
        reviews = self.store.query('SELECT * FROM stage4_channel_reviews WHERE account_id=? ORDER BY revision DESC LIMIT 1', (account_id,))
        return dict(row, capabilities=json.loads(reviews[0]['capabilities_json']), review_id=reviews[0]['review_id'],
                    external_connection_enabled=False, execution_mode='test_only')

    def _capabilities(self, values):
        if values is None:
            values = {}
        if not isinstance(values, dict) or set(values) - set(CAPABILITIES):
            raise RegistryError('invalid_channel_capabilities')
        result = {}
        for name in CAPABILITIES:
            value = values.get(name, {'status': 'unverified', 'evidence_reference': None})
            if not isinstance(value, dict) or set(value) - {'status', 'evidence_reference'} or value.get('status') not in CAPABILITY_STATES:
                raise RegistryError('invalid_channel_capability')
            evidence = value.get('evidence_reference')
            if value['status'] != 'unverified' or evidence is not None:
                evidence = _text(evidence, 'capability_evidence')
            result[name] = dict(status=value['status'], evidence_reference=evidence, enabled=False)
        return result

    def configure_account(self, actor, *, channel, display_name, source_key, evidence_reference, capabilities=None):
        if channel not in CHANNELS:
            raise RegistryError('unsupported_channel')
        name, evidence = _text(display_name, 'account_name', 200), _text(evidence_reference, 'account_evidence')
        caps = self._capabilities(capabilities)
        payload = dict(action='channel_account', channel=channel, name=name, evidence=evidence, capabilities=caps)
        def create():
            account = dict(account_id=_id('ca4'), channel=channel, display_name=name, actor_user_id=actor.user_id,
                           recorded_at=_now(), evidence_reference=evidence, source_key=source_key)
            self._insert('stage4_channel_accounts', account)
            self._insert('stage4_channel_reviews', dict(review_id=_id('cr4'), account_id=account['account_id'], revision=1,
                capabilities_json=_json(caps), evidence_reference=evidence, actor_user_id=actor.user_id, recorded_at=_now(), source_key=source_key + ':review'))
            return self._account(account['account_id'])
        return self._operation(actor, source_key, payload, create)

    def review_account(self, actor, account_id, *, capabilities, evidence_reference, source_key):
        self.store.authorize(actor, staff_required=True, finance=True)
        evidence, caps = _text(evidence_reference, 'account_evidence'), self._capabilities(capabilities)
        def create():
            self._account(account_id)
            revision = self.store.query('SELECT MAX(revision) AS n FROM stage4_channel_reviews WHERE account_id=?', (account_id,))[0]['n'] + 1
            self._insert('stage4_channel_reviews', dict(review_id=_id('cr4'), account_id=account_id, revision=revision,
                capabilities_json=_json(caps), evidence_reference=evidence, actor_user_id=actor.user_id, recorded_at=_now(), source_key=source_key))
            return self._account(account_id)
        return self._operation(actor, source_key, dict(action='channel_review', account=account_id, evidence=evidence, capabilities=caps), create)

    def accounts(self, actor):
        staff = self.store.authorize(actor)
        rows = self.store.query('SELECT account_id,channel,display_name FROM stage4_channel_accounts ORDER BY recorded_at,account_id')
        return [self._account(row['account_id']) for row in rows] if staff else rows

    def _sync_legacy(self, actor, item_id, customer_id, kind, when, evidence, key, amount=None, channel=None):
        """Narrow existing read-model projection with an immutable before-image.

        No new legacy product is generated; ambiguous/foreign/admin links stop
        the whole transaction rather than updating a different owner's row.
        """
        links = self.store.query('SELECT merchandise_id FROM business_item_links WHERE physical_item_id=?', (item_id,))
        if not links:
            return
        suffix = ' FOR UPDATE' if self.store.dialect == 'postgres' else ''
        day = datetime.fromisoformat(when).astimezone(timezone(timedelta(hours=9))).date().isoformat()
        for link in links:
            rows = self.store.query('SELECT * FROM merchandise WHERE id=?' + suffix, (link['merchandise_id'],))
            if not rows or rows[0].get('user_id') != customer_id or rows[0].get('scope') != 'user' or rows[0].get('custody_location') != 'kaika':
                raise RegistryError('legacy_projection_scope_unverified', '旧在庫との所有者・保管場所の照合が必要です。', 409)
            old = rows[0]
            values = {}
            if kind == 'listed':
                values = dict(is_listed=True, listing_date=day, listing_price=amount)
            elif kind == 'sold':
                values = dict(sale_date=day, sale_price=amount, is_shipped=False, is_listed=False)
                if channel:
                    values['sales_destination'] = channel
            elif kind == 'corrected':
                values = dict(sale_price=amount)
            elif kind == 'shipped':
                values = dict(is_shipped=True)
            elif kind == 'listing_stopped':
                values = dict(is_listed=False)
            elif kind in ('returned', 'sale_cancelled'):
                values = dict(sale_date=None, sale_price=0, is_shipped=False, is_listed=False)
            values = {field: value for field, value in values.items() if field in old}
            if not values:
                raise RegistryError('legacy_projection_schema_missing', status=409)
            self._insert('stage4_legacy_projections', dict(projection_id=_id('lp4'), physical_item_id=item_id,
                merchandise_id=old['id'], customer_id=customer_id, actor_user_id=actor.user_id, kind=kind,
                occurred_at=when, recorded_at=_now(), before_json=_json({field:old[field] for field in values}),
                after_json=_json(values), evidence_reference=evidence, source_key=key + ':' + str(old['id'])))
            self.store.execute('UPDATE merchandise SET ' + ','.join(field + '=?' for field in values) + ' WHERE id=? AND user_id=?',
                               (*values.values(),old['id'],customer_id))

    def _listing(self, actor, listing_id, write=False):
        self.store.authorize(actor, staff_required=write, finance=write)
        row = self._one('stage4_channel_listings', 'listing_id', listing_id)
        self.store.authorize(actor, row['customer_id'], staff_required=write, finance=write)
        return row

    def _current_owner(self, actor, listing):
        self._lock_legacy(listing['physical_item_id'])
        self.store.lock_item(listing['physical_item_id'])
        item = self.store.registry.detail(actor, listing['physical_item_id'])
        owner = self.store.query("SELECT assignment_id FROM business_item_assignments WHERE physical_item_id=? AND role='owner' AND ended_at IS NULL", (listing['physical_item_id'],))
        if item['owner_user_id'] != listing['customer_id'] or len(owner) != 1 or owner[0]['assignment_id'] != listing['ownership_assignment_id']:
            raise RegistryError('channel_owner_changed', '所有者が変わったため取引を保留しました。', 409)
        return item

    def _lock_legacy(self, item_id):
        # Match the existing receipt/self-management adapter's legacy-first
        # order, then lock the registry. No web/network operation holds locks.
        if self.store.dialect == 'postgres':
            for row in self.store.query('SELECT merchandise_id FROM business_item_links WHERE physical_item_id=? ORDER BY merchandise_id', (item_id,)):
                self.store.query('SELECT id FROM merchandise WHERE id=? FOR UPDATE', (row['merchandise_id'],))

    def record_listing(self, actor, version_id, account_id, *, external_listing_id, occurred_at, source_key, evidence_reference):
        self.store.authorize(actor, staff_required=True, finance=True)
        from stage4_listings import ListingService
        initial = ListingService(self.store).detail(actor, version_id, at=self.clock())
        account = self._account(account_id)
        external = _text(external_listing_id, 'external_listing_id', 300)
        when, evidence = self._when(occurred_at), _text(evidence_reference, 'listing_evidence')
        payload = dict(action='record_listing', version_id=version_id, account_id=account_id, external=external, when=when, evidence=evidence)
        def create():
            previous = self.store.query('SELECT * FROM stage4_channel_listings WHERE account_id=? AND external_listing_id=?', (account_id, external))
            if previous:
                if previous[0]['payload_hash'] != _hash(payload):
                    raise RegistryError('external_listing_conflict', status=409)
                return self._decode_listing(previous[0], actor)
            approved = ListingService(self.store).approved_for_publication(actor, version_id, account['channel'], at=self.clock(), account_id=account_id)
            item_id, customer_id = approved['physical_item_id'], approved['customer_id']
            self.store.finance.customer_lock(customer_id)
            self._lock_legacy(item_id)
            self.store.lock_item(item_id)
            # Re-read after locks: review or ownership changes cannot authorize
            # publication using a stale snapshot read before another writer.
            approved = ListingService(self.store).approved_for_publication(actor, version_id, account['channel'], at=self.clock(), account_id=account_id)
            if when > _time(self.clock()) or when < max(_time(approval['occurred_at']) for approval in approved['approvals']):
                raise RegistryError('listing_time_outside_approval')
            if self.store.query('SELECT order_id FROM stage4_order_claims WHERE physical_item_id=?', (item_id,)):
                raise RegistryError('item_already_ordered', status=409)
            outstanding = self._stop_rows(item_id=item_id)
            if any(task['status'] != 'confirmed_stopped' for task in outstanding):
                raise RegistryError('previous_listing_stop_unconfirmed', status=409)
            if any(when < task['latest_at'] for task in outstanding):
                raise RegistryError('listing_before_previous_stop_confirmation',
                    '前回の出品停止を確認した後の出品日時を記録してください。', 409)
            row = dict(listing_id=_id('cl4'), physical_item_id=item_id, customer_id=customer_id,
                ownership_assignment_id=approved['ownership_assignment_id'], version_id=version_id, account_id=account_id,
                external_listing_id=external, approved_snapshot_json=_json(approved), occurred_at=when, recorded_at=_now(),
                actor_user_id=actor.user_id, evidence_reference=evidence, source_key=source_key, payload_hash=_hash(payload))
            self._insert('stage4_channel_listings', row)
            item = self.store.registry.detail(actor, item_id)
            if item['sales_state'] != 'listed':
                if item['workflow_state'] not in ('registered', 'approval_pending', 'listed'):
                    resolver = getattr(self.store, 'reusable_catalog_registration', None)
                    proof = resolver(item_id, customer_id) if resolver else None
                    if not proof or proof.get('ownership_assignment_id') != approved['ownership_assignment_id']:
                        raise RegistryError('listing_registration_required', status=409)
                    # Adopt existing company product information. No fabricated
                    # registration event/date/work or registration quota count.
                    self.store.execute("UPDATE business_item_profiles SET workflow_state='registered' WHERE physical_item_id=?", (item_id,))
                self.store.registry.record_event(actor, item_id, 'listed', when, 'channel-listing:' + row['listing_id'], evidence,
                                                {'note': '検証用・手入力の出品記録。外部公開は未実行。'})
            self._sync_legacy(actor, item_id, customer_id, 'listed', when, evidence, 'channel-legacy:' + row['listing_id'], approved['price_gross_yen'])
            return self._decode_listing(row, actor)
        return self._operation(actor, source_key, payload, create, customer_id=initial['customer_id'])

    def _approval_reasons(self, actor, row):
        from stage4_listings import ListingService
        try:
            current = ListingService(self.store).detail(actor, row['version_id'], at=self.clock())
            return current['review_reasons']
        except RegistryError as error:
            return [error.code]

    def _decode_listing(self, row, actor):
        result = dict(row)
        result['approved_snapshot'] = json.loads(result.pop('approved_snapshot_json'))
        tasks = self._stop_rows(listing_id=row['listing_id'])
        reasons = self._approval_reasons(actor, row)
        result.update(local_sale_blocked=bool(tasks or reasons), stop_tasks=tasks, external_connection_enabled=False,
                      approval_review_reasons=reasons, approval_stop_required=bool(reasons and not tasks), mode='test_only')
        return result

    def _order(self, actor, order_id, write=False):
        self.store.authorize(actor, staff_required=write, finance=write)
        order = self._one('stage4_channel_orders', 'order_id', order_id)
        self.store.authorize(actor, order['customer_id'], staff_required=write, finance=write)
        return order

    def _financial_channel(self, account_id):
        channel = self._account(account_id)['channel']
        return 'multichannel' if channel in ('mercari_shops', 'yahoo_store', 'rakuma_official') else channel

    def _record_sale_facts(self, actor, order, amount, when, evidence, key):
        before = self.profit.snapshot(actor, order['physical_item_id'])
        amounts = {name: before.get(name) for name in MONEY_FIELDS}
        # A resale must not inherit charges, payment or return adjustments of
        # its predecessor. Purchase cost belongs to this ownership period.
        observations = self.store.query("SELECT observation_id FROM business_item_observations WHERE physical_item_id=? AND kind='sold' ORDER BY occurred_at DESC,recorded_at DESC LIMIT 1", (order['physical_item_id'],))
        sale_id = observations[0]['observation_id']
        same_sale = before.get('sale_event_id') == sale_id
        if not same_sale:
            amounts = {name: (before.get(name) if name == 'purchase_cost_yen' else None) for name in MONEY_FIELDS}
            resolver = getattr(self.store, 'reusable_catalog_registration', None)
            proof = resolver(order['physical_item_id'], order['customer_id']) if resolver else None
            if amounts['purchase_cost_yen'] is None and proof:
                purchases = self.store.query('SELECT amount_yen FROM stage4_purchase_orders WHERE order_id=? AND customer_id=? AND physical_item_id=?',
                    (proof['order_id'], order['customer_id'], order['physical_item_id']))
                if purchases:
                    amounts['purchase_cost_yen'] = purchases[0]['amount_yen']
                    evidence = 'stage4-order:' + order['order_id'] + ';purchase-order:' + proof['order_id']
        amounts['sale_gross_yen'] = amount
        return self.profit.record_facts(actor, order['physical_item_id'], amounts=amounts, channel=self._financial_channel(order['account_id']),
            source_reference=evidence, occurred_at=when, source_key=key, previous_version_id=before.get('version_id'),
            shipping_entries=before.get('shipping_entries') if same_sale else None,
            channel_fee_includes_shipping=before.get('channel_fee_includes_shipping') if same_sale else None, sale_event_id=sale_id)

    def _make_tasks(self, order):
        kinds = ('conflict_resolution',) if order['initial_status'] == 'conflict' else ('customer_response', 'packing', 'shipping')
        for kind in kinds:
            self._insert('stage4_order_tasks', dict(task_id=_id('ot4'), order_id=order['order_id'], kind=kind, created_at=order['occurred_at']))
        for listing in self.store.query('SELECT listing_id FROM stage4_channel_listings WHERE physical_item_id=?', (order['physical_item_id'],)):
            self._insert('stage4_stop_tasks', dict(task_id=_id('st4'), order_id=order['order_id'], listing_id=listing['listing_id'],
                created_at=order['occurred_at'], reason='注文受付後の出品停止確認', reason_key=order['order_id'] + ':' + listing['listing_id']))

    def record_order(self, actor, listing_id, *, external_order_id, sale_gross_yen, occurred_at, source_key, evidence_reference):
        listing = self._listing(actor, listing_id, write=True)
        amount, when = yen(sale_gross_yen), self._when(occurred_at)
        if amount == 0:
            raise RegistryError('positive_sale_amount_required')
        external, evidence = _text(external_order_id, 'external_order_id', 300), _text(evidence_reference, 'order_evidence')
        payload = dict(action='record_order', listing_id=listing_id, external_order_id=external, sale_gross_yen=amount, occurred_at=when, evidence_reference=evidence)
        def create():
            prior = self.store.query('SELECT * FROM stage4_channel_orders WHERE account_id=? AND external_order_id=?', (listing['account_id'], external))
            if prior:
                if prior[0]['payload_hash'] != _hash(payload):
                    raise RegistryError('external_order_conflict', '外部注文番号の内容が変わっています。訂正記録を使ってください。', 409)
                return self._decode_order(prior[0])
            item = self._current_owner(actor, listing)
            if when < listing['occurred_at']:
                raise RegistryError('order_before_listing')
            approved = json.loads(listing['approved_snapshot_json'])
            claims = self.store.query('SELECT order_id FROM stage4_order_claims WHERE physical_item_id=?', (listing['physical_item_id'],))
            reason = None
            if claims or item['sales_state'] in ('sold', 'shipped'):
                reason = 'item_already_ordered'
            elif item['held'] or item['custodian'] != 'kaika' or item['physical_state'] != 'received':
                reason = 'item_unavailable'
            elif self._stop_rows(listing_id=listing_id):
                reason = 'listing_already_stopped_or_stop_pending'
            elif self._approval_reasons(actor, listing):
                reason = 'listing_approval_no_longer_valid'
            elif amount != approved['price_gross_yen']:
                # Until a permitted price-range policy is decided, all changes
                # require a new dual-approved publication version.
                reason = 'sale_price_differs_from_approved_version'
            order = dict(order_id=_id('co4'), listing_id=listing_id, account_id=listing['account_id'], external_order_id=external,
                physical_item_id=listing['physical_item_id'], customer_id=listing['customer_id'], sale_gross_yen=amount,
                occurred_at=when, recorded_at=_now(), actor_user_id=actor.user_id, evidence_reference=evidence,
                raw_snapshot_json=_json(payload), payload_hash=_hash(payload), initial_status='conflict' if reason else 'accepted',
                conflict_reason=reason, conflicting_order_id=claims[0]['order_id'] if claims else None, source_key=source_key)
            self._insert('stage4_channel_orders', order)
            self._make_tasks(order)
            if not reason:
                self._insert('stage4_order_claims', dict(physical_item_id=order['physical_item_id'], order_id=order['order_id']))
                self.store.registry.record_event(actor, order['physical_item_id'], 'sold', when, 'channel-order:' + order['order_id'], evidence)
                self._record_sale_facts(actor, order, amount, when, evidence, 'channel-facts:' + order['order_id'])
                self.profit.record_sales_status(actor, order['physical_item_id'], status='unpaid', occurred_at=when,
                    source_key='channel-unpaid:' + order['order_id'], source_reference=evidence)
                self._sync_legacy(actor, order['physical_item_id'], order['customer_id'], 'sold', when, evidence,
                    'channel-legacy:' + order['order_id'], amount, self._account(order['account_id'])['channel'])
            return self._decode_order(order)
        return self._operation(actor, source_key, payload, create, customer_id=listing['customer_id'])

    def audit_listing_approvals(self, actor, *, occurred_at, evidence_reference, source_key):
        """Explicit staff action materializes expiry/rejection stop work; no IO."""
        self.store.authorize(actor, staff_required=True, finance=True)
        when, evidence = self._when(occurred_at), _text(evidence_reference, 'approval_audit_evidence')
        def create():
            tasks = []
            for listing in self.store.query('SELECT * FROM stage4_channel_listings'):
                reasons = self._approval_reasons(actor, listing)
                if not reasons or self._stop_rows(listing_id=listing['listing_id']):
                    continue
                task = dict(task_id=_id('st4'), order_id=None, listing_id=listing['listing_id'], created_at=when,
                    reason='承認の再確認: ' + ','.join(reasons), reason_key='approval:' + listing['listing_id'])
                self._insert('stage4_stop_tasks', task)
                tasks.append(task)
            return dict(tasks=tasks, evidence_reference=evidence, external_request_sent=False)
        return self._operation(actor, source_key, dict(action='audit_listing_approvals', when=when, evidence=evidence), create)

    def request_item_stop(self, actor, item_id, *, reason, occurred_at, evidence_reference, source_key):
        """Queue all recorded listings for an unsold return or another manual stop.

        A repeated request reuses outstanding/confirmed tasks. Only a separate
        evidenced stop confirmation clears the return-dispatch guard.
        """
        self.store.authorize(actor, staff_required=True, finance=True)
        item = self.store.registry.detail(actor, item_id)
        when, reason = self._when(occurred_at), _text(reason, 'stop_reason', 1000)
        evidence = _text(evidence_reference, 'stop_evidence')
        payload = dict(action='request_item_stop', item=item_id, reason=reason, when=when, evidence=evidence)
        def create():
            self._lock_legacy(item_id)
            self.store.lock_item(item_id)
            fresh = self.store.registry.detail(actor, item_id)
            if fresh['owner_user_id'] != item['owner_user_id']:
                raise RegistryError('channel_owner_changed', status=409)
            listings = self.store.query('SELECT listing_id,occurred_at FROM stage4_channel_listings WHERE physical_item_id=?', (item_id,))
            if not listings:
                raise RegistryError('item_listing_required', 'この個品には出品記録がありません。', 409)
            tasks = []
            for listing in listings:
                if when < listing['occurred_at']:
                    raise RegistryError('stop_before_listing', '出品を記録した後の日時で停止依頼を記録してください。', 409)
                existing = self._stop_rows(listing_id=listing['listing_id'])
                if existing:
                    tasks.extend(existing)
                    continue
                task = dict(task_id=_id('st4'), order_id=None, listing_id=listing['listing_id'], created_at=when,
                    reason=reason, reason_key='manual:' + listing['listing_id'])
                self._insert('stage4_stop_tasks', task)
                tasks.append(task)
            return dict(physical_item_id=item_id, tasks=tasks, external_request_sent=False)
        return self._operation(actor, source_key, payload, create, customer_id=item['owner_user_id'])

    def correct_order(self, actor, order_id, *, sale_gross_yen, reason, occurred_at, evidence_reference, source_key):
        order = self._order(actor, order_id, write=True)
        amount, when = yen(sale_gross_yen), self._when(occurred_at)
        if not amount:
            raise RegistryError('positive_sale_amount_required')
        reason, evidence = _text(reason, 'correction_reason'), _text(evidence_reference, 'correction_evidence')
        payload = dict(action='correct_order', order=order_id, amount=amount, reason=reason, when=when, evidence=evidence)
        def create():
            self._assert_active_order(actor, order)
            previous = self.store.query('SELECT * FROM stage4_order_corrections WHERE order_id=? ORDER BY revision DESC LIMIT 1', (order_id,))
            if when < (previous[0]['occurred_at'] if previous else order['occurred_at']):
                raise RegistryError('order_correction_time_reversed')
            self._insert('stage4_order_corrections', dict(correction_id=_id('oc4'), order_id=order_id,
                revision=previous[0]['revision'] + 1 if previous else 1, sale_gross_yen=amount, reason=reason, occurred_at=when,
                recorded_at=_now(), actor_user_id=actor.user_id, evidence_reference=evidence, source_key=source_key))
            self._record_sale_facts(actor, order, amount, when, evidence, source_key + ':facts')
            self._sync_legacy(actor, order['physical_item_id'], order['customer_id'], 'corrected', when, evidence, source_key + ':legacy', amount)
            # Price corrections are evidence, never permission to settle or
            # ship a different unapproved price; fulfillment checks below.
            return self._decode_order(order)
        return self._operation(actor, source_key, payload, create, customer_id=order['customer_id'])

    def _stop_rows(self, item_id=None, listing_id=None):
        query = '''SELECT t.*,l.account_id,l.external_listing_id,l.customer_id,l.physical_item_id FROM stage4_stop_tasks t
            JOIN stage4_channel_listings l ON l.listing_id=t.listing_id'''
        params = ()
        if item_id is not None:
            query += ' WHERE l.physical_item_id=?'
            params = (item_id,)
        elif listing_id is not None:
            query += ' WHERE l.listing_id=?'
            params = (listing_id,)
        rows = self.store.query(query + ' ORDER BY t.created_at,t.task_id', params)
        for row in rows:
            history = self.store.query('SELECT * FROM stage4_stop_events WHERE task_id=? ORDER BY revision', (row['task_id'],))
            row.update(status=history[-1]['status'] if history else 'pending', history=history,
                       latest_at=history[-1]['occurred_at'] if history else row['created_at'])
        return rows

    def stop_task(self, actor, task_id, *, status, evidence_reference, occurred_at, source_key):
        self.store.authorize(actor, staff_required=True, finance=True)
        task = self._one('stage4_stop_tasks', 'task_id', task_id)
        listing = self._listing(actor, task['listing_id'], write=True)
        if status not in ('confirmed_stopped', 'failed', 'retry_requested'):
            raise RegistryError('invalid_stop_status')
        when, evidence = self._when(occurred_at), _text(evidence_reference, 'stop_evidence')
        def create():
            self._lock_legacy(listing['physical_item_id'])
            self.store.lock_item(listing['physical_item_id'])
            history = self.store.query('SELECT * FROM stage4_stop_events WHERE task_id=? ORDER BY revision', (task_id,))
            if history and history[-1]['status'] == 'confirmed_stopped':
                raise RegistryError('listing_stop_already_confirmed', status=409)
            if when < (history[-1]['occurred_at'] if history else task['created_at']):
                raise RegistryError('stop_time_reversed')
            record = dict(event_id=_id('se4'), task_id=task_id, revision=len(history) + 1, status=status,
                evidence_reference=evidence, occurred_at=when, recorded_at=_now(), actor_user_id=actor.user_id, source_key=source_key)
            self._insert('stage4_stop_events', record)
            self._project_stopped_listing(actor, listing, when, evidence, source_key)
            return dict(record, external_request_sent=False)
        return self._operation(actor, source_key, dict(action='stop_task', task=task_id, status=status, when=when, evidence=evidence), create, customer_id=listing['customer_id'])

    def _project_stopped_listing(self, actor, listing, when, evidence, source_key):
        """Keep unsold read models in sync with evidenced stops, never sales.

        The immutable stop history is authoritative. No earlier date, sale,
        ownership or fee record is erased when the current listing flag clears.
        """
        item_id = listing['physical_item_id']
        current = self.store.registry.detail(actor, item_id)
        if (current['owner_user_id'] != listing['customer_id'] or current['sales_state'] != 'listed'
                or current['custodian'] != 'kaika'):
            return
        listings = self.store.query('SELECT listing_id FROM stage4_channel_listings WHERE physical_item_id=?', (item_id,))
        tasks = self._stop_rows(item_id=item_id)
        covered = {task['listing_id'] for task in tasks}
        if any(row['listing_id'] not in covered for row in listings) or any(task['status'] != 'confirmed_stopped' for task in tasks):
            return
        self._sync_legacy(actor, item_id, current['owner_user_id'], 'listing_stopped', when, evidence,
            'channel-legacy-stop:' + source_key)
        self.store.execute("UPDATE business_item_profiles SET sales_state='unlisted' WHERE physical_item_id=? AND sales_state='listed'", (item_id,))

    def update_task(self, actor, task_id, *, status, note, evidence_reference, occurred_at, source_key, assignee_user_id=None, deadline_at=None):
        self.store.authorize(actor, staff_required=True, finance=True)
        task = self._one('stage4_order_tasks', 'task_id', task_id)
        order = self._order(actor, task['order_id'], write=True)
        if status not in ('pending', 'working', 'done', 'blocked'):
            raise RegistryError('invalid_fulfillment_task_status')
        when, note, evidence = self._when(occurred_at), _text(note, 'task_note'), _text(evidence_reference, 'task_evidence')
        deadline = _time(deadline_at) if deadline_at else None
        if assignee_user_id is not None:
            users = self.store.query('SELECT role FROM users WHERE id=?', (assignee_user_id,))
            if not users or users[0]['role'] not in ('owner', 'admin'):
                raise RegistryError('task_staff_assignee_required')
        payload = dict(action='fulfillment_task', task=task_id, status=status, note=note, evidence=evidence,
                       when=when, assignee=assignee_user_id, deadline=deadline)
        def create():
            history = self.store.query('SELECT * FROM stage4_order_task_events WHERE task_id=? ORDER BY revision', (task_id,))
            if when < (history[-1]['occurred_at'] if history else task['created_at']):
                raise RegistryError('task_time_reversed')
            if task['kind'] == 'shipping' and status == 'done' and not self.store.query('SELECT shipment_id FROM stage4_order_shipments WHERE order_id=?', (order['order_id'],)):
                raise RegistryError('shipment_evidence_required', status=409)
            record = dict(event_id=_id('te4'), task_id=task_id, revision=len(history) + 1, status=status, note=note,
                assignee_user_id=assignee_user_id, deadline_at=deadline, evidence_reference=evidence, occurred_at=when,
                recorded_at=_now(), actor_user_id=actor.user_id, source_key=source_key)
            self._insert('stage4_order_task_events', record)
            return record
        return self._operation(actor, source_key, payload, create, customer_id=order['customer_id'])

    def _assert_active_order(self, actor, order):
        listing = self._listing(actor, order['listing_id'], write=True)
        self._current_owner(actor, listing)
        claim = self.store.query('SELECT order_id FROM stage4_order_claims WHERE physical_item_id=?', (order['physical_item_id'],))
        if order['initial_status'] != 'accepted' or not claim or claim[0]['order_id'] != order['order_id']:
            raise RegistryError('order_not_active', status=409)
        if self.store.query('SELECT return_id FROM stage4_order_returns WHERE order_id=?', (order['order_id'],)):
            raise RegistryError('order_returned_or_cancelled', status=409)
        return listing

    def ship_order(self, actor, order_id, *, tracking_reference, occurred_at, source_key, evidence_reference):
        order = self._order(actor, order_id, write=True)
        when, evidence, tracking = self._when(occurred_at), _text(evidence_reference, 'shipment_evidence'), _text(tracking_reference, 'tracking_reference')
        payload = dict(action='ship_order', order=order_id, when=when, evidence=evidence, tracking=tracking)
        def create():
            prior = self.store.query('SELECT * FROM stage4_order_shipments WHERE order_id=?', (order_id,))
            if prior:
                if prior[0]['payload_hash'] != _hash(payload):
                    raise RegistryError('shipment_record_conflict', status=409)
                return self._decode_order(order)
            listing = self._assert_active_order(actor, order)
            stops = self._stop_rows(item_id=order['physical_item_id'])
            outstanding = [task for task in stops if task['status'] != 'confirmed_stopped']
            if outstanding:
                raise RegistryError('other_listing_stop_unconfirmed', '他の出品の停止確認がそろってから発送してください。', 409)
            if any(when < task['latest_at'] for task in stops):
                raise RegistryError('shipment_before_listing_stop_confirmation', '出品停止を確認した後の発送日時を記録してください。', 409)
            approved = json.loads(listing['approved_snapshot_json'])
            corrections = self.store.query('SELECT sale_gross_yen FROM stage4_order_corrections WHERE order_id=? ORDER BY revision DESC LIMIT 1', (order_id,))
            amount = corrections[0]['sale_gross_yen'] if corrections else order['sale_gross_yen']
            if amount != approved['price_gross_yen']:
                raise RegistryError('order_price_requires_reapproval', status=409)
            self.store.registry.record_event(actor, order['physical_item_id'], 'shipped', when,
                'channel-shipment:' + order_id, evidence, {'tracking_reference': tracking})
            drafts = self.billing.create_item_drafts(actor, order['physical_item_id'], source_key='channel-charge-drafts:' + order_id)
            self._sync_legacy(actor, order['physical_item_id'], order['customer_id'], 'shipped', when, evidence, 'channel-legacy-shipment:' + order_id)
            self._insert('stage4_order_shipments', dict(shipment_id=_id('sh4'), order_id=order_id, tracking_reference=tracking,
                occurred_at=when, recorded_at=_now(), actor_user_id=actor.user_id, evidence_reference=evidence,
                payload_hash=_hash(payload), draft_result_json=_json(drafts), source_key=source_key))
            return self._decode_order(order)
        return self._operation(actor, source_key, payload, create, customer_id=order['customer_id'])

    def return_order(self, actor, order_id, *, reason, occurred_at, source_key, evidence_reference):
        return self._return_or_cancel(actor, order_id, 'returned', reason, occurred_at, source_key, evidence_reference)

    def cancel_order(self, actor, order_id, *, reason, occurred_at, source_key, evidence_reference):
        return self._return_or_cancel(actor, order_id, 'sale_cancelled', reason, occurred_at, source_key, evidence_reference)

    def _return_or_cancel(self, actor, order_id, kind, reason, occurred_at, source_key, evidence_reference):
        order = self._order(actor, order_id, write=True)
        reason, when, evidence = _text(reason, 'return_reason'), self._when(occurred_at), _text(evidence_reference, 'return_evidence')
        payload = dict(action=kind, order=order_id, reason=reason, when=when, evidence=evidence)
        def create():
            prior = self.store.query('SELECT * FROM stage4_order_returns WHERE order_id=?', (order_id,))
            if prior:
                if prior[0]['payload_hash'] != _hash(payload):
                    raise RegistryError('return_record_conflict', status=409)
                return self._decode_order(order)
            self._assert_active_order(actor, order)
            self.store.registry.record_event(actor, order['physical_item_id'], kind, when, 'channel-return:' + order_id, evidence,
                {'return_reason': reason} if kind == 'returned' else {'note': reason})
            self.profit.record_sales_status(actor, order['physical_item_id'], status='return_hold', occurred_at=when,
                source_key='channel-return-review:' + order_id, source_reference=evidence)
            self._sync_legacy(actor, order['physical_item_id'], order['customer_id'], kind, when, evidence, 'channel-legacy-return:' + order_id)
            self._insert('stage4_order_returns', dict(return_id=_id('rt4'), order_id=order_id, kind=kind, reason=reason,
                occurred_at=when, recorded_at=_now(), actor_user_id=actor.user_id, evidence_reference=evidence,
                source_key=source_key, payload_hash=_hash(payload)))
            self.store.execute('DELETE FROM stage4_order_claims WHERE physical_item_id=? AND order_id=?', (order['physical_item_id'], order_id))
            return self._decode_order(order)
        return self._operation(actor, source_key, payload, create, customer_id=order['customer_id'])

    def _decode_order(self, row):
        result = dict(row)
        result['raw_snapshot'] = json.loads(result.pop('raw_snapshot_json'))
        result['corrections'] = self.store.query('SELECT * FROM stage4_order_corrections WHERE order_id=? ORDER BY revision', (row['order_id'],))
        shipments = self.store.query('SELECT * FROM stage4_order_shipments WHERE order_id=?', (row['order_id'],))
        returns = self.store.query('SELECT * FROM stage4_order_returns WHERE order_id=?', (row['order_id'],))
        result['shipment'] = shipments[0] if shipments else None
        if result['shipment']:
            result['shipment']['draft_result'] = json.loads(result['shipment'].pop('draft_result_json'))
        result['return_record'] = returns[0] if returns else None
        result['status'] = returns[0]['kind'] if returns else 'shipped' if shipments else row['initial_status']
        result['current_sale_gross_yen'] = result['corrections'][-1]['sale_gross_yen'] if result['corrections'] else row['sale_gross_yen']
        result['tasks'] = self.store.query('SELECT * FROM stage4_order_tasks WHERE order_id=? ORDER BY kind', (row['order_id'],))
        for task in result['tasks']:
            task['history'] = self.store.query('SELECT * FROM stage4_order_task_events WHERE task_id=? ORDER BY revision', (task['task_id'],))
            task['status'] = task['history'][-1]['status'] if task['history'] else 'pending'
            task['latest'] = task['history'][-1] if task['history'] else None
        result.update(real_payment_enabled=False, purchase_funds_credit_yen=0, external_connection_enabled=False, mode='test_only')
        return result

    def overview(self, actor, customer_id=None):
        staff = self.store.authorize(actor, customer_id)
        customer = customer_id if staff else actor.user_id
        params, where = (() , '') if customer is None else ((customer,), ' WHERE customer_id=?')
        listings = [self._decode_listing(row, actor) for row in self.store.query('SELECT * FROM stage4_channel_listings' + where + ' ORDER BY occurred_at,listing_id', params)]
        orders = [self._decode_order(row) for row in self.store.query('SELECT * FROM stage4_channel_orders' + where + ' ORDER BY occurred_at,order_id', params)]
        ids = {listing['listing_id'] for listing in listings}
        tasks = [task for task in self._stop_rows() if task['listing_id'] in ids]
        return dict(listings=listings, orders=orders, stop_tasks=tasks, execution_mode='test_only', external_connection_enabled=False,
                    real_payment_enabled=False, manual_work_required=True)

    def get_order(self, actor, order_id):
        return self._decode_order(self._order(actor, order_id))

    def get_listing(self, actor, listing_id):
        return self._decode_listing(self._listing(actor, listing_id), actor)

    def preview_order_csv(self, actor, content):
        """Our own UTF-8 worksheet, not a vendor's CSV format or permission."""
        self.store.authorize(actor, staff_required=True, finance=True)
        if not isinstance(content, bytes) or len(content) > 2 * 1024 * 1024:
            raise RegistryError('invalid_order_csv_size')
        try:
            reader = csv.DictReader(io.StringIO(content.decode('utf-8-sig')))
            if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
                raise RegistryError('invalid_order_csv_columns')
            rows, seen = [], set()
            for line, row in enumerate(reader, 2):
                if len(rows) >= 200:
                    raise RegistryError('order_csv_too_many_rows')
                errors = []
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise RegistryError('invalid_order_csv_row')
                    self._listing(actor, row['listing_id'], write=True)
                    amount = row['sale_gross_yen']
                    if not amount.isascii() or not amount.isdecimal() or not yen(int(amount)):
                        raise RegistryError('invalid_order_csv_amount')
                    row['sale_gross_yen'] = int(amount)
                    row['occurred_at'] = self._when(row['occurred_at'])
                    row['external_order_id'] = _text(row['external_order_id'], 'external_order_id', 300)
                    row['evidence_reference'] = _text(row['evidence_reference'], 'order_evidence')
                    key = (row['listing_id'], row['external_order_id'])
                    if key in seen:
                        raise RegistryError('duplicate_csv_order')
                    seen.add(key)
                except RegistryError as error:
                    errors.append(error.code)
                rows.append(dict(line=line, values=row, errors=errors))
        except (UnicodeError, csv.Error):
            raise RegistryError('invalid_order_csv_encoding') from None
        return dict(rows=rows, valid=all(not row['errors'] for row in rows), provider_format=False,
                    import_performed=False, external_connection_enabled=False, mode='test_only')
