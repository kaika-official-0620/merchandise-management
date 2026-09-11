# -*- coding: utf-8 -*-
"""Runtime integrity patch for the proxy purchasing service.

The production entry point is ``render_app:app``.  This patch is applied last
from render_app so every proxy route uses one auction-scoped history model and
one settlement routine, regardless of whether settlement is triggered by a
request, the scheduler, or an administrator.
"""
from __future__ import annotations

from datetime import datetime
import json
import threading
from typing import Any, Iterable


INVALID_CLIENT_STATUSES = {
    "past_due",
    "canceled",
    "cancelled",
    "disabled",
    "deleted",
    "suspended",
}


def apply(module: Any) -> None:
    if getattr(module, "_proxy_service_integrity_patch_20260822_applied", False):
        return
    module._proxy_service_integrity_patch_20260822_applied = True

    app = module.app
    request = module.request
    current_user = module.current_user
    jsonify = module.jsonify
    redirect = module.redirect
    url_for = module.url_for
    flash = module.flash
    render_template = module.render_template
    login_required = module.login_required
    RealDictCursor = getattr(module, "RealDictCursor", None)
    is_postgres = bool(getattr(module, "DATABASE_URL", None))

    original_create_reflected_item = module.create_proxy_service_reflected_item
    original_annotate_items = module.annotate_proxy_service_items
    original_history_datasets = module.build_proxy_service_history_datasets
    original_public_sections = module.build_public_proxy_service_sections
    original_is_user_allowed = module.is_proxy_service_user_allowed
    original_settings_view = app.view_functions.get("admin_proxy_service_settings")
    original_start_view = app.view_functions.get("admin_proxy_service_start")
    original_visibility_view = app.view_functions.get("admin_proxy_service_visibility")
    original_end_now_view = app.view_functions.get("admin_proxy_service_end_now")
    original_init_scheduler = getattr(module, "init_scheduler", None)

    def row_dict(row):
        if row is None:
            return None
        return dict(row)

    def cursor_for(conn, *, rows=False):
        if is_postgres and rows:
            return conn.cursor(cursor_factory=RealDictCursor)
        return conn.cursor()

    def execute(cur, postgres_sql, sqlite_sql, params=()):
        cur.execute(postgres_sql if is_postgres else sqlite_sql, params)

    def auction_name_display(auction_id, value=None):
        name = str(value or "").strip()
        return name or f"代行仕入れオークション #{auction_id}"

    schema_state = {"ready": False}

    def ensure_schema(conn=None):
        if schema_state["ready"]:
            return
        owns_connection = conn is None
        conn = conn or module.get_db()
        cur = cursor_for(conn)
        try:
            if is_postgres:
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name VARCHAR(100)")
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name VARCHAR(100)")
                cur.execute(
                    "ALTER TABLE proxy_service_bids ADD COLUMN IF NOT EXISTS auction_id INTEGER REFERENCES proxy_service_settings(id)"
                )
                cur.execute(
                    "ALTER TABLE merchandise ADD COLUMN IF NOT EXISTS proxy_source_auction_id INTEGER REFERENCES proxy_service_settings(id)"
                )
                cur.execute(
                    """
                    UPDATE proxy_service_bids b
                    SET auction_id = m.auction_id
                    FROM merchandise m
                    JOIN proxy_service_settings ps ON ps.id = m.auction_id
                    WHERE b.auction_id IS NULL
                      AND b.merchandise_id = m.id
                      AND m.auction_id IS NOT NULL
                      AND ps.start_datetime IS NOT NULL
                      AND b.created_at IS NOT NULL
                      AND b.created_at >= ps.start_datetime
                      AND (ps.end_datetime IS NULL OR b.created_at <= ps.end_datetime)
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS proxy_service_auction_items (
                        id BIGSERIAL PRIMARY KEY,
                        auction_id INTEGER NOT NULL REFERENCES proxy_service_settings(id) ON DELETE CASCADE,
                        merchandise_id INTEGER NOT NULL REFERENCES merchandise(id) ON DELETE CASCADE,
                        outcome_status VARCHAR(32) NOT NULL DEFAULT 'active',
                        winner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                        winner_name TEXT,
                        winning_price INTEGER,
                        reflected_item_id INTEGER REFERENCES merchandise(id) ON DELETE SET NULL,
                        snapshot_product_name TEXT,
                        snapshot_brand_name TEXT,
                        snapshot_photo_path TEXT,
                        snapshot_listing_price INTEGER,
                        snapshot_purchase_price INTEGER,
                        listed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        finalized_at TIMESTAMP,
                        released_at TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (auction_id, merchandise_id)
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_proxy_bids_auction_item ON proxy_service_bids(auction_id, merchandise_id, bid_amount DESC, id ASC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_proxy_runs_auction_outcome ON proxy_service_auction_items(auction_id, outcome_status)"
                )
                cur.execute(
                    """
                    INSERT INTO proxy_service_auction_items (
                        auction_id, merchandise_id, outcome_status,
                        snapshot_product_name, snapshot_brand_name, snapshot_photo_path,
                        snapshot_listing_price, snapshot_purchase_price
                    )
                    SELECT m.auction_id, m.id,
                           CASE
                               WHEN m.sale_date IS NULL THEN 'active'
                               WHEN COALESCE(m.sale_type, '') = 'fixed' THEN 'fixed_sold'
                               ELSE 'auction_won'
                           END,
                           m.product_name, m.brand_name, m.photo_path,
                           m.listing_price, m.purchase_price
                    FROM merchandise m
                    WHERE m.auction_id IS NOT NULL
                      AND COALESCE(NULLIF(m.scope, ''), 'admin') = 'admin'
                    ON CONFLICT (auction_id, merchandise_id) DO NOTHING
                    """
                )
                cur.execute(
                    """
                    UPDATE merchandise child
                    SET proxy_source_auction_id = parent.auction_id
                    FROM merchandise parent
                    WHERE child.proxy_source_auction_id IS NULL
                      AND child.proxy_parent_item_id = parent.id
                      AND parent.auction_id IS NOT NULL
                    """
                )
            else:
                user_columns = {row[1] for row in cur.execute("PRAGMA table_info(users)").fetchall()}
                if "last_name" not in user_columns:
                    cur.execute("ALTER TABLE users ADD COLUMN last_name TEXT")
                if "first_name" not in user_columns:
                    cur.execute("ALTER TABLE users ADD COLUMN first_name TEXT")
                bid_columns = {row[1] for row in cur.execute("PRAGMA table_info(proxy_service_bids)").fetchall()}
                if "auction_id" not in bid_columns:
                    cur.execute("ALTER TABLE proxy_service_bids ADD COLUMN auction_id INTEGER")
                merchandise_columns = {row[1] for row in cur.execute("PRAGMA table_info(merchandise)").fetchall()}
                if "proxy_source_auction_id" not in merchandise_columns:
                    cur.execute("ALTER TABLE merchandise ADD COLUMN proxy_source_auction_id INTEGER")
                cur.execute(
                    """
                    UPDATE proxy_service_bids
                    SET auction_id = (
                        SELECT merchandise.auction_id
                        FROM merchandise
                        JOIN proxy_service_settings ps ON ps.id = merchandise.auction_id
                        WHERE merchandise.id = proxy_service_bids.merchandise_id
                          AND ps.start_datetime IS NOT NULL
                          AND proxy_service_bids.created_at IS NOT NULL
                          AND datetime(proxy_service_bids.created_at) >= datetime(ps.start_datetime)
                          AND (ps.end_datetime IS NULL OR datetime(proxy_service_bids.created_at) <= datetime(ps.end_datetime))
                    )
                    WHERE auction_id IS NULL
                      AND EXISTS (
                          SELECT 1
                          FROM merchandise
                          JOIN proxy_service_settings ps ON ps.id = merchandise.auction_id
                          WHERE merchandise.id = proxy_service_bids.merchandise_id
                            AND ps.start_datetime IS NOT NULL
                            AND proxy_service_bids.created_at IS NOT NULL
                            AND datetime(proxy_service_bids.created_at) >= datetime(ps.start_datetime)
                            AND (ps.end_datetime IS NULL OR datetime(proxy_service_bids.created_at) <= datetime(ps.end_datetime))
                      )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS proxy_service_auction_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        auction_id INTEGER NOT NULL,
                        merchandise_id INTEGER NOT NULL,
                        outcome_status TEXT NOT NULL DEFAULT 'active',
                        winner_user_id INTEGER,
                        winner_name TEXT,
                        winning_price INTEGER,
                        reflected_item_id INTEGER,
                        snapshot_product_name TEXT,
                        snapshot_brand_name TEXT,
                        snapshot_photo_path TEXT,
                        snapshot_listing_price INTEGER,
                        snapshot_purchase_price INTEGER,
                        listed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        finalized_at TIMESTAMP,
                        released_at TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE (auction_id, merchandise_id),
                        FOREIGN KEY (auction_id) REFERENCES proxy_service_settings(id) ON DELETE CASCADE,
                        FOREIGN KEY (merchandise_id) REFERENCES merchandise(id) ON DELETE CASCADE
                    )
                    """
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_proxy_bids_auction_item ON proxy_service_bids(auction_id, merchandise_id, bid_amount DESC, id ASC)"
                )
                cur.execute(
                    "CREATE INDEX IF NOT EXISTS idx_proxy_runs_auction_outcome ON proxy_service_auction_items(auction_id, outcome_status)"
                )
                cur.execute(
                    """
                    INSERT OR IGNORE INTO proxy_service_auction_items (
                        auction_id, merchandise_id, outcome_status,
                        snapshot_product_name, snapshot_brand_name, snapshot_photo_path,
                        snapshot_listing_price, snapshot_purchase_price
                    )
                    SELECT m.auction_id, m.id,
                           CASE
                               WHEN m.sale_date IS NULL THEN 'active'
                               WHEN COALESCE(m.sale_type, '') = 'fixed' THEN 'fixed_sold'
                               ELSE 'auction_won'
                           END,
                           m.product_name, m.brand_name, m.photo_path,
                           m.listing_price, m.purchase_price
                    FROM merchandise m
                    WHERE m.auction_id IS NOT NULL
                      AND COALESCE(NULLIF(m.scope, ''), 'admin') = 'admin'
                    """
                )
                cur.execute(
                    """
                    UPDATE merchandise
                    SET proxy_source_auction_id = (
                        SELECT parent.auction_id
                        FROM merchandise parent
                        WHERE parent.id = merchandise.proxy_parent_item_id
                    )
                    WHERE proxy_source_auction_id IS NULL
                      AND proxy_parent_item_id IS NOT NULL
                    """
                )
            # Older bid rows did not store user_id. Backfill only when the
            # bidder label uniquely identifies an eligible client; ambiguous
            # rows remain explicit review cases instead of being misassigned.
            map_cur = cursor_for(conn, rows=True)
            map_cur.execute(
                "SELECT id, username, display_name, role, subscription_status FROM users WHERE role = 'user'"
            )
            identity_map = {}
            for raw_user in map_cur.fetchall():
                user = row_dict(raw_user)
                status = str(user.get("subscription_status") or "inactive").strip().lower()
                if status in INVALID_CLIENT_STATUSES:
                    continue
                # username/member number is the only stable legacy identity.
                # A mutable/non-unique display name must never transfer ownership.
                for identity in (user.get("username"),):
                    key = str(identity or "").strip().lower()
                    if not key:
                        continue
                    if key in identity_map and identity_map[key] != int(user["id"]):
                        identity_map[key] = None
                    else:
                        identity_map[key] = int(user["id"])
            map_cur.execute("SELECT id, bidder_name FROM proxy_service_bids WHERE user_id IS NULL")
            for raw_bid in map_cur.fetchall():
                bid = row_dict(raw_bid)
                bidder_key = str(bid.get("bidder_name") or "").strip()
                if bidder_key.endswith("（購入）"):
                    bidder_key = bidder_key[:-4].strip()
                matched_user_id = identity_map.get(bidder_key.lower())
                if matched_user_id:
                    execute(
                        map_cur,
                        "UPDATE proxy_service_bids SET user_id = %s WHERE id = %s AND user_id IS NULL",
                        "UPDATE proxy_service_bids SET user_id = ? WHERE id = ? AND user_id IS NULL",
                        (matched_user_id, bid["id"]),
                    )
            map_cur.close()
            module.ensure_proxy_service_keisan_columns(conn)
            if owns_connection:
                conn.commit()
                schema_state["ready"] = True
        finally:
            cur.close()
            if owns_connection:
                conn.close()

    def valid_client_status_sql(alias="u"):
        excluded = ", ".join(f"'{value}'" for value in sorted(INVALID_CLIENT_STATUSES))
        return (
            f"{alias}.role = 'user' AND "
            f"LOWER(COALESCE(NULLIF(TRIM({alias}.subscription_status), ''), 'inactive')) NOT IN ({excluded})"
        )

    def fetch_valid_clients(conn, auction_id=None):
        module.ensure_proxy_service_auction_user_table(conn)
        use_auction_scope = bool(auction_id and module.proxy_service_auction_has_user_config(conn, auction_id))
        cur = cursor_for(conn, rows=True)
        if auction_id and use_auction_scope:
            selected_expr = (
                "EXISTS (SELECT 1 FROM proxy_service_auction_users pau "
                "WHERE pau.auction_id = %s AND pau.user_id = u.id AND pau.is_enabled = TRUE)"
                if is_postgres
                else
                "EXISTS (SELECT 1 FROM proxy_service_auction_users pau "
                "WHERE pau.auction_id = ? AND pau.user_id = u.id AND pau.is_enabled = 1)"
            )
            params = (auction_id,)
        elif auction_id:
            selected_expr = (
                "EXISTS (SELECT 1 FROM proxy_service_users psu WHERE psu.user_id = u.id AND psu.is_enabled = TRUE)"
                if is_postgres
                else
                "EXISTS (SELECT 1 FROM proxy_service_users psu WHERE psu.user_id = u.id AND psu.is_enabled = 1)"
            )
            params = ()
        else:
            selected_expr = "FALSE" if is_postgres else "0"
            params = ()
        cur.execute(
            f"""
            SELECT u.id, u.username, u.display_name, u.last_name, u.first_name,
                   u.role, u.subscription_status,
                   {selected_expr} AS is_selected
            FROM users u
            WHERE {valid_client_status_sql('u')}
            ORDER BY
                CASE WHEN NULLIF(TRIM(COALESCE(u.last_name, '') || ' ' || COALESCE(u.first_name, '')), '') IS NULL
                           AND NULLIF(TRIM(COALESCE(u.display_name, '')), '') IS NULL THEN 1 ELSE 0 END,
                LOWER(COALESCE(
                    NULLIF(TRIM(COALESCE(u.last_name, '') || ' ' || COALESCE(u.first_name, '')), ''),
                    NULLIF(TRIM(u.display_name), ''),
                    u.username
                )),
                LOWER(COALESCE(u.username, '')),
                u.id
            """,
            params,
        )
        users = [row_dict(row) for row in cur.fetchall()]
        cur.close()
        for user in users:
            raw_name = " ".join(
                str(user.get(key) or "").strip()
                for key in ("last_name", "first_name")
                if str(user.get(key) or "").strip()
            )
            display_name = str(user.get("display_name") or "").strip()
            if not raw_name and display_name and not module.is_placeholder_user_display_name(
                display_name,
                username=user.get("username"),
                role=user.get("role"),
            ):
                raw_name = display_name
            user["display_name_label"] = raw_name or "氏名未登録"
            user["member_number"] = str(user.get("username") or "").strip() or "未登録"
            user["user_id_display"] = str(user.get("id"))
            user["search_text"] = " ".join(
                [raw_name, user["member_number"], user["user_id_display"]]
            ).lower()
            user["is_selected"] = bool(user.get("is_selected"))
        return users, use_auction_scope

    def validate_client_ids(conn, raw_ids: Iterable[Any]):
        normalized = []
        for raw_id in raw_ids:
            try:
                user_id = int(str(raw_id).strip())
            except (TypeError, ValueError):
                raise ValueError("対象ユーザーIDが不正です")
            if user_id <= 0:
                raise ValueError("対象ユーザーIDが不正です")
            if user_id not in normalized:
                normalized.append(user_id)
        if not normalized:
            return []
        placeholders = ",".join(["%s" if is_postgres else "?"] * len(normalized))
        cur = cursor_for(conn, rows=True)
        cur.execute(
            f"SELECT id FROM users u WHERE id IN ({placeholders}) AND {valid_client_status_sql('u')}",
            tuple(normalized),
        )
        found = {int(row_dict(row)["id"]) for row in cur.fetchall()}
        cur.close()
        if found != set(normalized):
            raise ValueError("有効な一般クライアント以外は対象にできません")
        return normalized

    def is_user_allowed_with_integrity(conn, user_id, auction_id=None):
        try:
            normalized_user_id = int(user_id)
        except (TypeError, ValueError):
            return False
        cur = cursor_for(conn, rows=True)
        execute(
            cur,
            f"SELECT id FROM users u WHERE id = %s AND {valid_client_status_sql('u')}",
            f"SELECT id FROM users u WHERE id = ? AND {valid_client_status_sql('u')}",
            (normalized_user_id,),
        )
        eligible = cur.fetchone() is not None
        cur.close()
        if not eligible:
            return False
        if auction_id and module.proxy_service_auction_has_user_config(conn, auction_id):
            return bool(original_is_user_allowed(conn, normalized_user_id, auction_id))
        if auction_id:
            cur = cursor_for(conn, rows=True)
            execute(
                cur,
                "SELECT 1 FROM proxy_service_users WHERE user_id = %s AND is_enabled = TRUE",
                "SELECT 1 FROM proxy_service_users WHERE user_id = ? AND is_enabled = 1",
                (normalized_user_id,),
            )
            allowed = cur.fetchone() is not None
            cur.close()
            return allowed
        return bool(original_is_user_allowed(conn, normalized_user_id, auction_id))

    def fetch_proxy_service_target_users(conn, auction_id):
        return fetch_valid_clients(conn, auction_id)

    def upsert_run(cur, auction_id, item, *, outcome_status="active"):
        item = row_dict(item) or {}
        params = (
            auction_id,
            item.get("id"),
            outcome_status,
            item.get("product_name"),
            item.get("brand_name"),
            item.get("photo_path"),
            module.normalize_proxy_service_price_value(item.get("listing_price")),
            module.normalize_proxy_service_price_value(item.get("purchase_price")),
        )
        if is_postgres:
            cur.execute(
                """
                INSERT INTO proxy_service_auction_items (
                    auction_id, merchandise_id, outcome_status,
                    snapshot_product_name, snapshot_brand_name, snapshot_photo_path,
                    snapshot_listing_price, snapshot_purchase_price
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (auction_id, merchandise_id) DO UPDATE SET
                    outcome_status = CASE
                        WHEN proxy_service_auction_items.outcome_status IN ('auction_won', 'fixed_sold', 'ended_no_bid', 'needs_review')
                        THEN proxy_service_auction_items.outcome_status
                        ELSE EXCLUDED.outcome_status
                    END,
                    snapshot_product_name = COALESCE(proxy_service_auction_items.snapshot_product_name, EXCLUDED.snapshot_product_name),
                    snapshot_brand_name = COALESCE(proxy_service_auction_items.snapshot_brand_name, EXCLUDED.snapshot_brand_name),
                    snapshot_photo_path = COALESCE(proxy_service_auction_items.snapshot_photo_path, EXCLUDED.snapshot_photo_path),
                    snapshot_listing_price = COALESCE(proxy_service_auction_items.snapshot_listing_price, EXCLUDED.snapshot_listing_price),
                    snapshot_purchase_price = COALESCE(proxy_service_auction_items.snapshot_purchase_price, EXCLUDED.snapshot_purchase_price),
                    updated_at = CURRENT_TIMESTAMP
                """,
                params,
            )
        else:
            cur.execute(
                """
                INSERT INTO proxy_service_auction_items (
                    auction_id, merchandise_id, outcome_status,
                    snapshot_product_name, snapshot_brand_name, snapshot_photo_path,
                    snapshot_listing_price, snapshot_purchase_price
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (auction_id, merchandise_id) DO UPDATE SET
                    outcome_status = CASE
                        WHEN proxy_service_auction_items.outcome_status IN ('auction_won', 'fixed_sold', 'ended_no_bid', 'needs_review')
                        THEN proxy_service_auction_items.outcome_status
                        ELSE excluded.outcome_status
                    END,
                    snapshot_product_name = COALESCE(proxy_service_auction_items.snapshot_product_name, excluded.snapshot_product_name),
                    snapshot_brand_name = COALESCE(proxy_service_auction_items.snapshot_brand_name, excluded.snapshot_brand_name),
                    snapshot_photo_path = COALESCE(proxy_service_auction_items.snapshot_photo_path, excluded.snapshot_photo_path),
                    snapshot_listing_price = COALESCE(proxy_service_auction_items.snapshot_listing_price, excluded.snapshot_listing_price),
                    snapshot_purchase_price = COALESCE(proxy_service_auction_items.snapshot_purchase_price, excluded.snapshot_purchase_price),
                    updated_at = CURRENT_TIMESTAMP
                """,
                params,
            )

    def fetch_proxy_service_items(conn, auction_id):
        ensure_schema(conn)
        cur = cursor_for(conn, rows=True)
        execute(
            cur,
            """
            SELECT m.*, r.id AS proxy_run_id, r.auction_id AS run_auction_id,
                   r.outcome_status AS proxy_run_status, r.winner_user_id AS run_winner_user_id,
                   r.winner_name AS run_winner_name, r.winning_price AS run_winning_price,
                   r.reflected_item_id AS run_reflected_item_id, r.finalized_at AS run_finalized_at,
                   r.snapshot_product_name, r.snapshot_brand_name, r.snapshot_photo_path,
                   r.snapshot_listing_price, r.snapshot_purchase_price,
                   ps.auction_name AS run_auction_name, COALESCE(ps.sale_mode, 'auction') AS run_sale_mode,
                   COALESCE(owner.display_name, owner.username, '不明') AS owner_name
            FROM proxy_service_auction_items r
            JOIN merchandise m ON m.id = r.merchandise_id
            JOIN proxy_service_settings ps ON ps.id = r.auction_id
            LEFT JOIN users owner ON owner.id = m.user_id
            WHERE r.auction_id = %s AND r.outcome_status <> 'withdrawn'
            ORDER BY r.id DESC
            """,
            """
            SELECT m.*, r.id AS proxy_run_id, r.auction_id AS run_auction_id,
                   r.outcome_status AS proxy_run_status, r.winner_user_id AS run_winner_user_id,
                   r.winner_name AS run_winner_name, r.winning_price AS run_winning_price,
                   r.reflected_item_id AS run_reflected_item_id, r.finalized_at AS run_finalized_at,
                   r.snapshot_product_name, r.snapshot_brand_name, r.snapshot_photo_path,
                   r.snapshot_listing_price, r.snapshot_purchase_price,
                   ps.auction_name AS run_auction_name, COALESCE(ps.sale_mode, 'auction') AS run_sale_mode,
                   COALESCE(owner.display_name, owner.username, '不明') AS owner_name
            FROM proxy_service_auction_items r
            JOIN merchandise m ON m.id = r.merchandise_id
            JOIN proxy_service_settings ps ON ps.id = r.auction_id
            LEFT JOIN users owner ON owner.id = m.user_id
            WHERE r.auction_id = ? AND r.outcome_status <> 'withdrawn'
            ORDER BY r.id DESC
            """,
            (auction_id,),
        )
        items = [row_dict(row) for row in cur.fetchall()]
        if not items:
            cur.close()
            return []
        item_ids = [int(item["id"]) for item in items]
        placeholders = ",".join(["%s" if is_postgres else "?"] * len(item_ids))
        cur.execute(
            f"""
            SELECT b.*, COALESCE(u.display_name, u.username, b.bidder_name, '氏名未登録') AS resolved_bidder_name
            FROM proxy_service_bids b
            LEFT JOIN users u ON u.id = b.user_id
            WHERE b.auction_id = {'%s' if is_postgres else '?'}
              AND b.merchandise_id IN ({placeholders})
            ORDER BY b.merchandise_id, b.bid_amount DESC, b.id ASC
            """,
            tuple([auction_id] + item_ids),
        )
        bids_by_item = {}
        for raw_bid in cur.fetchall():
            bid = row_dict(raw_bid)
            group = bids_by_item.setdefault(int(bid["merchandise_id"]), [])
            group.append(bid)
        cur.execute(
            f"""
            SELECT child.id, child.user_id, child.proxy_parent_item_id,
                   child.proxy_source_auction_id, child.created_at,
                   COALESCE(u.display_name, u.username, '氏名未登録') AS reflected_user_name
            FROM merchandise child
            LEFT JOIN users u ON u.id = child.user_id
            WHERE child.proxy_parent_item_id IN ({placeholders})
              AND (child.proxy_source_auction_id = {'%s' if is_postgres else '?'}
                   OR child.proxy_source_auction_id IS NULL)
            ORDER BY child.id DESC
            """,
            tuple(item_ids + [auction_id]),
        )
        children_by_parent = {}
        for raw_child in cur.fetchall():
            child = row_dict(raw_child)
            children_by_parent.setdefault(int(child["proxy_parent_item_id"]), child)
        cur.close()

        result = []
        for item in items:
            item_id = int(item["id"])
            bids = bids_by_item.get(item_id, [])
            top_bid = bids[0] if bids else None
            child = children_by_parent.get(item_id)
            outcome = item.get("proxy_run_status") or "active"
            item["auction_id"] = auction_id
            item["auction_name"] = auction_name_display(auction_id, item.get("run_auction_name"))
            item["sale_mode"] = item.get("run_sale_mode") or "auction"
            item["product_name"] = item.get("snapshot_product_name") or item.get("product_name")
            item["brand_name"] = item.get("snapshot_brand_name") or item.get("brand_name")
            item["photo_path"] = item.get("snapshot_photo_path") or item.get("photo_path")
            item["listing_price"] = item.get("snapshot_listing_price") or item.get("listing_price")
            item["purchase_price"] = item.get("snapshot_purchase_price") or item.get("purchase_price")
            item["highest_bid"] = (top_bid or {}).get("bid_amount") or item.get("run_winning_price")
            item["highest_bidder"] = (top_bid or {}).get("resolved_bidder_name") or item.get("run_winner_name")
            item["highest_bid_user_id"] = (top_bid or {}).get("user_id") or item.get("run_winner_user_id")
            item["bid_count"] = len(bids)
            item["last_bid_at"] = max((bid.get("created_at") for bid in bids), default=None)
            item["reflected_item_id"] = item.get("run_reflected_item_id") or (child or {}).get("id")
            item["reflected_user_id"] = item.get("run_winner_user_id") or (child or {}).get("user_id")
            item["reflected_user_name"] = item.get("run_winner_name") or (child or {}).get("reflected_user_name")
            item["reflected_at"] = item.get("run_finalized_at") or (child or {}).get("created_at")
            if outcome == "ended_no_bid":
                item["sale_date"] = None
                item["sale_price"] = 0
                item["sale_type"] = "normal"
                item["sales_destination"] = None
                item["highest_bid"] = None
                item["highest_bidder"] = None
                item["highest_bid_user_id"] = None
                item["bid_count"] = 0
            elif outcome in {"auction_won", "fixed_sold"}:
                item["sale_date"] = item.get("sale_date") or item.get("run_finalized_at")
                item["sale_price"] = item.get("run_winning_price") or item.get("sale_price")
                item["sale_type"] = "fixed" if outcome == "fixed_sold" else (item.get("sale_type") or "auction")
            result.append(item)
        return result

    def create_reflected_item_with_integrity(conn, item, reflected_by_user_id, now=None, skip_keisan=False):
        ensure_schema(conn)
        result = original_create_reflected_item(
            conn,
            item,
            reflected_by_user_id,
            now=now,
            skip_keisan=skip_keisan,
        )
        auction_id = item.get("auction_id")
        reflected_item_id = result.get("reflected_item_id")
        if reflected_item_id:
            cur = cursor_for(conn)
            execute(
                cur,
                "UPDATE merchandise SET scope = 'user', proxy_source_auction_id = %s WHERE id = %s",
                "UPDATE merchandise SET scope = 'user', proxy_source_auction_id = ? WHERE id = ?",
                (auction_id, reflected_item_id),
            )
            if auction_id:
                upsert_run(cur, auction_id, item, outcome_status="fixed_sold" if item.get("sale_mode") == "fixed" else "auction_won")
                execute(
                    cur,
                    """
                    UPDATE proxy_service_auction_items
                    SET outcome_status = %s, winner_user_id = %s, winner_name = %s,
                        winning_price = %s, reflected_item_id = %s,
                        finalized_at = COALESCE(finalized_at, %s), updated_at = CURRENT_TIMESTAMP
                    WHERE auction_id = %s AND merchandise_id = %s
                    """,
                    """
                    UPDATE proxy_service_auction_items
                    SET outcome_status = ?, winner_user_id = ?, winner_name = ?,
                        winning_price = ?, reflected_item_id = ?,
                        finalized_at = COALESCE(finalized_at, ?), updated_at = CURRENT_TIMESTAMP
                    WHERE auction_id = ? AND merchandise_id = ?
                    """,
                    (
                        "fixed_sold" if item.get("sale_mode") == "fixed" else "auction_won",
                        result.get("winner_user_id"),
                        result.get("winner_name"),
                        module.normalize_proxy_service_price_value(item.get("result_price") or item.get("highest_bid")),
                        reflected_item_id,
                        now or module.get_jst_now(),
                        auction_id,
                        item.get("id"),
                    ),
                )
            cur.close()
        return result

    def settle_winner_locked(conn, cur, item, settings, bid, actor_user_id, now):
        item = row_dict(item)
        bid = row_dict(bid)
        auction_id = int(settings["id"])
        item_id = int(item["id"])
        winner_user_id = int(bid["user_id"])
        winner_name = str(bid.get("resolved_bidder_name") or bid.get("bidder_name") or "氏名未登録")
        winning_price = module.normalize_proxy_service_price_value(bid.get("bid_amount"))
        upsert_run(cur, auction_id, item, outcome_status="active")
        execute(
            cur,
            "SELECT id, role, subscription_status, proxy_service_budget FROM users WHERE id = %s FOR UPDATE",
            "SELECT id, role, subscription_status, proxy_service_budget FROM users WHERE id = ?",
            (winner_user_id,),
        )
        locked_winner = row_dict(cur.fetchone())
        locked_status = str((locked_winner or {}).get("subscription_status") or "inactive").strip().lower()
        winner_can_settle = (
            bool(locked_winner)
            and str(locked_winner.get("role") or "") == "user"
            and locked_status not in INVALID_CLIENT_STATUSES
            and module.normalize_proxy_service_price_value(locked_winner.get("proxy_service_budget")) >= winning_price
        )
        if not winner_can_settle:
            execute(
                cur,
                """
                UPDATE proxy_service_auction_items
                SET outcome_status = 'needs_review', winner_user_id = %s, winner_name = %s,
                    winning_price = %s, updated_at = CURRENT_TIMESTAMP
                WHERE auction_id = %s AND merchandise_id = %s
                """,
                """
                UPDATE proxy_service_auction_items
                SET outcome_status = 'needs_review', winner_user_id = ?, winner_name = ?,
                    winning_price = ?, updated_at = CURRENT_TIMESTAMP
                WHERE auction_id = ? AND merchandise_id = ?
                """,
                (winner_user_id, winner_name, winning_price, auction_id, item_id),
            )
            return False, {"unresolved": True}
        execute(
            cur,
            """
            UPDATE merchandise
            SET sale_date = %s, sale_price = %s, sale_type = 'auction',
                sales_destination = %s, show_in_proxy_service = FALSE,
                updated_by = %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND auction_id = %s AND sale_date IS NULL
            """,
            """
            UPDATE merchandise
            SET sale_date = ?, sale_price = ?, sale_type = 'auction',
                sales_destination = ?, show_in_proxy_service = 0,
                updated_by = ?, updated_at = ?
            WHERE id = ? AND auction_id = ? AND sale_date IS NULL
            """,
            (
                now.date() if is_postgres else now.strftime("%Y-%m-%d"),
                winning_price,
                f"代行仕入れ落札: {winner_name}",
                actor_user_id,
                *((item_id, auction_id) if is_postgres else (now.strftime("%Y-%m-%d %H:%M:%S"), item_id, auction_id)),
            ),
        )
        updated = cur.rowcount
        if not updated:
            return False, {}
        execute(
            cur,
            "UPDATE users SET proxy_service_budget = COALESCE(proxy_service_budget, 0) - %s WHERE id = %s AND COALESCE(proxy_service_budget, 0) >= %s",
            "UPDATE users SET proxy_service_budget = COALESCE(proxy_service_budget, 0) - ? WHERE id = ? AND COALESCE(proxy_service_budget, 0) >= ?",
            (winning_price, winner_user_id, winning_price),
        )
        if cur.rowcount != 1:
            raise RuntimeError("winner budget changed while settling")
        reflection = create_reflected_item_with_integrity(
            conn,
            {
                **item,
                "auction_id": auction_id,
                "auction_name": auction_name_display(auction_id, settings.get("auction_name")),
                "sale_mode": "auction",
                "winner_user_id": winner_user_id,
                "winner_name": winner_name,
                "result_price": winning_price,
            },
            actor_user_id,
            now=now,
        )
        execute(
            cur,
            """
            UPDATE proxy_service_auction_items
            SET outcome_status = 'auction_won', winner_user_id = %s, winner_name = %s,
                winning_price = %s, reflected_item_id = %s,
                finalized_at = COALESCE(finalized_at, %s), updated_at = CURRENT_TIMESTAMP
            WHERE auction_id = %s AND merchandise_id = %s
            """,
            """
            UPDATE proxy_service_auction_items
            SET outcome_status = 'auction_won', winner_user_id = ?, winner_name = ?,
                winning_price = ?, reflected_item_id = ?,
                finalized_at = COALESCE(finalized_at, ?), updated_at = CURRENT_TIMESTAMP
            WHERE auction_id = ? AND merchandise_id = ?
            """,
            (winner_user_id, winner_name, winning_price, reflection.get("reflected_item_id"), now, auction_id, item_id),
        )
        return bool(updated), reflection

    def reconcile_auction(
        auction_id,
        *,
        now=None,
        actor_user_id=None,
        require_ended=True,
        only_item_id=None,
    ):
        now = now or module.get_jst_now()
        conn = module.get_db()
        ensure_schema(conn)
        cur = cursor_for(conn, rows=True)
        summary = {
            "finalized": 0,
            "released": 0,
            "repaired": 0,
            "unresolved": 0,
            "already": 0,
            "auction_id": auction_id,
        }
        try:
            if not is_postgres:
                cur.execute("BEGIN IMMEDIATE")
            execute(
                cur,
                "SELECT * FROM proxy_service_settings WHERE id = %s",
                "SELECT * FROM proxy_service_settings WHERE id = ?",
                (auction_id,),
            )
            settings = row_dict(cur.fetchone())
            if not settings:
                raise LookupError("オークションが見つかりません")
            state = module.get_proxy_service_auction_state(settings, now=now)
            if require_ended and not state.get("is_ended"):
                raise ValueError("開催中のオークションは確定できません")
            if only_item_id is None:
                execute(
                    cur,
                    """SELECT m.*, owner.role AS owner_role
                       FROM merchandise m
                       LEFT JOIN users owner ON owner.id = m.user_id
                       WHERE m.auction_id = %s ORDER BY m.id FOR UPDATE OF m""",
                    """SELECT m.*, owner.role AS owner_role
                       FROM merchandise m
                       LEFT JOIN users owner ON owner.id = m.user_id
                       WHERE m.auction_id = ? ORDER BY m.id""",
                    (auction_id,),
                )
            else:
                execute(
                    cur,
                    """SELECT m.*, owner.role AS owner_role
                       FROM merchandise m
                       LEFT JOIN users owner ON owner.id = m.user_id
                       WHERE m.auction_id = %s AND m.id = %s ORDER BY m.id FOR UPDATE OF m""",
                    """SELECT m.*, owner.role AS owner_role
                       FROM merchandise m
                       LEFT JOIN users owner ON owner.id = m.user_id
                       WHERE m.auction_id = ? AND m.id = ? ORDER BY m.id""",
                    (auction_id, only_item_id),
                )
            items = [row_dict(row) for row in cur.fetchall()]
            if only_item_id is not None and not items:
                raise LookupError("このオークションの対象商品が見つかりません")
            execute(
                cur,
                "SELECT * FROM proxy_service_settings WHERE id = %s FOR UPDATE",
                "SELECT * FROM proxy_service_settings WHERE id = ?",
                (auction_id,),
            )
            settings = row_dict(cur.fetchone())
            state = module.get_proxy_service_auction_state(settings, now=now)
            if require_ended and not state.get("is_ended"):
                raise ValueError("開催中のオークションは確定できません")
            if is_postgres:
                cur.execute(
                    """
                    SELECT u.id
                    FROM users u
                    WHERE u.id IN (
                        SELECT DISTINCT b.user_id
                        FROM proxy_service_bids b
                        WHERE b.auction_id = %s AND b.user_id IS NOT NULL
                    )
                    ORDER BY u.id
                    FOR UPDATE OF u
                    """,
                    (auction_id,),
                )
                cur.fetchall()
            for item in items:
                upsert_run(cur, auction_id, item, outcome_status="active")
                is_kaika_item = (
                    str(item.get("scope") or "admin") == "admin"
                    and (item.get("user_id") is None or item.get("owner_role") in {"admin", "owner"})
                )
                if not is_kaika_item:
                    execute(
                        cur,
                        "UPDATE merchandise SET auction_id = NULL, show_in_proxy_service = FALSE, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND auction_id = %s",
                        "UPDATE merchandise SET auction_id = NULL, show_in_proxy_service = 0, updated_by = ?, updated_at = ? WHERE id = ? AND auction_id = ?",
                        (
                            (actor_user_id or settings.get("updated_by")),
                            *((item["id"], auction_id) if is_postgres else (now.strftime("%Y-%m-%d %H:%M:%S"), item["id"], auction_id)),
                        ),
                    )
                    execute(
                        cur,
                        "UPDATE proxy_service_auction_items SET outcome_status = 'needs_review', updated_at = CURRENT_TIMESTAMP WHERE auction_id = %s AND merchandise_id = %s",
                        "UPDATE proxy_service_auction_items SET outcome_status = 'needs_review', updated_at = CURRENT_TIMESTAMP WHERE auction_id = ? AND merchandise_id = ?",
                        (auction_id, item["id"]),
                    )
                    summary["unresolved"] += 1
                    continue
                execute(
                    cur,
                    """
                    SELECT b.bidder_name, b.bid_amount
                    FROM proxy_service_bids b
                    JOIN proxy_service_settings ps ON ps.id = %s
                    WHERE b.auction_id IS NULL AND b.merchandise_id = %s
                      AND (
                          b.created_at IS NULL OR ps.start_datetime IS NULL
                          OR (
                              b.created_at >= ps.start_datetime
                              AND (ps.end_datetime IS NULL OR b.created_at <= ps.end_datetime)
                          )
                      )
                    ORDER BY b.bid_amount DESC, b.id ASC LIMIT 1
                    """,
                    """
                    SELECT b.bidder_name, b.bid_amount
                    FROM proxy_service_bids b
                    JOIN proxy_service_settings ps ON ps.id = ?
                    WHERE b.auction_id IS NULL AND b.merchandise_id = ?
                      AND (
                          b.created_at IS NULL OR ps.start_datetime IS NULL
                          OR (
                              datetime(b.created_at) >= datetime(ps.start_datetime)
                              AND (ps.end_datetime IS NULL OR datetime(b.created_at) <= datetime(ps.end_datetime))
                          )
                      )
                    ORDER BY b.bid_amount DESC, b.id ASC LIMIT 1
                    """,
                    (auction_id, item["id"]),
                )
                ambiguous_legacy_bid = row_dict(cur.fetchone())
                if ambiguous_legacy_bid:
                    execute(
                        cur,
                        """
                        UPDATE proxy_service_auction_items
                        SET outcome_status = 'needs_review', winner_name = %s,
                            winning_price = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE auction_id = %s AND merchandise_id = %s
                        """,
                        """
                        UPDATE proxy_service_auction_items
                        SET outcome_status = 'needs_review', winner_name = ?,
                            winning_price = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE auction_id = ? AND merchandise_id = ?
                        """,
                        (
                            ambiguous_legacy_bid.get("bidder_name"),
                            module.normalize_proxy_service_price_value(ambiguous_legacy_bid.get("bid_amount")),
                            auction_id,
                            item["id"],
                        ),
                    )
                    summary["unresolved"] += 1
                    continue
                execute(
                    cur,
                    """
                    SELECT b.*, u.role AS winner_role, u.subscription_status AS winner_subscription_status,
                           COALESCE(u.display_name, u.username, b.bidder_name, '氏名未登録') AS resolved_bidder_name
                    FROM proxy_service_bids b
                    LEFT JOIN users u ON u.id = b.user_id
                    WHERE b.auction_id = %s AND b.merchandise_id = %s
                    ORDER BY b.bid_amount DESC, b.id ASC LIMIT 1
                    """,
                    """
                    SELECT b.*, u.role AS winner_role, u.subscription_status AS winner_subscription_status,
                           COALESCE(u.display_name, u.username, b.bidder_name, '氏名未登録') AS resolved_bidder_name
                    FROM proxy_service_bids b
                    LEFT JOIN users u ON u.id = b.user_id
                    WHERE b.auction_id = ? AND b.merchandise_id = ?
                    ORDER BY b.bid_amount DESC, b.id ASC LIMIT 1
                    """,
                    (auction_id, item["id"]),
                )
                highest_bid = row_dict(cur.fetchone())
                if item.get("sale_date"):
                    historic_winner_valid = (
                        bool(highest_bid)
                        and highest_bid.get("user_id") not in (None, "")
                        and str(highest_bid.get("winner_role") or "") == "user"
                        and module.normalize_proxy_service_price_value(
                            item.get("sale_price") or highest_bid.get("bid_amount")
                        ) > 0
                    )
                    existing_reflected = module.lookup_proxy_service_reflected_item(
                        cur, item["id"]
                    )
                    if (
                        not historic_winner_valid
                        or (
                            existing_reflected
                            and int(existing_reflected.get("user_id") or 0)
                            != int(highest_bid.get("user_id") or 0)
                        )
                    ):
                        execute(
                            cur,
                            "UPDATE proxy_service_auction_items SET outcome_status = 'needs_review', updated_at = CURRENT_TIMESTAMP WHERE auction_id = %s AND merchandise_id = %s",
                            "UPDATE proxy_service_auction_items SET outcome_status = 'needs_review', updated_at = CURRENT_TIMESTAMP WHERE auction_id = ? AND merchandise_id = ?",
                            (auction_id, item["id"]),
                        )
                        summary["unresolved"] += 1
                        continue
                    repair_result = create_reflected_item_with_integrity(
                        conn,
                        {
                            **item,
                            "auction_id": auction_id,
                            "auction_name": auction_name_display(auction_id, settings.get("auction_name")),
                            "sale_mode": "fixed" if (item.get("sale_type") or "") == "fixed" else "auction",
                            "result_code": "fixed_sold" if (item.get("sale_type") or "") == "fixed" else "auction_finalized",
                            "winner_user_id": int(highest_bid["user_id"]),
                            "winner_name": highest_bid.get("resolved_bidder_name") or highest_bid.get("bidder_name") or "氏名未登録",
                            "result_price": module.normalize_proxy_service_price_value(
                                item.get("sale_price") or highest_bid.get("bid_amount")
                            ),
                        },
                        actor_user_id or settings.get("updated_by"),
                        now=now,
                    )
                    if (
                        repair_result.get("created")
                        or repair_result.get("keisan_created")
                        or repair_result.get("keisan_item_added")
                    ):
                        summary["repaired"] += 1
                    summary["already"] += 1
                    continue
                if highest_bid:
                    winner_status = str(
                        highest_bid.get("winner_subscription_status") or "inactive"
                    ).strip().lower()
                    winner_is_valid = (
                        highest_bid.get("user_id") not in (None, "")
                        and str(highest_bid.get("winner_role") or "") == "user"
                        and winner_status not in INVALID_CLIENT_STATUSES
                        and module.normalize_proxy_service_price_value(highest_bid.get("bid_amount")) > 0
                    )
                    if not winner_is_valid:
                        execute(
                            cur,
                            """
                            UPDATE proxy_service_auction_items
                            SET outcome_status = 'needs_review', winner_name = %s,
                                winning_price = %s, updated_at = CURRENT_TIMESTAMP
                            WHERE auction_id = %s AND merchandise_id = %s
                            """,
                            """
                            UPDATE proxy_service_auction_items
                            SET outcome_status = 'needs_review', winner_name = ?,
                                winning_price = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE auction_id = ? AND merchandise_id = ?
                            """,
                            (
                                highest_bid.get("resolved_bidder_name") or highest_bid.get("bidder_name"),
                                module.normalize_proxy_service_price_value(highest_bid.get("bid_amount")),
                                auction_id,
                                item["id"],
                            ),
                        )
                        summary["unresolved"] += 1
                        continue
                    updated, reflection = settle_winner_locked(
                        conn,
                        cur,
                        item,
                        settings,
                        highest_bid,
                        actor_user_id or settings.get("updated_by"),
                        now,
                    )
                    if reflection.get("unresolved"):
                        summary["unresolved"] += 1
                        continue
                    summary["finalized"] += int(updated)
                    summary["already"] += int(not updated)
                else:
                    execute(
                        cur,
                        """
                        UPDATE merchandise
                        SET auction_id = NULL, show_in_proxy_service = FALSE,
                            updated_by = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s AND auction_id = %s AND sale_date IS NULL
                        """,
                        """
                        UPDATE merchandise
                        SET auction_id = NULL, show_in_proxy_service = 0,
                            updated_by = ?, updated_at = ?
                        WHERE id = ? AND auction_id = ? AND sale_date IS NULL
                        """,
                        (
                            (actor_user_id or settings.get("updated_by")),
                            *((item["id"], auction_id) if is_postgres else (now.strftime("%Y-%m-%d %H:%M:%S"), item["id"], auction_id)),
                        ),
                    )
                    if cur.rowcount:
                        execute(
                            cur,
                            """
                            UPDATE proxy_service_auction_items
                            SET outcome_status = 'ended_no_bid', released_at = COALESCE(released_at, %s),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE auction_id = %s AND merchandise_id = %s
                            """,
                            """
                            UPDATE proxy_service_auction_items
                            SET outcome_status = 'ended_no_bid', released_at = COALESCE(released_at, ?),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE auction_id = ? AND merchandise_id = ?
                            """,
                            (now, auction_id, item["id"]),
                        )
                        summary["released"] += 1
                    else:
                        summary["already"] += 1
            if state.get("is_ended"):
                execute(
                    cur,
                    "UPDATE proxy_service_settings SET is_public = FALSE, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND is_public = TRUE",
                    "UPDATE proxy_service_settings SET is_public = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND is_public = 1",
                    (auction_id,),
                )
            conn.commit()
            return summary
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
            conn.close()

    def reconcile_expired_auctions(now=None):
        now = now or module.get_jst_now()
        conn = module.get_db()
        ensure_schema(conn)
        cur = cursor_for(conn, rows=True)
        execute(
            cur,
            """SELECT ps.id FROM proxy_service_settings ps
               WHERE ps.end_datetime IS NOT NULL AND ps.end_datetime <= %s
                 AND (
                     COALESCE(ps.is_public, FALSE) = TRUE
                     OR EXISTS (
                         SELECT 1 FROM merchandise m
                         WHERE m.auction_id = ps.id AND m.sale_date IS NULL
                     )
                 )
               ORDER BY ps.id""",
            """SELECT ps.id FROM proxy_service_settings ps
               WHERE ps.end_datetime IS NOT NULL AND datetime(ps.end_datetime) <= datetime(?)
                 AND (
                     COALESCE(ps.is_public, 0) = 1
                     OR EXISTS (
                         SELECT 1 FROM merchandise m
                         WHERE m.auction_id = ps.id AND m.sale_date IS NULL
                     )
                 )
               ORDER BY ps.id""",
            (now,),
        )
        auction_ids = [int(row_dict(row)["id"]) for row in cur.fetchall()]
        cur.close()
        conn.close()
        summaries = []
        for auction_id in auction_ids:
            try:
                summaries.append(reconcile_auction(auction_id, now=now, require_ended=True))
            except Exception as exc:
                print(f"[PROXY] expiry reconciliation failed for auction {auction_id}: {exc}", flush=True)
                summaries.append({"auction_id": auction_id, "error": True})
        return summaries

    def annotate_items_with_integrity(items, settings, now=None, current_user_id=None):
        annotated, summary, winners = original_annotate_items(
            items,
            settings,
            now=now,
            current_user_id=current_user_id,
        )
        for item in annotated:
            if item.get("proxy_run_status") == "ended_no_bid":
                item.update(
                    result_code="auction_closed_no_bid" if (settings.get("sale_mode") or "auction") == "auction" else "fixed_closed_unsold",
                    status_label="落札者なし" if (settings.get("sale_mode") or "auction") == "auction" else "販売終了",
                    status_class="muted",
                    ended_method_label="時間締切",
                    result_price=None,
                    can_reflect_to_client=False,
                    reflection_status_label="対象外",
                    reflection_status_class="muted",
                )
            elif item.get("proxy_run_status") == "needs_review":
                item.update(
                    result_code="auction_closed_with_bid",
                    status_label="落札者確認待ち",
                    status_class="warning",
                    ended_method_label="管理者確認が必要",
                    can_reflect_to_client=False,
                    reflection_status_label="確認待ち",
                    reflection_status_class="warning",
                )
        summary["no_bid_count"] = sum(
            1 for item in annotated if item.get("result_code") in {"auction_closed_no_bid", "fixed_closed_unsold"}
        )
        summary["pending_finalize_count"] = sum(
            1 for item in annotated if item.get("proxy_run_status") == "needs_review"
        )
        return annotated, summary, winners

    def history_datasets_with_integrity(conn, *args, **kwargs):
        data = original_history_datasets(conn, *args, **kwargs)
        all_cards = list(data.get("pending_history", [])) + list(data.get("archived_history", []))
        total_items = 0
        total_bids = 0
        cur = cursor_for(conn, rows=True)
        for card in all_cards:
            card["auction_name"] = auction_name_display(card.get("id"), card.get("auction_name"))
            card["item_count"] = len(fetch_proxy_service_items(conn, card["id"]))
            execute(
                cur,
                "SELECT COUNT(*) AS count FROM proxy_service_bids WHERE auction_id = %s",
                "SELECT COUNT(*) AS count FROM proxy_service_bids WHERE auction_id = ?",
                (card["id"],),
            )
            count_row = row_dict(cur.fetchone())
            card["bid_count"] = int((count_row or {}).get("count") or 0)
            total_items += card["item_count"]
            total_bids += card["bid_count"]
        cur.close()
        data.setdefault("summary", {})["item_count"] = total_items
        data["summary"]["bid_count"] = total_bids
        return data

    def public_sections_with_integrity(conn, *args, **kwargs):
        data = original_public_sections(conn, *args, **kwargs)
        for section_name in ("current_auctions", "upcoming_auctions", "history_auctions"):
            for auction in data.get(section_name, []):
                auction["auction_name"] = auction_name_display(
                    auction.get("id"), auction.get("auction_name")
                )
        return data

    def count_user_bids(conn, auction_id, user_id):
        if not auction_id or not user_id:
            return 0
        ensure_schema(conn)
        cur = cursor_for(conn, rows=True)
        execute(
            cur,
            "SELECT COUNT(*) AS bid_count FROM proxy_service_bids WHERE auction_id = %s AND user_id = %s",
            "SELECT COUNT(*) AS bid_count FROM proxy_service_bids WHERE auction_id = ? AND user_id = ?",
            (auction_id, user_id),
        )
        row = row_dict(cur.fetchone())
        cur.close()
        return int((row or {}).get("bid_count") or 0)

    def parse_request_data():
        data = request.get_json(silent=True)
        if isinstance(data, dict):
            return data
        raw_body = request.get_data(as_text=True) or ""
        if raw_body:
            try:
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict):
                    return parsed
            except (TypeError, ValueError):
                pass
        return request.form

    def validate_admin_actor(json_response=True):
        if not (current_user.is_owner() or current_user.is_admin()):
            return jsonify({"success": False, "error": "権限がありません"}), 403
        if not current_user.can_manage_proxy_service():
            return module.get_proxy_publish_denied_response(json_response=json_response)
        return None

    def safe_toggle_item(auction_id, item_id):
        denied = validate_admin_actor()
        if denied:
            return denied
        data = parse_request_data()
        raw_price = data.get("proxy_price")
        try:
            proxy_price = int(raw_price) if raw_price not in (None, "") else None
            if proxy_price is not None and proxy_price <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "公開価格は1円以上で入力してください"}), 400
        conn = module.get_db()
        ensure_schema(conn)
        cur = cursor_for(conn, rows=True)
        try:
            if not is_postgres:
                cur.execute("BEGIN IMMEDIATE")
            execute(cur, "SELECT * FROM proxy_service_settings WHERE id = %s", "SELECT * FROM proxy_service_settings WHERE id = ?", (auction_id,))
            settings = row_dict(cur.fetchone())
            if not settings:
                raise LookupError("オークションが見つかりません")
            if module.get_proxy_service_auction_state(settings).get("is_ended"):
                raise ValueError("終了済みオークションの商品は変更できません")
            execute(
                cur,
                """
                SELECT m.*, u.role AS owner_role FROM merchandise m
                LEFT JOIN users u ON u.id = m.user_id WHERE m.id = %s FOR UPDATE OF m
                """,
                """
                SELECT m.*, u.role AS owner_role FROM merchandise m
                LEFT JOIN users u ON u.id = m.user_id WHERE m.id = ?
                """,
                (item_id,),
            )
            item = row_dict(cur.fetchone())
            if not item:
                raise LookupError("商品が見つかりません")
            execute(
                cur,
                "SELECT * FROM proxy_service_settings WHERE id = %s FOR UPDATE",
                "SELECT * FROM proxy_service_settings WHERE id = ?",
                (auction_id,),
            )
            settings = row_dict(cur.fetchone())
            if not settings:
                raise LookupError("オークションが見つかりません")
            if module.get_proxy_service_auction_state(settings).get("is_ended"):
                raise ValueError("終了済みオークションの商品は変更できません")
            is_kaika = (
                str(item.get("scope") or "admin") == "admin"
                and (item.get("user_id") is None or item.get("owner_role") in {"admin", "owner"})
            )
            if not is_kaika or item.get("sale_date"):
                raise ValueError("未売却の開花商品のみ選択できます")
            if item.get("auction_id") == auction_id:
                execute(
                    cur,
                    "SELECT COUNT(*) AS count FROM proxy_service_bids WHERE auction_id = %s AND merchandise_id = %s",
                    "SELECT COUNT(*) AS count FROM proxy_service_bids WHERE auction_id = ? AND merchandise_id = ?",
                    (auction_id, item_id),
                )
                if int((row_dict(cur.fetchone()) or {}).get("count") or 0):
                    raise ValueError("入札済みの商品は開催中に解除できません")
                execute(
                    cur,
                    "UPDATE merchandise SET show_in_proxy_service = FALSE, auction_id = NULL, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND auction_id = %s",
                    "UPDATE merchandise SET show_in_proxy_service = 0, auction_id = NULL, updated_by = ?, updated_at = ? WHERE id = ? AND auction_id = ?",
                    (
                        current_user.id,
                        *((item_id, auction_id) if is_postgres else (module.get_jst_now().strftime("%Y-%m-%d %H:%M:%S"), item_id, auction_id)),
                    ),
                )
                execute(
                    cur,
                    "UPDATE proxy_service_auction_items SET outcome_status = 'withdrawn', updated_at = CURRENT_TIMESTAMP WHERE auction_id = %s AND merchandise_id = %s",
                    "UPDATE proxy_service_auction_items SET outcome_status = 'withdrawn', updated_at = CURRENT_TIMESTAMP WHERE auction_id = ? AND merchandise_id = ?",
                    (auction_id, item_id),
                )
                new_value = False
            else:
                if item.get("auction_id") is not None:
                    raise ValueError("この商品は別の開催中オークションで使用中です")
                selected_price = proxy_price or module.get_effective_proxy_price(item)
                execute(
                    cur,
                    """
                    UPDATE merchandise SET show_in_proxy_service = TRUE, auction_id = %s,
                        listing_price = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s AND auction_id IS NULL AND sale_date IS NULL
                    """,
                    """
                    UPDATE merchandise SET show_in_proxy_service = 1, auction_id = ?,
                        listing_price = ?, updated_by = ?, updated_at = ?
                    WHERE id = ? AND auction_id IS NULL AND sale_date IS NULL
                    """,
                    (
                        *((auction_id, selected_price, current_user.id, item_id) if is_postgres else (auction_id, selected_price, current_user.id, module.get_jst_now().strftime("%Y-%m-%d %H:%M:%S"), item_id)),
                    ),
                )
                if cur.rowcount != 1:
                    raise ValueError("商品の状態が変わったため追加できません")
                item["listing_price"] = selected_price
                upsert_run(cur, auction_id, item, outcome_status="active")
                new_value = True
            conn.commit()
            return jsonify({"success": True, "new_value": new_value})
        except LookupError as exc:
            conn.rollback()
            return jsonify({"success": False, "error": str(exc)}), 404
        except ValueError as exc:
            conn.rollback()
            return jsonify({"success": False, "error": str(exc)}), 409
        except Exception as exc:
            conn.rollback()
            print(f"[PROXY] toggle failed: {exc}", flush=True)
            return jsonify({"success": False, "error": "商品設定を保存できませんでした"}), 500
        finally:
            cur.close()
            conn.close()

    def safe_bulk_toggle(auction_id):
        denied = validate_admin_actor()
        if denied:
            return denied
        data = parse_request_data()
        raw_ids = data.getlist("item_ids") if hasattr(data, "getlist") else data.get("item_ids", [])
        if raw_ids and not isinstance(raw_ids, (list, tuple)):
            raw_ids = [raw_ids]
        payloads = data.get("items", []) if isinstance(data.get("items", []), list) else []
        if not raw_ids and payloads:
            raw_ids = [entry.get("id") for entry in payloads]
        try:
            item_ids = []
            for raw_id in raw_ids:
                item_id = int(raw_id)
                if item_id <= 0:
                    raise ValueError
                if item_id not in item_ids:
                    item_ids.append(item_id)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "商品IDが不正です"}), 400
        if not item_ids:
            return jsonify({"success": False, "error": "商品が選択されていません"}), 400
        action = data.get("action", "add")
        if action not in {"add", "remove"}:
            return jsonify({"success": False, "error": "操作が不正です"}), 400
        prices = data.get("item_prices", {}) or {}
        conn = module.get_db()
        ensure_schema(conn)
        cur = cursor_for(conn, rows=True)
        try:
            if not is_postgres:
                cur.execute("BEGIN IMMEDIATE")
            execute(cur, "SELECT * FROM proxy_service_settings WHERE id = %s", "SELECT * FROM proxy_service_settings WHERE id = ?", (auction_id,))
            settings = row_dict(cur.fetchone())
            if not settings:
                raise LookupError("オークションが見つかりません")
            if module.get_proxy_service_auction_state(settings).get("is_ended"):
                raise ValueError("終了済みオークションの商品は変更できません")
            placeholders = ",".join(["%s" if is_postgres else "?"] * len(item_ids))
            cur.execute(
                f"""
                SELECT m.*, u.role AS owner_role FROM merchandise m
                LEFT JOIN users u ON u.id = m.user_id
                WHERE m.id IN ({placeholders})
                ORDER BY m.id
                {"FOR UPDATE OF m" if is_postgres else ""}
                """,
                tuple(item_ids),
            )
            rows = {int(row_dict(row)["id"]): row_dict(row) for row in cur.fetchall()}
            if set(rows) != set(item_ids):
                raise LookupError("商品が見つかりません")
            execute(
                cur,
                "SELECT * FROM proxy_service_settings WHERE id = %s FOR UPDATE",
                "SELECT * FROM proxy_service_settings WHERE id = ?",
                (auction_id,),
            )
            settings = row_dict(cur.fetchone())
            if not settings:
                raise LookupError("オークションが見つかりません")
            if module.get_proxy_service_auction_state(settings).get("is_ended"):
                raise ValueError("終了済みオークションの商品は変更できません")
            for item_id in item_ids:
                item = rows[item_id]
                is_kaika = (
                    str(item.get("scope") or "admin") == "admin"
                    and (item.get("user_id") is None or item.get("owner_role") in {"admin", "owner"})
                )
                if not is_kaika or item.get("sale_date"):
                    raise ValueError("未売却の開花商品のみ選択できます")
                if action == "add" and item.get("auction_id") not in (None, auction_id):
                    raise ValueError("別の開催中オークションで使用中の商品が含まれています")
                if action == "remove" and item.get("auction_id") != auction_id:
                    raise ValueError("このオークションに属さない商品が含まれています")
                execute(
                    cur,
                    "SELECT COUNT(*) AS count FROM proxy_service_bids WHERE auction_id = %s AND merchandise_id = %s",
                    "SELECT COUNT(*) AS count FROM proxy_service_bids WHERE auction_id = ? AND merchandise_id = ?",
                    (auction_id, item_id),
                )
                if action == "remove" and int((row_dict(cur.fetchone()) or {}).get("count") or 0):
                    raise ValueError("入札済みの商品は開催中に解除できません")
            for item_id in item_ids:
                item = rows[item_id]
                if action == "add":
                    raw_price = prices.get(str(item_id), prices.get(item_id))
                    try:
                        selected_price = int(raw_price) if raw_price not in (None, "") else module.get_effective_proxy_price(item)
                    except (TypeError, ValueError):
                        raise ValueError("公開価格が不正です")
                    if selected_price <= 0:
                        raise ValueError("公開価格は1円以上で入力してください")
                    execute(
                        cur,
                        "UPDATE merchandise SET show_in_proxy_service = TRUE, auction_id = %s, listing_price = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND (auction_id IS NULL OR auction_id = %s) AND sale_date IS NULL",
                        "UPDATE merchandise SET show_in_proxy_service = 1, auction_id = ?, listing_price = ?, updated_by = ?, updated_at = ? WHERE id = ? AND (auction_id IS NULL OR auction_id = ?) AND sale_date IS NULL",
                        (
                            *((auction_id, selected_price, current_user.id, item_id, auction_id) if is_postgres else (auction_id, selected_price, current_user.id, module.get_jst_now().strftime("%Y-%m-%d %H:%M:%S"), item_id, auction_id)),
                        ),
                    )
                    item["listing_price"] = selected_price
                    upsert_run(cur, auction_id, item, outcome_status="active")
                else:
                    execute(
                        cur,
                        "UPDATE merchandise SET show_in_proxy_service = FALSE, auction_id = NULL, updated_by = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s AND auction_id = %s",
                        "UPDATE merchandise SET show_in_proxy_service = 0, auction_id = NULL, updated_by = ?, updated_at = ? WHERE id = ? AND auction_id = ?",
                        (
                            current_user.id,
                            *((item_id, auction_id) if is_postgres else (module.get_jst_now().strftime("%Y-%m-%d %H:%M:%S"), item_id, auction_id)),
                        ),
                    )
                    execute(
                        cur,
                        "UPDATE proxy_service_auction_items SET outcome_status = 'withdrawn', updated_at = CURRENT_TIMESTAMP WHERE auction_id = %s AND merchandise_id = %s",
                        "UPDATE proxy_service_auction_items SET outcome_status = 'withdrawn', updated_at = CURRENT_TIMESTAMP WHERE auction_id = ? AND merchandise_id = ?",
                        (auction_id, item_id),
                    )
            conn.commit()
            return jsonify({"success": True, "updated_count": len(item_ids)})
        except LookupError as exc:
            conn.rollback()
            return jsonify({"success": False, "error": str(exc)}), 404
        except ValueError as exc:
            conn.rollback()
            return jsonify({"success": False, "error": str(exc)}), 409
        except Exception as exc:
            conn.rollback()
            print(f"[PROXY] bulk toggle failed: {exc}", flush=True)
            return jsonify({"success": False, "error": "商品設定を保存できませんでした"}), 500
        finally:
            cur.close()
            conn.close()

    def admin_create():
        if not (current_user.is_owner() or current_user.is_admin()):
            flash("この機能はオーナーまたは管理者のみ利用可能です", "error")
            return redirect(url_for("index"))
        if not current_user.can_manage_proxy_service():
            return module.get_proxy_publish_denied_response()
        conn = module.get_db()
        ensure_schema(conn)
        if request.method == "GET":
            users, _ = fetch_valid_clients(conn)
            conn.close()
            return render_template("admin/proxy_service_create.html", users=users)
        name = str(request.form.get("auction_name") or "").strip()
        if not name:
            conn.close()
            flash("オークション名は必須です", "error")
            return redirect(url_for("admin_proxy_service_create"))
        if len(name) > 100:
            conn.close()
            flash("オークション名は100文字以内で入力してください", "error")
            return redirect(url_for("admin_proxy_service_create"))
        try:
            selected_ids = validate_client_ids(conn, request.form.getlist("selected_users"))
            page_title = str(request.form.get("page_title") or "").strip() or "代行仕入れサービス"
            page_description = str(request.form.get("page_description") or "").strip()
            start_datetime = module.normalize_proxy_service_datetime_input(request.form.get("start_datetime"))
            end_datetime = module.normalize_proxy_service_datetime_input(request.form.get("end_datetime"))
            start_dt = module.parse_proxy_service_datetime(start_datetime)
            end_dt = module.parse_proxy_service_datetime(end_datetime)
            if start_dt and end_dt and start_dt >= end_dt:
                raise ValueError("終了日時は開始日時より後に設定してください")
            sale_mode = request.form.get("sale_mode", "auction")
            if sale_mode not in {"auction", "fixed"}:
                raise ValueError("販売方式が不正です")
            draft = request.form.get("submit_action") == "draft"
            is_public = request.form.get("is_public") == "on" and not draft
            if is_public and not selected_ids:
                raise ValueError("公開する場合は対象ユーザーを1名以上選択してください")
            cur = cursor_for(conn)
            if is_postgres:
                cur.execute(
                    """
                    INSERT INTO proxy_service_settings
                        (auction_name, page_title, page_description, start_datetime, end_datetime, sale_mode, is_public, updated_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
                    """,
                    (name, page_title, page_description, start_datetime, end_datetime, sale_mode, is_public, current_user.id),
                )
                new_id = int(cur.fetchone()[0])
            else:
                cur.execute(
                    """
                    INSERT INTO proxy_service_settings
                        (auction_name, page_title, page_description, start_datetime, end_datetime, sale_mode, is_public, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (name, page_title, page_description, start_datetime, end_datetime, sale_mode, 1 if is_public else 0, current_user.id),
                )
                new_id = int(cur.lastrowid)
            if selected_ids:
                for user_id in selected_ids:
                    execute(
                        cur,
                        "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (%s, %s, TRUE)",
                        "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (?, ?, 1)",
                        (new_id, user_id),
                    )
            else:
                execute(
                    cur,
                    "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (%s, NULL, FALSE)",
                    "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (?, NULL, 0)",
                    (new_id,),
                )
            conn.commit()
            cur.close()
            flash(f"オークション「{name}」を{'一時保存' if draft else '作成'}しました", "success")
            return redirect(url_for("admin_proxy_service_detail", auction_id=new_id))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), "error")
            return redirect(url_for("admin_proxy_service_create"))
        except Exception as exc:
            conn.rollback()
            print(f"[PROXY] create failed: {exc}", flush=True)
            flash("オークションを作成できませんでした", "error")
            return redirect(url_for("admin_proxy_service_create"))
        finally:
            conn.close()

    def settings_guard(auction_id):
        if not (current_user.is_owner() or current_user.is_admin()):
            flash("この機能はオーナーまたは管理者のみ利用可能です", "error")
            return redirect(url_for("index"))
        if not current_user.can_manage_proxy_service():
            return module.get_proxy_publish_denied_response()

        name = str(request.form.get("auction_name") or "").strip()
        page_title = str(request.form.get("page_title") or "").strip() or "代行仕入れサービス"
        page_description = str(request.form.get("page_description") or "").strip()
        sale_mode = str(request.form.get("sale_mode") or "auction").strip()
        is_draft = request.form.get("submit_action") == "draft"
        requested_public = request.form.get("is_public") == "on" and not is_draft
        start_datetime = module.normalize_proxy_service_datetime_input(
            request.form.get("start_datetime") or None
        )
        end_datetime = module.normalize_proxy_service_datetime_input(
            request.form.get("end_datetime") or None
        )
        conn = module.get_db()
        ensure_schema(conn)
        cur = cursor_for(conn, rows=True)
        try:
            if not is_postgres:
                cur.execute("BEGIN IMMEDIATE")
            if not name:
                raise ValueError("オークション名は必須です")
            if len(name) > 100:
                raise ValueError("オークション名は100文字以内で入力してください")
            if sale_mode not in {"auction", "fixed"}:
                raise ValueError("販売方式が不正です")
            execute(
                cur,
                "SELECT * FROM proxy_service_settings WHERE id = %s FOR UPDATE",
                "SELECT * FROM proxy_service_settings WHERE id = ?",
                (auction_id,),
            )
            settings = row_dict(cur.fetchone())
            if not settings:
                raise LookupError("オークションが見つかりません")
            now = module.get_jst_now()
            state = module.get_proxy_service_auction_state(settings, now=now)
            if state.get("is_ended"):
                raise ValueError("終了済みオークションは再利用できません。新しいオークションを作成してください")
            selected_ids = validate_client_ids(conn, request.form.getlist("selected_users"))
            if requested_public and not selected_ids:
                raise ValueError("公開する場合は対象ユーザーを1名以上選択してください")

            if state.get("is_open"):
                original_end = module.parse_proxy_service_datetime(settings.get("end_datetime"))
                requested_end = module.parse_proxy_service_datetime(end_datetime)
                if requested_end and requested_end <= now:
                    raise ValueError("公開中の終了日時は、現在時刻より後の日時にしてください")
                if original_end and requested_end and requested_end < original_end:
                    raise ValueError("公開中の終了日時は短縮できません。延長する場合のみ変更できます")
                requested_public = bool(settings.get("is_public"))
                start_datetime = module.normalize_proxy_service_datetime_input(settings.get("start_datetime"))
                sale_mode = settings.get("sale_mode") or "auction"

            start_dt = module.parse_proxy_service_datetime(start_datetime)
            end_dt = module.parse_proxy_service_datetime(end_datetime)
            if start_dt and end_dt and start_dt >= end_dt:
                raise ValueError("終了日時は開始日時より後に設定してください")

            execute(
                cur,
                """
                UPDATE proxy_service_settings
                SET is_public = %s, auction_name = %s, page_title = %s,
                    page_description = %s, start_datetime = %s, end_datetime = %s,
                    sale_mode = %s, updated_by = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                """
                UPDATE proxy_service_settings
                SET is_public = ?, auction_name = ?, page_title = ?,
                    page_description = ?, start_datetime = ?, end_datetime = ?,
                    sale_mode = ?, updated_by = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    requested_public if is_postgres else (1 if requested_public else 0),
                    name,
                    page_title,
                    page_description,
                    start_datetime,
                    end_datetime,
                    sale_mode,
                    current_user.id,
                    auction_id,
                ),
            )
            execute(
                cur,
                "DELETE FROM proxy_service_auction_users WHERE auction_id = %s",
                "DELETE FROM proxy_service_auction_users WHERE auction_id = ?",
                (auction_id,),
            )
            if selected_ids:
                for user_id in selected_ids:
                    execute(
                        cur,
                        "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (%s, %s, TRUE)",
                        "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (?, ?, 1)",
                        (auction_id, user_id),
                    )
            else:
                execute(
                    cur,
                    "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (%s, NULL, FALSE)",
                    "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (?, NULL, 0)",
                    (auction_id,),
                )
            conn.commit()
            flash("ページを一時保存しました" if is_draft else "オークション設定を更新しました", "success")
        except LookupError as exc:
            conn.rollback()
            flash(str(exc), "error")
            return redirect(url_for("admin_proxy_service"))
        except ValueError as exc:
            conn.rollback()
            flash(str(exc), "error")
            return redirect(url_for("admin_proxy_service_detail", auction_id=auction_id))
        except Exception as exc:
            conn.rollback()
            print(f"[PROXY] settings update failed for auction {auction_id}: {type(exc).__name__}", flush=True)
            flash("オークション設定を更新できませんでした", "error")
            return redirect(url_for("admin_proxy_service_detail", auction_id=auction_id))
        finally:
            cur.close()
            conn.close()
        return redirect(url_for("admin_proxy_service_detail", auction_id=auction_id))

    def validate_publishable_auction(auction_id):
        conn = module.get_db()
        ensure_schema(conn)
        cur = cursor_for(conn, rows=True)
        try:
            execute(
                cur,
                "SELECT id, auction_name FROM proxy_service_settings WHERE id = %s",
                "SELECT id, auction_name FROM proxy_service_settings WHERE id = ?",
                (auction_id,),
            )
            settings = row_dict(cur.fetchone())
            if not settings:
                raise LookupError("オークションが見つかりません")
            if not str(settings.get("auction_name") or "").strip():
                raise ValueError("公開前にオークション名を設定してください")
            users, use_auction_scope = fetch_valid_clients(conn, auction_id)
            if not use_auction_scope or not any(user.get("is_selected") for user in users):
                raise ValueError("公開前に有効な一般クライアントを1名以上選択してください")
        finally:
            cur.close()
            conn.close()

    def start_guard(auction_id):
        try:
            validate_publishable_auction(auction_id)
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        return original_start_view(auction_id)

    def visibility_guard(auction_id):
        data = parse_request_data()
        requested_public = str(data.get("is_public") or data.get("visible") or "").lower() in {
            "1", "true", "yes", "on"
        }
        if requested_public:
            try:
                validate_publishable_auction(auction_id)
            except LookupError as exc:
                return jsonify({"success": False, "error": str(exc)}), 404
            except ValueError as exc:
                return jsonify({"success": False, "error": str(exc)}), 409
        return original_visibility_view(auction_id)

    def manual_finalize(auction_id):
        denied = validate_admin_actor()
        if denied:
            return denied
        try:
            result = reconcile_auction(
                auction_id,
                now=module.get_jst_now(),
                actor_user_id=current_user.id,
                require_ended=True,
            )
            return jsonify({
                "success": True,
                "message": (
                    f"落札確定 {result['finalized']}件 / "
                    f"落札者なし・在庫へ返却 {result['released']}件 / "
                    f"既存反映補修 {result['repaired']}件 / "
                    f"管理者確認待ち {result['unresolved']}件 / "
                    f"処理済み {result['already']}件"
                ),
                **result,
            })
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        except Exception as exc:
            print(f"[PROXY] manual finalize failed: {exc}", flush=True)
            return jsonify({"success": False, "error": "落札確定処理に失敗しました"}), 500

    def reflect_item_with_integrity(auction_id, item_id):
        denied = validate_admin_actor()
        if denied:
            return denied
        try:
            result = reconcile_auction(
                auction_id,
                now=module.get_jst_now(),
                actor_user_id=current_user.id,
                require_ended=True,
                only_item_id=item_id,
            )
            return jsonify({
                "success": True,
                "message": (
                    f"落札確定 {result['finalized']}件 / "
                    f"在庫へ返却 {result['released']}件 / "
                    f"既存反映補修 {result['repaired']}件 / "
                    f"管理者確認待ち {result['unresolved']}件 / "
                    f"処理済み {result['already']}件"
                ),
                **result,
            })
        except LookupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 409
        except Exception as exc:
            print(f"[PROXY] item reconciliation failed for {auction_id}/{item_id}: {type(exc).__name__}", flush=True)
            return jsonify({"success": False, "error": "落札結果を確定できませんでした"}), 409

    def reflect_all_with_integrity(auction_id):
        return manual_finalize(auction_id)

    def end_now_and_reconcile(auction_id):
        response = original_end_now_view(auction_id)
        status_code = response[1] if isinstance(response, tuple) and len(response) > 1 else 200
        if int(status_code) < 400:
            try:
                reconcile_auction(
                    auction_id,
                    now=module.get_jst_now(),
                    actor_user_id=current_user.id,
                    require_ended=True,
                )
            except Exception as exc:
                print(f"[PROXY] end-now reconciliation failed: {exc}", flush=True)
                return jsonify({"success": False, "error": "終了結果の確定に失敗しました"}), 500
        return response

    request_reconcile_lock = threading.Lock()
    last_request_reconcile = {"at": None}

    @app.before_request
    def proxy_service_access_reconciliation():
        path = request.path or ""
        if not (path.startswith("/proxy-service") or path.startswith("/admin/proxy-service")):
            return None
        now = module.get_jst_now()
        with request_reconcile_lock:
            last_at = last_request_reconcile.get("at")
            if last_at and (now - last_at).total_seconds() < 1:
                return None
            last_request_reconcile["at"] = now
        try:
            reconcile_expired_auctions(now=now)
        except Exception as exc:
            print(f"[PROXY] access reconciliation failed: {exc}", flush=True)
        return None

    def install_scheduler_job():
        scheduler = getattr(module, "scheduler", None)
        if scheduler is None or not getattr(scheduler, "running", False):
            return
        scheduler.add_job(
            reconcile_expired_auctions,
            "interval",
            minutes=1,
            id="proxy_service_expiry_reconciliation",
            name="代行仕入れ終了結果確定",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    def init_scheduler_with_proxy_job():
        result = original_init_scheduler()
        install_scheduler_job()
        return result

    ensure_schema()
    module.proxy_service_auction_name_display = auction_name_display
    module.ensure_proxy_service_integrity_schema = ensure_schema
    module.fetch_proxy_service_target_users = fetch_proxy_service_target_users
    module.is_proxy_service_user_allowed = is_user_allowed_with_integrity
    module.fetch_proxy_service_items = fetch_proxy_service_items
    module.create_proxy_service_reflected_item = create_reflected_item_with_integrity
    module.annotate_proxy_service_items = annotate_items_with_integrity
    module.build_proxy_service_history_datasets = history_datasets_with_integrity
    module.build_public_proxy_service_sections = public_sections_with_integrity
    module.count_proxy_service_user_bids = count_user_bids
    module.reconcile_proxy_service_auction = reconcile_auction
    module.reconcile_expired_proxy_service_auctions = reconcile_expired_auctions
    module.admin_proxy_service_toggle_item = safe_toggle_item
    module.admin_proxy_service_bulk_toggle = safe_bulk_toggle
    if callable(original_init_scheduler):
        module.init_scheduler = init_scheduler_with_proxy_job
    install_scheduler_job()

    app.view_functions["admin_proxy_service_create"] = login_required(admin_create)
    if callable(original_settings_view):
        app.view_functions["admin_proxy_service_settings"] = login_required(settings_guard)
    if callable(original_start_view):
        app.view_functions["admin_proxy_service_start"] = login_required(start_guard)
    if callable(original_visibility_view):
        app.view_functions["admin_proxy_service_visibility"] = login_required(visibility_guard)
    if callable(original_end_now_view):
        app.view_functions["admin_proxy_service_end_now"] = login_required(end_now_and_reconcile)
    app.view_functions["admin_proxy_service_finalize"] = login_required(manual_finalize)
    app.view_functions["admin_proxy_service_reflect_item"] = login_required(reflect_item_with_integrity)
    app.view_functions["admin_proxy_service_reflect_all"] = login_required(reflect_all_with_integrity)
    app.view_functions["admin_proxy_service_toggle_item"] = login_required(safe_toggle_item)
    app.view_functions["admin_proxy_service_bulk_toggle"] = login_required(safe_bulk_toggle)
