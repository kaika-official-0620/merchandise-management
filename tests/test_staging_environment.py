"""Staging preflight/seed/access checks against temporary, fictional data only."""
import ast
from datetime import datetime
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask, jsonify, redirect, request
from flask_login import LoginManager, UserMixin, login_user
from werkzeug.security import check_password_hash, generate_password_hash

from feature_plans import FeaturePlans
from self_inventory import SelfInventory
from staging_environment import (DISABLED_FLAGS, StagingConfigurationError,
    check_upload_disk, install_outbound_guard, register_staging_boundary,
    seed_test_data, validate_environment, verify_database_marker)


ROOT = Path(__file__).resolve().parents[1]


class StagingConfigurationTests(unittest.TestCase):
    def environment(self):
        return {"KAIKA_RUNTIME_ENV": "staging",
                "DATABASE_URL": "postgresql://fixture:fixture-password@db.invalid/kaika_staging",
                "SECRET_KEY": "fixture-secret-" * 4,
                "STAGING_ADMIN_PASSWORD": "fixture-admin-password-012345",
                "STAGING_NORMAL_PASSWORD": "fixture-normal-password-012345",
                "STAGING_BUSINESS_PASSWORD": "fixture-business-password-012345",
                "STAGING_HOSTNAME": "kaika-stage.example.invalid"}

    def test_dedicated_configuration_and_secret_redaction(self):
        config = validate_environment(self.environment())
        self.assertEqual(config.database_name, "kaika_staging")
        self.assertNotIn("fixture-password", repr(config))

    def test_refuses_production_database_and_missing_database(self):
        for url in ("", "sqlite:///fixture.db", "postgres://x:y@host/merchandise", "postgres://x:y@host/kaika_recovery_12345678"):
            env = self.environment()
            env["DATABASE_URL"] = url
            with self.subTest(url=url), self.assertRaises(StagingConfigurationError):
                validate_environment(env)

    def test_refuses_missing_environment_or_weak_password(self):
        for key, value in (("KAIKA_RUNTIME_ENV", "production"), ("SECRET_KEY", "short"), ("STAGING_ADMIN_PASSWORD", "admin123")):
            env = self.environment()
            env[key] = value
            with self.subTest(key=key), self.assertRaises(StagingConfigurationError):
                validate_environment(env)

    def test_external_credentials_cannot_be_accidentally_inherited(self):
        for key in ("STRIPE_SECRET_KEY", "FEATURE_STRIPE_SECRET_KEY", "LINE_CHANNEL_ACCESS_TOKEN", "SMTP_PASSWORD", "GOOGLE_APPLICATION_CREDENTIALS", "EXPO_ACCESS_TOKEN"):
            env = self.environment()
            env[key] = "fixture-sensitive-value-never-log"
            with self.subTest(key=key), self.assertRaises(StagingConfigurationError) as caught:
                validate_environment(env)
            self.assertNotIn("fixture-sensitive", str(caught.exception))

    def test_refuses_external_enabled_flags(self):
        for key in DISABLED_FLAGS:
            env = self.environment()
            env[key] = "1"
            with self.subTest(key=key), self.assertRaises(StagingConfigurationError):
                validate_environment(env)

    def test_refuses_production_hostname_or_origin(self):
        for key, value in (("STAGING_HOSTNAME", "stock.kaika-potential.co.jp"), ("PRIMARY_DOMAIN", "stock.kaika-potential.co.jp"), ("FEATURE_BILLING_ORIGIN", "https://other.invalid")):
            env = self.environment()
            env[key] = value
            with self.subTest(key=key), self.assertRaises(StagingConfigurationError):
                validate_environment(env)

    def test_outbound_clients_are_blocked_before_connect(self):
        restore = install_outbound_guard()
        try:
            with socket.socket() as connection:
                with self.assertRaisesRegex(OSError, "disabled"):
                    connection.connect(("127.0.0.1", 12345))
                with self.assertRaisesRegex(OSError, "disabled"):
                    connection.connect_ex(("127.0.0.1", 12345))
            with self.assertRaisesRegex(OSError, "disabled"):
                socket.create_connection(("example.invalid", 443))
        finally:
            restore()

    def test_scheduler_returns_before_constructing_any_job(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8-sig"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "init_scheduler")
        namespace = {"os": os, "scheduler": None}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "scheduler-fixture", "exec"), namespace)
        # Undefined scheduler classes would raise if the early return failed.
        with patch.dict(os.environ, {"KAIKA_RUNTIME_ENV": "staging"}):
            namespace["init_scheduler"]()
        self.assertIsNone(namespace["scheduler"])


class StagingMarkerTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.cur = self.conn.cursor()

    def tearDown(self):
        self.conn.close()

    def test_empty_database_is_marked_and_recheck_keeps_it(self):
        verify_database_marker(self.cur, False)
        self.cur.execute("CREATE TABLE users (id INTEGER)")
        verify_database_marker(self.cur, False)
        self.assertEqual(self.cur.execute("SELECT name FROM kaika_environment").fetchall(), [("staging",)])

    def test_unmarked_existing_database_is_untouched(self):
        self.cur.execute("CREATE TABLE users (id INTEGER)")
        self.cur.execute("INSERT INTO users VALUES (7)")
        with self.assertRaises(StagingConfigurationError):
            verify_database_marker(self.cur, False)
        self.assertEqual(self.cur.execute("SELECT * FROM users").fetchall(), [(7,)])
        self.assertEqual(self.cur.execute("SELECT name FROM sqlite_master WHERE name='kaika_environment'").fetchall(), [])

    def test_production_or_empty_marker_is_refused(self):
        self.cur.execute("CREATE TABLE kaika_environment (name TEXT)")
        for name in (None, "production"):
            if name:
                self.cur.execute("INSERT INTO kaika_environment VALUES (?)", (name,))
            with self.subTest(name=name), self.assertRaises(StagingConfigurationError):
                verify_database_marker(self.cur, False)

    def test_quarantined_recovery_is_refused_even_with_staging_marker(self):
        verify_database_marker(self.cur, False)
        self.cur.execute("CREATE TABLE kaika_recovery_quarantine (id INTEGER)")
        with self.assertRaises(StagingConfigurationError):
            verify_database_marker(self.cur, False)

    def test_fresh_disk_marker_and_existing_data_refusal(self):
        with tempfile.TemporaryDirectory(prefix="kaika-stage-volume-") as directory:
            check_upload_disk(directory, require_mount=False)
            (Path(directory) / "fixture.txt").write_text("fictional")
            check_upload_disk(directory, require_mount=False)
        with tempfile.TemporaryDirectory(prefix="kaika-stage-unmarked-") as directory:
            (Path(directory) / "existing.txt").write_text("fictional")
            with self.assertRaises(StagingConfigurationError):
                check_upload_disk(directory, require_mount=False)
            self.assertFalse((Path(directory) / ".kaika-staging-volume").exists())

    def test_upload_disk_must_be_mounted_in_hosted_startup(self):
        with tempfile.TemporaryDirectory(prefix="kaika-stage-mount-") as directory:
            with patch("os.path.ismount", return_value=False), self.assertRaises(StagingConfigurationError):
                check_upload_disk(directory)


class StagingSeedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kaika-stage-seed-")
        self.path = str(Path(self.temp.name) / "fixture.sqlite3")
        self.connections = []
        def get_db():
            connection = sqlite3.connect(self.path)
            self.connections.append(connection)
            return connection
        app = Flask(__name__)
        app.secret_key = "fixture-only"
        self.runtime = SimpleNamespace(DATABASE_URL=None, app=app,
            get_db=get_db, get_jst_now=lambda: datetime(2026, 9, 12, 12, 0))
        plans = FeaturePlans(self.runtime)
        app.extensions["kaika_feature_plans"] = plans
        app.extensions["kaika_self_inventory"] = SelfInventory(self.runtime, plans)
        self.passwords = {role: "fixture-" + role + "-strong-password-012345" for role in ("admin", "normal", "business")}
        with self.runtime.get_db() as conn:
            verify_database_marker(conn.cursor(), False)
            conn.execute("""CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE, email TEXT UNIQUE, password_hash TEXT, role TEXT, display_name TEXT)""")
            conn.execute("INSERT INTO users (username,password_hash) VALUES (?,?)", ("admin", generate_password_hash("admin123")))
            conn.execute("""CREATE TABLE merchandise (id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, product_name TEXT, scope TEXT, custody_location TEXT,
                purchase_date TEXT, storage_start_date TEXT, purchase_price INTEGER,
                listing_price INTEGER, notes TEXT)""")

    def tearDown(self):
        for connection in self.connections:
            connection.close()
        self.temp.cleanup()

    def test_seed_is_idempotent_preserves_test_work_and_changed_password(self):
        self.assertEqual(seed_test_data(self.runtime, self.passwords)["created_users"], 3)
        with self.runtime.get_db() as conn:
            conn.execute("UPDATE merchandise SET product_name='試験中に修正' WHERE id=1")
            conn.execute("UPDATE users SET password_hash='changed-by-tester' WHERE username='staging_normal'")
        self.assertEqual(seed_test_data(self.runtime, self.passwords)["created_users"], 0)
        with self.runtime.get_db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM merchandise").fetchone()[0], 4)
            self.assertEqual(conn.execute("SELECT product_name FROM merchandise WHERE id=1").fetchone()[0], "試験中に修正")
            self.assertEqual(conn.execute("SELECT password_hash FROM users WHERE username='staging_normal'").fetchone()[0], "changed-by-tester")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM self_inventory_items").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT plan_code FROM feature_manual_grants ORDER BY plan_code").fetchall(), [("business",), ("normal",)])
            self.assertFalse(check_password_hash(conn.execute("SELECT password_hash FROM users WHERE username='admin'").fetchone()[0], "admin123"))

    def test_existing_account_collision_refuses_without_overwriting(self):
        with self.runtime.get_db() as conn:
            conn.execute("INSERT INTO users (username,email,password_hash) VALUES ('staging_normal','existing@example.invalid','existing-hash')")
        with self.assertRaises(StagingConfigurationError):
            seed_test_data(self.runtime, self.passwords)
        with self.runtime.get_db() as conn:
            self.assertEqual(conn.execute("SELECT password_hash FROM users WHERE username='staging_normal'").fetchone()[0], "existing-hash")
            self.assertIsNone(conn.execute("SELECT id FROM users WHERE username='staging_admin'").fetchone())

    def test_deleted_seed_user_is_not_silently_recreated(self):
        seed_test_data(self.runtime, self.passwords)
        with self.runtime.get_db() as conn:
            conn.execute("DELETE FROM users WHERE username='staging_business'")
        with self.assertRaises(StagingConfigurationError):
            seed_test_data(self.runtime, self.passwords)


class StagingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "fixture-only"
        self.app.testing = True
        manager = LoginManager(self.app)

        class User(UserMixin):
            def __init__(self, name):
                self.id = self.username = name

        manager.user_loader(lambda identifier: User(identifier))

        @self.app.route("/login", methods=["GET", "POST"])
        def login():
            if request.method == "POST":
                login_user(User(request.form["username"]))
                return redirect(request.args.get("next") or "/")
            return "<html><body>login</body></html>"

        @self.app.get("/")
        def home():
            return "<html><body>inventory</body></html>"

        @self.app.route("/api/mobile/v1/session", methods=["POST"])
        def native_login():
            # Mirrors the bounded parser in the actual mobile_api module.
            body = json.loads(request.stream.read(8193))
            return jsonify(ok=body["username"] == "staging_business")

        @self.app.get("/api/mobile/v1/me")
        def native_me():
            return jsonify(error="fixture token required"), 401

        @self.app.get("/healthz")
        def healthz():
            return jsonify(status="ok")

        @self.app.get("/external-redirect")
        def external_redirect():
            return redirect("https://stock.kaika-potential.co.jp/")

        register_staging_boundary(SimpleNamespace(app=self.app))
        self.client = self.app.test_client()
        self.base = "https://kaika-stage.example.invalid"

    def login(self):
        return self.client.post("/login", data={"username": "staging_normal"}, base_url=self.base)

    def test_private_pages_and_uploads_require_login_health_remains_available(self):
        for path in ("/", "/proxy-service", "/static/uploads/photo.jpg"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, base_url=self.base).status_code, 302)
        self.assertEqual(self.client.get("/healthz", base_url=self.base).status_code, 200)
        self.assertEqual(self.client.get("/api/plans/me", base_url=self.base).status_code, 401)

    def test_seeded_browser_login_works_without_http_basic_auth(self):
        self.assertEqual(self.login().status_code, 302)
        response = self.client.get("/", base_url=self.base)
        self.assertEqual(response.status_code, 200)
        self.assertIn("架空データ専用", response.get_data(as_text=True))
        self.assertEqual(response.headers["X-Kaika-Environment"], "staging")
        self.assertIn("noindex", response.headers["X-Robots-Tag"])

    def test_default_account_and_unknown_native_account_are_rejected(self):
        self.assertEqual(self.client.post("/login", data={"username": "admin"}, base_url=self.base).status_code, 403)
        self.assertEqual(self.client.post("/api/mobile/v1/session", json={"username": "admin"}, base_url=self.base).status_code, 403)
        self.assertEqual(self.client.post("/api/mobile/v1/session", json={"username": "staging_business"}, base_url=self.base).status_code, 200)
        self.assertEqual(self.client.post("/api/mobile/v1/session", json=["invalid"], base_url=self.base).status_code, 400)
        self.assertEqual(self.client.post("/api/mobile/v1/session", json={"username": "staging_business", "password": "x" * 9000}, base_url=self.base).status_code, 413)
        self.assertEqual(self.client.get("/api/mobile/v1/me", base_url=self.base).status_code, 401)

    def test_external_side_effect_routes_and_imports_are_disabled_for_admin_too(self):
        self.login()
        for path in ("/admin/stripe/subscribe/1", "/admin/line/settings", "/line/webhook", "/billing/checkout", "/api/google-drive/download", "/internal/backups/run", "/admin/backup/import", "/backup/import_user", "/api/plans/store-verify"):
            with self.subTest(path=path):
                self.assertEqual(self.client.post(path, base_url=self.base).status_code, 403)

    def test_external_login_destination_and_runtime_redirect_are_rejected(self):
        for target in ("https://other.invalid", "//other.invalid", "/\\other.invalid"):
            with self.subTest(target=target):
                self.assertEqual(self.client.get("/login", query_string={"next": target}, base_url=self.base).status_code, 400)
        self.login()
        self.assertEqual(self.client.get("/external-redirect", base_url=self.base).status_code, 403)


if __name__ == "__main__":
    unittest.main()
