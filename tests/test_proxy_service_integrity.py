# -*- coding: utf-8 -*-
"""Isolated integration tests for the Step D proxy-service integrity patch."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid

from werkzeug.security import generate_password_hash


class ProxyServiceIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, database_path = tempfile.mkstemp(prefix="kaika_proxy_d_", suffix=".db")
        os.close(fd)
        cls.database_path = Path(database_path)
        os.environ["MERCHANDISE_DB_PATH"] = str(cls.database_path)
        os.environ["GOOGLE_DRIVE_ENABLED"] = "false"
        os.environ["PRIMARY_DOMAIN_REDIRECT"] = "0"
        os.environ.pop("DATABASE_URL", None)

        import render_app

        cls.runtime = render_app
        cls.module = render_app.module
        cls.app = render_app.app
        cls.app.config.update(TESTING=True)
        cls.prefix = f"DVERIFY_{uuid.uuid4().hex[:10]}"

        conn = cls.module.get_db()
        row = conn.execute("SELECT id FROM users WHERE role IN ('owner', 'admin') ORDER BY id LIMIT 1").fetchone()
        cls.owner_id = int(row[0])
        conn.execute(
            "UPDATE users SET role = 'owner', admin_permissions = NULL WHERE id = ?",
            (cls.owner_id,),
        )
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        scheduler = getattr(cls.module, "scheduler", None)
        if scheduler is not None and getattr(scheduler, "running", False):
            scheduler.shutdown(wait=False)
        try:
            import gc
            import time

            gc.collect()
            for attempt in range(10):
                try:
                    cls.database_path.unlink(missing_ok=True)
                    break
                except PermissionError:
                    if attempt == 9:
                        raise
                    time.sleep(0.1)
        finally:
            os.environ.pop("MERCHANDISE_DB_PATH", None)

    def db(self):
        return self.module.get_db()

    def unique(self, label):
        return f"{self.prefix}_{label}_{uuid.uuid4().hex[:7]}"

    def add_user(
        self,
        label,
        *,
        role="user",
        status="inactive",
        display_name=None,
        budget=500000,
        last_name="",
        first_name="",
    ):
        username = self.unique(label)
        conn = self.db()
        cur = conn.execute(
            """
            INSERT INTO users (
                username, email, password_hash, role, display_name,
                subscription_status, proxy_service_budget, last_name, first_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                username,
                f"{username.lower()}@example.test",
                generate_password_hash("test-password"),
                role,
                display_name,
                status,
                budget,
                last_name,
                first_name,
            ),
        )
        user_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return user_id, username

    def add_auction(self, label, users=(), *, ended=False, sale_mode="auction", public=True, name=None):
        now = self.module.get_jst_now()
        start = now - timedelta(hours=2)
        end = now - timedelta(minutes=1) if ended else now + timedelta(hours=2)
        conn = self.db()
        cur = conn.execute(
            """
            INSERT INTO proxy_service_settings (
                auction_name, page_title, page_description, start_datetime,
                end_datetime, sale_mode, is_public, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.unique(label) if name is None else name,
                "代行仕入れサービス",
                f"{self.prefix} verification",
                start.strftime("%Y-%m-%d %H:%M:%S"),
                end.strftime("%Y-%m-%d %H:%M:%S"),
                sale_mode,
                1 if public else 0,
                self.owner_id,
            ),
        )
        auction_id = int(cur.lastrowid)
        if users:
            conn.executemany(
                "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (?, ?, 1)",
                [(auction_id, user_id) for user_id in users],
            )
        else:
            conn.execute(
                "INSERT INTO proxy_service_auction_users (auction_id, user_id, is_enabled) VALUES (?, NULL, 0)",
                (auction_id,),
            )
        conn.commit()
        conn.close()
        return auction_id

    def add_item(self, label, *, auction_id=None, sold=False, scope="admin", price=1000):
        conn = self.db()
        cur = conn.execute(
            """
            INSERT INTO merchandise (
                user_id, product_name, brand_name, purchase_price, listing_price,
                sale_date, scope, auction_id, show_in_proxy_service, is_listed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                self.owner_id,
                self.unique(label),
                "D検証ブランド",
                price,
                price,
                self.module.get_jst_now().strftime("%Y-%m-%d") if sold else None,
                scope,
                auction_id,
                1 if auction_id is not None else 0,
            ),
        )
        item_id = int(cur.lastrowid)
        conn.commit()
        conn.close()
        return item_id

    def add_bid(self, auction_id, item_id, user_id, amount, name):
        conn = self.db()
        conn.execute(
            """
            INSERT INTO proxy_service_bids
                (auction_id, merchandise_id, user_id, bidder_name, bid_amount)
            VALUES (?, ?, ?, ?, ?)
            """,
            (auction_id, item_id, user_id, name, amount),
        )
        conn.commit()
        conn.close()

    def client_as(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        return client

    def scalar(self, query, params=()):
        conn = self.db()
        value = conn.execute(query, params).fetchone()[0]
        conn.close()
        return value

    def test_d1_candidates_and_tampered_user_ids(self):
        user_a, username_a = self.add_user("CLIENT_A", display_name="検証 花子")
        user_no_name, username_no_name = self.add_user("NO_NAME", display_name=None)
        invalid_user, _ = self.add_user("DISABLED", status="disabled", display_name="無効")
        admin_user, _ = self.add_user("ADMIN", role="admin", display_name="管理者候補外")

        conn = self.db()
        users, _ = self.module.fetch_proxy_service_target_users(conn, None)
        conn.close()
        ids = {int(user["id"]) for user in users}
        self.assertIn(user_a, ids)
        self.assertIn(user_no_name, ids)
        self.assertNotIn(invalid_user, ids)
        self.assertNotIn(admin_user, ids)
        unnamed = next(user for user in users if int(user["id"]) == user_no_name)
        self.assertEqual("氏名未登録", unnamed["display_name_label"])
        self.assertEqual(username_no_name, unnamed["member_number"])
        self.assertIn(str(user_no_name), unnamed["search_text"])

        admin = self.client_as(self.owner_id)
        page = admin.get("/admin/proxy-service/create")
        self.assertEqual(200, page.status_code)
        html = page.get_data(as_text=True)
        self.assertIn("氏名・ユーザーID・会員番号で検索", html)
        self.assertIn("氏名未登録", html)
        self.assertIn(username_a, html)
        self.assertNotIn("管理者候補外", html)

        before = self.scalar("SELECT COUNT(*) FROM proxy_service_settings")
        invalid_response = admin.post(
            "/admin/proxy-service/create",
            data={
                "auction_name": self.unique("INVALID_TARGET"),
                "sale_mode": "auction",
                "is_public": "on",
                "selected_users": str(admin_user),
            },
        )
        self.assertEqual(302, invalid_response.status_code)
        self.assertEqual(before, self.scalar("SELECT COUNT(*) FROM proxy_service_settings"))

        blank_name = admin.post(
            "/admin/proxy-service/create",
            data={"auction_name": "   ", "selected_users": str(user_a)},
        )
        self.assertEqual(302, blank_name.status_code)
        self.assertEqual(before, self.scalar("SELECT COUNT(*) FROM proxy_service_settings"))

        valid_name = self.unique("VALID_AUCTION")
        valid = admin.post(
            "/admin/proxy-service/create",
            data={
                "auction_name": valid_name,
                "sale_mode": "auction",
                "selected_users": [str(user_a), str(user_no_name)],
                "submit_action": "draft",
            },
        )
        self.assertEqual(302, valid.status_code)
        created = self.scalar(
            "SELECT COUNT(*) FROM proxy_service_settings WHERE auction_name = ?", (valid_name,)
        )
        self.assertEqual(1, created)

    def test_d2_no_bid_release_history_and_relisting(self):
        user_a, _ = self.add_user("D2_CLIENT", display_name="D2利用者")
        old_auction = self.add_auction("D2_OLD", [user_a], ended=True)
        item_id = self.add_item("D2_NO_BID", auction_id=old_auction)

        first = self.module.reconcile_proxy_service_auction(
            old_auction, actor_user_id=self.owner_id, require_ended=True
        )
        second = self.module.reconcile_proxy_service_auction(
            old_auction, actor_user_id=self.owner_id, require_ended=True
        )
        self.assertEqual(1, first["released"])
        self.assertEqual(0, second["released"])

        conn = self.db()
        source = dict(conn.execute("SELECT * FROM merchandise WHERE id = ?", (item_id,)).fetchone())
        run = dict(
            conn.execute(
                "SELECT * FROM proxy_service_auction_items WHERE auction_id = ? AND merchandise_id = ?",
                (old_auction, item_id),
            ).fetchone()
        )
        old_items = self.module.fetch_proxy_service_items(conn, old_auction)
        conn.close()
        self.assertIsNone(source["sale_date"])
        self.assertIsNone(source["auction_id"])
        self.assertEqual(0, source["show_in_proxy_service"])
        self.assertEqual("ended_no_bid", run["outcome_status"])
        self.assertEqual(1, len(old_items))
        self.assertEqual("ended_no_bid", old_items[0]["proxy_run_status"])
        self.assertEqual(
            0,
            self.scalar("SELECT COUNT(*) FROM merchandise WHERE proxy_parent_item_id = ?", (item_id,)),
        )

        new_auction = self.add_auction("D2_RELIST", [user_a], public=False)
        response = self.client_as(self.owner_id).post(
            f"/admin/proxy-service/{new_auction}/toggle-item/{item_id}",
            json={"proxy_price": 1500},
        )
        self.assertEqual(200, response.status_code, response.get_data(as_text=True))
        self.assertEqual(
            new_auction,
            self.scalar("SELECT auction_id FROM merchandise WHERE id = ?", (item_id,)),
        )
        self.assertEqual(
            2,
            self.scalar(
                "SELECT COUNT(*) FROM proxy_service_auction_items WHERE merchandise_id = ?",
                (item_id,),
            ),
        )

    def test_legacy_global_client_fallback_remains_available(self):
        user_a, _ = self.add_user("LEGACY_GLOBAL", display_name="旧設定利用者", budget=10000)
        disabled_user, _ = self.add_user(
            "LEGACY_GLOBAL_DISABLED", display_name="旧設定無効", status="disabled", budget=10000
        )
        auction_id = self.add_auction("LEGACY_GLOBAL_AUCTION", [user_a])
        item_id = self.add_item("LEGACY_GLOBAL_ITEM", auction_id=auction_id)
        conn = self.db()
        conn.execute("DELETE FROM proxy_service_auction_users WHERE auction_id = ?", (auction_id,))
        conn.execute(
            "INSERT OR REPLACE INTO proxy_service_users (user_id, is_enabled) VALUES (?, 1)",
            (user_a,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO proxy_service_users (user_id, is_enabled) VALUES (?, 1)",
            (disabled_user,),
        )
        conn.commit()
        self.assertTrue(self.module.is_proxy_service_user_allowed(conn, user_a, auction_id))
        self.assertFalse(self.module.is_proxy_service_user_allowed(conn, disabled_user, auction_id))
        conn.close()

        detail = self.client_as(user_a).get(f"/proxy-service/{auction_id}")
        self.assertEqual(200, detail.status_code)
        bid = self.client_as(user_a).post(
            "/proxy-service/bid", json={"merchandise_id": item_id, "bid_amount": 1500}
        )
        self.assertEqual(200, bid.status_code, bid.get_data(as_text=True))

    def test_d3_highest_bid_reflection_visibility_and_fixed_purchase(self):
        user_a, username_a = self.add_user("D3_LOW", display_name="低額入札者", budget=50000)
        user_b, username_b = self.add_user("D3_WIN", display_name="最高入札者", budget=50000)
        auction_id = self.add_auction("D3_AUCTION", [user_a, user_b], ended=True)
        item_id = self.add_item("D3_AUCTION_ITEM", auction_id=auction_id, price=1000)
        product_name = self.scalar("SELECT product_name FROM merchandise WHERE id = ?", (item_id,))
        self.add_bid(auction_id, item_id, user_a, 1500, "低額入札者")
        self.add_bid(auction_id, item_id, user_b, 2500, "最高入札者")

        result = self.module.reconcile_proxy_service_auction(
            auction_id, actor_user_id=self.owner_id, require_ended=True
        )
        self.assertEqual(1, result["finalized"])
        conn = self.db()
        source = dict(conn.execute("SELECT * FROM merchandise WHERE id = ?", (item_id,)).fetchone())
        children = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM merchandise WHERE proxy_parent_item_id = ?", (item_id,)
            ).fetchall()
        ]
        conn.close()
        self.assertEqual(1, len(children))
        child = children[0]
        self.assertEqual(user_b, child["user_id"])
        self.assertEqual("user", child["scope"])
        self.assertEqual(2500, child["purchase_price"])
        self.assertEqual(2500, child["wholesale_price"])
        self.assertEqual(auction_id, child["proxy_source_auction_id"])
        self.assertEqual(2500, source["sale_price"])
        self.assertIsNotNone(source["sale_date"])
        self.assertEqual(47500, self.scalar("SELECT proxy_service_budget FROM users WHERE id = ?", (user_b,)))
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM user_keisan WHERE user_id = ? AND proxy_service_auction_id = ? AND status = 'draft'",
                (user_b, auction_id),
            ),
        )

        winner_home = self.client_as(user_b).get("/")
        self.assertEqual(200, winner_home.status_code)
        self.assertIn(product_name, winner_home.get_data(as_text=True))
        winner_detail = self.client_as(user_b).get(f"/view/{child['id']}")
        self.assertEqual(200, winner_detail.status_code)
        self.assertIn(product_name, winner_detail.get_data(as_text=True))
        other_detail = self.client_as(user_a).get(f"/view/{child['id']}")
        self.assertNotIn(product_name, other_detail.get_data(as_text=True))
        admin_products = self.client_as(self.owner_id).get(f"/admin/user-products?owner_id={user_b}")
        self.assertEqual(200, admin_products.status_code)
        self.assertIn(product_name, admin_products.get_data(as_text=True))

        fixed_auction = self.add_auction("D3_FIXED", [user_a], sale_mode="fixed")
        fixed_item = self.add_item("D3_FIXED_ITEM", auction_id=fixed_auction, price=3000)
        fixed_response = self.client_as(user_a).post(
            "/proxy-service/purchase", json={"merchandise_id": fixed_item}
        )
        self.assertEqual(200, fixed_response.status_code, fixed_response.get_data(as_text=True))
        fixed_children = self.scalar(
            "SELECT COUNT(*) FROM merchandise WHERE proxy_parent_item_id = ? AND user_id = ? AND scope = 'user'",
            (fixed_item, user_a),
        )
        self.assertEqual(1, fixed_children)
        self.assertEqual(
            fixed_auction,
            self.scalar(
                "SELECT proxy_source_auction_id FROM merchandise WHERE proxy_parent_item_id = ?",
                (fixed_item,),
            ),
        )
        self.assertTrue(username_a)
        self.assertTrue(username_b)

    def test_duplicate_selection_finalize_race_and_atomic_bulk(self):
        user_a, _ = self.add_user("RACE_A", display_name="競合A", budget=100000)
        user_b, _ = self.add_user("RACE_B", display_name="競合B", budget=100000)
        auction_one = self.add_auction("ACTIVE_ONE", [user_a, user_b])
        auction_two = self.add_auction("ACTIVE_TWO", [user_a, user_b], public=False)
        occupied = self.add_item("OCCUPIED", auction_id=auction_one)
        candidate = self.add_item("BULK_CANDIDATE")
        user_owned = self.add_item("USER_OWNED", scope="user")

        admin = self.client_as(self.owner_id)
        duplicate = admin.post(
            f"/admin/proxy-service/{auction_two}/toggle-item/{occupied}", json={"proxy_price": 1000}
        )
        self.assertEqual(409, duplicate.status_code)
        bulk = admin.post(
            f"/admin/proxy-service/{auction_two}/bulk-toggle",
            json={"action": "add", "item_ids": [candidate, user_owned]},
        )
        self.assertEqual(409, bulk.status_code)
        self.assertIsNone(self.scalar("SELECT auction_id FROM merchandise WHERE id = ?", (candidate,)))

        ended = self.add_auction("DOUBLE_FINALIZE", [user_a, user_b], ended=True)
        item_id = self.add_item("DOUBLE_ITEM", auction_id=ended)
        self.add_bid(ended, item_id, user_a, 2000, "競合A")
        barrier = threading.Barrier(2)

        def finalize_once():
            client = self.client_as(self.owner_id)
            barrier.wait(timeout=5)
            response = client.post(f"/admin/proxy-service/{ended}/finalize")
            return response.status_code

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = list(executor.map(lambda _: finalize_once(), range(2)))
        self.assertNotIn(500, statuses)
        self.assertTrue(all(status in {200, 409} for status in statuses))
        self.assertEqual(
            1,
            self.scalar("SELECT COUNT(*) FROM merchandise WHERE proxy_parent_item_id = ?", (item_id,)),
        )
        self.assertEqual(98000, self.scalar("SELECT proxy_service_budget FROM users WHERE id = ?", (user_a,)))

    def test_deadline_bid_race_never_misclassifies_successful_bid(self):
        user_a, _ = self.add_user("DEADLINE", display_name="締切入札者", budget=100000)
        auction_id = self.add_auction("DEADLINE_RACE", [user_a])
        item_id = self.add_item("DEADLINE_ITEM", auction_id=auction_id)
        barrier = threading.Barrier(2)
        future_now = self.module.get_jst_now() + timedelta(hours=3)

        def bid_once():
            client = self.client_as(user_a)
            barrier.wait(timeout=5)
            response = client.post(
                "/proxy-service/bid", json={"merchandise_id": item_id, "bid_amount": 1500}
            )
            return response.status_code

        def expire_once():
            barrier.wait(timeout=5)
            return self.module.reconcile_proxy_service_auction(
                auction_id,
                now=future_now,
                actor_user_id=self.owner_id,
                require_ended=True,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            bid_future = executor.submit(bid_once)
            settle_future = executor.submit(expire_once)
            bid_status = bid_future.result(timeout=15)
            settle_future.result(timeout=15)

        self.assertNotEqual(500, bid_status)
        bid_count = self.scalar(
            "SELECT COUNT(*) FROM proxy_service_bids WHERE auction_id = ? AND merchandise_id = ?",
            (auction_id, item_id),
        )
        child_count = self.scalar(
            "SELECT COUNT(*) FROM merchandise WHERE proxy_parent_item_id = ?", (item_id,)
        )
        outcome = self.scalar(
            "SELECT outcome_status FROM proxy_service_auction_items WHERE auction_id = ? AND merchandise_id = ?",
            (auction_id, item_id),
        )
        if bid_status == 200:
            self.assertEqual(1, bid_count)
            self.assertEqual(1, child_count)
            self.assertEqual("auction_won", outcome)
        else:
            self.assertEqual(0, bid_count)
            self.assertEqual(0, child_count)
            self.assertEqual("ended_no_bid", outcome)

    def test_database_failure_rolls_back_all_winner_changes(self):
        user_a, _ = self.add_user("ROLLBACK", display_name="Rollback利用者", budget=10000)
        auction_id = self.add_auction("ROLLBACK_AUCTION", [user_a], ended=True)
        item_id = self.add_item("ROLLBACK_ITEM", auction_id=auction_id)
        self.add_bid(auction_id, item_id, user_a, 2000, "Rollback利用者")

        conn = self.db()
        conn.execute(
            """
            CREATE TRIGGER fail_proxy_child_insert
            BEFORE INSERT ON merchandise
            WHEN NEW.proxy_parent_item_id IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'forced reflected item failure');
            END
            """
        )
        conn.commit()
        conn.close()

        with self.assertRaises(Exception):
            self.module.reconcile_proxy_service_auction(
                auction_id, actor_user_id=self.owner_id, require_ended=True
            )

        self.assertIsNone(self.scalar("SELECT sale_date FROM merchandise WHERE id = ?", (item_id,)))
        self.assertEqual(1, self.scalar("SELECT show_in_proxy_service FROM merchandise WHERE id = ?", (item_id,)))
        self.assertEqual(10000, self.scalar("SELECT proxy_service_budget FROM users WHERE id = ?", (user_a,)))
        self.assertEqual(
            0,
            self.scalar("SELECT COUNT(*) FROM merchandise WHERE proxy_parent_item_id = ?", (item_id,)),
        )
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM user_keisan WHERE user_id = ? AND proxy_service_auction_id = ?",
                (user_a, auction_id),
            ),
        )

        conn = self.db()
        conn.execute("DROP TRIGGER fail_proxy_child_insert")
        conn.commit()
        conn.close()

    def test_invalid_posts_disabled_clients_and_ended_settings_are_rejected(self):
        valid_user, _ = self.add_user("VALID_POST", display_name="有効利用者", budget=10000)
        disabled_user, _ = self.add_user(
            "DISABLED_POST", display_name="無効利用者", status="disabled", budget=10000
        )
        auction_id = self.add_auction("POST_GUARD", [valid_user, disabled_user])
        item_id = self.add_item("POST_GUARD_ITEM", auction_id=auction_id)

        valid_client = self.client_as(valid_user)
        for bad_id in ([], {}, True, 0, -1, "not-an-id"):
            bid_response = valid_client.post(
                "/proxy-service/bid",
                json={"merchandise_id": bad_id, "bid_amount": 1500},
            )
            purchase_response = valid_client.post(
                "/proxy-service/purchase", json={"merchandise_id": bad_id}
            )
            self.assertEqual(400, bid_response.status_code)
            self.assertEqual(400, purchase_response.status_code)

        self.assertEqual(
            403,
            self.client_as(disabled_user)
            .post("/proxy-service/bid", json={"merchandise_id": item_id, "bid_amount": 1500})
            .status_code,
        )
        self.assertEqual(
            403,
            self.client_as(self.owner_id)
            .post("/proxy-service/bid", json={"merchandise_id": item_id, "bid_amount": 1500})
            .status_code,
        )

        conn = self.db()
        conn.execute(
            "UPDATE merchandise SET scope = 'user', user_id = ? WHERE id = ?",
            (valid_user, item_id),
        )
        conn.commit()
        conn.close()
        blocked = valid_client.post(
            "/proxy-service/bid", json={"merchandise_id": item_id, "bid_amount": 1500}
        )
        self.assertEqual(404, blocked.status_code)
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM proxy_service_bids WHERE merchandise_id = ?", (item_id,)
            ),
        )

        ended_id = self.add_auction("ENDED_SETTINGS", [valid_user], ended=True, name="終了前名称")
        admin = self.client_as(self.owner_id)
        settings_response = admin.post(
            f"/admin/proxy-service/{ended_id}/settings",
            data={
                "auction_name": "改ざん後名称",
                "selected_users": str(valid_user),
                "end_datetime": (
                    self.module.get_jst_now() + timedelta(days=1)
                ).strftime("%Y-%m-%dT%H:%M"),
            },
        )
        self.assertEqual(302, settings_response.status_code)
        self.assertEqual(
            "終了前名称",
            self.scalar("SELECT auction_name FROM proxy_service_settings WHERE id = ?", (ended_id,)),
        )
        self.assertEqual(
            404,
            admin.post(f"/admin/proxy-service/{ended_id}/reflect-item/999999999").status_code,
        )

    def test_legacy_ambiguous_bid_stays_reviewable_and_expiry_continues(self):
        user_a, _ = self.add_user("LEGACY", display_name="同名表示", budget=10000)
        review_auction = self.add_auction("LEGACY_REVIEW", [user_a], ended=True)
        review_item = self.add_item("LEGACY_REVIEW_ITEM", auction_id=review_auction)
        conn = self.db()
        conn.execute(
            """
            INSERT INTO proxy_service_bids
                (auction_id, merchandise_id, user_id, bidder_name, bid_amount, created_at)
            VALUES (NULL, ?, NULL, ?, ?, ?)
            """,
            (
                review_item,
                "同名表示",
                2500,
                (self.module.get_jst_now() - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        conn.close()

        second_auction = self.add_auction("EXPIRY_CONTINUES", [user_a], ended=True)
        second_item = self.add_item("EXPIRY_CONTINUES_ITEM", auction_id=second_auction)
        summaries = self.module.reconcile_expired_proxy_service_auctions()
        summary_by_id = {entry["auction_id"]: entry for entry in summaries}
        self.assertEqual(1, summary_by_id[review_auction]["unresolved"])
        self.assertEqual(1, summary_by_id[second_auction]["released"])
        self.assertIsNone(self.scalar("SELECT sale_date FROM merchandise WHERE id = ?", (review_item,)))
        self.assertEqual(
            "needs_review",
            self.scalar(
                "SELECT outcome_status FROM proxy_service_auction_items WHERE auction_id = ? AND merchandise_id = ?",
                (review_auction, review_item),
            ),
        )
        conn = self.db()
        fetched = self.module.fetch_proxy_service_items(conn, review_auction)
        conn.close()
        self.assertEqual("needs_review", fetched[0]["proxy_run_status"])
        self.assertIsNone(self.scalar("SELECT auction_id FROM merchandise WHERE id = ?", (second_item,)))

        relisted_auction = self.add_auction("LEGACY_RELISTED", [user_a], ended=True)
        relisted_item = self.add_item("LEGACY_RELISTED_ITEM", auction_id=relisted_auction)
        conn = self.db()
        conn.execute(
            """
            INSERT INTO proxy_service_bids
                (auction_id, merchandise_id, user_id, bidder_name, bid_amount, created_at)
            VALUES (NULL, ?, NULL, ?, ?, ?)
            """,
            (
                relisted_item,
                "旧開催入札者",
                9000,
                (self.module.get_jst_now() - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        conn.commit()
        conn.close()
        self.add_bid(relisted_auction, relisted_item, user_a, 3000, "同名表示")
        relisted_result = self.module.reconcile_proxy_service_auction(
            relisted_auction, actor_user_id=self.owner_id, require_ended=True
        )
        self.assertEqual(1, relisted_result["finalized"])
        self.assertEqual(0, relisted_result["unresolved"])
        self.assertEqual(
            user_a,
            self.scalar(
                "SELECT user_id FROM merchandise WHERE proxy_parent_item_id = ?", (relisted_item,)
            ),
        )

    def test_parallel_wins_cannot_overspend_budget(self):
        user_a, _ = self.add_user("BUDGET_RACE", display_name="残高競合", budget=10000)
        auctions = [self.add_auction(f"BUDGET_{index}", [user_a], ended=True) for index in range(2)]
        item_ids = [
            self.add_item(f"BUDGET_ITEM_{index}", auction_id=auction_id, price=1000)
            for index, auction_id in enumerate(auctions)
        ]
        for auction_id, item_id in zip(auctions, item_ids):
            self.add_bid(auction_id, item_id, user_a, 8000, "残高競合")
        barrier = threading.Barrier(2)

        def settle(auction_id):
            barrier.wait(timeout=5)
            return self.module.reconcile_proxy_service_auction(
                auction_id, actor_user_id=self.owner_id, require_ended=True
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(settle, auctions))
        self.assertEqual(1, sum(result["finalized"] for result in results))
        self.assertEqual(1, sum(result["unresolved"] for result in results))
        self.assertEqual(2000, self.scalar("SELECT proxy_service_budget FROM users WHERE id = ?", (user_a,)))
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM merchandise WHERE proxy_parent_item_id IN (?, ?)",
                tuple(item_ids),
            ),
        )

    def test_multiple_winners_receive_unique_keisan_numbers(self):
        user_a, _ = self.add_user("DOCNO_A", display_name="採番A", budget=10000)
        user_b, _ = self.add_user("DOCNO_B", display_name="採番B", budget=10000)
        auction_id = self.add_auction("DOCNO_AUCTION", [user_a, user_b], ended=True)
        item_a = self.add_item("DOCNO_ITEM_A", auction_id=auction_id)
        item_b = self.add_item("DOCNO_ITEM_B", auction_id=auction_id)
        self.add_bid(auction_id, item_a, user_a, 2000, "採番A")
        self.add_bid(auction_id, item_b, user_b, 2500, "採番B")
        result = self.module.reconcile_proxy_service_auction(
            auction_id, actor_user_id=self.owner_id, require_ended=True
        )
        self.assertEqual(2, result["finalized"])
        conn = self.db()
        numbers = [
            row[0]
            for row in conn.execute(
                "SELECT document_no FROM user_keisan WHERE proxy_service_auction_id = ? ORDER BY id",
                (auction_id,),
            ).fetchall()
        ]
        conn.close()
        self.assertEqual(2, len(numbers))
        self.assertEqual(2, len(set(numbers)))

    def test_legacy_partial_sale_repairs_reflection_without_double_budget_charge(self):
        user_a, _ = self.add_user("PARTIAL_REPAIR", display_name="反映補修", budget=10000)
        auction_id = self.add_auction("PARTIAL_REPAIR_AUCTION", [user_a], ended=True)
        item_id = self.add_item("PARTIAL_REPAIR_ITEM", auction_id=auction_id)
        self.add_bid(auction_id, item_id, user_a, 2500, "反映補修")
        conn = self.db()
        conn.execute(
            """
            UPDATE merchandise
            SET sale_date = ?, sale_price = 2500, sale_type = 'auction',
                sales_destination = '旧処理で確定済み', show_in_proxy_service = 0
            WHERE id = ?
            """,
            (self.module.get_jst_now().strftime("%Y-%m-%d"), item_id),
        )
        conn.commit()
        conn.close()
        result = self.module.reconcile_proxy_service_auction(
            auction_id, actor_user_id=self.owner_id, require_ended=True
        )
        self.assertEqual(1, result["repaired"])
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM merchandise WHERE proxy_parent_item_id = ? AND user_id = ?",
                (item_id, user_a),
            ),
        )
        self.assertEqual(10000, self.scalar("SELECT proxy_service_budget FROM users WHERE id = ?", (user_a,)))
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM user_keisan WHERE proxy_service_auction_id = ? AND user_id = ?",
                (auction_id, user_a),
            ),
        )

    def test_legacy_note_marker_uses_exact_numeric_boundary(self):
        user_a, _ = self.add_user("NOTE_BOUNDARY", display_name="境界利用者", budget=10000)
        auction_id = self.add_auction("NOTE_BOUNDARY_AUCTION", [user_a], ended=True)
        source_id = 900001
        other_source_id = 9000019
        conn = self.db()
        conn.execute(
            """
            INSERT INTO merchandise
                (id, user_id, product_name, purchase_price, listing_price, scope, auction_id, show_in_proxy_service, is_listed)
            VALUES (?, ?, ?, 1000, 1000, 'admin', ?, 1, 0)
            """,
            (source_id, self.owner_id, self.unique("BOUNDARY_SOURCE"), auction_id),
        )
        conn.execute(
            """
            INSERT INTO merchandise
                (id, user_id, product_name, purchase_price, listing_price, scope, is_listed)
            VALUES (?, ?, ?, 1000, 1000, 'admin', 0)
            """,
            (other_source_id, self.owner_id, self.unique("BOUNDARY_OTHER")),
        )
        child_cursor = conn.execute(
            """
            INSERT INTO merchandise
                (user_id, product_name, purchase_price, listing_price, notes, scope, is_listed)
            VALUES (?, ?, 1000, 1000, ?, 'user', 0)
            """,
            (user_a, self.unique("BOUNDARY_CHILD"), f"代行仕入れサービス落札 / 元商品ID:{other_source_id}"),
        )
        existing_child_id = int(child_cursor.lastrowid)
        conn.commit()
        lookup_cur = conn.cursor()
        self.assertIsNone(
            self.module.lookup_proxy_service_reflected_item(
                lookup_cur, source_id, winner_user_id=user_a
            )
        )
        lookup_cur.close()
        source = dict(conn.execute("SELECT * FROM merchandise WHERE id = ?", (source_id,)).fetchone())
        self.module.create_proxy_service_reflected_item(
            conn,
            {
                **source,
                "auction_id": auction_id,
                "auction_name": "境界検証",
                "sale_mode": "auction",
                "winner_user_id": user_a,
                "winner_name": "境界利用者",
                "result_price": 2000,
            },
            self.owner_id,
        )
        conn.commit()
        conn.close()
        self.assertIsNone(
            self.scalar(
                "SELECT proxy_parent_item_id FROM merchandise WHERE id = ?", (existing_child_id,)
            )
        )
        self.assertEqual(
            1,
            self.scalar(
                "SELECT COUNT(*) FROM merchandise WHERE proxy_parent_item_id = ? AND user_id = ?",
                (source_id, user_a),
            ),
        )

    def test_fixed_purchase_reflection_failure_returns_controlled_rollback(self):
        user_a, _ = self.add_user("FIXED_ROLLBACK", display_name="即決Rollback", budget=10000)
        auction_id = self.add_auction("FIXED_ROLLBACK", [user_a], sale_mode="fixed")
        item_id = self.add_item("FIXED_ROLLBACK_ITEM", auction_id=auction_id, price=3000)
        conn = self.db()
        conn.execute(
            """
            CREATE TRIGGER fail_fixed_proxy_child_insert
            BEFORE INSERT ON merchandise
            WHEN NEW.proxy_parent_item_id IS NOT NULL
            BEGIN
                SELECT RAISE(ABORT, 'forced fixed reflected item failure');
            END
            """
        )
        conn.commit()
        conn.close()
        response = self.client_as(user_a).post(
            "/proxy-service/purchase", json={"merchandise_id": item_id}
        )
        self.assertEqual(409, response.status_code)
        self.assertIsNone(self.scalar("SELECT sale_date FROM merchandise WHERE id = ?", (item_id,)))
        self.assertEqual(10000, self.scalar("SELECT proxy_service_budget FROM users WHERE id = ?", (user_a,)))
        self.assertEqual(
            0,
            self.scalar(
                "SELECT COUNT(*) FROM proxy_service_bids WHERE auction_id = ? AND merchandise_id = ?",
                (auction_id, item_id),
            ),
        )
        conn = self.db()
        conn.execute("DROP TRIGGER fail_fixed_proxy_child_insert")
        conn.commit()
        conn.close()

    def test_d4_names_on_admin_and_user_surfaces(self):
        user_a, _ = self.add_user("NAME_USER", display_name="名称確認者")
        long_name = self.unique("非常に長い代行仕入れオークション名") + ("長" * 80)
        auction_id = self.add_auction("NAME", [user_a], name=long_name)
        self.add_item("NAME_ITEM", auction_id=auction_id)

        admin_list = self.client_as(self.owner_id).get("/admin/proxy-service")
        admin_detail = self.client_as(self.owner_id).get(f"/admin/proxy-service/{auction_id}")
        user_list = self.client_as(user_a).get("/proxy-service")
        user_detail = self.client_as(user_a).get(f"/proxy-service/{auction_id}")
        for response in (admin_list, admin_detail, user_list, user_detail):
            self.assertEqual(200, response.status_code, response.get_data(as_text=True)[:500])
            self.assertIn(long_name, response.get_data(as_text=True))

        fallback_id = self.add_auction("BLANK_NAME", [user_a], name="")
        self.add_item("BLANK_NAME_ITEM", auction_id=fallback_id)
        fallback = f"代行仕入れオークション #{fallback_id}"
        self.assertIn(
            fallback,
            self.client_as(user_a).get(f"/proxy-service/{fallback_id}").get_data(as_text=True),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
