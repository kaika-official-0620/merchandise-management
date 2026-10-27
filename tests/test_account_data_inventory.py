"""Discovery is counts-only and must never widen erasure into other customers."""
import json
from itertools import chain
import sqlite3
import unittest

from account_data_inventory import inventory_data
from item_registry import Registry


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,email TEXT,shipping_addresses TEXT)')
        self.db.executemany('INSERT INTO users VALUES (?,?,?)', [
            (1, 'private-one@example.invalid', '{"address":"PRIVATE_ADDRESS_ONE"}'),
            (2, 'private-two@example.invalid', '{"address":"PRIVATE_ADDRESS_TWO"}')])
        self.store = Registry(self.db)

    def table(self, definition, rows=()):
        self.db.execute('CREATE TABLE '+definition)
        name = definition.split('(')[0]
        for row in rows:
            self.db.execute('INSERT INTO '+name+' VALUES ('+','.join('?' for _ in row)+')', row)

    def report(self, customer=1):
        self.db.commit()
        return inventory_data(self.store, customer)

    def records(self, report):
        return {row['table']: row for row in report['tables']}

    def test_home_display_settings_are_profile_records_for_only_the_customer(self):
        self.table('stage6_home_preferences(customer_id INTEGER PRIMARY KEY,sections_json TEXT,updated_at TEXT)',
            [(1, '["private-choice-one"]', 'fixture-one'), (2, '["private-choice-two"]', 'fixture-two')])
        report = self.report(1)
        row = self.records(report)['stage6_home_preferences']
        self.assertEqual(row['classification'], 'profile')
        self.assertEqual(row['direct_records'], 1)
        self.assertEqual(row['related_records'], 1)
        self.assertNotIn('private-choice-one', json.dumps(report))
        self.assertNotIn('private-choice-two', json.dumps(report))

    def test_legacy_document_subject_and_child_items_are_counted(self):
        self.table('invoices(id INTEGER,sender_id INTEGER,bank_info TEXT)', [(10,1,'SECRET_BANK'),(20,2,'OTHER_BANK')])
        self.table('invoice_items(id INTEGER,invoice_id INTEGER,amount INTEGER)', [(1,10,1155),(2,10,500),(3,20,999)])
        self.table('shikiriosho(id INTEGER,sender_id INTEGER,recipient_id INTEGER)', [(1,2,1),(2,2,2)])
        self.table('shikiriosho_items(id INTEGER,shikiriosho_id INTEGER)', [(1,1),(2,2)])
        report = self.report()
        rows = self.records(report)
        self.assertEqual((1,0), (rows['invoices']['direct_records'],rows['invoices']['indirect_records']))
        self.assertEqual((None,2), (rows['invoice_items']['direct_records'],rows['invoice_items']['indirect_records']))
        self.assertEqual(1, rows['shikiriosho_items']['related_records'])
        self.assertEqual('retain_pending_policy', rows['invoice_items']['action'])
        self.assertNotIn('SECRET_BANK', json.dumps(report))

    def test_nested_support_rows_are_counted_once_without_loading_text(self):
        self.table('inquiries(id INTEGER,user_id INTEGER,content TEXT)', [(1,1,'PRIVATE_MESSAGE'),(2,2,'OTHER_MESSAGE')])
        self.table('inquiry_replies(id INTEGER,inquiry_id INTEGER,user_id INTEGER,content TEXT)', [
            (1,1,1,'OWN_REPLY'),(2,1,9,'STAFF_REPLY'),(3,2,2,'OTHER_REPLY')])
        row = self.records(self.report())['inquiry_replies']
        self.assertEqual((1,1,2), (row['direct_records'],row['indirect_records'],row['related_records']))

    def test_multilevel_order_tasks_follow_customer_order_not_actor(self):
        self.table('stage4_channel_orders(order_id TEXT,customer_id INTEGER,actor_user_id INTEGER)', [('one',1,9),('two',2,1)])
        self.table('stage4_order_tasks(task_id TEXT,order_id TEXT)', [('ta','one'),('tb','two')])
        self.table('stage4_order_task_events(event_id TEXT,task_id TEXT,actor_user_id INTEGER,note TEXT)', [
            ('ea','ta',9,'PRIVATE_NOTE'),('eb','tb',1,'OTHER_NOTE')])
        rows = self.records(self.report())
        self.assertEqual(1,rows['stage4_channel_orders']['related_records'])
        self.assertEqual(1,rows['stage4_order_task_events']['related_records'])

    def test_historical_item_ownership_does_not_fan_out_to_later_owner_orders(self):
        self.table('business_parties(party_id TEXT,user_id INTEGER)', [('p1',1),('p2',2)])
        self.table('business_item_assignments(assignment_id TEXT,physical_item_id TEXT,party_id TEXT,ended_at TEXT)', [
            ('old','shared-item','p1','2026-01-01'),('again','shared-item','p1','2026-02-01'),('new','shared-item','p2',None)])
        self.table('business_items(physical_item_id TEXT,label_code TEXT)', [('shared-item','PRIVATE_LABEL')])
        self.table('stage4_channel_orders(order_id TEXT,customer_id INTEGER,physical_item_id TEXT)', [('new-order',2,'shared-item')])
        rows = self.records(self.report())
        self.assertEqual(1, rows['business_items']['related_records'])
        self.assertEqual(2, rows['business_item_assignments']['related_records'])
        self.assertEqual(0, rows['stage4_channel_orders']['related_records'])
        self.assertTrue(rows['business_items']['notes'])

    def test_provider_customer_ids_are_not_confused_with_local_customer(self):
        self.table('feature_billing_accounts(user_id INTEGER,customer_id TEXT)', [(2,'1'),(1,'cus_private')])
        self.table('feature_stripe_receipts(subscription_id TEXT,customer_id TEXT)', [('other','1'),('own','cus_private')])
        rows=self.records(self.report())
        self.assertEqual(1,rows['feature_billing_accounts']['direct_records'])
        self.assertEqual(1,rows['feature_stripe_receipts']['related_records'])

    def test_store_receipts_and_refunds_use_provider_plus_subscription(self):
        self.table('feature_subscriptions(provider TEXT,subscription_id TEXT,user_id INTEGER)', [('apple','same',1),('google','same',2)])
        self.table('feature_store_receipts(provider TEXT,subscription_id TEXT,account_token TEXT)', [('apple','same','SECRET_A'),('google','same','SECRET_B')])
        self.table('feature_store_refunds(provider TEXT,subscription_id TEXT,order_id TEXT)', [('apple','same','a'),('google','same','b')])
        rows=self.records(self.report())
        self.assertEqual(1,rows['feature_store_receipts']['related_records'])
        self.assertEqual(1,rows['feature_store_refunds']['related_records'])

    def test_missing_parent_is_unknown_not_zero(self):
        self.table('invoice_items(id INTEGER,invoice_id INTEGER)', [(1,1)])
        report=self.report(); row=self.records(report)['invoice_items']
        self.assertIsNone(row['related_records'])
        self.assertIsNone(row['indirect_records'])
        group=next(g for g in report['groups'] if g['key']=='documents')
        self.assertIsNone(group['record_count'])
        self.assertEqual(0,group['known_record_count'])
        self.assertIn('unresolved_table_relationships',report['blockers'])

    def test_unknown_tables_are_never_hidden_or_declared_safe_by_name(self):
        self.table('future_schema(customer_id INTEGER,payload TEXT)', [(1,'PRIVATE_FUTURE')])
        report=self.report(); row=self.records(report)['future_schema']
        self.assertEqual('unreviewed',row['classification'])
        self.assertIsNone(row['related_records'])
        self.assertIn('unreviewed_tables',report['blockers'])
        self.assertNotIn('PRIVATE_FUTURE',json.dumps(report))

    def test_unsafe_table_identifier_is_reported_but_never_interpolated(self):
        name='untrusted; DROP TABLE users'
        self.db.execute('CREATE TABLE "'+name+'"(customer_id INTEGER)')
        report=self.report()
        self.assertEqual('unreviewed',self.records(report)[name]['classification'])
        self.assertIn('unsupported_table_identifier',report['blockers'])
        self.assertEqual(2,self.db.execute('SELECT COUNT(*) FROM users').fetchone()[0])

    def test_json_photo_and_external_copies_are_explicit_unfinished_scope(self):
        self.table('merchandise(id INTEGER,user_id INTEGER,photo_path TEXT,additional_photos TEXT)', [
            (1,1,'private/local/photo.jpg','["https://private.invalid/secret.jpg"]'),
            (2,2,'other/photo.jpg','[]')])
        self.table('business_legacy_snapshots(snapshot_id TEXT,customer_id INTEGER,snapshot_json TEXT)', [
            ('s1',1,'{"address":"PRIVATE_EMBEDDED","customer_id":2}')])
        report=self.report(); serialized=json.dumps(report)
        self.assertFalse(report['complete'])
        for secret in ('private-one@example.invalid','PRIVATE_ADDRESS_ONE','PRIVATE_EMBEDDED','private/local/photo.jpg','secret.jpg'):
            self.assertNotIn(secret,serialized)
        self.assertIn('embedded_payload_review_required',report['blockers'])
        self.assertIn('shared_media_review_required',report['blockers'])
        for name in ('providers','backups'):
            self.assertIsNone(next(g for g in report['groups'] if g['key']==name)['record_count'])

    def test_line_invitation_scope_requires_exact_verified_destination_association(self):
        self.table('stage6_destinations(destination_id TEXT,customer_id INTEGER,channel TEXT,address TEXT)', [
            ('a',1,'line','U_PRIVATE_A'),('b',2,'line','U_PRIVATE_B'),('c',1,'email','U_PRIVATE_B')])
        self.table('stage6_line_invitations(invitation_id TEXT,webhook_event_id TEXT,line_user_id TEXT,token_hash TEXT)', [
            ('ia','wa','U_PRIVATE_A','SECRET_A'),('ib','wb','U_PRIVATE_B','SECRET_B'),('iu','wu','UNLINKED','SECRET_U')])
        self.table('stage6_line_link_tokens(invitation_id TEXT,link_token_hash TEXT)', [('ia','SECRET_LA'),('ib','SECRET_LB')])
        self.table('stage6_line_webhooks(webhook_event_id TEXT,request_hash TEXT)', [('wa','PRIVATE_HASH_A'),('wb','PRIVATE_HASH_B')])
        report=self.report(); rows=self.records(report)
        for name in ('stage6_line_invitations','stage6_line_link_tokens','stage6_line_webhooks'):
            self.assertEqual(1,rows[name]['related_records'])
        self.assertIn('unlinked_line_identity_review_required',report['blockers'])
        self.assertNotIn('SECRET_',json.dumps(report))

    def test_discovery_does_not_write_any_data_or_return_private_values(self):
        self.table('invoices(id INTEGER,sender_id INTEGER)', [(1,1)])
        self.db.commit()
        before='\n'.join(self.db.iterdump())
        denied={sqlite3.SQLITE_INSERT,sqlite3.SQLITE_UPDATE,sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE,sqlite3.SQLITE_DROP_TABLE,sqlite3.SQLITE_ALTER_TABLE}
        self.db.set_authorizer(lambda action,*args: sqlite3.SQLITE_DENY if action in denied else sqlite3.SQLITE_OK)
        report=inventory_data(self.store,1)
        self.db.set_authorizer(None)
        self.assertEqual(before,'\n'.join(self.db.iterdump()))
        self.assertTrue(report['read_only'])
        self.assertTrue(report['counts_only'])

    def test_rejects_noninteger_or_nonpositive_customer_identifiers(self):
        for value in (True,False,0,-1,'1','1 OR 1=1',None,1.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                inventory_data(self.store,value)

    def test_known_absence_does_not_claim_complete_deletion(self):
        report=self.report(999)
        self.assertEqual(0,self.records(report)['users']['related_records'])
        self.assertFalse(report['complete'])
        self.assertIn('retention_policy_required',report['blockers'])

    def test_current_stage_schemas_can_be_inspected_without_app_startup(self):
        from test_stage5_payments import fixture
        from stage4_catalog_schema import migration_statements as catalog
        from stage4_listings_schema import migration_statements as listings
        from stage4_channels_schema import migration_statements as channels
        from stage6_migration import statements as notifications
        from stage7_migration import statements as measurement
        from stage9_payment_schema import statements as payments
        from account_lifecycle import migration_statements as lifecycle
        db=fixture(); self.addCleanup(db.close)
        for sql in chain(catalog(),listings(),channels(),notifications(),measurement(),payments(),lifecycle()):
            db.execute(sql)
        db.commit()
        before=db.total_changes
        report=inventory_data(Registry(db),1)
        rows=self.records(report)
        self.assertIn('stage9_payment_webhooks',rows)
        self.assertIn('stage6_custody_responses',rows)
        self.assertIn('stage7_observations',rows)
        self.assertEqual(0,rows['stage9_payment_events']['related_records'])
        self.assertEqual(before,db.total_changes)
        self.assertFalse(report['complete'])


if __name__ == '__main__':
    unittest.main()
