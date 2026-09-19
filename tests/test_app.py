"""Offline Telegram integration tests: real handlers, real DB, mocked network only."""

import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageMedia
from aiogram.types import Chat, Message, PhotoSize, User

from shop.app import ROOT, Shop
from shop.config import Config
from shop.content import LABELS, PAGES, parse_price, validate_support
from shop.store import Store


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.copied = []
        self.next_id = 100
        self.fail_copy = False
        self.fail_notice = set()
        self.fail_edit = False

    def validate(self, caption, markup=None):
        plain = unescape(re.sub(r"<[^>]+>", "", caption))
        assert len(plain.encode("utf-16-le")) // 2 <= 1024, "Caption exceeds Telegram limit"
        if markup:
            for row in markup.inline_keyboard:
                for button in row:
                    assert button.text
                    if button.callback_data:
                        assert len(button.callback_data.encode()) <= 64

    async def send_photo(self, chat_id, photo, **kwargs):
        if chat_id in self.fail_notice:
            raise RuntimeError("simulated unavailable chat")
        self.validate(kwargs.get("caption", ""), kwargs.get("reply_markup"))
        self.next_id += 1
        self.sent.append(dict(chat_id=chat_id, photo=photo, **kwargs))
        return SimpleNamespace(message_id=self.next_id)

    async def edit_message_media(self, **kwargs):
        if self.fail_edit:
            raise TelegramBadRequest(
                method=EditMessageMedia(media=kwargs["media"]), message="message to edit not found"
            )
        self.validate(kwargs["media"].caption, kwargs.get("reply_markup"))
        self.edits.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        self.next_id += 1
        self.sent.append(dict(chat_id=chat_id, text=text, **kwargs))
        return SimpleNamespace(message_id=self.next_id)

    async def send_document(self, chat_id, document, **kwargs):
        self.sent.append(dict(chat_id=chat_id, document=document, **kwargs))

    async def copy_message(self, **kwargs):
        self.copied.append(kwargs)
        if self.fail_copy:
            raise TimeoutError("ambiguous network result")
        return SimpleNamespace(message_id=555)


def message(uid, text=None, photo=False, mid=77, album=False):
    data = dict(
        message_id=mid,
        date=datetime.now(timezone.utc),
        chat=Chat(id=uid, type="private"),
        from_user=User(id=uid, is_bot=False, first_name="Buyer", username="buyer"),
        text=text,
    )
    if photo:
        data["photo"] = [
            PhotoSize(file_id="telegram_photo_id", file_unique_id="unique_photo_id", width=1000, height=600)
        ]
    if album:
        data["media_group_id"] = "album"
    return Message(**data)


class AppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "shop.sqlite3")
        self.s = Store(self.path)
        self.addCleanup(self.s.close)
        self.bot = FakeBot()
        self.config = Config("123:fake", frozenset({101, 102}), self.path, True)
        self.app = Shop(self.bot, self.s, self.config)
        self.app.username = "aurora_test_bot"
        for uid in (1, 2, 101, 102):
            self.s.register(uid, "user" + str(uid), "User")
        self.pid = self.s.list_entities("products")[0]["id"]
        self.cid = self.s.list_entities("categories")[0]["id"]
        self.mid = self.s.list_entities("methods")[0]["id"]
        self.s.update_entity("products", self.pid, "active", 1)
        self.s.update_entity("products", self.pid, "price_cents", 1990)
        self.s.update_entity("methods", self.mid, "wallet", "0xTestWallet")
        self.s.update_entity("methods", self.mid, "active", 1)
        self.s.set("support", "test_support")
        self.s.set("page:terms:text", "Условия тестовой покупки")
        self.s.set("checkout_enabled", "1")

    def last_caption(self):
        if self.bot.edits:
            return self.bot.edits[-1]["media"].caption
        return self.bot.sent[-1].get("caption", "")

    def review_order(self):
        o = self.s.checkout(1, self.pid, 2)
        self.s.choose_method(o["id"], 1, self.mid)
        self.s.request_proof(o["id"], 1)
        return self.s.submit_proof(o["id"], 1, "proof_photo", "proof_unique" + str(o["id"]))

    async def test_single_message_navigation(self):
        await self.app.home(1)
        await self.app.catalog(1)
        await self.app.category(1, self.cid)
        await self.app.product(1, self.pid, 1)
        self.assertEqual(len(self.bot.sent), 1)
        self.assertEqual(len(self.bot.edits), 3)
        self.assertTrue(all(x["message_id"] == 101 for x in self.bot.edits))

    async def test_deleted_screen_recreated(self):
        await self.app.home(1)
        self.bot.fail_edit = True
        await self.app.catalog(1)
        self.assertEqual(len(self.bot.sent), 2)
        self.assertEqual(self.s.get("screen:1"), "102")

    async def test_catalog_product_cart_profile_navigation(self):
        for action in [
            "home",
            "catalog:0",
            f"cat:{self.cid}:0",
            f"product:{self.pid}:2",
            f"add:{self.pid}:2",
            "cart:0",
            "profile",
            "orders:0",
            "support",
            "terms",
        ]:
            await self.app.callback(1, action)
        self.assertEqual(self.s.cart(1)[0]["quantity"], 2)

    async def test_direct_purchase_is_idempotent_at_confirmation(self):
        await self.app.callback(1, f"buy:{self.pid}:2")
        await self.app.callback(1, "place")
        with self.assertRaises(ValueError):
            await self.app.callback(1, "place")
        self.assertEqual(len(self.s.orders(user_id=1)), 1)
        self.assertEqual(self.s.orders(user_id=1)[0]["total_cents"], 3980)

    async def test_price_changed_requires_new_confirmation(self):
        await self.app.callback(1, f"buy:{self.pid}:2")
        self.s.update_entity("products", self.pid, "price_cents", 2500)
        await self.app.callback(1, "place")
        self.assertEqual(self.s.orders(), [])
        await self.app.callback(1, "place")
        self.assertEqual(self.s.orders()[0]["total_cents"], 5000)

    async def test_gate_prevents_crypto_checkout_by_default(self):
        self.app.config = Config("123:fake", frozenset({101}), self.path, False)
        with self.assertRaises(ValueError):
            await self.app.callback(1, "checkout")
        self.s.set("checkout_enabled", "0")
        with self.assertRaises(ValueError):
            await self.app.admin_callback(101, ["enable"])

    async def test_store_pause_and_missing_method_block_checkout(self):
        self.s.set("checkout_enabled", "0")
        with self.assertRaises(ValueError):
            self.app.checkout_allowed()
        self.s.set("checkout_enabled", "1")
        self.s.update_entity("methods", self.mid, "active", 0)
        with self.assertRaises(ValueError):
            self.app.checkout_allowed()

    async def test_complete_manual_payment_approval_delivery(self):
        await self.app.callback(1, f"buy:{self.pid}:2")
        await self.app.callback(1, "place")
        oid = self.s.orders(user_id=1)[0]["id"]
        await self.app.callback(1, f"method:{oid}:{self.mid}")
        await self.app.callback(1, f"paid:{oid}")
        await self.app.message(message(1, photo=True))
        self.assertEqual(self.s.order(oid)["status"], "review")
        await self.app.background_once()
        admins = [x["chat_id"] for x in self.bot.sent if x.get("photo") == "telegram_photo_id"]
        self.assertEqual(admins, [101, 102])
        await self.app.callback(101, f"a:approve:{oid}")
        self.assertEqual(self.s.session(101)["mode"], "delivery")
        await self.app.message(message(101, "Activation details", mid=88))
        self.assertEqual(self.bot.copied, [])
        await self.app.callback(101, f"a:send:{oid}")
        await self.app.background_once()
        self.assertEqual(self.s.order(oid)["status"], "delivered")
        self.assertEqual(self.bot.copied[0]["message_id"], 88)
        self.assertEqual(self.bot.copied[0]["chat_id"], 1)
        self.assertEqual(self.s.profile(1)["purchases"], 1)

    async def test_rejection_notifies_buyer_with_support(self):
        o = self.review_order()
        await self.app.callback(101, f"a:rejectask:{o['id']}")
        await self.app.message(message(101, "Перевод на указанный кошелёк не поступил."))
        await self.app.callback(101, f"a:rejectcommit:{o['id']}")
        await self.app.background_once()
        notice = [x for x in self.bot.sent if x["chat_id"] == 1][-1]
        self.assertIn("не подтверждена", notice["caption"])
        self.assertEqual(notice["reply_markup"].inline_keyboard[0][0].url, "https://t.me/test_support")
        self.assertEqual(self.bot.copied, [])

    async def test_delivery_timeout_never_autoretries(self):
        o = self.review_order()
        self.s.review(o["id"], 101, True)
        self.s.queue_delivery(o["id"], 101, 101, 88)
        self.bot.fail_copy = True
        await self.app.background_once()
        await self.app.background_once()
        self.assertEqual(self.s.order(o["id"])["status"], "delivery_uncertain")
        self.assertEqual(len(self.bot.copied), 1)
        await self.app.callback(101, f"a:resolve:{o['id']}:0")
        self.assertEqual(self.s.order(o["id"])["status"], "approved")

    async def test_partial_admin_notification_retry_skips_already_sent(self):
        o = self.review_order()
        self.bot.fail_notice.add(102)
        await self.app.background_once()
        self.assertFalse(self.s.order(o["id"])["review_notified"])
        self.bot.fail_notice.clear()
        self.app.notice_attempt.clear()
        await self.app.background_once()
        targets = [x["chat_id"] for x in self.bot.sent if x.get("photo") == "proof_photo"]
        self.assertEqual(targets, [101, 102])
        self.assertTrue(self.s.order(o["id"])["review_notified"])

    async def test_admin_access_denied_at_both_entrypoints(self):
        for action in ["a:home", "a:new:products", "a:enable", "a:backup", "a:stats", "a:queue:0"]:
            with self.assertRaises(ValueError):
                await self.app.callback(1, action)
        with self.assertRaises(ValueError):
            await self.app.admin_callback(1, ["new", "products"])

    async def test_crafted_order_callbacks_enforce_ownership(self):
        o = self.s.checkout(1, self.pid)
        for action in [
            f"order:{o['id']}:0",
            f"items:{o['id']}:0",
            f"paid:{o['id']}",
            f"method:{o['id']}:{self.mid}",
            f"cancelok:{o['id']}",
        ]:
            with self.assertRaises(ValueError):
                await self.app.callback(2, action)
        self.assertEqual(self.s.order(o["id"])["status"], "awaiting_payment")

    async def test_admin_all_editor_sections(self):
        actions = [
            "a:home",
            "a:list:products:0",
            f"a:item:products:{self.pid}",
            "a:list:categories:0",
            "a:list:methods:0",
            "a:pages:0",
            "a:page:home",
            "a:labels:0",
            "a:settings",
            "a:stats",
            "a:queue:0",
            "a:all:0",
        ]
        for action in actions:
            await self.app.callback(101, action)
        self.assertGreater(len(self.bot.edits), 8)

    async def test_entity_text_price_photo_edit(self):
        await self.app.callback(101, f"a:edit:products:{self.pid}:name")
        await self.app.message(message(101, "Premium Plan"))
        self.assertEqual(self.s.entity("products", self.pid)["name"], "Premium Plan")
        await self.app.callback(101, f"a:edit:products:{self.pid}:price_cents")
        await self.app.message(message(101, "25,50"))
        self.assertEqual(self.s.entity("products", self.pid)["price_cents"], 2550)
        await self.app.callback(101, f"a:edit:products:{self.pid}:photo")
        await self.app.message(message(101, photo=True))
        self.assertEqual(self.s.entity("products", self.pid)["photo"], "telegram_photo_id")

    async def test_labels_pages_and_support_edit(self):
        await self.app.callback(101, "a:label:catalog")
        await self.app.message(message(101, "Все подписки"))
        self.assertEqual(self.app.label("catalog"), "Все подписки")
        await self.app.callback(101, "a:pageedit:home:text")
        await self.app.message(message(101, "Новый главный экран"))
        await self.app.callback(101, "a:setting:support")
        await self.app.message(message(101, "@owner_support"))
        self.assertEqual(self.s.get("support"), "owner_support")

    async def test_photo_reset_and_reject_text_as_file_path(self):
        await self.app.callback(101, "a:pageedit:home:photo")
        with self.assertRaises(ValueError):
            await self.app.message(message(101, "/etc/passwd"))
        await self.app.message(message(101, photo=True))
        await self.app.callback(101, "a:pageedit:home:photo")
        await self.app.callback(101, "a:resetphoto")
        self.assertEqual(self.s.get("page:home:photo"), "")

    async def test_proof_navigation_clears_input_session(self):
        o = self.s.checkout(1, self.pid)
        self.s.choose_method(o["id"], 1, self.mid)
        await self.app.callback(1, f"paid:{o['id']}")
        await self.app.callback(1, "profile")
        self.assertIsNone(self.s.session(1))
        await self.app.message(message(1, photo=True))
        self.assertEqual(self.s.order(o["id"])["status"], "awaiting_proof")

    async def test_proof_rejects_text_and_album(self):
        o = self.s.checkout(1, self.pid)
        self.s.choose_method(o["id"], 1, self.mid)
        await self.app.callback(1, f"paid:{o['id']}")
        with self.assertRaises(ValueError):
            await self.app.message(message(1, "I paid"))
        with self.assertRaises(ValueError):
            await self.app.message(message(1, photo=True, album=True))
        self.assertEqual(self.s.order(o["id"])["status"], "awaiting_proof")

    async def test_restart_preserves_proof_input_session(self):
        o = self.s.checkout(1, self.pid)
        self.s.choose_method(o["id"], 1, self.mid)
        await self.app.callback(1, f"paid:{o['id']}")
        app2 = Shop(self.bot, self.s, self.config)
        await app2.message(message(1, photo=True))
        self.assertEqual(self.s.order(o["id"])["status"], "review")

    async def test_queue_next_skips_other_admin_claim(self):
        first = self.review_order()
        self.s.review(first["id"], 102, True)
        second = self.review_order()
        await self.app.process_next(101)
        self.assertIn(f"#{second['id']}", self.last_caption())

    async def test_find_order_and_later_queue(self):
        o = self.review_order()
        await self.app.callback(101, "a:find")
        await self.app.message(message(101, "#" + str(o["id"])))
        await self.app.callback(101, "a:queue:0")
        self.assertIsNone(self.s.session(101))

    async def test_caption_and_button_limits_all_default_pages(self):
        for key in PAGES:
            await self.app.screen(1, key)
        for key in LABELS:
            self.assertLessEqual(len(self.app.button(key, "noop").text), 60)

    async def test_maximum_cart_and_admin_order_pagination(self):
        for i in range(20):
            pid = self.s.create_entity("products")
            self.s.update_entity("products", pid, "name", "Subscription " + str(i) + "x" * 40)
            self.s.update_entity("products", pid, "active", 1)
            self.s.cart_add(1, pid, 99)
        for page in range(4):
            await self.app.cart(1, page)
        o = self.s.checkout(1)
        for page in range(4):
            await self.app.admin_order(101, o["id"], page)
            await self.app.callback(1, f"items:{o['id']}:{page}")

    async def test_html_escaped_in_admin_owned_content(self):
        self.s.update_entity("products", self.pid, "name", "<b>unsafe & text</b>")
        await self.app.product(1, self.pid, 1)
        self.assertIn("&lt;b&gt;", self.last_caption())

    async def test_referral_is_registered_once_from_start(self):
        await self.app.message(message(9, "/start ref_1"))
        await self.app.message(message(9, "/start ref_2"))
        self.assertEqual(self.s.user(9)["referrer_id"], 1)

    async def test_backup_admin_only_and_sqlite_valid(self):
        await self.app.callback(101, "a:backup")
        self.assertTrue((Path(self.path).parent / "shop-backup.sqlite3").exists())
        self.assertTrue(self.bot.sent[-1]["protect_content"])

    async def test_failed_edit_preserves_session_for_retry(self):
        await self.app.callback(101, f"a:edit:products:{self.pid}:price_cents")
        with self.assertRaises(ValueError):
            await self.app.message(message(101, "nan"))
        self.assertEqual(self.s.session(101)["mode"], "edit")
        await self.app.message(message(101, "20.01"))
        self.assertEqual(self.s.entity("products", self.pid)["price_cents"], 2001)

    async def test_stale_delivery_confirmation_cannot_send_twice(self):
        o = self.review_order()
        await self.app.callback(101, f"a:approve:{o['id']}")
        await self.app.message(message(101, "secret", mid=90))
        await self.app.callback(101, f"a:send:{o['id']}")
        with self.assertRaises(ValueError):
            await self.app.callback(101, f"a:send:{o['id']}")
        await self.app.background_once()
        await self.app.background_once()
        self.assertEqual(len(self.bot.copied), 1)

    async def test_equal_price_replacement_requires_new_confirmation(self):
        self.s.cart_add(1, self.pid, 1)
        await self.app.callback(1, "checkout")
        pid2 = self.s.create_entity("products")
        self.s.update_entity("products", pid2, "price_cents", 1990)
        self.s.update_entity("products", pid2, "active", 1)
        self.s.cart_clear(1)
        self.s.cart_add(1, pid2, 1)
        await self.app.callback(1, "place")
        self.assertEqual(self.s.orders(), [])
        await self.app.callback(1, "place")
        self.assertEqual(self.s.orders()[0]["items"][0]["product_id"], pid2)

    async def test_changed_terms_require_new_consent(self):
        await self.app.callback(1, f"buy:{self.pid}:1")
        self.s.set("page:terms:text", "Изменённые условия")
        await self.app.callback(1, "place")
        self.assertEqual(self.s.orders(), [])
        await self.app.callback(1, "place")
        oid = self.s.orders()[0]["id"]
        self.assertEqual(self.s.get(f"order_terms:{oid}"), "Изменённые условия")

    async def test_admin_navigation_clears_old_edit_session(self):
        await self.app.callback(101, f"a:edit:products:{self.pid}:name")
        await self.app.callback(101, "a:settings")
        self.assertIsNone(self.s.session(101))

    async def test_empty_checkout_does_not_leave_invalid_session(self):
        with self.assertRaises(ValueError):
            await self.app.callback(1, "checkout")
        self.assertIsNone(self.s.session(1))

    async def test_cart_removal_confirmation_is_required(self):
        self.s.cart_add(1, self.pid, 2)
        await self.app.callback(1, "cartclear")
        self.assertEqual(len(self.s.cart(1)), 1)
        await self.app.callback(1, "cartclearok")
        self.assertEqual(self.s.cart(1), [])

    async def test_unpaid_cancel_renders_final_status(self):
        o = self.s.checkout(1, self.pid)
        await self.app.callback(1, f"cancelask:{o['id']}")
        self.assertEqual(self.s.order(o["id"])["status"], "awaiting_payment")
        await self.app.callback(1, f"cancelok:{o['id']}")
        self.assertEqual(self.s.order(o["id"])["status"], "cancelled")

    async def test_full_length_product_with_escaped_text_fits(self):
        self.s.update_entity("products", self.pid, "description", "&" * 550)
        await self.app.product(1, self.pid, 99)
        self.assertIn("&amp;", self.last_caption())

    async def test_long_payment_wallet_and_description_fit(self):
        self.s.update_entity("methods", self.mid, "wallet", "w" * 250)
        self.s.update_entity("methods", self.mid, "description", "d" * 220)
        o = self.s.checkout(1, self.pid)
        self.s.choose_method(o["id"], 1, self.mid)
        await self.app.order(1, o["id"])
        self.assertIn("w" * 250, self.last_caption())

    async def test_method_description_limit_is_enforced(self):
        await self.app.callback(101, f"a:edit:methods:{self.mid}:description")
        with self.assertRaises(ValueError):
            await self.app.message(message(101, "d" * 221))
        self.assertEqual(self.s.session(101)["mode"], "edit")

    async def test_page_length_limit_preserves_previous_copy(self):
        before = self.app.page("home")
        await self.app.callback(101, "a:pageedit:home:text")
        with self.assertRaises(ValueError):
            await self.app.message(message(101, "x" * 321))
        self.assertEqual(self.app.page("home"), before)

    async def test_checkout_requires_support(self):
        self.s.set("support", "")
        with self.assertRaises(ValueError):
            self.app.checkout_allowed()

    async def test_later_preserves_order_for_original_admin(self):
        o = self.review_order()
        await self.app.callback(101, f"a:approve:{o['id']}")
        await self.app.callback(101, "a:queue:0")
        self.assertEqual(self.s.order(o["id"])["status"], "approved")
        self.assertIsNone(self.s.session(101))
        await self.app.callback(101, f"a:deliver:{o['id']}")
        self.assertEqual(self.s.session(101)["data"]["id"], o["id"])

    async def test_other_admin_cannot_send_claimed_order(self):
        o = self.review_order()
        self.s.review(o["id"], 101, True)
        with self.assertRaises(ValueError):
            await self.app.callback(102, f"a:deliver:{o['id']}")
        self.assertIsNone(self.s.session(102))

    async def test_release_allows_second_admin_to_fulfill(self):
        o = self.review_order()
        self.s.review(o["id"], 101, True)
        await self.app.callback(101, f"a:release:{o['id']}")
        await self.app.callback(102, f"a:deliver:{o['id']}")
        self.assertEqual(self.s.order(o["id"])["claimed_by"], 102)

    async def test_admin_review_action_is_audited(self):
        o = self.review_order()
        await self.app.callback(101, f"a:rejectask:{o['id']}")
        await self.app.message(message(101, "Перевод на указанный кошелёк не поступил."))
        await self.app.callback(101, f"a:rejectcommit:{o['id']}")
        row = self.s.db.execute("SELECT action,admin_id FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(tuple(row), ("reject", 101))

    async def test_shop_name_is_visible_on_home(self):
        self.s.set("shop_name", "My Subscription Shop")
        await self.app.home(1)
        self.assertIn("My Subscription Shop", self.last_caption())

    async def test_empty_wallet_method_cannot_be_enabled_in_admin(self):
        mid = self.s.create_entity("methods")
        with self.assertRaises(ValueError):
            await self.app.callback(101, f"a:toggle:methods:{mid}")
        self.assertEqual(self.s.entity("methods", mid)["active"], 0)

    async def test_whitespace_name_cannot_be_saved_from_admin(self):
        await self.app.callback(101, f"a:edit:products:{self.pid}:name")
        with self.assertRaises(ValueError):
            await self.app.message(message(101, "   "))
        self.assertTrue(self.s.entity("products", self.pid)["name"].strip())

    async def test_buyer_completion_notice_retries_independently(self):
        o = self.review_order()
        self.s.review(o["id"], 101, True)
        self.s.queue_delivery(o["id"], 101, 101, 88)
        self.bot.fail_notice.add(1)
        await self.app.background_once()
        self.assertEqual(self.s.order(o["id"])["status"], "delivered")
        self.assertFalse(self.s.order(o["id"])["delivery_notified"])
        admin_count = len([x for x in self.bot.sent if x["chat_id"] == 101])
        self.bot.fail_notice.clear()
        self.app.notice_attempt.clear()
        await self.app.background_once()
        self.assertTrue(self.s.order(o["id"])["delivery_notified"])
        self.assertEqual(len(self.bot.copied), 1)
        self.assertEqual(len([x for x in self.bot.sent if x["chat_id"] == 101]), admin_count)

    async def prepare_reject(self, reason="Не поступил перевод на указанный адрес."):
        o = self.review_order()
        await self.app.callback(101, f"a:rejectask:{o['id']}")
        await self.app.message(message(101, reason))
        return o

    async def test_rejection_waits_for_reason_and_confirmation(self):
        o = self.review_order()
        await self.app.callback(101, f"a:rejectask:{o['id']}")
        self.assertEqual(self.s.session(101)["mode"], "reject_reason")
        self.assertEqual(self.s.order(o["id"])["status"], "review")
        await self.app.message(message(101, "Нет поступления"))
        self.assertEqual(self.s.order(o["id"])["status"], "review")
        self.assertEqual(self.s.session(101)["mode"], "reject_confirm")
        await self.app.callback(101, f"a:rejectcommit:{o['id']}")
        self.assertEqual(self.s.order(o["id"])["rejection_reason"], "Нет поступления")
        self.assertIsNone(self.s.session(101))

    async def test_rejection_reason_visible_in_notification_and_histories(self):
        reason = "Не совпадает сеть <BEP20> & адрес получателя."
        o = await self.prepare_reject(reason)
        await self.app.callback(101, f"a:rejectcommit:{o['id']}")
        await self.app.background_once()
        notice = [x for x in self.bot.sent if x["chat_id"] == 1][-1]
        self.assertIn("&lt;BEP20&gt; &amp;", notice["caption"])
        await self.app.order(1, o["id"])
        buyer_screen = [x for x in self.bot.sent if x["chat_id"] == 1][-1]
        self.assertIn("Причина отказа", buyer_screen["caption"])
        await self.app.admin_order(101, o["id"])
        self.assertIn("&lt;BEP20&gt;", self.last_caption())

    async def test_legacy_reject_button_cannot_skip_comment(self):
        o = self.review_order()
        await self.app.callback(101, f"a:reject:{o['id']}")
        self.assertEqual(self.s.order(o["id"])["status"], "review")
        with self.assertRaises(ValueError):
            await self.app.callback(101, f"a:rejectcommit:{o['id']}")

    async def test_reject_input_requires_text_and_bounded_length(self):
        o = self.review_order()
        await self.app.callback(101, f"a:rejectask:{o['id']}")
        for msg in (message(101, " "), message(101, "x" * 321), message(101, photo=True)):
            with self.assertRaises(ValueError):
                await self.app.message(msg)
        self.assertEqual(self.s.session(101)["mode"], "reject_reason")
        self.assertEqual(self.s.order(o["id"])["status"], "review")

    async def test_reject_draft_survives_new_application_instance(self):
        o = await self.prepare_reject()
        app2 = Shop(self.bot, self.s, self.config)
        await app2.callback(101, f"a:rejectcommit:{o['id']}")
        self.assertEqual(self.s.order(o["id"])["status"], "rejected")

    async def test_reject_draft_cannot_overwrite_other_admin_approval(self):
        o = await self.prepare_reject()
        self.s.review(o["id"], 102, True)
        with self.assertRaises(ValueError):
            await self.app.callback(101, f"a:rejectcommit:{o['id']}")
        self.assertEqual(self.s.order(o["id"])["status"], "approved")
        self.assertEqual(self.s.order(o["id"])["rejection_reason"], "")

    async def test_reject_comment_can_be_replaced_before_confirmation(self):
        o = await self.prepare_reject("Старый черновик")
        await self.app.message(message(101, "Окончательная причина"))
        await self.app.callback(101, f"a:rejectcommit:{o['id']}")
        self.assertEqual(self.s.order(o["id"])["rejection_reason"], "Окончательная причина")
        with self.assertRaises(ValueError):
            await self.app.callback(101, f"a:rejectcommit:{o['id']}")

    async def test_reject_cancel_leaves_order_pending(self):
        o = await self.prepare_reject()
        await self.app.callback(101, "a:queue:0")
        self.assertIsNone(self.s.session(101))
        self.assertEqual(self.s.order(o["id"])["status"], "review")
        with self.assertRaises(ValueError):
            await self.app.callback(101, f"a:rejectcommit:{o['id']}")

    async def test_reject_confirm_wrong_order_or_nonadmin_is_denied(self):
        o = await self.prepare_reject()
        other = self.review_order()
        for uid, oid in ((101, other["id"]), (1, o["id"]), (102, o["id"])):
            with self.assertRaises(ValueError):
                await self.app.callback(uid, f"a:rejectcommit:{oid}")
        self.assertEqual(self.s.order(o["id"])["status"], "review")

    async def test_rejection_reason_notification_retry_preserves_comment(self):
        o = await self.prepare_reject("Недостаточная сумма перевода.")
        await self.app.callback(101, f"a:rejectcommit:{o['id']}")
        self.bot.fail_notice.add(1)
        await self.app.background_once()
        self.assertFalse(self.s.order(o["id"])["reject_notified"])
        self.bot.fail_notice.clear()
        self.app.notice_attempt.clear()
        await self.app.background_once()
        self.assertTrue(self.s.order(o["id"])["reject_notified"])
        self.assertIn("Недостаточная сумма", self.bot.sent[-1]["caption"])

    async def test_maximum_rejection_and_page_text_fit_caption(self):
        o = await self.prepare_reject("&" * 320)
        await self.app.callback(101, f"a:rejectcommit:{o['id']}")
        self.s.set("page:rejected:text", "x" * 320)
        await self.app.order(1, o["id"])
        await self.app.background_once()
        self.assertTrue(self.s.order(o["id"])["reject_notified"])


class UtilityTests(unittest.TestCase):
    def test_railway_data_must_be_on_volume(self):
        env = {
            "BOT_TOKEN": "1:x",
            "ADMIN_IDS": "101",
            "RAILWAY_ENVIRONMENT_ID": "test",
            "RAILWAY_VOLUME_MOUNT_PATH": "/data",
            "DATA_DIR": "/elsewhere",
        }
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()
        env["DATA_DIR"] = "/data/shop"
        with patch.dict("os.environ", env, clear=True):
            self.assertIn("shop", Config.from_env().db_path)

    def test_invalid_health_port_rejected(self):
        for port in ("0", "65536", "not-a-port"):
            with patch.dict("os.environ", {"BOT_TOKEN": "1:x", "ADMIN_IDS": "101", "PORT": port}, clear=True):
                with self.assertRaises(ValueError):
                    Config.from_env()

    def test_money_parsing_exact(self):
        self.assertEqual(parse_price("19,99"), 1999)
        self.assertEqual(parse_price("0.01"), 1)
        self.assertEqual(parse_price("1000000"), 100000000)
        for bad in ("nan", "Infinity", "0", "-1", "1.001", "1000001", "hello"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_price(bad)

    def test_support_validation(self):
        self.assertEqual(validate_support("@hello_user"), "hello_user")
        for bad in ("x", "https://t.me/example", "<admin>", "name space"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_support(bad)

    def test_config_requires_credentials(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()
        with patch.dict("os.environ", {"BOT_TOKEN": "1:x", "ADMIN_IDS": "101,102"}, clear=True):
            config = Config.from_env()
            self.assertFalse(config.allow_manual_checkout)
            self.assertEqual(config.admins, frozenset({101, 102}))

    def test_railway_requires_volume(self):
        env = {"BOT_TOKEN": "1:x", "ADMIN_IDS": "101", "RAILWAY_ENVIRONMENT": "production"}
        with patch.dict("os.environ", env, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()

    def test_banner_exists_and_is_jpeg(self):
        path = ROOT / "assets" / "cover.jpg"
        self.assertTrue(path.exists())
        self.assertEqual(path.read_bytes()[:2], b"\xff\xd8")


if __name__ == "__main__":
    unittest.main()
