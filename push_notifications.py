"""Opt-in Expo push outbox. Import/registration never connects to DB or Expo.

Delivery is explicit: ``flask --app render_app push-deliver --send``. Business
transactions only enqueue after commit. No message, token or credential is logged.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from urllib.request import Request, urlopen

import click
from flask import Blueprint, abort, current_app, g, has_request_context, jsonify, request, session
from flask_login import current_user, user_logged_in, user_logged_out

TOKEN = re.compile(r'^(?:ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_-]{10,200}\]$')
SECRET = re.compile(r'^[A-Za-z0-9_-]{32,128}$')
SAFE_PATH = re.compile(r'^/(?:self-inventory|inventory/intakes(?:/[0-9]+)?|announcements(?:/[0-9]+)?|inquiry(?:/[0-9]+)?|(?:shikiriosho|invoices|keisan)/view/[0-9]+|admin/sale-requests)?/?$')


class PushNotifications:
    def __init__(self, app, runtime):
        self.app, self.runtime = app, runtime
        self.postgres = bool(getattr(runtime, 'DATABASE_URL', None))
        self.clock = time.time
        self.http = self._http

    def setting(self, key):
        return str(self.app.config.get(key, os.environ.get(key, ''))).strip()

    def available(self):
        try:
            uuid.UUID(self.setting('EAS_PROJECT_ID'))
        except ValueError:
            return False
        return self.setting('PUSH_NOTIFICATIONS_ENABLED') == '1' and bool(self.setting('EXPO_ACCESS_TOKEN'))

    def execute(self, cur, sql, args=()):
        cur.execute(sql.replace('?', '%s') if self.postgres else sql, args)

    def rows(self, cur):
        names = [v[0] for v in cur.description]
        return [dict(row) if hasattr(row, 'keys') else dict(zip(names, row)) for row in cur.fetchall()]

    @contextmanager
    def db(self):
        conn = self.runtime.get_db()
        cur = conn.cursor()
        try:
            if not self.postgres:
                cur.execute('BEGIN IMMEDIATE')
            cur.execute('''CREATE TABLE IF NOT EXISTS push_devices (
                installation TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL,
                user_id INTEGER NOT NULL, binding TEXT NOT NULL, project_id TEXT NOT NULL,
                platform TEXT NOT NULL, enabled INTEGER NOT NULL, expires_at BIGINT NOT NULL)''')
            cur.execute('''CREATE TABLE IF NOT EXISTS push_outbox (
                id TEXT PRIMARY KEY, event_key TEXT NOT NULL, installation TEXT NOT NULL,
                user_id INTEGER NOT NULL, binding TEXT NOT NULL, token TEXT NOT NULL,
                path TEXT NOT NULL, event_type TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL, due_at BIGINT NOT NULL, created_at BIGINT NOT NULL,
                receipt TEXT, UNIQUE (event_key, installation))''')
            cur.execute('CREATE INDEX IF NOT EXISTS push_outbox_due ON push_outbox (state, due_at)')
            yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def revoke_binding(self, binding):
        if not binding:
            return
        with self.db() as cur:
            self.execute(cur, 'UPDATE push_devices SET enabled=0 WHERE binding=?', (self.digest(binding),))

    def rotate(self, sender, user=None, **kwargs):
        self.revoke_binding(session.pop('_push_binding', None))
        session.pop('_push_user', None)
        session.pop('_push_csrf', None)

    def identity(self):
        if not current_user.is_authenticated:
            return None
        user_id = int(current_user.get_id())
        if session.get('_push_user') != user_id:
            self.rotate(None)
            session['_push_user'] = user_id
        if not session.get('_push_binding'):
            session['_push_binding'] = secrets.token_urlsafe(32)
        if not session.get('_push_csrf'):
            session['_push_csrf'] = secrets.token_urlsafe(32)
        return user_id

    def authorized_body(self):
        user_id = self.identity()
        if user_id is None:
            abort(401)
        if request.content_length is None or request.content_length > 4096 or not request.is_json:
            abort(400)
        expected = session.get('_push_csrf', '')
        if not expected or not hmac.compare_digest(expected, request.headers.get('X-CSRF-Token', '')):
            abort(400)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not hmac.compare_digest(str(data.get('account_key', '')), session['_push_binding']):
            abort(409)
        installation = str(data.get('installation', ''))
        if not SECRET.fullmatch(installation):
            abort(400)
        return user_id, data, self.digest(installation)

    def enqueue(self, user_id, event_key, path, event_type='status'):
        if not self.available():
            return 0
        if not SAFE_PATH.fullmatch(path) or not isinstance(event_key, str) or not 1 <= len(event_key) <= 240:
            raise ValueError('Invalid push event destination or key')
        if event_type not in ('inventory', 'document', 'announcement', 'status'):
            raise ValueError('Invalid push event type')
        now = int(self.clock())
        with self.db() as cur:
            self.execute(cur, '''SELECT * FROM push_devices WHERE user_id=? AND enabled=1
                AND expires_at>? AND project_id=?''', (int(user_id), now, self.setting('EAS_PROJECT_ID')))
            devices = self.rows(cur)
            for device in devices:
                self.execute(cur, '''INSERT INTO push_outbox
                    (id,event_key,installation,user_id,binding,token,path,event_type,state,attempts,due_at,created_at)
                    VALUES (?,?,?,?,?,?,?,?, 'queued',0,?,?) ON CONFLICT (event_key,installation) DO NOTHING''',
                    (str(uuid.uuid4()), event_key, device['installation'], int(user_id), device['binding'],
                     device['token'], path, event_type, now, now))
        return len(devices)

    def safe_enqueue(self, *args, **kwargs):
        try:
            return self.enqueue(*args, **kwargs)
        except Exception:
            self.app.logger.warning('Push enqueue failed; existing business operation retained.')
            return 0

    def _http(self, route, payload):
        req = Request('https://exp.host/--/api/v2/push/' + route,
            data=json.dumps(payload).encode(), method='POST', headers={
                'Content-Type': 'application/json', 'Accept': 'application/json',
                'Authorization': 'Bearer ' + self.setting('EXPO_ACCESS_TOKEN')})
        with urlopen(req, timeout=20) as response:
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise ValueError('Oversized Expo response')
            return json.loads(body)

    def deliver(self, limit=50):
        """Bounded worker tick, at-least-once transport; checks device before each send.

        A ticket is not delivery. Receipts are checked after 15 minutes, with a
        24-hour expiry. Network failures use capped exponential retry.
        """
        if not self.available():
            return {'processed': 0, 'available': False}
        now = int(self.clock())
        processed = 0
        with self.db() as cur:
            self.execute(cur, 'DELETE FROM push_outbox WHERE created_at<?', (now - 30 * 86400,))
            self.execute(cur, 'DELETE FROM push_devices WHERE expires_at<?', (now - 30 * 86400,))
        for _ in range(min(max(int(limit), 0), 100)):
            now = int(self.clock())
            with self.db() as cur:
                self.execute(cur, '''SELECT q.* FROM push_outbox q WHERE q.state IN ('queued','receipt','sending')
                    AND q.due_at<=? ORDER BY q.due_at LIMIT 1''' + (' FOR UPDATE SKIP LOCKED' if self.postgres else ''), (now,))
                rows = self.rows(cur)
                if not rows:
                    break
                job = rows[0]
                self.execute(cur, '''SELECT * FROM push_devices WHERE installation=? AND user_id=?
                    AND binding=? AND token=? AND enabled=1 AND expires_at>? AND project_id=?''',
                    (job['installation'], job['user_id'], job['binding'], job['token'], now, self.setting('EAS_PROJECT_ID')))
                valid = bool(self.rows(cur))
                if not valid or now - job['created_at'] > 86400 or job['attempts'] >= 8:
                    self.execute(cur, "UPDATE push_outbox SET state='cancelled' WHERE id=?", (job['id'],))
                    continue
                self.execute(cur, "UPDATE push_outbox SET state='sending', due_at=?, attempts=attempts+1 WHERE id=?", (now + 120, job['id']))
            processed += 1
            state, receipt, due = 'queued', job['receipt'], now + min(3600, 30 * 2 ** job['attempts'])
            invalid = False
            try:
                if receipt:
                    response = self.http('getReceipts', {'ids': [receipt]})
                    result = response.get('data', {}).get(receipt)
                    state = 'receipt'
                    if result is None:
                        due = now + 900
                    elif result.get('status') == 'ok':
                        state = 'delivered'
                    else:
                        error = result.get('details', {}).get('error')
                        invalid = error == 'DeviceNotRegistered'
                        if error == 'MessageRateExceeded':
                            receipt, state = None, 'queued'
                        else:
                            state = 'failed'
                else:
                    response = self.http('send', [{
                        'to': job['token'], 'title': '開花からのお知らせ',
                        'body': '更新のお知らせがあります。アプリでご確認ください。',
                        'sound': 'default', 'channelId': 'kaika-updates', 'ttl': 3600,
                        'data': {'notification_id': job['id']}}])
                    values = response.get('data')
                    if not isinstance(values, list) or len(values) != 1:
                        raise ValueError('Invalid Expo ticket response')
                    result = values[0]
                    if result.get('status') == 'ok' and isinstance(result.get('id'), str):
                        receipt, state, due = result['id'], 'receipt', now + 900
                    else:
                        error = result.get('details', {}).get('error')
                        invalid = error == 'DeviceNotRegistered'
                        state = 'queued' if error == 'MessageRateExceeded' else 'failed'
            except Exception:
                state = 'receipt' if receipt else 'queued'
            with self.db() as cur:
                self.execute(cur, '''UPDATE push_outbox SET state=?, receipt=?, due_at=?
                    WHERE id=? AND state='sending' AND due_at=? AND attempts=?''',
                    (state, receipt, due, job['id'], now + 120, job['attempts'] + 1))
                if invalid and cur.rowcount:
                    self.execute(cur, 'UPDATE push_devices SET enabled=0 WHERE installation=? AND token=? AND binding=?',
                        (job['installation'], job['token'], job['binding']))
        return {'processed': processed, 'available': True}


def register_push_notifications(app, runtime):
    if 'push_notifications' in app.extensions:
        return app.extensions['push_notifications']
    service = PushNotifications(app, runtime)
    app.extensions['push_notifications'] = service
    bp = Blueprint('push_notifications', __name__)

    @bp.get('/api/push/context')
    def context():
        identity = service.identity()
        if identity is None:
            return jsonify(available=False, authenticated=False)
        return jsonify(available=service.available(), authenticated=True,
            project_id=service.setting('EAS_PROJECT_ID') if service.available() else None,
            account_key=session['_push_binding'], csrf_token=session['_push_csrf'])

    @bp.post('/api/push/register')
    def register():
        user_id, data, installation = service.authorized_body()
        if not service.available():
            abort(503)
        token = str(data.get('token', ''))
        if not TOKEN.fullmatch(token) or data.get('platform') not in ('ios', 'android') or data.get('project_id') != service.setting('EAS_PROJECT_ID') or data.get('consent') is not True:
            abort(400)
        with service.db() as cur:
            service.execute(cur, 'SELECT installation,user_id,enabled,expires_at FROM push_devices WHERE token=?', (token,))
            rows = service.rows(cur)
            if rows and rows[0]['installation'] != installation:
                previous = rows[0]
                if previous['user_id'] != user_id or (previous['enabled'] and previous['expires_at'] > int(service.clock())):
                    abort(409)
                # A reinstallation may retain the Expo token. Only the same
                # authenticated owner can replace a previously revoked/expired
                # installation; queued events for the old installation stay void.
                service.execute(cur, 'DELETE FROM push_devices WHERE installation=?', (previous['installation'],))
            service.execute(cur, '''INSERT INTO push_devices (installation,token,user_id,binding,project_id,platform,enabled,expires_at)
                VALUES (?,?,?,?,?,?,1,?) ON CONFLICT (installation) DO UPDATE SET token=excluded.token,
                user_id=excluded.user_id,binding=excluded.binding,project_id=excluded.project_id,
                platform=excluded.platform,enabled=1,expires_at=excluded.expires_at''',
                (installation, token, user_id, service.digest(session['_push_binding']), data['project_id'], data['platform'], int(service.clock()) + 30 * 86400))
        return jsonify(enabled=True)

    @bp.post('/api/push/status')
    def status():
        user_id, data, installation = service.authorized_body()
        with service.db() as cur:
            service.execute(cur, '''SELECT enabled,expires_at FROM push_devices WHERE installation=?
                AND user_id=? AND binding=?''', (installation, user_id, service.digest(session['_push_binding'])))
            rows = service.rows(cur)
        return jsonify(enabled=bool(rows and rows[0]['enabled'] and rows[0]['expires_at'] > int(service.clock())))

    @bp.post('/api/push/revoke')
    def revoke():
        user_id, data, installation = service.authorized_body()
        with service.db() as cur:
            service.execute(cur, 'UPDATE push_devices SET enabled=0 WHERE installation=? AND user_id=? AND binding=?',
                (installation, user_id, service.digest(session['_push_binding'])))
        return jsonify(enabled=False)

    @bp.post('/api/push/open')
    def open_notification():
        user_id, data, installation = service.authorized_body()
        with service.db() as cur:
            service.execute(cur, '''SELECT q.path FROM push_outbox q JOIN push_devices d ON d.installation=q.installation
                WHERE q.id=? AND q.installation=? AND q.user_id=? AND d.user_id=? AND q.binding=d.binding
                AND d.binding=? AND d.enabled=1 AND d.expires_at>?''',
                (str(data.get('notification_id', '')), installation, user_id, user_id,
                 service.digest(session['_push_binding']), int(service.clock())))
            rows = service.rows(cur)
        if not rows or not SAFE_PATH.fullmatch(rows[0]['path']):
            abort(404)
        return jsonify(path=rows[0]['path'])

    @bp.after_request
    def private_response(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

    app.register_blueprint(bp)
    user_logged_in.connect(service.rotate, app, weak=False)
    user_logged_out.connect(service.rotate, app, weak=False)

    @app.cli.command('push-deliver')
    @click.option('--send', is_flag=True, help='Explicitly allow Expo network delivery and receipt queries.')
    @click.option('--limit', default=50, type=click.IntRange(1, 100))
    def deliver(send, limit):
        if not send:
            click.echo('No delivery: add --send after completing push credentials and device tests.')
            return
        click.echo(json.dumps(service.deliver(limit)))

    # Register before runtime patches capture these helpers. LINE is untouched.
    for name in ('create_targeted_system_announcement', 'create_targeted_system_announcement_once'):
        original = getattr(runtime, name, None)
        if not callable(original):
            continue
        def decorate(fn):
            @wraps(fn)
            def wrapped(user_id, *args, **kwargs):
                result = fn(user_id, *args, **kwargs)
                announcement_id = result.get('announcement_id') if isinstance(result, dict) else result
                if announcement_id:
                    service.safe_enqueue(user_id, f'announcement:{announcement_id}', f'/announcements/{int(announcement_id)}', 'announcement')
                return result
            return wrapped
        setattr(runtime, name, decorate(original))
    record_event = getattr(runtime, 'record_sale_request_event', None)
    if callable(record_event):
        @wraps(record_event)
        def record_with_push(conn, sale_request_id, event_type, *args, **kwargs):
            result = record_event(conn, sale_request_id, event_type, *args, **kwargs)
            if service.available() and has_request_context() and sale_request_id:
                # Read the ID inside the business transaction; enqueue only after
                # the handler returns and a separate connection sees its commit.
                cur = conn.cursor()
                try:
                    service.execute(cur, '''SELECT id FROM sale_request_events WHERE sale_request_id=? AND event_type=? ORDER BY id DESC LIMIT 1''', (sale_request_id, event_type))
                    rows = service.rows(cur)
                    if rows:
                        g.push_sale_events = getattr(g, 'push_sale_events', []) + [rows[0]['id']]
                except Exception:
                    app.logger.warning('Push sale event observation unavailable.')
                finally:
                    cur.close()
            return result
        runtime.record_sale_request_event = record_with_push

    notify_status = getattr(runtime, 'notify_sale_request_user_status', None)
    if callable(notify_status):
        @wraps(notify_status)
        def notify_status_with_push(item_dict, user_id, event_type, *args, **kwargs):
            result = notify_status(item_dict, user_id, event_type, *args, **kwargs)
            if service.available() and user_id:
                try:
                    with service.db() as cur:
                        service.execute(cur, '''SELECT id,processed_at,shipment_marked_at FROM sale_requests
                            WHERE merchandise_id=? AND user_id=? ORDER BY id DESC LIMIT 1''', (item_dict.get('id'), user_id))
                        rows = service.rows(cur)
                        events = []
                        if rows:
                            service.execute(cur, 'SELECT id,event_type FROM sale_request_events WHERE sale_request_id=? ORDER BY id DESC LIMIT 1', (rows[0]['id'],))
                            events = service.rows(cur)
                    if rows:
                        row = rows[0]
                        key = (f'sale-event:{events[0]["id"]}' if events and events[0]['event_type'] == event_type else
                            f'sale-status:{row["id"]}:{event_type}:{row["processed_at"]}:{row["shipment_marked_at"]}')
                        service.safe_enqueue(user_id, key, '/', 'status')
                except Exception:
                    app.logger.warning('Push sale status enqueue unavailable.')
            return result
        runtime.notify_sale_request_user_status = notify_status_with_push

    notify_admins = getattr(runtime, 'notify_admins_of_sale_request', None)
    if callable(notify_admins):
        @wraps(notify_admins)
        def notify_admins_with_push(item_dict, request_type, *args, **kwargs):
            result = notify_admins(item_dict, request_type, *args, **kwargs)
            if service.available() and has_request_context() and current_user.is_authenticated:
                try:
                    with service.db() as cur:
                        service.execute(cur, '''SELECT id FROM sale_requests WHERE merchandise_id=? AND user_id=?
                            AND status='pending' ORDER BY id DESC LIMIT 1''', (item_dict.get('id'), int(current_user.get_id())))
                        requests = service.rows(cur)
                        service.execute(cur, "SELECT id FROM users WHERE role IN ('admin','owner')")
                        admins = service.rows(cur)
                    if requests:
                        for admin in admins:
                            service.safe_enqueue(admin['id'], f'sale-request:{requests[0]["id"]}:pending', '/admin/sale-requests', 'status')
                except Exception:
                    app.logger.warning('Push admin request enqueue unavailable.')
            return result
        runtime.notify_admins_of_sale_request = notify_admins_with_push

    @app.before_request
    def observe_business_write():
        if not service.available() or not current_user.is_authenticated:
            return
        endpoint = request.endpoint or ''
        try:
            if endpoint == 'admin_inquiry_reply' and request.method == 'POST':
                with service.db() as cur:
                    service.execute(cur, 'SELECT COALESCE(MAX(id),0) AS last_id FROM inquiry_replies WHERE inquiry_id=?', (request.view_args['id'],))
                    g.push_inquiry_before = service.rows(cur)[0]['last_id']
            tables = {'admin_shikiriosho_send': 'shikiriosho', 'admin_shikiriosho_send_bulk': 'shikiriosho', 'admin_auction_keisan_send_bulk': 'user_keisan'}
            if endpoint in tables:
                table = tables[endpoint]
                with service.db() as cur:
                    service.execute(cur, f"SELECT id FROM {table} WHERE status='completed'")
                    g.push_documents_before = (table, [row['id'] for row in service.rows(cur)])
        except Exception:
            app.logger.warning('Push business observation unavailable; existing operation retained.')

    @app.after_request
    def enqueue_business_writes(response):
        if not service.available() or response.status_code >= 400:
            return response
        try:
            for event_id in getattr(g, 'push_sale_events', []):
                with service.db() as cur:
                    service.execute(cur, '''SELECT e.id,e.event_type,r.user_id FROM sale_request_events e
                        JOIN sale_requests r ON r.id=e.sale_request_id WHERE e.id=?''', (event_id,))
                    events = service.rows(cur)
                    service.execute(cur, "SELECT id FROM users WHERE role IN ('admin','owner')")
                    admins = service.rows(cur)
                for event in events:
                    service.safe_enqueue(event['user_id'], f'sale-event:{event_id}', '/', 'status')
                    if event['event_type'] in ('submitted', 'resubmitted', 'updated', 'cancelled'):
                        for admin in admins:
                            service.safe_enqueue(admin['id'], f'sale-event:{event_id}', '/admin/sale-requests', 'status')
            if hasattr(g, 'push_inquiry_before'):
                with service.db() as cur:
                    service.execute(cur, '''SELECT r.id,i.user_id,i.id AS inquiry_id FROM inquiry_replies r
                        JOIN inquiries i ON i.id=r.inquiry_id WHERE r.inquiry_id=? AND r.id>? AND r.is_admin_reply=?''',
                        (request.view_args['id'], g.push_inquiry_before, True if service.postgres else 1))
                    replies = service.rows(cur)
                for reply in replies:
                    service.safe_enqueue(reply['user_id'], f'inquiry-reply:{reply["id"]}', f'/inquiry/{reply["inquiry_id"]}', 'status')
            if hasattr(g, 'push_documents_before'):
                table, ids = g.push_documents_before
                with service.db() as cur:
                    recipient = 'recipient_id' if table == 'shikiriosho' else 'user_id'
                    service.execute(cur, f"SELECT id,{recipient} AS recipient_id FROM {table} WHERE status IN ('sent','submitted')")
                    documents = service.rows(cur)
                for document in documents:
                    if document['id'] in ids and document['recipient_id']:
                        prefix = 'shikiriosho' if table == 'shikiriosho' else 'keisan'
                        service.safe_enqueue(document['recipient_id'], f'document:{table}:{document["id"]}:sent', f'/{prefix}/view/{document["id"]}', 'document')
        except Exception:
            app.logger.warning('Push business enqueue failed; existing operation retained.')
        return response
    return service
