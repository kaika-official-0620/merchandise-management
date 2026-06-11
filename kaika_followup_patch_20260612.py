# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from flask import flash, redirect, render_template, request, url_for


def apply(module: Any) -> None:
    if getattr(module, "_kaika_followup_patch_20260612_applied", False):
        return
    module._kaika_followup_patch_20260612_applied = True

    app = module.app
    get_db = module.get_db
    DATABASE_URL = getattr(module, "DATABASE_URL", None)
    RealDictCursor = getattr(module, "RealDictCursor", None)
    login_required = module.login_required
    current_user = module.current_user
    get_jst_now = getattr(module, "get_jst_now", datetime.now)
    check_password_hash = module.check_password_hash
    generate_password_hash = module.generate_password_hash

    normalize_sale_request_type = module.normalize_sale_request_type
    get_sale_request_type_label = module.get_sale_request_type_label
    get_sale_request_messages = module.get_sale_request_messages
    save_sale_request_images = module.save_sale_request_images
    validate_sale_request_payload = module.validate_sale_request_payload
    has_shipped_sale_request = module.has_shipped_sale_request
    record_sale_request_event = module.record_sale_request_event
    notify_admins_of_sale_request = getattr(module, "notify_admins_of_sale_request", lambda *_args, **_kwargs: None)
    notify_sale_request_user_status = getattr(module, "notify_sale_request_user_status", lambda *_args, **_kwargs: None)
    render_permission_denied = getattr(module, "render_permission_denied", None)

    def placeholder() -> str:
        return "%s" if DATABASE_URL else "?"

    def open_cursor(dict_rows: bool = True):
        conn = get_db()
        if DATABASE_URL and dict_rows and RealDictCursor is not None:
            cur = conn.cursor(cursor_factory=RealDictCursor)
        else:
            if not DATABASE_URL:
                conn.row_factory = sqlite3.Row
            cur = conn.cursor()
        return conn, cur

    def row_to_dict(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        return row if isinstance(row, dict) else dict(row)

    def table_exists(cur, table_name: str) -> bool:
        if DATABASE_URL:
            cur.execute("SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = %s)", (table_name,))
            row = cur.fetchone()
            return bool(row["exists"] if isinstance(row, dict) else row[0])
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name = ?", (table_name,))
        return cur.fetchone() is not None

    def column_exists(cur, table_name: str, column_name: str) -> bool:
        if DATABASE_URL:
            cur.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = %s AND column_name = %s
                )
                """,
                (table_name, column_name),
            )
            row = cur.fetchone()
            return bool(row["exists"] if isinstance(row, dict) else row[0])
        cur.execute(f"PRAGMA table_info({table_name})")
        return any((row["name"] if isinstance(row, sqlite3.Row) else row[1]) == column_name for row in cur.fetchall())

    def add_column_if_missing(cur, table_name: str, column_name: str, pg_definition: str, sqlite_definition: str) -> None:
        if not table_exists(cur, table_name) or column_exists(cur, table_name, column_name):
            return
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {pg_definition if DATABASE_URL else sqlite_definition}")

    def ensure_schema() -> None:
        conn, cur = open_cursor()
        try:
            add_column_if_missing(cur, "sale_requests", "other_cost", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0")
            add_column_if_missing(cur, "sale_requests", "user_note", "TEXT", "TEXT")
            add_column_if_missing(cur, "merchandise", "created_by", "INTEGER REFERENCES users(id)", "INTEGER")
            add_column_if_missing(cur, "merchandise", "other_cost", "INTEGER DEFAULT 0", "INTEGER DEFAULT 0")
            conn.commit()
        finally:
            cur.close()
            conn.close()

    def safe_int(value: Any, default: int = 0) -> int:
        try:
            return max(int(float(value if value is not None and value != "" else default)), 0)
        except (TypeError, ValueError):
            return max(int(default), 0)

    def search_filters() -> dict[str, str]:
        return {
            "q": (request.args.get("q") or "").strip(),
            "date_from": (request.args.get("date_from") or "").strip(),
            "date_to": (request.args.get("date_to") or "").strip(),
        }

    def append_inquiry_search_filters(where: list[str], params: list[Any], table_alias: str = "i", include_user: bool = False) -> dict[str, str]:
        filters = search_filters()
        like = f"%{filters['q']}%"
        like_op = "ILIKE" if DATABASE_URL else "LIKE"
        if filters["q"]:
            search_parts = [
                f"{table_alias}.title {like_op} {placeholder()}",
                f"{table_alias}.content {like_op} {placeholder()}",
                f"{table_alias}.category {like_op} {placeholder()}",
                f"{table_alias}.status {like_op} {placeholder()}",
                f"EXISTS (SELECT 1 FROM inquiry_replies r WHERE r.inquiry_id = {table_alias}.id AND r.content {like_op} {placeholder()})",
            ]
            params.extend([like, like, like, like, like])
            if include_user:
                search_parts.extend([
                    f"COALESCE(u.display_name, '') {like_op} {placeholder()}",
                    f"COALESCE(u.username, '') {like_op} {placeholder()}",
                    f"COALESCE(u.email, '') {like_op} {placeholder()}",
                ])
                params.extend([like, like, like])
            where.append("(" + " OR ".join(search_parts) + ")")
        if filters["date_from"]:
            where.append(f"DATE({table_alias}.created_at) >= {placeholder()}")
            params.append(filters["date_from"])
        if filters["date_to"]:
            where.append(f"DATE({table_alias}.created_at) <= {placeholder()}")
            params.append(filters["date_to"])
        return filters

    @login_required
    def inquiry_list_followup():
        inquiries: list[dict[str, Any]] = []
        filters = search_filters()
        try:
            conn, cur = open_cursor()
            where = [f"i.user_id = {placeholder()}"]
            params: list[Any] = [current_user.id]
            filters = append_inquiry_search_filters(where, params)
            cur.execute(
                f"""
                SELECT i.id, i.user_id, i.category, i.title, i.content, i.image_path,
                       i.status, i.created_at, i.updated_at
                FROM inquiries i
                WHERE {' AND '.join(where)}
                ORDER BY i.created_at DESC
                """,
                tuple(params),
            )
            for row in cur.fetchall():
                inquiry = row_to_dict(row) or {}
                for key in ("created_at", "updated_at"):
                    if inquiry.get(key) and hasattr(inquiry[key], "strftime"):
                        inquiry[key] = inquiry[key].strftime("%Y-%m-%d %H:%M:%S")
                inquiries.append(inquiry)
            cur.close()
            conn.close()
        except Exception as exc:
            print(f"[ERROR] inquiry_list_followup: {exc}", flush=True)
            flash("お問い合わせの検索中にエラーが発生しました。", "error")

        return render_template(
            "inquiry/list.html",
            inquiries=inquiries,
            categories=getattr(module, "INQUIRY_CATEGORIES", {}),
            statuses=getattr(module, "INQUIRY_STATUS", {}),
            search_filters=filters,
            is_inquiry_filtered=bool(filters.get("q") or filters.get("date_from") or filters.get("date_to")),
        )

    @login_required
    def admin_inquiries_followup():
        if not current_user.is_admin():
            if callable(render_permission_denied):
                return render_permission_denied("管理者権限が必要です")
            return redirect(url_for("index"))

        status_filter = (request.args.get("status") or "").strip()
        inquiries: list[dict[str, Any]] = []
        new_count = 0
        filters = search_filters()
        try:
            conn, cur = open_cursor()
            where: list[str] = []
            params: list[Any] = []
            if status_filter:
                where.append(f"i.status = {placeholder()}")
                params.append(status_filter)
            filters = append_inquiry_search_filters(where, params, include_user=True)
            where_sql = "WHERE " + " AND ".join(where) if where else ""
            cur.execute(
                f"""
                SELECT i.*, u.display_name, u.username, u.email,
                       (SELECT COUNT(*) FROM inquiry_replies WHERE inquiry_id = i.id) as reply_count
                FROM inquiries i
                JOIN users u ON i.user_id = u.id
                {where_sql}
                ORDER BY
                    CASE WHEN i.status = 'new' THEN 0
                         WHEN i.status = 'in_progress' THEN 1
                         ELSE 2 END,
                    i.updated_at DESC
                """,
                tuple(params),
            )
            for row in cur.fetchall():
                inquiry = row_to_dict(row) or {}
                for key in ("created_at", "updated_at"):
                    if inquiry.get(key) and hasattr(inquiry[key], "strftime"):
                        inquiry[key] = inquiry[key].strftime("%Y-%m-%d %H:%M:%S")
                inquiries.append(inquiry)
            cur.execute("SELECT COUNT(*) FROM inquiries WHERE status = 'new'")
            count_row = cur.fetchone()
            new_count = int((count_row[0] if count_row and not isinstance(count_row, dict) else (count_row or {}).get("count", 0)) or 0)
            cur.close()
            conn.close()
        except Exception as exc:
            print(f"[ERROR] admin_inquiries_followup: {exc}", flush=True)
            flash("お問い合わせ管理の検索中にエラーが発生しました。", "error")

        return render_template(
            "admin/inquiries.html",
            inquiries=inquiries,
            categories=getattr(module, "INQUIRY_CATEGORIES", {}),
            statuses=getattr(module, "INQUIRY_STATUS", {}),
            status_filter=status_filter,
            new_count=new_count,
            search_filters=filters,
            is_inquiry_filtered=bool(status_filter or filters.get("q") or filters.get("date_from") or filters.get("date_to")),
        )

    original_profile = app.view_functions.get("profile")

    @login_required
    def profile_followup():
        if not (current_user.is_admin() or current_user.is_owner()):
            return original_profile()

        user_info: dict[str, Any] = {}
        if request.method == "POST":
            current_password = request.form.get("current_password") or ""
            new_password = request.form.get("new_password") or ""
            if current_password and new_password:
                conn, cur = open_cursor()
                try:
                    cur.execute(f"SELECT * FROM users WHERE id = {placeholder()}", (current_user.id,))
                    user = row_to_dict(cur.fetchone())
                    if user and check_password_hash(user.get("password_hash") or "", current_password):
                        cur.execute(f"UPDATE users SET password_hash = {placeholder()} WHERE id = {placeholder()}",
                                    (generate_password_hash(new_password), current_user.id))
                        conn.commit()
                        flash("パスワードを変更しました。", "success")
                    else:
                        flash("現在のパスワードが正しくありません。", "error")
                finally:
                    cur.close()
                    conn.close()
            else:
                flash("管理者設定で変更できる項目はパスワードのみです。", "info")
            return redirect(url_for("profile"))

        try:
            conn, cur = open_cursor()
            cur.execute(f"SELECT id, username, email, display_name, role, last_name, first_name FROM users WHERE id = {placeholder()}",
                        (current_user.id,))
            user_info = row_to_dict(cur.fetchone()) or {}
            cur.close()
            conn.close()
        except Exception as exc:
            print(f"[ERROR] profile_followup: {exc}", flush=True)
            user_info = {
                "username": getattr(current_user, "username", ""),
                "email": getattr(current_user, "email", ""),
                "display_name": getattr(current_user, "display_name", ""),
                "role": getattr(current_user, "role", ""),
            }
        return render_template("admin/profile_admin.html", profile_user=user_info)

    @login_required
    def submit_sale_request_followup(item_id: int):
        ensure_schema()
        request_type = normalize_sale_request_type(request.form.get("request_type"))
        sale_price = request.form.get("sale_price", type=int)
        shipping_cost = request.form.get("shipping_cost", type=int)
        commission = request.form.get("commission", type=int)
        other_cost = request.form.get("other_cost", type=int)
        user_note = (request.form.get("user_note") or "").strip()
        qr_image = request.files.get("qr_image")
        qr_image2 = request.files.get("qr_image2")

        if request_type == "shipping_request":
            sale_price = shipping_cost = commission = other_cost = 0
            user_note = ""
        else:
            sale_price = safe_int(sale_price)
            shipping_cost = safe_int(shipping_cost)
            commission = safe_int(commission)
            other_cost = safe_int(other_cost)
            if sale_price <= 0:
                flash("取引完了報告では実際の売上金額を入力してください。", "error")
                return redirect(url_for("index"))

        conn = None
        try:
            conn, cur = open_cursor()
            cur.execute(
                f"SELECT * FROM merchandise WHERE id = {placeholder()} AND user_id = {placeholder()}",
                (item_id, current_user.id),
            )
            item = row_to_dict(cur.fetchone())
            if not item:
                flash("商品が見つかりません。", "error")
                cur.close()
                conn.close()
                return redirect(url_for("index"))

            if request_type == "completion_report" and not (item.get("is_shipped") or has_shipped_sale_request(cur, item_id)):
                flash("先に発送依頼の承認を受けてから、取引完了報告を送信してください。", "error")
                cur.close()
                conn.close()
                return redirect(url_for("index"))

            cur.execute(
                f"""
                SELECT id
                FROM sale_requests
                WHERE merchandise_id = {placeholder()}
                  AND request_type = {placeholder()}
                  AND status = 'pending'
                LIMIT 1
                """,
                (item_id, request_type),
            )
            if cur.fetchone():
                flash(f"この商品は既に{get_sale_request_type_label(request_type)}を送信済みです。", "error")
                cur.close()
                conn.close()
                return redirect(url_for("index"))

            qr_image_path, qr_image_path2 = save_sale_request_images(qr_image, qr_image2)
            validation_error = validate_sale_request_payload(
                request_type, sale_price, shipping_cost, commission, qr_image_path, qr_image_path2
            )
            if validation_error:
                flash(validation_error, "error")
                cur.close()
                conn.close()
                return redirect(url_for("index"))

            if DATABASE_URL:
                cur.execute(
                    """
                    INSERT INTO sale_requests (
                        merchandise_id, user_id, request_type, sale_price, shipping_cost, commission, other_cost,
                        qr_image_path, qr_image_path2, user_note, status, shipment_status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                    RETURNING id
                    """,
                    (
                        item_id, current_user.id, request_type, sale_price, shipping_cost, commission, other_cost,
                        qr_image_path, qr_image_path2, user_note,
                        "pending_review" if request_type == "shipping_request" else None,
                    ),
                )
                new_row = cur.fetchone()
                sale_request_id = new_row["id"] if isinstance(new_row, dict) else new_row[0]
            else:
                cur.execute(
                    """
                    INSERT INTO sale_requests (
                        merchandise_id, user_id, request_type, sale_price, shipping_cost, commission, other_cost,
                        qr_image_path, qr_image_path2, user_note, status, shipment_status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        item_id, current_user.id, request_type, sale_price, shipping_cost, commission, other_cost,
                        qr_image_path, qr_image_path2, user_note,
                        "pending_review" if request_type == "shipping_request" else None,
                    ),
                )
                sale_request_id = cur.lastrowid

            record_sale_request_event(conn, sale_request_id, "submitted", actor_user_id=current_user.id,
                                      note=get_sale_request_type_label(request_type))
            conn.commit()
            cur.close()
            conn.close()
            notify_admins_of_sale_request(item, request_type)
            flash(get_sale_request_messages(request_type)["user_submit_success"], "success")
        except Exception as exc:
            print(f"[ERROR] submit_sale_request_followup: {exc}", flush=True)
            if conn:
                try:
                    conn.rollback()
                    conn.close()
                except Exception:
                    pass
            flash(f"{get_sale_request_type_label(request_type)}の送信中にエラーが発生しました。", "error")
        return redirect(url_for("index"))

    @login_required
    def edit_sale_request_followup(request_id: int):
        ensure_schema()
        sale_price = request.form.get("sale_price", type=int)
        shipping_cost = request.form.get("shipping_cost", type=int)
        commission = request.form.get("commission", type=int)
        other_cost = request.form.get("other_cost", type=int)
        user_note = (request.form.get("user_note") or "").strip()
        qr_image = request.files.get("qr_image")
        qr_image2 = request.files.get("qr_image2")

        conn = None
        try:
            conn, cur = open_cursor()
            cur.execute(
                f"SELECT * FROM sale_requests WHERE id = {placeholder()} AND user_id = {placeholder()} AND status = 'pending'",
                (request_id, current_user.id),
            )
            sale_request = row_to_dict(cur.fetchone())
            if not sale_request:
                flash("送信内容が見つからないか、既に処理済みです。", "error")
                cur.close()
                conn.close()
                return redirect(url_for("index"))

            request_type = normalize_sale_request_type(sale_request.get("request_type"))
            if request_type == "shipping_request":
                sale_price = shipping_cost = commission = other_cost = 0
                user_note = ""
            else:
                sale_price = safe_int(sale_price if sale_price is not None else sale_request.get("sale_price"))
                shipping_cost = safe_int(shipping_cost if shipping_cost is not None else sale_request.get("shipping_cost"))
                commission = safe_int(commission if commission is not None else sale_request.get("commission"))
                other_cost = safe_int(other_cost if other_cost is not None else sale_request.get("other_cost"))
                if sale_price <= 0:
                    flash("取引完了報告では実際の売上金額を入力してください。", "error")
                    cur.close()
                    conn.close()
                    return redirect(url_for("index"))

            qr_image_path, qr_image_path2 = save_sale_request_images(qr_image, qr_image2, sale_request)
            validation_error = validate_sale_request_payload(
                request_type, sale_price, shipping_cost, commission, qr_image_path, qr_image_path2
            )
            if validation_error:
                flash(validation_error, "error")
                cur.close()
                conn.close()
                return redirect(url_for("index"))

            cur.execute(
                f"""
                UPDATE sale_requests
                SET sale_price = {placeholder()}, shipping_cost = {placeholder()}, commission = {placeholder()},
                    other_cost = {placeholder()}, user_note = {placeholder()},
                    qr_image_path = {placeholder()}, qr_image_path2 = {placeholder()}
                WHERE id = {placeholder()}
                """,
                (sale_price, shipping_cost, commission, other_cost, user_note, qr_image_path, qr_image_path2, request_id),
            )
            record_sale_request_event(conn, request_id, "updated", actor_user_id=current_user.id,
                                      note=get_sale_request_type_label(request_type))
            conn.commit()
            cur.close()
            conn.close()
            flash(get_sale_request_messages(request_type)["user_update_success"], "success")
        except Exception as exc:
            print(f"[ERROR] edit_sale_request_followup: {exc}", flush=True)
            if conn:
                try:
                    conn.rollback()
                    conn.close()
                except Exception:
                    pass
            flash("送信内容の更新中にエラーが発生しました。", "error")
        return redirect(url_for("index"))

    @login_required
    def approve_sale_request_followup(request_id: int):
        ensure_schema()
        if not current_user.is_admin():
            if callable(render_permission_denied):
                return render_permission_denied("管理者権限が必要です")
            return redirect(url_for("index"))

        admin_note = request.form.get("admin_note", "")
        redirect_to = request.form.get("redirect_to") or request.referrer or url_for("admin_shipping_requests")
        approved_sale_price_input = request.form.get("approved_sale_price", type=int)
        approved_shipping_cost_input = request.form.get("approved_shipping_cost", type=int)
        approved_commission_input = request.form.get("approved_commission", type=int)
        approved_other_cost_input = request.form.get("approved_other_cost", type=int)

        conn = None
        request_type = "shipping_request"
        try:
            conn, cur = open_cursor()
            cur.execute(
                f"""
                SELECT sr.*, m.product_name, m.purchase_price
                FROM sale_requests sr
                JOIN merchandise m ON sr.merchandise_id = m.id
                WHERE sr.id = {placeholder()}
                """,
                (request_id,),
            )
            sale_request = row_to_dict(cur.fetchone())
            if not sale_request:
                flash("依頼が見つかりません。", "error")
                cur.close()
                conn.close()
                return redirect(redirect_to)

            request_type = normalize_sale_request_type(sale_request.get("request_type"))
            merchandise_id = sale_request["merchandise_id"]
            request_user_id = sale_request["user_id"]
            approved_sale_price = safe_int(sale_request.get("sale_price"))
            approved_shipping_cost = safe_int(sale_request.get("shipping_cost"))
            approved_commission = safe_int(sale_request.get("commission"))
            approved_other_cost = safe_int(sale_request.get("other_cost"))

            if request_type == "completion_report":
                if approved_sale_price_input is not None:
                    approved_sale_price = approved_sale_price_input
                if approved_shipping_cost_input is not None:
                    approved_shipping_cost = approved_shipping_cost_input
                if approved_commission_input is not None:
                    approved_commission = approved_commission_input
                if approved_other_cost_input is not None:
                    approved_other_cost = approved_other_cost_input
                if approved_sale_price <= 0:
                    flash("取引完了報告を承認するには売上金額を入力してください。", "error")
                    cur.close()
                    conn.close()
                    return redirect(redirect_to)
            else:
                approved_sale_price = approved_shipping_cost = approved_commission = approved_other_cost = 0

            combined_fee_for_profit = approved_commission + approved_other_cost
            if request_type == "completion_report":
                cur.execute(
                    f"""
                    UPDATE sale_requests
                    SET status = 'approved', processed_at = {placeholder()}, processed_by = {placeholder()}, admin_note = {placeholder()},
                        sale_price = {placeholder()}, shipping_cost = {placeholder()}, commission = {placeholder()}, other_cost = {placeholder()}
                    WHERE id = {placeholder()}
                    """,
                    (
                        get_jst_now(), current_user.id, admin_note,
                        approved_sale_price, approved_shipping_cost, approved_commission, approved_other_cost,
                        request_id,
                    ),
                )
                cur.execute(
                    f"""
                    UPDATE merchandise
                    SET is_listed = {placeholder()}, sale_price = {placeholder()}, shipping_cost = {placeholder()},
                        commission = {placeholder()}, other_cost = {placeholder()},
                        is_shipped = {placeholder()}, sale_date = {placeholder()}, updated_at = {placeholder()}, updated_by = {placeholder()}
                    WHERE id = {placeholder()}
                    """,
                    (
                        True if DATABASE_URL else 1,
                        approved_sale_price,
                        approved_shipping_cost,
                        combined_fee_for_profit,
                        approved_other_cost,
                        True if DATABASE_URL else 1,
                        get_jst_now().date() if DATABASE_URL else get_jst_now().strftime("%Y-%m-%d"),
                        get_jst_now() if DATABASE_URL else get_jst_now().strftime("%Y-%m-%d %H:%M:%S"),
                        current_user.id,
                        merchandise_id,
                    ),
                )
            else:
                cur.execute(
                    f"""
                    UPDATE sale_requests
                    SET status = 'approved', processed_at = {placeholder()}, processed_by = {placeholder()}, admin_note = {placeholder()},
                        sale_price = 0, shipping_cost = 0, commission = 0, other_cost = 0,
                        shipment_status = 'approved_waiting_shipment'
                    WHERE id = {placeholder()}
                    """,
                    (get_jst_now(), current_user.id, admin_note, request_id),
                )
                cur.execute(
                    f"UPDATE merchandise SET updated_at = {placeholder()}, updated_by = {placeholder()} WHERE id = {placeholder()}",
                    (get_jst_now(), current_user.id, merchandise_id),
                )

            record_sale_request_event(conn, request_id, "approved", actor_user_id=current_user.id,
                                      note=get_sale_request_type_label(request_type))
            conn.commit()
            cur.close()
            conn.close()
            notify_sale_request_user_status({"id": merchandise_id, "product_name": sale_request.get("product_name")}, request_user_id, "approved")
            flash(get_sale_request_messages(request_type)["admin_approve_success"], "success")
        except Exception as exc:
            print(f"[ERROR] approve_sale_request_followup: {exc}", flush=True)
            if conn:
                try:
                    conn.rollback()
                    conn.close()
                except Exception:
                    pass
            flash("承認処理中にエラーが発生しました。", "error")
        return redirect(redirect_to)

    ensure_schema()
    app.view_functions["inquiry_list"] = inquiry_list_followup
    app.view_functions["admin_inquiries"] = admin_inquiries_followup
    if original_profile is not None:
        app.view_functions["profile"] = profile_followup
    app.view_functions["submit_sale_request"] = submit_sale_request_followup
    app.view_functions["edit_sale_request"] = edit_sale_request_followup
    app.view_functions["approve_sale_request"] = approve_sale_request_followup
