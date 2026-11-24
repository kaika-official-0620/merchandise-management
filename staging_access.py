"""Opt-in, limited-use access to the fictional staging business account only.

No issuance on import/startup; raw tokens are returned only to the explicit
issuer. Browser links carry the token in a fragment, never a query/path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import tempfile
import time

from flask import Blueprint, jsonify, make_response, render_template_string, request, session
from flask_login import login_user

from staging_environment import StagingConfigurationError, validate_environment

HOST = 'kaika-platform-staging.onrender.com'
USERNAME = 'staging_business'
DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 24 * 60 * 60
MAX_LINK_USES = 5
ATTEMPT_WINDOW_SECONDS = 900
TTL_CONSTRAINT = 'kaika_staging_access_tokens_lifetime_check'
LEGACY_TTL_CONSTRAINT = 'kaika_staging_access_tokens_check'
TTL_EXPRESSION = f'expires_at > created_at AND expires_at - created_at <= {MAX_TTL_SECONDS}'
MAX_ATTEMPTS = 8
MESSAGE = 'このリンクではログインできません。新しい確認用リンクをご利用ください。'
TOKEN_PATTERN = re.compile(r'[A-Za-z0-9_-]{43}\Z')


def _now():
    return int(time.time())


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _validate(config, environ=None):
    env = os.environ if environ is None else environ
    if (validate_environment(env) != config or config.host != HOST
            or env.get('KAIKA_STAGING_ACCESS_ENABLED') != '1'):
        raise StagingConfigurationError('Staging access is not enabled for the dedicated service.')


def _sqlite(connection):
    if not isinstance(connection, sqlite3.Connection):
        if getattr(connection, 'autocommit', False):
            raise StagingConfigurationError('Staging access requires transactional connections.')
        return False
    # SQLite is an isolated test adapter, never a hosted runtime fallback.
    for _, _, filename in connection.execute('PRAGMA database_list').fetchall():
        if not filename:
            continue
        path = Path(filename).resolve()
        if (Path(tempfile.gettempdir()).resolve() not in path.parents
                or not any(part.startswith('kaika-staging-access-test-') for part in path.parts)):
            raise StagingConfigurationError('Staging access SQLite is limited to isolated test fixtures.')
    return True


def _execute(cursor, sql, args=(), *, sqlite=False):
    cursor.execute(sql.replace('%s', '?') if sqlite else sql, args)


def _dict(cursor, row):
    return dict(row) if hasattr(row, 'keys') else dict(zip((c[0] for c in cursor.description), row))


def _scope(connection, config):
    sqlite = _sqlite(connection)
    if sqlite and not connection.in_transaction:
        connection.execute('BEGIN IMMEDIATE')
    cur = connection.cursor()
    try:
        if not sqlite:
            cur.execute('SELECT current_database() AS name')
            if _dict(cur, cur.fetchone())['name'] != config.database_name:
                raise StagingConfigurationError('Staging access database identity mismatch.')
            cur.execute('SET LOCAL search_path=public,pg_catalog')
        cur.execute('SELECT name FROM kaika_environment')
        if [_dict(cur, row)['name'] for row in cur.fetchall()] != ['staging']:
            raise StagingConfigurationError('Staging access requires the existing staging marker.')
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'" if sqlite else
                    "SELECT tablename AS name FROM pg_tables WHERE schemaname='public'")
        tables = {_dict(cur, row)['name'] for row in cur.fetchall()}
        if 'kaika_recovery_quarantine' in tables:
            raise StagingConfigurationError('Staging access refuses a quarantined database.')
        return cur, sqlite, tables
    except Exception:
        cur.close()
        raise


def _eligible(cur, sqlite, tables, *, lock=True):
    sql = """SELECT u.* FROM users u JOIN kaika_staging_seeds s ON s.record_id=u.id
        WHERE s.seed_key='user:business' AND u.username=%s AND u.role='user'"""
    if lock and not sqlite:
        sql += ' FOR UPDATE OF u,s'
    _execute(cur, sql, (USERNAME,), sqlite=sqlite)
    rows = cur.fetchall()
    if len(rows) != 1:
        return None
    user = _dict(cur, rows[0])
    if not isinstance(user.get('password_hash'), str) or not user['password_hash']:
        return None
    if 'account_deletion_requests' in tables:
        _execute(cur, 'SELECT state FROM account_deletion_requests WHERE customer_id=%s',
                 (user['id'],), sqlite=sqlite)
        row = cur.fetchone()
        if row and _dict(cur, row)['state'] in ('access_closed', 'erasure_pending', 'completed'):
            return None
    return user


def _migrate_token_lifetime(cur):
    """Replace the original PostgreSQL limit without changing any token rows."""
    cur.execute("""SELECT conname, pg_get_expr(conbin, conrelid) AS expression
        FROM pg_constraint WHERE conrelid='public.kaika_staging_access_tokens'::regclass
        AND contype='c' AND conname IN (%s,%s)""", (TTL_CONSTRAINT, LEGACY_TTL_CONSTRAINT))
    checks = {}
    for row in cur.fetchall():
        constraint = _dict(cur, row)
        checks[constraint['conname']] = re.sub(r'[\s()]', '', constraint['expression']).lower()
    current = re.sub(r'\s', '', TTL_EXPRESSION).lower()
    legacy = 'expires_at>created_atandexpires_at-created_at<=900'
    if checks.get(TTL_CONSTRAINT) == current and checks.get(LEGACY_TTL_CONSTRAINT) != legacy:
        return
    if TTL_CONSTRAINT in checks or checks.get(LEGACY_TTL_CONSTRAINT) != legacy:
        raise StagingConfigurationError('Unexpected staging access lifetime constraint.')
    cur.execute(f'''ALTER TABLE kaika_staging_access_tokens
        ADD CONSTRAINT {TTL_CONSTRAINT} CHECK({TTL_EXPRESSION}),
        DROP CONSTRAINT {LEGACY_TTL_CONSTRAINT}''')


def _schema(cur, sqlite):
    token_ddl = f'''CREATE TABLE IF NOT EXISTS kaika_staging_access_tokens (
        token_hash TEXT PRIMARY KEY CHECK(length(token_hash)=64),
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        password_fingerprint TEXT NOT NULL CHECK(length(password_fingerprint)=64),
        created_at BIGINT NOT NULL, expires_at BIGINT NOT NULL, consumed_at BIGINT,
        CONSTRAINT {TTL_CONSTRAINT} CHECK({TTL_EXPRESSION}),
        CONSTRAINT kaika_staging_access_consumption_check
        CHECK(consumed_at IS NULL OR (consumed_at>=created_at AND consumed_at<expires_at)))'''
    attempt_ddl = '''CREATE TABLE IF NOT EXISTS kaika_staging_access_attempts (
        session_hash TEXT PRIMARY KEY CHECK(length(session_hash)=64),
        window_started BIGINT NOT NULL, attempt_count INTEGER NOT NULL CHECK(attempt_count>=1))'''
    quota_ddl = f'''CREATE TABLE IF NOT EXISTS kaika_staging_access_quotas (
        token_hash TEXT PRIMARY KEY REFERENCES kaika_staging_access_tokens(token_hash) ON DELETE RESTRICT,
        max_uses INTEGER NOT NULL CHECK(max_uses BETWEEN 2 AND {MAX_LINK_USES}),
        use_count INTEGER NOT NULL DEFAULT 0 CHECK(use_count BETWEEN 0 AND max_uses))'''
    cur.execute(token_ddl)
    cur.execute(attempt_ddl)
    cur.execute(quota_ddl)
    if sqlite:
        cur.execute('''CREATE TRIGGER IF NOT EXISTS kaika_staging_access_no_delete
            BEFORE DELETE ON kaika_staging_access_tokens BEGIN SELECT RAISE(ABORT,'retain staging access evidence'); END''')
        cur.execute('''CREATE TRIGGER IF NOT EXISTS kaika_staging_access_immutable
            BEFORE UPDATE ON kaika_staging_access_tokens WHEN
            NEW.token_hash IS NOT OLD.token_hash OR NEW.user_id IS NOT OLD.user_id OR
            NEW.password_fingerprint IS NOT OLD.password_fingerprint OR NEW.created_at IS NOT OLD.created_at OR
            NEW.expires_at IS NOT OLD.expires_at OR OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL
            BEGIN SELECT RAISE(ABORT,'staging access is single use'); END''')
        cur.execute('''CREATE TRIGGER IF NOT EXISTS kaika_staging_access_quota_no_delete
            BEFORE DELETE ON kaika_staging_access_quotas BEGIN SELECT RAISE(ABORT,'retain staging access quota'); END''')
        cur.execute('''CREATE TRIGGER IF NOT EXISTS kaika_staging_access_quota_increment
            BEFORE UPDATE ON kaika_staging_access_quotas WHEN NEW.token_hash IS NOT OLD.token_hash
            OR NEW.max_uses IS NOT OLD.max_uses OR NEW.use_count != OLD.use_count + 1
            BEGIN SELECT RAISE(ABORT,'staging access quota only permits single increments'); END''')
    else:
        from staging_review import _check_columns, _column_names, _function, _trigger
        _check_columns(cur, 'kaika_staging_access_tokens', _column_names(token_ddl))
        _check_columns(cur, 'kaika_staging_access_attempts', _column_names(attempt_ddl))
        _check_columns(cur, 'kaika_staging_access_quotas', _column_names(quota_ddl))
        _migrate_token_lifetime(cur)
        function = '''CREATE OR REPLACE FUNCTION kaika_staging_access_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP='DELETE' THEN RAISE EXCEPTION 'retain staging access evidence'; END IF;
            IF ROW(NEW.token_hash,NEW.user_id,NEW.password_fingerprint,NEW.created_at,NEW.expires_at)
                IS DISTINCT FROM ROW(OLD.token_hash,OLD.user_id,OLD.password_fingerprint,OLD.created_at,OLD.expires_at)
                OR OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL
            THEN RAISE EXCEPTION 'staging access is single use'; END IF;
            RETURN NEW; END; $$'''
        _function(cur, function, 'kaika_staging_access_guard')
        _trigger(cur, 'CREATE TRIGGER kaika_staging_access_immutable BEFORE UPDATE OR DELETE ON '
                 'kaika_staging_access_tokens FOR EACH ROW EXECUTE FUNCTION kaika_staging_access_guard()',
                 'kaika_staging_access_immutable')
        quota_function = '''CREATE OR REPLACE FUNCTION kaika_staging_access_quota_guard()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
            IF TG_OP='DELETE' THEN RAISE EXCEPTION 'retain staging access quota'; END IF;
            IF ROW(NEW.token_hash,NEW.max_uses) IS DISTINCT FROM ROW(OLD.token_hash,OLD.max_uses)
                OR NEW.use_count != OLD.use_count + 1
            THEN RAISE EXCEPTION 'staging access quota only permits single increments'; END IF;
            RETURN NEW; END; $$'''
        _function(cur, quota_function, 'kaika_staging_access_quota_guard')
        _trigger(cur, 'CREATE TRIGGER kaika_staging_access_quota_increment BEFORE UPDATE OR DELETE ON '
                 'kaika_staging_access_quotas FOR EACH ROW EXECUTE FUNCTION kaika_staging_access_quota_guard()',
                 'kaika_staging_access_quota_increment')


def mint_staging_access(config, *, connection, environ=None, ttl_seconds=DEFAULT_TTL_SECONDS, max_uses=1):
    """Return one raw secret to an explicit issuer; fixed seeded account only."""
    _validate(config, environ)
    if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise StagingConfigurationError(f'Staging access lifetime must be 1 to {MAX_TTL_SECONDS} seconds.')
    if type(max_uses) is not int or not 1 <= max_uses <= MAX_LINK_USES:
        raise StagingConfigurationError(f'Staging access use limit must be 1 to {MAX_LINK_USES}.')
    cur = None
    try:
        cur, sqlite, tables = _scope(connection, config)
        user = _eligible(cur, sqlite, tables)
        if not user:
            raise StagingConfigurationError('The dedicated staging business account is unavailable.')
        token, now = secrets.token_urlsafe(32), _now()
        token_hash = _digest(token)
        _execute(cur, '''INSERT INTO kaika_staging_access_tokens
            (token_hash,user_id,password_fingerprint,created_at,expires_at) VALUES (%s,%s,%s,%s,%s)''',
            (token_hash, user['id'], _digest(user['password_hash']), now, now + ttl_seconds), sqlite=sqlite)
        if max_uses > 1:
            _execute(cur, 'INSERT INTO kaika_staging_access_quotas (token_hash,max_uses) VALUES (%s,%s)',
                     (token_hash, max_uses), sqlite=sqlite)
        connection.commit()
        return token
    except StagingConfigurationError:
        connection.rollback()
        raise
    except Exception:
        connection.rollback()
        raise StagingConfigurationError('Staging access issuance failed.') from None
    finally:
        if cur is not None:
            cur.close()


def claim_staging_access(config, *, connection, token, environ=None):
    """Spend one permitted use and return the fixed user ID; owns transaction."""
    _validate(config, environ)
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        return None
    cur = None
    try:
        cur, sqlite, tables = _scope(connection, config)
        token_hash = _digest(token)
        sql = 'SELECT * FROM kaika_staging_access_tokens WHERE token_hash=%s'
        _execute(cur, sql + ('' if sqlite else ' FOR UPDATE'), (token_hash,), sqlite=sqlite)
        row = cur.fetchone()
        record = _dict(cur, row) if row else None
        user = _eligible(cur, sqlite, tables) if record else None
        now = _now()
        if (not record or not user or record['user_id'] != user['id']
                or not record['created_at'] <= now < record['expires_at']
                or not hmac.compare_digest(record['password_fingerprint'], _digest(user['password_hash']))):
            connection.rollback()
            return None
        first_use = record['consumed_at'] is None
        _execute(cur, 'SELECT use_count FROM kaika_staging_access_quotas WHERE token_hash=%s',
                 (token_hash,), sqlite=sqlite)
        quota = cur.fetchone()
        if quota is None and not first_use:
            connection.rollback()
            return None
        if quota is not None:
            # A pre-upgrade worker could have spent the link's original one use.
            # Treat an inconsistent first-use marker as spent.
            if first_use != (_dict(cur, quota)['use_count'] == 0):
                connection.rollback()
                return None
            _execute(cur, '''UPDATE kaika_staging_access_quotas SET use_count=use_count+1
                WHERE token_hash=%s AND use_count<max_uses''', (token_hash,), sqlite=sqlite)
            if cur.rowcount != 1:
                connection.rollback()
                return None
        if first_use:
            _execute(cur, '''UPDATE kaika_staging_access_tokens SET consumed_at=%s
                WHERE token_hash=%s AND consumed_at IS NULL''', (now, token_hash), sqlite=sqlite)
            if cur.rowcount != 1:
                connection.rollback()
                return None
        connection.commit()
        return int(user['id'])
    except Exception:
        connection.rollback()
        raise StagingConfigurationError('Staging access verification failed.') from None
    finally:
        if cur is not None:
            cur.close()


def _attempt(config, connection, browser):
    cur = None
    try:
        cur, sqlite, _ = _scope(connection, config)
        now = _now()
        _execute(cur, '''INSERT INTO kaika_staging_access_attempts(session_hash,window_started,attempt_count)
            VALUES (%s,%s,1) ON CONFLICT(session_hash) DO UPDATE SET
            attempt_count=CASE WHEN kaika_staging_access_attempts.window_started<=%s THEN 1
                ELSE kaika_staging_access_attempts.attempt_count+1 END,
            window_started=CASE WHEN kaika_staging_access_attempts.window_started<=%s THEN %s
                ELSE kaika_staging_access_attempts.window_started END RETURNING attempt_count''',
            (_digest(browser), now, now-ATTEMPT_WINDOW_SECONDS, now-ATTEMPT_WINDOW_SECONDS, now), sqlite=sqlite)
        allowed = _dict(cur, cur.fetchone())['attempt_count'] <= MAX_ATTEMPTS
        connection.commit()
        return allowed
    except Exception:
        connection.rollback()
        raise StagingConfigurationError('Staging access verification failed.') from None
    finally:
        if cur is not None:
            cur.close()


PAGE = '''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>開花の確認サイト</title>
<style nonce="{{ nonce }}">body{font-family:system-ui,sans-serif;margin:0;background:#f3f6fc;color:#17243a}main{max-width:440px;margin:12vh auto;padding:28px;background:white;border-radius:16px}h1{font-size:24px}p{line-height:1.8}button{width:100%;padding:16px;background:#3359bf;border:0;border-radius:8px;color:white;font-size:18px}button:disabled{opacity:.55}a{color:#3359bf}</style></head>
<body><main><h1>開花の確認サイト</h1><p>架空データ専用の「開花依頼ユーザー」で開きます。実際の課金・通知は行いません。</p><p>リンクは案内された期限と回数の範囲で使えます。</p>
<form id="access-form"><input type="hidden" id="csrf-token" name="csrf_token" value="{{ csrf }}"><button id="open-button" type="submit">確認サイトを開く</button></form>
<p id="status" role="status"></p><p><a href="/login">通常のログイン画面へ</a></p><noscript>JavaScriptを有効にするか、通常のログイン画面をご利用ください。</noscript></main>
<script nonce="{{ nonce }}">(()=>{'use strict';let token=new URLSearchParams(location.hash.slice(1)).get('token')||'';history.replaceState(null,'',location.pathname);const button=document.getElementById('open-button'),status=document.getElementById('status');
if(!/^[A-Za-z0-9_-]{43}$/.test(token)){button.disabled=true;status.textContent='確認用リンクを開き直してください。';}
document.getElementById('access-form').addEventListener('submit',async event=>{event.preventDefault();if(button.disabled)return;button.disabled=true;status.textContent='確認しています…';try{const response=await fetch('/staging/access/consume',{method:'POST',credentials:'same-origin',redirect:'error',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token,csrf_token:document.getElementById('csrf-token').value})});token='';const result=await response.json();if(response.ok&&result.ok===true){location.replace('/workspace-home');return;}}catch(_){token='';}status.textContent='このリンクではログインできません。新しい確認用リンクをご利用ください。';});})();</script></body></html>'''


def register_staging_access(runtime, config, *, connection):
    """Additive startup under the verified staging bootstrap connection only."""
    app = runtime.app
    value = os.environ.get('KAIKA_STAGING_ACCESS_ENABLED', '0')
    if value == '0':
        return None
    _validate(config)
    if (app.extensions.get('kaika_staging') is not True or runtime.DATABASE_URL != config.database_url):
        raise StagingConfigurationError('Staging access requires the verified staging runtime.')
    if app.extensions.get('kaika_staging_access'):
        return app.extensions['kaika_staging_access']
    cur = None
    try:
        cur, sqlite, tables = _scope(connection, config)
        if not _eligible(cur, sqlite, tables):
            raise StagingConfigurationError('The dedicated staging business account is unavailable.')
        _schema(cur, sqlite)
        connection.commit()
    except Exception:
        connection.rollback()
        raise StagingConfigurationError('Staging access preparation failed.') from None
    finally:
        if cur is not None:
            cur.close()
    bp = Blueprint('staging_access', __name__)

    def error(status=400):
        return jsonify(ok=False, message=MESSAGE), status

    @bp.before_request
    def scope_guard():
        try:
            _validate(config)
        except Exception:
            return error(404)
        if request.scheme != 'https' or request.host.lower() != HOST:
            return error(403)

    @bp.get('/staging/access')
    def landing():
        if request.query_string:
            return error()
        session['_staging_access_csrf'] = secrets.token_urlsafe(32)
        if not session.get('_staging_access_browser'):
            session['_staging_access_browser'] = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(24)
        response = make_response(render_template_string(PAGE, nonce=nonce, csrf=session['_staging_access_csrf']))
        response.headers['Content-Security-Policy'] = ("default-src 'none'; script-src 'nonce-" + nonce +
            "'; style-src 'nonce-" + nonce + "'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        return response

    @bp.post('/staging/access/consume')
    def consume():
        if request.headers.get('Origin') != 'https://' + HOST or not request.is_json:
            return error(403)
        raw = request.stream.read(2049)
        if len(raw) > 2048:
            return error()
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return error()
        expected = session.get('_staging_access_csrf')
        browser = session.get('_staging_access_browser')
        supplied = body.get('csrf_token') if isinstance(body, dict) else None
        if (not isinstance(supplied, str) or not TOKEN_PATTERN.fullmatch(supplied)
                or not isinstance(expected, str) or not TOKEN_PATTERN.fullmatch(expected)
                or not isinstance(browser, str) or not TOKEN_PATTERN.fullmatch(browser)
                or not hmac.compare_digest(expected, supplied)):
            return error(403)
        db = None
        try:
            db = runtime.get_db()
            if not _attempt(config, db, browser):
                return error(429)
            user_id = claim_staging_access(config, connection=db, token=body.get('token'))
            if user_id is None:
                return error()
            cur, sqlite, tables = _scope(db, config)
            try:
                record = _eligible(cur, sqlite, tables)
                user = runtime.build_user_from_record(record) if record and record['id'] == user_id else None
                db.rollback()
            finally:
                cur.close()
            if not user or user.role != 'user' or user.username != USERNAME:
                return error()
            session.clear()
            session['_remember'] = 'clear'  # Also expire any previous admin/other-account remember cookie.
            if not login_user(user, remember=False, fresh=True):
                session.clear()
                session['_remember'] = 'clear'
                return error()
            session['_staging_deletion_reauth'] = {'user_id': str(user.get_id()), 'verified_at': _now()}
            return jsonify(ok=True, next='/workspace-home')
        except Exception:
            return error()
        finally:
            if db is not None:
                db.close()

    @bp.after_request
    def private(response):
        response.headers.update({'Cache-Control': 'no-store, private', 'Pragma': 'no-cache',
            'Referrer-Policy': 'no-referrer', 'X-Robots-Tag': 'noindex, nofollow, noarchive',
            'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'})
        response.headers.setdefault('Content-Security-Policy', "default-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        return response

    app.register_blueprint(bp)
    app.extensions['kaika_staging_access'] = {'enabled': True}
    return app.extensions['kaika_staging_access']
