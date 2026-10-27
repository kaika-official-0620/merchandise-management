"""Disposable memory fixtures; no app import, credentials, network or real money."""
import sqlite3
import unittest

from item_registry import Actor, Registry, RegistryError
from item_registry_schema import migrate_registry
from stage3_common import migration_statements as finance_schema
from stage3_funds_schema import migration_statements as funds_schema
from stage3_profit_schema import migration_statements as profit_schema
from stage3_billing_schema import migration_statements as billing_schema
from stage4_common import migration_statements as commerce_schema
from stage4_catalog_schema import migration_statements as catalog_schema
from stage4_listings_schema import migration_statements as listing_schema
from stage4_channels_schema import migration_statements as channel_schema
from stage5_common import migration_statements as payment_common_schema
from stage5_payments_schema import migration_statements as payment_schema
from stage6_common import Stage6Store, migration_statements as common_schema
from stage6_actions_schema import migration_statements as action_schema
from stage3_profit import ProfitService
from stage3_funds import FundsService
from stage3_billing import BillingService
from stage4_listings import ListingService
from stage4_channels import ChannelService
from stage6_home import HomeService, _total


STAFF, CUSTOMER, OTHER, INVENTORY_STAFF = Actor(9, True), Actor(1), Actor(2), Actor(8, True)


def time(minute=0):
    return '2026-10-22T01:%02d:00+00:00' % minute


def fixture():
    db = sqlite3.connect(':memory:')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT)')
    db.executemany('INSERT INTO users VALUES(?,?,?)', [(1,'user',None),(2,'user',None),(9,'owner',None),(8,'admin','["merchandise"]')])
    db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,product_name TEXT)')
    migrate_registry(db)
    for schema in (finance_schema, funds_schema, profit_schema, billing_schema, commerce_schema,
                   catalog_schema, listing_schema, channel_schema, payment_common_schema, payment_schema,
                   common_schema, action_schema):
        for sql in schema():
            db.execute(sql)
    return db


class HomeFixture:
    def init_home(self):
        self.db = fixture()
        self.addCleanup(self.db.close)
        self.registry = Registry(self.db)
        self.store = Stage6Store(self.registry)
        self.now = time(50)
        self.home = HomeService(self.store, clock=lambda: self.now)
        self.profit = ProfitService(self.store.finance)
        self.item = self.registry.create_item(CUSTOMER, '自分の商品', occurred_at=time(), evidence_reference='fixture', source_key='item')['physical_item_id']

    def assertCode(self, code, callback):
        with self.assertRaises(RegistryError) as error:
            callback()
        self.assertEqual(error.exception.code, code)

    def event(self, kind, minute=1, **details):
        return self.registry.record_event(STAFF, self.item, kind, time(minute), 'event-' + kind + str(minute), 'fixture-event', details)

    def register(self):
        for minute, kind in enumerate(('received','photographed','registration_completed'), 1):
            self.event(kind, minute)

    def deadline(self, value='2026-10-22T01:50:00+00:00', key='deadline'):
        return self.registry.set_deadline(STAFF, self.item, deadline_at=value, evidence_reference='fixture-contract', source_key=key)

    def propose(self, channels=None):
        self.register()
        assignment = self.registry._roles(self.item)['owner']['assignment_id']
        self.db.execute('''INSERT INTO stage4_media(media_id,physical_item_id,ownership_assignment_id,storage_path,
            content_sha256,byte_size,actor_user_id,recorded_at) VALUES(?,?,?,?,?,?,?,?)''',
            ('photo',self.item,assignment,'fixture/photo.jpg','a'*64,100,9,time(5)))
        service = ListingService(self.store, clock=lambda: self.now)
        proposal = service.propose(CUSTOMER, self.item, title='safe title', description='safe description', condition='used',
            photo_references=['photo'], price_gross_yen=40000, minimum_price_gross_yen=35000,
            channels=channels or [dict(channel='wholesale', account_id='fixture-account')], valid_until='2026-11-22T00:00:00+00:00',
            occurred_at=time(10), source_key='listing', evidence_reference='fixture-proposal')
        return service, proposal

    def channel_order(self):
        service = ChannelService(self.store,clock=lambda:self.now)
        account = service.configure_account(STAFF,channel='wholesale',display_name='fixture',source_key='account',evidence_reference='fixture')
        listings,proposal = self.propose(channels=[dict(channel='wholesale',account_id=account['account_id'])])
        for actor in (CUSTOMER,STAFF):
            listings.approve(actor,proposal['version_id'],source_key='approve-'+str(actor.user_id),evidence_reference='fixture',occurred_at=time(11))
        listing=service.record_listing(STAFF,proposal['version_id'],account['account_id'],external_listing_id='listing',occurred_at=time(12),source_key='record-listing',evidence_reference='fixture')
        order=service.record_order(STAFF,listing['listing_id'],external_order_id='order',sale_gross_yen=40000,occurred_at=time(13),source_key='order',evidence_reference='fixture')
        return service,order

    def facts(self, **changes):
        params = dict(amounts=dict(purchase_cost_yen=30000, sale_gross_yen=40000, channel_fee_yen=4000,
            materials_yen=100, kaika_fee_yen=1100, return_adjustment_yen=0, monthly_allocation_yen=None),
            channel='multichannel', source_reference='fixture-amounts', occurred_at=time(10), source_key='facts',
            shipping_entries=[dict(source_id='shipping',amount_yen=800,origin='staff',source_reference='fixture-shipping')],
            channel_fee_includes_shipping=False)
        params.update(changes)
        return self.profit.record_facts(STAFF, self.item, **params)


class HomeTests(HomeFixture, unittest.TestCase):
    def setUp(self):
        self.init_home()

    def test_empty_money_and_unentered_values_are_unknown_not_zero(self):
        result = self.home.customer(CUSTOMER)
        for key in ('actual_purchase_available_yen','actual_unsettled_sales_yen','actual_unpaid_fees_yen'):
            self.assertIsNone(result['money'][key])
        self.assertIsNone(result['money']['recorded_sales']['amount_yen'])
        self.assertIsNone(result['money']['estimated_profit']['amount_yen'])
        self.assertEqual(result['money']['estimated_profit']['unknown_count'], 1)
        self.assertFalse(result['live_execution_enabled'])

    def test_record_only_and_test_funds_never_become_actual_balance(self):
        funds = FundsService(self.store.finance)
        for mode, amount in [('test', 5000), ('record_only', 7000)]:
            funds.post(STAFF, 1, kind='deposit',amount_yen=amount,mode=mode,occurred_at=time(),source_key=mode,evidence_reference='fixture')
        money = self.home.customer(CUSTOMER)['money']
        self.assertEqual(money['test_ledger']['balances_yen']['purchase_available'], 5000)
        self.assertEqual(money['record_only_ledger']['balances_yen']['purchase_available'], 7000)
        self.assertIsNone(money['actual_purchase_available_yen'])
        self.assertFalse(money['provider_verified'])

    def test_explicit_zero_known_and_empty_unknown(self):
        self.assertIsNone(_total([])['amount_yen'])
        self.assertEqual(_total([0])['amount_yen'], 0)
        self.assertEqual(_total([10, None])['known_subtotal_yen'], 10)
        self.assertIsNone(_total([10, None])['amount_yen'])

    def test_estimated_profit_separate_from_sales_and_monthly(self):
        self.facts(basis='estimate')
        result = self.home.customer(CUSTOMER)
        self.assertEqual(result['money']['estimated_profit']['amount_yen'], 4000)
        self.assertIsNone(result['money']['recorded_sales']['amount_yen'])
        row = result['profit_items'][0]
        self.assertEqual(row['sales_margin_percent'], '10.00')
        self.assertEqual(row['cost_return_percent'], '13.33')
        self.assertIsNone(row['profit_including_monthly_yen'])

    def test_recorded_profit_requires_existing_settlement_evidence(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        self.facts()
        for minute, status in enumerate(('unpaid','paid','settlement_ready'), 20):
            self.profit.record_sales_status(STAFF,self.item,status=status,occurred_at=time(minute),source_key=status,source_reference='fixture')
        money = self.home.customer(CUSTOMER)['money']
        self.assertEqual(money['recorded_realized_profit']['amount_yen'], 4000)
        self.assertEqual(money['recorded_sales']['amount_yen'], 40000)
        self.assertIsNone(money['actual_purchase_available_yen'])

    def test_return_is_not_reported_as_realized_sale_or_profit(self):
        self.event('sold', 1)
        self.event('shipped', 2)
        self.facts()
        self.event('returned', 11, return_reason='fixture return')
        money = self.home.customer(CUSTOMER)['money']
        self.assertIsNone(money['recorded_realized_profit']['amount_yen'])
        self.assertIsNone(money['recorded_sales']['amount_yen'])

    def test_unknown_cost_does_not_show_a_partial_profit_as_total(self):
        self.facts(amounts=dict(purchase_cost_yen=30000,sale_gross_yen=40000))
        self.assertIsNone(self.home.customer(CUSTOMER)['money']['estimated_profit']['amount_yen'])

    def test_other_customer_scope_and_forged_staff_denied(self):
        self.assertCode('customer_access_denied',lambda:self.home.customer(OTHER,1))
        self.assertCode('staff_permission_required',lambda:self.home.customer(Actor(2,True),1))
        self.assertEqual(self.home.customer(OTHER)['items'],[])

    def test_staff_customer_view_is_safe_and_explicit_scope_required(self):
        self.assertCode('customer_scope_required',lambda:self.home.customer(STAFF))
        result = self.home.customer(STAFF,1)
        self.assertNotIn('assignment_history',result['items'][0])
        self.assertNotIn('imported_fields',result['items'][0])
        self.assertNotIn('evidence_reference',result['profit_items'][0]['facts'])

    def test_ownership_change_hides_previous_financial_facts_and_deadline(self):
        self.register()
        self.deadline()
        self.facts()
        self.registry.change_assignment(STAFF,self.item,'owner','customer',2,occurred_at=time(20),evidence_reference='ownership',source_key='transfer')
        self.assertEqual(self.home.customer(CUSTOMER)['items'],[])
        next_owner = self.home.customer(OTHER)
        self.assertIsNone(next_owner['items'][0]['deadline_at'])
        self.assertIsNone(next_owner['profit_items'][0]['facts']['purchase_cost_yen'])

    def test_customer_cannot_read_staff_queue(self):
        self.assertCode('staff_permission_required',lambda:self.home.staff(CUSTOMER))

    def test_inventory_only_staff_sees_tasks_not_financial_items(self):
        self.register()
        BillingService(self.store.finance).create_month(STAFF,1,'2026-10',source_key='month')
        result = self.home.staff(INVENTORY_STAFF)
        self.assertFalse(result['financial_access'])
        self.assertTrue(result['tasks'])
        self.assertFalse(any(task['kind']=='invoice_draft' for task in result['tasks']))
        self.assertCode('finance_permission_required',lambda:self.home.customer(INVENTORY_STAFF,1))

    def test_unknown_term_shown_as_action_without_invented_date(self):
        self.register()
        result = self.home.customer(CUSTOMER)
        self.assertEqual(result['items'][0]['deadline_status'],'unknown')
        self.assertTrue(any('預かり期限' in action['body'] for action in result['next_actions']))
        self.assertFalse(any(action['event_type']=='storage_due' for action in result['next_actions']))

    def test_invoice_latest_revision_only_and_never_actual_debt(self):
        billing = BillingService(self.store.finance)
        old = billing.create_month(STAFF,1,'2026-10',source_key='old')
        billing.record_component(STAFF,1,fee_kind='expense',amount_net_yen=500,tax_rate_percent=10,
            occurred_at=time(10),evidence_reference='new-receipt',source_key='receipt')
        new = billing.create_month(STAFF,1,'2026-10',source_key='new')
        result = self.home.customer(CUSTOMER)
        self.assertEqual([row['invoice_id'] for row in result['invoices']], [new['invoice_id']])
        self.assertNotEqual(old['invoice_id'],new['invoice_id'])
        self.assertIsNone(result['money']['actual_unpaid_fees_yen'])

    def test_sold_inventory_separate_from_active_count(self):
        self.event('sold')
        result = self.home.customer(CUSTOMER)
        self.assertEqual(result['active_inventory_count'],0)
        self.assertEqual(result['sold_history_count'],1)

    def test_reads_do_not_modify_source_records(self):
        before = self.db.total_changes
        self.home.customer(CUSTOMER)
        self.home.staff(STAFF)
        self.assertEqual(self.db.total_changes,before)

    def test_stage4_test_order_never_counts_as_actual_sales(self):
        self.channel_order()
        money=self.home.customer(CUSTOMER)['money']
        self.assertEqual(money['test_sales']['amount_yen'],40000)
        self.assertIsNone(money['recorded_sales']['amount_yen'])
        self.assertIsNone(money['recorded_realized_profit']['amount_yen'])

    def test_staff_existing_assignment_deadline_preserved_without_duplicate_shipping(self):
        service,order=self.channel_order()
        task=next(task for task in order['tasks'] if task['kind']=='shipping')
        service.update_task(STAFF,task['task_id'],status='working',note='準備中',evidence_reference='fixture',occurred_at=time(20),
            source_key='assign',assignee_user_id=8,deadline_at='2026-10-23T01:00:00+00:00')
        tasks=[task for task in self.home.staff(INVENTORY_STAFF)['tasks'] if task['kind']=='shipping']
        self.assertEqual(len(tasks),1)
        self.assertEqual(tasks[0]['assignee_user_id'],8)
        self.assertEqual(tasks[0]['deadline_at'],'2026-10-23T01:00:00.000000+00:00')

    def test_old_ownership_order_tasks_do_not_follow_new_owner(self):
        service,order=self.channel_order()
        self.registry.change_assignment(STAFF,self.item,'owner','customer',2,occurred_at=time(20),evidence_reference='ownership',source_key='transfer')
        tasks=self.home.staff(STAFF)['tasks']
        self.assertFalse(any(task.get('task_id') in {row['task_id'] for row in order['tasks']} for task in tasks))
        self.assertIsNone(self.home.customer(OTHER)['money']['test_sales']['amount_yen'])


if __name__ == '__main__':
    unittest.main()
