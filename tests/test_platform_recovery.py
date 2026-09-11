"""Fictional databases only. No application import, live DB, credentials or network."""
from contextlib import closing, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from scripts import platform_recovery as recovery


class PlatformRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="kaika-recovery-tests-")
        self.root = Path(self.temp.name).resolve()
        # TemporaryDirectory owns only its fresh, resolved test directory.
        self.addCleanup(self.temp.cleanup)
        self.source = self.root / "source"; self.source.mkdir()
        self.target = self.root / "target"; self.target.mkdir()
        for root in (self.source, self.target):
            (root / recovery.FIXTURE_MARKER).write_text(recovery.FIXTURE_TEXT, encoding="utf-8")
        self.db = self.source / "fixture.sqlite3"
        self.target_db = self.target / "restored.sqlite3"
        self.uploads = self.source / "uploads"; self.uploads.mkdir()
        (self.uploads / "商品画像").mkdir()
        (self.uploads / "商品画像" / "fixture-photo.jpg").write_bytes(b"fictional-image-bytes\x00\xff")
        self.restored_uploads = self.target / "uploads"; self.restored_uploads.mkdir()
        self.code = self.root / "code"; self.code.mkdir()
        (self.code / "app.py").write_text("# fictional release\n", encoding="utf-8")
        (self.code / "templates").mkdir()
        (self.code / "templates" / "index.html").write_text("<p>fixture</p>", encoding="utf-8")
        self.bundle = self.root / "bundle"; self.bundle.mkdir()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);
                INSERT INTO users VALUES(1,'fictional-owner');
                CREATE TABLE merchandise(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER REFERENCES users(id),
                    product_name TEXT,custody_location TEXT,custody_received_at TEXT,photo TEXT);
                INSERT INTO merchandise VALUES(1,1,'home-stock','self',NULL,'商品画像/fixture-photo.jpg');
                INSERT INTO merchandise VALUES(2,1,'shipped-stock','transit',NULL,NULL);
                INSERT INTO merchandise VALUES(3,1,'kaika-stock','kaika','2026-09-12T00:00:00Z',NULL);
                CREATE TABLE self_inventory_items(item_id INTEGER PRIMARY KEY REFERENCES merchandise(id),user_id INTEGER);
                INSERT INTO self_inventory_items VALUES(1,1);
                CREATE TABLE inventory_intakes(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),state TEXT);
                INSERT INTO inventory_intakes VALUES(1,1,'shipped');
                CREATE TABLE inventory_intake_items(intake_id INTEGER REFERENCES inventory_intakes(id),item_id INTEGER REFERENCES merchandise(id));
                INSERT INTO inventory_intake_items VALUES(1,2);
                CREATE TABLE inventory_custody_events(id INTEGER PRIMARY KEY,item_id INTEGER REFERENCES merchandise(id),event TEXT);
                INSERT INTO inventory_custody_events VALUES(1,3,'received');
                CREATE TABLE invoices(id INTEGER PRIMARY KEY,user_id INTEGER,total INTEGER,tax_kind TEXT);
                INSERT INTO invoices VALUES(1,1,1100,'10');
                CREATE TABLE feature_billing_accounts(user_id INTEGER PRIMARY KEY,customer_id TEXT);
                INSERT INTO feature_billing_accounts VALUES(1,'fixture-customer');
                CREATE TABLE feature_subscriptions(subscription_id TEXT PRIMARY KEY,user_id INTEGER,status TEXT,period_end INTEGER);
                INSERT INTO feature_subscriptions VALUES('fixture-subscription',1,'active',2000000000);
                CREATE TABLE feature_store_accounts(user_id INTEGER PRIMARY KEY,account_token TEXT);
                INSERT INTO feature_store_accounts VALUES(1,'fictional-store-account');
                CREATE TABLE feature_store_receipts(id INTEGER PRIMARY KEY,payload BLOB);
                INSERT INTO feature_store_receipts VALUES(1,X'010002');
                CREATE TABLE feature_plan_changes(id INTEGER PRIMARY KEY,subscription_id TEXT REFERENCES feature_subscriptions(subscription_id));
                INSERT INTO feature_plan_changes VALUES(1,'fixture-subscription');
                CREATE TABLE push_devices(installation TEXT PRIMARY KEY,enabled INTEGER,token TEXT);
                INSERT INTO push_devices VALUES('fixture-device',1,'fictional-push-token');
                CREATE TABLE push_outbox(id TEXT PRIMARY KEY,state TEXT,receipt TEXT);
                INSERT INTO push_outbox VALUES('one','queued',NULL);
                INSERT INTO push_outbox VALUES('two','receipt','fictional-receipt');
                INSERT INTO push_outbox VALUES('three','sending',NULL);
                INSERT INTO push_outbox VALUES('four','delivered','fictional-completed-receipt');
                CREATE TABLE future_business_extension(id INTEGER PRIMARY KEY,payload BLOB);
                INSERT INTO future_business_extension VALUES(1,X'FF00');
                CREATE TABLE kaika_environment(name TEXT);
                INSERT INTO kaika_environment VALUES('fictional-source-marker');
                CREATE INDEX custody_idx ON merchandise(custody_location);
                CREATE VIEW self_goods AS SELECT id FROM merchandise WHERE custody_location='self';
            """)

    def snapshot(self):
        return recovery.snapshot("sqlite-fixture", self.db, self.uploads, self.code, self.bundle,
                                 "https://source.example.invalid")

    def restore(self, **overrides):
        values = dict(bundle=self.bundle, database=self.target_db, uploads=self.restored_uploads,
                      code_dir=self.code, production_origin="https://production.example.invalid",
                      target_origin="https://recovery.example.invalid")
        values.update(overrides)
        return recovery.restore_bundle(**values)

    def test_whole_database_photos_ids_and_new_modules_round_trip(self):
        self.snapshot()
        report = self.restore()
        self.assertEqual(report["state"], "quarantined-not-activated")
        self.assertEqual(report["tables_checked"], 16)
        self.assertEqual(report["uploads_checked"], 1)
        with closing(sqlite3.connect(self.db)) as source, closing(sqlite3.connect(self.target_db)) as target:
            tables = [row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                if table not in ("push_devices", "push_outbox"):
                    self.assertEqual(source.execute('SELECT * FROM "' + table + '"').fetchall(),
                                     target.execute('SELECT * FROM "' + table + '"').fetchall(), table)
            self.assertEqual(target.execute("SELECT id FROM self_goods").fetchall(), [(1,)])
            self.assertEqual(target.execute("PRAGMA foreign_key_check").fetchall(), [])
            target.execute("INSERT INTO merchandise(user_id,product_name,custody_location) VALUES(1,'after-recovery','self')")
            self.assertEqual(target.execute("SELECT MAX(id) FROM merchandise").fetchone()[0], 4)
        self.assertEqual((self.restored_uploads / "商品画像" / "fixture-photo.jpg").read_bytes(),
                         (self.uploads / "商品画像" / "fixture-photo.jpg").read_bytes())

    def test_push_is_quarantined_without_rewriting_source_or_completed_history(self):
        self.snapshot(); report = self.restore()
        self.assertEqual((report["disabled_devices"], report["cancelled_pushes"]), (1, 3))
        with closing(sqlite3.connect(self.target_db)) as conn:
            self.assertEqual(conn.execute("SELECT enabled FROM push_devices").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT state FROM push_outbox ORDER BY id").fetchall(),
                             [("delivered",), ("cancelled",), ("cancelled",), ("cancelled",)])
            self.assertEqual(conn.execute("SELECT state FROM kaika_recovery_quarantine").fetchone()[0], "quarantined")
            self.assertEqual(conn.execute("SELECT name FROM kaika_environment").fetchone()[0], "fictional-source-marker")
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT enabled FROM push_devices").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT state FROM push_outbox WHERE id='one'").fetchone()[0], "queued")

    def test_corrupt_upload_is_rejected_before_target_creation(self):
        self.snapshot()
        (self.bundle / "uploads" / "商品画像" / "fixture-photo.jpg").write_bytes(b"corrupted")
        with self.assertRaisesRegex(recovery.RecoveryError, "integrity"):
            self.restore()
        self.assertFalse(self.target_db.exists())
        self.assertEqual(list(self.restored_uploads.iterdir()), [])

    def test_missing_and_unexpected_files_are_rejected(self):
        self.snapshot()
        extra = self.bundle / "unexpected.txt"; extra.write_text("fixture")
        with self.assertRaisesRegex(recovery.RecoveryError, "inventory"):
            self.restore()
        self.assertFalse(self.target_db.exists())

    def test_same_source_or_production_origin_is_rejected_before_writes(self):
        self.snapshot()
        for target in ("https://SOURCE.example.invalid:443/", "https://production.example.invalid"):
            with self.subTest(target=target), self.assertRaisesRegex(recovery.RecoveryError, "origin"):
                self.restore(target_origin=target)
        self.assertFalse(self.target_db.exists())

    def test_existing_database_is_never_replaced(self):
        self.snapshot()
        self.target_db.write_bytes(b"must remain")
        with self.assertRaisesRegex(recovery.RecoveryError, "must not exist"):
            self.restore()
        self.assertEqual(self.target_db.read_bytes(), b"must remain")

    def test_source_database_itself_is_never_overwritten(self):
        self.snapshot()
        digest = recovery.digest_file(self.db)
        with self.assertRaises(recovery.RecoveryError):
            self.restore(database=self.db)
        self.assertEqual(recovery.digest_file(self.db), digest)

    def test_nonempty_uploads_are_not_overwritten(self):
        self.snapshot()
        existing = self.restored_uploads / "existing-photo.jpg"; existing.write_bytes(b"keep")
        with self.assertRaisesRegex(recovery.RecoveryError, "empty"):
            self.restore()
        self.assertFalse(self.target_db.exists())
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_code_change_requires_the_exact_old_release(self):
        self.snapshot()
        (self.code / "app.py").write_text("# changed release\n", encoding="utf-8")
        with self.assertRaisesRegex(recovery.RecoveryError, "exactly"):
            self.restore()
        self.assertFalse(self.target_db.exists())

    def test_startup_scripts_are_in_the_release_fingerprint(self):
        scripts = self.code / "scripts"; scripts.mkdir()
        startup = scripts / "start_render.sh"; startup.write_text("# fixture startup\n")
        staging = self.code / "render.staging.yaml"; staging.write_text("# fixture staging\n")
        self.snapshot()
        startup.write_text("# changed startup\n")
        with self.assertRaisesRegex(recovery.RecoveryError, "exactly"):
            self.restore()
        self.assertFalse(self.target_db.exists())
        startup.write_text("# fixture startup\n")
        staging.write_text("# changed staging\n")
        with self.assertRaisesRegex(recovery.RecoveryError, "exactly"):
            self.restore()
        self.assertFalse(self.target_db.exists())

    def test_schema_mismatch_does_not_return_a_success_report(self):
        self.snapshot()
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        manifest["database"]["counts"][0][1] += 1
        (self.bundle / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(recovery.RecoveryError, "schema or row counts"):
            self.restore()
        self.assertEqual(list(self.restored_uploads.iterdir()), [])

    def test_unlabelled_sqlite_source_is_rejected(self):
        (self.source / recovery.FIXTURE_MARKER).write_text("not a fixture\n", encoding="utf-8")
        with self.assertRaisesRegex(recovery.RecoveryError, "fictional fixtures"):
            self.snapshot()
        self.assertEqual(list(self.bundle.iterdir()), [])

    def test_quarantined_snapshot_is_not_an_authoritative_backup(self):
        self.snapshot(); self.restore()
        new_bundle = self.root / "new-bundle"; new_bundle.mkdir()
        with self.assertRaisesRegex(recovery.RecoveryError, "Quarantined"):
            recovery.snapshot("sqlite-fixture", self.target_db, self.restored_uploads, self.code,
                              new_bundle, "https://recovery.example.invalid")

    def test_code_fingerprint_never_reads_uploads_or_env(self):
        static = self.code / "static"; static.mkdir()
        uploads = static / "uploads"; uploads.mkdir()
        (uploads / "private-photo.jpg").write_bytes(b"fictional private bytes")
        (self.code / ".env").write_text("FICTIONAL_SECRET=must-not-read", encoding="utf-8")
        original = recovery.digest_file
        seen = []
        def tracked(path):
            seen.append(path)
            return original(path)
        with patch.object(recovery, "digest_file", side_effect=tracked):
            recovery.code_fingerprint(self.code)
        self.assertFalse(any("uploads" in item.parts or item.name == ".env" for item in seen))

    def fake_decrypt(self, members):
        # Fixture tar transport only: this deliberately does NOT test age cryptography.
        archive_path = self.root / "fake-input.tar"
        with tarfile.open(archive_path, "w") as archive:
            for name, contents, kind in members:
                info = tarfile.TarInfo(name); info.size = len(contents); info.type = kind
                if kind == tarfile.SYMTYPE:
                    info.linkname = "../../outside"
                archive.addfile(info, io.BytesIO(contents))
        work = self.root / "decrypt-work"; work.mkdir()
        def fake_age(command, **kwargs):
            shutil.copyfile(archive_path, Path(command[command.index("--output") + 1]))
            return b""
        return work, fake_age

    def test_tar_traversal_symlinks_duplicates_and_size_limit_are_rejected(self):
        cases = [([("../escape", b"x", tarfile.REGTYPE)], 1024),
                 ([("uploads/link", b"", tarfile.SYMTYPE)], 1024),
                 ([("uploads/a", b"x", tarfile.REGTYPE), ("uploads/a", b"y", tarfile.REGTYPE)], 1024),
                 ([("uploads/large", b"12", tarfile.REGTYPE)], 1)]
        for index, (members, max_bytes) in enumerate(cases):
            with self.subTest(index=index):
                sub = self.root / str(index); sub.mkdir()
                old_root = self.root; self.root = sub
                work, fake_age = self.fake_decrypt(members)
                out = sub / "out"; out.mkdir()
                try:
                    with patch.object(recovery, "executable", return_value="fictional-age"), \
                         patch.object(recovery, "run_private", side_effect=fake_age), \
                         self.assertRaises(recovery.RecoveryError):
                        recovery.decrypt_bundle(sub / "fake-input.tar", sub / "fictional-key", out, work, max_bytes)
                    self.assertFalse((sub / "escape").exists())
                finally:
                    self.root = old_root

    def test_empty_upload_archive_is_supported(self):
        work, fake_age = self.fake_decrypt([("manifest.json", b"{}", tarfile.REGTYPE)])
        out = self.root / "empty-out"; out.mkdir()
        with patch.object(recovery, "executable", return_value="fictional-age"), \
             patch.object(recovery, "run_private", side_effect=fake_age):
            recovery.decrypt_bundle(self.root / "fake-input.tar", self.root / "fictional-key", out, work, 1024)
        self.assertTrue((out / "uploads").is_dir())

    def postgres_manifest(self):
        return {"kind": "postgres", "database": {"major": 17, "schema": {}, "counts": [], "sequences": []},
                "source_identity": recovery.hashlib.sha256(b"production_database").hexdigest(),
                "source_origin": "https://source.example.invalid", "release": {}, "files": {"database.dump": {}},
                "database_file": "database.dump"}

    def postgres_restore_context(self, manifest, name="kaika_recovery_12345678", empty=True):
        conn = MagicMock(); conn.__enter__.return_value = conn
        return (patch.object(recovery, "validate_bundle", return_value=manifest),
                patch.object(recovery, "postgres_connect", return_value=conn),
                patch.object(recovery, "postgres_identity", return_value=(name, 17)),
                patch.object(recovery, "postgres_is_empty", return_value=empty))

    def test_postgres_staging_and_production_database_names_are_rejected(self):
        for name in ("kaika_staging", "production_database", "kaika_recovery_short"):
            contexts = self.postgres_restore_context(self.postgres_manifest(), name=name)
            with contexts[0], contexts[1], contexts[2], contexts[3], \
                 patch.object(recovery, "run_private") as runner, self.assertRaises(recovery.RecoveryError):
                self.restore(database="fictional-dsn")
            runner.assert_not_called()

    def test_postgres_nonempty_database_is_rejected_before_restore(self):
        contexts = self.postgres_restore_context(self.postgres_manifest(), empty=False)
        with contexts[0], contexts[1], contexts[2], contexts[3], \
             patch.object(recovery, "run_private") as runner, self.assertRaisesRegex(recovery.RecoveryError, "contains objects"):
            self.restore(database="fictional-dsn")
        runner.assert_not_called()

    def test_postgres_uses_atomic_restore_and_quarantine_without_clean_or_create(self):
        manifest = self.postgres_manifest()
        contexts = self.postgres_restore_context(manifest)
        (self.bundle / "uploads").mkdir()
        changes = {"disabled_devices": 1, "cancelled_pushes": 1}
        with contexts[0], contexts[1], contexts[2], contexts[3], \
             patch.object(recovery, "check_pg_tool", return_value="fictional-pg_restore"), \
             patch.object(recovery, "postgres_metadata", return_value=manifest["database"]), \
             patch.object(recovery, "quarantine", return_value=changes) as quarantine, \
             patch.object(recovery, "run_private") as runner:
            report = self.restore(database="fictional-dsn")
        command = runner.call_args.args[0]
        self.assertIn("--single-transaction", command)
        self.assertIn("--exit-on-error", command)
        self.assertIn("--dbname=", command)
        self.assertNotIn("--clean", command); self.assertNotIn("--create", command)
        self.assertNotIn("fictional-dsn", command)
        quarantine.assert_called_once()
        self.assertEqual(report["state"], "quarantined-not-activated")

    def test_postgres_same_database_name_is_rejected_even_on_another_alias(self):
        manifest = self.postgres_manifest()
        manifest["source_identity"] = recovery.hashlib.sha256(b"kaika_recovery_12345678").hexdigest()
        contexts = self.postgres_restore_context(manifest)
        with contexts[0], contexts[1], contexts[2], contexts[3], \
             patch.object(recovery, "run_private") as runner, self.assertRaisesRegex(recovery.RecoveryError, "aliases"):
            self.restore(database="fictional-dsn")
        runner.assert_not_called()

    def test_subprocess_credentials_are_in_environment_only_and_errors_are_redacted(self):
        fake_params = {"host": "fixture.invalid", "dbname": "fixture", "user": "fixture", "password": "PRIVATE"}
        failed = MagicMock(returncode=1, stderr=b"PRIVATE", stdout=b"")
        with patch.object(recovery, "postgres_parameters", return_value=fake_params), \
             patch.object(recovery.subprocess, "run", return_value=failed) as run:
            with self.assertRaises(recovery.RecoveryError) as result:
                recovery.run_private(["fictional-pg_dump", "--version"], dsn="PRIVATE-URL")
        self.assertNotIn("PRIVATE", str(result.exception))
        self.assertEqual(run.call_args.kwargs["env"]["PGPASSWORD"], "PRIVATE")
        self.assertFalse(run.call_args.kwargs["shell"])
        self.assertNotIn("PRIVATE", " ".join(run.call_args.args[0]))

    def test_cli_driver_error_does_not_log_url_or_token(self):
        args = ["backup", "--kind", "postgres", "--database-env", "RECOVERY_SOURCE_DB", "--source-origin-env", "RECOVERY_SOURCE_ORIGIN",
                "--output", str(self.root / "backup.tar.age"), "--recipient", "age1" + "q" * 60,
                "--uploads", str(self.uploads), "--code-dir", str(self.code), "--work-root", str(self.root), "--all-writers-stopped"]
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(recovery.os.environ, {"RECOVERY_SOURCE_DB": "PRIVATE-URL", "RECOVERY_SOURCE_ORIGIN": "https://source.example.invalid"}), \
             patch.object(recovery, "executable", return_value="fictional-age"), \
             patch.object(recovery, "snapshot", side_effect=ValueError("PRIVATE-TOKEN")), \
             redirect_stderr(stderr), redirect_stdout(stdout):
            code = recovery.main(args)
        self.assertEqual(code, 1)
        self.assertNotIn("PRIVATE", stdout.getvalue() + stderr.getvalue())
        self.assertFalse((self.root / "backup.tar.age").exists())
        self.assertFalse(any(path.name.startswith("kaika-recovery-") for path in self.root.iterdir()))

    def test_invalid_cli_arguments_do_not_echo_pasted_secrets(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as result:
            recovery.main(["PRIVATE-URL"])
        self.assertEqual(result.exception.code, 2)
        self.assertNotIn("PRIVATE-URL", stderr.getvalue())

    def test_untrusted_encrypted_archive_digest_is_rejected_before_decryption(self):
        backup = self.root / "untrusted.tar.age"; backup.write_bytes(b"fictional encrypted content")
        args = ["restore", "--backup", str(backup), "--expected-backup-sha256", "0" * 64,
                "--identity-file", str(self.root / "key"), "--database-env", "RECOVERY_TARGET_DB",
                "--production-origin-env", "PRODUCTION_ORIGIN", "--target-origin-env", "TARGET_ORIGIN",
                "--report", str(self.root / "report.json"), "--max-bytes", "1000000",
                "--uploads", str(self.restored_uploads), "--code-dir", str(self.code), "--work-root", str(self.root),
                "--all-writers-stopped", "--isolated-target-confirmed"]
        with patch.dict(recovery.os.environ, {"RECOVERY_TARGET_DB": "fictional-target"}), \
             patch.object(recovery, "decrypt_bundle") as decrypt, redirect_stderr(io.StringIO()):
            self.assertEqual(recovery.main(args), 1)
        decrypt.assert_not_called()
        self.assertFalse(self.target_db.exists())


if __name__ == "__main__":
    unittest.main()
