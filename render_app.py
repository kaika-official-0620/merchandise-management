# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parent
PYC_PATH = REPO_DIR / "__pycache__" / "app.cpython-311.pyc"

if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

os.chdir(REPO_DIR)


def load_runtime_module():
    def load_pyc_runtime(source_error=None):
        if not PYC_PATH.exists():
            raise RuntimeError(f"render runtime fallback not found: {PYC_PATH}") from source_error

        loader = importlib.machinery.SourcelessFileLoader("app_render_runtime", str(PYC_PATH))
        spec = importlib.util.spec_from_loader("app_render_runtime", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)

        # When loading from pyc, point Flask back at the repository assets.
        module.app.root_path = str(REPO_DIR)
        module.app.template_folder = str(REPO_DIR / "templates")
        module.app.static_folder = str(REPO_DIR / "static")
        if module.app.jinja_loader:
            module.app.jinja_loader.searchpath = [str(REPO_DIR / "templates")]
        module.UPLOAD_FOLDER = str(REPO_DIR / "static" / "uploads")
        module.app.config["UPLOAD_FOLDER"] = module.UPLOAD_FOLDER

        if source_error is not None:
            print(f"[INFO] loaded render runtime from pyc fallback: {source_error}", flush=True)
        else:
            print("[INFO] loaded render runtime from pyc (preferred on Render)", flush=True)
        return module, "pyc"

    source_error = None
    try:
        import app as source_module

        return source_module, "source"
    except Exception as exc:
        source_error = exc

    return load_pyc_runtime(source_error)


module, RUNTIME_SOURCE = load_runtime_module()

for initializer_name in ("init_db", "migrate_add_scope_column"):
    initializer = getattr(module, initializer_name, None)
    if callable(initializer):
        initializer()

ensure_curated_master_catalog = getattr(module, "ensure_curated_master_catalog", None)
get_db = getattr(module, "get_db", None)
if callable(ensure_curated_master_catalog) and callable(get_db):
    seed_conn = get_db()
    try:
        ensure_curated_master_catalog(seed_conn, "admin", force=True)
        ensure_curated_master_catalog(seed_conn, "user", force=True)
    finally:
        seed_conn.close()

try:
    from preview_runtime_patches import apply as apply_runtime_patches

    apply_runtime_patches(module)
except Exception as patch_exc:
    print(f"[WARN] render runtime patches skipped: {patch_exc}", flush=True)

app = module.app

# Keep the production menu usable even if the preview-only route patch is skipped.
# We fall back to the existing analytics endpoint so url_for('admin_company_sales_analytics')
# continues to resolve on Render.
if "admin_company_sales_analytics" not in app.view_functions and "admin_analytics" in app.view_functions:
    app.add_url_rule(
        "/admin/analytics/company",
        endpoint="admin_company_sales_analytics",
        view_func=app.view_functions["admin_analytics"],
        methods=["GET", "POST"],
    )
