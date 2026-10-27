"""Real Flask/SQLite bridge with fictional users, no providers or app import."""
from contextlib import closing
import sqlite3
import unittest
from unittest.mock import patch

from item_registry import Actor, RegistryError
from stage6_common import migration_statements as common_schema
from stage6_notifications_schema import migration_statements as notice_schema
from stage6_push_bridge import register_stage6_push_bridge
import test_push_notifications as push_fixture
from test_stage5_payments import fixture


class PushBridgeTests(unittest.TestCase):
    def setUp(self):
        self.push = push_fixture.PushTest(methodName='runTest')
        self.push.setUp()
        self.addCleanup(self.push.tearDown)
        with closing(fixture(str(self.push.path))) as db:
            for sql in common_schema() + notice_schema():
                db.execute(sql)
            db.commit()
        self.bridge = register_stage6_push_bridge(self.push.app, self.push.runtime)
        self.push.app.config['STAGE6_ENABLED'] = True

    def destinations(self):
        with self.bridge.service() as service:
            return service.store.query('SELECT * FROM stage6_destinations')

    def notice(self, customer=1):
        with self.bridge.service() as service:
            service.validator = lambda actor, source: True
            event = dict(customer_id=customer, event_type='listing_approval', source_type='fixture',
                source_id='fixture-1', source_version='v1', dedupe_key='fixture-1', path='/home', execution_mode='test')
            return service.publish_event(Actor(customer), event, source_key='fixture-publish-' + str(customer))['notification_id']

    def test_flag_off_keeps_stage6_empty_and_legacy_registration_available(self):
        self.push.app.config['STAGE6_ENABLED'] = False
        self.assertEqual(self.push.register().status_code, 200)
        self.assertEqual(self.destinations(), [])
        self.assertFalse(self.bridge.destination_valid({}))
        self.assertEqual(self.push.rows('push_outbox'), [])

    def test_authenticated_register_bridges_without_enqueuing_or_sending(self):
        self.assertEqual(self.push.register(user_id=2).status_code, 200)
        row = self.destinations()[0]
        self.assertEqual(row['customer_id'], 1)
        self.assertTrue(self.bridge.destination_valid(row))
        self.assertNotEqual(row['binding_hash'], self.push.context['account_key'])
        self.assertEqual(self.push.rows('push_outbox'), [])
        with self.bridge.service() as service:
            self.assertEqual(service.store.query('SELECT * FROM stage6_notification_outbox'), [])

    def test_repeated_refresh_preserves_current_destination_and_queued_reference(self):
        self.push.register()
        previous = self.destinations()[0]['destination_id']
        self.push.register()
        active = [x for x in self.destinations() if x['revoked_at'] is None]
        self.assertEqual([x['destination_id'] for x in active], [previous])

    def test_logout_and_revoke_disable_new_destination(self):
        self.push.register()
        row = self.destinations()[0]
        self.push.client.post('/fixture/logout')
        self.assertFalse(self.bridge.destination_valid(row))
        self.assertIsNotNone(self.destinations()[0]['revoked_at'])

    def test_revoke_failure_still_blocks_using_legacy_device_immediately(self):
        self.push.register()
        row = self.destinations()[0]
        with patch.object(self.bridge, 'revoke_binding', side_effect=RuntimeError('fixture only')):
            self.assertEqual(self.push.client.post('/fixture/logout').status_code, 200)
        self.assertIsNone(self.destinations()[0]['revoked_at'])
        self.assertFalse(self.bridge.destination_valid(row))

    def test_old_binding_token_or_expired_device_is_never_valid(self):
        self.push.register()
        row = self.destinations()[0]
        for patch_values in ({'binding_hash': 'a'*64}, {'address': 'ExpoPushToken[another_fixture_token]'},
                             {'customer_id': 2}, {'platform': 'android'}, {'revoked_at': 1}):
            self.assertFalse(self.bridge.destination_valid(dict(row, **patch_values)))
        self.push.now += 31 * 86400
        self.assertFalse(self.bridge.destination_valid(row))

    def test_tap_validates_notice_owner_and_current_registration(self):
        self.push.register()
        notice, foreign = self.notice(), self.notice(2)
        self.assertEqual(self.push.post('open', {'notification_id': notice}).json, {'path': '/notifications/' + notice})
        self.assertEqual(self.push.post('open', {'notification_id': foreign}).status_code, 404)
        self.push.post('revoke')
        self.assertEqual(self.push.post('open', {'notification_id': notice}).status_code, 404)
        self.assertIsNotNone(self.destinations()[0]['revoked_at'])

    def test_switch_account_cannot_reopen_old_notice_or_send_old_destination(self):
        self.push.register(); row = self.destinations()[0]; notice = self.notice()
        self.push.client.post('/fixture/login/2')
        self.push.context = self.push.client.get('/api/push/context').json
        self.assertEqual(self.push.register().status_code, 200)
        self.assertFalse(self.bridge.destination_valid(row))
        self.assertEqual(self.push.post('open', {'notification_id': notice}).status_code, 404)
        self.assertEqual([x['customer_id'] for x in self.destinations() if x['revoked_at'] is None], [2])

    def test_bridge_rejects_forged_registration_before_new_records(self):
        with self.assertRaises(RegistryError):
            self.bridge.register(1, self.push.token, 'a'*64, 'b'*64, 'ios')
        self.assertEqual(self.destinations(), [])

    def test_registration_hook_failure_returns_retryable_unavailable(self):
        with patch.object(self.bridge, 'register', side_effect=RuntimeError('fixture only')):
            self.assertEqual(self.push.register().status_code, 503)
        self.assertEqual(self.destinations(), [])
        self.assertEqual(self.push.register().status_code, 200)

    def test_web_revoke_disables_both_records_until_explicit_native_opt_in(self):
        self.push.register()
        destination = self.destinations()[0]['destination_id']
        with self.bridge.service() as service:
            service.store.connection.execute('BEGIN IMMEDIATE')
            service.revoke_destination(Actor(1), destination, source_key='web-revoke')
            self.assertEqual(self.bridge.deactivate_legacy_destination(service.store, Actor(1), destination), {'disabled': True})
        self.assertFalse(self.push.post('status').json['enabled'])
        self.assertEqual([x for x in self.destinations() if x['revoked_at'] is None], [])
        # A deliberate native ON action may register again. A normal native
        # refresh does not call register after receiving enabled=False.
        self.assertEqual(self.push.register().status_code, 200)
        self.assertTrue(self.push.post('status').json['enabled'])
        self.assertEqual(len([x for x in self.destinations() if x['revoked_at'] is None]), 1)

    def test_web_revoke_transaction_rolls_back_if_legacy_update_fails(self):
        self.push.register()
        destination = self.destinations()[0]['destination_id']
        with self.assertRaises(RuntimeError):
            with self.bridge.service() as service:
                service.store.connection.execute('BEGIN IMMEDIATE')
                service.revoke_destination(Actor(1), destination, source_key='web-revoke-rollback')
                self.bridge.deactivate_legacy_destination(service.store, Actor(1), destination)
                raise RuntimeError('fixture interruption before transaction commit')
        self.assertTrue(self.push.post('status').json['enabled'])
        self.assertIsNone(self.destinations()[0]['revoked_at'])

    def test_old_destination_revocation_cannot_disable_a_new_login_binding(self):
        self.push.register()
        old = self.destinations()[0]['destination_id']
        self.push.client.post('/fixture/login/1')
        self.push.context = self.push.client.get('/api/push/context').json
        self.assertEqual(self.push.register().status_code, 200)
        with self.bridge.service() as service:
            service.store.connection.execute('BEGIN IMMEDIATE')
            service.revoke_destination(Actor(1), old, source_key='old-web-revoke')
            self.assertEqual(self.bridge.deactivate_legacy_destination(service.store, Actor(1), old), {'disabled': False})
        self.assertTrue(self.push.post('status').json['enabled'])


if __name__ == '__main__':
    unittest.main()
