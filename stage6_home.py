"""Honest home projections for current customers and authorized staff.

Read-only. No network, migration, customer terms, charges or balances change.
Recorded calculations are not provider-verified funds; test orders stay separate.
"""
from item_registry import Actor, RegistryError, _now
from stage3_billing import BillingService
from stage3_funds import FundsService
from stage3_profit import ProfitService
from stage4_channels import ChannelService
from stage4_listings import ListingService
from stage5_payments import PhysicalPaymentService
from stage6_events import EventScanner, current_items, effective_deadline
from stage6_actions import CustodyActionService
from stage6_presentation import organize_tasks


def _total(values):
    """A known subtotal must not impersonate an unknown complete total."""
    values = list(values)
    known = [value for value in values if value is not None]
    return dict(amount_yen=sum(known) if values and len(known) == len(values) else None,
        known_subtotal_yen=sum(known) if known else None, known_count=len(known),
        unknown_count=len(values)-len(known), record_count=len(values), currency='JPY')


class HomeService:
    def __init__(self, store, clock=None, pre_due_days=7, sandbox_payments_enabled=True):
        self.store = store
        self.clock = clock or _now
        self.scanner = EventScanner(store, clock=self.clock, pre_due_days=pre_due_days,
            sandbox_payments_enabled=sandbox_payments_enabled)

    def _items(self, actor, customer_id=None):
        result = []
        for raw in current_items(self.store, actor, customer_id):
            # Even a staff-viewed customer home uses the customer's safe projection.
            item = self.store.registry.detail(Actor(raw['owner_user_id']), raw['physical_item_id'])
            keys = ('physical_item_id', 'title', 'label_code', 'external_label', 'owner_user_id',
                'custodian', 'physical_state', 'sales_state', 'workflow_state', 'held',
                'identity_status', 'received_at', 'registration_completed_at', 'sold_at', 'shipped_at')
            safe = {key: item[key] for key in keys}
            safe.update(ownership_assignment_id=raw['ownership_assignment_id'], deadline_at=effective_deadline(raw),
                deadline_status='confirmed' if effective_deadline(raw) else 'unknown',
                path='/inventory/items/' + raw['physical_item_id'])
            result.append(safe)
        return result

    def customer(self, actor, customer_id=None):
        staff = self.store.authorize(actor, customer_id, finance=True)
        if customer_id is None:
            if staff:
                raise RegistryError('customer_scope_required', status=400)
            customer_id = actor.user_id
        items = self._items(actor, customer_id)
        profits, actual, estimates, test = [], [], [], []
        profit_service = ProfitService(self.store.finance)
        for item in items:
            projection = profit_service.profit(actor, item['physical_item_id'])
            facts = projection['facts']
            test_orders = self.store.query('''SELECT o.order_id FROM stage4_channel_orders o
                JOIN stage4_channel_listings l ON l.listing_id=o.listing_id
                WHERE o.physical_item_id=? AND l.ownership_assignment_id=? AND o.mode='test_only' ''',
                (item['physical_item_id'], item['ownership_assignment_id']))
            # Stage4 writes synthetic order facts into the existing profit ledger.
            # Until a separate source provenance is available, the whole ownership
            # period with a test order is conservatively excluded from actual KPIs.
            mode = 'test' if test_orders else 'record_only'
            row = {key: projection[key] for key in ('recognition', 'profit_excluding_monthly_yen',
                'profit_including_monthly_yen', 'realized_profit_yen', 'estimated_profit_yen',
                'sales_margin_percent', 'cost_return_percent', 'monthly_sales_margin_percent',
                'monthly_cost_return_percent', 'sale_payment_status', 'review_reasons', 'missing_amounts')}
            row.update(physical_item_id=item['physical_item_id'], title=item['title'],
                facts={key: facts.get(key) for key in ('purchase_cost_yen', 'sale_gross_yen', 'channel_fee_yen',
                    'shipping_cost_yen', 'materials_yen', 'kaika_fee_yen', 'return_adjustment_yen', 'monthly_allocation_yen')},
                execution_mode=mode, provider_verified=False, path='/finance/items/' + item['physical_item_id'])
            profits.append(row)
            if mode == 'test':
                test.append(row)
            elif facts['basis'] == 'actual' and facts.get('sale_event_id'):
                actual.append(row)
            else:
                estimates.append(row)
        funds = FundsService(self.store.finance)
        from stage8_self_sales import self_sale_reports
        self_sales = self_sale_reports(self.store, items,
            excluded_item_ids={row['physical_item_id'] for row in profits if row['facts']['sale_gross_yen'] is not None or row['execution_mode']=='test'})
        money = {
            'self_reported_sales': _total(row['sale_gross_yen'] for row in self_sales),
            'self_sale_reports': self_sales,
            'actual_purchase_available_yen': None, 'actual_unsettled_sales_yen': None,
            'actual_unpaid_fees_yen': None, 'provider_verified': False,
            'actual_money_status': '実残高・精算・実請求は未接続です。入力記録やテスト資金から推定しません。',
            'recorded_sales': _total(row['facts']['sale_gross_yen'] if row['recognition'] != 'return_hold'
                and 'financial_sale_event_stale' not in row['review_reasons'] else None for row in actual),
            'recorded_realized_profit': _total(row['realized_profit_yen'] for row in actual),
            'estimated_profit': _total(row['estimated_profit_yen'] for row in estimates),
            'test_sales': _total(row['facts']['sale_gross_yen'] for row in test),
            'test_profit': _total(row['profit_excluding_monthly_yen'] for row in test),
            'record_only_ledger': funds.summary(actor, customer_id, mode='record_only'),
            'test_ledger': funds.summary(actor, customer_id, mode='test'),
            'profit_scope': '現所有期間の入力記録。商品利益は月額を除外。決済・入金の確認とは別です。',
            'sales_margin_definition': '商品利益 ÷ 売上 × 100（売上に対する割合）',
            'cost_return_definition': '商品利益 ÷ 仕入原価 × 100（原価に対する手残り率）',
        }
        billing = BillingService(self.store.finance)
        invoices = []
        seen = set()
        for row in billing.list_months(actor, customer_id):
            group = (row['period'], row['timezone_name'])
            if group in seen:
                continue
            seen.add(group)
            current = billing.get_month(actor, row['invoice_id'])
            invoices.append(dict(row, current_review_reasons=current['current_review_reasons'],
                is_current_snapshot=current['is_current_snapshot'], real_payment_enabled=False,
                path='/finance/months/' + row['invoice_id']))
        next_actions = list(self.scanner.scan(actor, customer_id))
        for item in items:
            missing = []
            if item['identity_status'] != 'verified':
                missing.append('個品の照合')
            if item['custodian'] == 'kaika' and item['deadline_status'] == 'unknown':
                missing.append('適用される預かり期限')
            if item['held']:
                missing.append('保留理由の確認')
            if missing:
                next_actions.append(dict(event_type='information_required', physical_item_id=item['physical_item_id'],
                    title='商品の要確認事項', body='・'.join(missing), path=item['path'],
                    customer_id=customer_id, execution_mode='record_only', external_delivery_allowed=False))
        payments = PhysicalPaymentService(self.store).overview(actor, customer_id)
        by_id = {item['physical_item_id']: item for item in items}
        for action in next_actions:
            item = by_id.get(action.get('physical_item_id'), {})
            action['item_title'] = item.get('title') or ''
        priority = {'payment_failed': 0, 'storage_due': 1, 'return_selection': 1,
                    'listing_approval': 2, 'storage_pre_due': 3, 'invoice_draft': 4}
        next_actions.sort(key=lambda action: priority.get(action['event_type'], 5))
        custody_requests = [request for item in items for request in CustodyActionService(self.store).list_for_item(actor, item['physical_item_id'])]
        return dict(customer_id=customer_id, items=items, money=money, profit_items=profits,
            next_actions=next_actions, invoices=invoices, test_payment_bills=payments['bills'],
            custody_requests=custody_requests,
            generated_at=self.clock(), scope='current_ownership', live_execution_enabled=False,
            active_inventory_count=sum(item['sales_state'] not in ('sold', 'shipped') for item in items),
            sold_history_count=sum(item['sales_state'] in ('sold', 'shipped') for item in items))

    def staff(self, actor):
        self.store.authorize(actor, staff_required=True)
        financial_access = True
        try:
            self.store.finance.authorize(actor)
        except RegistryError as error:
            if error.code != 'finance_permission_required':
                raise
            financial_access = False
        items = self._items(actor)
        tasks = []
        for item in items:
            kinds = []
            if item['physical_state'] in ('expected', 'unknown'):
                kinds.append(('receipt', '受領・現物の確認'))
            if item['held'] and item['custodian'] != 'customer':
                kinds.append(('hold', '保留理由の確認'))
            if item['custodian'] == 'kaika' and item['physical_state'] == 'received' and not item['registration_completed_at']:
                kinds.append(('registration', '検品・撮影・登録'))
            if item['custodian'] == 'kaika' and item['physical_state'] == 'received' and not item['deadline_at']:
                kinds.append(('deadline_review', '契約の預かり期限を確認'))
            if item['sales_state'] == 'sold' and item['custodian'] == 'kaika':
                kinds.append(('shipping', '発送の確認'))
            for kind, title in kinds:
                tasks.append(dict(kind=kind, title=title, physical_item_id=item['physical_item_id'],
                    customer_id=item['owner_user_id'], path=item['path'], status='pending',
                    assignee_user_id=None, deadline_at=None, assignment_status='未割当', execution_mode='record_only'))
            versions = self.store.query('''SELECT version_id FROM stage4_listing_versions
                WHERE ownership_assignment_id=? ORDER BY revision_number DESC LIMIT 1''', (item['ownership_assignment_id'],))
            if versions:
                proposal = ListingService(self.store, clock=self.clock).detail(actor, versions[0]['version_id'])
                if 'listing_kaika_approval_required' in proposal['review_reasons']:
                    tasks.append(dict(kind='approval', title='開花側の出品内容確認', physical_item_id=item['physical_item_id'],
                        customer_id=item['owner_user_id'], path='/commerce/items/' + item['physical_item_id'] + '/listing',
                        status='needs_review', review_reasons=proposal['review_reasons'], assignee_user_id=None,
                        deadline_at=proposal['valid_until'], assignment_status='未割当', execution_mode='test'))
        channels = ChannelService(self.store, clock=self.clock).overview(actor)
        current_assignments = {item['physical_item_id']: item['ownership_assignment_id'] for item in items}
        current_owners = {item['physical_item_id']: item['owner_user_id'] for item in items}
        listing_assignments = {row['listing_id']: row['ownership_assignment_id'] for row in channels['listings']}
        for task in channels['stop_tasks']:
            if task['status'] == 'confirmed_stopped' or task['physical_item_id'] not in current_owners:
                continue
            tasks.append(dict(kind='channel_stop', title='販路の出品停止を確認',
                task_id=task['task_id'], physical_item_id=task['physical_item_id'],
                customer_id=current_owners[task['physical_item_id']], path='/commerce/channels',
                status='blocked', assignee_user_id=None, deadline_at=None,
                assignment_status='未割当', execution_mode='test'))
        for order in channels['orders']:
            if current_assignments.get(order['physical_item_id']) != listing_assignments.get(order['listing_id']):
                continue
            if order['status'] in ('returned', 'sale_cancelled'):
                continue
            if any(task['kind']=='shipping' for task in order['tasks']):
                tasks = [task for task in tasks if not (task['kind']=='shipping'
                    and task['physical_item_id']==order['physical_item_id'] and 'task_id' not in task)]
            for task in order['tasks']:
                if task['status'] == 'done' or (task['kind'] == 'shipping' and order['shipment']):
                    continue
                latest = task['latest'] or {}
                tasks.append(dict(kind=task['kind'], task_id=task['task_id'], title={
                    'customer_response': '注文の顧客対応', 'packing': '梱包', 'shipping': '発送',
                    'conflict_resolution': '注文の競合解消'}[task['kind']],
                    physical_item_id=order['physical_item_id'], customer_id=current_owners[order['physical_item_id']],
                    path='/commerce/channels', status=task['status'], assignee_user_id=latest.get('assignee_user_id'),
                    deadline_at=latest.get('deadline_at'), assignment_status='割当済み' if latest.get('assignee_user_id') else '未割当',
                    execution_mode='test'))
        for event in self.scanner.scan(actor):
            if event['event_type'] in ('storage_pre_due', 'storage_due', 'invoice_draft', 'payment_failed'):
                deadline = next((item['deadline_at'] for item in items if item['physical_item_id']==event.get('physical_item_id')),None)
                tasks.append(dict(event, kind=event['event_type'], status='needs_review', assignee_user_id=None,
                    deadline_at=deadline, assignment_status='未割当'))
        custody_requests = CustodyActionService(self.store).overview(actor)
        for request in custody_requests:
            if request['current_source'] and request['status'] != 'resolved':
                tasks.append(dict(kind='custody_request', title='返送・別販路の相談受付',
                    physical_item_id=request['physical_item_id'], customer_id=request['customer_id'],
                    request_id=request['request_id'], path='/notifications/items/' + request['physical_item_id'] + '/choice', status=request['status'],
                    assignee_user_id=None, deadline_at=request['deadline_at'], assignment_status='未割当',
                    execution_mode='record_only'))
        tasks = organize_tasks(tasks, items, self.clock())
        return dict(items=items, tasks=tasks, generated_at=self.clock(), live_execution_enabled=False,
            custody_requests=custody_requests, financial_access=financial_access,
            attention_count=sum(task['attention'] in ('blocked', 'overdue') for task in tasks),
            shipping_count=sum(task['kind'] == 'shipping' for task in tasks),
            unassigned_count=sum(task['assignee_user_id'] is None for task in tasks),
            current_customer_count=len({item['owner_user_id'] for item in items}))
