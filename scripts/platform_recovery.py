"""Offline, encrypted whole-database + uploads recovery into a new quarantine.

Never imports the app, reads .env, starts workers, or activates a recovered site.
CLI backups always use age encryption. SQLite is restricted to labelled fixtures.
See docs/recovery-runbook.md before running against any authorized service.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit

FORMAT = "kaika-whole-recovery-v1"
QUARANTINE = "kaika_recovery_quarantine"
FIXTURE_MARKER = ".kaika-recovery-fixture"
FIXTURE_TEXT = "fictional-data-only\n"
RECOVERY_NAME = re.compile(r"kaika_recovery_[a-z0-9]{8,40}\Z")
SAFE_ENV = re.compile(r"[A-Z][A-Z0-9_]*\Z")


class RecoveryError(Exception):
    """Messages are deliberately constant; credentials and data never enter logs."""


def require(condition, message):
    if not condition:
        raise RecoveryError(message)


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def origin(value):
    parsed = urlsplit(value)
    require(parsed.scheme == "https" and bool(parsed.hostname) and
            not parsed.username and not parsed.password and
            parsed.path in ("", "/") and not parsed.query and not parsed.fragment,
            "An explicit HTTPS application origin without credentials is required.")
    return f"https://{parsed.hostname.lower()}:{parsed.port or 443}"


def env_value(name):
    require(bool(SAFE_ENV.fullmatch(name or "")), "Invalid environment variable name.")
    value = os.environ.get(name, "")
    require(bool(value), "A required environment variable is missing.")
    return value


def assert_separate_origins(production, target, source):
    require(origin(target) not in (origin(production), origin(source)),
            "The recovery application origin must differ from production and source.")


def plain_path(path):
    path = Path(path).absolute()
    for part in (path, *path.parents):
        require(not part.is_symlink() and not (hasattr(part, "is_junction") and part.is_junction()),
                "Symbolic links and junctions are not allowed in recovery paths.")
    return path.resolve()


def regular_files(root):
    root = plain_path(root)
    require(root.is_dir(), "An existing directory is required.")
    files = []
    for item in sorted(root.rglob("*")):
        plain_path(item)
        if item.is_dir():
            continue
        require(item.is_file() and item.stat().st_nlink == 1,
                "Only regular, unlinked files are supported.")
        files.append(item)
    return files


def copy_new(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as src, target.open("xb") as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)


def code_fingerprint(root):
    """Only code/public assets; explicitly excludes customer uploads and secrets."""
    root = plain_path(root)
    require((root / "app.py").is_file(), "The exact application release directory is required.")
    files = list(root.glob("*.py"))
    for name in ("requirements.txt", "render.yaml", "render.staging.yaml"):
        if (root / name).is_file():
            files.append(root / name)
    # Include deployment/startup and operational scripts in the release boundary.
    if (root / "scripts").is_dir():
        files.extend(item for item in (root / "scripts").glob("*") if item.suffix in (".py", ".sh", ".ps1"))
    for folder in ("templates", "static"):
        base = root / folder
        if not base.exists():
            continue
        for directory, dirs, names in os.walk(base, followlinks=False):
            # Do not enumerate or read the uploads tree, even to fingerprint code.
            dirs[:] = [name for name in dirs if name not in ("uploads", "__pycache__")]
            plain_path(directory)
            for name in names:
                item = Path(directory) / name
                if item.suffix.lower() in {".html", ".css", ".js", ".json", ".svg", ".png", ".jpg", ".jpeg", ".ico", ".webp"}:
                    files.append(item)
    entries = {}
    for item in sorted(files):
        plain_path(item)
        require(item.is_file(), "A release file is unavailable.")
        entries[item.relative_to(root).as_posix()] = digest_file(item)
    return {"sha256": digest_json(entries), "files": len(entries)}


@contextmanager
def private_workspace(parent):
    parent = plain_path(parent)
    require(parent.is_dir(), "A protected work directory must already exist.")
    work = Path(tempfile.mkdtemp(prefix="kaika-recovery-", dir=parent)).resolve()
    try:
        work.chmod(0o700)
        yield work
    finally:
        # Only a newly allocated child of the explicitly selected workspace.
        require(work.parent == parent and work.name.startswith("kaika-recovery-") and
                not work.is_symlink(), "Temporary workspace containment check failed.")
        shutil.rmtree(work)


@contextmanager
def sqlite_readonly(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        yield conn


def assert_fixture(path):
    path = plain_path(path)
    marker = path.parent / FIXTURE_MARKER
    require(marker.is_file() and marker.read_text(encoding="utf-8") == FIXTURE_TEXT,
            "SQLite recovery is supported only in explicitly labelled fictional fixtures.")
    return path


def sqlite_metadata(conn):
    schema = [list(row) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    tables = [row[1] for row in schema if row[0] == "table"]
    counts = [[name, conn.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0]]
              for name in tables]
    require({"users", "merchandise"}.issubset(tables), "The source is not a supported application database.")
    require(QUARANTINE not in tables, "Quarantined databases cannot be used as authoritative backups.")
    require(conn.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite integrity validation failed.")
    require(not conn.execute("PRAGMA foreign_key_check").fetchall(), "Database references are inconsistent.")
    return {"schema": schema, "counts": counts, "name": "sqlite-fixture", "major": None}


@contextmanager
def postgres_connect(dsn):
    import psycopg2
    with closing(psycopg2.connect(**postgres_parameters(dsn), connect_timeout=10,
                                 application_name="kaika-platform-recovery", options="-c search_path=public")) as conn:
        with conn:
            yield conn


def postgres_parameters(dsn):
    from psycopg2.extensions import parse_dsn
    params = parse_dsn(dsn)
    require(set(params).issubset({"host", "port", "dbname", "user", "password", "sslmode", "sslrootcert"}) and
            all(params.get(key) for key in ("host", "dbname", "user")) and "," not in params["host"],
            "Use a direct, single-host PostgreSQL URL without service or session overrides.")
    params.setdefault("sslmode", "require")
    require(params["sslmode"] in ("require", "verify-ca", "verify-full"), "PostgreSQL recovery requires TLS.")
    return params


def postgres_identity(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(),current_setting('server_version_num')::integer")
        name, version = cur.fetchone()
    return name, version // 10000


def postgres_is_empty(conn):
    with conn.cursor() as cur:
        # A newly provisioned DB may contain public and the built-in plpgsql extension.
        cur.execute("""SELECT EXISTS(
          SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema'
          UNION ALL SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
          WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema'
          UNION ALL SELECT 1 FROM pg_namespace n
          WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname NOT IN ('public','information_schema')
          UNION ALL SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
          WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema'
          UNION ALL SELECT 1 FROM pg_extension WHERE extname!='plpgsql')""")
        return not cur.fetchone()[0]


def postgres_metadata(conn):
    from psycopg2 import sql
    with conn.cursor() as cur:
        # This application does not use these facilities. Restoring background DB
        # extensions/subscriptions could make external calls before an app starts.
        cur.execute("""SELECT EXISTS(
            SELECT 1 FROM pg_subscription UNION ALL SELECT 1 FROM pg_foreign_server
            UNION ALL SELECT 1 FROM pg_event_trigger
            UNION ALL SELECT 1 FROM pg_extension WHERE extname!='plpgsql')""")
        require(not cur.fetchone()[0],
                "Unsupported database extensions or external/background facilities require a separate recovery review.")
        cur.execute("""SELECT table_schema,table_name FROM information_schema.tables
            WHERE table_schema NOT LIKE 'pg_%' AND table_schema!='information_schema'
              AND table_type='BASE TABLE' ORDER BY table_schema,table_name""")
        tables = cur.fetchall()
        require({("public", "users"), ("public", "merchandise")}.issubset(set(tables)),
                "The source is not a supported application database.")
        require(all(name != QUARANTINE for _, name in tables),
                "Quarantined databases cannot be used as authoritative backups.")
        counts = []
        for schema, table in tables:
            cur.execute(sql.SQL("SELECT COUNT(*) FROM {}.{}").format(sql.Identifier(schema), sql.Identifier(table)))
            counts.append([schema, table, cur.fetchone()[0]])
        schema = {}
        queries = {
            "columns": """SELECT table_schema,table_name,ordinal_position,column_name,data_type,
                udt_schema,udt_name,is_nullable,column_default FROM information_schema.columns
                WHERE table_schema NOT LIKE 'pg_%' AND table_schema!='information_schema'
                ORDER BY table_schema,table_name,ordinal_position""",
            "constraints": """SELECT n.nspname,c.conname,pg_get_constraintdef(c.oid)
                FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
                WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema'
                ORDER BY n.nspname,c.conname,pg_get_constraintdef(c.oid)""",
            "indexes": """SELECT schemaname,tablename,indexname,indexdef FROM pg_indexes
                WHERE schemaname NOT LIKE 'pg_%' AND schemaname!='information_schema'
                ORDER BY schemaname,tablename,indexname""",
            "views": """SELECT schemaname,viewname,definition FROM pg_views
                WHERE schemaname NOT LIKE 'pg_%' AND schemaname!='information_schema'
                ORDER BY schemaname,viewname""",
            "routines": """SELECT n.nspname,p.proname,pg_get_functiondef(p.oid)
                FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema' AND p.prokind IN ('f','p')
                ORDER BY n.nspname,p.proname,pg_get_functiondef(p.oid)""",
        }
        for name, query in queries.items():
            cur.execute(query)
            schema[name] = [list(row) for row in cur.fetchall()]
        cur.execute("""SELECT schemaname,sequencename,start_value,min_value,max_value,
            increment_by,cycle,cache_size,last_value FROM pg_sequences
            WHERE schemaname NOT LIKE 'pg_%' AND schemaname!='information_schema'
            ORDER BY schemaname,sequencename""")
        sequences = [list(row) for row in cur.fetchall()]
    name, major = postgres_identity(conn)
    return {"schema": schema, "counts": counts, "sequences": sequences, "name": name, "major": major}


def run_private(command, *, dsn=None):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    if dsn is not None:
        mapping = {"host": "PGHOST", "port": "PGPORT", "dbname": "PGDATABASE", "user": "PGUSER",
                   "password": "PGPASSWORD", "sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT"}
        environment.update({mapping[key]: value for key, value in postgres_parameters(dsn).items()})
        environment.update(PGCONNECT_TIMEOUT="10", PGAPPNAME="kaika-platform-recovery", PGOPTIONS="-c search_path=public")
    result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=environment, shell=False, check=False)
    require(result.returncode == 0 and not result.stderr,
            "An external recovery tool failed or emitted a warning; details were withheld from logs.")
    return result.stdout


def executable(name):
    value = shutil.which(name)
    require(value is not None, "A required recovery executable is not installed.")
    return value


def check_pg_tool(name, major):
    tool = executable(name)
    output = run_private([tool, "--version"]).decode("ascii", errors="replace")
    match = re.search(r"\b(\d+)(?:\.\d+)+", output)
    require(match is not None and int(match.group(1)) == major,
            "PostgreSQL client and source server major versions must match.")
    return tool


def snapshot(kind, database, uploads, code_dir, bundle, source_origin):
    """Private plaintext stage; public CLI always encrypts it before delivery."""
    uploads, bundle = plain_path(uploads), plain_path(bundle)
    require(not bundle.is_relative_to(uploads), "Recovery output cannot be inside uploads.")
    require(bundle.is_dir() and not any(bundle.iterdir()), "The private bundle directory must be empty.")
    release = code_fingerprint(code_dir)
    if kind == "sqlite-fixture":
        source = assert_fixture(database)
        require(uploads.is_relative_to(source.parent), "Fixture uploads must stay inside their labelled fixture directory.")
        with sqlite_readonly(source) as conn, closing(sqlite3.connect(bundle / "database.sqlite3")) as dest:
            conn.backup(dest)
        with sqlite_readonly(bundle / "database.sqlite3") as conn:
            metadata = sqlite_metadata(conn)
        identity = hashlib.sha256(str(source).encode()).hexdigest()
        database_file = "database.sqlite3"
    else:
        with postgres_connect(database) as conn:
            conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
            metadata = postgres_metadata(conn)
            tool = check_pg_tool("pg_dump", metadata["major"])
            with conn.cursor() as cur:
                cur.execute("SELECT pg_export_snapshot()")
                snapshot_id = cur.fetchone()[0]
            run_private([tool, "--format=custom", "--no-password", "--snapshot=" + snapshot_id,
                         "--file=" + str(bundle / "database.dump")], dsn=database)
        # Database-name inequality is intentionally conservative across hosts/aliases.
        identity = hashlib.sha256(metadata["name"].encode()).hexdigest()
        database_file = "database.dump"
    (bundle / "uploads").mkdir()
    source_files = regular_files(uploads)
    for item in source_files:
        copy_new(item, bundle / "uploads" / item.relative_to(uploads))
    files = {item.relative_to(bundle).as_posix(): {"sha256": digest_file(item), "size": item.stat().st_size}
             for item in regular_files(bundle)}
    # Detect edits/additions/removals during a copy; write suspension is still required.
    require([item.relative_to(uploads).as_posix() for item in regular_files(uploads)] ==
            [item.relative_to(uploads).as_posix() for item in source_files],
            "Uploads changed during the backup; no backup was completed.")
    for item in source_files:
        require(digest_file(item) == files["uploads/" + item.relative_to(uploads).as_posix()]["sha256"],
                "Uploads changed during the backup; no backup was completed.")
    require(code_fingerprint(code_dir) == release, "Application code changed during backup.")
    manifest = {"format": FORMAT, "created_at": utc_now(), "kind": kind,
                "source_origin": origin(source_origin), "source_identity": identity,
                "database_file": database_file, "database": metadata,
                "release": release, "files": files, "writes_suspended_attested": True}
    (bundle / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=True, indent=2), encoding="utf-8")
    return manifest


def validate_bundle(bundle, code_dir):
    manifest_path = bundle / "manifest.json"
    require(manifest_path.stat().st_size <= 32 * 1024 * 1024, "The manifest is too large.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(manifest.get("format") == FORMAT and manifest.get("kind") in ("postgres", "sqlite-fixture"),
            "Unsupported recovery format.")
    require(manifest.get("release") == code_fingerprint(code_dir),
            "Recovery requires exactly the application release recorded in the backup.")
    expected = "database.dump" if manifest["kind"] == "postgres" else "database.sqlite3"
    require(manifest.get("database_file") == expected, "Invalid database archive name.")
    actual = {item.relative_to(bundle).as_posix() for item in regular_files(bundle)}
    require(actual == set(manifest["files"]) | {"manifest.json"} and expected in actual,
            "Backup file inventory does not match its manifest.")
    require(all(name == expected or name.startswith("uploads/") for name in manifest["files"]),
            "Unexpected recovery file.")
    for name, metadata in manifest["files"].items():
        path = bundle / name
        require(path.stat().st_size == metadata["size"] and digest_file(path) == metadata["sha256"],
                "A backup file failed its integrity check.")
    return manifest


def encrypt_bundle(bundle, output, recipient, work):
    require(bool(re.fullmatch(r"age1[0-9a-z]{50,100}", recipient)), "An age X25519 public recipient is required.")
    tar_path = work / "backup.tar"
    with tarfile.open(tar_path, "w") as archive:
        for item in regular_files(bundle):
            archive.add(item, arcname=item.relative_to(bundle).as_posix(), recursive=False)
    encrypted = work / "backup.tar.age"
    run_private([executable("age"), "--encrypt", "--recipient", recipient, "--output", str(encrypted), str(tar_path)])
    copy_new(encrypted, output)


def decrypt_bundle(backup, identity_file, bundle, work, max_bytes):
    tar_path = work / "restored.tar"
    run_private([executable("age"), "--decrypt", "--identity", str(identity_file),
                 "--output", str(tar_path), str(backup)])
    total, seen = 0, set()
    with tarfile.open(tar_path, "r:") as archive:
        for member in archive:
            parts = PurePosixPath(member.name)
            require(member.isfile() and not parts.is_absolute() and ".." not in parts.parts and
                    "\\" not in member.name and ":" not in member.name and
                    member.name not in seen and parts.parts and member.name == parts.as_posix(),
                    "The archive contains an unsafe or duplicate member.")
            require(member.name in ("manifest.json", "database.dump", "database.sqlite3") or
                    (parts.parts[0] == "uploads" and len(parts.parts) > 1), "Unexpected archive member.")
            total += member.size
            require(member.size >= 0 and total <= max_bytes, "The backup exceeds the explicit recovery size limit.")
            seen.add(member.name)
            target = bundle.joinpath(*parts.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
    # Empty upload directories have no tar file member but are valid snapshots.
    (bundle / "uploads").mkdir(exist_ok=True)


def quarantine(conn, postgres, manifest):
    """Preserve business/history rows, but prevent any queued push replay."""
    with_cursor = conn.cursor()
    try:
        if postgres:
            with_cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        else:
            with_cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in with_cursor.fetchall()}
        changed = {"disabled_devices": 0, "cancelled_pushes": 0}
        if "push_devices" in tables:
            with_cursor.execute("UPDATE push_devices SET enabled=0 WHERE enabled!=0")
            changed["disabled_devices"] = with_cursor.rowcount
        if "push_outbox" in tables:
            with_cursor.execute("UPDATE push_outbox SET state='cancelled' WHERE state IN ('queued','receipt','sending')")
            changed["cancelled_pushes"] = with_cursor.rowcount
        with_cursor.execute("CREATE TABLE " + QUARANTINE +
                            " (id INTEGER PRIMARY KEY, state TEXT NOT NULL, source_digest TEXT NOT NULL, restored_at TEXT NOT NULL)")
        statement = "INSERT INTO " + QUARANTINE + " VALUES (1, 'quarantined', ?, ?)"
        with_cursor.execute(statement.replace("?", "%s") if postgres else statement,
                            (digest_json(manifest), utc_now()))
        conn.commit()
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        with_cursor.close()


def restore_bundle(bundle, database, uploads, code_dir, production_origin, target_origin):
    manifest = validate_bundle(bundle, code_dir)
    assert_separate_origins(production_origin, target_origin, manifest["source_origin"])
    uploads = plain_path(uploads)
    require(uploads.is_dir() and not any(uploads.iterdir()), "Recovery uploads must be an existing empty directory.")
    require(not uploads.is_relative_to(bundle) and not bundle.is_relative_to(uploads),
            "Recovery uploads and backup workspace must be separate.")
    if manifest["kind"] == "sqlite-fixture":
        target = assert_fixture(database)
        require(not target.exists(), "The SQLite recovery target must not exist.")
        require(hashlib.sha256(str(target).encode()).hexdigest() != manifest["source_identity"],
                "Source and target databases must differ.")
        # Exclusive creation, then backup, avoids overwriting a racing target file.
        with target.open("xb"):
            pass
        with sqlite_readonly(bundle / manifest["database_file"]) as source, closing(sqlite3.connect(target)) as conn:
            source.backup(conn)
            require(sqlite_metadata(conn) == manifest["database"], "Restored database schema or row counts differ.")
            changes = quarantine(conn, False, manifest)
    else:
        with postgres_connect(database) as conn:
            name, major = postgres_identity(conn)
            require(bool(RECOVERY_NAME.fullmatch(name)), "Use a dedicated kaika_recovery_ database with a unique suffix.")
            require(hashlib.sha256(name.encode()).hexdigest() != manifest["source_identity"],
                    "Source and target database names must differ, including across server aliases.")
            require(major == manifest["database"]["major"], "Recovery does not perform PostgreSQL version upgrades.")
            require(postgres_is_empty(conn), "The PostgreSQL recovery target contains objects; no restore was started.")
            conn.commit()
            tool = check_pg_tool("pg_restore", major)
            run_private([tool, "--no-password", "--single-transaction", "--exit-on-error", "--no-owner",
                         "--no-acl", "--no-tablespaces", "--dbname=", str(bundle / "database.dump")], dsn=database)
            recovered = postgres_metadata(conn)
            require(recovered["schema"] == manifest["database"]["schema"] and
                    recovered["counts"] == manifest["database"]["counts"] and
                    recovered["sequences"] == manifest["database"]["sequences"],
                    "Restored database schema or row counts differ.")
            changes = quarantine(conn, True, manifest)
    for item in regular_files(bundle / "uploads"):
        target = uploads / item.relative_to(bundle / "uploads")
        copy_new(item, target)
        require(digest_file(target) == digest_file(item), "A restored upload failed integrity validation.")
    return {"format": FORMAT, "state": "quarantined-not-activated", "restored_at": utc_now(),
            "backup_digest": digest_json(manifest), "release": manifest["release"],
            "tables_checked": len(manifest["database"]["counts"]),
            "uploads_checked": len(manifest["files"]) - 1, **changes}


class PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally repeats a bad option/value, which might be a pasted URL.
        self.print_usage(sys.stderr)
        self.exit(2, "RECOVERY_STOPPED: Invalid arguments. Use --help; provide variable names instead of secret values.\n")


def parser():
    result = PrivateArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup", help="Create an encrypted DB + uploads archive after stopping all writes.")
    backup.add_argument("--kind", choices=("postgres", "sqlite-fixture"), required=True)
    backup.add_argument("--database-env", required=True, help="Environment variable name, never a URL/password value.")
    backup.add_argument("--source-origin-env", required=True)
    backup.add_argument("--output", required=True)
    backup.add_argument("--recipient", required=True, help="age X25519 PUBLIC recipient; store its private identity separately.")
    restore = commands.add_parser("restore", help="Restore to an empty, dedicated, unstarted recovery quarantine.")
    restore.add_argument("--backup", required=True)
    restore.add_argument("--expected-backup-sha256", required=True,
                         help="Digest from the separately trusted backup inventory, not from inside the archive.")
    restore.add_argument("--identity-file", required=True)
    restore.add_argument("--database-env", required=True)
    restore.add_argument("--production-origin-env", required=True)
    restore.add_argument("--target-origin-env", required=True)
    restore.add_argument("--report", required=True)
    restore.add_argument("--max-bytes", type=int, required=True, help="Explicit maximum expanded size in bytes.")
    restore.add_argument("--isolated-target-confirmed", action="store_true", required=True)
    for command in (backup, restore):
        command.add_argument("--uploads", required=True)
        command.add_argument("--code-dir", required=True)
        command.add_argument("--work-root", required=True, help="Existing private encrypted volume with sufficient free space.")
        command.add_argument("--all-writers-stopped", action="store_true", required=True)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        uploads, work_root = plain_path(args.uploads), plain_path(args.work_root)
        require(not work_root.is_relative_to(uploads), "The work directory cannot be inside uploads.")
        database = env_value(args.database_env)
        if args.command == "backup":
            output = plain_path(args.output)
            require(not output.exists() and output.parent.is_dir() and not output.is_relative_to(uploads),
                    "Select a new backup file outside uploads in an existing directory.")
            executable("age")
            with private_workspace(work_root) as work:
                bundle = work / "bundle"; bundle.mkdir()
                manifest = snapshot(args.kind, database, uploads, args.code_dir, bundle, env_value(args.source_origin_env))
                encrypt_bundle(bundle, output, args.recipient, work)
            print(json.dumps({"state": "encrypted-backup-created", "tables": len(manifest["database"]["counts"]),
                              "uploads": len(manifest["files"]) - 1, "backup_sha256": digest_file(output)}))
        else:
            require(args.max_bytes > 0, "A positive recovery size limit is required.")
            report_path = plain_path(args.report)
            require(not report_path.exists() and report_path.parent.is_dir() and not report_path.is_relative_to(uploads),
                    "Select a new recovery report outside uploads.")
            backup_path = plain_path(args.backup)
            require(bool(re.fullmatch(r"[a-f0-9]{64}", args.expected_backup_sha256)) and
                    digest_file(backup_path) == args.expected_backup_sha256,
                    "The encrypted backup does not match the separately trusted inventory digest.")
            with private_workspace(work_root) as work:
                bundle = work / "bundle"; bundle.mkdir()
                decrypt_bundle(backup_path, plain_path(args.identity_file), bundle, work, args.max_bytes)
                report = restore_bundle(bundle, database, uploads, args.code_dir,
                                        env_value(args.production_origin_env), env_value(args.target_origin_env))
                with report_path.open("x", encoding="utf-8") as destination:
                    json.dump(report, destination, indent=2)
            print(json.dumps(report))
        return 0
    except RecoveryError as error:
        print("RECOVERY_STOPPED: " + str(error), file=sys.stderr)
    except Exception:
        # Driver/OS/subprocess errors may embed URLs, SQL, tokens, or filenames.
        print("RECOVERY_STOPPED: A private recovery operation failed. No application was activated.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
