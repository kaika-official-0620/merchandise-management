# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from flask import abort, flash, has_request_context, jsonify, redirect, request, send_from_directory, url_for
from werkzeug.middleware.proxy_fix import ProxyFix


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

try:
    from kaika_business_flow_patch_20260611 import apply as apply_business_flow_patch

    setattr(module, "_kaika_business_flow_patch_20260611_applied", False)
    apply_business_flow_patch(module)
except Exception as patch_exc:
    print(f"[WARN] kaika business flow patch skipped: {patch_exc}", flush=True)

app = module.app
PREVIEW_CLEAN_DIR = REPO_DIR / ".preview-site-documents-clean"
PREVIEW_V4_DIR = REPO_DIR / ".preview-site-documents-v4"
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)

login_required_decorator = getattr(module, "login_required", None)
admin_required_decorator = getattr(module, "admin_required", None)

DEFAULT_PRIMARY_DOMAIN = "stock.kaika-potential.co.jp"
PRIMARY_DOMAIN = (os.environ.get("PRIMARY_DOMAIN") or os.environ.get("APP_DOMAIN") or DEFAULT_PRIMARY_DOMAIN).strip().lower()
PRIMARY_SCHEME = (os.environ.get("PRIMARY_SCHEME") or "https").strip().lower() or "https"
PRIMARY_DOMAIN_REDIRECT = (os.environ.get("PRIMARY_DOMAIN_REDIRECT") or "1").strip().lower() in {"1", "true", "yes", "on"}
RENDER_EXTERNAL_HOSTNAME = (os.environ.get("RENDER_EXTERNAL_HOSTNAME") or "").strip().lower()

if PRIMARY_DOMAIN:
    app.config["PREFERRED_URL_SCHEME"] = PRIMARY_SCHEME


def normalize_request_host(hostname: str) -> str:
    return (hostname or "").split(":", 1)[0].strip().lower()


def build_primary_domain_url() -> str:
    parsed = urlsplit(request.url)
    return urlunsplit((PRIMARY_SCHEME, PRIMARY_DOMAIN, parsed.path, parsed.query, ""))


def should_redirect_to_primary_domain() -> bool:
    if not PRIMARY_DOMAIN or not PRIMARY_DOMAIN_REDIRECT or not has_request_context():
        return False

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        return False

    if request.path == "/healthz":
        return False

    current_host = normalize_request_host(request.host)
    if not current_host or current_host == PRIMARY_DOMAIN:
        return False

    if current_host in {"localhost", "127.0.0.1"}:
        return False

    trusted_hosts = {normalize_request_host(RENDER_EXTERNAL_HOSTNAME)}
    trusted_hosts.discard("")
    return current_host.endswith(".onrender.com") or current_host in trusted_hosts


@app.before_request
def render_enforce_primary_domain():
    if should_redirect_to_primary_domain():
        return redirect(build_primary_domain_url(), code=301)
    return None


if "healthz" not in app.view_functions:
    @app.route("/healthz")
    def healthz():
        try:
            if getattr(module, "DATABASE_URL", None):
                conn = module.get_db()
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1")
                    cur.fetchone()
                finally:
                    conn.close()
                database_backend = "postgres"
            else:
                import sqlite3

                conn = sqlite3.connect(module.DATABASE)
                try:
                    cur = conn.cursor()
                    cur.execute("SELECT 1")
                    cur.fetchone()
                finally:
                    conn.close()
                database_backend = "sqlite"

            return jsonify(
                {
                    "status": "ok",
                    "database": database_backend,
                    "primary_domain": PRIMARY_DOMAIN or None,
                }
            ), 200
        except Exception as exc:
            print(f"[HEALTH] health check failed: {exc}", flush=True)
            return jsonify({"status": "error"}), 503

def add_endpoint_fallback(endpoint: str, rule: str, target_endpoint: str, methods):
    if endpoint in app.view_functions or target_endpoint not in app.view_functions:
        return
    app.add_url_rule(
        rule,
        endpoint=endpoint,
        view_func=app.view_functions[target_endpoint],
        methods=methods,
    )


def harden_endpoint_auth(endpoint: str, *decorators):
    view = app.view_functions.get(endpoint)
    if not callable(view):
        return

    wrapped = view
    for decorator in reversed(decorators):
        if callable(decorator):
            wrapped = decorator(wrapped)

    wrapped._render_auth_hardened = True
    app.view_functions[endpoint] = wrapped


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
add_endpoint_fallback(
    "admin_shipping_requests",
    "/admin/sale-requests/shipping",
    "admin_sale_requests",
    ["GET"],
)
add_endpoint_fallback(
    "admin_completion_requests",
    "/admin/sale-requests/completion",
    "admin_sale_requests",
    ["GET"],
)
add_endpoint_fallback(
    "admin_analytics_kaika_sales",
    "/admin/analytics/kaika/sales",
    "admin_analytics_kaika",
    ["GET"],
)
add_endpoint_fallback(
    "admin_line_broadcast_v2",
    "/admin/line-v2/broadcast",
    "admin_line_broadcast",
    ["POST"],
)
add_endpoint_fallback(
    "admin_line_scheduled_v2",
    "/admin/line-v2/scheduled",
    "admin_line_scheduled",
    ["GET", "POST"],
)


for endpoint in [
    "user_home",
    "documents",
    "profile",
    "reports",
    "long_term_items",
]:
    harden_endpoint_auth(endpoint, login_required_decorator)


for endpoint in [
    "admin_dashboard",
    "admin_documents_dashboard",
    "admin_documents_history",
    "admin_line_dashboard_v2",
    "admin_shipping_requests",
    "admin_completion_requests",
    "admin_proxy_service_history",
]:
    harden_endpoint_auth(endpoint, login_required_decorator, admin_required_decorator)


def redirect_line_dashboard_with_notice(message: str):
    flash(message, "info")
    return redirect(url_for("admin_line_dashboard_v2"))


if "admin_line_scheduled_edit_v2" not in app.view_functions and "admin_line_scheduled_edit" in app.view_functions:
    @app.route("/admin/line-v2/scheduled/<int:message_id>/edit", endpoint="admin_line_scheduled_edit_v2", methods=["GET", "POST"])
    def admin_line_scheduled_edit_v2(message_id: int):
        return app.view_functions["admin_line_scheduled_edit"](message_id)


if "admin_line_scheduled_delete_v2" not in app.view_functions and "admin_line_scheduled_delete" in app.view_functions:
    @app.route("/admin/line-v2/scheduled/<int:message_id>/delete", endpoint="admin_line_scheduled_delete_v2", methods=["POST"])
    def admin_line_scheduled_delete_v2(message_id: int):
        return app.view_functions["admin_line_scheduled_delete"](message_id)


if "admin_line_account_new" not in app.view_functions:
    @app.route("/admin/line-v2/accounts/new", endpoint="admin_line_account_new", methods=["GET"])
    def admin_line_account_new():
        return redirect_line_dashboard_with_notice("LINEアカウント追加は現行画面では未対応です。")


if "admin_line_account_edit" not in app.view_functions:
    @app.route("/admin/line-v2/accounts/<int:account_id>/edit", endpoint="admin_line_account_edit", methods=["GET"])
    def admin_line_account_edit(account_id: int):
        return redirect_line_dashboard_with_notice("LINEアカウント編集は現行画面では未対応です。")


if "admin_line_account_set_default" not in app.view_functions:
    @app.route("/admin/line-v2/accounts/<int:account_id>/default", endpoint="admin_line_account_set_default", methods=["POST"])
    def admin_line_account_set_default(account_id: int):
        return redirect_line_dashboard_with_notice("既定アカウント切り替えは現行画面では未対応です。")


if "admin_line_account_delete" not in app.view_functions:
    @app.route("/admin/line-v2/accounts/<int:account_id>/delete", endpoint="admin_line_account_delete", methods=["POST"])
    def admin_line_account_delete(account_id: int):
        return redirect_line_dashboard_with_notice("LINEアカウント削除は現行画面では未対応です。")


if "public_proxy_service_history" not in app.view_functions and "public_proxy_service_list" in app.view_functions:
    @app.route("/proxy-service/history", endpoint="public_proxy_service_history", methods=["GET"])
    def public_proxy_service_history():
        return redirect(url_for("public_proxy_service_list"), code=302)


def ensure_template_helper(name: str, func):
    setattr(module, name, func)
    app.jinja_env.globals[name] = func


pending_sales_agency_helper = getattr(module, "get_pending_sales_agency_count_by_service", None)
if not callable(pending_sales_agency_helper):
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
    pending_sales_agency_helper = get_pending_sales_agency_count_by_service

ensure_template_helper(
    "get_pending_sales_agency_count_by_service",
    pending_sales_agency_helper,
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


def _preview_login_wrapped(view_func):
    wrapped = view_func
    login_required = getattr(module, "login_required", None)
    admin_required = getattr(module, "admin_required", None)
    if callable(login_required):
        wrapped = login_required(wrapped)
    if callable(admin_required):
        wrapped = admin_required(wrapped)
    return wrapped


def _active_preview_root() -> tuple[Path | None, str]:
    if PREVIEW_CLEAN_DIR.exists():
        return PREVIEW_CLEAN_DIR, "documents_v2_index.html"
    if PREVIEW_V4_DIR.exists():
        return PREVIEW_V4_DIR, "home.html"
    return None, ""


def _safe_preview_target(root: Path, page: str) -> Path:
    target = (root / page).resolve()
    root = root.resolve()
    if root not in target.parents and target != root:
        raise FileNotFoundError(page)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(page)
    return target


PREVIEW_ROOT, PREVIEW_ENTRY = _active_preview_root()

if PREVIEW_ROOT:
    @_preview_login_wrapped
    def admin_documents_v4_preview():
        return redirect(
            url_for("admin_documents_v4_preview_page", page=PREVIEW_ENTRY),
            code=302,
        )

    @_preview_login_wrapped
    def admin_documents_v4_preview_page(page: str):
        try:
            target = _safe_preview_target(PREVIEW_ROOT, page)
        except FileNotFoundError:
            abort(404)
        return send_from_directory(str(target.parent), target.name)

    if "admin_documents_v4_preview" not in app.view_functions:
        app.add_url_rule(
            "/admin/documents-v4-preview",
            endpoint="admin_documents_v4_preview",
            view_func=admin_documents_v4_preview,
            methods=["GET"],
        )
        app.add_url_rule(
            "/admin/documents-v4-preview/",
            endpoint="admin_documents_v4_preview_slash",
            view_func=admin_documents_v4_preview,
            methods=["GET"],
        )
    if "admin_documents_v4_preview_page" not in app.view_functions:
        app.add_url_rule(
            "/admin/documents-v4-preview/<path:page>",
            endpoint="admin_documents_v4_preview_page",
            view_func=admin_documents_v4_preview_page,
            methods=["GET"],
        )
