"""Check real site + staging seed/access boundary in a disposable SQLite mirror.

This deliberately does not claim hosted PostgreSQL/disk startup verification.
Only test passwords generated in memory are used; no credential enters output.
"""
import importlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile

import run_app_preview as sandbox


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="kaika-staging-runtime-") as directory:
        preview = Path(directory).resolve()
        runtime_path = sandbox.mirror_source(preview, None, None)
        sandbox.isolate_environment(preview, 18792)
        os.environ.update({"MOBILE_API_ENABLED": "1", "KAIKA_RUNTIME_ENV": "staging"})
        connections = sandbox.install_runtime_boundary(preview, 18792)
        sys.path[:] = [str(runtime_path)] + [p for p in sys.path if p and not sandbox.inside(Path(p).resolve(), sandbox.SOURCE)]
        os.chdir(runtime_path)
        try:
            boundary = importlib.import_module("staging_environment")
            with sqlite3.connect(preview / "preview.sqlite3") as conn:
                boundary.verify_database_marker(conn.cursor(), False)
            loaded = importlib.import_module("render_app")
            passwords = {role: secrets.token_urlsafe(32) for role in boundary.TEST_USERS}
            first = boundary.seed_test_data(loaded.module, passwords)
            second = boundary.seed_test_data(loaded.module, passwords)
            boundary.register_staging_boundary(loaded.module)
            app = loaded.app
            checks = []
            def check(name, value):
                checks.append({"name": name, "pass": bool(value)})
            check("current source runtime", loaded.RUNTIME_SOURCE == "source")
            check("three users seeded exactly once", first["created_users"] == 3 and second["created_users"] == 0)
            base = "https://kaika-stage.example.invalid"
            for role, (username, _, _) in boundary.TEST_USERS.items():
                client = app.test_client()
                response = client.post("/login", data={"username": username, "password": passwords[role]}, base_url=base)
                check(role + " browser authenticates", response.status_code == 302)
                response = client.get("/" if role != "admin" else "/admin", base_url=base)
                check(role + " HTML retains stage marker", response.status_code == 200 and b'data-kaika-staging="true"' in response.data)
                if role != "admin":
                    response = client.get("/plans", base_url=base)
                    check(role + " plan page works", response.status_code == 200)
                check(role + " real billing remains blocked", client.get("/admin/stripe/subscribe/1", base_url=base).status_code == 403)
            native = app.test_client()
            response = native.post("/api/mobile/v1/session", json={"username": "staging_normal", "password": passwords["normal"]}, base_url=base)
            payload = response.get_json() or {}
            if response.status_code != 200:
                print("STAGING_NATIVE_DIAGNOSTIC=" + json.dumps({"status": response.status_code, "error": payload.get("error"), "location": response.location}))
            check("native API authenticates seeded tester", response.status_code == 200 and bool(payload.get("token")))
            token = payload.get("token", "")
            headers = {"Authorization": "Bearer " + token}
            response = native.get("/api/mobile/v1/session", headers=headers, base_url=base)
            check("native bearer session remains usable", response.status_code == 200)
            response = native.get("/api/mobile/v1/items", headers=headers, base_url=base)
            check("native API sees seeded same-user inventory", response.status_code == 200 and (response.get_json() or {}).get("total") == 2)
            check("legacy default admin denied", native.post("/login", data={"username": "admin", "password": "admin123"}, base_url=base).status_code == 403)
            check("anonymous private inventory requires login", app.test_client().get("/", base_url=base).status_code == 302)
            check("health check stays available", app.test_client().get("/healthz", base_url=base).status_code == 200)
            result = {"scope": "Copied real render_app + staging helpers; disposable SQLite; all outbound networking blocked",
                      "hosted_postgres_and_render_disk_tested": False, "checks": checks,
                      "passed": sum(check["pass"] for check in checks), "failed": sum(not check["pass"] for check in checks)}
            print("STAGING_RUNTIME_JSON=" + json.dumps(result, ensure_ascii=False))
        finally:
            for connection in connections:
                connection.close()
            os.chdir(original)
    return int(result["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
