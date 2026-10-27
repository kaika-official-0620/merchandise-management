import sqlite3
import unittest

from stage6_actions import CustodyActionService
from test_stage6_home import HomeFixture, STAFF, CUSTOMER, OTHER, INVENTORY_STAFF, time


class CustodyActionTests(HomeFixture, unittest.TestCase):
    def setUp(self):
        self.init_home()
        self.service=CustodyActionService(self.store)
        self.register()

    def request(self, key='request', choice='return'):
        return self.service.request(CUSTOMER,self.item,choice=choice,note='返送または別販路について相談したい',source_key=key)

    def test_confirmed_current_term_required(self):
        self.assertCode('custody_current_terms_required',lambda:self.request())
        self.deadline()
        result=self.request()
        self.assertEqual(result['status'],'pending')
        self.assertFalse(result['physical_shipping_enabled'])
        self.assertFalse(result['charge_created'])

    def test_only_actual_owner_can_request_not_staff_or_other_customer(self):
        self.deadline()
        self.assertCode('item_not_found',lambda:self.service.request(OTHER,self.item,choice='return',note='fixture',source_key='other'))
        self.assertCode('custody_customer_choice_required',lambda:self.service.request(STAFF,self.item,choice='return',note='fixture',source_key='staff'))

    def test_retry_idempotent_duplicate_active_request_prevented(self):
        self.deadline()
        first=self.request()
        self.assertEqual(self.request()['request_id'],first['request_id'])
        self.assertCode('custody_request_already_open',lambda:self.request('second','alternate_channel'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage6_custody_requests').fetchone()[0],1)

    def test_source_key_reuse_for_other_choice_rejected(self):
        self.deadline(); self.request()
        self.assertCode('source_key_conflict',lambda:self.request(choice='alternate_channel'))

    def test_staff_response_and_customer_cannot_respond(self):
        self.deadline(); request=self.request()
        self.assertCode('staff_permission_required',lambda:self.service.review(CUSTOMER,request['request_id'],status='resolved',note='fixture',source_key='bad'))
        got=self.service.review(INVENTORY_STAFF,request['request_id'],status='acknowledged',note='確認して連絡します',source_key='ack')
        self.assertEqual(got['status'],'acknowledged')
        self.assertEqual(self.service.review(INVENTORY_STAFF,request['request_id'],status='acknowledged',note='確認して連絡します',source_key='ack')['request_id'],request['request_id'])

    def test_resolved_can_start_new_revision_without_rewriting_history(self):
        self.deadline(); old=self.request()
        self.service.review(STAFF,old['request_id'],status='resolved',note='方針の確認が完了',source_key='resolve')
        new=self.request('next','alternate_channel')
        self.assertEqual(new['revision'],2)
        self.assertEqual(new['previous_request_id'],old['request_id'])
        self.assertEqual(self.service.get(CUSTOMER,old['request_id'])['status'],'resolved')

    def test_deadline_change_invalidates_old_and_allows_new_request(self):
        self.deadline(); old=self.request()
        self.deadline('2026-11-22T00:00:00+00:00','extended')
        self.assertFalse(self.service.get(CUSTOMER,old['request_id'])['current_source'])
        self.assertCode('custody_source_changed',lambda:self.service.review(STAFF,old['request_id'],status='resolved',note='fixture',source_key='bad'))
        self.assertEqual(self.request('next')['revision'],2)

    def test_owner_change_blocks_both_previous_and_new_customer_from_old_request(self):
        self.deadline(); old=self.request()
        self.registry.change_assignment(STAFF,self.item,'owner','customer',2,occurred_at=time(20),evidence_reference='ownership',source_key='transfer')
        self.assertCode('item_not_found',lambda:self.service.get(CUSTOMER,old['request_id']))
        self.assertCode('customer_access_denied',lambda:self.service.get(OTHER,old['request_id']))
        self.assertEqual(self.service.list_for_item(OTHER,self.item),[])
        self.assertFalse(self.service.get(STAFF,old['request_id'])['current_source'])

    def test_sold_or_return_shipped_cannot_be_new_choice(self):
        self.deadline(); self.event('sold',20)
        self.assertCode('custody_current_terms_required',lambda:self.request())

    def test_request_does_not_change_item_state_or_create_fees(self):
        self.deadline()
        before=self.registry.detail(CUSTOMER,self.item)
        self.request()
        self.assertEqual(before,self.registry.detail(CUSTOMER,self.item))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stage3_charge_drafts').fetchone()[0],0)

    def test_immutable_requests_and_responses(self):
        self.deadline(); old=self.request()
        self.service.review(STAFF,old['request_id'],status='acknowledged',note='確認',source_key='ack')
        for table in ('stage6_custody_requests','stage6_custody_responses'):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('DELETE FROM '+table)

    def test_staff_queue_carries_pending_request_and_hides_after_resolution(self):
        self.deadline(); old=self.request()
        self.assertTrue(any(task['kind']=='custody_request' for task in self.home.staff(STAFF)['tasks']))
        self.service.review(STAFF,old['request_id'],status='resolved',note='方針確認',source_key='resolve')
        self.assertFalse(any(task['kind']=='custody_request' for task in self.home.staff(STAFF)['tasks']))

    def test_customer_optional_note_can_be_empty_but_limit_is_checked(self):
        self.deadline()
        self.assertCode('invalid_custody_note',lambda:self.service.request(CUSTOMER,self.item,choice='return',note='a'*1001,source_key='bad'))
        self.assertEqual(self.service.request(CUSTOMER,self.item,choice='return',note='',source_key='empty')['note'],'')


if __name__=='__main__':
    unittest.main()
