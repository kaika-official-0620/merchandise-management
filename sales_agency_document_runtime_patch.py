# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from flask import flash, redirect, render_template, request, url_for
from flask_login import current_user
from werkzeug.routing import BuildError


SALES_AGENCY_DOCUMENT_FLOW_STEPS = [
    ("user_request", "ユーザー依頼受領"),
    ("vendor_estimate", "業者向け見積依頼書"),
    ("vendor_statement", "業者買取明細書"),
    ("client_invoice", "ユーザー向け買取明細書"),
    ("completed", "処理完了"),
]


def prepare(module: Any) -> None:
    if getattr(module, "_sales_agency_document_prepare_done", False):
        return
    module._sales_agency_document_prepare_done = True

    DATABASE_URL = getattr(module, "DATABASE_URL", None)
    RealDictCursor = getattr(module, "RealDictCursor", None)
    get_db = module.get_db

    originals = {}
    for name in [
        "fetch_sales_agency_request_details",
        "fetch_admin_document_history_rows_v2",
        "admin_sales_agency_requests",
        "admin_sales_agency_request_detail",
        "admin_sales_agency_process",
        "sales_agency_my_requests",
        "admin_documents_dashboard",
        "admin_documents_history",
        "documents",
        "user_invoice_list",
        "user_invoice_view",
        "user_invoice_edit",
        "user_invoice_delete",
        "user_invoice_send",
        "user_mitsumori_list",
        "user_mitsumori_view",
        "user_mitsumori_edit",
        "user_mitsumori_delete",
        "user_kaitori_shoudaku_list",
        "user_kaitori_shoudaku_view",
        "user_kaitori_shoudaku_edit",
        "user_kaitori_shoudaku_delete",
        "admin_invoice_view",
        "admin_kaitori_add",
        "admin_kaitori_view",
        "admin_kaitori_shoudaku_add",
        "admin_mitsumori_add",
    ]:
        originals[name] = getattr(module, name, None)
    module._sales_agency_document_originals = originals

    def open_cursor():
        conn = get_db()
        if DATABASE_URL:
            cur = conn.cursor(cursor_factory=RealDictCursor)
        else:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
        return conn, cur

    def placeholder() -> str:
        return "%s" if DATABASE_URL else "?"

    def row_to_dict(row):
        return dict(row) if row else None

    def rows_to_dicts(rows):
        return [dict(row) for row in rows]

    def column_exists(cur, table_name: str, column_name: str) -> bool:
        if DATABASE_URL:
            cur.execute(
                """
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = %s AND column_name = %s
                LIMIT 1
                """,
                (table_name, column_name),
            )
            return cur.fetchone() is not None
        cur.execute(f"PRAGMA table_info({table_name})")
        for column in cur.fetchall():
            if (column["name"] if isinstance(column, sqlite3.Row) else column[1]) == column_name:
                return True
        return False

    def ensure_columns() -> None:
        specs = {
            "sales_agency_requests": [
                (
                    "vendor_mitsumori_id",
                    "ALTER TABLE sales_agency_requests ADD COLUMN vendor_mitsumori_id INTEGER",
                    "ALTER TABLE sales_agency_requests ADD COLUMN vendor_mitsumori_id INTEGER",
                ),
                (
                    "vendor_kaitori_shoudaku_id",
                    "ALTER TABLE sales_agency_requests ADD COLUMN vendor_kaitori_shoudaku_id INTEGER",
                    "ALTER TABLE sales_agency_requests ADD COLUMN vendor_kaitori_shoudaku_id INTEGER",
                ),
                (
                    "client_invoice_id",
                    "ALTER TABLE sales_agency_requests ADD COLUMN client_invoice_id INTEGER",
                    "ALTER TABLE sales_agency_requests ADD COLUMN client_invoice_id INTEGER",
                ),
                (
                    "internal_checked_at",
                    "ALTER TABLE sales_agency_requests ADD COLUMN internal_checked_at TIMESTAMP",
                    "ALTER TABLE sales_agency_requests ADD COLUMN internal_checked_at TEXT",
                ),
                (
                    "client_invoice_sent_at",
                    "ALTER TABLE sales_agency_requests ADD COLUMN client_invoice_sent_at TIMESTAMP",
                    "ALTER TABLE sales_agency_requests ADD COLUMN client_invoice_sent_at TEXT",
                ),
            ],
            "user_mitsumori": [
                (
                    "document_scope",
                    "ALTER TABLE user_mitsumori ADD COLUMN document_scope VARCHAR(40) DEFAULT 'client_incoming'",
                    "ALTER TABLE user_mitsumori ADD COLUMN document_scope TEXT DEFAULT 'client_incoming'",
                ),
                (
                    "sales_agency_request_id",
                    "ALTER TABLE user_mitsumori ADD COLUMN sales_agency_request_id INTEGER",
                    "ALTER TABLE user_mitsumori ADD COLUMN sales_agency_request_id INTEGER",
                ),
                (
                    "created_by_admin_id",
                    "ALTER TABLE user_mitsumori ADD COLUMN created_by_admin_id INTEGER",
                    "ALTER TABLE user_mitsumori ADD COLUMN created_by_admin_id INTEGER",
                ),
            ],
            "user_kaitori_shoudaku": [
                (
                    "document_scope",
                    "ALTER TABLE user_kaitori_shoudaku ADD COLUMN document_scope VARCHAR(40) DEFAULT 'client_incoming'",
                    "ALTER TABLE user_kaitori_shoudaku ADD COLUMN document_scope TEXT DEFAULT 'client_incoming'",
                ),
                (
                    "sales_agency_request_id",
                    "ALTER TABLE user_kaitori_shoudaku ADD COLUMN sales_agency_request_id INTEGER",
                    "ALTER TABLE user_kaitori_shoudaku ADD COLUMN sales_agency_request_id INTEGER",
                ),
            ],
            "admin_kaitori_shoudaku": [
                (
                    "document_scope",
                    "ALTER TABLE admin_kaitori_shoudaku ADD COLUMN document_scope VARCHAR(40) DEFAULT 'vendor_incoming'",
                    "ALTER TABLE admin_kaitori_shoudaku ADD COLUMN document_scope TEXT DEFAULT 'vendor_incoming'",
                ),
                (
                    "sales_agency_request_id",
                    "ALTER TABLE admin_kaitori_shoudaku ADD COLUMN sales_agency_request_id INTEGER",
                    "ALTER TABLE admin_kaitori_shoudaku ADD COLUMN sales_agency_request_id INTEGER",
                ),
                (
                    "source_mitsumori_id",
                    "ALTER TABLE admin_kaitori_shoudaku ADD COLUMN source_mitsumori_id INTEGER",
                    "ALTER TABLE admin_kaitori_shoudaku ADD COLUMN source_mitsumori_id INTEGER",
                ),
            ],
            "invoices": [
                (
                    "document_scope",
                    "ALTER TABLE invoices ADD COLUMN document_scope VARCHAR(40) DEFAULT 'client_outgoing'",
                    "ALTER TABLE invoices ADD COLUMN document_scope TEXT DEFAULT 'client_outgoing'",
                ),
                (
                    "sales_agency_request_id",
                    "ALTER TABLE invoices ADD COLUMN sales_agency_request_id INTEGER",
                    "ALTER TABLE invoices ADD COLUMN sales_agency_request_id INTEGER",
                ),
                (
                    "source_admin_kaitori_id",
                    "ALTER TABLE invoices ADD COLUMN source_admin_kaitori_id INTEGER",
                    "ALTER TABLE invoices ADD COLUMN source_admin_kaitori_id INTEGER",
                ),
            ],
        }

        conn, cur = open_cursor()
        try:
            for table_name, columns in specs.items():
                for column_name, pg_sql, sqlite_sql in columns:
                    if column_exists(cur, table_name, column_name):
                        continue
                    cur.execute(pg_sql if DATABASE_URL else sqlite_sql)

            cur.execute(
                """
                UPDATE sales_agency_requests
                SET vendor_mitsumori_id = created_mitsumori_id
                WHERE vendor_mitsumori_id IS NULL
                  AND created_mitsumori_id IS NOT NULL
                """
            )
            cur.execute(
                """
                UPDATE sales_agency_requests
                SET client_invoice_id = created_invoice_id
                WHERE client_invoice_id IS NULL
                  AND created_invoice_id IS NOT NULL
                """
            )

            cur.execute(
                """
                UPDATE user_mitsumori
                SET document_scope = CASE
                    WHEN document_no LIKE 'MT-%' THEN 'vendor_outgoing'
                    ELSE 'client_incoming'
                END
                WHERE document_scope IS NULL OR TRIM(document_scope) = ''
                """
            )
            cur.execute(
                """
                UPDATE user_kaitori_shoudaku
                SET document_scope = 'client_incoming'
                WHERE document_scope IS NULL OR TRIM(document_scope) = ''
                """
            )
            cur.execute(
                """
                UPDATE admin_kaitori_shoudaku
                SET document_scope = 'vendor_incoming'
                WHERE document_scope IS NULL OR TRIM(document_scope) = ''
                """
            )
            cur.execute(
                """
                UPDATE user_mitsumori
                SET sales_agency_request_id = (
                    SELECT sar.id
                    FROM sales_agency_requests sar
                    WHERE sar.vendor_mitsumori_id = user_mitsumori.id
                    LIMIT 1
                )
                WHERE sales_agency_request_id IS NULL
                  AND EXISTS (
                    SELECT 1
                    FROM sales_agency_requests sar
                    WHERE sar.vendor_mitsumori_id = user_mitsumori.id
                  )
                """
            )
            cur.execute(
                """
                UPDATE admin_kaitori_shoudaku
                SET sales_agency_request_id = (
                    SELECT sar.id
                    FROM sales_agency_requests sar
                    WHERE sar.vendor_kaitori_shoudaku_id = admin_kaitori_shoudaku.id
                    LIMIT 1
                )
                WHERE sales_agency_request_id IS NULL
                  AND EXISTS (
                    SELECT 1
                    FROM sales_agency_requests sar
                    WHERE sar.vendor_kaitori_shoudaku_id = admin_kaitori_shoudaku.id
                  )
                """
            )
            cur.execute(
                """
                UPDATE invoices
                SET document_scope = 'client_outgoing'
                WHERE (document_scope IS NULL OR TRIM(document_scope) = '')
                  AND invoice_no LIKE 'KT-%'
                  AND EXISTS (
                    SELECT 1
                    FROM sales_agency_requests sar
                    WHERE sar.client_invoice_id = invoices.id
                  )
                """
            )
            cur.execute(
                """
                UPDATE invoices
                SET sales_agency_request_id = (
                    SELECT sar.id
                    FROM sales_agency_requests sar
                    WHERE sar.client_invoice_id = invoices.id
                    LIMIT 1
                )
                WHERE sales_agency_request_id IS NULL
                  AND EXISTS (
                    SELECT 1
                    FROM sales_agency_requests sar
                    WHERE sar.client_invoice_id = invoices.id
                  )
                """
            )
            conn.commit()
        finally:
            cur.close()
            conn.close()

    def generate_document_no(prefix: str, table_name: str, column_name: str) -> str:
        conn, cur = open_cursor()
        try:
            day_key = datetime.now().strftime("%Y%m%d")
            like_value = f"{prefix}-{day_key}-%"
            sql = (
                f"SELECT COUNT(*) AS cnt FROM {table_name} WHERE {column_name} LIKE %s"
                if DATABASE_URL
                else f"SELECT COUNT(*) AS cnt FROM {table_name} WHERE {column_name} LIKE ?"
            )
            cur.execute(sql, (like_value,))
            row = cur.fetchone()
            count = (row["cnt"] if isinstance(row, dict) else row[0]) if row else 0
            return f"{prefix}-{day_key}-{count + 1:03d}"
        finally:
            cur.close()
            conn.close()

    def generate_admin_mitsumori_document_no() -> str:
        return generate_document_no("MT", "user_mitsumori", "document_no")

    def generate_admin_kaitori_document_no() -> str:
        return generate_document_no("KT", "invoices", "invoice_no")

    def build_mitsumori_items_from_form():
        item_names = request.form.getlist("item_name[]")
        merchandise_ids = request.form.getlist("merchandise_id[]")
        quantities = request.form.getlist("quantity[]")
        units = request.form.getlist("unit[]")
        unit_prices = request.form.getlist("unit_price[]")

        items = []
        total_amount = 0
        for index, item_name in enumerate(item_names, start=1):
            item_name = (item_name or "").strip()
            if not item_name:
                continue
            try:
                quantity = int(quantities[index - 1] or 1)
            except (TypeError, ValueError):
                quantity = 1
            try:
                unit_price = int(float(unit_prices[index - 1] or 0))
            except (TypeError, ValueError):
                unit_price = 0
            amount = quantity * unit_price
            total_amount += amount
            merchandise_id_raw = merchandise_ids[index - 1] if index - 1 < len(merchandise_ids) else ""
            try:
                merchandise_id = int(merchandise_id_raw) if merchandise_id_raw not in ("", None) else None
            except (TypeError, ValueError):
                merchandise_id = None
            items.append(
                {
                    "item_no": len(items) + 1,
                    "item_name": item_name,
                    "merchandise_id": merchandise_id,
                    "quantity": quantity,
                    "unit": (units[index - 1] if index - 1 < len(units) else "") or "点",
                    "unit_price": unit_price,
                    "amount": amount,
                }
            )
        return items, total_amount

    def fetch_sales_agency_request_source(request_id: int, waiting_only: bool = False):
        conn, cur = open_cursor()
        try:
            mark = placeholder()
            cur.execute(
                f"""
                SELECT sar.*, u.display_name AS user_name, u.username,
                       p.display_name AS processor_name
                FROM sales_agency_requests sar
                JOIN users u ON sar.user_id = u.id
                LEFT JOIN users p ON sar.processed_by = p.id
                WHERE sar.id = {mark}
                """,
                (request_id,),
            )
            request_row = row_to_dict(cur.fetchone())
            if not request_row:
                return None, []

            has_appraisal_status = column_exists(cur, "merchandise", "appraisal_status")
            cur.execute(
                f"""
                SELECT m.*
                FROM sales_agency_request_items sari
                JOIN merchandise m ON sari.merchandise_id = m.id
                WHERE sari.request_id = {mark}
                ORDER BY m.id DESC
                """,
                (request_id,),
            )
            items = []
            pending_appraisal_count = 0
            appraisal_label = getattr(module, "get_sales_agency_appraisal_label", lambda value: value or "")
            for item in rows_to_dicts(cur.fetchall()):
                appraisal_status = (item.get("appraisal_status") if has_appraisal_status else "") or ""
                if not appraisal_status and request_row.get("status") in {"approved", "appraising", "inspecting"}:
                    appraisal_status = "waiting"
                item["appraisal_status"] = appraisal_status
                item["appraisal_status_label"] = appraisal_label(appraisal_status)
                item["detail_url"] = url_for("view_item", id=item["id"])
                item["has_id_doc"] = bool(item.get("id_document_path"))
                item["has_consent"] = bool(item.get("consent_form_path"))

                is_waiting_item = appraisal_status in {"", "waiting", "inspecting"}
                if is_waiting_item:
                    pending_appraisal_count += 1
                if waiting_only and not is_waiting_item:
                    continue
                items.append(item)

            request_row["service_name"] = getattr(module, "get_sales_agency_service_name")(request_row.get("service_type"))
            request_row["client_name"] = request_row.get("user_name") or request_row.get("username") or f"ID:{request_row.get('user_id')}"
            request_row["status_label"] = getattr(module, "get_sales_agency_status_label")(request_row.get("status"), viewer="admin")
            request_row["client_status_label"] = getattr(module, "get_sales_agency_status_label")(request_row.get("status"), viewer="client")
            request_row["pending_appraisal_count"] = pending_appraisal_count
            request_row["merchandise_items"] = items
            return request_row, items
        finally:
            cur.close()
            conn.close()

    module._sales_agency_document_open_cursor = open_cursor
    module._sales_agency_document_row_to_dict = row_to_dict
    module._sales_agency_document_rows_to_dicts = rows_to_dicts
    module._sales_agency_document_column_exists = column_exists
    module.generate_admin_mitsumori_document_no = generate_admin_mitsumori_document_no
    module.generate_admin_kaitori_document_no = generate_admin_kaitori_document_no
    module.build_mitsumori_items_from_form = build_mitsumori_items_from_form
    module.fetch_sales_agency_request_source = fetch_sales_agency_request_source

    ensure_columns()


def apply(module: Any) -> None:
    if getattr(module, "_sales_agency_document_patch_applied", False):
        return
    module._sales_agency_document_patch_applied = True

    prepare(module)

    DATABASE_URL = getattr(module, "DATABASE_URL", None)
    app = module.app
    originals = getattr(module, "_sales_agency_document_originals", {})
    open_cursor = module._sales_agency_document_open_cursor
    row_to_dict = module._sales_agency_document_row_to_dict
    rows_to_dicts = module._sales_agency_document_rows_to_dicts
    column_exists = module._sales_agency_document_column_exists
    fetch_request_source = module.fetch_sales_agency_request_source
    original_fetch_request_details = originals.get("fetch_sales_agency_request_details") or module.fetch_sales_agency_request_details
    format_date = getattr(module, "document_format_date", lambda value: str(value or "")[:10])
    document_status_label = getattr(module, "document_status_label", lambda _kind, status: status or "-")
    service_name = getattr(module, "get_sales_agency_service_name")
    sales_status_label = getattr(module, "get_sales_agency_status_label")
    sales_action_labels = getattr(module, "SALES_AGENCY_ACTION_LABELS", {})
    service_types = getattr(
        module,
        "SALES_AGENCY_SERVICE_TYPES",
        {
            "wholesale": "業者卸販売サービス",
            "simultaneous": "同時出品サービス",
            "auction": "業者オークション出品",
        },
    )

    def mark():
        return "%s" if DATABASE_URL else "?"

    def safe_url(endpoint: str | None, **values):
        if not endpoint or endpoint not in app.view_functions:
            return None
        try:
            return url_for(endpoint, **values)
        except (BuildError, KeyError, TypeError, ValueError):
            return None

    def to_timestamp_text(value):
        if not value:
            return ""
        if hasattr(value, "strftime"):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return str(value)

    def infer_document_flow(request_row: dict) -> tuple[str, str, list[dict[str, str]]]:
        if request_row.get("service_type") != "wholesale":
            current_key = request_row.get("status") or "pending"
            current_label = request_row.get("status_label") or sales_status_label(current_key, viewer="admin")
            steps = []
            linear_steps = [
                ("pending", "申請受領"),
                ("approved", "認証済み"),
                ("appraising", "査定中"),
                ("inspecting", "検品中"),
                ("completed", "処理完了"),
            ]
            current_index = {key: index for index, (key, _) in enumerate(linear_steps)}.get(current_key, -1)
            for index, (step_key, step_label) in enumerate(linear_steps):
                state = "pending"
                if current_key == step_key:
                    state = "current"
                elif current_index > index:
                    state = "done"
                steps.append({"key": step_key, "label": step_label, "state": state})
            if current_key == "rejected":
                steps.append({"key": "rejected", "label": "却下", "state": "current"})
            return current_key, current_label, steps

        if request_row.get("client_invoice_id"):
            current_key = "client_invoice"
            current_label = "ユーザー向け買取明細書作成済み"
        elif request_row.get("vendor_kaitori_shoudaku_id"):
            current_key = "vendor_statement"
            current_label = "業者買取明細書受領済み"
        elif request_row.get("vendor_mitsumori_id"):
            current_key = "vendor_estimate"
            current_label = "業者向け見積依頼書作成済み"
        else:
            current_key = "user_request"
            current_label = "ユーザー依頼受付"

        if request_row.get("status") == "completed":
            current_key = "completed"
            current_label = "ユーザー送付完了"
        elif request_row.get("status") == "rejected":
            current_key = "rejected"
            current_label = "却下"

        order = {key: index for index, (key, _label) in enumerate(SALES_AGENCY_DOCUMENT_FLOW_STEPS)}
        current_index = order.get(current_key, -1)
        steps = []
        for index, (step_key, step_label) in enumerate(SALES_AGENCY_DOCUMENT_FLOW_STEPS):
            state = "pending"
            if current_key == step_key:
                state = "current"
            elif current_index > index:
                state = "done"
            steps.append({"key": step_key, "label": step_label, "state": state})
        if current_key == "rejected":
            steps.append({"key": "rejected", "label": "却下", "state": "current"})
        return current_key, current_label, steps

    def fetch_sales_agency_request_details_v2(request_id: int, viewer: str = "admin"):
        request_row, merchandise_items = original_fetch_request_details(request_id, viewer=viewer)
        if not request_row:
            return None, []

        request_row["vendor_mitsumori_id"] = request_row.get("vendor_mitsumori_id") or request_row.get("created_mitsumori_id")
        request_row["vendor_kaitori_shoudaku_id"] = request_row.get("vendor_kaitori_shoudaku_id")
        request_row["client_invoice_id"] = request_row.get("client_invoice_id") or request_row.get("created_invoice_id")
        request_row["created_mitsumori_id"] = request_row.get("vendor_mitsumori_id")
        request_row["created_invoice_id"] = request_row.get("client_invoice_id")

        request_row["request_can_create_vendor_estimate"] = (
            request_row.get("service_type") == "wholesale"
            and request_row.get("status") in {"approved", "appraising", "inspecting"}
            and not request_row.get("vendor_mitsumori_id")
        )
        request_row["request_can_register_vendor_kaitori"] = (
            request_row.get("service_type") == "wholesale"
            and bool(request_row.get("vendor_mitsumori_id"))
            and not request_row.get("vendor_kaitori_shoudaku_id")
        )
        request_row["request_can_create_client_invoice"] = (
            request_row.get("service_type") == "wholesale"
            and bool(request_row.get("vendor_kaitori_shoudaku_id"))
            and not request_row.get("client_invoice_id")
        )
        request_row["request_can_create_documents"] = request_row["request_can_create_vendor_estimate"]

        flow_key, flow_label, flow_steps = infer_document_flow(request_row)
        request_row["document_flow_key"] = flow_key
        request_row["document_flow_label"] = flow_label
        request_row["document_flow_steps"] = flow_steps
        request_row["client_status_label"] = sales_status_label(request_row.get("status"), viewer="client")
        request_row["status_label"] = sales_status_label(request_row.get("status"), viewer=viewer)
        request_row["merchandise_items"] = merchandise_items
        return request_row, merchandise_items

    def fetch_admin_document_history_rows_v3():
        rows = []
        conn, cur = open_cursor()
        try:
            request_map = {}
            request_detail_cache = {}
            cur.execute(
                """
                SELECT id, service_type, vendor_mitsumori_id, vendor_kaitori_shoudaku_id, client_invoice_id
                FROM sales_agency_requests
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                request_map[row["id"]] = row

            vendor_mitsumori_map = {
                row["vendor_mitsumori_id"]: row for row in request_map.values() if row.get("vendor_mitsumori_id")
            }
            vendor_kaitori_map = {
                row["vendor_kaitori_shoudaku_id"]: row for row in request_map.values() if row.get("vendor_kaitori_shoudaku_id")
            }
            client_invoice_map = {
                row["client_invoice_id"]: row for row in request_map.values() if row.get("client_invoice_id")
            }

            def get_request_detail(request_id):
                if not request_id:
                    return {}
                if request_id not in request_detail_cache:
                    request_row, _ = fetch_sales_agency_request_details_v2(request_id, viewer="admin")
                    request_detail_cache[request_id] = request_row or {}
                return request_detail_cache[request_id]

            def append_row(payload):
                request_id = payload.get("request_id")
                request_detail = get_request_detail(request_id)
                if request_detail:
                    payload.setdefault("request_status", request_detail.get("status") or "")
                    payload.setdefault("request_status_label", request_detail.get("status_label") or "")
                    payload.setdefault("request_flow_label", request_detail.get("document_flow_label") or "")
                    if not payload.get("client_name") or payload.get("client_name") == "-":
                        payload["client_name"] = request_detail.get("client_name") or payload.get("client_name") or "-"
                    if not payload.get("service_type"):
                        payload["service_type"] = request_detail.get("service_type") or ""
                    if not payload.get("service_name"):
                        payload["service_name"] = request_detail.get("service_name") or service_name(payload.get("service_type") or "")
                else:
                    payload.setdefault("request_status", "")
                    payload.setdefault("request_status_label", "")
                    payload.setdefault("request_flow_label", "")

                payload["detail_url"] = safe_url(payload.get("detail_endpoint"), id=payload.get("id"))
                payload["request_url"] = safe_url("admin_sales_agency_request_detail", id=request_id) if request_id else None
                rows.append(payload)

            cur.execute(
                """
                SELECT s.id, s.document_no, s.issue_date, s.total_amount, s.status, s.created_at,
                       s.recipient_name, u.display_name AS client_name, u.username
                FROM shikiriosho s
                LEFT JOIN users u ON s.recipient_id = u.id
                ORDER BY COALESCE(s.issue_date, s.created_at) DESC, s.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                append_row(
                    {
                        "kind": "shikiriosho",
                        "document_key": "shikiriosho",
                        "id": row["id"],
                        "request_id": None,
                        "document_type": "仕切書",
                        "document_no": row.get("document_no") or "-",
                        "client_name": row.get("client_name") or row.get("recipient_name") or row.get("username") or "未設定",
                        "service_type": "",
                        "service_name": "",
                        "issue_date": format_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": document_status_label("shikiriosho", row.get("status")),
                        "direction_key": "client_outgoing",
                        "direction_label": "開花→クライアント",
                        "subject": "",
                        "notes": "",
                        "detail_endpoint": "admin_shikiriosho_view",
                        "sort_key": str(row.get("issue_date") or row.get("created_at") or ""),
                    }
                )

            cur.execute(
                """
                SELECT m.id, m.document_no, m.issue_date, m.total_amount, m.status, m.created_at,
                       m.subject, m.notes, m.company_name, m.document_scope, m.sales_agency_request_id,
                       u.display_name AS client_name, u.username
                FROM user_mitsumori m
                LEFT JOIN users u ON m.user_id = u.id
                ORDER BY COALESCE(m.issue_date, m.created_at) DESC, m.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                scope = row.get("document_scope") or ("vendor_outgoing" if (row.get("document_no") or "").startswith("MT-") else "client_incoming")
                request_info = request_map.get(row.get("sales_agency_request_id")) or vendor_mitsumori_map.get(row["id"]) or {}
                is_vendor_outgoing = scope == "vendor_outgoing"
                append_row(
                    {
                        "kind": "user_mitsumori",
                        "document_key": "vendor_estimate" if is_vendor_outgoing else "client_mitsumori",
                        "id": row["id"],
                        "request_id": request_info.get("id"),
                        "document_type": "業者向け見積依頼書" if is_vendor_outgoing else "見積り依頼書",
                        "document_no": row.get("document_no") or "-",
                        "client_name": row.get("client_name") or row.get("username") or "未設定",
                        "service_type": request_info.get("service_type") or "",
                        "service_name": service_name(request_info.get("service_type") or ""),
                        "issue_date": format_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": document_status_label("user_mitsumori", row.get("status")),
                        "direction_key": "vendor_outgoing" if is_vendor_outgoing else "client_incoming",
                        "direction_label": "開花→業者" if is_vendor_outgoing else "クライアント→開花",
                        "subject": row.get("subject") or row.get("company_name") or "",
                        "notes": row.get("notes") or "",
                        "detail_endpoint": "admin_mitsumori_view" if is_vendor_outgoing else "admin_user_mitsumori_view",
                        "sort_key": str(row.get("issue_date") or row.get("created_at") or ""),
                    }
                )

            cur.execute(
                """
                SELECT k.id, k.document_no, k.issue_date, k.total_amount, k.status, k.created_at,
                       k.notes, k.customer_name, k.document_scope, k.sales_agency_request_id,
                       u.display_name AS client_name, u.username
                FROM user_kaitori_shoudaku k
                LEFT JOIN users u ON k.user_id = u.id
                ORDER BY COALESCE(k.issue_date, k.created_at) DESC, k.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                request_info = request_map.get(row.get("sales_agency_request_id")) or {}
                append_row(
                    {
                        "kind": "user_kaitori_shoudaku",
                        "document_key": "client_kaitori_request",
                        "id": row["id"],
                        "request_id": request_info.get("id"),
                        "document_type": "買取依頼書",
                        "document_no": row.get("document_no") or "-",
                        "client_name": row.get("client_name") or row.get("username") or "未設定",
                        "service_type": request_info.get("service_type") or "",
                        "service_name": service_name(request_info.get("service_type") or ""),
                        "issue_date": format_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": document_status_label("user_kaitori_shoudaku", row.get("status")),
                        "direction_key": "client_incoming",
                        "direction_label": "クライアント→開花",
                        "subject": row.get("customer_name") or "",
                        "notes": row.get("notes") or "",
                        "detail_endpoint": "admin_user_kaitori_shoudaku_view",
                        "sort_key": str(row.get("issue_date") or row.get("created_at") or ""),
                    }
                )

            cur.execute(
                """
                SELECT k.id, k.document_no, k.issue_date, k.total_amount, k.status, k.created_at,
                       k.notes, k.company_name, k.document_scope, k.sales_agency_request_id
                FROM admin_kaitori_shoudaku k
                ORDER BY COALESCE(k.issue_date, k.created_at) DESC, k.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                request_info = request_map.get(row.get("sales_agency_request_id")) or vendor_kaitori_map.get(row["id"]) or {}
                append_row(
                    {
                        "kind": "admin_kaitori_shoudaku",
                        "document_key": "vendor_statement",
                        "id": row["id"],
                        "request_id": request_info.get("id"),
                        "document_type": "業者買取明細書",
                        "document_no": row.get("document_no") or "-",
                        "client_name": "-",
                        "service_type": request_info.get("service_type") or "",
                        "service_name": service_name(request_info.get("service_type") or ""),
                        "issue_date": format_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": document_status_label("admin_kaitori_shoudaku", row.get("status")),
                        "direction_key": "vendor_incoming",
                        "direction_label": "業者→開花",
                        "subject": row.get("company_name") or "",
                        "notes": row.get("notes") or "",
                        "detail_endpoint": "admin_kaitori_shoudaku_view",
                        "sort_key": str(row.get("issue_date") or row.get("created_at") or ""),
                    }
                )

            cur.execute(
                """
                SELECT i.id, i.invoice_no, i.issue_date, i.total_amount, i.status, i.created_at,
                       i.notes, i.recipient_name, i.document_scope, i.sales_agency_request_id,
                       u.display_name AS client_name, u.username
                FROM invoices i
                LEFT JOIN users u ON i.sender_id = u.id
                WHERE i.invoice_no LIKE 'KT-%'
                ORDER BY COALESCE(i.issue_date, i.created_at) DESC, i.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                request_info = request_map.get(row.get("sales_agency_request_id")) or client_invoice_map.get(row["id"]) or {}
                append_row(
                    {
                        "kind": "invoice",
                        "document_key": "client_invoice",
                        "id": row["id"],
                        "request_id": request_info.get("id"),
                        "document_type": "ユーザー向け買取明細書",
                        "document_no": row.get("invoice_no") or "-",
                        "client_name": row.get("client_name") or row.get("recipient_name") or row.get("username") or "未設定",
                        "service_type": request_info.get("service_type") or "",
                        "service_name": service_name(request_info.get("service_type") or ""),
                        "issue_date": format_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": document_status_label("invoice", row.get("status")),
                        "direction_key": "client_outgoing",
                        "direction_label": "開花→クライアント",
                        "subject": "",
                        "notes": row.get("notes") or "",
                        "detail_endpoint": "admin_kaitori_view",
                        "sort_key": str(row.get("issue_date") or row.get("created_at") or ""),
                    }
                )

            admin_created_condition = (
                "COALESCE(k.is_admin_created, FALSE) = TRUE"
                if DATABASE_URL
                else "COALESCE(k.is_admin_created, 0) = 1"
            )
            cur.execute(
                f"""
                SELECT k.id, k.document_no, k.issue_date, k.total_amount, k.status, k.created_at
                FROM user_keisan k
                WHERE {admin_created_condition}
                ORDER BY COALESCE(k.issue_date, k.created_at) DESC, k.id DESC
                """
            )
            for row in rows_to_dicts(cur.fetchall()):
                append_row(
                    {
                        "kind": "user_keisan",
                        "document_key": "auction_keisan",
                        "id": row["id"],
                        "request_id": None,
                        "document_type": "オークション計算書",
                        "document_no": row.get("document_no") or "-",
                        "client_name": "-",
                        "service_type": "auction",
                        "service_name": service_name("auction"),
                        "issue_date": format_date(row.get("issue_date") or row.get("created_at")),
                        "total_amount": int(row.get("total_amount") or 0),
                        "status": row.get("status") or "",
                        "status_label": document_status_label("user_keisan", row.get("status")),
                        "direction_key": "client_outgoing",
                        "direction_label": "開花→クライアント",
                        "subject": "",
                        "notes": "",
                        "detail_endpoint": "admin_auction_keisan_view",
                        "sort_key": str(row.get("issue_date") or row.get("created_at") or ""),
                    }
                )

            rows.sort(key=lambda row: (row.get("sort_key") or "", row.get("id") or 0), reverse=True)
            return rows
        finally:
            cur.close()
            conn.close()

    def is_admin_generated_invoice(row: dict | None) -> bool:
        if not row:
            return False
        scope = (row.get("document_scope") or "").strip()
        return scope == "client_outgoing" or bool(row.get("source_admin_kaitori_id")) or bool(row.get("sales_agency_request_id"))

    def is_user_visible_invoice(row: dict | None) -> bool:
        if not row:
            return False
        if not is_admin_generated_invoice(row):
            return True
        return (row.get("status") or "").strip() != "draft"

    def is_admin_vendor_mitsumori(row: dict | None) -> bool:
        if not row:
            return False
        return (row.get("document_scope") or "").strip() == "vendor_outgoing"

    def is_visible_user_scope(row: dict | None, default_scope: str) -> bool:
        if not row:
            return False
        scope = (row.get("document_scope") or default_scope).strip() or default_scope
        return scope == default_scope

    def load_invoice_for_user(invoice_id: int):
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"SELECT * FROM invoices WHERE id = {mark()} AND sender_id = {mark()}",
                (invoice_id, current_user.id),
            )
            return rows_to_dicts(cur.fetchall())[:1]
        finally:
            cur.close()
            conn.close()

    def load_mitsumori_for_user(mitsumori_id: int):
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"SELECT * FROM user_mitsumori WHERE id = {mark()} AND user_id = {mark()}",
                (mitsumori_id, current_user.id),
            )
            return rows_to_dicts(cur.fetchall())[:1]
        finally:
            cur.close()
            conn.close()

    def load_kaitori_request_for_user(doc_id: int):
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"SELECT * FROM user_kaitori_shoudaku WHERE id = {mark()} AND user_id = {mark()}",
                (doc_id, current_user.id),
            )
            return rows_to_dicts(cur.fetchall())[:1]
        finally:
            cur.close()
            conn.close()

    def load_vendor_mitsumori_items(request_row: dict | None):
        if not request_row or not request_row.get("vendor_mitsumori_id"):
            return []
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT umi.item_no,
                       umi.item_name AS product_name,
                       umi.quantity,
                       umi.unit_price,
                       umi.amount,
                       umi.merchandise_id,
                       m.brand_name,
                       m.item_condition AS item_condition
                FROM user_mitsumori_items umi
                LEFT JOIN merchandise m ON umi.merchandise_id = m.id
                WHERE umi.mitsumori_id = {mark()}
                ORDER BY umi.item_no ASC, umi.id ASC
                """,
                (request_row["vendor_mitsumori_id"],),
            )
            return rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()

    def load_vendor_kaitori_context(request_row: dict | None):
        if not request_row or not request_row.get("vendor_kaitori_shoudaku_id"):
            return None, []
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"SELECT * FROM admin_kaitori_shoudaku WHERE id = {mark()}",
                (request_row["vendor_kaitori_shoudaku_id"],),
            )
            kaitori_row = row_to_dict(cur.fetchone())
            cur.execute(
                f"""
                SELECT item_no,
                       product_name,
                       brand_name,
                       condition AS item_condition,
                       quantity,
                       unit_price,
                       amount
                FROM admin_kaitori_shoudaku_items
                WHERE kaitori_shoudaku_id = {mark()}
                ORDER BY item_no ASC, id ASC
                """,
                (request_row["vendor_kaitori_shoudaku_id"],),
            )
            item_rows = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()

        merchandise_lookup = {}
        for item in request_row.get("merchandise_items") or []:
            item_name = (item.get("product_name") or "").strip()
            if item_name and item_name not in merchandise_lookup:
                merchandise_lookup[item_name] = item

        prepared_items = []
        for item in item_rows:
            matched_item = merchandise_lookup.get((item.get("product_name") or "").strip(), {})
            prepared_items.append(
                {
                    "item_no": item.get("item_no"),
                    "product_name": item.get("product_name") or "",
                    "brand_name": item.get("brand_name") or matched_item.get("brand_name") or "",
                    "purchase_date": matched_item.get("purchase_date") or "",
                    "item_condition": item.get("item_condition") or matched_item.get("item_condition") or "",
                    "amount": int(item.get("amount") or 0),
                    "merchandise_id": matched_item.get("id"),
                }
            )
        return kaitori_row, prepared_items

    def apply_admin_document_history_filters(rows, filters):
        doc_type = (filters.get("doc_type") or "all").strip()
        client = (filters.get("client") or "").strip().lower()
        status = (filters.get("status") or "all").strip()
        direction = (filters.get("direction") or "all").strip()
        service_type_filter = (filters.get("service_type") or "all").strip()
        date_from = (filters.get("date_from") or "").strip()
        date_to = (filters.get("date_to") or "").strip()
        keyword = (filters.get("keyword") or "").strip().lower()

        filtered_rows = []
        for row in rows:
            if doc_type != "all" and row.get("document_key") != doc_type:
                continue
            if status != "all" and row.get("status") != status:
                continue
            if direction != "all" and row.get("direction_key") != direction:
                continue
            if service_type_filter != "all" and (row.get("service_type") or "") != service_type_filter:
                continue
            if client and client not in (row.get("client_name") or "").lower():
                continue
            if date_from and (row.get("issue_date") or "") < date_from:
                continue
            if date_to and (row.get("issue_date") or "") > date_to:
                continue
            if keyword:
                haystack = " ".join(
                    [
                        row.get("document_no") or "",
                        row.get("client_name") or "",
                        row.get("document_type") or "",
                        row.get("service_name") or "",
                        row.get("subject") or "",
                        row.get("notes") or "",
                    ]
                ).lower()
                if keyword not in haystack:
                    continue
            filtered_rows.append(row)
        return filtered_rows

    def build_ongoing_request_rows(limit: int = 12):
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT id
                FROM sales_agency_requests
                WHERE status NOT IN ('completed', 'rejected')
                ORDER BY CASE status
                    WHEN 'pending' THEN 0
                    WHEN 'approved' THEN 1
                    WHEN 'appraising' THEN 2
                    WHEN 'inspecting' THEN 3
                    ELSE 9
                END, created_at DESC
                LIMIT {int(limit)}
                """
            )
            request_ids = [row["id"] if isinstance(row, dict) else row[0] for row in cur.fetchall()]
        finally:
            cur.close()
            conn.close()

        request_rows = []
        for request_id in request_ids:
            request_row, _ = fetch_sales_agency_request_details_v2(request_id, viewer="admin")
            if not request_row:
                continue
            request_row["created_date_label"] = format_date(request_row.get("created_at"))
            request_row["detail_url"] = safe_url("admin_sales_agency_request_detail", id=request_row.get("id"))
            request_row["box_url"] = safe_url(
                "admin_sales_agency_requests",
                service_type=request_row.get("service_type"),
            ) or safe_url("admin_sales_agency_requests")
            request_row["vendor_mitsumori_url"] = safe_url("admin_mitsumori_view", id=request_row.get("vendor_mitsumori_id")) if request_row.get("vendor_mitsumori_id") else None
            request_row["vendor_kaitori_url"] = safe_url("admin_kaitori_shoudaku_view", id=request_row.get("vendor_kaitori_shoudaku_id")) if request_row.get("vendor_kaitori_shoudaku_id") else None
            request_row["client_invoice_url"] = safe_url("admin_kaitori_view", id=request_row.get("client_invoice_id")) if request_row.get("client_invoice_id") else None
            request_row["create_vendor_estimate_url"] = safe_url("admin_mitsumori_add", request_id=request_row.get("id")) if request_row.get("request_can_create_vendor_estimate") else None
            request_row["create_vendor_kaitori_url"] = safe_url("admin_kaitori_shoudaku_add", request_id=request_row.get("id")) if request_row.get("request_can_register_vendor_kaitori") else None
            request_row["create_client_invoice_url"] = safe_url("admin_kaitori_add", request_id=request_row.get("id")) if request_row.get("request_can_create_client_invoice") else None
            if request_row.get("request_can_create_vendor_estimate"):
                request_row["next_document_label"] = "業者向け見積依頼書を作成"
            elif request_row.get("request_can_register_vendor_kaitori"):
                request_row["next_document_label"] = "業者買取明細書を登録"
            elif request_row.get("request_can_create_client_invoice"):
                request_row["next_document_label"] = "ユーザー向け買取明細書を確認・発行"
            else:
                request_row["next_document_label"] = "次の書類待ちまたは処理完了"
            request_rows.append(request_row)
        return request_rows

    def admin_documents_dashboard_v2():
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("アクセス権限がありません。", "error")
            return redirect(url_for("index"))

        history_rows = fetch_admin_document_history_rows_v3()
        selected_group = request.args.get("group", "all").strip() or "all"
        document_counts = {
            "client_incoming": 0,
            "vendor_estimate": 0,
            "vendor_statement": 0,
            "client_outgoing": 0,
        }
        document_type_counts = {
            "client_mitsumori": 0,
            "client_kaitori_request": 0,
            "vendor_estimate": 0,
            "vendor_statement": 0,
            "client_invoice": 0,
            "shikiriosho": 0,
            "auction_keisan": 0,
        }
        for row in history_rows:
            if row.get("direction_key") == "client_incoming":
                document_counts["client_incoming"] += 1
            elif row.get("document_key") == "vendor_estimate":
                document_counts["vendor_estimate"] += 1
            elif row.get("document_key") == "vendor_statement":
                document_counts["vendor_statement"] += 1
            elif row.get("direction_key") == "client_outgoing":
                document_counts["client_outgoing"] += 1
            if row.get("document_key") in document_type_counts:
                document_type_counts[row.get("document_key")] += 1

        def row_matches_dashboard_filters(row: dict[str, Any]) -> bool:
            return selected_group == "all" or row.get("direction_key") == selected_group

        recent_client_incoming_documents = [
            row for row in history_rows
            if row.get("direction_key") == "client_incoming" and row_matches_dashboard_filters(row)
        ][:8]
        recent_vendor_outgoing_documents = [
            row for row in history_rows
            if row.get("direction_key") == "vendor_outgoing" and row_matches_dashboard_filters(row)
        ][:8]
        recent_vendor_incoming_documents = [
            row for row in history_rows
            if row.get("direction_key") == "vendor_incoming" and row_matches_dashboard_filters(row)
        ][:8]
        recent_client_outgoing_documents = [
            row for row in history_rows
            if row.get("direction_key") == "client_outgoing" and row_matches_dashboard_filters(row)
        ][:8]
        all_ongoing_request_rows = build_ongoing_request_rows()

        def request_matches_group(row: dict[str, Any]) -> bool:
            if selected_group == "all":
                return True
            flow_key = row.get("document_flow_key") or ""
            if selected_group == "client_incoming":
                return flow_key == "user_request" or row.get("request_can_create_vendor_estimate")
            if selected_group == "vendor_outgoing":
                return flow_key == "vendor_estimate" or row.get("request_can_register_vendor_kaitori")
            if selected_group == "vendor_incoming":
                return flow_key == "vendor_statement" or row.get("request_can_create_client_invoice")
            if selected_group == "client_outgoing":
                return flow_key in {"client_invoice", "completed"} or bool(row.get("client_invoice_id"))
            return True

        ongoing_request_rows = [row for row in all_ongoing_request_rows if request_matches_group(row)]
        review_ready_request_rows = [
            row for row in ongoing_request_rows
            if row.get("request_can_create_client_invoice")
        ]
        group_meta = {
            "all": {
                "title": "進行中の販売代行と次の作業",
                "note": "案件ごとに、今どこまで進んでいて、次に何を作るべきかをまとめています。誤送信を防ぐため、書類は順番どおりにしか進めません。",
            },
            "client_incoming": {
                "title": "受付後の案件と次の作業",
                "note": "クライアントから依頼書が届いた段階の案件です。ここで内容確認をして、次は業者向け見積依頼書の作成へ進みます。",
            },
            "vendor_outgoing": {
                "title": "業者へ依頼中の案件と次の作業",
                "note": "開花から業者へ見積依頼を送った段階です。次は業者買取明細書の受領・登録へ進みます。",
            },
            "vendor_incoming": {
                "title": "業者回答受領後の案件と次の作業",
                "note": "業者買取明細書が届いた案件です。内容確認をして、次はユーザー向け買取明細書の確認・発行へ進みます。",
            },
            "client_outgoing": {
                "title": "クライアント返送段階の案件と次の作業",
                "note": "ユーザー向け書類の作成・返送段階です。送付済みや処理完了の案件もここで追えます。",
            },
        }

        return render_template(
            "admin/documents_dashboard.html",
            document_counts=document_counts,
            document_type_counts=document_type_counts,
            ongoing_request_rows=ongoing_request_rows,
            review_ready_request_rows=review_ready_request_rows,
            recent_client_incoming_documents=recent_client_incoming_documents,
            recent_vendor_outgoing_documents=recent_vendor_outgoing_documents,
            recent_vendor_incoming_documents=recent_vendor_incoming_documents,
            recent_client_outgoing_documents=recent_client_outgoing_documents,
            selected_group=selected_group,
            group_meta=group_meta,
        )

    def admin_documents_history_v2():
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("アクセス権限がありません。", "error")
            return redirect(url_for("index"))

        filters = {
            "doc_type": request.args.get("doc_type", "all"),
            "client": request.args.get("client", "").strip(),
            "status": request.args.get("status", "all"),
            "direction": request.args.get("direction", "all"),
            "service_type": request.args.get("service_type", "all"),
            "date_from": request.args.get("date_from", "").strip(),
            "date_to": request.args.get("date_to", "").strip(),
            "keyword": request.args.get("keyword", "").strip(),
        }
        all_rows = fetch_admin_document_history_rows_v3()
        history_rows = apply_admin_document_history_filters(all_rows, filters)
        client_options = sorted({row.get("client_name") for row in all_rows if row.get("client_name")})
        status_options = sorted({(row.get("status"), row.get("status_label")) for row in all_rows if row.get("status")})
        document_type_options = [
            ("client_mitsumori", "見積り依頼書"),
            ("client_kaitori_request", "買取依頼書"),
            ("vendor_estimate", "業者向け見積依頼書"),
            ("vendor_statement", "業者買取明細書"),
            ("client_invoice", "ユーザー向け買取明細書"),
            ("shikiriosho", "仕切書"),
            ("auction_keisan", "オークション計算書"),
        ]
        return render_template(
            "admin/documents_history.html",
            history_rows=history_rows,
            filters=filters,
            client_options=client_options,
            status_options=status_options,
            document_type_options=document_type_options,
            service_types=service_types,
        )

    def documents_v2():
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT * FROM invoices
                WHERE sender_id = {mark()}
                  AND COALESCE(document_scope, 'client_outgoing') = 'client_outgoing'
                  AND COALESCE(status, 'draft') <> 'draft'
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            invoices = rows_to_dicts(cur.fetchall())

            cur.execute(
                f"""
                SELECT * FROM user_mitsumori
                WHERE user_id = {mark()}
                  AND COALESCE(document_scope, 'client_incoming') = 'client_incoming'
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            user_mitsumori_list = rows_to_dicts(cur.fetchall())

            cur.execute(
                f"""
                SELECT * FROM user_keisan
                WHERE user_id = {mark()}
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            user_keisan_list = rows_to_dicts(cur.fetchall())

            cur.execute(
                f"""
                SELECT s.*, u.display_name AS sender_display_name
                FROM shikiriosho s
                LEFT JOIN users u ON s.sender_id = u.id
                WHERE s.recipient_id = {mark()}
                ORDER BY s.issue_date DESC, s.id DESC
                """,
                (current_user.id,),
            )
            shikiriosho_list = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()

        return render_template(
            "documents.html",
            invoices=invoices,
            user_mitsumori_list=user_mitsumori_list,
            user_keisan_list=user_keisan_list,
            shikiriosho_list=shikiriosho_list,
        )

    def user_invoice_list_v2():
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT * FROM invoices
                WHERE sender_id = {mark()}
                  AND COALESCE(document_scope, 'client_outgoing') = 'client_outgoing'
                  AND COALESCE(status, 'draft') <> 'draft'
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            invoices = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()
        return render_template("invoice_list.html", invoices=invoices)

    def user_invoice_view_v2(id: int):
        invoice = (load_invoice_for_user(id) or [None])[0]
        if invoice and not is_user_visible_invoice(invoice):
            flash("この買取明細書は開花側で確認中のため、まだ表示できません。", "error")
            return redirect(url_for("user_invoice_list"))
        return originals["user_invoice_view"](id)

    def user_mitsumori_list_v2():
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT * FROM user_mitsumori
                WHERE user_id = {mark()}
                  AND COALESCE(document_scope, 'client_incoming') = 'client_incoming'
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            mitsumori_list = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()
        return render_template("mitsumori_list.html", mitsumori_list=mitsumori_list)

    def user_kaitori_shoudaku_list_v2():
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT * FROM user_kaitori_shoudaku
                WHERE user_id = {mark()}
                  AND COALESCE(document_scope, 'client_incoming') = 'client_incoming'
                ORDER BY created_at DESC
                """,
                (current_user.id,),
            )
            kaitori_list = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()
        return render_template("kaitori_shoudaku_list.html", kaitori_list=kaitori_list)

    def user_invoice_edit_v2(id: int):
        invoice = (load_invoice_for_user(id) or [None])[0]
        if is_admin_generated_invoice(invoice):
            flash("開花側で作成した買取明細書はユーザー画面から編集できません。", "error")
            return redirect(url_for("user_invoice_list"))
        return originals["user_invoice_edit"](id)

    def user_invoice_delete_v2(id: int):
        invoice = (load_invoice_for_user(id) or [None])[0]
        if is_admin_generated_invoice(invoice):
            flash("開花側で作成した買取明細書は削除できません。", "error")
            return redirect(url_for("user_invoice_list"))
        return originals["user_invoice_delete"](id)

    def user_invoice_send_v2(id: int):
        invoice = (load_invoice_for_user(id) or [None])[0]
        if is_admin_generated_invoice(invoice):
            flash("開花側で作成した買取明細書はユーザーから送付できません。", "error")
            return redirect(url_for("user_invoice_list"))
        return originals["user_invoice_send"](id)

    def user_mitsumori_view_v2(id: int):
        mitsumori = (load_mitsumori_for_user(id) or [None])[0]
        if mitsumori and is_admin_vendor_mitsumori(mitsumori):
            flash("業者向け見積依頼書はユーザー画面には表示されません。", "error")
            return redirect(url_for("documents"))
        return originals["user_mitsumori_view"](id)

    def user_mitsumori_edit_v2(id: int):
        mitsumori = (load_mitsumori_for_user(id) or [None])[0]
        if mitsumori and not is_visible_user_scope(mitsumori, "client_incoming"):
            flash("この見積り依頼書は編集できません。", "error")
            return redirect(url_for("user_mitsumori_list"))
        return originals["user_mitsumori_edit"](id)

    def user_mitsumori_delete_v2(id: int):
        mitsumori = (load_mitsumori_for_user(id) or [None])[0]
        if mitsumori and not is_visible_user_scope(mitsumori, "client_incoming"):
            flash("この見積り依頼書は削除できません。", "error")
            return redirect(url_for("user_mitsumori_list"))
        return originals["user_mitsumori_delete"](id)

    def user_kaitori_shoudaku_view_v2(id: int):
        kaitori = (load_kaitori_request_for_user(id) or [None])[0]
        if kaitori and not is_visible_user_scope(kaitori, "client_incoming"):
            flash("この買取依頼書は表示できません。", "error")
            return redirect(url_for("user_kaitori_shoudaku_list"))
        return originals["user_kaitori_shoudaku_view"](id)

    def user_kaitori_shoudaku_edit_v2(id: int):
        kaitori = (load_kaitori_request_for_user(id) or [None])[0]
        if kaitori and not is_visible_user_scope(kaitori, "client_incoming"):
            flash("この買取依頼書は編集できません。", "error")
            return redirect(url_for("user_kaitori_shoudaku_list"))
        return originals["user_kaitori_shoudaku_edit"](id)

    def user_kaitori_shoudaku_delete_v2(id: int):
        kaitori = (load_kaitori_request_for_user(id) or [None])[0]
        if kaitori and not is_visible_user_scope(kaitori, "client_incoming"):
            flash("この買取依頼書は削除できません。", "error")
            return redirect(url_for("user_kaitori_shoudaku_list"))
        return originals["user_kaitori_shoudaku_delete"](id)

    def admin_kaitori_view_v2(id: int):
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("アクセス権限がありません。", "error")
            return redirect(url_for("index"))
        original_admin_invoice_view = originals.get("admin_invoice_view")
        if callable(original_admin_invoice_view):
            return original_admin_invoice_view(id)
        flash("買取明細書の表示に必要な画面が見つかりません。", "error")
        return redirect(url_for("admin_documents_dashboard"))

    def admin_mitsumori_add_v4():
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("アクセス権限がありません。", "error")
            return redirect(url_for("index"))

        source_request = None
        source_request_products = []
        request_id = request.args.get("request_id", type=int)
        target_user_id = request.args.get("target_user_id", type=int)

        if request.method == "POST":
            request_id = request.form.get("request_id", type=int)
            target_user_id = request.form.get("target_user_id", type=int)
            issue_date = request.form.get("issue_date")
            valid_until = request.form.get("valid_until") or None
            company_name = (request.form.get("company_name") or "").strip()
            department = (request.form.get("department") or "").strip()
            contact_person = (request.form.get("contact_person") or "").strip()
            address = (request.form.get("address") or "").strip()
            subject = (request.form.get("subject") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            raw_status = request.form.get("status", "draft")
            document_status = "completed" if raw_status == "completed" else "draft"
            items_data, total_amount = module.build_mitsumori_items_from_form()

            if request_id:
                source_request, source_request_products = fetch_request_source(request_id, waiting_only=True)
                if source_request:
                    target_user_id = target_user_id or source_request.get("user_id")
                    if source_request.get("vendor_mitsumori_id"):
                        flash("この申請ではすでに業者向け見積依頼書を作成済みです。", "info")
                        return redirect(url_for("admin_sales_agency_request_detail", id=request_id))

            if not target_user_id:
                flash("対象クライアントを特定できませんでした。", "error")
                return redirect(request.url)
            if not items_data:
                flash("見積り依頼書に追加する商品を選択してください。", "error")
                return redirect(request.url)

            conn, cur = open_cursor()
            try:
                now = datetime.now()
                document_no = module.generate_admin_mitsumori_document_no()
                if DATABASE_URL:
                    cur.execute(
                        """
                        INSERT INTO user_mitsumori
                        (document_no, user_id, issue_date, valid_until, company_name, department,
                         contact_person, address, subject, total_amount, notes, status,
                         document_scope, sales_agency_request_id, created_by_admin_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            document_no,
                            target_user_id,
                            issue_date,
                            valid_until,
                            company_name,
                            department,
                            contact_person,
                            address,
                            subject,
                            total_amount,
                            notes,
                            document_status,
                            "vendor_outgoing",
                            request_id,
                            current_user.id,
                        ),
                    )
                    mitsumori_id = cur.fetchone()["id"]
                else:
                    cur.execute(
                        """
                        INSERT INTO user_mitsumori
                        (document_no, user_id, issue_date, valid_until, company_name, department,
                         contact_person, address, subject, total_amount, notes, status,
                         document_scope, sales_agency_request_id, created_by_admin_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_no,
                            target_user_id,
                            issue_date,
                            valid_until,
                            company_name,
                            department,
                            contact_person,
                            address,
                            subject,
                            total_amount,
                            notes,
                            document_status,
                            "vendor_outgoing",
                            request_id,
                            current_user.id,
                        ),
                    )
                    mitsumori_id = cur.lastrowid

                for item in items_data:
                    cur.execute(
                        f"""
                        INSERT INTO user_mitsumori_items
                        (mitsumori_id, item_no, item_name, quantity, unit, unit_price, amount, merchandise_id)
                        VALUES ({mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()})
                        """,
                        (
                            mitsumori_id,
                            item["item_no"],
                            item["item_name"],
                            item["quantity"],
                            item["unit"],
                            item["unit_price"],
                            item["amount"],
                            item["merchandise_id"],
                        ),
                    )

                if request_id:
                    processed_value = now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S")
                    cur.execute(
                        f"""
                        UPDATE sales_agency_requests
                        SET vendor_mitsumori_id = {mark()},
                            created_mitsumori_id = {mark()},
                            documents_created_at = {mark()},
                            status = CASE
                                WHEN status IN ('pending', 'approved') THEN 'appraising'
                                ELSE status
                            END,
                            processed_at = {mark()},
                            processed_by = {mark()}
                        WHERE id = {mark()}
                        """,
                        (
                            mitsumori_id,
                            mitsumori_id,
                            processed_value,
                            processed_value,
                            current_user.id,
                            request_id,
                        ),
                    )

                conn.commit()
                flash("業者向け見積依頼書を作成しました。", "success")
                if request_id:
                    return redirect(url_for("admin_sales_agency_request_detail", id=request_id))
                return redirect(url_for("admin_mitsumori_view", id=mitsumori_id))
            except Exception as exc:
                conn.rollback()
                flash(f"見積依頼書の作成に失敗しました: {exc}", "error")
                return redirect(request.url)
            finally:
                cur.close()
                conn.close()

        if request_id:
            source_request, source_request_products = fetch_request_source(request_id, waiting_only=True)
            if not source_request:
                flash("対象の申請が見つかりません。", "error")
                return redirect(url_for("admin_sales_agency_requests"))
            target_user_id = target_user_id or source_request.get("user_id")
            if source_request.get("vendor_mitsumori_id"):
                flash("この申請ではすでに業者向け見積依頼書を作成済みです。", "info")
                return redirect(url_for("admin_sales_agency_request_detail", id=request_id))

        today = datetime.now()
        return render_template(
            "admin/mitsumori_form.html",
            mitsumori=None,
            items=[],
            today=today.strftime("%Y-%m-%d"),
            document_no=module.generate_admin_mitsumori_document_no(),
            default_valid_until=(today + timedelta(days=30)).strftime("%Y-%m-%d"),
            company_name_default=source_request.get("client_name") if source_request else "",
            department_default="",
            contact_person_default="",
            address_default="",
            subject_default=f"{source_request.get('service_name')} 業者向け見積依頼書" if source_request else "見積依頼書",
            notes_default="",
            source_request=source_request,
            source_request_products=source_request_products,
            target_user_id=target_user_id,
        )

    def admin_kaitori_shoudaku_add_v2():
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("アクセス権限がありません。", "error")
            return redirect(url_for("index"))

        request_id = request.args.get("request_id", type=int)
        source_request = None
        source_vendor_items = []

        if request.method == "POST":
            request_id = request.form.get("request_id", type=int)
            company_name = (request.form.get("company_name") or "").strip()
            company_address = (request.form.get("company_address") or "").strip()
            company_phone = (request.form.get("company_phone") or "").strip()
            contact_name = (request.form.get("contact_name") or "").strip()
            issue_date = request.form.get("issue_date", datetime.now().strftime("%Y-%m-%d"))
            payment_method = (request.form.get("payment_method") or "").strip()
            bank_info = (request.form.get("bank_info") or "").strip()
            notes = (request.form.get("notes") or "").strip()
            tax_rate = float(request.form.get("tax_rate") or 10)
            product_names = request.form.getlist("product_name[]")
            brand_names = request.form.getlist("brand_name[]")
            conditions = request.form.getlist("condition[]")
            quantities = request.form.getlist("quantity[]")
            unit_prices = request.form.getlist("unit_price[]")

            source_request = fetch_sales_agency_request_details_v2(request_id, viewer="admin")[0] if request_id else None
            if request_id and not source_request:
                flash("対象の申請が見つかりません。", "error")
                return redirect(url_for("admin_sales_agency_requests"))
            if source_request:
                if source_request.get("vendor_kaitori_shoudaku_id"):
                    flash("この申請ではすでに業者買取明細書を登録済みです。", "info")
                    return redirect(url_for("admin_sales_agency_request_detail", id=request_id))
                if not source_request.get("vendor_mitsumori_id"):
                    flash("先に業者向け見積依頼書を作成してください。", "error")
                    return redirect(url_for("admin_sales_agency_request_detail", id=request_id))

            subtotal = 0
            prepared_items = []
            for index, product_name in enumerate(product_names, start=1):
                product_name = (product_name or "").strip()
                if not product_name:
                    continue
                quantity = int((quantities[index - 1] or "1")) if index - 1 < len(quantities) else 1
                unit_price = int(float((unit_prices[index - 1] or "0"))) if index - 1 < len(unit_prices) else 0
                amount = quantity * unit_price
                subtotal += amount
                prepared_items.append(
                    {
                        "item_no": len(prepared_items) + 1,
                        "product_name": product_name,
                        "brand_name": brand_names[index - 1] if index - 1 < len(brand_names) else "",
                        "condition": conditions[index - 1] if index - 1 < len(conditions) else "",
                        "quantity": quantity,
                        "unit_price": unit_price,
                        "amount": amount,
                    }
                )

            if not prepared_items:
                flash("業者買取明細書に登録する商品を入力してください。", "error")
                return redirect(request.url)

            tax_amount = int(subtotal * tax_rate / 100)
            total_amount = subtotal + tax_amount

            conn, cur = open_cursor()
            try:
                now = datetime.now()
                document_no = f"KSH-{now.strftime('%Y%m%d%H%M%S')}"
                if DATABASE_URL:
                    cur.execute(
                        """
                        INSERT INTO admin_kaitori_shoudaku
                        (document_no, admin_id, company_name, company_address, company_phone, contact_name,
                         issue_date, subtotal, tax_amount, total_amount, tax_rate, payment_method, bank_info, notes,
                         status, document_scope, sales_agency_request_id, source_mitsumori_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            document_no,
                            current_user.id,
                            company_name,
                            company_address,
                            company_phone,
                            contact_name,
                            issue_date,
                            subtotal,
                            tax_amount,
                            total_amount,
                            tax_rate,
                            payment_method,
                            bank_info,
                            notes,
                            "completed",
                            "vendor_incoming",
                            request_id,
                            source_request.get("vendor_mitsumori_id") if source_request else None,
                        ),
                    )
                    kaitori_id = cur.fetchone()["id"]
                else:
                    cur.execute(
                        """
                        INSERT INTO admin_kaitori_shoudaku
                        (document_no, admin_id, company_name, company_address, company_phone, contact_name,
                         issue_date, subtotal, tax_amount, total_amount, tax_rate, payment_method, bank_info, notes,
                         status, document_scope, sales_agency_request_id, source_mitsumori_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document_no,
                            current_user.id,
                            company_name,
                            company_address,
                            company_phone,
                            contact_name,
                            issue_date,
                            subtotal,
                            tax_amount,
                            total_amount,
                            tax_rate,
                            payment_method,
                            bank_info,
                            notes,
                            "completed",
                            "vendor_incoming",
                            request_id,
                            source_request.get("vendor_mitsumori_id") if source_request else None,
                        ),
                    )
                    kaitori_id = cur.lastrowid

                for item in prepared_items:
                    cur.execute(
                        f"""
                        INSERT INTO admin_kaitori_shoudaku_items
                        (kaitori_shoudaku_id, item_no, product_name, brand_name, condition, quantity, unit_price, amount)
                        VALUES ({mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()})
                        """,
                        (
                            kaitori_id,
                            item["item_no"],
                            item["product_name"],
                            item["brand_name"],
                            item["condition"],
                            item["quantity"],
                            item["unit_price"],
                            item["amount"],
                        ),
                    )

                if request_id:
                    processed_value = now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S")
                    cur.execute(
                        f"""
                        UPDATE sales_agency_requests
                        SET vendor_kaitori_shoudaku_id = {mark()},
                            status = CASE
                                WHEN status IN ('approved', 'appraising') THEN 'inspecting'
                                ELSE status
                            END,
                            processed_at = {mark()},
                            processed_by = {mark()}
                        WHERE id = {mark()}
                        """,
                        (kaitori_id, processed_value, current_user.id, request_id),
                    )

                conn.commit()
                flash("業者買取明細書を登録しました。", "success")
                if request_id:
                    return redirect(url_for("admin_sales_agency_request_detail", id=request_id))
                return redirect(url_for("admin_kaitori_shoudaku_view", id=kaitori_id))
            except Exception as exc:
                conn.rollback()
                flash(f"業者買取明細書の登録に失敗しました: {exc}", "error")
                return redirect(request.url)
            finally:
                cur.close()
                conn.close()

        if request_id:
            source_request, _ = fetch_sales_agency_request_details_v2(request_id, viewer="admin")
            if not source_request:
                flash("対象の申請が見つかりません。", "error")
                return redirect(url_for("admin_sales_agency_requests"))
            if source_request.get("vendor_kaitori_shoudaku_id"):
                flash("この申請ではすでに業者買取明細書を登録済みです。", "info")
                return redirect(url_for("admin_sales_agency_request_detail", id=request_id))
            if not source_request.get("vendor_mitsumori_id"):
                flash("先に業者向け見積依頼書を作成してください。", "error")
                return redirect(url_for("admin_sales_agency_request_detail", id=request_id))
            source_vendor_items = load_vendor_mitsumori_items(source_request)

        return render_template(
            "admin/kaitori_shoudaku_form.html",
            kaitori=None,
            items=source_vendor_items,
            mode="add",
            today=datetime.now().strftime("%Y-%m-%d"),
            source_request=source_request,
        )

    def admin_kaitori_add_v2():
        if not current_user.is_authenticated or not current_user.is_admin():
            flash("アクセス権限がありません。", "error")
            return redirect(url_for("index"))

        request_id = request.args.get("request_id", type=int)
        source_request = None
        source_vendor_kaitori = None

        if request.method == "POST":
            request_id = request.form.get("request_id", type=int)
            issue_date = request.form.get("issue_date")
            seller_id = request.form.get("seller_id", type=int)
            notes = (request.form.get("notes") or "").strip()
            raw_status = request.form.get("status", "draft")
            status_value = "sent" if raw_status == "completed" else "draft"
            item_names = request.form.getlist("item_name[]")
            brand_names = request.form.getlist("brand_name[]")
            purchase_dates = request.form.getlist("purchase_date[]")
            item_conditions = request.form.getlist("item_condition[]")
            amounts = request.form.getlist("amount[]")
            merchandise_ids = request.form.getlist("merchandise_id[]")

            source_request = fetch_sales_agency_request_details_v2(request_id, viewer="admin")[0] if request_id else None
            if request_id and not source_request:
                flash("対象の申請が見つかりません。", "error")
                return redirect(url_for("admin_sales_agency_requests"))
            if source_request:
                seller_id = seller_id or source_request.get("user_id")
                if source_request.get("client_invoice_id"):
                    flash("この申請ではすでにユーザー向け買取明細書を作成済みです。", "info")
                    return redirect(url_for("admin_sales_agency_request_detail", id=request_id))
                if not source_request.get("vendor_kaitori_shoudaku_id"):
                    flash("先に業者買取明細書を登録してください。", "error")
                    return redirect(url_for("admin_sales_agency_request_detail", id=request_id))

            prepared_items = []
            total_amount = 0
            for index, item_name in enumerate(item_names, start=1):
                item_name = (item_name or "").strip()
                if not item_name:
                    continue
                amount_value = int(float((amounts[index - 1] or "0"))) if index - 1 < len(amounts) else 0
                total_amount += amount_value
                merchandise_id_raw = merchandise_ids[index - 1] if index - 1 < len(merchandise_ids) else ""
                try:
                    merchandise_id = int(merchandise_id_raw) if merchandise_id_raw not in ("", None) else None
                except (TypeError, ValueError):
                    merchandise_id = None
                prepared_items.append(
                    {
                        "item_no": len(prepared_items) + 1,
                        "product_name": item_name,
                        "brand_name": brand_names[index - 1] if index - 1 < len(brand_names) else "",
                        "purchase_date": purchase_dates[index - 1] if index - 1 < len(purchase_dates) else "",
                        "item_condition": item_conditions[index - 1] if index - 1 < len(item_conditions) else "",
                        "amount": amount_value,
                        "merchandise_id": merchandise_id,
                    }
                )

            if not seller_id or not prepared_items:
                flash("買取明細書の宛先と商品を確認してください。", "error")
                return redirect(request.url)

            conn, cur = open_cursor()
            try:
                now = datetime.now()
                invoice_no = module.generate_admin_kaitori_document_no()
                if DATABASE_URL:
                    cur.execute(
                        """
                        INSERT INTO invoices
                        (invoice_no, sender_id, issue_date, subtotal, total_amount, service_type, notes, status, created_at,
                         document_scope, sales_agency_request_id, source_admin_kaitori_id)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            invoice_no,
                            seller_id,
                            issue_date,
                            total_amount,
                            total_amount,
                            source_request.get("service_type") if source_request else "normal",
                            notes,
                            status_value,
                            now,
                            "client_outgoing",
                            request_id,
                            source_request.get("vendor_kaitori_shoudaku_id") if source_request else None,
                        ),
                    )
                    invoice_id = cur.fetchone()["id"]
                else:
                    cur.execute(
                        """
                        INSERT INTO invoices
                        (invoice_no, sender_id, issue_date, subtotal, total_amount, service_type, notes, status, created_at,
                         document_scope, sales_agency_request_id, source_admin_kaitori_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            invoice_no,
                            seller_id,
                            issue_date,
                            total_amount,
                            total_amount,
                            source_request.get("service_type") if source_request else "normal",
                            notes,
                            status_value,
                            now.strftime("%Y-%m-%d %H:%M:%S"),
                            "client_outgoing",
                            request_id,
                            source_request.get("vendor_kaitori_shoudaku_id") if source_request else None,
                        ),
                    )
                    invoice_id = cur.lastrowid

                for item in prepared_items:
                    cur.execute(
                        f"""
                        INSERT INTO invoice_items
                        (invoice_id, item_no, product_name, quantity, unit, unit_price, amount, product_date, merchandise_id)
                        VALUES ({mark()}, {mark()}, {mark()}, 1, '点', {mark()}, {mark()}, {mark()}, {mark()})
                        """,
                        (
                            invoice_id,
                            item["item_no"],
                            item["product_name"],
                            item["amount"],
                            item["amount"],
                            item["purchase_date"] or None,
                            item["merchandise_id"],
                        ),
                    )

                if request_id:
                    processed_value = now if DATABASE_URL else now.strftime("%Y-%m-%d %H:%M:%S")
                    sent_value = processed_value if status_value == "sent" else None
                    cur.execute(
                        f"""
                        UPDATE sales_agency_requests
                        SET client_invoice_id = {mark()},
                            created_invoice_id = {mark()},
                            internal_checked_at = {mark()},
                            client_invoice_sent_at = {mark()},
                            documents_created_at = {mark()},
                            status = CASE
                                WHEN {mark()} = 'sent' THEN 'completed'
                                ELSE status
                            END,
                            processed_at = {mark()},
                            processed_by = {mark()}
                        WHERE id = {mark()}
                        """,
                        (
                            invoice_id,
                            invoice_id,
                            processed_value,
                            sent_value,
                            processed_value,
                            status_value,
                            processed_value,
                            current_user.id,
                            request_id,
                        ),
                    )

                conn.commit()
                flash("ユーザー向け買取明細書を作成しました。", "success")
                if request_id:
                    return redirect(url_for("admin_sales_agency_request_detail", id=request_id))
                return redirect(url_for("admin_kaitori_view", id=invoice_id))
            except Exception as exc:
                conn.rollback()
                flash(f"買取明細書の作成に失敗しました: {exc}", "error")
                return redirect(request.url)
            finally:
                cur.close()
                conn.close()

        if request_id:
            source_request, _ = fetch_sales_agency_request_details_v2(request_id, viewer="admin")
            if not source_request:
                flash("対象の申請が見つかりません。", "error")
                return redirect(url_for("admin_sales_agency_requests"))
            if source_request.get("client_invoice_id"):
                flash("この申請ではすでにユーザー向け買取明細書を作成済みです。", "info")
                return redirect(url_for("admin_sales_agency_request_detail", id=request_id))
            if not source_request.get("vendor_kaitori_shoudaku_id"):
                flash("先に業者買取明細書を登録してください。", "error")
                return redirect(url_for("admin_sales_agency_request_detail", id=request_id))
            source_vendor_kaitori, prefilled_items = load_vendor_kaitori_context(source_request)
        else:
            prefilled_items = []

        users = []
        conn, cur = open_cursor()
        try:
            cur.execute("SELECT id, username, display_name FROM users ORDER BY display_name")
            users = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()

        return render_template(
            "admin/kaitori_form.html",
            kaitori=None,
            users=users,
            today=datetime.now().strftime("%Y-%m-%d"),
            document_no=module.generate_admin_kaitori_document_no(),
            items=prefilled_items,
            source_request=source_request,
            source_vendor_kaitori=source_vendor_kaitori,
        )

    module.fetch_sales_agency_request_details = fetch_sales_agency_request_details_v2
    module.fetch_admin_document_history_rows_v2 = fetch_admin_document_history_rows_v3
    module.fetch_admin_document_history_rows = fetch_admin_document_history_rows_v3
    module.admin_documents_dashboard = admin_documents_dashboard_v2
    module.admin_documents_history = admin_documents_history_v2
    module.documents = documents_v2
    module.user_invoice_list = user_invoice_list_v2
    module.user_invoice_view = user_invoice_view_v2
    module.user_mitsumori_list = user_mitsumori_list_v2
    module.user_kaitori_shoudaku_list = user_kaitori_shoudaku_list_v2
    module.user_invoice_edit = user_invoice_edit_v2
    module.user_invoice_delete = user_invoice_delete_v2
    module.user_invoice_send = user_invoice_send_v2
    module.user_mitsumori_view = user_mitsumori_view_v2
    module.user_mitsumori_edit = user_mitsumori_edit_v2
    module.user_mitsumori_delete = user_mitsumori_delete_v2
    module.user_kaitori_shoudaku_view = user_kaitori_shoudaku_view_v2
    module.user_kaitori_shoudaku_edit = user_kaitori_shoudaku_edit_v2
    module.user_kaitori_shoudaku_delete = user_kaitori_shoudaku_delete_v2
    module.admin_mitsumori_add = admin_mitsumori_add_v4
    module.admin_kaitori_shoudaku_add = admin_kaitori_shoudaku_add_v2
    module.admin_kaitori_add = admin_kaitori_add_v2
    module.admin_kaitori_view = admin_kaitori_view_v2

    for endpoint in [
        "sales_agency_my_requests",
        "admin_sales_agency_requests",
        "admin_sales_agency_request_detail",
        "admin_sales_agency_process",
    ]:
        original = originals.get(endpoint)
        if callable(original):
            app.view_functions[endpoint] = original

    app.view_functions["documents"] = documents_v2
    app.view_functions["user_invoice_list"] = user_invoice_list_v2
    app.view_functions["user_invoice_view"] = user_invoice_view_v2
    app.view_functions["user_mitsumori_list"] = user_mitsumori_list_v2
    app.view_functions["user_kaitori_shoudaku_list"] = user_kaitori_shoudaku_list_v2
    app.view_functions["user_invoice_edit"] = user_invoice_edit_v2
    app.view_functions["user_invoice_delete"] = user_invoice_delete_v2
    app.view_functions["user_invoice_send"] = user_invoice_send_v2
    app.view_functions["user_mitsumori_view"] = user_mitsumori_view_v2
    app.view_functions["user_mitsumori_edit"] = user_mitsumori_edit_v2
    app.view_functions["user_mitsumori_delete"] = user_mitsumori_delete_v2
    app.view_functions["user_kaitori_shoudaku_view"] = user_kaitori_shoudaku_view_v2
    app.view_functions["user_kaitori_shoudaku_edit"] = user_kaitori_shoudaku_edit_v2
    app.view_functions["user_kaitori_shoudaku_delete"] = user_kaitori_shoudaku_delete_v2
    app.view_functions["admin_mitsumori_add"] = admin_mitsumori_add_v4
    app.view_functions["admin_kaitori_shoudaku_add"] = admin_kaitori_shoudaku_add_v2
    app.view_functions["admin_kaitori_add"] = admin_kaitori_add_v2
    app.view_functions["admin_kaitori_view"] = admin_kaitori_view_v2
    app.view_functions["admin_documents_dashboard"] = admin_documents_dashboard_v2
    app.view_functions["admin_documents_history"] = admin_documents_history_v2
