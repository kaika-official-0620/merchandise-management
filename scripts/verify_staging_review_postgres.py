"""Real Flask staging-review boot/reboot against a newly owned local PostgreSQL.

Accepts a portable binary directory, never a connection URL or environment DSN.
The parent creates and stops its own TLS cluster; isolated child processes run
the source-only app mirror. Passwords and raw app output stay in that temporary
fixture. This does not exercise Render mounts, delivery providers or devices.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from html.parser import HTMLParser
import importlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import quote, urlsplit, urlunsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Flask test_client uses this only as an in-process Host header. No HTTP client
# connects to this hostname, and every application socket remains prohibited.
HTTP_HOST = 'kaika-platform-staging.onrender.com'


def safe_error(error):
    return {'type': type(error).__name__, 'frames': [
        {'file': Path(frame.filename).name, 'line': frame.lineno}
        for frame in traceback.extract_tb(error.__traceback__)]}


def write_report(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def verify_access(runtime, config, access, state, state_path, users, phase_name, check):
    """Exercise only fixture tokens; no bearer material enters reports/logs."""
    base = 'https://' + HTTP_HOST
    app = runtime.app
    business_id = users['business']['id']

    class Inputs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.values = {}

        def handle_starttag(self, tag, attributes):
            attributes = dict(attributes)
            if tag == 'input' and attributes.get('name'):
                self.values[attributes['name']] = attributes.get('value', '')

    def mint(ttl=900):
        with closing(runtime.get_db()) as connection:
            return access.mint_staging_access(config, connection=connection, ttl_seconds=ttl)

    def claimed(token):
        with closing(runtime.get_db()) as connection:
            return access.claim_staging_access(config, connection=connection, token=token)

    def row(token):
        with closing(runtime.get_db()) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT token_hash,user_id,password_fingerprint,created_at,expires_at,consumed_at '
                    'FROM kaika_staging_access_tokens WHERE token_hash=%s', (hashlib.sha256(token.encode()).hexdigest(),))
                return cursor.fetchone()

    def prepare_client():
        client = app.test_client()
        response = client.get('/staging/access', base_url=base)
        fields = Inputs()
        fields.feed(response.get_data(as_text=True))
        check('access_page_public_https', response.status_code == 200)
        check('access_page_private', 'no-store' in response.headers.get('Cache-Control', '')
              and response.headers.get('Referrer-Policy') == 'no-referrer')
        check('access_page_session_csrf', bool(fields.values.get('csrf_token')))
        check('access_fragment_page_markup', 'location.hash' in response.get_data(as_text=True)
              and 'replaceState' in response.get_data(as_text=True))
        return client, fields.values.get('csrf_token', '')

    def submit(client, token, csrf, origin=base):
        return client.post('/staging/access/consume', base_url=base,
            json={'token': token, 'csrf_token': csrf}, headers={'Origin': origin})

    check('access_extension_registered', bool(app.extensions.get('kaika_staging_access')))
    if phase_name == 'restart':
        check('access_restart_used_token_stays_rejected', claimed(state['used_access_token']) is None)
        check('access_restart_pending_token_survives', claimed(state['pending_access_token']) == business_id)
        check('access_restart_pending_becomes_one_use', claimed(state['pending_access_token']) is None)
    token = mint()
    stored = row(token)
    check('access_database_hash_only', stored is not None and stored[0] == hashlib.sha256(token.encode()).hexdigest()
          and len(stored[0]) == 64 and token not in tuple(str(value) for value in stored))
    check('access_fixed_business_lifetime', stored[1] == business_id and stored[4] - stored[3] == 900 and stored[5] is None)
    client, csrf = prepare_client()
    check('access_get_does_not_consume', row(token)[5] is None)
    check('access_cross_origin_rejected', submit(client, token, csrf, 'https://other.example.invalid').status_code == 403)
    check('access_wrong_csrf_rejected', submit(client, token, 'fictional-wrong-csrf').status_code == 403)
    check('access_csrf_bound_to_session', submit(app.test_client(), token, csrf).status_code == 403)
    check('access_refusals_keep_token_unused', row(token)[5] is None)
    consumed = submit(client, token, csrf)
    check('access_post_one_use_login', consumed.status_code == 200 and
          consumed.get_json() == {'ok': True, 'next': '/workspace-home'})
    with client.session_transaction(base_url=base) as session:
        check('access_session_business_only', session.get('_user_id') == str(business_id)
              and all(token != str(value) for value in session.values()))
    check('access_login_home', client.get('/workspace-home', base_url=base).status_code == 200)
    replay_client, replay_csrf = prepare_client()
    replay = submit(replay_client, token, replay_csrf)
    check('access_web_replay_rejected', replay.status_code in (400, 401, 403, 410))
    with replay_client.session_transaction(base_url=base) as session:
        check('access_replay_stays_anonymous', '_user_id' not in session)
    check('access_database_replay_rejected', claimed(token) is None)

    expiring = mint(ttl=1)
    time.sleep(2)
    check('access_expired_rejected', claimed(expiring) is None and row(expiring)[5] is None)
    ineligible = mint()
    try:
        with closing(runtime.get_db()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE users SET role='admin' WHERE id=%s", (business_id,))
            connection.commit()
        check('access_ineligible_user_rejected', claimed(ineligible) is None and row(ineligible)[5] is None)
    finally:
        with closing(runtime.get_db()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("UPDATE users SET role='user' WHERE id=%s", (business_id,))
            connection.commit()

    racing = mint()
    digest = hashlib.sha256(racing.encode()).hexdigest()
    with closing(runtime.get_db()) as first, closing(runtime.get_db()) as second:
        check('access_race_independent_connections', first.get_backend_pid() != second.get_backend_pid())
        with first.cursor() as cursor:
            cursor.execute('SELECT token_hash FROM kaika_staging_access_tokens WHERE token_hash=%s FOR UPDATE', (digest,))
        started = threading.Event()

        def contender():
            started.set()
            return access.claim_staging_access(config, connection=second, token=racing)

        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(contender)
            try:
                if not started.wait(5):
                    raise AssertionError('Fixture token contender did not start')
                waiting = False
                deadline = time.monotonic() + 5
                with closing(runtime.get_db()) as observer:
                    observer.autocommit = True
                    while time.monotonic() < deadline:
                        with observer.cursor() as cursor:
                            cursor.execute('SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s', (second.get_backend_pid(),))
                            wait_row = cursor.fetchone()
                        if wait_row and wait_row[0] == 'Lock':
                            waiting = True
                            break
                        time.sleep(.02)
                check('access_race_actual_postgres_lock_wait', waiting)
                winner = access.claim_staging_access(config, connection=first, token=racing)
            finally:
                first.rollback()
            loser = pending.result(timeout=10)
        check('access_race_exactly_one_winner', winner == business_id and loser is None)
    check('access_race_consumption_persisted', row(racing)[5] is not None and claimed(racing) is None)

    if phase_name == 'initialize':
        # Like generated fixture account passwords, these are retained only in
        # the owned private temporary workspace to exercise a separate process.
        state['used_access_token'] = token
        state['pending_access_token'] = mint()
        write_report(state_path, state)
        check('access_pending_token_prepared_for_restart', row(state['pending_access_token'])[5] is None)


def verify_documents(runtime, state, state_path, users, clients, phase_name, check):
    """Real fixture HTTP creation, independent submission and PG item locking.

    Only the four existing fictional fixture products are referenced. The
    records created here have no billing, outbound delivery or customer data.
    """
    app, base = runtime.app, 'https://' + HTTP_HOST
    normal, other, admin = clients['normal'], clients['business'], clients['admin']

    class Tokens(HTMLParser):
        value = ''

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'meta' and attrs.get('name') == 'kaika-security-csrf':
                self.value = attrs.get('content', '')

    def fetch_rows(sql, values=()):
        with closing(runtime.get_db()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, values)
                return cursor.fetchall()

    def token(client):
        response = client.get('/mitsumori/add', base_url=base)
        parsed = Tokens()
        parsed.feed(response.get_data(as_text=True))
        check('document_form_get_' + ('owner' if client is normal else 'other'),
              response.status_code == 200 and len(parsed.value) == 43)
        return parsed.value

    owner_token, other_token = token(normal), token(other)

    def candidates(client):
        response = client.get('/api/document-products?inventory_only=1', base_url=base)
        check('document_candidate_api_http_' + ('owner' if client is normal else 'other'), response.status_code == 200)
        return {int(row['id']) for row in (response.get_json() or [])}

    def send(client, document_id, csrf=owner_token, destination='kaika'):
        return client.post('/documents/submit/estimate/' + str(document_id), base_url=base,
            data={'security_csrf_token': csrf, 'destination': destination})

    def event_count(document_id):
        return fetch_rows("SELECT COUNT(*) FROM final_document_events WHERE document_kind='user_mitsumori' AND document_id=%s AND action='customer_submit'", (document_id,))[0][0]

    check('document_workflow_registered', 'kaika_document_workflow' in app.extensions)
    owner_ids = {row[0] for row in fetch_rows("SELECT id FROM merchandise WHERE user_id=%s AND scope='user'", (users['normal']['id'],))}
    other_ids = {row[0] for row in fetch_rows("SELECT id FROM merchandise WHERE user_id=%s AND scope='user'", (users['business']['id'],))}
    check('document_candidates_private_to_owner', candidates(normal).issubset(owner_ids) and not candidates(normal) & other_ids)
    check('document_candidates_private_to_other', candidates(other).issubset(other_ids) and not candidates(other) & owner_ids)
    check('document_candidate_filter_rejects_invalid', normal.get('/api/document-products?sold_days=bad', base_url=base).status_code == 400)
    check('document_staff_candidate_api_denied', admin.get('/api/document-products', base_url=base).status_code == 403)
    check('document_customer_staff_inbox_denied', normal.get('/admin/customer-submissions', base_url=base).status_code == 403)

    if phase_name == 'initialize':
        subject = 'fictional-document-review-' + secrets.token_hex(6)
        form = {'status': 'completed', 'issue_date': '2026-10-27', 'company_name': 'fictional-kaika',
                'subject': subject, 'item_name[]': ['fictional-preserve-on-reboot'],
                'merchandise_id[]': [str(state['legacy_id'])], 'quantity[]': ['1'],
                'unit[]': ['item'], 'unit_price[]': ['1000'], 'security_csrf_token': owner_token}
        check('document_save_forged_submission_rejected', normal.post('/mitsumori/add', base_url=base,
              data={**form, 'status': 'submitted'}).status_code == 400)
        check('document_save_other_customer_product_rejected', normal.post('/mitsumori/add', base_url=base,
              data={**form, 'merchandise_id[]': [str(next(iter(other_ids)))]}).status_code == 404)
        check('document_invalid_price_rejected_before_save', normal.post('/mitsumori/add', base_url=base,
              data={**form, 'unit_price[]': ['1.5']}).status_code == 400)
        for number in range(2):
            saved = normal.post('/mitsumori/add', base_url=base, data=form)
            check('document_direct_completion_without_preview_' + str(number), saved.status_code == 302)
        rows = fetch_rows('SELECT id,status,total_amount,document_no FROM user_mitsumori WHERE user_id=%s AND subject=%s ORDER BY id',
                          (users['normal']['id'], subject))
        check('document_two_distinct_completed_unsent', len(rows) == 2 and all(row[1] == 'completed' and row[2] == 1000 for row in rows))
        if len(rows) != 2:
            raise AssertionError('Fixture document creation did not return two saved documents')
        first, second = rows[0][0], rows[1][0]
        check('document_save_does_not_submit_or_emit_event', event_count(first) == 0 and event_count(second) == 0)
        check('document_completed_unsent_product_still_available', state['legacy_id'] in candidates(normal))
        inbox = admin.get('/admin/customer-submissions', base_url=base)
        check('document_staff_inbox_excludes_unsent', inbox.status_code == 200 and
              ('/admin/customer-submissions/estimate/' + str(first)) not in inbox.get_data(as_text=True))
        check('document_staff_unsent_detail_denied', admin.get('/admin/customer-submissions/estimate/' + str(first), base_url=base).status_code == 404)
        detail = normal.get('/mitsumori/view/' + str(first), base_url=base)
        check('document_completed_detail_has_separate_send', detail.status_code == 200 and
              ('/documents/submit/estimate/' + str(first)) in detail.get_data(as_text=True))
        check('document_other_customer_submission_denied', send(other, first, other_token).status_code == 404)
        check('document_invalid_csrf_denied', send(normal, first, 'fictional-invalid').status_code == 400)
        check('document_invalid_destination_denied', send(normal, first, destination='outside').status_code == 400)
        check('document_invalid_requests_preserve_completed', fetch_rows('SELECT status FROM user_mitsumori WHERE id=%s', (first,))[0][0] == 'completed')

        # Both independent HTTP submissions wait on the same actual PG item
        # lock. Releasing it must permit only one distinct document to submit.
        with normal.session_transaction(base_url=base) as session:
            original_session = dict(session)

        def concurrent_submit(document_id):
            contender = app.test_client()
            with contender.session_transaction(base_url=base) as session:
                session.update(original_session)
            return send(contender, document_id).status_code

        with closing(runtime.get_db()) as blocker:
            with blocker.cursor() as cursor:
                cursor.execute('SELECT id FROM merchandise WHERE id=%s FOR UPDATE', (state['legacy_id'],))
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = [pool.submit(concurrent_submit, value) for value in (first, second)]
                try:
                    waiting = False
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        waiting_count = fetch_rows("SELECT COUNT(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event_type='Lock'")[0][0]
                        if waiting_count >= 2:
                            waiting = True
                            break
                        if all(task.done() for task in pending):
                            break
                        time.sleep(.05)
                    check('document_two_submissions_wait_on_actual_pg_lock', waiting)
                finally:
                    blocker.rollback()
                statuses = sorted(task.result(timeout=15) for task in pending)
            check('document_same_item_exactly_one_submission', statuses == [302, 409])
        final_rows = fetch_rows('SELECT id,status,document_no FROM user_mitsumori WHERE id IN (%s,%s) ORDER BY id', (first, second))
        winners = [row for row in final_rows if row[1] == 'submitted']
        losers = [row for row in final_rows if row[1] == 'completed']
        check('document_competing_draft_kept_without_submission', len(winners) == 1 and len(losers) == 1)
        if len(winners) != 1 or len(losers) != 1:
            raise AssertionError('Fixture document submission did not have exactly one winner')
        state['document_review'] = {'winner': winners[0][0], 'loser': losers[0][0], 'number': winners[0][2], 'subject': subject}
        write_report(state_path, state)
    else:
        saved = state['document_review']
        persisted = dict(fetch_rows('SELECT id,status FROM user_mitsumori WHERE id IN (%s,%s)', (saved['winner'], saved['loser'])))
        check('document_restart_submission_and_unsent_preserved', persisted == {saved['winner']: 'submitted', saved['loser']: 'completed'})

    saved = state['document_review']
    winner, loser = saved['winner'], saved['loser']
    check('document_replay_is_safe', send(normal, winner).status_code == 302)
    check('document_one_submission_event_only', event_count(winner) == 1 and event_count(loser) == 0)
    check('document_other_saved_document_conflicts', send(normal, loser).status_code == 409)
    check('document_sent_product_excluded_from_candidates', state['legacy_id'] not in candidates(normal))
    inbox = admin.get('/admin/customer-submissions', base_url=base)
    check('document_staff_inbox_receives_winner', inbox.status_code == 200 and
          ('/admin/customer-submissions/estimate/' + str(winner)) in inbox.get_data(as_text=True) and
          ('/admin/customer-submissions/estimate/' + str(loser)) not in inbox.get_data(as_text=True))
    received = admin.get('/admin/customer-submissions/estimate/' + str(winner), base_url=base)
    check('document_staff_received_detail_matches_saved', received.status_code == 200 and saved['number'] in received.get_data(as_text=True))
    check('document_other_customer_received_detail_denied', other.get('/admin/customer-submissions/estimate/' + str(winner), base_url=base).status_code == 403)
    check('document_customer_cannot_change_sent_document', normal.post('/mitsumori/edit/' + str(winner), base_url=base,
          data={'status': 'completed', 'item_name[]': ['changed'], 'quantity[]': ['1'], 'unit_price[]': ['1']}).status_code == 409)
    check('document_fixture_products_not_deleted_or_added', fetch_rows('SELECT COUNT(*) FROM merchandise')[0][0] == 4)


def verify_source_documents(runtime, state, state_path, users, clients, phase_name, check):
    """An already-sent fictional invoice may continue into its own estimate.

    The invoice fixture is inserted only into this newly owned database: real
    invoice sending remains disabled. Its item cannot be selected generally,
    and neither another source invoice nor a mismatched snapshot is exempted.
    """
    base = 'https://' + HTTP_HOST
    business, normal, admin = clients['business'], clients['normal'], clients['admin']
    with business.session_transaction(base_url=base) as session:
        token = session['_security_csrf']
    with normal.session_transaction(base_url=base) as session:
        other_token = session['_security_csrf']

    def query(sql, values=(), write=False):
        with closing(runtime.get_db()) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, values)
                rows = cursor.fetchall() if cursor.description else []
            if write:
                connection.commit()
            return rows

    def send(document_id, client=business, csrf=token):
        return client.post('/documents/submit/estimate/' + str(document_id), base_url=base,
            data={'security_csrf_token': csrf, 'destination': 'kaika'})

    if phase_name == 'initialize':
        item_id = query("SELECT id FROM merchandise WHERE user_id=%s AND scope='user' ORDER BY id LIMIT 1", (users['business']['id'],))[0][0]
        # A sold item is deliberately used to exercise the explicit source
        # exception. This does not change any of the normal-user fixtures.
        query("UPDATE merchandise SET sale_date='2026-10-26' WHERE id=%s", (item_id,), write=True)
        prefix = 'KT-FIXTURE-' + secrets.token_hex(5)
        invoice_ids, source_rows = [], []
        for suffix in ('SOURCE', 'OTHER'):
            invoice_id = query("INSERT INTO invoices (invoice_no,sender_id,issue_date,subtotal,total_amount,status,document_scope,service_type,commission_rate,commission_amount) VALUES (%s,%s,'2026-10-27',30000,30000,'sent','client_outgoing','pending_review',0,0) RETURNING id",
                (prefix + '-' + suffix, users['business']['id']), write=True)[0][0]
            source_row = query("INSERT INTO invoice_items (invoice_id,item_no,product_name,merchandise_id,quantity,unit,unit_price,amount) VALUES (%s,1,'fictional-source-product',%s,1,'点',30000,30000) RETURNING id",
                (invoice_id, item_id), write=True)[0][0]
            invoice_ids.append(invoice_id)
            source_rows.append(source_row)
        invoice_id, unrelated_id = invoice_ids
        path = '/mitsumori/add?source_invoice_id=' + str(invoice_id)
        form = {'status': 'completed', 'source_invoice_id': str(invoice_id),
                'source_invoice_item_id[]': [str(source_rows[0])],
                'subject': 'fictional-source-continuation', 'security_csrf_token': token}
        page = business.get(path, base_url=base)
        check('document_source_owned_form_available', page.status_code == 200 and 'fictional-source-product' in page.get_data(as_text=True))
        check('document_source_other_owner_post_denied', normal.post(path, base_url=base,
            data={**form, 'security_csrf_token': other_token}).status_code == 404)
        check('document_source_other_invoice_row_denied', business.post(path, base_url=base,
            data={**form, 'source_invoice_item_id[]': [str(source_rows[1])]}).status_code == 400)
        check('document_source_excludes_only_its_own_invoice', business.post(path, base_url=base, data=form).status_code == 409)
        query("UPDATE invoices SET status='cancelled' WHERE id=%s", (unrelated_id,), write=True)
        saved = business.post(path, base_url=base, data=form)
        check('document_source_direct_completion_allowed', saved.status_code == 302)
        rows = query('SELECT id,status,source_document_id,source_document_type FROM user_mitsumori WHERE user_id=%s AND source_invoice_id=%s', (users['business']['id'], invoice_id))
        check('document_source_saved_relation_and_unsent_state', len(rows) == 1 and rows[0][1:] == ('completed', invoice_id, 'invoice'))
        if len(rows) != 1:
            raise AssertionError('The explicit source fixture did not produce one estimate')
        document_id = rows[0][0]
        query('UPDATE user_mitsumori_items SET amount=29999 WHERE mitsumori_id=%s', (document_id,), write=True)
        check('document_source_snapshot_change_denied', send(document_id).status_code == 409)
        query('UPDATE user_mitsumori_items SET amount=30000 WHERE mitsumori_id=%s', (document_id,), write=True)
        query("UPDATE invoices SET status='draft' WHERE id=%s", (invoice_id,), write=True)
        check('document_source_status_rechecked_before_send', send(document_id).status_code == 409)
        query("UPDATE invoices SET status='sent' WHERE id=%s", (invoice_id,), write=True)
        check('document_source_explicit_send_succeeds', send(document_id).status_code == 302)
        state['document_source_review'] = {'invoice_id': invoice_id, 'unrelated_id': unrelated_id,
            'document_id': document_id, 'item_id': item_id}
        write_report(state_path, state)
    saved = state['document_source_review']
    document_id = saved['document_id']
    check('document_source_sent_state_preserved', query('SELECT status FROM user_mitsumori WHERE id=%s', (document_id,))[0][0] == 'submitted')
    check('document_source_original_keeps_its_own_state', query('SELECT status FROM invoices WHERE id=%s', (saved['invoice_id'],))[0][0] == 'sent')
    check('document_source_replay_safe', send(document_id).status_code == 302)
    check('document_source_exactly_one_submit_event', query("SELECT COUNT(*) FROM final_document_events WHERE document_kind='user_mitsumori' AND document_id=%s AND action='customer_submit'", (document_id,))[0][0] == 1)
    check('document_source_other_customer_send_denied', send(document_id, normal, other_token).status_code == 404)
    check('document_source_staff_received_detail', admin.get('/admin/customer-submissions/estimate/' + str(document_id), base_url=base).status_code == 200)
    listed = business.get('/api/document-products', base_url=base)
    check('document_source_product_not_generally_selectable', listed.status_code == 200 and
          saved['item_id'] not in {int(row['id']) for row in (listed.get_json() or [])})


def phase(args):
    import psycopg2
    import run_app_preview as sandbox
    from verify_postgres_recovery import read_context

    root = args.workspace.resolve()
    context = read_context(root / 'fixture-context.json')
    state_path = root / 'private-review-state.json'
    state = json.loads(state_path.read_text(encoding='utf-8'))
    parsed = urlsplit(state['database_url'])
    if (parsed.hostname != '127.0.0.1' or parsed.port != context['port']
            or parsed.port == 5432 or parsed.path != '/' + state['database_name']):
        raise ValueError('Only this newly owned non-default fixture is supported.')
    mirror = root / 'mirror'
    if args.phase == 'initialize':
        mirror.mkdir()
        runtime_path = sandbox.mirror_source(mirror, None, None)
        state['runtime_path'] = str(runtime_path)
        write_report(state_path, state)
    else:
        runtime_path = Path(state['runtime_path']).resolve()
        if not sandbox.inside(runtime_path, mirror) or not (runtime_path / 'app.py').is_file():
            raise ValueError('The first boot source-only mirror is required.')

    pg_modules = {name: value for name, value in sys.modules.items()
                  if name == 'psycopg2' or name.startswith('psycopg2.')}
    sandbox.isolate_environment(mirror, 18794)
    sys.modules.update(pg_modules)
    os.environ.update({
        'DATABASE_URL': state['database_url'], 'KAIKA_RUNTIME_ENV': 'staging',
        'KAIKA_STAGING_DATABASE_NAME': state['database_name'],
        'STAGING_HOSTNAME': HTTP_HOST,
        'PRIMARY_DOMAIN': HTTP_HOST, 'SECRET_KEY': state['secret'],
        'KAIKA_STAGING_REVIEW_ENABLED': '1',
        'KAIKA_STAGING_ACCESS_ENABLED': '1',
    })
    for role, password in state['passwords'].items():
        os.environ['STAGING_' + role.upper() + '_PASSWORD'] = password
    connections = sandbox.install_runtime_boundary(root, 18794)
    sys.path[:] = [str(runtime_path)] + [p for p in sys.path
        if p and not sandbox.inside(Path(p).resolve(), sandbox.SOURCE)]
    original = Path.cwd()
    os.chdir(runtime_path)
    checks = {}
    report = {'phase': args.phase, 'scope': 'new owned local PostgreSQL, TLS, fictional users, source-only mirror',
              'render_disk_tested': False, 'external_delivery_tested': False,
              'real_payment_tested': False, 'checks': checks}

    def check(name, condition):
        checks[name] = bool(condition)

    try:
        boundary = importlib.import_module('staging_environment')
        review = importlib.import_module('staging_review')
        access = importlib.import_module('staging_access')
        config = boundary.validate_environment()
        boundary.prepare_environment(config)
        boundary.install_outbound_guard()
        with boundary.staging_bootstrap(config) as connection:
            loaded = importlib.import_module('render_app')
            check('current_python_source', loaded.RUNTIME_SOURCE == 'source')
            boundary.check_upload_disk(loaded.module, require_mount=False)
            seeded = boundary.seed_test_data(loaded.module, state['passwords'])
            check('seed_first_boot_only', seeded['created_users'] == (3 if args.phase == 'initialize' else 0))
            boundary.register_staging_boundary(loaded.module)
            # A normal temporary directory has no actual Render mount. Keep
            # path containment and the actual staging volume marker checks.
            with patch.object(review, 'check_upload_disk',
                              side_effect=lambda runtime: boundary.check_upload_disk(runtime, require_mount=False)):
                result = review.bootstrap_staging_review(loaded.module, config, connection=connection)
            check('review_bootstrap_enabled', result.get('enabled') is True)
            access.register_staging_access(loaded.module, config, connection=connection)
        app, runtime = loaded.app, loaded.module
        check('all_stage_2_to_7_flags_enabled', all(app.config.get(key) is True for key in review.REVIEW_FLAGS))
        check('external_and_destructive_flags_disabled', all(app.config.get(key) is False for key in review.DISABLED_REVIEW_FLAGS))
        check('scheduler_and_stripe_sdk_disabled', not runtime.SCHEDULER_ENABLED and not runtime.STRIPE_ENABLED)
        check('no_loopback_preview_shortcut', not any(rule.rule.startswith('/__preview/') for rule in app.url_map.iter_rules()))

        with closing(runtime.get_db()) as connection:
            cursor = connection.cursor()
            cursor.execute('SELECT current_database(), ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()')
            db_name, ssl = cursor.fetchone()
            check('only_generated_database_tls', db_name == state['database_name'] and ssl is True)
            check('verify_full_preserved', connection.info.dsn_parameters.get('sslmode') == 'verify-full')
            cursor.execute('SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user')
            check('restricted_database_owner', tuple(cursor.fetchone()) == (False, False, False))
            cursor.execute('SELECT id,username,password_hash FROM users WHERE username LIKE %s', ('staging_%',))
            users = {row[1].removeprefix('staging_'): {'id': row[0], 'password_hash': row[2]} for row in cursor.fetchall()}
            cursor.execute('SELECT COUNT(*) FROM merchandise')
            check('four_legacy_fixtures_no_duplicate_seed', cursor.fetchone()[0] == 4)
            cursor.execute('SELECT COUNT(*) FROM business_items')
            check('no_automatic_legacy_backfill', cursor.fetchone()[0] == (0 if args.phase == 'initialize' else 1))
            cursor.execute('SELECT COUNT(*) FROM business_work_records')
            check('no_automatic_work_creation', cursor.fetchone()[0] == (0 if args.phase == 'initialize' else 1))
            for table in ('stage3_charge_drafts', 'stage3_invoice_revisions', 'stage3_fund_records',
                          'stage5_payment_attempts', 'stage6_notifications'):
                cursor.execute('SELECT COUNT(*) FROM ' + table)
                check('no_unrequested_rows_' + table, cursor.fetchone()[0] == 0)
            photo = Path(app.config['UPLOAD_FOLDER']).resolve() / 'fictional-review.png'
            if args.phase == 'initialize':
                cursor.execute("UPDATE merchandise SET product_name='fictional-preserve-on-reboot',photo_path='uploads/fictional-review.png' WHERE user_id=%s AND custody_location='self' RETURNING id", (users['normal']['id'],))
                state['legacy_id'] = cursor.fetchone()[0]
                state['password_hash'] = users['normal']['password_hash']
                cursor.execute('SELECT expires_at FROM feature_manual_grants WHERE user_id=%s', (users['normal']['id'],))
                state['grant_expiry'] = cursor.fetchone()[0]
                connection.commit()
                sandbox.placeholder_image(photo, 0)
                state['photo_sha256'] = hashlib.sha256(photo.read_bytes()).hexdigest()
                from item_registry import Actor, Registry
                registry = Registry(connection, 'postgres')
                item = registry.create_item(Actor(users['normal']['id']), 'fictional-individual-persisted',
                    occurred_at='2026-10-22T01:00:00Z', evidence_reference='fictional-only', source_key='fictional-item')
                state['physical_id'] = item['physical_item_id']
                work = registry.record_work(Actor(users['normal']['id']), state['physical_id'], 'photography',
                    '2026-10-22T02:00:00Z', 'fictional-work', 'fictional-only', duration_seconds=60,
                    measurement_source='observed')
                check('explicit_work_does_not_create_charge', work['charge_created'] is False)
                connection.commit()
                write_report(state_path, state)
            else:
                cursor.execute('SELECT product_name FROM merchandise WHERE id=%s', (state['legacy_id'],))
                check('edited_legacy_item_survives_reboot', cursor.fetchone()[0] == 'fictional-preserve-on-reboot')
                check('password_hash_unchanged', users['normal']['password_hash'] == state['password_hash'])
                cursor.execute('SELECT expires_at FROM feature_manual_grants WHERE user_id=%s', (users['normal']['id'],))
                check('grant_expiry_not_extended', cursor.fetchone()[0] == state['grant_expiry'])
                check('photo_bytes_survive_reboot', photo.is_file() and hashlib.sha256(photo.read_bytes()).hexdigest() == state['photo_sha256'])

        base = 'https://' + HTTP_HOST
        clients = {}
        common = ('/workspace-home', '/inventory/items', '/finance', '/commerce', '/commerce/orders',
                  '/commerce/listings', '/payments', '/notifications', '/notifications/settings', '/plans')
        hubs = tuple('/my/' + key for key in ('products', 'requests', 'money', 'documents', 'support'))
        for role, user in users.items():
            client = app.test_client()
            clients[role] = client
            response = client.post('/login', data={'username': 'staging_' + role,
                'password': state['passwords'][role]}, base_url=base)
            check(role + '_password_login', response.status_code == 302)
            paths = common + (('/pilot-review', '/pilot-review/cohorts', '/pilot-review/costs',
                              '/pilot-review/scenarios', '/inventory/reconcile', '/inventory/imports',
                              '/commerce/channels') if role == 'admin' else hubs)
            for path in paths:
                response = client.get(path, base_url=base)
                expected_status = 403 if role == 'admin' and path == '/notifications/settings' else 200
                check(role + '_page_' + path, response.status_code == expected_status)
                if role != 'admin' and path in hubs:
                    check(role + '_private_hub_' + path, 'no-store' in response.headers.get('Cache-Control', ''))
            if role != 'admin':
                response = client.get('/finance/customers/' + str(user['id']), base_url=base)
                check(role + '_own_finance', response.status_code == 200)
                other = users['business' if role == 'normal' else 'normal']['id']
                check(role + '_other_finance_denied', client.get('/finance/customers/' + str(other), base_url=base).status_code in (403, 404))
                check(role + '_staff_pilot_denied', client.get('/pilot-review', base_url=base).status_code == 403)
                check(role + '_staff_reconcile_denied', client.get('/inventory/reconcile', base_url=base).status_code == 403)
        for path in ('/inventory/items/', '/finance/items/'):
            check('owner_detail_' + path, clients['normal'].get(path + state['physical_id'], base_url=base).status_code == 200)
            check('other_customer_detail_denied_' + path, clients['business'].get(path + state['physical_id'], base_url=base).status_code in (403, 404))
        client = clients['normal']
        photo_response = client.get('/static/uploads/fictional-review.png', base_url=base)
        check('owner_photo_available', photo_response.status_code == 200 and
              hashlib.sha256(photo_response.data).hexdigest() == state['photo_sha256'])
        check('owner_photo_private', 'no-store' in photo_response.headers.get('Cache-Control', ''))
        check('other_customer_photo_denied', clients['business'].get('/static/uploads/fictional-review.png', base_url=base).status_code == 404)
        for path in ('/billing/checkout', '/api/stripe/webhook', '/api/plans/store-verify'):
            check('external_route_blocked_' + path, client.post(path, base_url=base).status_code == 403)
        check('line_link_inactive', client.get('/notifications/line/link', base_url=base).status_code in (403, 404))
        check('healthz', client.get('/healthz', base_url=base).status_code == 200)
        anon = app.test_client()
        anonymous_photo = anon.get('/static/uploads/fictional-review.png', base_url=base)
        login_target = urlsplit(anonymous_photo.headers.get('Location', ''))
        check('anonymous_photo_denied', anonymous_photo.status_code in (401, 403) or
              anonymous_photo.status_code in (302, 303) and
              login_target.path == '/login' and login_target.netloc in ('', HTTP_HOST)
              and login_target.scheme in ('', 'https'))
        check('home_requires_auth', anon.get('/workspace-home', base_url=base).status_code in (302, 303, 401))
        rejected = anon.post('/api/mobile/v1/session', json={'username': 'admin', 'password': 'fictional-rejected'}, base_url=base)
        check('nonfixture_login_denied', rejected.status_code == 403 and (rejected.get_json() or {}).get('error', {}).get('code') == 'staging_test_account_required')
        token_response = anon.post('/api/mobile/v1/session', json={'username': 'staging_normal', 'password': state['passwords']['normal']}, base_url=base)
        token = (token_response.get_json() or {}).get('token', '')
        mobile = anon.get('/api/mobile/v1/items', headers={'Authorization': 'Bearer ' + token}, base_url=base)
        check('native_api_same_legacy_inventory', token_response.status_code == 200 and mobile.status_code == 200 and (mobile.get_json() or {}).get('total') == 2)
        verify_access(runtime, config, access, state, state_path, users, args.phase, check)
        verify_documents(runtime, state, state_path, users, clients, args.phase, check)
        verify_source_documents(runtime, state, state_path, users, clients, args.phase, check)
        try:
            with socket.socket() as denied:
                denied.connect(('203.0.113.1', 443))
        except OSError as error:
            check('outgoing_network_denied', str(error) == 'External communication is disabled in the initial staging phase.')
        else:
            check('outgoing_network_denied', False)
    except Exception as error:
        report['error'] = safe_error(error)
    finally:
        for connection in connections:
            connection.close()
        os.chdir(original)
        report['passed'] = sum(checks.values())
        report['failed'] = sum(not value for value in checks.values()) + int('error' in report)
        write_report(root / (args.phase + '-report.json'), report)
    return int(report['failed'] > 0)


def parent(args):
    from verify_postgres_recovery import prepare, read_context, fixture_connect, run, clean_environment
    from psycopg2 import sql

    pg_bin = args.pg_bin.resolve()
    if not all((pg_bin / name).is_file() for name in ('postgres.exe', 'initdb.exe', 'pg_ctl.exe')):
        raise ValueError('A verified portable PostgreSQL binary directory is required.')
    root = Path(tempfile.mkdtemp(prefix='kaika-staging-review-pg-')).resolve()
    result = {'scope': 'new disposable loopback PostgreSQL only', 'fixture_workspace': str(root), 'phases': [], 'cluster_stopped': False}
    context = None
    try:
        prepare(root, pg_bin)
        context = read_context(root / 'fixture-context.json')
        role, database = 'kaika_review_owner_' + secrets.token_hex(5), 'kaika_staging_review_' + secrets.token_hex(5)
        password = secrets.token_urlsafe(32)
        with closing(fixture_connect(context['admin_database_url'])) as admin:
            admin.autocommit = True
            with admin.cursor() as cursor:
                cursor.execute(sql.SQL('CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %s').format(sql.Identifier(role)), (password,))
                cursor.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(database), sql.Identifier(role)))
        parsed = urlsplit(context['admin_database_url'])
        url = urlunsplit((parsed.scheme, quote(role, safe='') + ':' + quote(password, safe='') + '@127.0.0.1:' + str(context['port']), '/' + database, parsed.query, ''))
        write_report(root / 'private-review-state.json', {'database_url': url, 'database_name': database,
            'secret': secrets.token_urlsafe(48), 'passwords': {key: secrets.token_urlsafe(32) for key in ('admin', 'normal', 'business')}})
        for name in ('initialize', 'restart'):
            with (root / (name + '-private.log')).open('wb') as log:
                completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--phase', name,
                    '--workspace', str(root)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    timeout=300, shell=False, creationflags=0x08000000 if os.name == 'nt' else 0)
            path = root / (name + '-report.json')
            report = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'phase': name, 'failed': 1, 'missing_report': True}
            result['phases'].append(report)
            if completed.returncode or report.get('failed'):
                break
    except Exception as error:
        result['error'] = safe_error(error)
    finally:
        if context is None and (root / 'fixture-context.json').is_file():
            context = read_context(root / 'fixture-context.json')
        if context:
            try:
                run([str(pg_bin / 'pg_ctl.exe'), 'stop', '--pgdata=' + context['cluster'], '--mode=fast', '--wait', '--timeout=30'], env=clean_environment())
                with socket.socket() as probe:
                    probe.settimeout(1)
                    result['cluster_stopped'] = not (root / 'cluster' / 'postmaster.pid').exists() and probe.connect_ex(('127.0.0.1', context['port'])) != 0
            except Exception as error:
                result['stop_error'] = safe_error(error)
    result['passed'] = sum(phase.get('passed', 0) for phase in result['phases'])
    result['failed'] = sum(phase.get('failed', 0) for phase in result['phases']) + int('error' in result) + int(not result['cluster_stopped'])
    if len(result['phases']) != 2:
        result['failed'] += 1
    output = args.report.resolve() if args.report else root / 'report.json'
    write_report(output, result)
    print(json.dumps({'report': str(output), 'fixture_workspace': str(root), 'passed': result['passed'],
                      'failed': result['failed'], 'cluster_stopped': result['cluster_stopped']}, ensure_ascii=False))
    return int(result['failed'] > 0)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    # Do not inspect or forward ambient database credentials to any fixture tool.
    for key in list(os.environ):
        if key.startswith('PG') or key in ('DATABASE_URL', 'SQLALCHEMY_DATABASE_URI', 'MERCHANDISE_DB_PATH'):
            del os.environ[key]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pg-bin', type=Path)
    parser.add_argument('--report', type=Path)
    parser.add_argument('--phase', choices=('initialize', 'restart'))
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args()
    if args.phase:
        if args.workspace is None:
            parser.error('--phase requires the newly owned fixture workspace')
        return phase(args)
    if args.pg_bin is None:
        parser.error('--pg-bin is required; no ambient or existing database is accepted')
    return parent(args)


if __name__ == '__main__':
    raise SystemExit(main())
