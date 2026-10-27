"""Home visibility must never imply receipt, profit or a new charge."""
import json
import unittest

from test_stage6_home import HomeFixture, CUSTOMER, OTHER, STAFF, time
from stage8_self_sales import _yen


class SelfSaleHomeTests(HomeFixture, unittest.TestCase):
    def setUp(self):
        self.init_home()
        for name, kind in [('scope','TEXT'),('custody_location','TEXT'),('sale_date','TEXT'),('sale_price','NUMERIC'),('is_shipped','INTEGER')]:
            self.db.execute('ALTER TABLE merchandise ADD COLUMN ' + name + ' ' + kind)
        self.db.execute('CREATE TABLE self_inventory_items(merchandise_id INTEGER PRIMARY KEY,user_id INTEGER)')
        self.db.execute('CREATE TABLE inventory_custody_events(id INTEGER PRIMARY KEY,merchandise_id INTEGER,user_id INTEGER,actor_id INTEGER,event_type TEXT,details TEXT)')
        self.db.execute("INSERT INTO merchandise VALUES(10,1,'自分の商品','user','self','2026-10-22',40000,1)")
        self.db.execute('INSERT INTO self_inventory_items VALUES(10,1)')
        self.registry.attach_legacy(CUSTOMER,10,physical_item_id=self.item,occurred_at=time(),evidence_reference='fixture',source_key='link')
        self.registry.record_event(CUSTOMER,self.item,'sold',time(1),'sale','customer-self-sale-report:10')
        self.registry.record_event(CUSTOMER,self.item,'shipped',time(2),'ship','customer-self-sale-report:10')
        self.audit()

    def audit(self, amount=40000, actor=1, details=None):
        sold=self.registry.query("SELECT observation_id FROM business_item_observations WHERE physical_item_id=? AND kind='sold' ORDER BY occurred_at DESC,recorded_at DESC,observation_id DESC",(self.item,))[0]['observation_id']
        details=details if details is not None else {'registry_sale_observation_id':sold,'after':{'sale_date':'2026-10-22','sale_price':amount,'is_shipped':True}}
        self.db.execute('INSERT INTO inventory_custody_events(merchandise_id,user_id,actor_id,event_type,details) VALUES(10,1,?,\'sale\',?)',(actor,json.dumps(details)))

    def money(self, actor=CUSTOMER):
        return self.home.customer(actor)['money']

    def reports(self):
        return self.money()['self_sale_reports']

    def test_explicit_sale_visible_separate_from_received_money_and_profit(self):
        value=self.money()
        self.assertEqual(value['self_reported_sales']['amount_yen'],40000)
        self.assertEqual(len(value['self_sale_reports']),1)
        self.assertFalse(value['self_sale_reports'][0]['provider_verified'])
        self.assertIsNone(value['self_sale_reports'][0]['profit_yen'])
        self.assertIsNone(value['actual_purchase_available_yen'])
        self.assertIsNone(value['recorded_realized_profit']['amount_yen'])

    def test_projection_creates_no_fees_or_financial_facts(self):
        before=self.db.total_changes
        self.reports()
        self.assertEqual(self.db.total_changes,before)
        self.assertEqual(self.db.execute('SELECT count(*) FROM stage3_item_financial_facts').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM stage3_charge_drafts').fetchone()[0],0)

    def test_other_customer_cannot_see_sale(self):
        self.assertEqual(self.money(OTHER)['self_sale_reports'],[])

    def test_owner_transfer_excludes_old_customer_values(self):
        self.registry.change_assignment(STAFF,self.item,'owner','customer',2,occurred_at=time(5),evidence_reference='transfer',source_key='transfer')
        self.assertEqual(self.reports(),[])
        self.assertEqual(self.money(OTHER)['self_sale_reports'],[])

    def test_return_excludes_original_sale(self):
        self.registry.record_event(CUSTOMER,self.item,'returned',time(5),'return','confirmed-customer-return',{'return_reason':'fixture'})
        self.assertEqual(self.reports(),[])

    def test_no_self_audit_means_no_legacy_inference(self):
        self.db.execute('DELETE FROM inventory_custody_events')
        self.assertEqual(self.reports(),[])

    def test_stale_audit_is_not_current_sale(self):
        self.db.execute('UPDATE merchandise SET sale_price=45000')
        self.assertEqual(self.reports(),[])

    def test_corrected_report_replaces_old_value(self):
        self.db.execute('UPDATE merchandise SET sale_price=45000')
        self.audit(45000)
        self.assertEqual(self.money()['self_reported_sales']['amount_yen'],45000)

    def test_malformed_audit_is_unknown(self):
        self.db.execute("UPDATE inventory_custody_events SET details='[]'")
        self.assertEqual(self.reports(),[])

    def test_return_then_same_amount_resale_requires_new_cycle_audit(self):
        self.registry.record_event(CUSTOMER,self.item,'returned',time(5),'return','return',{'return_reason':'fixture'})
        self.registry.record_event(CUSTOMER,self.item,'release_hold',time(6),'release','release',{'note':'confirmed'})
        self.registry.record_event(CUSTOMER,self.item,'sold',time(7),'resale','customer-self-sale-report:10')
        self.registry.record_event(CUSTOMER,self.item,'shipped',time(8),'reship','customer-self-sale-report:10')
        self.assertEqual(self.reports(),[])
        self.audit()
        self.assertEqual(self.money()['self_reported_sales']['amount_yen'],40000)

    def test_old_audit_without_cycle_id_remains_unverified(self):
        self.audit(details={'after':{'sale_date':'2026-10-22','sale_price':40000,'is_shipped':True}})
        self.assertEqual(self.reports(),[])

    def test_string_shipped_audit_is_not_truthy_evidence(self):
        row=self.db.execute('SELECT details FROM inventory_custody_events').fetchone()
        details=json.loads(row[0])
        for invalid in ('false','0',1,None):
            details['after']['is_shipped']=invalid
            self.audit(details=details)
            self.assertEqual(self.reports(),[])

    def test_legacy_shipped_state_must_match_registry(self):
        self.db.execute('UPDATE merchandise SET is_shipped=0')
        self.assertEqual(self.reports(),[])

    def test_mismatched_actor_is_not_self_report(self):
        self.db.execute('UPDATE inventory_custody_events SET actor_id=9')
        self.assertEqual(self.reports(),[])

    def test_kaika_stock_is_not_self_sale(self):
        self.db.execute("UPDATE merchandise SET custody_location='kaika'")
        self.assertEqual(self.reports(),[])

    def test_ambiguous_links_not_counted_twice(self):
        self.db.execute("INSERT INTO merchandise VALUES(11,1,'duplicate','user','self','2026-10-22',40000,1)")
        self.db.execute('INSERT INTO self_inventory_items VALUES(11,1)')
        self.registry.attach_legacy(CUSTOMER,11,physical_item_id=self.item,occurred_at=time(4),evidence_reference='fixture',source_key='duplicate')
        self.assertEqual(self.reports(),[])

    def test_detailed_facts_take_precedence(self):
        self.profit.record_facts(STAFF,self.item,amounts={'sale_gross_yen':40000},channel='other',
            source_reference='reviewed-source',occurred_at=time(4),source_key='facts')
        value=self.money()
        self.assertEqual(value['self_sale_reports'],[])
        self.assertEqual(value['recorded_sales']['amount_yen'],40000)

    def test_fractional_or_unknown_yen_is_not_silently_rounded(self):
        for value in (None,True,'nan','inf',-1,0.5,'unknown'):
            self.assertIsNone(_yen(value))
        self.assertEqual(_yen('40000.0'),40000)
        self.assertEqual(_yen(0),0)


if __name__=='__main__':
    unittest.main()
