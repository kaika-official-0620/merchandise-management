"""Real Flask staging-review boot/reboot against a newly owned local PostgreSQL.

Accepts a portable binary directory, never a connection URL or environment DSN.
The parent creates and stops its own TLS cluster; isolated child processes run
the source-only app mirror. Passwords and raw app output stay in that temporary
fixture. This does not exercise Render mounts, delivery providers or devices.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import traceback
from urllib.parse import quote, urlsplit, urlunsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def safe_error(error):
    return {'type': type(error).__name__, 'frames': [
        {'file': Path(frame.filename).name, 'line': frame.lineno}
        for frame in traceback.extract_tb(error.__traceback__)]}


def write_report(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


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
        'STAGING_HOSTNAME': 'kaika-fixture.example.invalid',
        'PRIMARY_DOMAIN': 'kaika-fixture.example.invalid', 'SECRET_KEY': state['secret'],
        'KAIKA_STAGING_REVIEW_ENABLED': '1',
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

        base = 'https://kaika-fixture.example.invalid'
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
              login_target.path == '/login' and login_target.netloc in ('', 'kaika-fixture.example.invalid')
              and login_target.scheme in ('', 'https'))
        check('home_requires_auth', anon.get('/workspace-home', base_url=base).status_code in (302, 303, 401))
        rejected = anon.post('/api/mobile/v1/session', json={'username': 'admin', 'password': 'fictional-rejected'}, base_url=base)
        check('nonfixture_login_denied', rejected.status_code == 403 and (rejected.get_json() or {}).get('error', {}).get('code') == 'staging_test_account_required')
        token_response = anon.post('/api/mobile/v1/session', json={'username': 'staging_normal', 'password': state['passwords']['normal']}, base_url=base)
        token = (token_response.get_json() or {}).get('token', '')
        mobile = anon.get('/api/mobile/v1/items', headers={'Authorization': 'Bearer ' + token}, base_url=base)
        check('native_api_same_legacy_inventory', token_response.status_code == 200 and mobile.status_code == 200 and (mobile.get_json() or {}).get('total') == 2)
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
                    timeout=180, shell=False, creationflags=0x08000000 if os.name == 'nt' else 0)
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
