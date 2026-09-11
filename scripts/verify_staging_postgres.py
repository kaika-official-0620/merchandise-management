"""Run staging initialization against an explicitly supplied LOCAL fixture PG.

Connection JSON stays outside the repo and is never printed. This script does
not provision/stop the shared fixture cluster, use Render, or inspect user data.
Run once with --phase initialize, then in a new process with --phase restart.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import secrets
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import run_app_preview as sandbox


def fixture_url(raw):
    parsed = urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Only a loopback PostgreSQL fixture is supported.")
    if not parsed.port or parsed.port == 5432:
        raise ValueError("A dedicated, non-default fixture port is required.")
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(query_items)
    if parsed.fragment or len(query) != len(query_items) or set(query) - {"sslmode", "sslrootcert"}:
        raise ValueError("Fixture connection overrides are not allowed.")
    if query.get("sslmode") not in {"require", "verify-ca", "verify-full"}:
        raise ValueError("The fixture connection must require TLS.")
    return urlunsplit((parsed.scheme, parsed.netloc, "/kaika_staging", urlencode(query), ""))


def run(args):
    import psycopg2
    configuration = json.loads(args.connection_file.read_text(encoding="utf-8-sig"))
    raw = configuration.get("admin_database_url") or configuration.get("admin_dsn") or configuration.get("dsn") or configuration.get("database_url")
    if not raw:
        raise ValueError("Fixture connection JSON requires an explicit admin database URL.")
    dsn = fixture_url(raw)
    fingerprint = hashlib.sha256(dsn.encode()).hexdigest()
    args.workspace.mkdir(parents=True, exist_ok=True)
    state_path = args.workspace / "private-staging-test-state.json"
    if args.phase == "initialize" and not state_path.exists():
        parsed = urlsplit(dsn)
        admin_dsn = urlunsplit((parsed.scheme, parsed.netloc, "/postgres", parsed.query, ""))
        connection = psycopg2.connect(admin_dsn, connect_timeout=10)
        try:
            connection.autocommit = True
            cursor = connection.cursor()
            cursor.execute("SELECT 1 FROM pg_database WHERE datname='kaika_staging'")
            if cursor.fetchone():
                raise ValueError("kaika_staging already exists; refusing to overwrite fixture data.")
            cursor.execute("CREATE DATABASE kaika_staging")
        finally:
            connection.close()
        passwords = {role: secrets.token_urlsafe(32) for role in ("admin", "normal", "business")}
        state = {"passwords": passwords, "secret": secrets.token_urlsafe(48), "database_fingerprint": fingerprint}
        # Retain only in the private fixture workspace so a failed first boot
        # can be retried without replacing passwords or creating another DB.
        state_path.write_text(json.dumps(state), encoding="utf-8")
    else:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("database_fingerprint") != fingerprint:
            raise ValueError("The private state belongs to another fixture connection.")
        passwords = state["passwords"]
    # Source-only copy: existing DBs, uploads, env files and bytecode are omitted.
    mirror_root = args.workspace / (args.phase + "-" + secrets.token_hex(4))
    mirror_root.mkdir()
    runtime_path = sandbox.mirror_source(mirror_root, None, None)
    pg_modules = {name: value for name, value in sys.modules.items() if name == "psycopg2" or name.startswith("psycopg2.")}
    sandbox.isolate_environment(mirror_root, 18793)
    # The generic PC sandbox disables PG import; this runner retains the already
    # imported psycopg2 package and only the explicit local fixture connection.
    sys.modules.update(pg_modules)
    os.environ.update({"DATABASE_URL": dsn, "KAIKA_RUNTIME_ENV": "staging",
                       "STAGING_HOSTNAME": "kaika-fixture.example.invalid",
                       "PRIMARY_DOMAIN": "kaika-fixture.example.invalid", "SECRET_KEY": state["secret"]})
    for role, password in passwords.items():
        os.environ["STAGING_" + role.upper() + "_PASSWORD"] = password
    # No Python socket client, subprocess, existing local DB/upload read, or
    # write outside the new fixture directory can run after this boundary.
    connections = sandbox.install_runtime_boundary(args.workspace, 18793)
    sys.path[:] = [str(runtime_path)] + [p for p in sys.path if p and not sandbox.inside(Path(p).resolve(), sandbox.SOURCE)]
    original = Path.cwd()
    os.chdir(runtime_path)
    checks = []
    def check(name, condition):
        checks.append({"name": name, "pass": bool(condition)})
    try:
        boundary = importlib.import_module("staging_environment")
        config = boundary.validate_environment()
        boundary.prepare_environment(config)
        boundary.install_outbound_guard()
        with boundary.staging_bootstrap(config) as bootstrap_connection:
            check("bootstrap preserves verify-full TLS mode", bootstrap_connection.info.dsn_parameters.get("sslmode") == "verify-full")
            loaded = importlib.import_module("render_app")
            check("current source loads on local PostgreSQL", loaded.RUNTIME_SOURCE == "source")
            boundary.check_upload_disk(loaded.module, require_mount=False)
            seeded = boundary.seed_test_data(loaded.module, passwords)
            check("seed adds only first-run users", seeded["created_users"] == (3 if args.phase == "initialize" else 0))
            boundary.register_staging_boundary(loaded.module)
        conn = loaded.module.get_db()
        try:
            cur = conn.cursor()
            check("application preserves verify-full TLS mode", conn.info.dsn_parameters.get("sslmode") == "verify-full")
            cur.execute("SHOW server_version")
            server_version = cur.fetchone()[0]
            cur.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()")
            check("application DB connection uses TLS", cur.fetchone()[0] is True)
            cur.execute("SELECT COUNT(*) FROM merchandise")
            check("exactly four seed items", cur.fetchone()[0] == 4)
            cur.execute("SELECT id,password_hash FROM users WHERE username='staging_normal'")
            normal_id, password_hash = cur.fetchone()
            if args.phase == "initialize":
                cur.execute("UPDATE merchandise SET product_name='【検証用】再起動後にも保持する商品' WHERE user_id=%s AND custody_location='self' RETURNING id", (normal_id,))
                state["item_id"] = cur.fetchone()[0]
                state["password_hash"] = password_hash
                cur.execute("SELECT expires_at FROM feature_manual_grants WHERE user_id=%s", (normal_id,))
                state["grant_expires"] = cur.fetchone()[0]
                conn.commit()
                state_path.write_text(json.dumps(state), encoding="utf-8")
            else:
                cur.execute("SELECT product_name FROM merchandise WHERE id=%s", (state["item_id"],))
                check("restart preserves edited inventory", cur.fetchone()[0] == "【検証用】再起動後にも保持する商品")
                check("restart preserves password hash", password_hash == state["password_hash"])
                cur.execute("SELECT expires_at FROM feature_manual_grants WHERE user_id=%s", (normal_id,))
                check("restart preserves original grant expiry", cur.fetchone()[0] == state["grant_expires"])
            cur.close()
        finally:
            conn.close()
        base = "https://kaika-fixture.example.invalid"
        for role in ("admin", "normal", "business"):
            client = loaded.app.test_client()
            response = client.post("/login", data={"username": "staging_" + role, "password": passwords[role]}, base_url=base)
            check(role + " browser logs in", response.status_code == 302)
            response = client.get("/admin" if role == "admin" else "/", base_url=base)
            check(role + " inventory HTML works", response.status_code == 200 and b'data-kaika-staging="true"' in response.data)
        client = loaded.app.test_client()
        response = client.post("/api/mobile/v1/session", json={"username": "staging_normal", "password": passwords["normal"]}, base_url=base)
        payload = response.get_json() or {}
        check("native API authenticates against PostgreSQL", response.status_code == 200 and bool(payload.get("token")))
        response = client.get("/api/mobile/v1/items", headers={"Authorization": "Bearer " + payload.get("token", "")}, base_url=base)
        check("native API shares two owned items", response.status_code == 200 and (response.get_json() or {}).get("total") == 2)
        check("legacy admin remains denied", client.post("/login", data={"username": "admin", "password": "admin123"}, base_url=base).status_code == 403)
        check("external billing remains denied", client.post("/billing/checkout", base_url=base).status_code == 403)
        check("database health responds", client.get("/healthz", base_url=base).status_code == 200)
        report = {"phase": args.phase, "scope": "Local fictional PostgreSQL fixture over TLS; source-only mirror",
                  "postgres_version": server_version,
                  "render_hosted_or_device_tested": False, "render_disk_tested": False,
                  "checks": checks, "passed": sum(x["pass"] for x in checks), "failed": sum(not x["pass"] for x in checks)}
        (args.workspace / (args.phase + "-report.json")).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("STAGING_POSTGRES_JSON=" + json.dumps(report, ensure_ascii=False))
        return int(report["failed"] > 0)
    finally:
        for connection in connections:
            connection.close()
        os.chdir(original)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--phase", choices=("initialize", "restart"), required=True)
    args = parser.parse_args()
    args.workspace = args.workspace.resolve()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
