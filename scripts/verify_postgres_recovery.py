"""Disposable localhost PostgreSQL + actual age drill; never imports the app.

Use official portable binaries. This creates no Windows service and reads no
existing database, .env, or uploads. Context contains ephemeral fixture secrets;
share only its local path, never its contents. Stop only after all drill users finish.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlencode, urlsplit

import platform_recovery as recovery

LABEL = "kaika-postgresql-fictional-drill-v1"


def run(command, *, env=None, timeout=90):
    # A Windows daemon may inherit pipe handles after pg_ctl exits. File capture
    # lets the control process finish without waiting for the server's stdout EOF.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout,
            stderr=stderr, env=env, timeout=timeout, shell=False,
            creationflags=0x08000000 if os.name == "nt" else 0)
        recovery.require(result.returncode == 0, "A fixture command failed; private output was withheld.")
        stdout.seek(0)
        return stdout.read().decode("utf-8", errors="replace")


def clean_environment():
    return {key: value for key, value in os.environ.items() if not key.startswith("PG")}


def prepare(root, pg_bin):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    root, pg_bin = recovery.plain_path(root), recovery.plain_path(pg_bin)
    recovery.require(root.is_dir() and not any(root.iterdir()), "The new fixture root must be empty.")
    root.chmod(0o700)
    (root / ".fixture-owned").write_text(LABEL, encoding="utf-8")
    password = secrets.token_urlsafe(32)
    password_file = root / "fixture-password.txt"
    password_file.write_text(password + "\n", encoding="utf-8"); password_file.chmod(0o600)
    cluster = root / "cluster"
    run([str(pg_bin / "initdb.exe"), "--pgdata=" + str(cluster), "--username=kaika_fixture_admin",
         "--auth=scram-sha-256", "--pwfile=" + str(password_file), "--locale=C", "--encoding=UTF8",
         "--no-instructions"], env=clean_environment())
    now = datetime.now(timezone.utc)
    authority = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Kaika fictional local drill CA")])
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_cert = x509.CertificateBuilder().subject_name(authority).issuer_name(authority).public_key(ca_key.public_key()) \
        .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=5)) \
        .not_valid_after(now + timedelta(days=2)).add_extension(x509.BasicConstraints(ca=True, path_length=0), True) \
        .sign(ca_key, hashes.SHA256())
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_cert = x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])) \
        .issuer_name(authority).public_key(server_key.public_key()).serial_number(x509.random_serial_number()) \
        .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=2)) \
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False) \
        .sign(ca_key, hashes.SHA256())
    cert_path = root / "fixture-ca.crt"; cert_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    (cluster / "server.crt").write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    key_path = cluster / "server.key"
    key_path.write_bytes(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                 serialization.NoEncryption())); key_path.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    with (cluster / "postgresql.conf").open("a", encoding="utf-8") as settings:
        settings.write(f"\nlisten_addresses='127.0.0.1'\nport={port}\nssl=on\nssl_cert_file='server.crt'\nssl_key_file='server.key'\nmax_connections=30\n")
    (cluster / "pg_hba.conf").write_text(
        "hostssl all all 127.0.0.1/32 scram-sha-256\nhostnossl all all 127.0.0.1/32 reject\n", encoding="utf-8")
    url = f"postgresql://kaika_fixture_admin:{quote(password, safe='')}@127.0.0.1:{port}/postgres?" + \
          urlencode({"sslmode": "verify-full", "sslrootcert": str(cert_path)})
    context = {"label": LABEL, "root": str(root), "pg_bin": str(pg_bin), "cluster": str(cluster),
               "admin_database_url": url, "sslrootcert": str(cert_path), "port": port}
    context_file = root / "fixture-context.json"
    context_file.write_text(json.dumps(context), encoding="utf-8"); context_file.chmod(0o600)
    run([str(pg_bin / "pg_ctl.exe"), "start", "--pgdata=" + str(cluster), "--log=" + str(root / "postgres.log"),
         "--wait", "--timeout=30"], env=clean_environment())
    return {"state": "fictional-cluster-running-localhost-tls", "context_file": str(context_file)}


def read_context(path):
    path = recovery.plain_path(path)
    recovery.require(path.name == "fixture-context.json", "Use the owned fixture context.")
    context = json.loads(path.read_text(encoding="utf-8"))
    root = recovery.plain_path(context["root"])
    recovery.require(path.parent == root and (root / ".fixture-owned").read_text(encoding="utf-8") == LABEL and
                     context.get("label") == LABEL and Path(context["cluster"]).resolve() == root / "cluster" and
                     urlsplit(context["admin_database_url"]).hostname == "127.0.0.1", "Invalid fixture context.")
    return context


def fixture_connect(url, database=None):
    import psycopg2
    params = recovery.postgres_parameters(url)
    recovery.require(params["host"] == "127.0.0.1", "Fixture DB must be on localhost.")
    if database:
        params["dbname"] = database
    return psycopg2.connect(**params, connect_timeout=10)


def source_fixture(conn):
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE users(id BIGSERIAL PRIMARY KEY,username TEXT);
        INSERT INTO users(username) VALUES('fictional-owner');
        CREATE TABLE merchandise(id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id),
          product_name TEXT,custody_location TEXT,custody_received_at TIMESTAMPTZ,photo TEXT);
        INSERT INTO merchandise(user_id,product_name,custody_location,custody_received_at,photo) VALUES
          (1,'home-stock','self',NULL,'商品画像/fixture-photo.jpg'),
          (1,'shipped-stock','transit',NULL,NULL),(1,'kaika-stock','kaika','2026-09-12T00:00:00Z',NULL);
        CREATE TABLE self_inventory_items(item_id BIGINT PRIMARY KEY REFERENCES merchandise(id),user_id BIGINT);
        INSERT INTO self_inventory_items VALUES(1,1);
        CREATE TABLE inventory_intakes(id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id),state TEXT);
        INSERT INTO inventory_intakes(user_id,state) VALUES(1,'shipped');
        CREATE TABLE inventory_intake_items(intake_id BIGINT REFERENCES inventory_intakes(id),item_id BIGINT REFERENCES merchandise(id));
        INSERT INTO inventory_intake_items VALUES(1,2);
        CREATE TABLE inventory_custody_events(id BIGSERIAL PRIMARY KEY,item_id BIGINT REFERENCES merchandise(id),event TEXT);
        INSERT INTO inventory_custody_events(item_id,event) VALUES(3,'received');
        CREATE TABLE invoices(id BIGSERIAL PRIMARY KEY,user_id BIGINT,total INTEGER,tax_kind TEXT);
        INSERT INTO invoices(user_id,total,tax_kind) VALUES(1,1100,'10');
        CREATE TABLE feature_billing_accounts(user_id BIGINT PRIMARY KEY,customer_id TEXT);
        INSERT INTO feature_billing_accounts VALUES(1,'fixture-customer');
        CREATE TABLE feature_subscriptions(subscription_id TEXT PRIMARY KEY,user_id BIGINT,status TEXT,period_end BIGINT);
        INSERT INTO feature_subscriptions VALUES('fixture-subscription',1,'active',2000000000);
        CREATE TABLE feature_store_accounts(user_id BIGINT PRIMARY KEY,account_token TEXT);
        INSERT INTO feature_store_accounts VALUES(1,'fictional-store-account');
        CREATE TABLE feature_store_receipts(id BIGSERIAL PRIMARY KEY,payload BYTEA);
        INSERT INTO feature_store_receipts(payload) VALUES(decode('010002','hex'));
        CREATE TABLE feature_plan_changes(id BIGSERIAL PRIMARY KEY,subscription_id TEXT REFERENCES feature_subscriptions(subscription_id));
        INSERT INTO feature_plan_changes(subscription_id) VALUES('fixture-subscription');
        CREATE TABLE push_devices(installation TEXT PRIMARY KEY,enabled INTEGER,token TEXT);
        INSERT INTO push_devices VALUES('fixture-device',1,'fictional-push-token');
        CREATE TABLE push_outbox(id TEXT PRIMARY KEY,state TEXT,receipt TEXT);
        INSERT INTO push_outbox VALUES('one','queued',NULL),('two','receipt','fictional-receipt'),
          ('three','sending',NULL),('four','delivered','fictional-completed-receipt');
        CREATE TABLE future_business_extension(id BIGSERIAL PRIMARY KEY,payload BYTEA);
        INSERT INTO future_business_extension(payload) VALUES(decode('ff00','hex'));
        CREATE TABLE kaika_environment(name TEXT);
        INSERT INTO kaika_environment VALUES('fictional-source-marker');
        CREATE INDEX custody_idx ON merchandise(custody_location);
        CREATE VIEW self_goods AS SELECT id FROM merchandise WHERE custody_location='self';
        """)
    conn.commit()


def verify(context, age_bin, report_path):
    from psycopg2 import sql
    from psycopg2.extensions import make_dsn
    root = Path(context["root"])
    work = root / ("recovery-drill-" + secrets.token_hex(6)); work.mkdir()
    source_name = "kaika_fixture_source_" + secrets.token_hex(4)
    target_name = "kaika_recovery_" + secrets.token_hex(8)
    blocked_name = "kaika_recovery_" + secrets.token_hex(8)
    owner_name = "kaika_fixture_owner_" + secrets.token_hex(4)
    owner_password = secrets.token_urlsafe(32)
    with closing(fixture_connect(context["admin_database_url"])) as admin:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %s").format(sql.Identifier(owner_name)),
                        (owner_password,))
            for name in (source_name, target_name, blocked_name):
                cur.execute(sql.SQL("CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'").format(
                    sql.Identifier(name), sql.Identifier(owner_name)))
    params = {**recovery.postgres_parameters(context["admin_database_url"]), "user": owner_name, "password": owner_password}
    owner_dsn = make_dsn(**params)
    with closing(fixture_connect(owner_dsn, source_name)) as conn:
        source_fixture(conn)
    with closing(fixture_connect(owner_dsn, blocked_name)) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE keep_existing(value INTEGER); INSERT INTO keep_existing VALUES(42)")
        conn.commit()
    code = work / "fictional-release"; code.mkdir()
    (code / "app.py").write_text("# fictional release; never imported\n", encoding="utf-8")
    uploads = work / "source-uploads"; uploads.mkdir()
    (uploads / "商品画像").mkdir()
    image = uploads / "商品画像" / "fixture-photo.jpg"; image.write_bytes(b"fictional-image-bytes\x00\xff")
    target_uploads = work / "target-uploads"; target_uploads.mkdir()
    blocked_uploads = work / "blocked-uploads"; blocked_uploads.mkdir()
    identities = work / "identities"; identities.mkdir()
    identity = identities / "fixture.agekey"
    run([str(Path(age_bin) / "age-keygen.exe"), "--output", str(identity)])
    recipient = run([str(Path(age_bin) / "age-keygen.exe"), "-y", str(identity)]).strip()
    archive = work / "fixture-backup.tar.age"
    environment = clean_environment()
    environment["PATH"] = str(age_bin) + os.pathsep + context["pg_bin"] + os.pathsep + environment.get("PATH", "")
    environment["FIXTURE_SOURCE_DB"] = make_dsn(**{**params, "dbname": source_name})
    environment["FIXTURE_TARGET_DB"] = make_dsn(**{**params, "dbname": target_name})
    environment["FIXTURE_BLOCKED_DB"] = make_dsn(**{**params, "dbname": blocked_name})
    environment.update(FIXTURE_SOURCE_ORIGIN="https://fixture-source.example.invalid",
                       FIXTURE_PRODUCTION_ORIGIN="https://fixture-production.example.invalid",
                       FIXTURE_TARGET_ORIGIN="https://fixture-recovery.example.invalid")
    cli = [sys.executable, str(Path(recovery.__file__).resolve())]
    backup_args = ["backup", "--kind", "postgres", "--database-env", "FIXTURE_SOURCE_DB", "--source-origin-env", "FIXTURE_SOURCE_ORIGIN",
                   "--output", str(archive), "--recipient", recipient, "--uploads", str(uploads), "--code-dir", str(code),
                   "--work-root", str(work), "--all-writers-stopped"]
    saved = json.loads(run(cli + backup_args, env=environment))
    recovery.require(saved["state"] == "encrypted-backup-created", "Backup did not complete.")
    restore_args = ["restore", "--backup", str(archive), "--expected-backup-sha256", saved["backup_sha256"],
        "--identity-file", str(identity), "--database-env", "FIXTURE_TARGET_DB", "--production-origin-env", "FIXTURE_PRODUCTION_ORIGIN",
        "--target-origin-env", "FIXTURE_TARGET_ORIGIN", "--report", str(work / "restored.json"), "--max-bytes", "10000000",
        "--uploads", str(target_uploads), "--code-dir", str(code), "--work-root", str(work),
        "--all-writers-stopped", "--isolated-target-confirmed"]
    restored = json.loads(run(cli + restore_args, env=environment))
    checks = {"encrypted_backup_created": saved["state"] == "encrypted-backup-created",
              "actual_age_decrypt_and_restore": restored["state"] == "quarantined-not-activated",
              "all_16_tables_checked": restored["tables_checked"] == 16,
              "photo_hash_equal": recovery.digest_file(target_uploads / "商品画像" / "fixture-photo.jpg") == recovery.digest_file(image),
              "push_counts": restored["disabled_devices"] == 1 and restored["cancelled_pushes"] == 3}
    with closing(fixture_connect(owner_dsn, source_name)) as source, \
         closing(fixture_connect(owner_dsn, target_name)) as target:
        with source.cursor() as source_cur, target.cursor() as target_cur:
            source_cur.execute("SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user")
            checks["ordinary_database_owner"] = source_cur.fetchone() == (False, False, False)
            source_cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'")
            tables = [row[0] for row in source_cur.fetchall()]
            for table in tables:
                if table not in ("push_devices", "push_outbox"):
                    statement = sql.SQL("SELECT * FROM {} ORDER BY 1").format(sql.Identifier(table))
                    source_cur.execute(statement); target_cur.execute(statement)
                    checks["rows_" + table] = source_cur.fetchall() == target_cur.fetchall()
            target_cur.execute("SELECT count(*) FROM push_outbox WHERE state IN ('queued','receipt','sending')")
            checks["no_replayable_push"] = target_cur.fetchone()[0] == 0
            target_cur.execute("SELECT enabled FROM push_devices")
            checks["devices_disabled"] = target_cur.fetchone()[0] == 0
            target_cur.execute("SELECT state FROM push_outbox WHERE id='four'")
            checks["delivered_history_retained"] = target_cur.fetchone()[0] == "delivered"
            source_cur.execute("SELECT state FROM push_outbox WHERE id='one'")
            checks["source_queue_unchanged"] = source_cur.fetchone()[0] == "queued"
            target_cur.execute("SELECT state FROM kaika_recovery_quarantine")
            checks["quarantine_marker"] = target_cur.fetchone()[0] == "quarantined"
            target_cur.execute("INSERT INTO merchandise(user_id,product_name,custody_location) VALUES(1,'after-restore','self') RETURNING id")
            checks["sequence_continues"] = target_cur.fetchone()[0] == 4
            target.rollback()
    blocked_args = restore_args.copy()
    blocked_args[blocked_args.index("FIXTURE_TARGET_DB")] = "FIXTURE_BLOCKED_DB"
    blocked_args[blocked_args.index(str(target_uploads))] = str(blocked_uploads)
    blocked_args[blocked_args.index(str(work / "restored.json"))] = str(work / "blocked.json")
    result = subprocess.run(cli + blocked_args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=environment, shell=False, timeout=90, creationflags=0x08000000 if os.name == "nt" else 0)
    checks["existing_database_refused"] = result.returncode == 1 and b"contains objects" in result.stderr
    with closing(fixture_connect(owner_dsn, blocked_name)) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM keep_existing")
            checks["existing_database_preserved"] = cur.fetchall() == [(42,)]
    checks["failed_restore_wrote_no_photos"] = not any(blocked_uploads.iterdir())
    report = {"date": recovery.utc_now(), "scope": "localhost-only fictional PostgreSQL + real age",
              "postgres_version": run([str(Path(context["pg_bin"]) / "postgres.exe"), "--version"]).strip(),
              "age_version": run([str(Path(age_bin) / "age.exe"), "--version"]).strip(),
              "tls": "verify-full with disposable local CA", "checks": checks, "check_count": len(checks),
              "failures": [name for name, passed in checks.items() if not passed],
              "production_data_accessed": False, "application_started": False, "windows_service_created": False}
    recovery.require(all(checks.values()), "The recovery drill found an inconsistency.")
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = recovery.PrivateArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "start", "verify", "stop"))
    parser.add_argument("--root"); parser.add_argument("--pg-bin"); parser.add_argument("--context")
    parser.add_argument("--age-bin"); parser.add_argument("--report")
    args = parser.parse_args()
    try:
        if args.action == "prepare":
            result = prepare(args.root, args.pg_bin)
        else:
            context = read_context(args.context)
            if args.action == "verify":
                result = verify(context, args.age_bin, args.report)
            elif args.action == "start":
                run([str(Path(context["pg_bin"]) / "pg_ctl.exe"), "start", "--pgdata=" + context["cluster"],
                     "--log=" + str(Path(context["root"]) / "postgres.log"), "--wait", "--timeout=30"], env=clean_environment())
                result = {"state": "fictional-cluster-running-localhost-tls"}
            else:
                run([str(Path(context["pg_bin"]) / "pg_ctl.exe"), "stop", "--pgdata=" + context["cluster"],
                     "--mode=fast", "--wait", "--timeout=30"], env=clean_environment())
                result = {"state": "fictional-cluster-stopped"}
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except Exception:
        print("FICTIONAL_RECOVERY_DRILL_FAILED: private details withheld; no production operation was attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
