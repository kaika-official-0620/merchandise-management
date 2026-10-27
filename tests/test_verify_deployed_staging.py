"""Guard tests; optional child process exercises copied real routes with fake transport.

All fixtures are new and temporary. No .env, saved database, or user photo is read.
The child transport substitutes ONLY health's database label (SQLite -> postgres)
so it can check the deployed CLI's route flow; this is never a real TLS/PG claim.
"""
from contextlib import redirect_stdout, redirect_stderr
import importlib
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import verify_deployed_staging as verify

ORIGIN = "https://" + verify.DEPLOYED_HOST


class Response:
    def __init__(self, body=b"ok", status=200, headers=None, url=None):
        self._content = body
        self.status_code = status
        self.headers = {"X-Kaika-Environment": "staging", **(headers or {})}
        self.url = url or ORIGIN + "/"
        self.closed = False

    @property
    def content(self):
        return self._content

    def iter_content(self, chunk_size):
        for offset in range(0, len(self._content), chunk_size):
            yield self._content[offset:offset + chunk_size]

    def json(self):
        return json.loads(self.content)

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.calls = []
        self.headers = {}
        self.trust_env = True
        self.closed = False

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def client(self, responses):
        session = Session(responses)
        return verify.GuardedSession(ORIGIN, "fixture-agent", lambda: session), session

    def test_rejects_other_origins_credentials_and_schemes_before_transport(self):
        targets = ["https://other.invalid/login", "//other.invalid/login", "http://" + verify.DEPLOYED_HOST,
                   "https://x@y.invalid/", "https://" + verify.DEPLOYED_HOST + ":444/login", "/login#fragment", "\\other.invalid"]
        client, session = self.client([])
        client.confirmed = True
        for target in targets:
            with self.subTest(target=target), self.assertRaises(verify.VerificationStopped):
                client.request("POST", target, data={"password": "not-a-real-secret"})
        self.assertEqual(session.calls, [])

    def test_password_cannot_be_sent_before_anonymous_confirmation(self):
        client, session = self.client([])
        with self.assertRaisesRegex(verify.VerificationStopped, "anonymous_staging_confirmation_required"):
            client.request("POST", "/login", data={"password": secrets.token_urlsafe(32)})
        self.assertEqual(session.calls, [])

    def test_missing_header_never_confirms(self):
        client, session = self.client([Response(headers={"X-Kaika-Environment": "production"})])
        with self.assertRaisesRegex(verify.VerificationStopped, "staging_response_header_required"):
            client.confirm(verify.DEPLOYED_HOST)
        self.assertFalse(client.confirmed)
        self.assertEqual([call[0] for call in session.calls], ["GET"])

    def test_health_database_and_domain_must_both_match(self):
        for database, domain in (("sqlite", verify.DEPLOYED_HOST), ("postgres", "elsewhere.invalid")):
            client, _ = self.client([Response(status=302, headers={"Location": "/login"}), Response(json.dumps({"status": "ok", "database": database, "primary_domain": domain}).encode())])
            with self.assertRaisesRegex(verify.VerificationStopped, "staging_health_identity_mismatch"):
                client.confirm(verify.DEPLOYED_HOST)
            self.assertFalse(client.confirmed)

    def test_login_html_marker_is_required_before_password(self):
        health = {"status": "ok", "database": "postgres", "primary_domain": verify.DEPLOYED_HOST}
        client, session = self.client([Response(status=302, headers={"Location": "/login"}), Response(json.dumps(health).encode()), Response(b"ordinary login")])
        with self.assertRaisesRegex(verify.VerificationStopped, "staging_login_marker_required"):
            client.confirm(verify.DEPLOYED_HOST)
        self.assertFalse(client.confirmed)
        self.assertTrue(all(method == "GET" for method, _, _ in session.calls))

    def test_verified_tls_no_proxy_and_no_redirect_following(self):
        response = Response(status=302, headers={"Location": "/login?next=%2F"})
        client, session = self.client([response])
        result = client.request("GET", "/")
        self.assertIs(result, response)
        self.assertFalse(session.trust_env)
        kwargs = session.calls[0][2]
        self.assertIs(kwargs["verify"], True)
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertTrue(response.closed)
        self.assertEqual(len(session.calls), 1)

    def test_external_redirect_is_rejected_without_following(self):
        client, session = self.client([Response(status=302, headers={"Location": "https://other.invalid/?token=not-real"})])
        with self.assertRaisesRegex(verify.VerificationStopped, "same_origin_https_required"):
            client.request("GET", "/login")
        self.assertEqual(len(session.calls), 1)

    def test_transport_override_is_not_allowed(self):
        for key in ("verify", "allow_redirects", "timeout", "auth", "stream"):
            client, session = self.client([])
            with self.assertRaisesRegex(verify.VerificationStopped, "transport_override_not_allowed"):
                client.request("GET", "/", **{key: False})
            self.assertEqual(session.calls, [])

    def test_error_and_oversize_response_never_echo_response_or_exception(self):
        secret = secrets.token_urlsafe(32)
        for result, code in ((RuntimeError(secret), "https_request_failed"), (Response(secret.encode() * 100000), "response_size_limit")):
            client, _ = self.client([result])
            with self.assertRaises(verify.VerificationStopped) as raised:
                client.request("GET", "/")
            self.assertEqual(str(raised.exception), code)
            self.assertNotIn(secret, str(raised.exception))

    def test_form_parser_never_selects_other_products_or_photo_removal(self):
        document = verify.Forms('''<form id="self-inventory-form" method="POST"><input name="csrf_token" value="fixture-csrf">
          <input name="submission_token" value="fixture-submission"><input name="product_name" value="架空 &amp; 確認">
          <input type="checkbox" checked name="item_ids" value="999"><input type="checkbox" checked name="remove_photo" value="1">
          <input name="disabled_value" disabled value="x"><textarea name="notes">架空 &lt;確認&gt;\nのみ</textarea></form>''')
        values = document.select(form_id="self-inventory-form")["values"]
        self.assertEqual(values["product_name"], "架空 & 確認")
        self.assertEqual(values["notes"], "架空 <確認>\nのみ")
        self.assertNotIn("item_ids", values)
        self.assertNotIn("remove_photo", values)
        self.assertNotIn("disabled_value", values)

    def test_ambiguous_or_changed_form_action_is_rejected(self):
        form = '<form action="/different" method="POST"><input name="csrf_token" value="x"><input name="submission_token" value="y"></form>'
        with self.assertRaises(verify.VerificationStopped):
            verify.Forms(form * 2).select()
        client, _ = self.client([Response(form.encode())])
        with self.assertRaisesRegex(verify.VerificationStopped, "form_action_changed"):
            verify.form_data(client, "/inventory/self/new")


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kaika-test-verification-ledger-")
        self.root = Path(self.temporary.name)
        (self.root / ".kaika-staging-volume").write_text("staging\n", encoding="utf-8")
        self.key = secrets.token_urlsafe(32)
        self.ledger = verify.RunLedger(self.root, self.key)
        self.run_id = secrets.token_hex(16)
        self.manifest = {"host": verify.DEPLOYED_HOST, "run_id": self.run_id, "name": "【動作確認・架空】" + self.run_id,
                         "item_id": 6, "photo_path": "/static/uploads/fixture-check.png", "photo_sha256": "a" * 64,
                         "received": False, "intake_id": None}

    def tearDown(self):
        self.temporary.cleanup()

    def test_requires_existing_staging_volume_marker(self):
        (self.root / ".kaika-staging-volume").write_text("production", encoding="utf-8")
        with self.assertRaisesRegex(verify.VerificationStopped, "marked_staging_disk_required"):
            verify.RunLedger(self.root, self.key)

    def test_new_manifest_round_trip_has_no_secret_and_never_overwrites(self):
        self.ledger.write(self.manifest)
        self.assertEqual(self.ledger.read(self.run_id, verify.DEPLOYED_HOST), self.manifest)
        self.assertNotIn(self.key, self.ledger.path(self.run_id).read_text(encoding="utf-8"))
        with self.assertRaises(FileExistsError):
            self.ledger.write(self.manifest)

    def test_cannot_read_arbitrary_path_or_item_from_tampered_manifest(self):
        self.ledger.write(self.manifest)
        for bad in ("../test", "a" * 31, "A" * 32, "a" * 32 + ".json"):
            with self.assertRaises(verify.VerificationStopped):
                self.ledger.read(bad, verify.DEPLOYED_HOST)
        path = self.ledger.path(self.run_id)
        changed = json.loads(path.read_text(encoding="utf-8"))
        changed["manifest"]["item_id"] = 12345
        path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaisesRegex(verify.VerificationStopped, "verification_manifest_invalid"):
            self.ledger.read(self.run_id, verify.DEPLOYED_HOST)

    def test_changed_host_or_rotated_signing_key_refuses_replay(self):
        self.ledger.write(self.manifest)
        for ledger, host in ((self.ledger, "other.invalid"), (verify.RunLedger(self.root, secrets.token_urlsafe(32)), verify.DEPLOYED_HOST)):
            with self.assertRaisesRegex(verify.VerificationStopped, "verification_manifest_invalid"):
                ledger.read(self.run_id, host)

    def test_invalid_replay_stops_before_any_network_or_mutation(self):
        for run_id in ("bad", ""):
            calls = []
            result = verify.exercise(SimpleNamespace(host=verify.DEPLOYED_HOST), {}, ledger=self.ledger,
                                     session_factory=lambda: calls.append(True), verify_run=run_id)
            self.assertEqual(result["failed"], 1)
            self.assertFalse(result["created_item"])
            self.assertEqual(result["mode"], "inventory_read_only_replay")
            self.assertEqual(calls, [])


def runtime_child():
    """Disposable real routes; HTTPS socket and PG health are fixture transport."""
    import run_app_preview as sandbox
    original = Path.cwd()
    logs = io.StringIO()
    with tempfile.TemporaryDirectory(prefix="kaika-deployed-cli-fixture-") as directory:
        preview = Path(directory).resolve()
        runtime = sandbox.mirror_source(preview, None, None)
        sandbox.isolate_environment(preview, 18793)
        os.environ.update({"MOBILE_API_ENABLED": "1", "KAIKA_RUNTIME_ENV": "staging",
                           "PRIMARY_DOMAIN": verify.DEPLOYED_HOST, "STAGING_HOSTNAME": verify.DEPLOYED_HOST,
                           "PRIMARY_SCHEME": "https"})
        connections = sandbox.install_runtime_boundary(preview, 18793)
        sys.path[:] = [str(runtime)] + [p for p in sys.path if p and not sandbox.inside(Path(p).resolve(), sandbox.SOURCE)]
        os.chdir(runtime)
        try:
            with redirect_stdout(logs), redirect_stderr(logs):
                boundary = importlib.import_module("staging_environment")
                with sqlite3.connect(preview / "preview.sqlite3") as connection:
                    boundary.verify_database_marker(connection.cursor(), False)
                loaded = importlib.import_module("render_app")
                passwords = {role: secrets.token_urlsafe(32) for role in boundary.TEST_USERS}
                boundary.seed_test_data(loaded.module, passwords)
                boundary.register_staging_boundary(loaded.module)
                app = loaded.app
                uploads = Path(app.config["UPLOAD_FOLDER"]).resolve()
                boundary.check_upload_disk(uploads, require_mount=False)
                ledger = verify.RunLedger(uploads, os.environ["SECRET_KEY"])
                environ = {"STAGING_" + role.upper() + "_PASSWORD": value for role, value in passwords.items()}
                environ["SECRET_KEY"] = os.environ["SECRET_KEY"]
                calls = []

                class FlaskTransport(Session):
                    def __init__(self):
                        super().__init__()
                        self.client = app.test_client()

                    def request(self, method, url, **kwargs):
                        path = urlsplit(url).path
                        data = dict(kwargs.get("data", {}))
                        for key, (filename, body, content_type) in kwargs.get("files", {}).items():
                            data[key] = (io.BytesIO(body), filename, content_type)
                        response = self.client.open(path, method=method, data=data or None, base_url=ORIGIN,
                                                    headers=self.headers, follow_redirects=False)
                        calls.append((method, path, response.status_code))
                        body = response.get_data()
                        if path == "/healthz":
                            health = response.get_json()
                            assert health["database"] == "sqlite", "Only the disposable SQLite fixture is allowed"
                            health["database"] = "postgres"  # Explicit transport fixture, no PostgreSQL assertion.
                            body = json.dumps(health).encode()
                        return Response(body, response.status_code, dict(response.headers), url)

                config = SimpleNamespace(host=verify.DEPLOYED_HOST)
                created = verify.exercise(config, environ, include_intake=True, session_factory=FlaskTransport, ledger=ledger)
                before_replay = len(calls)
                replay = verify.exercise(config, environ, session_factory=FlaskTransport, ledger=ledger, verify_run=created.get("run_id", "invalid"))
                replay_writes = [method for method, path, _ in calls[before_replay:] if method != "GET" and path != "/login"]
                assert not replay_writes, "Replay may authenticate but cannot write inventory"
                body = {"scope": "Copied real render_app routes; disposable SQLite; synthetic HTTPS/PG health transport only",
                        "real_https_or_postgres_tested": False, "server_errors": sum(status >= 500 for _, _, status in calls),
                        "replay_inventory_writes": len(replay_writes), "create": created, "replay": replay}
        finally:
            for connection in connections:
                connection.close()
            os.chdir(original)
    print("DEPLOYED_CLI_FIXTURE_JSON=" + json.dumps(body, ensure_ascii=False))
    return int(body["server_errors"] or created["failed"] or replay["failed"])


if __name__ == "__main__":
    if "--runtime-child" in sys.argv:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        raise SystemExit(runtime_child())
    unittest.main()
