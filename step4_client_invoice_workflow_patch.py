# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Iterable

from flask import flash, redirect, render_template, request, url_for


def apply(module: Any) -> None:
    if getattr(module, "_step4_client_invoice_workflow_patch_applied", False):
        return
    module._step4_client_invoice_workflow_patch_applied = True

    app = module.app
    DATABASE_URL = getattr(module, "DATABASE_URL", None)
    login_required = module.login_required
    admin_required = module.admin_required
    current_user = module.current_user
    get_jst_now = module.get_jst_now

    open_cursor = module._docs_open_cursor
    mark = module._docs_mark
    row_to_dict = module._docs_row_to_dict
    rows_to_dicts = module._docs_rows_to_dicts
    column_exists = module._docs_column_exists
    table_exists = module._docs_table_exists
    safe_int = module._docs_safe_int
    format_date = module._stepa_format_date

    previous_dashboard = app.view_functions.get("admin_documents_dashboard")

    def _add_column(cur, table: str, column: str, pg_sql: str, sqlite_sql: str) -> None:
        if table_exists(cur, table) and not column_exists(cur, table, column):
            cur.execute(pg_sql if DATABASE_URL else sqlite_sql)

    def ensure_step4_schema() -> None:
        module._ensure_stepa_document_workflow_columns()
        conn, cur = open_cursor()
        try:
            invoice_columns = [
                ("created_by_admin_id", "ALTER TABLE invoices ADD COLUMN created_by_admin_id INTEGER", "ALTER TABLE invoices ADD COLUMN created_by_admin_id INTEGER"),
                ("updated_by_admin_id", "ALTER TABLE invoices ADD COLUMN updated_by_admin_id INTEGER", "ALTER TABLE invoices ADD COLUMN updated_by_admin_id INTEGER"),
                ("sent_at", "ALTER TABLE invoices ADD COLUMN sent_at TIMESTAMP", "ALTER TABLE invoices ADD COLUMN sent_at TEXT"),
                ("source_workflow_step", "ALTER TABLE invoices ADD COLUMN source_workflow_step VARCHAR(40)", "ALTER TABLE invoices ADD COLUMN source_workflow_step TEXT"),
            ]
            item_columns = [
                ("brand_name", "ALTER TABLE invoice_items ADD COLUMN brand_name TEXT", "ALTER TABLE invoice_items ADD COLUMN brand_name TEXT"),
                ("request_item_id", "ALTER TABLE invoice_items ADD COLUMN request_item_id INTEGER", "ALTER TABLE invoice_items ADD COLUMN request_item_id INTEGER"),
                ("vendor_document_id", "ALTER TABLE invoice_items ADD COLUMN vendor_document_id INTEGER", "ALTER TABLE invoice_items ADD COLUMN vendor_document_id INTEGER"),
                ("vendor_document_title", "ALTER TABLE invoice_items ADD COLUMN vendor_document_title TEXT", "ALTER TABLE invoice_items ADD COLUMN vendor_document_title TEXT"),
                ("vendor_document_filename", "ALTER TABLE invoice_items ADD COLUMN vendor_document_filename TEXT", "ALTER TABLE invoice_items ADD COLUMN vendor_document_filename TEXT"),
                ("vendor_name", "ALTER TABLE invoice_items ADD COLUMN vendor_name TEXT", "ALTER TABLE invoice_items ADD COLUMN vendor_name TEXT"),
                ("vendor_reference_amount", "ALTER TABLE invoice_items ADD COLUMN vendor_reference_amount INTEGER DEFAULT 0", "ALTER TABLE invoice_items ADD COLUMN vendor_reference_amount INTEGER DEFAULT 0"),
                ("client_payment_amount", "ALTER TABLE invoice_items ADD COLUMN client_payment_amount INTEGER DEFAULT 0", "ALTER TABLE invoice_items ADD COLUMN client_payment_amount INTEGER DEFAULT 0"),
                ("difference_amount", "ALTER TABLE invoice_items ADD COLUMN difference_amount INTEGER DEFAULT 0", "ALTER TABLE invoice_items ADD COLUMN difference_amount INTEGER DEFAULT 0"),
                ("difference_rate", "ALTER TABLE invoice_items ADD COLUMN difference_rate NUMERIC(10,2) DEFAULT 0", "ALTER TABLE invoice_items ADD COLUMN difference_rate REAL DEFAULT 0"),
                ("item_note", "ALTER TABLE invoice_items ADD COLUMN item_note TEXT", "ALTER TABLE invoice_items ADD COLUMN item_note TEXT"),
                ("source_workflow_step", "ALTER TABLE invoice_items ADD COLUMN source_workflow_step VARCHAR(40)", "ALTER TABLE invoice_items ADD COLUMN source_workflow_step TEXT"),
            ]
            request_item_columns = [
                ("client_invoice_id", "ALTER TABLE sales_agency_request_items ADD COLUMN client_invoice_id INTEGER", "ALTER TABLE sales_agency_request_items ADD COLUMN client_invoice_id INTEGER"),
                ("client_invoice_sent_at", "ALTER TABLE sales_agency_request_items ADD COLUMN client_invoice_sent_at TIMESTAMP", "ALTER TABLE sales_agency_request_items ADD COLUMN client_invoice_sent_at TEXT"),
                ("client_payment_amount", "ALTER TABLE sales_agency_request_items ADD COLUMN client_payment_amount INTEGER DEFAULT 0", "ALTER TABLE sales_agency_request_items ADD COLUMN client_payment_amount INTEGER DEFAULT 0"),
                ("vendor_reference_amount", "ALTER TABLE sales_agency_request_items ADD COLUMN vendor_reference_amount INTEGER DEFAULT 0", "ALTER TABLE sales_agency_request_items ADD COLUMN vendor_reference_amount INTEGER DEFAULT 0"),
            ]
            for spec in invoice_columns:
                _add_column(cur, "invoices", *spec)
            for spec in item_columns:
                _add_column(cur, "invoice_items", *spec)
            for spec in request_item_columns:
                _add_column(cur, "sales_agency_request_items", *spec)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_sales_agency_request_items_step4 ON sales_agency_request_items (workflow_status, vendor_document_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_invoice_items_request_item ON invoice_items (request_item_id)")
            conn.commit()
        finally:
            cur.close()
            conn.close()

    def _clean_ids(values: Iterable[Any]) -> list[int]:
        cleaned: list[int] = []
        for value in values or []:
            try:
                item_id = int(value)
            except (TypeError, ValueError):
                continue
            if item_id > 0 and item_id not in cleaned:
                cleaned.append(item_id)
        return cleaned

    def _photo_path(value: Any) -> str:
        return str(value or "").replace("\\", "/").lstrip("/")

    def _display_user_name(row: dict[str, Any]) -> str:
        return row.get("user_display_name") or row.get("username") or f"ユーザーID {row.get('user_id')}"

    def _amount(value: Any) -> int:
        return safe_int(value, 0)

    def _difference_rate(vendor_amount: int, client_amount: int) -> float:
        if not vendor_amount:
            return 0.0
        return round(((vendor_amount - client_amount) / vendor_amount) * 100, 2)

    def _fetch_step4_items(user_id: int | None = None, item_ids: list[int] | None = None) -> list[dict[str, Any]]:
        ensure_step4_schema()
        item_ids = _clean_ids(item_ids or [])
        linked_doc_expr = "COALESCE(sari.vendor_document_id, vdl.vendor_document_id)"
        where = [
            "(COALESCE(NULLIF(sari.workflow_status, ''), 'step1_pending') = 'step4_ready' OR vdl.vendor_document_id IS NOT NULL)",
            "COALESCE(sari.item_status, 'active') NOT IN ('cancelled', 'canceled')",
            f"{linked_doc_expr} IS NOT NULL",
        ]
        params: list[Any] = []
        if user_id:
            where.append(f"sar.user_id = {mark()}")
            params.append(user_id)
        if item_ids:
            where.append(f"sari.id IN ({', '.join([mark()] * len(item_ids))})")
            params.extend(item_ids)
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT
                    sari.id AS request_item_id,
                    sari.request_id,
                    sari.merchandise_id,
                    sari.vendor_mitsumori_id,
                    {linked_doc_expr} AS vendor_document_id,
                    sari.moved_to_step4_at,
                    sari.updated_at,
                    sari.client_payment_amount,
                    sari.vendor_reference_amount,
                    sar.user_id,
                    sar.service_type,
                    sar.created_at AS requested_at,
                    u.display_name AS user_display_name,
                    u.username,
                    u.email AS user_email,
                    COALESCE(m.id, sari.merchandise_id) AS actual_merchandise_id,
                    COALESCE(m.product_name, sari.snapshot_product_name, '') AS product_name,
                    COALESCE(m.brand_name, sari.snapshot_brand_name, '') AS brand_name,
                    COALESCE(m.model_number, sari.snapshot_model_number, '') AS model_number,
                    COALESCE(m.kaika_product_code, sari.snapshot_kaika_product_code, '') AS kaika_product_code,
                    COALESCE(m.photo_path, sari.snapshot_photo_path, '') AS photo_path,
                    COALESCE(m.item_condition, '') AS item_condition,
                    m.purchase_date,
                    um.document_no AS vendor_estimate_no,
                    um.id AS vendor_estimate_id,
                    COALESCE(v.name, um.company_name, vd.vendor_name, '') AS estimate_vendor_name,
                    vd.title AS vendor_document_title,
                    vd.original_filename AS vendor_document_filename,
                    vd.registered_at AS vendor_document_registered_at,
                    vd.vendor_name AS vendor_document_vendor_name,
                    vd.vendor_amount AS vendor_document_amount,
                    vd.stored_path AS vendor_document_path,
                    vdl.linked_at AS vendor_linked_at
                FROM sales_agency_request_items sari
                JOIN sales_agency_requests sar ON sari.request_id = sar.id
                JOIN users u ON sar.user_id = u.id
                LEFT JOIN merchandise m ON sari.merchandise_id = m.id
                LEFT JOIN user_mitsumori um ON sari.vendor_mitsumori_id = um.id
                LEFT JOIN vendors v ON um.vendor_id = v.id
                LEFT JOIN (
                    SELECT request_item_id,
                           MAX(vendor_document_id) AS vendor_document_id,
                           MAX(linked_at) AS linked_at
                    FROM vendor_document_item_links
                    GROUP BY request_item_id
                ) vdl ON vdl.request_item_id = sari.id
                LEFT JOIN vendor_documents vd ON {linked_doc_expr} = vd.id
                WHERE {' AND '.join(where)}
                ORDER BY COALESCE(sari.updated_at, sari.moved_to_step4_at, vd.registered_at, sar.created_at) DESC, sari.id DESC
                """,
                tuple(params),
            )
            rows = rows_to_dicts(cur.fetchall())
        finally:
            cur.close()
            conn.close()

        for row in rows:
            product_name = (row.get("product_name") or "").strip()
            fallback = " / ".join(
                str(part).strip()
                for part in [row.get("brand_name"), row.get("model_number"), row.get("kaika_product_code")]
                if str(part or "").strip()
            )
            row["product_name"] = product_name or fallback or f"商品ID {row.get('merchandise_id') or row.get('request_item_id')}"
            row["user_name"] = _display_user_name(row)
            row["photo_path"] = _photo_path(row.get("photo_path"))
            row["vendor_name"] = row.get("vendor_document_vendor_name") or row.get("estimate_vendor_name") or "未設定"
            row["vendor_document_label"] = row.get("vendor_document_title") or row.get("vendor_document_filename") or f"業者書類ID {row.get('vendor_document_id')}"
            row["vendor_document_registered_label"] = format_date(row.get("vendor_document_registered_at"), with_time=True)
            row["linked_label"] = format_date(row.get("vendor_linked_at") or row.get("moved_to_step4_at"), with_time=True)
            row["updated_label"] = format_date(row.get("updated_at") or row.get("moved_to_step4_at") or row.get("vendor_document_registered_at"), with_time=True)
            row["vendor_reference_amount"] = _amount(row.get("vendor_reference_amount") or row.get("vendor_document_amount"))
            row["client_payment_amount"] = _amount(row.get("client_payment_amount") or row["vendor_reference_amount"])
            row["difference_amount"] = row["vendor_reference_amount"] - row["client_payment_amount"]
            row["difference_rate"] = _difference_rate(row["vendor_reference_amount"], row["client_payment_amount"])
            row["detail_url"] = url_for("view_item", id=row.get("actual_merchandise_id")) if row.get("actual_merchandise_id") else None
            row["vendor_document_url"] = url_for("admin_vendor_document_download", document_id=row.get("vendor_document_id")) if row.get("vendor_document_id") else None
            row["vendor_estimate_url"] = url_for("admin_mitsumori_view", id=row.get("vendor_estimate_id")) if row.get("vendor_estimate_id") else None
        return rows

    def _group_items_by_user(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[int, dict[str, Any]] = {}
        for item in items:
            user_id = int(item.get("user_id") or 0)
            group = grouped.setdefault(
                user_id,
                {
                    "user_id": user_id,
                    "user_name": item.get("user_name") or f"ユーザーID {user_id}",
                    "username": item.get("username") or "",
                    "items": [],
                    "latest_sort": "",
                    "vendor_document_ids": set(),
                },
            )
            group["items"].append(item)
            if item.get("vendor_document_id"):
                group["vendor_document_ids"].add(int(item.get("vendor_document_id")))
            latest = str(item.get("updated_at") or item.get("moved_to_step4_at") or item.get("vendor_document_registered_at") or "")
            if latest > group["latest_sort"]:
                group["latest_sort"] = latest
                group["last_date_label"] = item.get("updated_label") or "-"

        prepared: list[dict[str, Any]] = []
        for group in grouped.values():
            vendor_names = []
            for item in group["items"]:
                vendor_name = item.get("vendor_name") or ""
                if vendor_name and vendor_name not in vendor_names:
                    vendor_names.append(vendor_name)
            group["item_count"] = len(group["items"])
            group["vendor_document_count"] = len(group["vendor_document_ids"])
            group["vendor_summary"] = " / ".join(vendor_names[:3]) + (" ほか" if len(vendor_names) > 3 else "")
            group["status_summary"] = "買取明細書作成待ち"
            group["last_date_label"] = group.get("last_date_label") or "-"
            group.pop("vendor_document_ids", None)
            prepared.append(group)
        prepared.sort(key=lambda group: group.get("latest_sort") or "", reverse=True)
        return prepared

    def _step_counts() -> dict[str, int]:
        try:
            context = module._stepa_load_dashboard_context("client_outgoing")
            return dict(context.get("stepa_counts") or {})
        except Exception:
            step4_count = len(_fetch_step4_items())
            return {"step1": 0, "step2": 0, "step3": 0, "step4": step4_count}

    def _load_user(user_id: int) -> dict[str, Any] | None:
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"SELECT id, username, display_name, email FROM users WHERE id = {mark()}",
                (user_id,),
            )
            row = row_to_dict(cur.fetchone())
            if row:
                row["user_name"] = row.get("display_name") or row.get("username") or f"ユーザーID {user_id}"
            return row
        finally:
            cur.close()
            conn.close()

    def _update_sales_agency_request_links(cur, request_ids: list[int], invoice_id: int) -> None:
        if not request_ids or not table_exists(cur, "sales_agency_requests") or not column_exists(cur, "sales_agency_requests", "client_invoice_id"):
            return
        placeholders = ", ".join([mark()] * len(request_ids))
        cur.execute(
            f"UPDATE sales_agency_requests SET client_invoice_id = {mark()} WHERE id IN ({placeholders})",
            tuple([invoice_id] + request_ids),
        )

    def _insert_invoice(user_id: int, user_row: dict[str, Any], selected_items: list[dict[str, Any]], form_items: list[dict[str, Any]]) -> int:
        invoice_no = module._generate_prefixed_document_no("KT", "invoices", "invoice_no")
        issue_date = request.form.get("issue_date") or get_jst_now().strftime("%Y-%m-%d")
        payment_due_date = request.form.get("payment_due_date") or None
        recipient_name = (request.form.get("recipient_name") or user_row.get("user_name") or "").strip()
        notes = (request.form.get("notes") or "").strip()
        service_type = selected_items[0].get("service_type") if selected_items else "sales_agency"
        total_amount = sum(item["client_payment_amount"] for item in form_items)
        now_value = get_jst_now()
        request_ids = []
        for item in selected_items:
            request_id = int(item.get("request_id") or 0)
            if request_id and request_id not in request_ids:
                request_ids.append(request_id)

        conn, cur = open_cursor()
        try:
            if DATABASE_URL:
                cur.execute(
                    """
                    INSERT INTO invoices
                    (invoice_no, sender_id, issue_date, payment_due_date, recipient_name, subtotal,
                     tax_amount_8, tax_amount_10, total_amount, service_type, commission_rate,
                     commission_amount, bank_info, notes, status, is_read, document_scope,
                     sales_agency_request_id, source_admin_kaitori_id, created_by_admin_id,
                     updated_by_admin_id, sent_at, source_workflow_step, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        invoice_no,
                        user_id,
                        issue_date,
                        payment_due_date,
                        recipient_name,
                        total_amount,
                        0,
                        0,
                        total_amount,
                        service_type or "sales_agency",
                        0,
                        0,
                        "",
                        notes,
                        "sent",
                        0,
                        "admin_kaitori",
                        request_ids[0] if request_ids else None,
                        1,
                        getattr(current_user, "id", None),
                        getattr(current_user, "id", None),
                        now_value,
                        "step4_client_outgoing",
                        now_value,
                        now_value,
                    ),
                )
                invoice_id = int(row_to_dict(cur.fetchone())["id"])
            else:
                cur.execute(
                    """
                    INSERT INTO invoices
                    (invoice_no, sender_id, issue_date, payment_due_date, recipient_name, subtotal,
                     tax_amount_8, tax_amount_10, total_amount, service_type, commission_rate,
                     commission_amount, bank_info, notes, status, is_read, document_scope,
                     sales_agency_request_id, source_admin_kaitori_id, created_by_admin_id,
                     updated_by_admin_id, sent_at, source_workflow_step, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        invoice_no,
                        user_id,
                        issue_date,
                        payment_due_date,
                        recipient_name,
                        total_amount,
                        0,
                        0,
                        total_amount,
                        service_type or "sales_agency",
                        0,
                        0,
                        "",
                        notes,
                        "sent",
                        0,
                        "admin_kaitori",
                        request_ids[0] if request_ids else None,
                        1,
                        getattr(current_user, "id", None),
                        getattr(current_user, "id", None),
                        now_value.strftime("%Y-%m-%d %H:%M:%S"),
                        "step4_client_outgoing",
                        now_value.strftime("%Y-%m-%d %H:%M:%S"),
                        now_value.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                invoice_id = int(cur.lastrowid)

            cur.execute(
                f"UPDATE invoices SET source_admin_kaitori_id = {mark()} WHERE id = {mark()}",
                (invoice_id, invoice_id),
            )

            selected_map = {int(item["request_item_id"]): item for item in selected_items}
            for index, form_item in enumerate(form_items, start=1):
                item = selected_map[int(form_item["request_item_id"])]
                vendor_amount = form_item["vendor_reference_amount"]
                client_amount = form_item["client_payment_amount"]
                difference = vendor_amount - client_amount
                diff_rate = _difference_rate(vendor_amount, client_amount)
                product_code = item.get("kaika_product_code") or item.get("model_number") or ""
                cur.execute(
                    f"""
                    INSERT INTO invoice_items
                    (invoice_id, item_no, tax_category, product_date, product_name, product_code,
                     merchandise_id, quantity, unit, unit_price, amount, brand_name, request_item_id,
                     vendor_document_id, vendor_document_title, vendor_document_filename, vendor_name,
                     vendor_reference_amount, client_payment_amount, difference_amount,
                     difference_rate, item_note, source_workflow_step)
                    VALUES ({mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()},
                            {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()},
                            {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()}, {mark()},
                            {mark()}, {mark()}, {mark()})
                    """,
                    (
                        invoice_id,
                        index,
                        "0",
                        format_date(item.get("vendor_document_registered_at")) if item.get("vendor_document_registered_at") else None,
                        item.get("product_name"),
                        product_code,
                        item.get("actual_merchandise_id") or item.get("merchandise_id"),
                        1,
                        "点",
                        client_amount,
                        client_amount,
                        item.get("brand_name") or "",
                        item.get("request_item_id"),
                        item.get("vendor_document_id"),
                        item.get("vendor_document_title") or "",
                        item.get("vendor_document_filename") or "",
                        item.get("vendor_name") or "",
                        vendor_amount,
                        client_amount,
                        difference,
                        diff_rate,
                        form_item.get("item_note") or "",
                        "step4_client_outgoing",
                    ),
                )

            item_ids = [int(item["request_item_id"]) for item in form_items]
            placeholders = ", ".join([mark()] * len(item_ids))
            sent_value = now_value if DATABASE_URL else now_value.strftime("%Y-%m-%d %H:%M:%S")
            cur.execute(
                f"""
                UPDATE sales_agency_request_items
                SET workflow_status = {mark()},
                    client_invoice_id = {mark()},
                    client_invoice_sent_at = {mark()},
                    updated_at = {mark()}
                WHERE id IN ({placeholders})
                """,
                tuple(["step4_sent", invoice_id, sent_value, sent_value] + item_ids),
            )
            for form_item in form_items:
                cur.execute(
                    f"""
                    UPDATE sales_agency_request_items
                    SET client_payment_amount = {mark()},
                        vendor_reference_amount = {mark()}
                    WHERE id = {mark()}
                    """,
                    (form_item["client_payment_amount"], form_item["vendor_reference_amount"], form_item["request_item_id"]),
                )
            _update_sales_agency_request_links(cur, request_ids, invoice_id)
            conn.commit()
            return invoice_id
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    @login_required
    @admin_required
    def admin_documents_step4_dashboard():
        items = _fetch_step4_items()
        return render_template(
            "admin/documents_step4_dashboard.html",
            step4_groups=_group_items_by_user(items),
            step_counts=_step_counts(),
            total_items=len(items),
        )

    @login_required
    @admin_required
    def admin_step4_client_invoice_create(user_id: int):
        user_row = _load_user(user_id)
        if not user_row:
            flash("対象ユーザーが見つかりません。", "error")
            return redirect(url_for("admin_documents_dashboard", group="client_outgoing"))
        items = _fetch_step4_items(user_id=user_id)
        if not items:
            flash("このユーザーにステップ4対象商品はありません。", "info")
            return redirect(url_for("admin_documents_dashboard", group="client_outgoing"))

        if request.method == "POST":
            selected_ids = _clean_ids(request.form.getlist("request_item_ids"))
            if not selected_ids:
                flash("買取明細書に入れる商品を選択してください。", "error")
                return redirect(request.url)
            selected_items = _fetch_step4_items(user_id=user_id, item_ids=selected_ids)
            found_ids = {int(item["request_item_id"]) for item in selected_items}
            if found_ids != set(selected_ids):
                flash("選択商品の中にステップ4対象外の商品が含まれています。", "error")
                return redirect(request.url)

            form_items = []
            for item in selected_items:
                item_id = int(item["request_item_id"])
                vendor_amount = _amount(request.form.get(f"vendor_reference_amount_{item_id}") or item.get("vendor_reference_amount"))
                client_amount = _amount(request.form.get(f"client_payment_amount_{item_id}") or item.get("client_payment_amount"))
                form_items.append(
                    {
                        "request_item_id": item_id,
                        "vendor_reference_amount": vendor_amount,
                        "client_payment_amount": client_amount,
                        "item_note": (request.form.get(f"item_note_{item_id}") or "").strip(),
                    }
                )
            invoice_id = _insert_invoice(user_id, user_row, selected_items, form_items)
            flash("買取明細書を作成し、顧客へ送付済みにしました。対象商品はステップ4から移動しました。", "success")
            return redirect(url_for("admin_kaitori_view", id=invoice_id))

        total_default = sum(_amount(item.get("client_payment_amount")) for item in items)
        return render_template(
            "admin/step4_client_invoice_form.html",
            user=user_row,
            items=items,
            today=get_jst_now().strftime("%Y-%m-%d"),
            total_default=total_default,
            back_url=url_for("admin_documents_dashboard", group="client_outgoing"),
        )

    @login_required
    @admin_required
    def admin_kaitori_view_step4_aware(id: int):
        ensure_step4_schema()
        conn, cur = open_cursor()
        try:
            cur.execute(
                f"""
                SELECT i.*,
                       u.display_name AS sender_display_name,
                       u.username AS sender_username,
                       creator.display_name AS created_by_admin_name,
                       creator.username AS created_by_admin_username,
                       updater.display_name AS updated_by_admin_name,
                       updater.username AS updated_by_admin_username
                FROM invoices i
                LEFT JOIN users u ON i.sender_id = u.id
                LEFT JOIN users creator ON i.created_by_admin_id = creator.id
                LEFT JOIN users updater ON i.updated_by_admin_id = updater.id
                WHERE i.id = {mark()}
                """,
                (id,),
            )
            invoice = row_to_dict(cur.fetchone())
            if invoice and module.is_user_created_invoice_document(invoice):
                cur.execute(f"UPDATE invoices SET is_read = 1 WHERE id = {mark()}", (id,))
            cur.execute(
                f"SELECT * FROM invoice_items WHERE invoice_id = {mark()} ORDER BY item_no, id",
                (id,),
            )
            items = rows_to_dicts(cur.fetchall())
            conn.commit()
        finally:
            cur.close()
            conn.close()

        if not invoice:
            flash("買取明細書が見つかりません。", "error")
            return redirect(url_for("admin_documents_dashboard", group="client_outgoing"))
        return render_template("admin/invoice_view.html", invoice=invoice, items=items)

    @login_required
    @admin_required
    def admin_documents_dashboard_with_step4():
        if (request.args.get("group") or "all").strip() == "client_outgoing":
            return admin_documents_step4_dashboard()
        if callable(previous_dashboard):
            return previous_dashboard()
        return module.admin_documents_dashboard_preview()

    def register(endpoint: str, rule: str, view_func, methods: list[str]) -> None:
        wrapped = login_required(admin_required(view_func))
        if endpoint in app.view_functions:
            app.view_functions[endpoint] = wrapped
            return
        app.add_url_rule(rule, endpoint=endpoint, view_func=wrapped, methods=methods)

    register(
        "admin_step4_client_invoice_create",
        "/admin/documents/step4/user/<int:user_id>/client-invoice",
        admin_step4_client_invoice_create.__wrapped__.__wrapped__,
        ["GET", "POST"],
    )
    app.view_functions["admin_kaitori_view"] = admin_kaitori_view_step4_aware
    app.view_functions["admin_documents_dashboard"] = admin_documents_dashboard_with_step4

    module.fetch_step4_client_invoice_items = _fetch_step4_items
    module.admin_documents_step4_dashboard = admin_documents_step4_dashboard
