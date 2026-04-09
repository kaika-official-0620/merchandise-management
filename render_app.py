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

def add_endpoint_fallback(endpoint: str, rule: str, target_endpoint: str, methods):
    if endpoint in app.view_functions or target_endpoint not in app.view_functions:
        return
    app.add_url_rule(
        rule,
        endpoint=endpoint,
        view_func=app.view_functions[target_endpoint],
        methods=methods,
    )


# Keep the production menu usable even if preview/runtime-only endpoints are absent on Render.
add_endpoint_fallback(
    "admin_company_sales_analytics",
    "/admin/analytics/company",
    "admin_analytics",
    ["GET", "POST"],
)
add_endpoint_fallback(
    "admin_documents_history",
    "/admin/documents/history",
    "admin_documents_dashboard",
    ["GET"],
)
add_endpoint_fallback(
    "admin_operator_users",
    "/admin/operators",
    "admin_users",
    ["GET"],
)
add_endpoint_fallback(
    "admin_operator_add_user",
    "/admin/operators/add",
    "admin_add_user",
    ["GET", "POST"],
)
add_endpoint_fallback(
    "admin_operator_edit_user",
    "/admin/operators/<int:id>/edit",
    "admin_edit_user",
    ["GET", "POST"],
)
add_endpoint_fallback(
    "admin_line_dashboard_v2",
    "/admin/line-v2",
    "admin_line_dashboard",
    ["GET"],
)


def ensure_template_helper(name: str, func):
    setattr(module, name, func)
    app.jinja_env.globals[name] = func


if not hasattr(module, "get_pending_sales_agency_count_by_service"):
    def get_pending_sales_agency_count_by_service(service_type: str):
        try:
            current_user = getattr(module, "current_user", None)
            if current_user is not None:
                if not getattr(current_user, "is_authenticated", False):
                    return 0
                is_admin = getattr(current_user, "is_admin", None)
                if callable(is_admin) and not is_admin():
                    return 0

            conn = module.get_db()
            cur = conn.cursor()
            if getattr(module, "DATABASE_URL", None):
                cur.execute(
                    "SELECT COUNT(*) FROM sales_agency_requests WHERE status = %s AND service_type = %s",
                    ("pending", service_type),
                )
            else:
                cur.execute(
                    "SELECT COUNT(*) FROM sales_agency_requests WHERE status = ? AND service_type = ?",
                    ("pending", service_type),
                )
            count = cur.fetchone()[0]
            cur.close()
            conn.close()
            return count
        except Exception as exc:
            print(f"get_pending_sales_agency_count_by_service error: {exc}", flush=True)
            return 0

ensure_template_helper(
    "get_pending_sales_agency_count_by_service",
    getattr(module, "get_pending_sales_agency_count_by_service"),
)


for helper_name in [
    "get_pending_sale_request_count",
    "get_pending_sale_request_count_by_type",
    "get_pending_sales_agency_count",
    "get_pending_disposal_count",
    "get_long_term_item_count",
    "get_unread_inquiry_count",
]:
    helper = getattr(module, helper_name, None)
    if callable(helper):
        ensure_template_helper(helper_name, helper)


@app.context_processor
def inject_render_sidebar_helpers():
    helper_names = [
        "get_pending_sale_request_count",
        "get_pending_sale_request_count_by_type",
        "get_pending_sales_agency_count",
        "get_pending_sales_agency_count_by_service",
        "get_pending_disposal_count",
        "get_long_term_item_count",
        "get_unread_inquiry_count",
    ]
    return {
        name: getattr(module, name)
        for name in helper_names
        if callable(getattr(module, name, None))
    }
