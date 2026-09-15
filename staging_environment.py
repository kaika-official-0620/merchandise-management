"""Fail-closed staging boundary; never imported by the production entrypoint.

The hosted entrypoint requires a separate PostgreSQL database. Unit tests pass
an isolated SQLite runtime directly to the seed/HTTP helpers, never to startup.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import socket
import time
from urllib.parse import parse_qsl, unquote, urlsplit

from flask import abort, flash, jsonify, redirect, request, url_for
from flask_login import current_user, logout_user
from werkzeug.security import generate_password_hash


TEST_USERS = {
    "admin": ("staging_admin", "【検証用】開花担当者", "admin"),
    "normal": ("staging_normal", "【検証用】自己保管ユーザー", "user"),
    "business": ("staging_business", "【検証用】開花依頼ユーザー", "user"),
}
DISABLED_FLAGS = {
    "AUTO_BACKUP_ENABLED": "0", "GOOGLE_DRIVE_ENABLED": "0",
    "PUSH_NOTIFICATIONS_ENABLED": "0", "FEATURE_APPLE_BILLING_ENABLED": "0",
    "FEATURE_GOOGLE_BILLING_ENABLED": "0", "FEATURE_STORE_NEW_PURCHASES_ENABLED": "0",
    "PRIMARY_DOMAIN_REDIRECT": "0",
}
CREDENTIAL_PREFIXES = ("STRIPE_", "FEATURE_STRIPE_", "LINE_", "SMTP_", "MAIL_",
                       "GOOGLE_", "FEATURE_APPLE_", "FEATURE_GOOGLE_")
CREDENTIAL_NAMES = {"EXPO_ACCESS_TOKEN", "BACKUP_CRON_TOKEN", "BATCH_API_KEY",
                    "GOOGLE_APPLICATION_CREDENTIALS"}
PRODUCTION_HOST = "stock.kaika-potential.co.jp"


class StagingConfigurationError(RuntimeError):
    """Safe messages name fields only; credentials/connection errors are omitted."""


@dataclass(frozen=True)
class StagingConfig:
    database_url: str = field(repr=False)
    database_name: str
    host: str


def validate_environment(environ=None):
    env = os.environ if environ is None else environ
    if env.get("KAIKA_RUNTIME_ENV") != "staging":
        raise StagingConfigurationError("KAIKA_RUNTIME_ENV must be staging.")
    expected = env.get("KAIKA_STAGING_DATABASE_NAME", "kaika_staging")
    if not re.fullmatch(r"kaika_staging(?:_[a-z0-9_]+)?", expected):
        raise StagingConfigurationError("KAIKA_STAGING_DATABASE_NAME must be a dedicated staging name.")
    try:
        parsed = urlsplit(env.get("DATABASE_URL", ""))
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        query = dict(query_items)
        valid = (parsed.scheme in {"postgres", "postgresql"} and parsed.hostname
                 and parsed.username and parsed.password and unquote(parsed.path[1:]) == expected
                 and not parsed.fragment and len(query) == len(query_items)
                 and not (set(query) - {"sslmode", "sslrootcert"})
                 and query.get("sslmode", "require") in {"require", "verify-ca", "verify-full"})
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise StagingConfigurationError("DATABASE_URL must point to the dedicated staging PostgreSQL database.")
    if len(env.get("SECRET_KEY", "")) < 32 or "merchandise-management-secret-key" in env.get("SECRET_KEY", ""):
        raise StagingConfigurationError("SECRET_KEY requires a unique generated value of at least 32 characters.")
    for role in TEST_USERS:
        key = "STAGING_" + role.upper() + "_PASSWORD"
        if len(env.get(key, "")) < 20:
            raise StagingConfigurationError(key + " requires a generated value of at least 20 characters.")
    for key, expected_value in DISABLED_FLAGS.items():
        if key in env and env[key] != expected_value:
            raise StagingConfigurationError(key + " must remain disabled for the initial staging phase.")
    for key, value in env.items():
        if value and key not in DISABLED_FLAGS and (key in CREDENTIAL_NAMES or key.startswith(CREDENTIAL_PREFIXES)):
            raise StagingConfigurationError(key + " must not be configured in the initial staging phase.")
    host = (env.get("RENDER_EXTERNAL_HOSTNAME") or env.get("STAGING_HOSTNAME") or "").strip().lower()
    if not host or not re.fullmatch(r"[a-z0-9][a-z0-9.-]+", host) or host == PRODUCTION_HOST:
        raise StagingConfigurationError("A separate RENDER_EXTERNAL_HOSTNAME or STAGING_HOSTNAME is required.")
    if env.get("PRIMARY_DOMAIN") and env["PRIMARY_DOMAIN"].strip().lower() != host:
        raise StagingConfigurationError("PRIMARY_DOMAIN must match the staging hostname.")
    if env.get("FEATURE_BILLING_ORIGIN") and env["FEATURE_BILLING_ORIGIN"] != "https://" + host:
        raise StagingConfigurationError("FEATURE_BILLING_ORIGIN must not reference another environment.")
    return StagingConfig(env["DATABASE_URL"], expected, host)


def prepare_environment(config):
    os.environ.update(DISABLED_FLAGS)
    os.environ.update({"PRIMARY_DOMAIN": config.host, "PRIMARY_SCHEME": "https",
                       "FEATURE_PLANS_ENABLED": "1", "MOBILE_API_ENABLED": "1",
                       "FEATURE_STORE_ENVIRONMENT": "test"})


def install_outbound_guard():
    """Deny Python network clients, including requests/urllib/SMTP/Google/Expo.

    PostgreSQL uses psycopg2/libpq's separately validated native connection.
    Incoming Gunicorn sockets use bind/listen/accept and are unaffected.
    Returns a restoration callback exclusively for isolated tests.
    """
    originals = {name: getattr(socket.socket, name) for name in ("connect", "connect_ex", "sendto", "sendmsg") if hasattr(socket.socket, name)}
    original_create = socket.create_connection

    def blocked(*args, **kwargs):
        raise OSError("External communication is disabled in the initial staging phase.")

    for name in originals:
        setattr(socket.socket, name, blocked)
    socket.create_connection = blocked

    def restore():
        for name, method in originals.items():
            setattr(socket.socket, name, method)
        socket.create_connection = original_create
    return restore


def verify_database_marker(cursor, postgres=True):
    """Reject populated databases without our marker and quarantined restores."""
    if postgres:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    else:
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    tables = {row[0] if not hasattr(row, "keys") else next(iter(dict(row).values())) for row in cursor.fetchall()}
    if "kaika_recovery_quarantine" in tables:
        raise StagingConfigurationError("A quarantined recovery database cannot run the staging application.")
    if tables and "kaika_environment" not in tables:
        raise StagingConfigurationError("Staging refuses an existing database without its environment marker.")
    if "kaika_environment" in tables:
        cursor.execute("SELECT name FROM kaika_environment")
        values = [row[0] if not hasattr(row, "keys") else next(iter(dict(row).values())) for row in cursor.fetchall()]
        if values != ["staging"]:
            raise StagingConfigurationError("The database environment marker is not staging.")
    else:
        cursor.execute("CREATE TABLE kaika_environment (name TEXT PRIMARY KEY)")
        cursor.execute("INSERT INTO kaika_environment (name) VALUES ('staging')")


@contextmanager
def staging_bootstrap(config):
    """Hold an advisory lock across source initialization and idempotent seed."""
    import psycopg2
    try:
        query = dict(parse_qsl(urlsplit(config.database_url).query))
        defaults = {} if "sslmode" in query else {"sslmode": "require"}
        connection = psycopg2.connect(config.database_url, connect_timeout=15, **defaults)
    except Exception:
        raise StagingConfigurationError("Cannot connect to the dedicated staging database.") from None
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT pg_advisory_lock(574920269)")
        verify_database_marker(cursor)
        connection.commit()
        yield connection
    finally:
        connection.close()  # Also releases the session advisory lock on failure.


def seed_test_data(runtime, passwords):
    """Add three fictional users and four items once; never reset user work."""
    for role in TEST_USERS:
        if len(passwords.get(role, "")) < 20:
            raise StagingConfigurationError("A strong initial password is required for every test account.")
    pg = bool(runtime.DATABASE_URL)
    mark = "%s" if pg else "?"
    conn = runtime.get_db()
    cur = conn.cursor()
    try:
        if not pg:
            cur.execute("BEGIN IMMEDIATE")
        verify_database_marker(cur, pg)
        cur.execute("""CREATE TABLE IF NOT EXISTS kaika_staging_seeds (
            seed_key TEXT PRIMARY KEY, record_id INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
        plans = runtime.app.extensions["kaika_feature_plans"]
        plans.schema(cur)
        self_inventory = runtime.app.extensions["kaika_self_inventory"]
        self_inventory._ensure_schema(cur)
        ids = {}
        created = 0
        for role, (username, name, user_role) in TEST_USERS.items():
            key = "user:" + role
            cur.execute(f"SELECT record_id FROM kaika_staging_seeds WHERE seed_key={mark}", (key,))
            seeded = cur.fetchone()
            if seeded:
                user_id = seeded[0]
                cur.execute(f"SELECT username FROM users WHERE id={mark}", (user_id,))
                existing = cur.fetchone()
                if not existing or existing[0] != username:
                    raise StagingConfigurationError("An existing staging test account was renamed or removed; automatic recreation is refused.")
            else:
                cur.execute(f"SELECT id FROM users WHERE username={mark} OR email={mark}", (username, username + "@example.invalid"))
                if cur.fetchone():
                    raise StagingConfigurationError("A test username already exists without a seed record.")
                values = (username, username + "@example.invalid", generate_password_hash(passwords[role]), user_role, name)
                cur.execute("INSERT INTO users (username,email,password_hash,role,display_name) VALUES (" + ",".join([mark] * 5) + ")" + (" RETURNING id" if pg else ""), values)
                user_id = cur.fetchone()[0] if pg else cur.lastrowid
                cur.execute(f"INSERT INTO kaika_staging_seeds (seed_key,record_id) VALUES ({mark},{mark})", (key, user_id))
                if role != "admin":
                    plans.execute(cur, "INSERT INTO feature_manual_grants (user_id,plan_code,expires_at) VALUES (?,?,?)", (user_id, role, int(time.time()) + 180 * 86400))
                created += 1
            ids[role] = user_id

        # The legacy initializer creates this account. Invalidate it exactly
        # once; the HTTP boundary additionally rejects all non-test usernames.
        cur.execute("SELECT record_id FROM kaika_staging_seeds WHERE seed_key='legacy-admin-disabled'")
        if not cur.fetchone():
            cur.execute(f"UPDATE users SET password_hash={mark} WHERE username='admin'", (generate_password_hash(secrets.token_urlsafe(48)),))
            cur.execute("INSERT INTO kaika_staging_seeds (seed_key,record_id) VALUES ('legacy-admin-disabled',0)")

        for role in ("normal", "business"):
            for custody in ("self", "kaika"):
                key = "item:" + role + ":" + custody
                cur.execute(f"SELECT record_id FROM kaika_staging_seeds WHERE seed_key={mark}", (key,))
                if cur.fetchone():
                    continue
                today = runtime.get_jst_now().date().isoformat()
                values = (ids[role], "【検証用】" + ("自宅のバッグ" if custody == "self" else "開花に預けた時計"),
                          "user", custody, today, today if custody == "kaika" else None,
                          3000, 5000, "架空の商品です。実在のお客様・商品・金額を入力しないでください。")
                cur.execute("INSERT INTO merchandise (user_id,product_name,scope,custody_location,purchase_date,storage_start_date,purchase_price,listing_price,notes) VALUES (" + ",".join([mark] * 9) + ")" + (" RETURNING id" if pg else ""), values)
                item_id = cur.fetchone()[0] if pg else cur.lastrowid
                if custody == "self":
                    digest = hashlib.sha256(key.encode()).hexdigest()
                    cur.execute(f"INSERT INTO self_inventory_items (merchandise_id,user_id,submission_hash) VALUES ({mark},{mark},{mark})", (item_id, ids[role], digest))
                cur.execute(f"INSERT INTO kaika_staging_seeds (seed_key,record_id) VALUES ({mark},{mark})", (key, item_id))
        conn.commit()
        return {"created_users": created, "test_accounts": list(TEST_USERS)}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def register_staging_boundary(runtime):
    app = runtime.app
    if app.extensions.get("kaika_staging"):
        return
    app.extensions["kaika_staging"] = True
    app.config.update(SESSION_COOKIE_NAME="kaika_staging_session", REMEMBER_COOKIE_NAME="kaika_staging_remember",
                      SESSION_COOKIE_SECURE=True, REMEMBER_COOKIE_SECURE=True,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
    blocked_prefixes = ("/admin/line", "/line/", "/admin/stripe", "/stripe/", "/api/stripe/",
                        "/api/google-drive/", "/billing/", "/internal/", "/api/plans/store-intent",
                        "/api/plans/store-verify")
    test_usernames = {value[0] for value in TEST_USERS.values()}

    @app.context_processor
    def staging_login_context():
        return {"kaika_staging_test_accounts": tuple(TEST_USERS.values())}

    def test_account_required():
        if request.path.startswith("/api/"):
            return jsonify(error={"code": "staging_test_account_required", "message": "指定された検証用アカウントでログインしてください。"}), 403
        flash("ここは開花の検証サイトです。通常サイトのアカウントは使えません。検証用アカウントを選び、専用のパスワードでログインしてください。", "error")
        return redirect(url_for("login"), code=303)

    def boundary():
        path = request.path
        if path.startswith(blocked_prefixes) or path in {"/admin/backup/import", "/backup/import_user"}:
            return jsonify(error={"code": "staging_disabled", "message": "検証環境では外部連携・実課金・旧データの取込みを停止しています。"}), 403
        if path == "/register":
            return redirect(url_for("login"))
        if (path not in {"/healthz", "/robots.txt"}
                and not (path.startswith("/static/") and not path.startswith("/static/uploads/"))
                and current_user.is_authenticated and current_user.username not in test_usernames):
            logout_user()
            return test_account_required()
        if path == "/login" or path == "/api/mobile/v1/session":
            next_page = request.args.get("next", "")
            if next_page and (not next_page.startswith("/") or next_page.startswith("//") or "\\" in next_page):
                abort(400, description="検証環境内の画面を指定してください。")
            if request.method == "POST":
                if path == "/api/mobile/v1/session":
                    # The real API parses a bounded request.stream itself.
                    # Peek only its maximum payload then restore that stream;
                    # get_json() here would consume it and break native login.
                    if not request.is_json:
                        abort(400)
                    raw = request.stream.read(8193)
                    if len(raw) > 8192:
                        abort(413)
                    try:
                        data = json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        abort(400)
                    request.stream = io.BytesIO(raw)
                else:
                    data = request.form
                if not hasattr(data, "get"):
                    abort(400, description="ログイン情報の形式を確認してください。")
                username = data.get("username")
                if not isinstance(username, str) or username not in test_usernames:
                    return test_account_required()
            return None
        if path == "/healthz" or path == "/robots.txt":
            return None
        if path.startswith("/static/") and not path.startswith("/static/uploads/"):
            return None
        # The native API has its own bearer-token ownership checks. Its only
        # session-creation route above accepts exclusively the seeded testers.
        if path.startswith("/api/mobile/v1/"):
            return None
        if not current_user.is_authenticated:
            if path.startswith("/api/"):
                return jsonify(error={"code": "authentication_required", "message": "ログインしてください。"}), 401
            return redirect(url_for("login", next=request.full_path.rstrip("?")))
        return None

    # Run before existing hooks that query user data or write workflow state.
    app.before_request_funcs.setdefault(None, []).insert(0, boundary)

    @app.after_request
    def staging_headers(response):
        location = response.headers.get("Location", "")
        if location:
            target = urlsplit(location)
            if target.netloc and target.netloc != request.host:
                response = jsonify(error={"code": "staging_external_redirect_disabled", "message": "検証環境の外への移動は停止しています。"})
                response.status_code = 403
        response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        response.headers["X-Kaika-Environment"] = "staging"
        response.headers["Cache-Control"] = "no-store"
        if response.status_code == 200 and response.mimetype == "text/html" and not response.is_streamed:
            body = response.get_data(as_text=True)
            banner = ('<aside role="status" data-kaika-staging="true" style="position:relative;z-index:10000;'
                      'padding:9px 12px;background:#fff3cd;color:#593f00;text-align:center;font:14px/1.5 sans-serif;">'
                      '開花の検証環境 · 架空データ専用 · 実課金・外部通知は停止中</aside>')
            body = re.sub(r"(<body\b[^>]*>)", lambda match: match.group(1) + banner, body, count=1, flags=re.I)
            response.set_data(body)
        return response

    if "staging_robots" not in app.view_functions:
        app.add_url_rule("/robots.txt", "staging_robots", lambda: ("User-agent: *\nDisallow: /\n", 200, {"Content-Type": "text/plain"}))


def check_upload_disk(runtime_or_path, *, require_mount=True):
    configured = runtime_or_path.app.config["UPLOAD_FOLDER"] if hasattr(runtime_or_path, "app") else runtime_or_path
    path = Path(configured).resolve()
    if not path.is_dir():
        raise StagingConfigurationError("The dedicated staging uploads directory is unavailable.")
    if require_mount and not os.path.ismount(path):
        raise StagingConfigurationError("The dedicated staging uploads disk is not mounted.")
    marker = path / ".kaika-staging-volume"
    if marker.exists():
        if marker.read_text(encoding="utf-8").strip() != "staging":
            raise StagingConfigurationError("The uploads disk environment marker is invalid.")
    else:
        # Source may create its normal empty runtime folders at startup. Refuse
        # any actual existing files when identifying the disk for the first time.
        if any(entry.is_file() for entry in path.rglob("*")):
            raise StagingConfigurationError("Staging refuses an unmarked uploads disk containing files.")
        marker.write_text("staging\n", encoding="utf-8")
