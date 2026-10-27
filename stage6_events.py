"""Read-only, authorization-aware notification sources; never sends messages.

The scanner owns event identity. HTTP form fields are not notification facts.
Every event is re-resolved against current ownership, versions and deadlines.
"""
from datetime import datetime, timedelta
import hashlib

from item_registry import RegistryError, _json, _now, _time
from stage3_billing import BillingService
from stage4_listings import ListingService


EVENT_TEXT = {
    'listing_approval': ('出品内容の確認', 'アプリで出品案を確認してください。'),
    'storage_pre_due': ('預かり期限が近づいています', 'アプリで適用される期限と今後の対応を確認してください。'),
    'storage_due': ('預かり期限の確認', 'アプリで今後の販売・返送について確認してください。'),
    'return_selection': ('販売・返送方法の確認', 'アプリから開花への相談が必要な商品を確認してください。'),
    'invoice_draft': ('請求下書きの確認', 'アプリで明細と要確認事項を確認してください。まだ実請求ではありません。'),
    'payment_failed': ('検証用決済の確認', '検証用の決済記録に確認が必要です。実際の引落しはありません。'),
}


def _date(value):
    return datetime.fromisoformat(_time(value))


def current_items(store, actor, customer_id=None, item_id=None):
    """Select active ownership, not a cached item customer or initial owner."""
    staff = store.authorize(actor, customer_id)
    customer = customer_id if staff else actor.user_id
    sql = '''SELECT p.*,a.assignment_id AS ownership_assignment_id,o.user_id AS owner_user_id,
        c.kind AS custodian_kind FROM business_item_profiles p
        JOIN business_item_assignments a ON a.physical_item_id=p.physical_item_id AND a.role='owner' AND a.ended_at IS NULL
        JOIN business_parties o ON o.party_id=a.party_id AND o.kind='customer'
        LEFT JOIN business_item_assignments ca ON ca.physical_item_id=p.physical_item_id AND ca.role='custodian' AND ca.ended_at IS NULL
        LEFT JOIN business_parties c ON c.party_id=ca.party_id'''
    params = ()
    if customer is not None:
        sql += ' WHERE o.user_id=?'
        params = (customer,)
    if item_id is not None:
        sql += (' AND ' if customer is not None else ' WHERE ') + 'p.physical_item_id=?'
        params += (item_id,)
    return store.query(sql + ' ORDER BY p.physical_item_id', params)


def effective_deadline(item):
    # A previous customer's term is not inherited after an ownership change.
    if (item['deadline_status'] != 'confirmed' or not item['deadline_at'] or
            item['deadline_customer_id'] != item['owner_user_id'] or not item['deadline_evidence']):
        return None
    try:
        return _time(item['deadline_at'])
    except RegistryError:
        return None


def sandbox_payment_attention(store, customer_id=None, job_id=None):
    """Read only safe status fields from an explicitly migrated test ledger.

    Stage9 is optional. Catalog checks avoid a missing-table error aborting an
    older PostgreSQL transaction; there are no imports that initialize a PSP.
    Provider identifiers, payloads, card/consent references and amounts never
    enter notification sources.
    """
    names = ('stage9_payment_jobs', 'stage9_payment_scope', 'stage9_payment_customers')
    if store.dialect == 'postgres':
        tables = store.query("SELECT table_name AS name FROM information_schema.tables WHERE table_schema=current_schema() AND table_name IN (?,?,?)", names)
    else:
        tables = store.query("SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?)", names)
    if {row['name'] for row in tables} != set(names):
        return []
    from stage9_payment_schema import VERSION
    scope = store.query('SELECT sandbox_id,schema_version,environment FROM stage9_payment_scope')
    if len(scope) != 1 or scope[0]['schema_version'] != VERSION or scope[0]['environment'] != 'test':
        return []
    sql = '''SELECT j.job_id,j.customer_id,j.kind,j.state FROM stage9_payment_jobs j
        JOIN stage9_payment_customers c ON c.customer_id=j.customer_id AND c.sandbox_id=j.sandbox_id
        WHERE j.sandbox_id=? AND j.environment='test' AND c.enabled=1
        AND j.state IN ('requires_action','requires_payment_method','requires_confirmation','requires_capture','unknown','failed')'''
    params = (scope[0]['sandbox_id'],)
    if customer_id is not None:
        sql += ' AND j.customer_id=?'
        params += (customer_id,)
    if job_id is not None:
        sql += ' AND j.job_id=?'
        params += (job_id,)
    return store.query(sql + ' ORDER BY j.job_id', params)


class EventScanner:
    def __init__(self, store, clock=None, pre_due_days=7, *, sandbox_payments_enabled=True):
        if type(pre_due_days) is not int or not 0 <= pre_due_days <= 90:
            raise ValueError('Pre-due window must be an integer between 0 and 90')
        if type(sandbox_payments_enabled) is not bool:
            raise ValueError('Explicit sandbox payment route availability required')
        self.store = store
        self.clock = clock or _now
        self.pre_due_days = pre_due_days
        self.sandbox_payments_enabled = sandbox_payments_enabled

    def _event(self, event_type, source_type, source_id, version, customer_id,
               path, item=None, mode='record_only'):
        source_version = hashlib.sha256(_json(version).encode()).hexdigest()
        identity = dict(event_type=event_type, source_type=source_type, source_id=source_id,
            source_version=source_version, customer_id=customer_id,
            ownership_assignment_id=item['ownership_assignment_id'] if item else None)
        return dict(identity, dedupe_key='stage6:' + hashlib.sha256(_json(identity).encode()).hexdigest(),
            physical_item_id=item['physical_item_id'] if item else None,
            title=EVENT_TEXT[event_type][0], body=EVENT_TEXT[event_type][1], path=path,
            execution_mode=mode, external_delivery_allowed=False)

    def scan(self, actor, customer_id=None):
        return self._scan(actor, customer_id)

    def _scan(self, actor, customer_id=None, source=None):
        staff = self.store.authorize(actor, customer_id)
        financial_access = True
        try:
            self.store.finance.authorize(actor, customer_id)
        except RegistryError as error:
            if not staff or error.code != 'finance_permission_required':
                raise
            financial_access = False
        customer = customer_id if staff else actor.user_id
        now = _date(self.clock())
        events = []
        listings = ListingService(self.store, clock=self.clock)
        item_scope = None
        if source and source[0]=='listing':
            rows=self.store.query('SELECT physical_item_id FROM stage4_listing_versions WHERE version_id=?',(source[1],))
            if not rows:
                return []
            item_scope=rows[0]['physical_item_id']
        elif source and source[0]=='storage_deadline':
            item_scope=source[1]
        items=[] if source and source[0] not in ('listing','storage_deadline') else current_items(self.store, actor, customer, item_scope)
        for item in items:
            item_id, owner = item['physical_item_id'], item['owner_user_id']
            versions = self.store.query('''SELECT version_id FROM stage4_listing_versions
                WHERE ownership_assignment_id=? ORDER BY revision_number DESC LIMIT 1''', (item['ownership_assignment_id'],))
            if versions and (not source or source[0]=='listing'):
                proposal = listings.detail(actor, versions[0]['version_id'], at=self.clock())
                reasons = set(proposal['review_reasons'])
                blocked = reasons - {'listing_customer_approval_required', 'listing_kaika_approval_required'}
                if 'listing_customer_approval_required' in reasons and not blocked:
                    events.append(self._event('listing_approval', 'listing', proposal['version_id'],
                        [proposal['version_id'], proposal['content_hash']], owner,
                        '/commerce/items/' + item_id + '/listing', item, mode='test'))
            # No arbitrary two-month term; only confirmed current-customer terms.
            deadline = effective_deadline(item)
            present = item['custodian_kind'] == 'kaika' and item['physical_state'] == 'received'
            if deadline and present and item['sales_state'] not in ('sold', 'shipped') and (not source or source[0]=='storage_deadline'):
                from stage6_actions import CustodyActionService, deadline_version
                choice = CustodyActionService(self.store).latest_for_item(item)
                # A recorded choice stays visible in its own request history.
                # Do not keep urging a second choice while it is being handled.
                if choice and choice['current_source']:
                    continue
                dt = _date(deadline)
                event_type = 'storage_due' if dt <= now else 'storage_pre_due' if dt <= now + timedelta(days=self.pre_due_days) else None
                if event_type:
                    version = [deadline_version(self.store, item), choice['request_id'] if choice else None]
                    events.append(self._event(event_type, 'storage_deadline', item_id, version, owner,
                        '/inventory/items/' + item_id, item))
                    if event_type == 'storage_due':
                        events.append(self._event('return_selection', 'storage_deadline', item_id, version, owner,
                            '/notifications/items/' + item_id + '/choice', item))
        if not financial_access or source and source[0] in ('listing','storage_deadline'):
            return sorted(events, key=lambda event: (event['customer_id'], event['event_type'], event['source_id']))
        params, where = ((customer,), ' WHERE i.customer_id=?') if customer is not None else ((), '')
        if source and source[0]=='invoice':
            where += (' AND ' if where else ' WHERE ') + 'i.invoice_id=?'
            params += (source[1],)
        invoices = [] if source and source[0]!='invoice' else self.store.query('''SELECT i.* FROM stage3_invoice_revisions i''' + where +
            ((' AND ' if where else ' WHERE ') + '''NOT EXISTS (SELECT 1 FROM stage3_invoice_revisions n
            WHERE n.customer_id=i.customer_id AND n.period=i.period AND n.timezone_name=i.timezone_name AND n.revision>i.revision)'''), params)
        billing = BillingService(self.store.finance)
        for invoice in invoices:
            current = billing.get_month(actor, invoice['invoice_id'])
            if current['is_current_snapshot']:
                events.append(self._event('invoice_draft', 'invoice', invoice['invoice_id'],
                    [invoice['invoice_id'], invoice['revision'], current['current_review_reasons']], invoice['customer_id'],
                    '/finance/months/' + invoice['invoice_id'], mode='test'))
        params, where = ((customer,), ' AND a.customer_id=?') if customer is not None else ((), '')
        if source and source[0]=='payment_attempt':
            where += ' AND a.attempt_id=?'
            params += (source[1],)
        attempts = [] if source and source[0]!='payment_attempt' else self.store.query('''SELECT a.attempt_id,a.customer_id,e.event_id,e.state FROM stage5_payment_attempts a
            JOIN stage5_payment_events e ON e.attempt_id=a.attempt_id
            WHERE NOT EXISTS (SELECT 1 FROM stage5_payment_events n WHERE n.attempt_id=e.attempt_id AND n.sequence>e.sequence)''' + where, params)
        for attempt in attempts:
            if attempt['state'] in ('failed', 'requires_action', 'unknown'):
                events.append(self._event('payment_failed', 'payment_attempt', attempt['attempt_id'],
                    [attempt['event_id'], attempt['state']], attempt['customer_id'],
                    '/payments/attempts/' + attempt['attempt_id'], mode='test'))
        if self.sandbox_payments_enabled and (not source or source[0] == 'sandbox_payment'):
            for job in sandbox_payment_attention(self.store, customer, source[1] if source else None):
                # A repeated poll/webhook in the same state is one notice.
                # Success, cancellation or account pause invalidates its link.
                events.append(self._event('payment_failed', 'sandbox_payment', job['job_id'],
                    [job['job_id'], job['kind'], job['state']], job['customer_id'],
                    '/payments/sandbox/' + job['job_id'], mode='test'))
        return sorted(events, key=lambda event: (event['customer_id'], event['event_type'], event['source_id']))

    def resolve(self, actor, dedupe_key, customer_id=None):
        for event in self.scan(actor, customer_id):
            if event['dedupe_key'] == dedupe_key:
                return event
        raise RegistryError('notification_source_no_longer_current', 'このお知らせの対象は変更済みです。最新の一覧を確認してください。', 404)

    def is_current(self, actor, event):
        if not isinstance(event, dict):
            return False
        customer = event.get('customer_id')
        if type(customer) is not int:
            return False
        source_type,source_id=event.get('source_type'),event.get('source_id')
        if source_type not in ('listing','storage_deadline','invoice','payment_attempt','sandbox_payment') or not isinstance(source_id,str):
            return False
        try:
            current = next((value for value in self._scan(actor, customer, (source_type,source_id))
                if value['dedupe_key']==event.get('dedupe_key')),None)
        except RegistryError:
            return False
        if current is None:
            return False
        # Binding more than a caller-provided hash also prevents path substitution.
        keys = ('customer_id', 'event_type', 'source_type', 'source_id', 'source_version',
            'dedupe_key', 'ownership_assignment_id', 'physical_item_id', 'path',
            'execution_mode', 'external_delivery_allowed')
        return all(current.get(key) == event.get(key) for key in keys)
