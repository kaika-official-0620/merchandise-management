"""Disposable SQLite, fake users, fake Expo only; never imports app/render_app."""
import sqlite3
import tempfile
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
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'delivered')
        self.assertEqual([call[0] for call in self.calls], ['send', 'getReceipts'])

    def test_invalid_receipt_deactivates_exact_device(self):
        self.register(); self.enqueue()
        self.service.http = lambda route, payload: {'data': [{'status': 'ok', 'id': 'bad-ticket'}]} if route == 'send' else {'data': {'bad-ticket': {'status': 'error', 'details': {'error': 'DeviceNotRegistered'}}}}
        self.service.deliver(); self.now += 900; self.service.deliver()
        self.assertEqual(self.rows('push_devices')[0]['enabled'], 0)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'failed')

    def test_transient_failure_retries_without_infinite_loop(self):
        self.register(); self.enqueue()
        def failing(route, payload):
            self.calls.append(route)
            raise TimeoutError()
        self.service.http = failing
        self.service.deliver(); self.service.deliver()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.rows('push_outbox')[0]['state'], 'queued')
        self.now += 30; self.service.deliver()
        self.assertEqual(len(self.calls), 2)

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
