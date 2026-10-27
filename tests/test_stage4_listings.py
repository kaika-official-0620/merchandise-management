import json
import sqlite3
import unittest

from item_registry import Actor, Registry, RegistryError
from item_registry_schema import migrate_registry
from stage3_common import migration_statements as finance_sql
from stage4_common import Stage4Store, migration_statements as common_sql
from stage4_listings import ListingService
from stage4_listings_schema import migration_statements


def time(minute=0):
    return '2026-10-22T01:%02d:00+00:00' % minute


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT)')
        self.db.executemany('INSERT INTO users VALUES(?,?,?)', [(1,'user',None),(2,'user',None),(9,'owner',None),(8,'admin','["invoices"]')])
        self.db.execute('CREATE TABLE merchandise(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),product_name TEXT)')
        migrate_registry(self.db)
        for sql in tuple(finance_sql()) + tuple(common_sql()) + migration_statements():
            self.db.execute(sql)
        self.registry = Registry(self.db)
        self.store = Stage4Store(self.registry)
        self.now = time(50)
        self.service = ListingService(self.store, clock=lambda: self.now)
        self.customer, self.other, self.staff = Actor(1), Actor(2), Actor(9,True)
        self.item = self.registry.create_item(self.customer,'test item',occurred_at=time(),evidence_reference='fixture',source_key='create')['physical_item_id']
        self.receive_register()
        self.media(self.item, 'photo-one')

    def media(self, item, media_id):
        assignment = self.registry._roles(item)['owner']['assignment_id']
        self.db.execute('''INSERT INTO stage4_media(media_id,physical_item_id,ownership_assignment_id,
            storage_path,content_sha256,byte_size,actor_user_id,recorded_at) VALUES(?,?,?,?,?,?,?,?)''',
            (media_id,item,assignment,'fixture/'+media_id+'.jpg','a'*64,100,9,time(5)))

    def receive_register(self):
        for minute, kind in enumerate(('received','photographed','registration_completed'),1):
            self.event(kind,minute)

    def event(self, kind, minute, **details):
        return self.registry.record_event(self.staff,self.item,kind,time(minute),'event-'+self.item+kind+str(minute),'fixture-event',details)

    def propose(self, key='proposal', actor=None, **changes):
        params = dict(title='approved item',description='condition and item content',condition='used good',
            photo_references=['photo-one'],price_gross_yen=40000,minimum_price_gross_yen=35000,
            channels=[dict(channel='mercari_shops',account_id='kaika-shop'),dict(channel='yahoo_store',account_id='kaika-yahoo')],
            valid_until=time(59),occurred_at=time(10),source_key=key,evidence_reference='fixture-draft')
        params.update(changes)
        return self.service.propose(actor or self.customer,self.item,**params)

    def approve(self, actor, version, minute=11, key=None):
        return self.service.approve(actor,version['version_id'],source_key=key or 'approve-'+str(actor.user_id),evidence_reference='fixture-approval',occurred_at=time(minute))

    def approved(self):
        version = self.propose()
        self.approve(self.customer,version)
        return self.approve(self.staff,version,12)

    def publish(self, version, channel='mercari_shops', account='kaika-shop'):
        return self.service.approved_for_publication(self.staff,version['version_id'],channel,self.now,account)

    def assertCode(self, code, callback):
        with self.assertRaises(RegistryError) as error:
            callback()
        self.assertEqual(error.exception.code,code)

    def test_proposal_does_not_implicitly_approve(self):
        result = self.propose()
        self.assertEqual(result['approvals'],[])
        self.assertIn('listing_customer_approval_required',result['review_reasons'])
        self.assertIn('listing_kaika_approval_required',result['review_reasons'])

    def test_dual_approval_of_exact_hash_enables_recording_not_external_publish(self):
        result = self.approved()
        publication = self.publish(result)
        self.assertEqual(publication['status'],'approved')
        self.assertFalse(publication['external_publish_enabled'])
        self.assertEqual({a['content_hash'] for a in publication['approvals']},{result['content_hash']})
        self.assertEqual({a['party_role'] for a in publication['approvals']},{'customer','kaika'})
        self.assertNotIn('evidence_reference',publication)
        self.assertNotIn('decisions',publication)

    def test_staff_approval_cannot_impersonate_customer(self):
        version = self.propose(actor=self.staff)
        result = self.approve(self.staff,version)
        self.assertEqual([a['party_role'] for a in result['approvals']],['kaika'])
        self.assertCode('listing_customer_approval_required',lambda:self.publish(result))

    def test_customer_cannot_publish(self):
        version = self.approved()
        self.assertCode('staff_permission_required',lambda:self.service.approved_for_publication(self.customer,version['version_id'],'mercari_shops',self.now,'kaika-shop'))

    def test_customer_forged_staff_flag_is_rejected(self):
        self.assertCode('staff_permission_required',lambda:self.propose(actor=Actor(1,True)))

    def test_invoice_only_staff_cannot_change_listing(self):
        self.assertCode('staff_permission_required',lambda:self.propose(actor=Actor(8,True)))

    def test_other_customer_cannot_read_or_approve(self):
        version = self.propose()
        self.assertCode('item_not_found',lambda:self.service.detail(self.other,version['version_id']))
        self.assertCode('item_not_found',lambda:self.approve(self.other,version))

    def test_changed_price_requires_both_approvals_even_above_minimum(self):
        old = self.approved()
        changed = self.propose(key='reprice',price_gross_yen=38000,supersedes_version_id=old['version_id'],occurred_at=time(20))
        self.assertNotEqual(old['content_hash'],changed['content_hash'])
        self.assertEqual(changed['approvals'],[])
        self.assertCode('listing_superseded',lambda:self.publish(old))
        self.assertCode('listing_customer_approval_required',lambda:self.publish(changed))

    def test_each_content_change_invalidates_approval(self):
        old = self.approved()
        changes = [('description','new contents'),('condition','scratch disclosed'),('title','new title'),
            ('channels',[dict(channel='wholesale',account_id='dealer-one')]),('valid_until','2026-10-23T01:59:00+00:00')]
        for index,(field,value) in enumerate(changes,21):
            result = self.propose(key='changed-'+field,occurred_at=time(index),supersedes_version_id=old['version_id'],**{field:value})
            self.assertEqual(result['approvals'],[])
            self.assertNotEqual(result['content_hash'],old['content_hash'])
            old = result

    def test_stale_edit_does_not_fork_version_chain(self):
        old = self.propose()
        new = self.propose(key='second',supersedes_version_id=old['version_id'],occurred_at=time(20))
        self.assertCode('listing_version_conflict',lambda:self.propose(key='stale',supersedes_version_id=old['version_id'],occurred_at=time(21)))
        self.assertEqual(len(self.service.list_versions(self.customer,self.item)),2)
        self.assertEqual(new['revision_number'],2)

    def test_exact_retry_does_not_duplicate_version_or_decision(self):
        first = self.propose()
        self.assertEqual(self.propose()['version_id'],first['version_id'])
        self.approve(self.customer,first)
        self.approve(self.customer,first)
        self.approve(self.customer,first,key='new-key-same-approval')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_listing_versions').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage4_listing_decisions').fetchone()[0],1)

    def test_same_operation_key_changed_payload_is_rejected(self):
        self.propose()
        self.assertCode('source_key_conflict',lambda:self.propose(price_gross_yen=39000))

    def test_price_boundaries_and_integer_money(self):
        self.assertCode('listing_price_below_minimum',lambda:self.propose(price_gross_yen=34999))
        self.assertCode('listing_price_below_minimum',lambda:self.propose(minimum_price_gross_yen=0))
        self.assertCode('integer_yen_required',lambda:self.propose(price_gross_yen=40000.0))
        result = self.propose(price_gross_yen=35000)
        self.assertEqual(result['price_gross_yen'],35000)

    def test_unowned_photo_and_remote_url_cannot_be_injected(self):
        item2 = self.registry.create_item(self.other,'other item',occurred_at=time(),evidence_reference='fixture',source_key='other-create')['physical_item_id']
        self.media(item2,'photo-other')
        self.assertCode('listing_photo_not_owned',lambda:self.propose(photo_references=['photo-other']))
        self.assertCode('listing_photo_not_owned',lambda:self.propose(photo_references=['https://example.invalid/private.jpg']))
        self.assertCode('listing_photos_required',lambda:self.propose(photo_references=[]))

    def test_unapproved_account_or_channel_is_blocked(self):
        approved = self.approved()
        self.assertCode('listing_channel_not_approved',lambda:self.publish(approved,account='another-shop'))
        self.assertCode('listing_channel_not_approved',lambda:self.publish(approved,channel='wholesale',account='dealer-one'))
        self.assertCode('invalid_listing_channel',lambda:self.propose(key='personal',channels=[dict(channel='mercari_personal',account_id='one')]))
        self.assertCode('invalid_listing_channel',lambda:self.propose(key='malformed',channels=[dict(channel=[],account_id='one')]))

    def test_expiry_is_checked_again_at_use_and_not_at_reported_old_date(self):
        version = self.approved()
        self.now = time(59)
        self.assertCode('listing_expired',lambda:self.publish(version))
        self.assertCode('listing_expired',lambda:self.approve(self.staff,version,key='late',minute=12))

    def test_past_expiry_and_future_approval_are_blocked(self):
        self.assertCode('listing_expired',lambda:self.propose(valid_until=time(49)))
        version = self.propose()
        self.assertCode('listing_time_in_future',lambda:self.approve(self.customer,version,minute=51))

    def test_rejection_revokes_an_existing_approval_and_needs_new_version(self):
        version = self.approved()
        result = self.service.reject(self.customer,version['version_id'],source_key='reject',evidence_reference='fixture-disagreement',occurred_at=time(15),reason='説明の訂正が必要')
        self.assertEqual(result['status'],'rejected')
        self.assertCode('listing_rejected',lambda:self.publish(version))
        self.assertCode('listing_rejected',lambda:self.approve(self.customer,version,key='reapprove',minute=16))

    def test_sale_or_hold_blocks_publication(self):
        version = self.approved()
        self.event('hold',15,note='damage suspected')
        self.assertCode('listing_item_unavailable',lambda:self.publish(version))
        self.event('release_hold',16,note='checked')
        self.assertCode('listing_item_changed',lambda:self.publish(version))

    def test_other_channel_recorded_listing_does_not_invalidate_same_approval(self):
        version = self.approved()
        self.event('listed',15)
        self.assertEqual(self.publish(version,channel='yahoo_store',account='kaika-yahoo')['status'],'approved')

    def test_sold_item_cannot_be_republished(self):
        version = self.approved()
        self.event('sold',15)
        self.assertCode('listing_item_unavailable',lambda:self.publish(version))

    def test_ownership_change_invalidates_and_hides_old_private_terms(self):
        version = self.approved()
        self.registry.change_assignment(self.staff,self.item,'owner','customer',2,occurred_at=time(20),evidence_reference='fixture-sale',source_key='ownership')
        self.assertCode('item_not_found',lambda:self.service.detail(self.customer,version['version_id']))
        self.assertCode('listing_not_found',lambda:self.service.detail(self.other,version['version_id']))
        self.assertEqual(self.service.list_versions(self.other,self.item),[])
        self.assertCode('listing_ownership_changed',lambda:self.publish(version))
        self.media(self.item,'new-owner-photo')
        new = self.propose(actor=self.other,key='new-owner',photo_references=['new-owner-photo'],occurred_at=time(25))
        self.assertIsNone(new['supersedes_version_id'])
        self.assertEqual(new['customer_id'],2)
        self.assertEqual(new['approvals'],[])

    def test_receipt_and_registration_are_required_to_approve(self):
        other = self.registry.create_item(self.customer,'self item',occurred_at=time(),evidence_reference='fixture',source_key='self-item')['physical_item_id']
        self.media(other,'self-photo')
        self.item = other
        version = self.propose(photo_references=['self-photo'])
        self.assertCode('listing_kaika_receipt_required',lambda:self.approve(self.customer,version))

    def test_new_registration_invalidates_preparation_draft(self):
        item = self.registry.create_item(self.customer,'new item',occurred_at=time(),evidence_reference='fixture',source_key='new-item')['physical_item_id']
        self.item = item
        self.event('received',1)
        self.media(item,'new-photo')
        version = self.propose(photo_references=['new-photo'])
        self.assertCode('listing_registration_required',lambda:self.approve(self.customer,version))
        self.event('photographed',11)
        self.event('registration_completed',12)
        self.assertCode('listing_item_changed',lambda:self.approve(self.customer,version,minute=13))

    def test_database_blocks_rewriting_approved_content_and_hash_mismatch(self):
        version = self.propose()
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('UPDATE stage4_listing_versions SET price_gross_yen=35000 WHERE version_id=?',(version['version_id'],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('DELETE FROM stage4_listing_versions WHERE version_id=?',(version['version_id'],))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute('''INSERT INTO stage4_listing_decisions(decision_id,version_id,party_role,decision,
                actor_user_id,content_hash,occurred_at,recorded_at,evidence_reference,source_key)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',('bad',version['version_id'],'customer','approved',2,'wrong',time(11),time(11),'forged','bad'))

    def test_customer_view_excludes_staff_evidence(self):
        version = self.approved()
        detail = self.service.detail(self.customer,version['version_id'])
        self.assertNotIn('decisions',detail)
        self.assertNotIn('evidence_reference',detail)
        self.assertNotIn('actor_user_id',json.dumps(detail))

    def test_photo_permission_is_rechecked_at_publication(self):
        version = self.approved()
        self.store.listing_photo_allowed = lambda *args: False
        self.assertCode('listing_photo_not_owned',lambda:self.publish(version))

    def test_returned_item_can_have_a_new_review_without_erasing_previous_sale(self):
        version = self.approved()
        self.event('sold',15)
        self.event('shipped',16)
        self.event('returned',17,return_reason='description disagreement')
        self.event('release_hold',18,note='returned item inspected')
        self.event('photographed',19)
        self.event('registration_completed',20)
        new = self.propose(key='resale',supersedes_version_id=version['version_id'],occurred_at=time(21))
        self.approve(self.customer,new,minute=22,key='resale-customer')
        self.approve(self.staff,new,minute=23,key='resale-staff')
        self.assertEqual(self.publish(new)['status'],'approved')
        item = self.registry.detail(self.staff,self.item)
        self.assertIsNotNone(item['sold_at'])
        self.assertIsNotNone(item['shipped_at'])
        self.assertEqual(len([e for e in item['events'] if e['kind']=='sold']),1)

    def test_company_purchase_can_reuse_registration_without_new_work_or_count(self):
        from stage3_funds_schema import migration_statements as funds_sql
        from stage3_funds import FundsService
        from stage4_catalog_schema import migration_statements as catalog_sql
        from stage4_catalog import CatalogService, catalog_registration_proof
        for sql in tuple(funds_sql()) + tuple(catalog_sql()):
            self.db.execute(sql)
        catalog = CatalogService(self.store,ownership_rule='test_payment_confirmation',clock=lambda:self.now)
        def args(key,minute):
            return dict(source_key='catalog-'+key,occurred_at=time(minute),evidence_reference='fixture')
        self.item = catalog.create_company_item(self.staff,title='company object',**args('create',1))['physical_item_id']
        self.media(self.item,'company-photo')
        revision = catalog.revise(self.staff,self.item,title='company object',description='existing description',condition='used',price_yen=30000,photo_refs=['company-photo'],**args('revise',2))
        catalog.publish(self.staff,self.item,revision['revision_id'],**args('publish',3))
        FundsService(self.store.finance).post(self.staff,1,kind='deposit',amount_yen=50000,**args('deposit',4))
        order = catalog.reserve(self.customer,self.item,revision['revision_id'],custody_choice='kaika',consent_reference='fixture agreement',occurred_at=time(5),source_key='catalog-reserve')
        catalog.confirm_test_purchase(self.staff,order['order_id'],**args('confirm',6))
        self.store.reusable_catalog_registration = lambda item,customer: catalog_registration_proof(self.store,item,customer)
        before_works = self.db.execute('SELECT COUNT(*) FROM business_work_records').fetchone()[0]
        before_registrations = self.db.execute("SELECT COUNT(*) FROM business_item_observations WHERE kind='registration_completed'").fetchone()[0]
        version = self.propose(photo_references=['company-photo'])
        self.approve(self.customer,version)
        self.approve(self.staff,version,12)
        self.assertEqual(self.publish(version)['status'],'approved')
        self.assertIsNone(self.registry.detail(self.customer,self.item)['registration_completed_at'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM business_work_records').fetchone()[0],before_works)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM business_item_observations WHERE kind='registration_completed'").fetchone()[0],before_registrations)
        anchor = json.loads(self.db.execute('SELECT item_anchor_json FROM stage4_listing_versions WHERE version_id=?',(version['version_id'],)).fetchone()[0])
        self.assertEqual(anchor['reused_catalog_registration']['revision_id'],revision['revision_id'])
        self.assertEqual(anchor['reused_catalog_registration']['registration_count_increment'],0)

    def test_postgres_schema_has_equivalent_immutable_and_owner_guards(self):
        sql = '\n'.join(migration_statements('postgres'))
        self.assertNotIn('typeof(',sql)
        self.assertIn('stage4_listing_decision_guard_fn',sql)
        self.assertIn('append-only listing evidence',sql)
        self.assertIn("p.kind='customer'",sql)


if __name__ == '__main__':
    unittest.main()
