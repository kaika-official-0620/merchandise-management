"""Disposable database tests; no application startup, network or real money."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest

from item_registry import Actor,Registry,RegistryError
from item_registry_schema import migrate_registry
from stage3_common import migration_statements as finance_schema
from stage3_funds_schema import migration_statements as funds_schema
from stage3_funds import FundsService
from stage4_common import Stage4Store,migration_statements as common_schema
from stage4_catalog_schema import migration_statements
from stage4_catalog import CatalogService,_OrderFundsBridge,catalog_registration_proof

T='2026-10-22T01:00:00+00:00'
STAFF=Actor(9,True)
USER=Actor(1)
OTHER=Actor(2)


def fixture(path=':memory:'):
    db=sqlite3.connect(path,timeout=10)
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT)')
    db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER,product_name TEXT)')
    db.executemany('INSERT INTO users VALUES (?,?,?)',[(1,'user',None),(2,'user',None),(9,'owner',None),
        (8,'admin','["merchandise"]'),(7,'admin','["invoices"]')])
    migrate_registry(db,allowed_test_path=path if path!=':memory:' else None)
    for sql in [*finance_schema(),*funds_schema(),*common_schema(),*migration_statements()]:
        db.execute(sql)
    db.commit()
    return db


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.db=fixture()
        self.addCleanup(self.db.close)
        self.store=Stage4Store(Registry(self.db))
        self.now=datetime.fromisoformat(T)
        self.service=CatalogService(self.store,clock=lambda:self.now)
        self.sequence=0

    def args(self,**updates):
        self.sequence+=1
        return dict(occurred_at=self.now,evidence_reference='fixture-evidence',source_key='op-'+str(self.sequence))|updates

    def assertCode(self,code,call):
        with self.assertRaises(RegistryError) as caught:
            call()
        self.assertEqual(code,caught.exception.code)

    def company(self):
        return self.service.create_company_item(STAFF,title='架空商品',purchase_cost_yen=12345,**self.args())['physical_item_id']

    def photo(self,item):
        owner=self.store.registry._roles(item)['owner']['assignment_id']
        media='m-'+str(self.sequence)
        self.db.execute('INSERT INTO stage4_media VALUES (?,?,?,?,?,?,?,?)',(media,item,owner,'fixture/'+media,'a'*64,10,9,T))
        return media

    def offer(self,item=None,price=30000):
        item=item or self.company()
        photo=self.photo(item)
        revision=self.service.revise(STAFF,item,title='販売品',description='確認済みの説明',condition='中古',price_yen=price,photo_refs=[photo],**self.args())
        return self.service.publish(STAFF,item,revision['revision_id'],**self.args())

    def deposit(self,customer=1,amount=100000):
        return FundsService(self.store.finance).post(STAFF,customer,kind='deposit',amount_yen=amount,**self.args())

    def reserve(self,offer,actor=USER,**kwargs):
        args=self.args()
        args.pop('evidence_reference')
        return self.service.reserve(actor,offer['physical_item_id'],offer['revision_id'],custody_choice='kaika',consent_reference='agreed-test-offer',**args,**kwargs)

    def buy(self,offer=None,custody='kaika'):
        offer=offer or self.offer()
        self.deposit()
        args=self.args();args.pop('evidence_reference')
        order=self.service.reserve(USER,offer['physical_item_id'],offer['revision_id'],custody_choice=custody,consent_reference='agreed',**args)
        self.service.ownership_rule='test_payment_confirmation'
        return self.service.confirm_test_purchase(STAFF,order['order_id'],**self.args())

    def test_company_item_has_one_internal_id_without_customer_profile(self):
        item=self.company()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_items').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_item_profiles').fetchone()[0],0)
        self.assertEqual(self.store.registry._roles(item)['owner']['kind'],'kaika')
        self.assertIsNone(self.db.execute('SELECT label_code FROM business_items').fetchone()[0])

    def test_customer_cannot_create_or_read_internal_catalog(self):
        item=self.company()
        self.assertCode('staff_permission_required',lambda:self.service.create_company_item(USER,title='no',**self.args()))
        self.assertCode('staff_permission_required',lambda:self.service.get_company_item(USER,item))
        self.assertCode('staff_permission_required',lambda:self.service.list_company_items(USER))

    def test_public_offer_never_contains_company_cost_or_staff_evidence(self):
        offer=self.offer()
        public=self.service.get_catalog(USER,offer['physical_item_id'])
        text=json.dumps(public)
        for private in ('purchase_cost','12345','evidence_reference','storage_path','actor_user_id','customer_id'):
            self.assertNotIn(private,text)
        self.assertEqual(public['available_count'],1)
        self.assertFalse(public['live_execution_enabled'])

    def test_draft_not_public_and_photo_required_to_publish(self):
        item=self.company()
        self.assertCode('catalog_item_not_found',lambda:self.service.get_catalog(USER,item))
        rev=self.service.revise(STAFF,item,title='品',description='説明',condition='中古',price_yen=10,**self.args())
        self.assertCode('catalog_photo_required',lambda:self.service.publish(STAFF,item,rev['revision_id'],**self.args()))

    def test_wrong_item_photo_rejected(self):
        first=self.company(); photo=self.photo(first);second=self.company()
        self.assertCode('catalog_photo_not_owned',lambda:self.service.revise(STAFF,second,title='品',description='説明',condition='中古',price_yen=10,photo_refs=[photo],**self.args()))

    def test_prices_are_integer_positive_yen(self):
        item=self.company()
        for price in (True,1.1,'100',-1,None,0):
            with self.assertRaises(RegistryError):
                self.service.revise(STAFF,item,title='品',description='説明',condition='中古',price_yen=price,**self.args())

    def test_reservation_locks_exact_offer_and_test_funds(self):
        offer=self.offer();self.deposit()
        order=self.reserve(offer)
        self.assertEqual(order['offer']['price_yen'],30000)
        self.assertEqual(order['status'],'reserved')
        summary=FundsService(self.store.finance).summary(USER,1)['balances_yen']
        self.assertEqual((summary['purchase_available'],summary['purchase_reserved']),(70000,30000))
        self.assertEqual(self.service.get_catalog(OTHER,offer['physical_item_id'])['available_count'],0)

    def test_insufficient_funds_does_not_claim_item(self):
        offer=self.offer();self.deposit(amount=200)
        self.assertCode('fund_insufficient_purchase_available',lambda:self.reserve(offer))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_purchase_orders').fetchone()[0],0)
        self.assertEqual(self.service.get_catalog(USER,offer['physical_item_id'])['available_count'],1)

    def test_other_customer_cannot_buy_claimed_item_or_read_order(self):
        offer=self.offer();self.deposit();self.deposit(customer=2)
        order=self.reserve(offer)
        self.assertCode('catalog_item_unavailable',lambda:self.reserve(offer,OTHER))
        self.assertCode('customer_access_denied',lambda:self.service.get_order(OTHER,order['order_id']))
        self.assertCode('customer_access_denied',lambda:self.service.cancel(OTHER,order['order_id'],**self.args()))
        self.assertEqual(self.service.list_orders(OTHER),[])

    def test_replay_is_once_and_changed_payload_conflicts(self):
        offer=self.offer();self.deposit()
        args=dict(custody_choice='kaika',consent_reference='agreed',occurred_at=self.now,source_key='same-reserve')
        one=self.service.reserve(USER,offer['physical_item_id'],offer['revision_id'],**args)
        self.assertEqual(one,self.service.reserve(USER,offer['physical_item_id'],offer['revision_id'],**args))
        args['custody_choice']='self'
        self.assertCode('source_key_conflict',lambda:self.service.reserve(USER,offer['physical_item_id'],offer['revision_id'],**args))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM stage3_fund_records WHERE kind='reserve'").fetchone()[0],1)

    def test_price_change_requires_new_consent_but_existing_order_keeps_snapshot(self):
        old=self.offer();self.deposit()
        order=self.reserve(old)
        new=self.offer(old['physical_item_id'],price=40000)
        self.assertEqual(self.service.get_order(USER,order['order_id'])['amount_yen'],30000)
        self.service.cancel(USER,order['order_id'],**self.args())
        self.assertCode('catalog_offer_changed',lambda:self.reserve(old))
        self.assertEqual(self.reserve(new)['amount_yen'],40000)

    def test_cancel_releases_exact_original_reservation(self):
        offer=self.offer();self.deposit();order=self.reserve(offer)
        result=self.service.cancel(USER,order['order_id'],**self.args())
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(FundsService(self.store.finance).summary(USER,1)['balances_yen']['purchase_available'],100000)
        self.assertEqual(self.service.get_catalog(USER,offer['physical_item_id'])['available_count'],1)
        self.assertCode('catalog_order_not_reserved',lambda:self.service.cancel(USER,order['order_id'],**self.args()))

    def test_expiry_holds_item_until_authorized_release(self):
        offer=self.offer();self.deposit();order=self.reserve(offer)
        self.now+=timedelta(minutes=31)
        self.assertTrue(self.service.get_order(USER,order['order_id'])['expired_pending_release'])
        self.assertCode('catalog_reservation_expired',lambda:self.service.confirm_test_purchase(STAFF,order['order_id'],**self.args()))
        self.assertCode('staff_permission_required',lambda:self.service.expire_orders(USER,**self.args()))
        self.assertEqual(self.service.expire_orders(STAFF,**self.args())['released_order_ids'],[order['order_id']])
        self.assertEqual(self.service.get_order(USER,order['order_id'])['status'],'expired')
        self.assertEqual(self.service.get_catalog(USER,offer['physical_item_id'])['available_count'],1)

    def test_unresolved_ownership_holds_transfer_after_test_payment(self):
        offer=self.offer();self.deposit();order=self.reserve(offer)
        result=self.service.confirm_test_purchase(STAFF,order['order_id'],**self.args())
        self.assertEqual(result['status'],'paid_ownership_pending')
        self.assertEqual(self.store.registry._roles(offer['physical_item_id'])['owner']['kind'],'kaika')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_item_profiles').fetchone()[0],0)
        self.assertEqual(self.service.get_catalog(USER,offer['physical_item_id'])['available_count'],0)

    def test_test_confirmation_preserves_one_item_and_no_new_work_or_fees(self):
        order=self.buy()
        item=self.store.registry.detail(USER,order['physical_item_id'])
        self.assertEqual(item['owner_user_id'],1)
        self.assertEqual(item['custodian'],'kaika')
        self.assertEqual(item['works'],[])
        self.assertIsNone(item['registration_completed_at'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_items').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_work_records').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_item_events').fetchone()[0],0)
        self.assertEqual(order['status'],'confirmed')

    def test_new_owner_cannot_see_company_history_or_cost(self):
        order=self.buy()
        detail=self.store.registry.detail(USER,order['physical_item_id'])
        text=json.dumps(detail)
        self.assertNotIn('12345',text)
        self.assertNotIn('assignment_history',detail)
        self.assertCode('item_not_found',lambda:self.store.registry.detail(OTHER,order['physical_item_id']))
        self.assertCode('catalog_item_not_found',lambda:self.service.get_catalog(USER,order['physical_item_id']))

    def test_pending_transfer_can_finish_without_second_test_payment(self):
        offer=self.offer();self.deposit();order=self.reserve(offer)
        self.service.confirm_test_purchase(STAFF,order['order_id'],**self.args())
        self.service.ownership_rule='test_payment_confirmation'
        confirmed=self.service.confirm_test_purchase(STAFF,order['order_id'],**self.args())
        self.assertEqual(confirmed['status'],'confirmed')
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM stage3_fund_records WHERE kind='purchase'").fetchone()[0],1)

    def test_customer_or_nonfinance_staff_cannot_confirm_payment(self):
        offer=self.offer();self.deposit();order=self.reserve(offer)
        self.assertCode('staff_permission_required',lambda:self.service.confirm_test_purchase(USER,order['order_id'],**self.args()))
        self.assertCode('finance_permission_required',lambda:self.service.confirm_test_purchase(Actor(8,True),order['order_id'],**self.args()))

    def test_self_choice_does_not_claim_customer_receipt(self):
        order=self.buy(custody='self')
        before=self.store.registry.detail(USER,order['physical_item_id'])
        self.assertEqual(before['custodian'],'kaika')
        self.assertCode('staff_permission_required',lambda:self.service.handover_received(USER,order['order_id'],**self.args()))
        handed=self.service.handover_received(STAFF,order['order_id'],**self.args())
        after=self.store.registry.detail(USER,order['physical_item_id'])
        self.assertEqual((handed['status'],after['custodian'],after['physical_state']),('handed_over','customer','with_customer'))
        self.assertEqual(after['assignments']['sales_operator']['user_id'],1)

    def test_kaika_choice_cannot_be_marked_handed_home(self):
        order=self.buy()
        self.assertCode('catalog_handover_not_ready',lambda:self.service.handover_received(STAFF,order['order_id'],**self.args()))

    def test_revisions_orders_and_events_are_append_only(self):
        offer=self.offer();self.deposit();self.reserve(offer)
        for table in ('stage4_company_items','stage4_catalog_revisions','stage4_purchase_orders','stage4_catalog_events'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('DELETE FROM '+table)

    def test_bridge_rejects_different_actor_item_customer_and_deposit(self):
        item=self.company()
        expected={'kind':'deposit','mode':'test'}
        bridge=_OrderFundsBridge(self.store,USER,1,item,'only',expected)
        self.assertCode('catalog_funds_scope_denied',lambda:bridge.authorize(OTHER,1))
        self.assertCode('catalog_funds_scope_denied',lambda:bridge.authorize(USER,2))
        self.assertCode('catalog_funds_item_denied',lambda:bridge.item(USER,'other'))
        self.assertCode('catalog_funds_operation_denied',lambda:bridge.operation(USER,1,'funds:only',expected,lambda:None))

    def test_postgres_sql_is_generation_only(self):
        sql=';'.join(migration_statements('postgres'))
        self.assertIn('BIGINT',sql)
        self.assertNotIn('RAISE(ABORT',sql)
        self.assertIn('execution_mode',sql)

    def test_reused_registration_has_no_new_count_and_stops_after_owner_change(self):
        order=self.buy()
        item=order['physical_item_id']
        proof=catalog_registration_proof(self.store,item,1)
        self.assertEqual(proof['order_id'],order['order_id'])
        self.assertEqual(proof['revision_id'],order['revision_id'])
        self.assertEqual(proof['photo_refs'],order['offer']['photo_refs'])
        self.assertEqual(proof['registration_count_increment'],0)
        self.assertIsNone(catalog_registration_proof(self.store,item,2))
        self.store.registry.change_assignment(STAFF,item,'owner','customer',2,**self.args())
        self.assertIsNone(catalog_registration_proof(self.store,item,1))
        self.assertIsNone(catalog_registration_proof(self.store,item,2))

    def test_pending_order_does_not_grant_registration_proof(self):
        offer=self.offer();self.deposit();order=self.reserve(offer)
        self.service.confirm_test_purchase(STAFF,order['order_id'],**self.args())
        self.assertIsNone(catalog_registration_proof(self.store,offer['physical_item_id'],1))

    def test_transfer_failure_rolls_back_purchase_and_keeps_reservation(self):
        offer=self.offer();self.deposit();order=self.reserve(offer)
        item=order['physical_item_id']
        # Conflict simulates an unexpected independent legacy enrollment.
        self.db.execute('''INSERT INTO business_item_profiles(physical_item_id,title,origin,created_at,initial_customer_id)
            VALUES (?,?,?,?,?)''',(item,'conflict','manual',T,1))
        self.service.ownership_rule='test_payment_confirmation'
        self.assertCode('catalog_ownership_conflict',lambda:self.service.confirm_test_purchase(STAFF,order['order_id'],**self.args()))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM stage3_fund_records WHERE kind='purchase'").fetchone()[0],0)
        self.assertEqual(FundsService(self.store.finance).summary(USER,1)['balances_yen']['purchase_reserved'],30000)
        self.assertEqual(self.service.get_order(USER,order['order_id'])['status'],'reserved')

    def test_future_occurrence_not_accepted_from_caller(self):
        self.assertCode('catalog_time_in_future',lambda:self.service.create_company_item(STAFF,title='future',
            **self.args(occurred_at=self.now+timedelta(minutes=1))))

    def test_paid_pending_cannot_be_transferred_before_payment_time(self):
        offer=self.offer();self.deposit();order=self.reserve(offer)
        self.now+=timedelta(minutes=2)
        self.service.confirm_test_purchase(STAFF,order['order_id'],**self.args())
        self.service.ownership_rule='test_payment_confirmation'
        self.assertCode('catalog_time_reversed',lambda:self.service.confirm_test_purchase(STAFF,order['order_id'],**self.args(occurred_at=T)))

    def test_handover_does_not_rewind_later_workflow_event(self):
        order=self.buy(custody='self')
        self.now+=timedelta(minutes=2)
        self.store.registry.record_event(STAFF,order['physical_item_id'],'inspection_completed',**self.args())
        self.assertCode('catalog_handover_time_invalid',lambda:self.service.handover_received(STAFF,order['order_id'],**self.args(occurred_at=T)))

    def test_database_rejects_fractional_money_even_without_service_validation(self):
        offer=self.offer();self.deposit();self.reserve(offer)
        self.db.execute("INSERT INTO business_items(physical_item_id) VALUES ('fractional-company')")
        statements=[
            ('''INSERT INTO stage4_company_items(physical_item_id,initial_title,purchase_cost_yen,created_at,actor_user_id,evidence_reference)
                VALUES ('fractional-company','fixture',1.5,?,9,'fixture')''',(T,)),
            ('''INSERT INTO stage4_catalog_revisions(revision_id,physical_item_id,revision_no,title,description,item_condition,price_yen,
                photo_refs_json,created_at,actor_user_id,evidence_reference)
                SELECT 'fractional-revision',physical_item_id,99,title,description,item_condition,2.5,photo_refs_json,created_at,actor_user_id,evidence_reference
                FROM stage4_catalog_revisions LIMIT 1''',()),
            ('''INSERT INTO stage4_purchase_orders(order_id,physical_item_id,revision_id,customer_id,amount_yen,custody_choice,
                consent_reference,created_at,expires_at,reservation_record_id)
                SELECT 'fractional-order',physical_item_id,revision_id,customer_id,3.5,custody_choice,consent_reference,created_at,expires_at,reservation_record_id
                FROM stage4_purchase_orders LIMIT 1''',()),
        ]
        for sql,params in statements:
            with self.assertRaises(sqlite3.IntegrityError) as caught:
                self.db.execute(sql,params)
            self.assertIn('CHECK constraint failed',str(caught.exception))
        self.assertNotIn('typeof(', ';'.join(migration_statements('postgres')))

    def test_two_connections_cannot_reserve_the_same_physical_item(self):
        with tempfile.TemporaryDirectory(prefix='kaika-stage4-catalog-') as folder:
            path=str(Path(folder)/'fixture.db')
            db=fixture(path)
            store=Stage4Store(Registry(db))
            catalog=CatalogService(store,clock=lambda:datetime.fromisoformat(T))
            created=catalog.create_company_item(STAFF,title='競合テスト品',occurred_at=T,evidence_reference='fixture',source_key='create')
            item=created['physical_item_id'];owner=store.registry._roles(item)['owner']['assignment_id']
            db.execute('INSERT INTO stage4_media VALUES (?,?,?,?,?,?,?,?)',('photo',item,owner,'fixture-photo','b'*64,1,9,T))
            rev=catalog.revise(STAFF,item,title='競合品',description='説明',condition='中古',price_yen=100,photo_refs=['photo'],occurred_at=T,evidence_reference='fixture',source_key='rev')
            catalog.publish(STAFF,item,rev['revision_id'],occurred_at=T,evidence_reference='fixture',source_key='pub')
            for customer in (1,2):
                FundsService(store.finance).post(STAFF,customer,kind='deposit',amount_yen=100,occurred_at=T,evidence_reference='fixture',source_key='deposit'+str(customer))
            db.commit();db.close()
            barrier=threading.Barrier(2)
            def attempt(customer):
                conn=sqlite3.connect(path,timeout=10)
                conn.execute('PRAGMA foreign_keys=ON')
                service=CatalogService(Stage4Store(Registry(conn)),clock=lambda:datetime.fromisoformat(T))
                barrier.wait()
                try:
                    result=service.reserve(Actor(customer),item,rev['revision_id'],custody_choice='kaika',consent_reference='fixture',occurred_at=T,source_key='reserve'+str(customer))
                    conn.commit()
                    return result['status']
                except RegistryError as err:
                    conn.rollback();return err.code
                finally:
                    conn.close()
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(attempt,(1,2)))
            self.assertEqual(sorted(results),['catalog_item_unavailable','reserved'])
            conn=sqlite3.connect(path)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM stage4_purchase_claims').fetchone()[0],1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM stage3_fund_records WHERE kind='reserve'").fetchone()[0],1)
            conn.close()


if __name__=='__main__':
    unittest.main()
