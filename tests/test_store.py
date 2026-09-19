import concurrent.futures
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

# Support discovery from any working directory without writing bytecode files.
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location(
    "aurora_store_under_test", Path(__file__).resolve().parents[1] / "shop" / "store.py"
)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
Store = _module.Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "shop.sqlite3")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.store.register(1, "alice", "Alice")
        self.store.register(2, "bob", "Bob")
        self.category = self.store.list_entities("categories")[0]["id"]
        self.product = self.store.list_entities("products")[0]["id"]
        self.method = self.store.list_entities("methods")[0]["id"]
        self.store.update_entity("products", self.product, "active", 1)
        self.store.update_entity("products", self.product, "name", "Original product")
        self.store.update_entity("products", self.product, "price_cents", 250)
        self.store.update_entity("methods", self.method, "wallet", "0xOriginalWallet")
        self.store.update_entity("methods", self.method, "active", 1)

    def new_order(self, uid=1, quantity=1):
        return self.store.checkout(uid, self.product, quantity)

    def review_order(self, uid=1):
        order = self.new_order(uid)
        self.store.choose_method(order["id"], uid, self.method)
        self.store.request_proof(order["id"], uid)
        return self.store.submit_proof(
            order["id"], uid, "file-" + str(order["id"]), "unique-" + str(order["id"])
        )

    def approved_order(self, uid=1, aid=101):
        order = self.review_order(uid)
        return self.store.review(order["id"], aid, True)

    def queued_order(self, uid=1, aid=101):
        order = self.approved_order(uid, aid)
        return self.store.queue_delivery(order["id"], aid, -10012345, 73)

    def delivering_order(self):
        order = self.queued_order()
        return self.store.start_delivery(order["id"])

    def uncertain_order(self):
        order = self.delivering_order()
        return self.store.delivery_result(order["id"], False, "ambiguous timeout")

    def race(self, actions):
        barrier = threading.Barrier(len(actions))

        def worker(action):
            other = Store(self.path)
            try:
                barrier.wait(timeout=10)
                try:
                    return action(other)
                except ValueError:
                    return "invalid-state"
            finally:
                other.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(actions)) as pool:
            return list(pool.map(worker, actions))

    def assertRussianError(self, callback):
        with self.assertRaisesRegex(ValueError, "[А-Яа-я]"):
            callback()

    def test_seed_is_safe_and_not_duplicated_on_reopen(self):
        other_path = str(Path(self.tmp.name) / "seed.sqlite3")
        seeded = Store(other_path)
        try:
            categories = seeded.list_entities("categories")
            products = seeded.list_entities("products")
            methods = seeded.list_entities("methods")
            self.assertEqual(categories[0]["name"], "GPT")
            self.assertEqual(products[0]["category_id"], categories[0]["id"])
            self.assertEqual(products[0]["active"], 0)
            self.assertEqual(methods[0]["active"], 0)
            self.assertEqual(methods[0]["wallet"], "")
            self.assertEqual((methods[0]["network"], methods[0]["asset"]), ("BEP20", "USDT"))
            seeded.update_entity("categories", categories[0]["id"], "name", "Renamed")
        finally:
            seeded.close()
        reopened = Store(other_path)
        try:
            self.assertEqual(len(reopened.list_entities("products")), 1)
            self.assertEqual(len(reopened.list_entities("methods")), 1)
            self.assertEqual(reopened.list_entities("categories")[0]["name"], "Renamed")
        finally:
            reopened.close()

    def test_sqlite_pragmas_and_foreign_keys(self):
        self.assertEqual(self.store._conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(self.store._conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertGreaterEqual(self.store._conn.execute("PRAGMA busy_timeout").fetchone()[0], 5000)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store._conn.execute("INSERT INTO cart VALUES (?,?,?)", (999, self.product, 1))

    def test_settings_defaults_upsert_and_sql_injection(self):
        self.assertEqual(self.store.get("missing"), "")
        self.assertEqual(self.store.get("missing", "fallback"), "fallback")
        key = "x'); DROP TABLE users; --"
        self.store.set(key, "'; DELETE FROM orders; --")
        self.assertEqual(self.store.get(key), "'; DELETE FROM orders; --")
        self.store.set(key, 42)
        self.assertEqual(self.store.get(key), "42")
        self.assertIsNotNone(self.store.user(1))

    def test_entity_creation_field_shapes_and_inactive_defaults(self):
        fields = {
            "categories": {"id", "name", "description", "photo", "active", "sort_order"},
            "products": {
                "id",
                "category_id",
                "name",
                "description",
                "photo",
                "price_cents",
                "active",
                "sort_order",
            },
            "methods": {
                "id",
                "name",
                "description",
                "photo",
                "wallet",
                "asset",
                "network",
                "active",
                "sort_order",
            },
        }
        for kind, expected in fields.items():
            with self.subTest(kind=kind):
                identity = self.store.create_entity(kind)
                self.assertIsInstance(identity, int)
                entity = self.store.entity(kind, identity)
                self.assertEqual(set(entity), expected)
                self.assertFalse(entity["active"])
        self.assertIsNone(self.store.entity("products", 99999))

    def test_entity_and_column_injection_rejected(self):
        for kind in ("users", "products; DROP TABLE users", "", [], None):
            with self.subTest(kind=kind):
                self.assertRussianError(lambda: self.store.list_entities(kind))
                self.assertRussianError(lambda: self.store.create_entity(kind))
        for field in ("id", "status", "name=?,active=1 --", "", [], None):
            self.assertRussianError(lambda: self.store.update_entity("products", self.product, field, "x"))
        self.assertRussianError(lambda: self.store.entity("products", "1 OR 1=1"))
        self.assertRussianError(lambda: self.store.update_entity("products", 99999, "name", "x"))

    def test_entity_text_length_boundaries(self):
        for kind, identity, field, limit in (
            ("categories", self.category, "name", 60),
            ("products", self.product, "name", 60),
            ("products", self.product, "description", 550),
            ("methods", self.method, "description", 550),
            ("methods", self.method, "wallet", 250),
            ("categories", self.category, "photo", 512),
            ("products", self.product, "photo", 512),
            ("methods", self.method, "photo", 512),
        ):
            with self.subTest(kind=kind, field=field):
                self.store.update_entity(kind, identity, field, "Ж" * limit)
                self.assertEqual(len(self.store.entity(kind, identity)[field]), limit)
                self.assertRussianError(
                    lambda: self.store.update_entity(kind, identity, field, "Ж" * (limit + 1))
                )
                self.assertEqual(len(self.store.entity(kind, identity)[field]), limit)
                self.assertRussianError(lambda: self.store.update_entity(kind, identity, field, None))
                self.assertRussianError(lambda: self.store.update_entity(kind, identity, field, "\x00"))

    def test_price_active_sort_and_asset_validation(self):
        for price in (1, 100000000, "350"):
            self.store.update_entity("products", self.product, "price_cents", price)
            self.assertEqual(self.store.entity("products", self.product)["price_cents"], int(price))
        for price in (0, -1, 100000001, 1.5, True, "1.1", "1e3", None, 10**100):
            self.assertRussianError(
                lambda: self.store.update_entity("products", self.product, "price_cents", price)
            )
        for asset in ("USDT", "USDC"):
            self.store.update_entity("methods", self.method, "asset", asset)
        for asset in ("BTC", "ETH", "usdt", "", None, []):
            self.assertRussianError(lambda: self.store.update_entity("methods", self.method, "asset", asset))
        self.store.update_entity("products", self.product, "sort_order", -5)
        self.store.update_entity("products", self.product, "active", False)
        self.assertFalse(self.store.entity("products", self.product)["active"])
        for active in (2, -1, "yes", 0.5):
            self.assertRussianError(
                lambda: self.store.update_entity("products", self.product, "active", active)
            )

    def test_category_fk_filter_and_sort_order(self):
        category = self.store.create_entity("categories")
        second = self.store.create_entity("products")
        self.store.update_entity("products", second, "category_id", category)
        self.store.update_entity("products", second, "sort_order", -1)
        self.assertEqual(self.store.list_entities("products")[0]["id"], second)
        self.assertEqual(
            [p["id"] for p in self.store.list_entities("products", category_id=category)], [second]
        )
        self.assertRussianError(lambda: self.store.update_entity("products", second, "category_id", 9999))
        self.assertEqual(self.store.entity("products", second)["category_id"], category)
        self.assertRussianError(lambda: self.store.list_entities("methods", category_id=category))

    def test_active_product_listing_requires_active_category(self):
        self.assertEqual(len(self.store.list_entities("products", active_only=True)), 1)
        self.store.update_entity("categories", self.category, "active", 0)
        self.assertEqual(self.store.list_entities("products", active_only=True), [])
        self.assertEqual(len(self.store.list_entities("products")), 1)

    def test_sessions_roundtrip_replace_clear_and_isolation(self):
        self.assertIsNone(self.store.session(1))
        data = {"step": 2, "nested": ["value", {"safe": True}], "unicode": "Пример"}
        self.store.set_session(1, "editing", data)
        data["step"] = 9
        self.assertEqual(self.store.session(1)["data"]["step"], 2)
        self.assertIsNone(self.store.session(2))
        self.store.set_session(1, "waiting", {"id": 10})
        self.assertEqual(self.store.session(1), {"mode": "waiting", "data": {"id": 10}})
        self.store.clear_session(1)
        self.store.clear_session(1)
        self.assertIsNone(self.store.session(1))
        self.assertRussianError(lambda: self.store.set_session(1, "", {}))
        self.assertRussianError(lambda: self.store.set_session(1, "x", {"bad": object()}))
        self.assertRussianError(lambda: self.store.set_session(1, "x", float("nan")))

    def test_registration_referral_and_profile(self):
        self.store.register(3, None, "Carol", 1)
        self.assertEqual(self.store.user(3)["referrer_id"], 1)
        self.assertEqual(self.store.user(3)["username"], "")
        self.assertEqual(
            self.store.profile(1), {"referrals": 1, "purchases": 0, "cart_quantity": 0, "cart_additions": 0}
        )
        self.assertIsNone(self.store.user(9999))
        self.assertRussianError(lambda: self.store.profile(9999))

    def test_referrer_is_immutable_and_invalid_referrers_ignored(self):
        self.store.register(3, "c", "C", 1)
        self.store.register(3, "updated", "New", 2)
        self.assertEqual(self.store.user(3)["referrer_id"], 1)
        self.assertEqual(self.store.user(3)["username"], "updated")
        self.store.register(2, "bob", "Bob", 1)
        self.assertIsNone(self.store.user(2)["referrer_id"])
        for uid, referrer in ((4, 4), (5, 9999), (6, "1 OR 1=1"), (7, True), (8, -1)):
            self.store.register(uid, "x", "X", referrer)
            self.assertIsNone(self.store.user(uid)["referrer_id"])

    def test_cart_add_set_remove_total_and_lifetime_count(self):
        self.store.cart_add(1, self.product, 2)
        self.store.cart_add(1, self.product, 3)
        self.assertEqual(self.store.cart(1)[0]["quantity"], 5)
        self.assertEqual(self.store.cart_total(1), 1250)
        self.store.cart_set(1, self.product, 7)
        self.store.cart_set(1, self.product, 1)
        self.assertEqual(self.store.profile(1)["cart_additions"], 7)
        self.assertEqual(self.store.profile(1)["cart_quantity"], 1)
        self.store.cart_set(1, self.product, 0)
        self.assertEqual(self.store.cart(1), [])
        self.assertEqual(self.store.cart_total(1), 0)
        self.assertEqual(self.store.profile(1)["cart_additions"], 7)

    def test_cart_users_are_isolated(self):
        self.store.cart_add(1, self.product, 2)
        self.store.cart_add(2, self.product, 3)
        self.store.cart_clear(1)
        self.assertEqual(self.store.cart(1), [])
        self.assertEqual(self.store.cart(2)[0]["quantity"], 3)
        self.assertRussianError(lambda: self.store.cart_add(9999, self.product, 1))
        self.assertRussianError(lambda: self.store.cart_add("1 OR 1=1", self.product, 1))

    def test_quantity_bounds_and_cumulative_limit_rollback(self):
        for qty in (0, -1, 100, True, 1.5, "1.0", None, 10**100):
            self.assertRussianError(lambda: self.store.cart_add(1, self.product, qty))
            self.assertRussianError(lambda: self.store.checkout(1, self.product, qty))
        for qty in (-1, 100, True, None):
            self.assertRussianError(lambda: self.store.cart_set(1, self.product, qty))
        self.store.cart_add(1, self.product, 99)
        self.assertRussianError(lambda: self.store.cart_add(1, self.product, 1))
        self.assertEqual(self.store.cart(1)[0]["quantity"], 99)
        self.assertEqual(self.store.profile(1)["cart_additions"], 99)

    def test_cart_twenty_line_limit_allows_existing_updates(self):
        products = [self.product]
        for _ in range(20):
            pid = self.store.create_entity("products")
            self.store.update_entity("products", pid, "active", 1)
            products.append(pid)
        for pid in products[:20]:
            self.store.cart_add(1, pid, 1)
        self.assertRussianError(lambda: self.store.cart_add(1, products[20], 1))
        self.assertRussianError(lambda: self.store.cart_set(1, products[20], 1))
        self.store.cart_set(1, products[0], 2)
        self.assertEqual(len(self.store.cart(1)), 20)
        self.store.cart_set(1, products[1], 0)
        self.store.cart_add(1, products[20], 1)
        self.assertEqual(len(self.store.cart(1)), 20)

    def test_inactive_products_and_categories_block_add_and_checkout(self):
        for kind, identity in (("products", self.product), ("categories", self.category)):
            with self.subTest(kind=kind):
                self.store.cart_add(1, self.product, 1)
                self.store.update_entity(kind, identity, "active", 0)
                self.assertRussianError(lambda: self.store.cart_add(1, self.product, 1))
                self.assertRussianError(lambda: self.store.cart_set(1, self.product, 2))
                self.assertRussianError(lambda: self.new_order())
                self.assertRussianError(lambda: self.store.checkout(1))
                self.assertEqual(self.store.cart(1)[0]["quantity"], 1)
                self.store.cart_set(1, self.product, 0)
                self.store.update_entity(kind, identity, "active", 1)
        self.assertRussianError(lambda: self.store.cart_add(1, 99999, 1))

    def test_cart_checkout_snapshots_clears_and_has_exact_order_keys(self):
        self.store.cart_add(1, self.product, 3)
        order = self.store.checkout(1)
        expected = {
            "id",
            "user_id",
            "username",
            "first_name",
            "status",
            "total_cents",
            "items",
            "method",
            "proof_file_id",
            "proof_unique_id",
            "claimed_by",
            "created_at",
            "updated_at",
            "delivery_chat_id",
            "delivery_message_id",
            "delivery_error",
            "review_notified",
            "reject_notified",
            "delivery_notified",
            "rejection_reason",
        }
        self.assertEqual(set(order), expected)
        self.assertEqual(
            order["items"],
            [{"product_id": self.product, "name": "Original product", "quantity": 3, "price_cents": 250}],
        )
        self.assertEqual(order["total_cents"], 750)
        self.assertEqual(order["status"], "awaiting_payment")
        self.assertIsNone(order["method"])
        self.assertEqual(self.store.cart(1), [])
        self.store.update_entity("products", self.product, "price_cents", 999)
        self.store.update_entity("products", self.product, "name", "Changed")
        self.store.register(1, "changed", "Changed")
        saved = self.store.order(order["id"])
        self.assertEqual(saved["items"], order["items"])
        self.assertEqual(saved["username"], "alice")
        self.assertEqual(saved["first_name"], "Alice")
        self.assertEqual(saved["total_cents"], 750)

    def test_direct_checkout_leaves_cart_untouched(self):
        self.store.cart_add(1, self.product, 4)
        order = self.new_order(quantity=2)
        self.assertEqual(order["total_cents"], 500)
        self.assertEqual(self.store.cart(1)[0]["quantity"], 4)
        self.assertRussianError(lambda: self.store.checkout(2))
        self.assertRussianError(lambda: self.store.checkout(9999, self.product))

    def test_unpaid_limit_includes_awaiting_proof_and_preserves_cart(self):
        orders = [self.new_order() for _ in range(5)]
        self.store.choose_method(orders[0]["id"], 1, self.method)
        self.store.request_proof(orders[0]["id"], 1)
        self.store.cart_add(1, self.product, 1)
        self.assertRussianError(lambda: self.new_order())
        self.assertRussianError(lambda: self.store.checkout(1))
        self.assertEqual(len(self.store.orders(1)), 5)
        self.assertEqual(self.store.cart(1)[0]["quantity"], 1)
        self.store.submit_proof(orders[0]["id"], 1, "file", "unique")
        self.store.checkout(1)
        self.assertEqual(len(self.store.orders(1)), 6)
        self.assertEqual(self.store.cart(1), [])
        self.store.cancel_order(orders[1]["id"], 1)
        self.new_order()

    def test_checkout_failure_rolls_back_inserted_order_and_cart(self):
        self.store.cart_add(1, self.product, 2)
        self.store._conn.execute(
            "CREATE TEMP TRIGGER fail_items BEFORE INSERT ON order_items BEGIN SELECT RAISE(ABORT,'test'); END"
        )
        try:
            self.assertRussianError(lambda: self.store.checkout(1))
        finally:
            self.store._conn.execute("DROP TRIGGER fail_items")
        self.assertEqual(self.store.orders(1), [])
        self.assertEqual(self.store.cart(1)[0]["quantity"], 2)
        self.assertEqual(self.store.checkout(1)["total_cents"], 500)

    def test_order_lookup_and_all_customer_mutations_enforce_owner(self):
        order = self.new_order()
        oid = order["id"]
        self.assertIsNone(self.store.order(oid, 2))
        self.assertIsNone(self.store.order(99999))
        self.assertEqual(self.store.order(oid, 1)["id"], oid)
        for callback in (
            lambda: self.store.choose_method(oid, 2, self.method),
            lambda: self.store.request_proof(oid, 2),
            lambda: self.store.submit_proof(oid, 2, "file", "unique"),
            lambda: self.store.cancel_order(oid, 2),
        ):
            self.assertRussianError(callback)
            self.assertEqual(self.store.order(oid), order)
        self.assertRussianError(lambda: self.store.order(oid, "1 OR 1=1"))
        self.assertEqual(self.store.orders(2), [])

    def test_choose_method_rejects_inactive_empty_and_missing_wallet(self):
        oid = self.new_order()["id"]
        self.store.update_entity("methods", self.method, "active", 0)
        self.assertRussianError(lambda: self.store.choose_method(oid, 1, self.method))
        self.store.update_entity("methods", self.method, "active", 1)
        for wallet in ("", " \t\n "):
            self.store.update_entity("methods", self.method, "wallet", wallet)
            self.assertRussianError(lambda: self.store.choose_method(oid, 1, self.method))
        self.assertRussianError(lambda: self.store.choose_method(oid, 1, 9999))
        self.assertIsNone(self.store.order(oid)["method"])

    def test_method_snapshot_is_immutable_and_can_change_before_proof(self):
        oid = self.new_order()["id"]
        self.store.choose_method(oid, 1, self.method)
        self.store.update_entity("methods", self.method, "wallet", "0xNewWallet")
        self.assertEqual(self.store.order(oid)["method"]["wallet"], "0xOriginalWallet")
        self.store.request_proof(oid, 1)
        self.store.choose_method(oid, 1, self.method)
        self.assertEqual(self.store.order(oid)["method"]["wallet"], "0xNewWallet")
        self.store.update_entity("methods", self.method, "active", 0)
        self.store.submit_proof(oid, 1, "file", "unique")
        self.assertEqual(self.store.order(oid)["method"]["active"], 1)
        self.assertRussianError(lambda: self.store.choose_method(oid, 1, self.method))

    def test_proof_state_machine_requires_method_and_valid_identifiers(self):
        oid = self.new_order()["id"]
        self.assertRussianError(lambda: self.store.request_proof(oid, 1))
        self.assertRussianError(lambda: self.store.submit_proof(oid, 1, "file", "unique"))
        self.store.choose_method(oid, 1, self.method)
        self.assertRussianError(lambda: self.store.submit_proof(oid, 1, "file", "unique"))
        self.store.request_proof(oid, 1)
        self.store.request_proof(oid, 1)
        for file_id, unique_id in (("", "unique"), ("file", ""), (" ", "unique"), ("file", None)):
            self.assertRussianError(lambda: self.store.submit_proof(oid, 1, file_id, unique_id))
        result = self.store.submit_proof(oid, 1, "file", "unique")
        self.assertEqual(
            (result["status"], result["proof_file_id"], result["proof_unique_id"]),
            ("review", "file", "unique"),
        )
        self.assertRussianError(lambda: self.store.submit_proof(oid, 1, "other", "other"))
        self.assertRussianError(lambda: self.store.request_proof(oid, 1))

    def test_reused_proof_blocked_across_users_and_after_rejection(self):
        first = self.review_order()
        second = self.new_order(2)
        self.store.choose_method(second["id"], 2, self.method)
        self.store.request_proof(second["id"], 2)
        self.store.review(first["id"], 101, False)
        self.assertRussianError(
            lambda: self.store.submit_proof(second["id"], 2, "different-file-id", first["proof_unique_id"])
        )
        saved = self.store.order(second["id"])
        self.assertEqual(saved["status"], "awaiting_proof")
        self.assertIsNone(saved["proof_file_id"])

    def test_cancel_only_unpaid_and_terminal_orders_stay_terminal(self):
        first = self.new_order()
        self.assertEqual(self.store.cancel_order(first["id"], 1)["status"], "cancelled")
        self.assertRussianError(lambda: self.store.cancel_order(first["id"], 1))
        self.assertRussianError(lambda: self.store.choose_method(first["id"], 1, self.method))
        second = self.new_order()
        self.store.choose_method(second["id"], 1, self.method)
        self.store.request_proof(second["id"], 1)
        self.assertEqual(self.store.cancel_order(second["id"], 1)["status"], "cancelled")
        third = self.review_order()
        self.assertRussianError(lambda: self.store.cancel_order(third["id"], 1))
        self.store.review(third["id"], 101, False)
        self.assertRussianError(lambda: self.store.cancel_order(third["id"], 1))
        self.assertRussianError(lambda: self.store.review(third["id"], 101, True))

    def test_review_compare_and_swap_and_strict_boolean(self):
        order = self.review_order()
        oid = order["id"]
        self.assertRussianError(lambda: self.store.review(oid, 101, "false"))
        self.assertRussianError(lambda: self.store.review(oid, 0, True))
        result = self.store.review(oid, 101, True)
        self.assertEqual((result["status"], result["claimed_by"]), ("approved", 101))
        self.assertRussianError(lambda: self.store.review(oid, 102, False))
        self.assertEqual(self.store.order(oid)["claimed_by"], 101)
        self.assertRussianError(lambda: self.store.review(self.new_order()["id"], 101, True))

    def test_claim_release_and_reclaim_security(self):
        oid = self.approved_order()["id"]
        self.assertRussianError(lambda: self.store.release_claim(oid, 102))
        self.assertRussianError(lambda: self.store.claim(oid, 102))
        self.assertEqual(self.store.claim(oid, 101)["claimed_by"], 101)
        self.assertIsNone(self.store.release_claim(oid, 101)["claimed_by"])
        self.assertRussianError(lambda: self.store.release_claim(oid, 101))
        self.assertEqual(self.store.claim(oid, 102)["claimed_by"], 102)
        self.assertRussianError(lambda: self.store.claim(self.new_order()["id"], 101))

    def test_queue_requires_claim_and_valid_delivery_coordinates(self):
        oid = self.approved_order()["id"]
        for aid, chat, message in ((102, 1, 1), (101, 0, 1), (101, 1, 0), (101, 1, -1), (101, True, 1)):
            self.assertRussianError(lambda: self.store.queue_delivery(oid, aid, chat, message))
        self.store.release_claim(oid, 101)
        self.assertRussianError(lambda: self.store.queue_delivery(oid, 101, 1, 1))
        self.store.claim(oid, 102)
        queued = self.store.queue_delivery(oid, 102, -100444, 12)
        self.assertEqual(
            (queued["status"], queued["delivery_chat_id"], queued["delivery_message_id"]),
            ("delivery_queued", -100444, 12),
        )
        self.assertRussianError(lambda: self.store.queue_delivery(oid, 102, -100444, 12))
        self.assertRussianError(lambda: self.store.release_claim(oid, 102))
        self.assertRussianError(lambda: self.store.claim(oid, 102))

    def test_delivery_start_is_single_use_and_success_terminal(self):
        oid = self.queued_order()["id"]
        self.assertRussianError(lambda: self.store.delivery_result(oid, True))
        self.assertEqual(self.store.start_delivery(oid)["status"], "delivering")
        self.assertIsNone(self.store.start_delivery(oid))
        self.assertIsNone(self.store.start_delivery(99999))
        result = self.store.delivery_result(oid, True, "irrelevant")
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["delivery_error"], "")
        self.assertRussianError(lambda: self.store.delivery_result(oid, True))
        self.assertRussianError(lambda: self.store.resolve_delivery(oid, 101, True))
        self.assertIsNone(self.store.start_delivery(oid))
        self.assertEqual(self.store.profile(1)["purchases"], 1)

    def test_uncertain_delivery_is_not_automatically_retried(self):
        oid = self.uncertain_order()["id"]
        result = self.store.order(oid)
        self.assertEqual(result["status"], "delivery_uncertain")
        self.assertEqual(result["delivery_error"], "ambiguous timeout")
        self.assertEqual(self.store.delivery_orders(), [])
        self.assertIsNone(self.store.start_delivery(oid))
        self.assertRussianError(lambda: self.store.queue_delivery(oid, 101, 1, 1))
        resolved = self.store.resolve_delivery(oid, 102, True)
        self.assertEqual((resolved["status"], resolved["claimed_by"]), ("delivered", 102))

    def test_uncertain_resolution_allows_explicit_requeue_with_new_claim(self):
        oid = self.uncertain_order()["id"]
        result = self.store.resolve_delivery(oid, 102, False)
        self.assertEqual((result["status"], result["claimed_by"]), ("approved", 102))
        self.assertIsNone(result["delivery_chat_id"])
        self.assertIsNone(result["delivery_message_id"])
        self.assertRussianError(lambda: self.store.queue_delivery(oid, 101, 1, 1))
        self.store.queue_delivery(oid, 102, -10099, 88)
        self.store.start_delivery(oid)
        self.assertEqual(self.store.delivery_result(oid, True)["status"], "delivered")

    def test_recovery_only_changes_inflight_deliveries(self):
        inflight = self.delivering_order()
        queued = self.queued_order()
        approved = self.approved_order()
        self.assertEqual(self.store.recover_deliveries(), 1)
        self.assertEqual(self.store.order(inflight["id"])["status"], "delivery_uncertain")
        self.assertTrue(self.store.order(inflight["id"])["delivery_error"])
        self.assertEqual(self.store.order(queued["id"])["status"], "delivery_queued")
        self.assertEqual(self.store.order(approved["id"])["status"], "approved")
        self.assertEqual(self.store.recover_deliveries(), 0)

    def test_notification_state_filters_and_independent_flags(self):
        review = self.review_order()
        rejected = self.review_order()
        self.store.review(rejected["id"], 101, False)
        delivered = self.delivering_order()
        self.store.delivery_result(delivered["id"], True)
        self.new_order()
        self.approved_order()
        self.assertEqual(
            [o["id"] for o in self.store.notification_orders()],
            [review["id"], rejected["id"], delivered["id"]],
        )
        self.store.mark_notified(review["id"], "review")
        self.store.mark_notified(review["id"], "review")
        self.store.mark_notified(rejected["id"], "reject")
        self.store.mark_notified(delivered["id"], "delivery")
        self.assertEqual(self.store.notification_orders(), [])
        self.store.review(review["id"], 101, False)
        self.assertEqual([o["id"] for o in self.store.notification_orders()], [review["id"]])
        self.assertRussianError(lambda: self.store.mark_notified(review["id"], "status"))
        self.assertRussianError(lambda: self.store.mark_notified(review["id"], "review_notified=1 --"))
        self.assertRussianError(lambda: self.store.mark_notified(9999, "review"))

    def test_orders_sorting_pending_statuses_and_owner_filter(self):
        unpaid = self.new_order()
        review = self.review_order()
        approved = self.approved_order(2)
        queued = self.queued_order()
        delivering = self.delivering_order()
        uncertain = self.uncertain_order()
        rejected = self.review_order()
        self.store.review(rejected["id"], 101, False)
        delivered = self.delivering_order()
        self.store.delivery_result(delivered["id"], True)
        expected = [review["id"], approved["id"], queued["id"], delivering["id"], uncertain["id"]]
        self.assertEqual([o["id"] for o in self.store.orders(pending=True)], expected)
        self.assertEqual([o["id"] for o in self.store.orders(2, pending=True)], [approved["id"]])
        ids = [o["id"] for o in self.store.orders()]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertEqual(ids[-1], unpaid["id"])
        self.assertEqual([o["id"] for o in self.store.delivery_orders()], [queued["id"]])

    def test_stats_count_delivered_revenue_only(self):
        self.assertEqual(
            self.store.stats(), {"users": 2, "orders": 0, "pending": 0, "delivered": 0, "revenue_cents": 0}
        )
        self.new_order(quantity=4)
        self.review_order()
        rejected = self.review_order()
        self.store.review(rejected["id"], 101, False)
        delivered = self.delivering_order()
        self.store.delivery_result(delivered["id"], True)
        self.uncertain_order()
        self.assertEqual(
            self.store.stats(), {"users": 2, "orders": 5, "pending": 2, "delivered": 1, "revenue_cents": 250}
        )

    def test_audit_preserves_untrusted_text_as_data(self):
        action = "approve'); DROP TABLE users; --"
        detail = json.dumps({"input": "' OR 1=1 --"})
        self.store.audit(101, action, "orders", 44, detail)
        row = self.store._conn.execute("SELECT * FROM audit_log").fetchone()
        self.assertEqual(
            (row["admin_id"], row["action"], row["entity_type"], row["entity_id"], row["detail"]),
            (101, action, "orders", "44", detail),
        )
        self.assertTrue(row["created_at"])
        self.assertIsNotNone(self.store.user(1))
        self.assertRussianError(lambda: self.store.audit(0, "x", "orders", 1))

    def test_backup_contains_wal_data_and_is_independent(self):
        order = self.review_order()
        self.store.set("marker", "before-backup")
        self.store.set_session(1, "waiting", {"order_id": order["id"]})
        self.store.audit(101, "review", "orders", order["id"])
        target = str(Path(self.tmp.name) / "backup.sqlite3")
        self.store.backup(target)
        copy = Store(target)
        try:
            self.assertEqual(copy.order(order["id"]), order)
            self.assertEqual(copy.get("marker"), "before-backup")
            self.assertEqual(copy.session(1)["data"]["order_id"], order["id"])
            self.assertEqual(copy._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(copy._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.store.set("marker", "after-backup")
            self.assertEqual(copy.get("marker"), "before-backup")
        finally:
            copy.close()
        self.assertRussianError(lambda: self.store.backup(self.path))

    def test_persistence_across_connections(self):
        self.store.cart_add(1, self.product, 2)
        self.store.set_session(1, "editing", {"id": self.product})
        order = self.review_order()
        other = Store(self.path)
        try:
            self.assertEqual(other.order(order["id"]), order)
            self.assertEqual(other.cart(1)[0]["quantity"], 2)
            self.assertEqual(other.session(1)["mode"], "editing")
            self.assertEqual(other.user(1)["username"], "alice")
        finally:
            other.close()

    def test_large_totals_use_integer_cents(self):
        self.store.update_entity("products", self.product, "price_cents", 100000000)
        self.store.cart_add(1, self.product, 99)
        self.assertEqual(self.store.cart_total(1), 9900000000)
        order = self.store.checkout(1)
        self.assertEqual(order["total_cents"], 9900000000)
        self.assertIsInstance(order["total_cents"], int)

    def test_returned_snapshots_cannot_mutate_persisted_state(self):
        order = self.review_order()
        order["items"][0]["price_cents"] = 0
        order["method"]["wallet"] = "attacker"
        self.assertEqual(self.store.order(order["id"])["items"][0]["price_cents"], 250)
        self.assertEqual(self.store.order(order["id"])["method"]["wallet"], "0xOriginalWallet")

    def test_concurrent_checkout_clears_cart_exactly_once(self):
        self.store.cart_add(1, self.product, 2)
        results = self.race([lambda s: s.checkout(1), lambda s: s.checkout(1)])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(len(self.store.orders()), 1)
        self.assertEqual(self.store.cart(1), [])

    def test_concurrent_direct_checkout_respects_unpaid_limit(self):
        for _ in range(4):
            self.new_order()
        results = self.race([lambda s: s.checkout(1, self.product), lambda s: s.checkout(1, self.product)])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(len(self.store.orders()), 5)

    def test_concurrent_review_only_one_admin_wins(self):
        oid = self.review_order()["id"]
        results = self.race([lambda s: s.review(oid, 101, True), lambda s: s.review(oid, 102, False)])
        winners = [result for result in results if isinstance(result, dict)]
        self.assertEqual(len(winners), 1)
        self.assertEqual(self.store.order(oid)["claimed_by"], winners[0]["claimed_by"])
        self.assertEqual(self.store.order(oid)["status"], winners[0]["status"])

    def test_concurrent_claim_only_one_admin_wins(self):
        oid = self.approved_order()["id"]
        self.store.release_claim(oid, 101)
        results = self.race([lambda s: s.claim(oid, 101), lambda s: s.claim(oid, 102)])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)

    def test_concurrent_delivery_start_only_one_worker_wins(self):
        oid = self.queued_order()["id"]
        results = self.race([lambda s: s.start_delivery(oid), lambda s: s.start_delivery(oid)])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(results.count(None), 1)

    def test_concurrent_proof_reuse_is_blocked_atomically(self):
        first, second = self.new_order(), self.new_order(2)
        for order in (first, second):
            self.store.choose_method(order["id"], order["user_id"], self.method)
            self.store.request_proof(order["id"], order["user_id"])
        results = self.race(
            [
                lambda s: s.submit_proof(first["id"], 1, "file-a", "shared-unique"),
                lambda s: s.submit_proof(second["id"], 2, "file-b", "shared-unique"),
            ]
        )
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        statuses = [self.store.order(o["id"])["status"] for o in (first, second)]
        self.assertCountEqual(statuses, ["review", "awaiting_proof"])

    def test_concurrent_cart_additions_do_not_lose_updates(self):
        self.race([lambda s: s.cart_add(1, self.product, 2), lambda s: s.cart_add(1, self.product, 3)])
        self.assertEqual(self.store.cart(1)[0]["quantity"], 5)
        self.assertEqual(self.store.profile(1)["cart_additions"], 5)

    def test_bool_and_injection_ids_rejected_without_mutation(self):
        for identity in (True, False, 0, -1, 1.5, "1; DROP TABLE users", None, 10**100):
            with self.subTest(identity=identity):
                self.assertRussianError(lambda: self.store.register(identity, "x", "X"))
                self.assertRussianError(lambda: self.store.cart(identity))
                if identity is not None:
                    self.assertRussianError(lambda: self.store.orders(identity))
        self.assertEqual(self.store.orders(None), [])
        self.assertEqual(self.store.stats()["users"], 2)

    def test_rejection_reason_is_persisted_with_terminal_state(self):
        o = self.review_order()
        reason = "Неверная сумма & сеть"
        saved = self.store.review(o["id"], 101, False, reason)
        self.assertEqual(saved["rejection_reason"], reason)
        reopened = Store(self.path)
        try:
            self.assertEqual(reopened.order(o["id"])["rejection_reason"], reason)
        finally:
            reopened.close()
        with self.assertRaises(ValueError):
            self.store.review(o["id"], 102, False, "Другая причина")
        self.assertEqual(self.store.order(o["id"])["rejection_reason"], reason)

    def test_rejection_invalid_reason_rolls_back(self):
        o = self.review_order()
        for reason in ("", "  ", "x" * 321, None, "x\u0000y"):
            with self.subTest(reason=repr(reason)), self.assertRaises(ValueError):
                self.store.review(o["id"], 101, False, reason)
        self.assertEqual(self.store.order(o["id"])["status"], "review")

    def test_schema_v1_migrates_without_losing_orders(self):
        o = self.review_order()
        path = str(Path(self.tmp.name) / "legacy.sqlite3")
        self.store.backup(path)
        legacy = sqlite3.connect(path)
        legacy.execute("ALTER TABLE orders DROP COLUMN rejection_reason")
        legacy.execute("PRAGMA user_version=1")
        legacy.commit()
        legacy.close()
        migrated = Store(path)
        try:
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(migrated.order(o["id"])["items"], o["items"])
            self.assertEqual(migrated.order(o["id"])["rejection_reason"], "")
            self.assertEqual(migrated.order(o["id"])["status"], "review")
        finally:
            migrated.close()

    def test_rejection_race_keeps_winning_comment(self):
        o = self.review_order()
        results = self.race(
            [
                lambda s: s.review(o["id"], 101, False, "Причина А"),
                lambda s: s.review(o["id"], 102, False, "Причина Б"),
            ]
        )
        winner = next(r for r in results if isinstance(r, dict))
        self.assertEqual(self.store.order(o["id"])["rejection_reason"], winner["rejection_reason"])
        self.assertEqual(sum(r == "invalid-state" for r in results), 1)


if __name__ == "__main__":
    unittest.main()
