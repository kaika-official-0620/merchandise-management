import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from item_registry import Actor, Registry, RegistryError
from item_registry_schema import migrate_registry
from stage3_common import migration_statements as finance_sql
from stage3_profit_schema import migration_statements as profit_sql
from stage3_billing_schema import migration_statements as billing_sql
from stage3_funds_schema import migration_statements as funds_sql
from stage4_common import Stage4Store, migration_statements as common_sql
from stage4_listings import ListingService
from stage4_listings_schema import migration_statements as listings_sql
from stage4_channels import ChannelService, CAPABILITIES, CSV_COLUMNS
from stage4_channels_schema import migration_statements


def time(minute=0):
    return '2026-10-22T10:%02d:00+00:00' % minute


class ChannelTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT)')
        self.db.executemany('INSERT INTO users VALUES (?,?,?)', [(1,'user',None),(2,'user',None),(9,'owner',None),(8,'admin','["merchandise"]')])
        self.db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),product_name TEXT)')
        migrate_registry(self.db)
        for statement in finance_sql()+list(profit_sql())+list(billing_sql())+list(funds_sql())+common_sql()+list(listings_sql())+migration_statements():
            self.db.execute(statement)
        self.registry = Registry(self.db)
        self.store = Stage4Store(self.registry)
        self.clock = lambda: '2026-10-22T11:00:00+00:00'
        self.service, self.listings = ChannelService(self.store, self.clock), ListingService(self.store, self.clock)
        self.staff, self.customer, self.other = Actor(9, True), Actor(1), Actor(2)
        self.item = self.registry.create_item(self.customer, '検証商品', occurred_at=time(), evidence_reference='fixture', source_key='item')['physical_item_id']
        for minute, kind in enumerate(('received','photographed','registration_completed'),1):
            self.registry.record_event(self.staff,self.item,kind,time(minute),kind,'fixture')
        assignment = self.store.query("SELECT assignment_id FROM business_item_assignments WHERE physical_item_id=? AND role='owner' AND ended_at IS NULL", (self.item,))[0]['assignment_id']
        self.db.execute('INSERT INTO stage4_media VALUES(?,?,?,?,?,?,?,?)', ('photo',self.item,assignment,'fixture.jpg','a'*64,123,9,time(3)))
        self.accounts = [self.service.configure_account(self.staff, channel=channel, display_name=channel,
            source_key='account-'+channel, evidence_reference='test-only') for channel in ('mercari_shops','yahoo_store')]
        self.version = self.listings.propose(self.customer, self.item, title='出品題名',description='記録した説明',condition='中古',
            photo_references=['photo'],price_gross_yen=40000,minimum_price_gross_yen=35000,
            channels=[dict(channel=account['channel'],account_id=account['account_id']) for account in self.accounts],
            valid_until='2026-10-23T10:00:00+00:00',source_key='proposal',evidence_reference='fixture',occurred_at=time(4))

    def approve(self):
        for minute,actor in ((5,self.customer),(6,self.staff)):
            self.listings.approve(actor,self.version['version_id'],source_key='approve-'+str(actor.user_id),evidence_reference='fixture',occurred_at=time(minute))

    def listing(self,index=0,key=None,**changes):
        values=dict(external_listing_id='external-'+str(index),occurred_at=time(7),source_key=key or 'listing-'+str(index),evidence_reference='fixture-listing')
        values.update(changes)
        return self.service.record_listing(self.staff,self.version['version_id'],self.accounts[index]['account_id'],**values)

    def ready(self,two=True):
        self.approve()
        first=self.listing()
        if two:
            self.listing(1)
        return first

    def order(self,listing=None,key='order',**changes):
        if listing is None:
            listing=self.ready()
        values=dict(external_order_id='external-order',sale_gross_yen=40000,occurred_at=time(8),source_key=key,evidence_reference='fixture-order')
        values.update(changes)
        return self.service.record_order(self.staff,listing['listing_id'],**values)

    def stop_all(self,minute=9):
        for task in self.service.overview(self.staff)['stop_tasks']:
            if task['status'] != 'confirmed_stopped':
                self.service.stop_task(self.staff,task['task_id'],status='confirmed_stopped',evidence_reference='fixture-stop',occurred_at=time(minute),source_key='stop-'+task['task_id'])

    def ship(self,order=None,key='ship',**changes):
        order=order or self.order()
        self.stop_all()
        values=dict(tracking_reference='fixture-tracking',occurred_at=time(10),source_key=key,evidence_reference='fixture-shipment')
        values.update(changes)
        return self.service.ship_order(self.staff,order['order_id'],**values)

    def code(self,code,callback):
        with self.assertRaises(RegistryError) as error:
            callback()
        self.assertEqual(error.exception.code,code)

    def test_capabilities_default_unknown_all_live_disabled(self):
        account=self.accounts[0]
        self.assertEqual(set(account['capabilities']),set(CAPABILITIES))
        self.assertTrue(all(v['status']=='unverified' and not v['enabled'] for v in account['capabilities'].values()))
        self.assertFalse(account['external_connection_enabled'])

    def test_documented_capability_does_not_connect_api(self):
        updated=self.service.review_account(self.staff,self.accounts[0]['account_id'],capabilities={'api':dict(status='documented',evidence_reference='official-answer')},evidence_reference='fixture',source_key='review')
        self.assertEqual(updated['capabilities']['api']['status'],'documented')
        self.assertFalse(updated['capabilities']['api']['enabled'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_channel_reviews').fetchone()[0],3)

    def test_documented_capability_requires_evidence(self):
        self.code('invalid_capability_evidence',lambda:self.service.review_account(self.staff,self.accounts[0]['account_id'],capabilities={'api':dict(status='documented')},evidence_reference='fixture',source_key='review'))

    def test_customer_account_list_exposes_only_public_target_names(self):
        accounts=self.service.accounts(self.customer)
        self.assertEqual(set(accounts[0]),{'account_id','channel','display_name'})
        self.assertNotIn('evidence_reference',json.dumps(accounts))

    def test_personal_mercari_not_supported(self):
        self.code('unsupported_channel',lambda:self.service.configure_account(self.staff,channel='mercari_personal',display_name='x',source_key='personal',evidence_reference='x'))

    def test_listing_requires_both_approvals(self):
        with self.assertRaises(RegistryError):
            self.listing()
        self.listings.approve(self.customer,self.version['version_id'],source_key='approve-customer',evidence_reference='x',occurred_at=time(5))
        with self.assertRaises(RegistryError):
            self.listing()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_channel_listings').fetchone()[0],0)

    def test_listing_uses_exact_same_approved_snapshot_across_channels(self):
        first=self.ready()
        second=self.service.overview(self.staff)['listings'][1]
        self.assertEqual(first['approved_snapshot']['content_hash'],second['approved_snapshot']['content_hash'])
        self.assertEqual(self.registry.detail(self.customer,self.item)['sales_state'],'listed')
        self.assertFalse(first['external_connection_enabled'])

    def test_listing_expiry_uses_current_clock_not_backdated_input(self):
        self.approve()
        self.service.clock=lambda:'2026-10-24T11:00:00+00:00'
        self.code('listing_expired',lambda:self.listing())

    def test_listing_before_approval_rejected(self):
        self.approve()
        self.code('listing_time_outside_approval',lambda:self.listing(occurred_at=time(4)))

    def test_listing_repeated_id_same_payload_replays_not_second_record(self):
        self.approve()
        first=self.listing()
        second=self.listing(key='listing-again')
        self.assertEqual(first['listing_id'],second['listing_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_channel_listings').fetchone()[0],1)

    def test_listing_same_external_id_changed_payload_conflicts(self):
        self.approve();self.listing()
        self.code('external_listing_conflict',lambda:self.listing(key='changed',evidence_reference='changed'))

    def test_order_creates_one_sale_unpaid_and_unknown_costs_no_fund_credit(self):
        order=self.order()
        self.assertEqual(order['status'],'accepted')
        profit=self.service.profit.profit(self.customer,self.item)
        self.assertEqual(profit['facts']['sale_gross_yen'],40000)
        self.assertIsNone(profit['facts']['purchase_cost_yen'])
        self.assertIsNone(profit['facts']['kaika_fee_yen'])
        self.assertEqual(profit['sale_payment_status'],'unpaid')
        self.assertEqual(profit['purchase_funds_credit_yen'],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_fund_records').fetchone()[0],0)

    def test_order_duplicate_source_or_external_id_never_doubles_sale(self):
        listing=self.ready();first=self.order(listing)
        again=self.order(listing,key='again')
        self.assertEqual(first['order_id'],again['order_id'])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM business_item_observations WHERE kind='sold'").fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_item_financial_facts').fetchone()[0],1)

    def test_order_same_external_id_different_amount_requires_correction(self):
        listing=self.ready();self.order(listing)
        self.code('external_order_conflict',lambda:self.order(listing,key='again',sale_gross_yen=39000))

    def test_second_channel_order_conflict_recorded_without_second_sale(self):
        listing=self.ready();first=self.order(listing)
        second_listing=next(row for row in self.service.overview(self.staff)['listings'] if row['listing_id'] != listing['listing_id'])
        second=self.order(second_listing,key='order2',external_order_id='second-order',occurred_at=time(9))
        self.assertEqual(second['status'],'conflict')
        self.assertEqual(second['conflicting_order_id'],first['order_id'])
        self.assertEqual(second['tasks'][0]['kind'],'conflict_resolution')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM business_item_observations WHERE kind='sold'").fetchone()[0],1)
        self.code('order_not_active',lambda:self.ship(second))

    def test_price_outside_approved_version_is_conflict_even_above_minimum(self):
        listing=self.ready();order=self.order(listing,sale_gross_yen=38000)
        self.assertEqual(order['conflict_reason'],'sale_price_differs_from_approved_version')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM business_item_observations WHERE kind='sold'").fetchone()[0],0)

    def test_order_blocks_all_local_listings_and_creates_stop_queue(self):
        self.order()
        view=self.service.overview(self.staff)
        self.assertEqual(len(view['stop_tasks']),2)
        self.assertTrue(all(row['local_sale_blocked'] for row in view['listings']))
        self.assertTrue(all(task['status']=='pending' for task in view['stop_tasks']))

    def test_stop_fail_retry_confirm_preserves_all_evidence(self):
        self.order();task=self.service.overview(self.staff)['stop_tasks'][0]
        for minute,status in enumerate(('failed','retry_requested','confirmed_stopped'),9):
            result=self.service.stop_task(self.staff,task['task_id'],status=status,evidence_reference='fixture',occurred_at=time(minute),source_key=status)
            self.assertFalse(result['external_request_sent'])
        found=next(row for row in self.service.overview(self.staff)['stop_tasks'] if row['task_id']==task['task_id'])
        self.assertEqual([row['status'] for row in found['history']],['failed','retry_requested','confirmed_stopped'])
        self.code('listing_stop_already_confirmed',lambda:self.service.stop_task(self.staff,task['task_id'],status='failed',evidence_reference='x',occurred_at=time(12),source_key='failagain'))

    def test_shipping_blocked_until_other_listing_stop_confirmed(self):
        order=self.order()
        self.code('other_listing_stop_unconfirmed',lambda:self.service.ship_order(self.staff,order['order_id'],tracking_reference='x',occurred_at=time(10),source_key='ship',evidence_reference='x'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0],0)

    def test_shipment_records_one_event_and_review_drafts_without_payment(self):
        order=self.order();shipped=self.ship(order)
        self.assertEqual(shipped['status'],'shipped')
        drafts=self.store.query('SELECT * FROM stage3_charge_drafts')
        self.assertEqual(len(drafts),2)
        self.assertTrue(all(draft['status']=='needs_review' for draft in drafts))
        self.assertFalse(shipped['real_payment_enabled'])
        self.assertEqual(self.ship(order,key='ship-again')['shipment']['shipment_id'],shipped['shipment']['shipment_id'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0],2)

    def test_shipment_duplicate_changed_tracking_rejected(self):
        order=self.order();self.ship(order)
        self.code('shipment_record_conflict',lambda:self.ship(order,key='ship2',tracking_reference='new'))

    def test_returns_keep_item_identity_hold_financial_review_and_release_claim(self):
        order=self.order();self.ship(order)
        returned=self.service.return_order(self.staff,order['order_id'],reason='サイズ相違',occurred_at=time(11),source_key='return',evidence_reference='fixture-return')
        self.assertEqual(returned['status'],'returned')
        item=self.registry.detail(self.customer,self.item)
        self.assertTrue(item['held'])
        self.assertEqual(item['physical_item_id'],self.item)
        self.assertEqual(self.service.profit.profit(self.customer,self.item)['sale_payment_status'],'return_hold')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_order_claims').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0],2)

    def test_cancel_before_shipping_has_no_charge_and_hold_sale_review(self):
        order=self.order()
        result=self.service.cancel_order(self.staff,order['order_id'],reason='注文取消',occurred_at=time(9),source_key='cancel',evidence_reference='fixture')
        self.assertEqual(result['status'],'sale_cancelled')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0],0)
        self.assertEqual(self.service.profit.profit(self.customer,self.item)['recognition'],'return_hold')

    def test_correction_keeps_original_and_requires_review_before_shipping(self):
        order=self.order()
        corrected=self.service.correct_order(self.staff,order['order_id'],sale_gross_yen=39000,reason='外部明細訂正',occurred_at=time(9),evidence_reference='revised-source',source_key='correction')
        self.assertEqual(corrected['sale_gross_yen'],40000)
        self.assertEqual(corrected['raw_snapshot']['sale_gross_yen'],40000)
        self.assertEqual(corrected['current_sale_gross_yen'],39000)
        self.assertEqual(self.service.profit.snapshot(self.customer,self.item)['sale_gross_yen'],39000)
        self.stop_all(9)
        self.code('order_price_requires_reapproval',lambda:self.service.ship_order(self.staff,order['order_id'],tracking_reference='x',occurred_at=time(10),evidence_reference='x',source_key='ship'))

    def test_old_owner_listing_cannot_sell_after_ownership_change(self):
        listing=self.ready()
        self.registry.change_assignment(self.staff,self.item,'owner','customer',2,occurred_at=time(8),evidence_reference='transfer',source_key='transfer')
        self.code('channel_owner_changed',lambda:self.order(listing,occurred_at=time(9)))
        self.assertEqual(self.service.overview(self.other)['listings'],[])
        self.code('customer_access_denied',lambda:self.service.get_listing(self.other,listing['listing_id']))

    def test_customers_cannot_enter_channel_financial_records(self):
        listing=self.ready()
        with self.assertRaises(RegistryError):
            self.service.record_order(self.customer,listing['listing_id'],external_order_id='x',sale_gross_yen=40000,occurred_at=time(8),source_key='order',evidence_reference='x')
        self.code('finance_permission_required',lambda:self.service.configure_account(Actor(8,True),channel='other',display_name='x',source_key='account',evidence_reference='x'))

    def test_task_assignment_deadline_and_shipping_evidence_are_separate(self):
        order=self.order();task=next(task for task in order['tasks'] if task['kind']=='packing')
        result=self.service.update_task(self.staff,task['task_id'],status='working',note='梱包担当',evidence_reference='fixture',occurred_at=time(9),source_key='packing',assignee_user_id=9,deadline_at=time(12))
        self.assertEqual(result['assignee_user_id'],9)
        self.assertEqual(self.service.get_order(self.customer,order['order_id'])['status'],'accepted')
        shipping=next(task for task in order['tasks'] if task['kind']=='shipping')
        self.code('shipment_evidence_required',lambda:self.service.update_task(self.staff,shipping['task_id'],status='done',note='done',evidence_reference='fixture',occurred_at=time(9),source_key='shipping-task'))

    def test_csv_own_template_preview_never_imports_or_claims_vendor_permission(self):
        listing=self.ready()
        content=(','.join(CSV_COLUMNS)+'\n'+','.join((listing['listing_id'],'test-order','40000',time(8),'fixture'))+'\n').encode()
        result=self.service.preview_order_csv(self.staff,content)
        self.assertTrue(result['valid']);self.assertFalse(result['provider_format']);self.assertFalse(result['import_performed'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_channel_orders').fetchone()[0],0)

    def test_csv_bad_fields_and_duplicate_rows_report_errors(self):
        listing=self.ready()
        row=','.join((listing['listing_id'],'test-order','40000',time(8),'fixture'))+'\n'
        result=self.service.preview_order_csv(self.staff,(','.join(CSV_COLUMNS)+'\n'+row+row).encode())
        self.assertEqual(result['rows'][1]['errors'],['duplicate_csv_order'])
        self.code('invalid_order_csv_columns',lambda:self.service.preview_order_csv(self.staff,b'unknown\nvalue'))

    def test_sql_raw_orders_and_approvals_are_immutable(self):
        order=self.order()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE stage4_channel_orders SET sale_gross_yen=1 WHERE order_id=?',(order['order_id'],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM stage4_channel_accounts')

    def test_sql_cannot_enable_live_mode(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO stage4_channel_accounts VALUES('bad','other','bad',9,?,'x','bad',1)",(time(),))

    def test_approval_expiry_after_publication_blocks_order_and_can_queue_manual_stop(self):
        listing=self.ready()
        self.service.clock=lambda:'2026-10-24T11:00:00+00:00'
        item=self.service.get_listing(self.staff,listing['listing_id'])
        self.assertTrue(item['local_sale_blocked']);self.assertTrue(item['approval_stop_required'])
        audit=self.service.audit_listing_approvals(self.staff,occurred_at='2026-10-24T11:00:00+00:00',evidence_reference='fixture-audit',source_key='audit')
        self.assertEqual(len(audit['tasks']),2)
        self.assertTrue(all(task['order_id'] is None for task in audit['tasks']))
        self.assertEqual(self.order(listing)['status'],'conflict')

    def test_rejection_after_publication_does_not_silently_keep_it_sellable(self):
        listing=self.ready()
        self.listings.reject(self.customer,self.version['version_id'],source_key='reject',evidence_reference='fixture',occurred_at=time(8),reason='状態訂正')
        view=self.service.get_listing(self.customer,listing['listing_id'])
        self.assertIn('listing_rejected',view['approval_review_reasons'])
        self.assertEqual(self.order(listing,occurred_at=time(9))['conflict_reason'],'listing_approval_no_longer_valid')

    def test_concurrent_channel_orders_accept_only_one_physical_item(self):
        self.ready()
        listings=self.service.overview(self.staff)['listings']
        self.db.commit()
        with tempfile.TemporaryDirectory(prefix='kaika-stage4-channel-concurrency-') as folder:
            target=Path(folder)/'fixture.sqlite'
            connection=sqlite3.connect(target)
            self.db.backup(connection);connection.close()
            barrier=threading.Barrier(2)
            def submit(index):
                conn=sqlite3.connect(target,timeout=10)
                conn.execute('PRAGMA foreign_keys=ON')
                try:
                    service=ChannelService(Stage4Store(Registry(conn)),self.clock)
                    barrier.wait(timeout=5)
                    result=service.record_order(self.staff,listings[index]['listing_id'],external_order_id='concurrent-'+str(index),
                        sale_gross_yen=40000,occurred_at=time(9),source_key='concurrent-'+str(index),evidence_reference='fixture')
                    conn.commit()
                    return result['initial_status']
                finally:
                    conn.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                result=list(pool.map(submit,(0,1)))
            self.assertEqual(sorted(result),['accepted','conflict'])
            conn=sqlite3.connect(target)
            try:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM stage4_order_claims').fetchone()[0],1)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM business_item_observations WHERE kind='sold'").fetchone()[0],1)
            finally:
                conn.close()

    def test_sql_order_customer_cannot_disagree_with_listing(self):
        order=self.order()
        values=self.store.query('SELECT * FROM stage4_channel_orders WHERE order_id=?',(order['order_id'],))[0]
        values.update(order_id='wrong-customer',customer_id=2,external_order_id='wrong',source_key='wrong')
        with self.assertRaises(sqlite3.IntegrityError):
            self.service._insert('stage4_channel_orders',values)

    def legacy(self):
        for column,kind in (('scope','TEXT'),('custody_location','TEXT'),('sale_date','TEXT'),('sale_price','INTEGER'),
                            ('is_shipped','INTEGER'),('is_listed','INTEGER'),('listing_date','TEXT'),('listing_price','INTEGER')):
            self.db.execute('ALTER TABLE merchandise ADD COLUMN ' + column + ' ' + kind)
        self.db.execute("INSERT INTO merchandise(id,user_id,product_name,scope,custody_location,sale_price,is_shipped,is_listed) VALUES(11,1,'検証商品','user','kaika',0,0,0)")
        self.registry.attach_legacy(self.staff,11,physical_item_id=self.item,evidence_reference='fixture-link',occurred_at=time(3),source_key='legacy-link')

    def test_linked_legacy_sale_shipment_return_projection_has_immutable_before_images(self):
        self.legacy()
        order=self.order()
        row=self.store.query('SELECT * FROM merchandise WHERE id=11')[0]
        self.assertEqual(row['sale_price'],40000);self.assertEqual(row['sale_date'],'2026-10-22')
        self.ship(order)
        self.assertEqual(self.db.execute('SELECT is_shipped FROM merchandise WHERE id=11').fetchone()[0],1)
        self.service.return_order(self.staff,order['order_id'],reason='返品',occurred_at=time(11),source_key='return',evidence_reference='fixture')
        row=self.store.query('SELECT * FROM merchandise WHERE id=11')[0]
        self.assertEqual(row['sale_price'],0);self.assertIsNone(row['sale_date']);self.assertEqual(row['is_shipped'],0)
        audit=self.store.query("SELECT * FROM stage4_legacy_projections WHERE kind='returned'")[0]
        self.assertEqual(json.loads(audit['before_json'])['sale_price'],40000)
        self.assertEqual(self.service.get_order(self.customer,order['order_id'])['sale_gross_yen'],40000)

    def test_legacy_owner_mismatch_rolls_back_entire_order(self):
        self.legacy();listing=self.ready()
        self.db.execute('UPDATE merchandise SET user_id=2 WHERE id=11')
        self.code('legacy_projection_scope_unverified',lambda:self.order(listing))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_channel_orders').fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM business_item_observations WHERE kind='sold'").fetchone()[0],0)
        self.assertEqual(self.registry.detail(self.customer,self.item)['sales_state'],'listed')

    def test_legacy_admin_scope_never_receives_customer_order_write(self):
        self.legacy();listing=self.ready()
        self.db.execute("UPDATE merchandise SET scope='admin' WHERE id=11")
        self.code('legacy_projection_scope_unverified',lambda:self.order(listing))

    def test_purchased_company_item_reuses_photo_and_registration_without_duplicate_fees(self):
        from stage4_catalog import CatalogService
        from stage4_catalog_schema import migration_statements as catalog_sql
        from stage3_funds import FundsService
        for statement in catalog_sql():
            self.db.execute(statement)
        catalog=CatalogService(self.store,clock=self.clock,ownership_rule='test_payment_confirmation')
        def args(key,minute):
            return dict(source_key=key,evidence_reference='fixture-'+key,occurred_at=time(minute))
        company=catalog.create_company_item(self.staff,title='会社仕入れ',purchase_cost_yen=12000,**args('company',12))
        item_id=company['physical_item_id']
        company_owner=self.registry._roles(item_id)['owner']['assignment_id']
        self.db.execute('INSERT INTO stage4_media VALUES(?,?,?,?,?,?,?,?)',('company-photo',item_id,company_owner,'company.jpg','b'*64,123,9,time(12)))
        revised=catalog.revise(self.staff,item_id,title='顧客向け商品',description='公開商品説明',condition='中古',price_yen=30000,photo_refs=['company-photo'],**args('revise',13))
        offer=catalog.publish(self.staff,item_id,revised['revision_id'],**args('publish',14))
        FundsService(self.store.finance).post(self.staff,1,kind='deposit',amount_yen=100000,**args('fixture-deposit',14))
        reserved=catalog.reserve(self.customer,item_id,offer['revision_id'],custody_choice='kaika',consent_reference='fixture-consent',source_key='reserve',occurred_at=time(15))
        catalog.confirm_test_purchase(self.staff,reserved['order_id'],**args('confirm',16))
        before=self.db.execute('SELECT COUNT(*) FROM business_items').fetchone()[0]
        version=self.listings.propose(self.customer,item_id,title='購入後販売',description='公開商品説明',condition='中古',photo_references=['company-photo'],
            price_gross_yen=40000,minimum_price_gross_yen=35000,channels=[dict(channel=self.accounts[0]['channel'],account_id=self.accounts[0]['account_id'])],
            valid_until='2026-10-23T10:00:00+00:00',**args('purchase-proposal',17))
        self.listings.approve(self.customer,version['version_id'],**args('purchase-customer-approval',18))
        self.listings.approve(self.staff,version['version_id'],**args('purchase-staff-approval',19))
        listing=self.service.record_listing(self.staff,version['version_id'],self.accounts[0]['account_id'],external_listing_id='purchased-external',**args('purchased-listing',20))
        order=self.service.record_order(self.staff,listing['listing_id'],external_order_id='purchased-order',sale_gross_yen=40000,**args('purchased-order',21))
        self.stop_all(22)
        shipped=self.service.ship_order(self.staff,order['order_id'],tracking_reference='fixture-track',**args('purchased-shipment',23))
        self.assertEqual(shipped['status'],'shipped')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_items').fetchone()[0],before)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_work_records WHERE physical_item_id=?',(item_id,)).fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM business_item_observations WHERE physical_item_id=? AND kind='registration_completed'",(item_id,)).fetchone()[0],0)
        self.assertIsNone(self.registry.detail(self.customer,item_id)['registration_completed_at'])
        facts=self.service.profit.snapshot(self.customer,item_id)
        self.assertEqual(facts['purchase_cost_yen'],30000)
        self.assertNotEqual(facts['purchase_cost_yen'],12000)
        self.assertTrue(all(row['status']=='needs_review' for row in self.store.query('SELECT * FROM stage3_charge_drafts WHERE physical_item_id=?',(item_id,))))

    def test_future_business_times_do_not_change_records(self):
        listing=self.ready()
        future='2026-10-23T10:00:00+00:00'
        self.code('channel_time_in_future',lambda:self.order(listing,occurred_at=future))
        order=self.order(listing)
        task=self.service.overview(self.staff)['stop_tasks'][0]
        self.code('channel_time_in_future',lambda:self.service.stop_task(self.staff,task['task_id'],status='confirmed_stopped',evidence_reference='x',occurred_at=future,source_key='future-stop'))
        self.code('channel_time_in_future',lambda:self.service.correct_order(self.staff,order['order_id'],sale_gross_yen=39000,reason='x',occurred_at=future,evidence_reference='x',source_key='future-correction'))
        self.code('channel_time_in_future',lambda:self.service.ship_order(self.staff,order['order_id'],tracking_reference='x',occurred_at=future,evidence_reference='x',source_key='future-ship'))
        self.code('channel_time_in_future',lambda:self.service.return_order(self.staff,order['order_id'],reason='x',occurred_at=future,evidence_reference='x',source_key='future-return'))
        self.code('channel_time_in_future',lambda:self.service.audit_listing_approvals(self.staff,occurred_at=future,evidence_reference='x',source_key='future-audit'))
        self.code('channel_time_in_future',lambda:self.service.update_task(self.staff,order['tasks'][0]['task_id'],status='working',note='x',occurred_at=future,evidence_reference='x',source_key='future-task'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_stop_events').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_order_corrections').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_order_shipments').fetchone()[0],0)

    def test_shipment_cannot_be_backdated_before_stop_confirmation(self):
        order=self.order();self.stop_all(12)
        self.code('shipment_before_listing_stop_confirmation',lambda:self.service.ship_order(self.staff,order['order_id'],tracking_reference='x',occurred_at=time(11),evidence_reference='x',source_key='ship'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0],0)

    def test_draft_failure_rolls_back_shipment_and_can_retry_safely(self):
        order=self.order();self.stop_all()
        original=self.service.billing.create_item_drafts
        def fail(*args,**kwargs):
            raise RegistryError('fixture_draft_failure')
        self.service.billing.create_item_drafts=fail
        self.code('fixture_draft_failure',lambda:self.ship(order))
        self.assertEqual(self.registry.detail(self.customer,self.item)['sales_state'],'sold')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM business_item_observations WHERE kind='shipped'").fetchone()[0],0)
        self.service.billing.create_item_drafts=original
        self.assertEqual(self.ship(order)['status'],'shipped')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0],2)

    def test_all_stop_confirmations_clear_only_unsold_listing_projection(self):
        self.legacy()
        self.ready()
        self.service.request_item_stop(self.staff, self.item, reason='返送', occurred_at=time(8),
            evidence_reference='fixture', source_key='request-stop')
        tasks = self.service.overview(self.staff)['stop_tasks']
        self.service.stop_task(self.staff, tasks[0]['task_id'], status='confirmed_stopped',
            occurred_at=time(9), evidence_reference='fixture', source_key='stop-first')
        self.assertEqual(self.registry.detail(self.staff,self.item)['sales_state'], 'listed')
        self.assertEqual(self.db.execute('SELECT is_listed FROM merchandise WHERE id=11').fetchone()[0], 1)
        self.stop_all(minute=10)
        self.assertEqual(self.registry.detail(self.staff,self.item)['sales_state'], 'unlisted')
        row = self.store.query('SELECT * FROM merchandise WHERE id=11')[0]
        self.assertEqual(row['is_listed'], 0)
        self.assertEqual(row['listing_price'], 40000)
        self.assertEqual(row['listing_date'], '2026-10-22')
        audit = self.store.query("SELECT * FROM stage4_legacy_projections WHERE kind='listing_stopped'")
        self.assertEqual(len(audit), 1)
        self.assertEqual(json.loads(audit[0]['before_json']), {'is_listed':1})
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 0)
        self.code('listing_before_previous_stop_confirmation', lambda: self.listing(
            key='new-publication',external_listing_id='new-publication',occurred_at=time(9)))

    def test_unsold_return_is_blocked_until_every_listing_stop_is_confirmed(self):
        self.ready()
        send = lambda at=time(10): self.registry.record_event(self.staff, self.item,
            'return_shipped', at, 'return-to-owner', 'fixture-return-agreement')
        self.code('return_listing_stop_unconfirmed', send)
        self.assertEqual(self.registry.detail(self.staff, self.item)['physical_state'], 'received')
        requested = self.service.request_item_stop(self.staff, self.item, reason='お客様へ返送',
            occurred_at=time(8), evidence_reference='fixture-agreement', source_key='manual-stop')
        self.assertEqual(len(requested['tasks']), 2)
        self.assertFalse(requested['external_request_sent'])
        self.code('return_listing_stop_unconfirmed', send)
        self.service.stop_task(self.staff, requested['tasks'][0]['task_id'], status='confirmed_stopped',
            occurred_at=time(9), evidence_reference='fixture-first-market', source_key='first-stop')
        self.service.stop_task(self.staff, requested['tasks'][1]['task_id'], status='failed',
            occurred_at=time(9), evidence_reference='fixture-stop-failed', source_key='second-failed')
        self.code('return_listing_stop_unconfirmed', send)
        self.stop_all(minute=12)
        self.code('return_before_listing_stop_confirmation', send)
        sent = send(time(13))
        self.assertEqual(sent['physical_state'], 'return_in_transit')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_fund_records').fetchone()[0], 0)

    def test_manual_stop_request_reuses_tasks_and_late_order_remains_conflict(self):
        listing = self.ready()
        args = dict(reason='返送の合意', occurred_at=time(8), evidence_reference='fixture-agreement')
        self.service.request_item_stop(self.staff, self.item, source_key='manual-stop', **args)
        self.service.request_item_stop(self.staff, self.item, source_key='manual-stop', **args)
        self.service.request_item_stop(self.staff, self.item, source_key='manual-stop-second-form', **args)
        self.assertEqual(len(self.service.overview(self.staff)['stop_tasks']), 2)
        late = self.order(listing, occurred_at=time(9))
        self.assertEqual(late['status'], 'conflict')
        self.assertEqual(late['conflict_reason'], 'listing_already_stopped_or_stop_pending')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_order_claims').fetchone()[0], 0)

    def test_manual_stop_request_requires_staff_and_valid_actual_time(self):
        self.ready()
        args = dict(reason='返送', occurred_at=time(8), evidence_reference='fixture-agreement', source_key='stop')
        self.code('staff_permission_required', lambda: self.service.request_item_stop(self.customer, self.item, **args))
        self.code('stop_before_listing', lambda: self.service.request_item_stop(self.staff, self.item, **{**args, 'occurred_at': time(6)}))
        self.code('channel_time_in_future', lambda: self.service.request_item_stop(self.staff, self.item,
            **{**args, 'occurred_at': '2026-10-24T10:00:00+00:00'}))
        self.assertEqual(len(self.service.overview(self.staff)['stop_tasks']), 0)

    def test_postgres_ddl_uses_integer_money_and_no_runtime_connections(self):
        sql='\n'.join(migration_statements('postgres'))
        self.assertIn('sale_gross_yen BIGINT',sql)
        self.assertNotIn('typeof(',sql)
        self.assertIn('UNIQUE(account_id,external_order_id)',sql)


if __name__=='__main__':
    unittest.main()
