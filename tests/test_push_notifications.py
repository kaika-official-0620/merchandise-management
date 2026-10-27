"""Disposable SQLite, fake users, fake Expo only; never imports app/render_app."""
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from flask import Flask, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user
from push_notifications import register_push_notifications


class User(UserMixin):
    def __init__(self, identifier):
        self.id = int(identifier)


class PushTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'fixture.sqlite'
        self.app = Flask(__name__)
        self.project = 'aaaa1111-2222-4333-8444-555555555555'
        self.app.config.update(TESTING=True, SECRET_KEY='fictional', PUSH_NOTIFICATIONS_ENABLED='1',
            EAS_PROJECT_ID=self.project, EXPO_ACCESS_TOKEN='fixture-only-no-network')
        LoginManager(self.app).user_loader(lambda identifier: User(identifier))
        self.runtime = SimpleNamespace(DATABASE_URL=None, get_db=lambda: sqlite3.connect(self.path),
            create_targeted_system_announcement=lambda user_id, *args, **kwargs: 123,
            notify_sale_request_user_status=lambda *args, **kwargs: None)
        def record_event(conn, sale_request_id, event_type, *args, **kwargs):
            conn.execute('INSERT INTO sale_request_events (sale_request_id,event_type) VALUES (?,?)', (sale_request_id, event_type))
        self.runtime.record_sale_request_event = record_event
        self.service = register_push_notifications(self.app, self.runtime)
        self.now = 2000000000
        self.service.clock = lambda: self.now
        self.calls = []
        def forbidden(*args):
            raise AssertionError('No real network permitted')
        self.service.http = forbidden
        @self.app.post('/fixture/login/<int:identifier>')
        def login(identifier):
            login_user(User(identifier))
            return 'ok'
        @self.app.post('/fixture/logout')
        def logout():
            logout_user()
            return 'ok'
        @self.app.post('/fixture/announcement')
        def announcement():
            return jsonify(id=self.runtime.create_targeted_system_announcement(1, 'Secret product', 'Private amount'))
        @self.app.post('/fixture/inquiry/<int:id>', endpoint='admin_inquiry_reply')
        def reply(id):
            conn = sqlite3.connect(self.path)
            conn.execute('INSERT INTO inquiry_replies(inquiry_id,is_admin_reply) VALUES (?,1)', (id,)); conn.commit(); conn.close()
            return 'ok'
        @self.app.post('/fixture/doc', endpoint='admin_shikiriosho_send_bulk')
        def doc():
            conn = sqlite3.connect(self.path); conn.execute("UPDATE shikiriosho SET status='sent' WHERE id=5"); conn.commit(); conn.close()
            return 'ok'
        @self.app.post('/fixture/sale/<int:commit>')
        def sale(commit):
            conn = sqlite3.connect(self.path)
            self.runtime.record_sale_request_event(conn, 4, 'shipment_marked')
            conn.commit() if commit else conn.rollback(); conn.close()
            if commit:
                self.runtime.notify_sale_request_user_status({'id': 77}, 1, 'shipment_marked')
            return 'ok'
        self.client = self.app.test_client()
        self.secret = 'a' * 64
        self.token = 'ExpoPushToken[fictional_token_12345]'
        self.client.post('/fixture/login/1')
        self.context = self.client.get('/api/push/context').json

    def tearDown(self):
        self.temp.cleanup()

    def post(self, action, extra=None, client=None, context=None):
        context = context or self.context
        return (client or self.client).post('/api/push/' + action, json={
            'installation': self.secret, 'account_key': context['account_key'], **(extra or {})},
            headers={'X-CSRF-Token': context['csrf_token']})

    def register(self, **extra):
        return self.post('register', {'token': self.token, 'project_id': self.project,
            'platform': 'ios', 'consent': True, **extra})

    def rows(self, table):
        with self.service.db() as cur:
            cur.execute('SELECT * FROM ' + table)
            return self.service.rows(cur)

    def enqueue(self):
        self.service.enqueue(1, 'fixture-event:123', '/inventory/intakes/7', 'inventory')

    def lifecycle(self, state, user_id=1):
        # A persisted lifecycle state must protect the legacy worker even when
        # the new request UI is disabled. Only disposable synthetic rows exist.
        from account_lifecycle import migration_statements
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,role TEXT,admin_permissions TEXT)')
            db.execute("INSERT INTO users(id,role) VALUES (?,'user') ON CONFLICT(id) DO NOTHING", (user_id,))
            for statement in migration_statements():
                db.execute(statement)
            db.execute('''INSERT INTO account_deletion_requests(customer_id,request_id,state,requested_at,updated_at)
                VALUES (?,?,?,'fixture-time','fixture-time') ON CONFLICT(customer_id) DO UPDATE SET state=excluded.state''',
                (user_id, 'fixture-request-' + str(user_id), state))
            db.commit()

    def test_explicit_config_and_consent_required(self):
        self.assertEqual(self.register(consent=False).status_code, 400)
        self.app.config['EXPO_ACCESS_TOKEN'] = ''
        self.assertFalse(self.client.get('/api/push/context').json['available'])
        self.assertEqual(self.register().status_code, 503)
        self.assertEqual(self.service.deliver(), {'processed': 0, 'available': False})

    def test_auth_csrf_session_and_token_checks(self):
        self.assertEqual(self.client.post('/api/push/register', json={}).status_code, 400)
        self.assertEqual(self.register(account_key='forged').status_code, 409)
        self.assertEqual(self.register(token='not-a-token').status_code, 400)
        self.assertEqual(self.register(project_id='foreign-project').status_code, 400)
        self.client.post('/fixture/logout')
        self.assertEqual(self.register().status_code, 401)

    def test_identity_is_cookie_owner_not_supplied_user(self):
        self.assertEqual(self.register(user_id=999).status_code, 200)
        device = self.rows('push_devices')[0]
        self.assertEqual(device['user_id'], 1)
        self.assertNotEqual(device['installation'], self.secret)

    def test_logout_revokes_queued_delivery(self):
        self.register(); self.enqueue()
        self.client.post('/fixture/logout')
        self.assertEqual(self.rows('push_devices')[0]['enabled'], 0)
        self.assertEqual(self.service.deliver()['processed'], 0)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'cancelled')

    def test_pending_deletion_disables_enqueue_and_registration_keeps_authenticated_context(self):
        self.register()
        self.lifecycle('requested')
        self.assertEqual(self.service.enqueue(1, 'blocked', '/'), 0)
        self.assertEqual(self.rows('push_outbox'), [])
        context = self.client.get('/api/push/context').json
        self.assertTrue(context['authenticated'])
        self.assertFalse(context['available'])
        self.assertIsNone(context['project_id'])
        self.assertEqual(self.post('status').json, {'enabled': False})
        self.assertEqual(self.register().status_code, 409)

    def test_pending_deletion_stops_legacy_queued_sending_and_receipt_without_network(self):
        self.register()
        for index, (initial, receipt, expected) in enumerate((('queued', None, 'cancelled'),
                ('sending', None, 'unknown'), ('receipt', 'fixture-ticket', 'unknown'))):
            with self.subTest(initial=initial):
                self.lifecycle('withdrawn')
                event = 'pending-' + str(index)
                self.service.enqueue(1, event, '/')
                with self.service.db() as cur:
                    cur.execute('UPDATE push_outbox SET state=?,receipt=? WHERE event_key=?', (initial, receipt, event))
                self.lifecycle('requested')
                self.assertEqual(self.service.deliver()['processed'], 0)
                self.assertEqual(next(row for row in self.rows('push_outbox') if row['event_key'] == event)['state'], expected)

    def test_pending_deletion_between_claim_and_transport_is_rechecked(self):
        self.register(); self.enqueue()
        original_db = self.service.db
        changed = False
        @contextmanager
        def pause_after_claim():
            nonlocal changed
            with original_db() as cur:
                yield cur
            if not changed:
                changed = True
                self.lifecycle('requested')
        self.service.db = pause_after_claim
        self.assertEqual(self.service.deliver()['processed'], 0)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'unknown')

    def test_closed_account_invalidates_preexisting_push_context_without_ui_flag(self):
        self.register(); self.enqueue()
        self.lifecycle('erasure_pending')
        self.app.config['ACCOUNT_LIFECYCLE_ENABLED'] = False
        response = self.client.get('/api/push/context')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {'authenticated': False, 'available': False})
        self.assertEqual(self.rows('push_devices')[0]['enabled'], 0)
        self.assertEqual(self.register().status_code, 401)
        self.assertEqual(self.service.deliver()['processed'], 0)

    def test_withdrawal_does_not_replay_suppressed_notification(self):
        self.register(); self.enqueue()
        self.lifecycle('requested')
        self.service.deliver()
        self.lifecycle('withdrawn')
        self.enqueue()
        self.assertEqual(len(self.rows('push_outbox')), 1)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'cancelled')
        self.assertEqual(self.service.deliver()['processed'], 0)
        self.assertEqual(self.service.enqueue(1, 'new-after-withdrawal', '/'), 1)

    def test_other_customer_deletion_does_not_suppress_current_customer(self):
        self.register(); self.enqueue()
        self.lifecycle('requested', user_id=2)
        self.service.http = lambda *args: {'data': [{'status': 'ok', 'id': 'fixture-ticket'}]}
        self.assertEqual(self.service.deliver()['processed'], 1)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'receipt')

    def test_account_switch_cannot_open_old_or_reuse_context(self):
        self.register(); self.enqueue()
        notification = self.rows('push_outbox')[0]['id']
        self.client.post('/fixture/login/2')
        self.assertEqual(self.register().status_code, 400)  # Old CSRF is rotated with login.
        self.context = self.client.get('/api/push/context').json
        self.assertEqual(self.post('status').json, {'enabled': False})
        self.assertEqual(self.post('open', {'notification_id': notification}).status_code, 404)

    def test_one_session_logout_keeps_other_session_device(self):
        self.register()
        other = self.app.test_client(); other.post('/fixture/login/1')
        context = other.get('/api/push/context').json
        self.assertEqual(self.post('register', {'installation': 'b' * 64, 'token': 'ExpoPushToken[second_fixture_12345]',
            'project_id': self.project, 'platform': 'android', 'consent': True}, client=other, context=context).status_code, 200)
        self.client.post('/fixture/logout')
        self.assertEqual(sum(row['enabled'] for row in self.rows('push_devices')), 1)

    def test_idempotence_and_tap_authorization(self):
        self.register(); self.enqueue(); self.enqueue()
        self.assertEqual(len(self.rows('push_outbox')), 1)
        notification = self.rows('push_outbox')[0]['id']
        self.assertEqual(self.post('open', {'notification_id': notification}).json, {'path': '/inventory/intakes/7'})
        self.post('revoke')
        self.assertEqual(self.post('open', {'notification_id': notification}).status_code, 404)
        self.assertEqual(self.post('status').json, {'enabled': False})

    def test_destination_rejects_mutations_and_external_urls(self):
        self.register()
        for path in ('//evil.test', 'https://evil.test/', '/logout', '/inquiry/7/delete', '/inquiry/7?delete=1'):
            with self.assertRaises(ValueError):
                self.service.enqueue(1, 'x', path)

    def test_ticket_then_receipt_not_immediate_delivery(self):
        self.register(); self.enqueue()
        def http(route, payload):
            self.calls.append((route, payload))
            return {'data': [{'status': 'ok', 'id': 'fixture-ticket'}]} if route == 'send' else {'data': {'fixture-ticket': {'status': 'ok'}}}
        self.service.http = http
        self.service.deliver()
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'receipt')
        self.assertEqual(self.service.deliver()['processed'], 0)
        message = self.calls[0][1][0]
        self.assertEqual(set(message['data']), {'notification_id'})
        self.assertNotIn('7', message['body'])
        self.now += 900; self.service.deliver()
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'provider_accepted')
        self.assertEqual([call[0] for call in self.calls], ['send', 'getReceipts'])

    def test_invalid_receipt_deactivates_exact_device(self):
        self.register(); self.enqueue()
        self.service.http = lambda route, payload: {'data': [{'status': 'ok', 'id': 'bad-ticket'}]} if route == 'send' else {'data': {'bad-ticket': {'status': 'error', 'details': {'error': 'DeviceNotRegistered'}}}}
        self.service.deliver(); self.now += 900; self.service.deliver()
        self.assertEqual(self.rows('push_devices')[0]['enabled'], 0)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'failed')

    def test_stage6_view_paths_still_require_current_device_binding(self):
        self.register()
        for path in ('/home', '/notifications', '/notifications/' + 'a' * 32):
            self.service.enqueue(1, 'stage6:' + path, path)
        for notice in self.rows('push_outbox'):
            self.assertEqual(self.post('open', {'notification_id': notice['id']}).json, {'path': notice['path']})
        for path in ('/notifications/1', '/notifications/' + 'g' * 32, '/notifications?token=private', '/notifications/' + 'a' * 32 + '/read', '/home#private'):
            with self.assertRaises(ValueError):
                self.service.enqueue(1, 'rejected', path)
        self.post('revoke')
        for notice in self.rows('push_outbox'):
            self.assertEqual(self.post('open', {'notification_id': notice['id']}).status_code, 404)

    def test_delivery_window_does_not_delete_old_history_or_registrations(self):
        self.register(); self.enqueue()
        self.now += 70 * 86400
        self.assertEqual(self.service.deliver()['processed'], 0)
        self.assertEqual(len(self.rows('push_devices')), 1)
        self.assertEqual(len(self.rows('push_outbox')), 1)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'cancelled')

    def test_send_timeout_is_unknown_and_never_blindly_retried(self):
        self.register(); self.enqueue()
        def failing(route, payload):
            self.calls.append(route)
            raise TimeoutError()
        self.service.http = failing
        self.service.deliver(); self.service.deliver()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'unknown')
        self.now += 30; self.service.deliver()
        self.assertEqual(len(self.calls), 1)

    def test_expired_sending_lease_without_ticket_is_held_without_network(self):
        self.register(); self.enqueue()
        with self.service.db() as cur:
            cur.execute("UPDATE push_outbox SET state='sending',attempts=1")
        self.assertEqual(self.service.deliver()['processed'], 0)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'unknown')

    def test_malformed_ticket_is_unknown_and_empty_id_cannot_trigger_resend(self):
        self.register(); self.enqueue()
        self.service.http=lambda *args: {'data':[{'status':'ok','id':''}]}
        self.assertEqual(self.service.deliver()['processed'],1)
        self.assertEqual(self.rows('push_outbox')[0]['state'],'unknown')
        self.now+=900
        self.assertEqual(self.service.deliver()['processed'],0)

    def test_explicit_rate_rejection_can_retry_but_result_unknown_cannot(self):
        self.register(); self.enqueue()
        def rejected(*args):
            self.calls.append('send')
            return {'data':[{'status':'error','details':{'error':'MessageRateExceeded'}}]}
        self.service.http=rejected
        self.service.deliver()
        self.assertEqual(self.rows('push_outbox')[0]['state'],'queued')
        self.now+=30; self.service.deliver()
        self.assertEqual(len(self.calls),2)

    def test_receipt_lookup_timeout_retries_only_read_never_send(self):
        self.register(); self.enqueue()
        def http(route,payload):
            self.calls.append(route)
            if route=='send': return {'data':[{'status':'ok','id':'fixture-ticket'}]}
            raise TimeoutError()
        self.service.http=http
        self.service.deliver(); self.now+=900; self.service.deliver()
        self.assertEqual(self.rows('push_outbox')[0]['state'],'receipt')
        self.now+=900; self.service.deliver()
        self.assertEqual(self.calls,['send','getReceipts','getReceipts'])

    def test_malformed_receipt_response_is_unknown_not_delivered(self):
        self.register(); self.enqueue()
        self.service.http=lambda route,payload: {'data':[{'status':'ok','id':'ticket'}]} if route=='send' else None
        self.service.deliver(); self.now+=900; self.service.deliver()
        self.assertEqual(self.rows('push_outbox')[0]['state'],'unknown')

    def test_expired_receipt_retains_uncertainty_not_unsent_cancelled(self):
        self.register(); self.enqueue()
        self.service.http=lambda *args: {'data':[{'status':'ok','id':'ticket'}]}
        self.service.deliver(); self.now+=86401
        self.assertEqual(self.service.deliver()['processed'],0)
        self.assertEqual(self.rows('push_outbox')[0]['state'],'unknown')

    def test_each_worker_claim_uses_current_clock_and_stale_claim_cannot_overwrite(self):
        self.register()
        self.service.enqueue(1, 'event-first', '/'); self.service.enqueue(1, 'event-second', '/')
        leases = []
        def slow(route, payload):
            job_id = payload[0]['data']['notification_id']
            with self.service.db() as cur:
                self.service.execute(cur, 'SELECT due_at FROM push_outbox WHERE id=?', (job_id,))
                leases.append(self.service.rows(cur)[0]['due_at'])
            self.assertEqual(leases[-1], self.now + 120)
            self.now += 180
            # Simulate a newer worker claiming after the lease expired. The old
            # response must not undo the newer worker's recorded delivery.
            with self.service.db() as cur:
                self.service.execute(cur, "UPDATE push_outbox SET state='delivered',attempts=attempts+1 WHERE id=?", (job_id,))
            return {'data': [{'status': 'ok', 'id': 'late-ticket'}]}
        self.service.http = slow; self.service.deliver(limit=2)
        self.assertEqual(len(leases), 2)
        self.assertEqual(leases[1] - leases[0], 180)
        self.assertTrue(all(row['state'] == 'delivered' for row in self.rows('push_outbox')))

    def test_reinstallation_handoff_requires_same_owner_and_revoked_registration(self):
        self.register()
        self.assertEqual(self.register(installation='b' * 64).status_code, 409)
        self.post('revoke')
        self.assertEqual(self.register(installation='b' * 64).status_code, 200)
        self.assertEqual(len(self.rows('push_devices')), 1)
        self.post('revoke', {'installation': 'b' * 64})
        self.client.post('/fixture/login/2'); self.context = self.client.get('/api/push/context').json
        self.assertEqual(self.register(installation='c' * 64).status_code, 409)

    def test_existing_announcement_hook_is_line_independent_and_deduplicated(self):
        self.register()
        self.assertEqual(self.client.post('/fixture/announcement').json, {'id': 123})
        self.client.post('/fixture/announcement')
        self.assertEqual(len(self.rows('push_outbox')), 1)
        self.assertEqual(self.rows('push_outbox')[0]['path'], '/announcements/123')

    def test_cli_without_send_has_no_network(self):
        self.register(); self.enqueue()
        result = self.app.test_cli_runner().invoke(args=['push-deliver'])
        self.assertEqual(result.exit_code, 0)
        self.assertIn('No delivery', result.output)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'queued')

    def test_inquiry_reply_hook_only_notifies_committed_owner(self):
        self.register()
        conn = sqlite3.connect(self.path)
        conn.executescript('''CREATE TABLE inquiries(id INTEGER PRIMARY KEY,user_id INTEGER);
            CREATE TABLE inquiry_replies(id INTEGER PRIMARY KEY,inquiry_id INTEGER,is_admin_reply INTEGER);
            INSERT INTO inquiries VALUES(7,1);'''); conn.commit(); conn.close()
        self.client.post('/fixture/inquiry/7')
        self.assertEqual(self.rows('push_outbox')[0]['path'], '/inquiry/7')
        self.assertEqual(self.rows('push_outbox')[0]['user_id'], 1)

    def test_document_hook_notifies_completed_to_sent_transition_once(self):
        self.register()
        conn = sqlite3.connect(self.path)
        conn.executescript("CREATE TABLE shikiriosho(id INTEGER PRIMARY KEY,recipient_id INTEGER,status TEXT); INSERT INTO shikiriosho VALUES(5,1,'completed');")
        conn.commit(); conn.close()
        self.client.post('/fixture/doc'); self.client.post('/fixture/doc')
        self.assertEqual(len(self.rows('push_outbox')), 1)
        self.assertEqual(self.rows('push_outbox')[0]['path'], '/shikiriosho/view/5')

    def test_rolled_back_sale_event_is_never_notified(self):
        self.register()
        conn = sqlite3.connect(self.path)
        conn.executescript('''CREATE TABLE sale_requests(id INTEGER PRIMARY KEY,user_id INTEGER,merchandise_id INTEGER,processed_at TEXT,shipment_marked_at TEXT);
            CREATE TABLE sale_request_events(id INTEGER PRIMARY KEY,sale_request_id INTEGER,event_type TEXT);
            CREATE TABLE users(id INTEGER PRIMARY KEY,role TEXT); INSERT INTO sale_requests VALUES(4,1,77,'fixture-now','fixture-now');''')
        conn.commit(); conn.close()
        self.client.post('/fixture/sale/0'); self.assertEqual(self.rows('push_outbox'), [])
        self.client.post('/fixture/sale/1'); self.assertEqual(len(self.rows('push_outbox')), 1)


if __name__ == '__main__':
    unittest.main()
