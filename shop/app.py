"""Telegram storefront and private-chat admin UI."""

import asyncio
import logging
import time
from pathlib import Path

from aiogram import Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

from .content import LABELS, PAGES, caption_units, esc, money, parse_price, short, validate_support
from .store import FIELDS

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
PAGE_SIZE = 6


class Shop:
    def __init__(self, bot, store, config):
        self.bot, self.s, self.config = bot, store, config
        self.username = ""
        # Stripe locks bound memory and serialize each user's callbacks/messages.
        self.locks = [asyncio.Lock() for _ in range(128)]
        self.background_lock = asyncio.Lock()
        self.throttle = {}
        self.notice_attempt = {}
        self.dp = Dispatcher()
        self.dp.callback_query.register(self.on_callback)
        self.dp.message.register(self.on_message, F.chat.type == "private")

    def label(self, key):
        return self.s.get("label:" + key, LABELS.get(key, key))

    def button(self, key, data, literal=False):
        text = key if literal else self.label(key)
        if len(data.encode("utf-8")) > 64:
            raise ValueError("Callback too long")
        return InlineKeyboardButton(text=short(text, 60), callback_data=data)

    def rows(self, *pairs):
        return [[self.button(k, d)] for k, d in pairs]

    def home_row(self):
        return [self.button("home", "home")]

    def admin_row(self):
        return [self.button("admin", "a:home")]

    def support_row(self):
        username = self.s.get("support")
        if username:
            return [InlineKeyboardButton(text=self.label("support"), url="https://t.me/" + username)]
        return [self.button("support", "support")]

    def page(self, key):
        title, text = PAGES.get(key, PAGES["home"])
        return self.s.get("page:" + key + ":text", title + "\n\n" + text)

    def photo(self, key, override=""):
        # Only Telegram file IDs set by uploaded photos; never arbitrary local paths/URLs.
        return (
            override
            or self.s.get("page:" + key + ":photo")
            or self.s.get("page:home:photo")
            or str(ROOT / "assets" / "cover.jpg")
        )

    def upload(self, photo):
        return FSInputFile(photo) if photo == str(ROOT / "assets" / "cover.jpg") else photo

    async def screen(self, uid, page, extra="", rows=None, photo="", text=None):
        caption = esc(self.page(page) if text is None else text)
        if extra:
            caption += "\n\n" + extra
        # Keep within 1024 UTF-16 caption units even with user-entered astral characters.
        if caption_units(caption) > 1000:
            raise ValueError("Слишком длинный экран. Сократите текст или описание в настройках.")
        markup = InlineKeyboardMarkup(inline_keyboard=rows or [self.home_row()])
        source = self.photo(page, photo)
        media = InputMediaPhoto(media=self.upload(source), caption=caption, parse_mode="HTML")
        old = self.s.get(f"screen:{uid}")
        if old:
            try:
                await self.bot.edit_message_media(
                    chat_id=uid, message_id=int(old), media=media, reply_markup=markup
                )
                return
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    return
                # Deleted/obsolete messages can be replaced. Strip old keyboard if possible.
                try:
                    await self.bot.edit_message_reply_markup(
                        chat_id=uid, message_id=int(old), reply_markup=None
                    )
                except TelegramBadRequest:
                    pass
        try:
            sent = await self.bot.send_photo(
                uid, self.upload(source), caption=caption, parse_mode="HTML", reply_markup=markup
            )
        except TelegramBadRequest:
            if source == str(ROOT / "assets" / "cover.jpg"):
                raise
            sent = await self.bot.send_photo(
                uid,
                FSInputFile(ROOT / "assets" / "cover.jpg"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
        self.s.set(f"screen:{uid}", sent.message_id)

    async def notice(self, uid, page, extra="", rows=None):
        caption = esc(self.page(page)) + ("\n\n" + extra if extra else "")
        if caption_units(caption) > 1000:
            caption = esc(PAGES[page][0]) + "\n\n" + extra
        source = self.photo(page)
        try:
            return await self.bot.send_photo(
                uid,
                self.upload(source),
                caption=caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows or [self.home_row()]),
            )
        except TelegramBadRequest:
            return await self.bot.send_photo(
                uid,
                FSInputFile(ROOT / "assets" / "cover.jpg"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=rows or [self.home_row()]),
            )

    def pagination(self, total, page, prefix):
        result = []
        if page > 0:
            result.append(self.button("prev", f"{prefix}:{page - 1}"))
        if (page + 1) * PAGE_SIZE < total:
            result.append(self.button("next", f"{prefix}:{page + 1}"))
        return [result] if result else []

    def paged(self, items, page):
        page = max(0, min(page, max(0, (len(items) - 1) // PAGE_SIZE)))
        return items[page * PAGE_SIZE : (page + 1) * PAGE_SIZE], page

    async def home(self, uid):
        rows = [
            [self.button("catalog", "catalog:0")],
            [self.button("profile", "profile"), self.button("cart", "cart:0")],
            [self.button("orders", "orders:0"), self.button("support", "support")],
            [self.button("terms", "terms")],
        ]
        if uid in self.config.admins:
            rows.append(self.admin_row())
        await self.screen(
            uid, "home", rows=rows, text=self.s.get("shop_name", "AURORA") + "\n\n" + self.page("home")
        )

    async def catalog(self, uid, page=0):
        items = self.s.list_entities("categories", active_only=True)
        subset, page = self.paged(items, page)
        rows = [[self.button(c["name"], f"cat:{c['id']}:0", True)] for c in subset]
        rows += self.pagination(len(items), page, "catalog") + [self.home_row()]
        await self.screen(uid, "catalog", "" if items else "Каталог пока пуст.", rows)

    async def category(self, uid, cid, page=0):
        cat = self.s.entity("categories", cid)
        if not cat or not cat["active"]:
            raise ValueError("Категория недоступна")
        products = self.s.list_entities("products", active_only=True, category_id=cid)
        subset, page = self.paged(products, page)
        rows = [
            [
                self.button(
                    f"{short(p['name'], 36)} · {money(p['price_cents'])} USD", f"product:{p['id']}:1", True
                )
            ]
            for p in subset
        ]
        rows += self.pagination(len(products), page, f"cat:{cid}")
        rows += self.rows(("back", "catalog:0")) + [self.home_row()]
        await self.screen(
            uid,
            "catalog",
            "" if products else "Нет доступных подписок.",
            rows,
            cat["photo"],
            cat["name"] + "\n\n" + cat["description"],
        )

    async def product(self, uid, pid, qty):
        p = self.s._product(pid)
        qty = max(1, min(99, qty))
        rows = [
            [
                self.button("minus", f"product:{pid}:{max(1, qty - 1)}"),
                self.button(str(qty), "noop", True),
                self.button("plus", f"product:{pid}:{min(99, qty + 1)}"),
            ],
            [self.button("buy", f"buy:{pid}:{qty}"), self.button("add", f"add:{pid}:{qty}")],
            [self.button("cart", "cart:0"), self.button("back", f"cat:{p['category_id']}:0")],
            self.home_row(),
        ]
        await self.screen(
            uid,
            "catalog",
            f"<b>{money(p['price_cents'])} USD</b> / шт.\nИтого: <b>{money(p['price_cents'] * qty)} USD</b>",
            rows,
            p["photo"],
            p["name"] + "\n\n" + p["description"],
        )

    async def cart(self, uid, page=0):
        items = self.s.cart(uid)
        subset, page = self.paged(items, page)
        rows = []
        lines = []
        for p in subset:
            lines.append(
                f"{esc(short(p['name'], 24))} × {p['quantity']} — {money(p['price_cents'] * p['quantity'])}"
            )
            rows.append(
                [
                    self.button("minus", f"cartset:{p['id']}:{p['quantity'] - 1}:{page}"),
                    self.button(
                        f"{short(p['name'], 17)} · {p['quantity']}",
                        f"product:{p['id']}:{p['quantity']}",
                        True,
                    ),
                    self.button("plus", f"cartset:{p['id']}:{min(99, p['quantity'] + 1)}:{page}"),
                    self.button("×", f"cartset:{p['id']}:0:{page}", True),
                ]
            )
        rows += self.pagination(len(items), page, "cart")
        if items:
            rows += self.rows(("checkout", "checkout"), ("clear", "cartclear"))
        rows += self.rows(("catalog", "catalog:0")) + [self.home_row()]
        extra = "\n".join(lines) + (
            f"\n\nВсего: <b>{money(self.s.cart_total(uid))} USD</b>" if items else "Корзина пуста."
        )
        await self.screen(uid, "cart", extra, rows)

    def checkout_allowed(self):
        if not self.config.allow_manual_checkout:
            raise ValueError("Криптооплата ещё не включена владельцем. Обратитесь в поддержку.")
        if self.s.get("checkout_enabled", "0") != "1":
            raise ValueError("Магазин временно не принимает новые заказы")
        if not self.s.get("support"):
            raise ValueError("Владелец ещё не настроил поддержку")
        if not any(m["wallet"] for m in self.s.list_entities("methods", active_only=True)):
            raise ValueError("Нет настроенных способов оплаты")

    def checkout_signature(self, uid, pid=None, qty=1):
        items = [dict(self.s._product(pid), quantity=qty)] if pid else self.s.cart(uid)
        return [[p["id"], p["name"], p["description"], p["price_cents"], p["quantity"]] for p in items]

    async def checkout_confirm(self, uid, pid=None, qty=1):
        self.checkout_allowed()
        total = self.s._product(pid)["price_cents"] * qty if pid else self.s.cart_total(uid)
        if total <= 0:
            raise ValueError("Корзина пуста")
        # Store amount to detect admin price changes between confirmation and commit.
        self.s.set_session(
            uid,
            "checkout",
            {
                "pid": pid,
                "qty": qty,
                "total": total,
                "signature": self.checkout_signature(uid, pid, qty),
                "terms": self.page("terms"),
            },
        )
        await self.screen(
            uid,
            "terms",
            f"Сумма: <b>{money(total)} USD</b>\nНажимая «{esc(self.label('confirm'))}», вы принимаете условия покупки.",
            self.rows(("confirm", "place"), ("back", "cart:0")) + [self.home_row()],
        )

    async def order(self, uid, oid, method_page=0, item_page=0):
        o = self.s.order(oid, uid)
        if not o:
            raise ValueError("Заказ не найден")
        status = o["status"]
        extra = f"<b>Заказ #{oid}</b> · {esc(self.label('state_' + status))}\nИтого: <b>{money(o['total_cents'])} USD</b>"
        if status == "rejected":
            extra += self.rejection_extra(o)
        rows = []
        photo = ""
        page = {
            "review": "review",
            "approved": "approved",
            "delivery_queued": "approved",
            "delivering": "approved",
            "delivery_uncertain": "approved",
            "delivered": "delivered",
            "rejected": "rejected",
            "cancelled": "cancelled",
        }.get(status, "payment")
        if status in ("awaiting_payment", "awaiting_proof"):
            if o["method"]:
                m = o["method"]
                photo = m["photo"]
                extra += f"\n\nОтправьте ровно <b>{money(o['total_cents'])} {esc(m['asset'])}</b>\nСеть: <b>{esc(m['network'])}</b>\n<code>{esc(m['wallet'])}</code>\nКомиссия сети — сверх этой суммы."
                rows += self.rows(("paid", f"paid:{oid}"))
            methods = self.s.list_entities("methods", active_only=True)
            subset, method_page = self.paged(methods, method_page)
            rows += [[self.button(m["name"], f"method:{oid}:{m['id']}", True)] for m in subset]
            rows += self.pagination(len(methods), method_page, f"order:{oid}")
            rows += self.rows(("cancel_order", f"cancelask:{oid}"))
        rows += self.rows(("order_items", f"items:{oid}:0"), ("orders", "orders:0")) + [
            self.support_row(),
            self.home_row(),
        ]
        # Payment description is editable per method; keep its caption budget conservative.
        text = None
        if page == "payment" and o["method"]:
            text = o["method"]["name"] + "\n\n" + o["method"]["description"][:220]
        await self.screen(uid, page, extra, rows, photo, text)

    async def user_orders(self, uid, page=0):
        orders = self.s.orders(user_id=uid)
        subset, page = self.paged(orders, page)
        rows = [
            [
                self.button(
                    f"#{o['id']} · {money(o['total_cents'])} · {self.label('state_' + o['status'])}",
                    f"order:{o['id']}:0",
                    True,
                )
            ]
            for o in subset
        ]
        rows += self.pagination(len(orders), page, "orders") + [self.home_row()]
        await self.screen(uid, "orders", "" if orders else "Покупок пока нет.", rows)

    async def profile(self, uid):
        p = self.s.profile(uid)
        ref = f"https://t.me/{self.username}?start=ref_{uid}"
        extra = f"ID: <code>{uid}</code>\nПриглашено: <b>{p['referrals']}</b>\nВыполненных покупок: <b>{p['purchases']}</b>\nСейчас в корзине: <b>{p['cart_quantity']}</b>\nДобавлено за всё время: <b>{p['cart_additions']}</b>\n\nВаша реферальная ссылка:\n<code>{ref}</code>"
        await self.screen(
            uid, "profile", extra, self.rows(("orders", "orders:0"), ("cart", "cart:0")) + [self.home_row()]
        )

    def require_admin(self, uid):
        if uid not in self.config.admins:
            raise ValueError("Доступ запрещён")

    async def admin_home(self, uid):
        self.require_admin(uid)
        rows = self.rows(("queue", "a:queue:0"), ("all_orders", "a:all:0"), ("find_order", "a:find"))
        rows += [
            [self.button("categories", "a:list:categories:0"), self.button("products", "a:list:products:0")],
            [self.button("methods", "a:list:methods:0")],
            [self.button("pages", "a:pages:0"), self.button("labels", "a:labels:0")],
            [self.button("settings", "a:settings"), self.button("stats", "a:stats")],
            self.home_row(),
        ]
        n = len(self.s.orders(pending=True))
        await self.screen(uid, "admin", f"Требуют внимания: <b>{n}</b>", rows)

    async def admin_list(self, uid, kind, page):
        items = self.s.list_entities(kind)
        subset, page = self.paged(items, page)
        rows = [
            [
                self.button(
                    f"{'ON' if x['active'] else 'OFF'} · #{x['id']} {x['name']}",
                    f"a:item:{kind}:{x['id']}",
                    True,
                )
            ]
            for x in subset
        ]
        rows += (
            self.pagination(len(items), page, f"a:list:{kind}")
            + self.rows(("new", f"a:new:{kind}"))
            + [self.admin_row()]
        )
        await self.screen(uid, "admin_edit", esc(self.label(kind)), rows)

    async def admin_item(self, uid, kind, eid):
        item = self.s.entity(kind, eid)
        if not item:
            raise ValueError("Запись не найдена")
        rows = []
        for field in sorted(FIELDS[kind]):
            if field != "active":
                rows.append([self.button("field_" + field, f"a:edit:{kind}:{eid}:{field}")])
        rows += self.rows(("active", f"a:toggle:{kind}:{eid}"), ("back", f"a:list:{kind}:0")) + [
            self.admin_row()
        ]
        desc = short(item["description"], 260)
        extra = f"<b>#{eid} · {esc(item['name'])}</b> · {'ON' if item['active'] else 'OFF'}\n{esc(desc)}"
        if kind == "products":
            extra += f"\nЦена: {money(item['price_cents'])} USD · Категория #{item['category_id']}"
        if kind == "methods":
            extra += f"\n{esc(item['asset'])} · {esc(item['network'])}\n{esc(short(item['wallet'], 80))}"
        await self.screen(uid, "admin_edit", extra, rows, item["photo"])

    async def admin_queue(self, uid, page=0, all_orders=False):
        orders = self.s.orders(pending=not all_orders)
        subset, page = self.paged(orders, page)
        rows = []
        for o in subset:
            who = "@" + o["username"] if o["username"] else str(o["user_id"])
            rows.append(
                [
                    self.button(
                        f"#{o['id']} {who} · {self.label('state_' + o['status'])}",
                        f"a:order:{o['id']}:0",
                        True,
                    )
                ]
            )
        rows += self.pagination(len(orders), page, "a:all" if all_orders else "a:queue") + [self.admin_row()]
        await self.screen(uid, "admin_queue", f"Заказов: {len(orders)}", rows)

    async def admin_order(self, uid, oid, page=0):
        o = self.s.order(oid)
        if not o:
            raise ValueError("Заказ не найден")
        items, page = self.paged(o["items"], page)
        who = "@" + o["username"] if o["username"] else o["first_name"]
        extra = f"<b>#{oid} · {esc(short(who, 30))}</b>\nID: <code>{o['user_id']}</code> · {esc(self.label('state_' + o['status']))}\n"
        extra += "\n".join(
            f"{esc(short(i['name'], 22))} × {i['quantity']} · {money(i['price_cents'] * i['quantity'])}"
            for i in items
        )
        extra += f"\n<b>Итого: {money(o['total_cents'])} USD</b>"
        if o["method"]:
            m = o["method"]
            extra += f"\n{esc(m['asset'])} / {esc(m['network'])} → <code>{esc(short(m['wallet'], 60))}</code>"
        if o["status"] == "rejected":
            extra += self.rejection_extra(o)
        rows = self.pagination(len(o["items"]), page, f"a:order:{oid}")
        if o["status"] == "review":
            rows += [[self.button("yes", f"a:approveask:{oid}"), self.button("no", f"a:rejectask:{oid}")]]
        elif o["status"] == "approved":
            rows += self.rows(("deliver", f"a:deliver:{oid}"))
            if o["claimed_by"] == uid:
                rows += self.rows(("release", f"a:release:{oid}"))
            elif o["claimed_by"]:
                extra += f"\nЗанят администратором {o['claimed_by']}"
        elif o["status"] == "delivery_uncertain":
            extra += "\nСбой при отправке: проверьте у покупателя, получил ли он данные. Автоповтора нет."
            rows += self.rows(("received", f"a:resolveask:{oid}:1"), ("retry", f"a:resolveask:{oid}:0"))
        rows += self.rows(("later", "a:queue:0")) + [self.admin_row()]
        await self.screen(uid, "admin_queue", extra, rows, o["proof_file_id"] or "", text="Проверка заказа")

    async def edit_prompt(self, uid, data):
        self.s.set_session(uid, "edit", data)
        field = data["field"]
        current = ""
        if data["scope"] == "entity":
            current = self.s.entity(data["kind"], data["id"])[field]
            if field == "price_cents":
                current = money(current)
        elif data["scope"] == "label":
            current = self.label(data["key"])
        elif data["scope"] == "page" and field == "text":
            current = self.page(data["key"])
        else:
            current = self.s.get(data.get("key", ""))
        instructions = (
            "Отправьте фотографию обычным сообщением, не документом."
            if field == "photo"
            else "Отправьте новое значение одним текстовым сообщением."
        )
        if field == "description":
            instructions += " Пустое описание: отправьте /empty."
        if field == "category_id":
            instructions += " ID категории виден в разделе Категории."
        extra = esc(instructions) + "\n\nТекущее значение:\n" + esc(short(current, 300))
        rows = self.rows(("abort", "a:abort"))
        if field == "photo":
            rows += self.rows(("reset_photo", "a:resetphoto"))
        await self.screen(uid, "admin_edit", extra, rows)

    async def admin_pages(self, uid, page=0):
        keys = list(PAGES)
        subset, page = self.paged(keys, page)
        rows = [[self.button(PAGES[key][0], f"a:page:{key}", True)] for key in subset]
        rows += self.pagination(len(keys), page, "a:pages") + [self.admin_row()]
        await self.screen(uid, "admin_edit", "Настройка фотографии и текста каждого экрана.", rows)

    async def admin_labels(self, uid, page=0):
        keys = list(LABELS)
        subset, page = self.paged(keys, page)
        rows = [[self.button(f"{key}: {self.label(key)}", f"a:label:{key}", True)] for key in subset]
        rows += self.pagination(len(keys), page, "a:labels") + [self.admin_row()]
        await self.screen(uid, "admin_edit", "Название кнопки меняется без изменения её действия.", rows)

    async def settings(self, uid):
        enabled = self.s.get("checkout_enabled", "0") == "1"
        rows = self.rows(
            ("field_support", "a:setting:support"),
            ("field_shop_name", "a:setting:shop_name"),
            ("field_checkout", "a:enable"),
            ("backup", "a:backup"),
        ) + [self.admin_row()]
        extra = f"Магазин: {esc(self.s.get('shop_name', 'AURORA'))}\nПоддержка: @{esc(self.s.get('support', 'не настроена'))}\nПриём заказов: {'ON' if enabled else 'OFF'}\nКриптошлюз окружения: {'ON' if self.config.allow_manual_checkout else 'OFF'}"
        await self.screen(uid, "admin_edit", extra, rows)

    async def process_next(self, uid):
        pending = self.s.orders(pending=True)
        available = [
            o
            for o in pending
            if o["status"] in ("review", "delivery_uncertain")
            or (o["status"] == "approved" and o["claimed_by"] in (None, uid))
        ]
        if available:
            await self.admin_order(uid, available[0]["id"])
        else:
            await self.admin_queue(uid)

    async def callback(self, uid, data):
        parts = data.split(":")
        cmd = parts[0]
        if cmd == "noop":
            return
        if cmd == "a":
            self.require_admin(uid)
            await self.admin_callback(uid, parts[1:])
            return
        session = self.s.session(uid)
        if session and session["mode"] in ("proof", "reject_reason", "reject_confirm") and cmd != "paid":
            self.s.clear_session(uid)
        if cmd == "home":
            self.s.clear_session(uid)
            await self.home(uid)
        elif cmd == "catalog":
            await self.catalog(uid, int(parts[1]))
        elif cmd == "cat":
            await self.category(uid, int(parts[1]), int(parts[2]))
        elif cmd == "product":
            await self.product(uid, int(parts[1]), int(parts[2]))
        elif cmd == "add":
            self.s.cart_add(uid, int(parts[1]), int(parts[2]))
            await self.cart(uid)
        elif cmd == "cart":
            await self.cart(uid, int(parts[1]))
        elif cmd == "cartset":
            self.s.cart_set(uid, int(parts[1]), int(parts[2]))
            await self.cart(uid, int(parts[3]))
        elif cmd == "cartclear":
            await self.screen(
                uid,
                "cart",
                "Удалить все товары из корзины?",
                self.rows(("confirm", "cartclearok"), ("back", "cart:0")),
            )
        elif cmd == "cartclearok":
            self.s.cart_clear(uid)
            await self.cart(uid)
        elif cmd in ("buy", "checkout"):
            await self.checkout_confirm(
                uid, int(parts[1]) if cmd == "buy" else None, int(parts[2]) if cmd == "buy" else 1
            )
        elif cmd == "place":
            self.checkout_allowed()
            session = self.s.session(uid)
            if not session or session["mode"] != "checkout":
                raise ValueError("Заказ уже создан. Посмотрите раздел Мои заказы.")
            x = session["data"]
            current_total = (
                self.s._product(x["pid"])["price_cents"] * x["qty"] if x["pid"] else self.s.cart_total(uid)
            )
            if (
                current_total != x["total"]
                or x.get("signature") != self.checkout_signature(uid, x["pid"], x["qty"])
                or x.get("terms") != self.page("terms")
            ):
                await self.checkout_confirm(uid, x["pid"], x["qty"])
                return
            o = self.s.checkout(uid, x["pid"], x["qty"])
            self.s.set(f"order_terms:{o['id']}", x["terms"])
            self.s.clear_session(uid)
            await self.order(uid, o["id"])
        elif cmd == "method":
            self.s.choose_method(int(parts[1]), uid, int(parts[2]))
            await self.order(uid, int(parts[1]))
        elif cmd == "order":
            await self.order(uid, int(parts[1]), int(parts[2]))
        elif cmd == "items":
            oid = int(parts[1])
            o = self.s.order(oid, uid)
            if not o:
                raise ValueError("Заказ не найден")
            items, page = self.paged(o["items"], int(parts[2]))
            extra = "\n".join(
                f"{esc(short(i['name'], 32))} × {i['quantity']} — {money(i['price_cents'] * i['quantity'])} USD"
                for i in items
            )
            rows = (
                self.pagination(len(o["items"]), page, f"items:{oid}")
                + self.rows(("back", f"order:{oid}:0"))
                + [self.home_row()]
            )
            await self.screen(uid, "orders", extra, rows, text=f"Состав заказа #{oid}")
        elif cmd == "paid":
            oid = int(parts[1])
            self.s.request_proof(oid, uid)
            self.s.set_session(uid, "proof", {"id": oid})
            await self.screen(
                uid,
                "proof",
                f"Заказ <b>#{oid}</b>",
                self.rows(("back", f"order:{oid}:0")) + [self.home_row()],
            )
        elif cmd == "cancelask":
            oid = int(parts[1])
            if not self.s.order(oid, uid):
                raise ValueError("Заказ не найден")
            await self.screen(
                uid,
                "cancelled",
                "Если уже отправили перевод, не отменяйте заказ — свяжитесь с поддержкой.",
                self.rows(("confirm", f"cancelok:{oid}"), ("back", f"order:{oid}:0")),
            )
        elif cmd == "cancelok":
            self.s.cancel_order(int(parts[1]), uid)
            self.s.clear_session(uid)
            await self.order(uid, int(parts[1]))
        elif cmd == "orders":
            await self.user_orders(uid, int(parts[1]))
        elif cmd == "profile":
            await self.profile(uid)
        elif cmd in ("support", "terms"):
            extra = "@" + esc(self.s.get("support")) if cmd == "support" and self.s.get("support") else ""
            rows = ([self.support_row()] if self.s.get("support") else []) + [self.home_row()]
            await self.screen(uid, cmd, extra, rows)
        else:
            raise ValueError("Неизвестная кнопка. Откройте /start")

    async def admin_callback(self, uid, p):
        self.require_admin(uid)
        cmd = p[0]
        if cmd in {"list", "item", "pages", "page", "labels", "settings", "stats", "order", "next"}:
            self.s.clear_session(uid)
        if cmd == "home":
            self.s.clear_session(uid)
            await self.admin_home(uid)
        elif cmd in ("queue", "all"):
            self.s.clear_session(uid)
            await self.admin_queue(uid, int(p[1]), cmd == "all")
        elif cmd == "find":
            self.s.set_session(uid, "find", {})
            await self.screen(
                uid, "admin_edit", "Отправьте номер заказа, например 42.", self.rows(("abort", "a:abort"))
            )
        elif cmd == "list":
            await self.admin_list(uid, p[1], int(p[2]))
        elif cmd == "new":
            eid = self.s.create_entity(p[1])
            self.s.audit(uid, "create", p[1], eid)
            await self.admin_item(uid, p[1], eid)
        elif cmd == "item":
            await self.admin_item(uid, p[1], int(p[2]))
        elif cmd == "toggle":
            item = self.s.entity(p[1], int(p[2]))
            if not item:
                raise ValueError("Запись не найдена")
            if not item["active"]:
                if not item["name"].strip():
                    raise ValueError("Сначала задайте название")
                if p[1] == "methods" and not item["wallet"].strip():
                    raise ValueError("Сначала задайте кошелёк")
            self.s.update_entity(p[1], int(p[2]), "active", 1 - item["active"])
            self.s.audit(uid, "toggle", p[1], p[2])
            await self.admin_item(uid, p[1], int(p[2]))
        elif cmd == "edit":
            if p[1] not in FIELDS or p[3] not in FIELDS[p[1]]:
                raise ValueError("Поле не найдено")
            await self.edit_prompt(uid, {"scope": "entity", "kind": p[1], "id": int(p[2]), "field": p[3]})
        elif cmd == "pages":
            await self.admin_pages(uid, int(p[1]))
        elif cmd == "page":
            if p[1] not in PAGES:
                raise ValueError("Экран не найден")
            await self.screen(
                uid,
                p[1],
                rows=self.rows(
                    ("field_text", f"a:pageedit:{p[1]}:text"),
                    ("field_photo", f"a:pageedit:{p[1]}:photo"),
                    ("back", "a:pages:0"),
                ),
            )
        elif cmd == "pageedit":
            if p[1] not in PAGES or p[2] not in ("text", "photo"):
                raise ValueError("Поле не найдено")
            await self.edit_prompt(uid, {"scope": "page", "key": p[1], "field": p[2]})
        elif cmd == "labels":
            await self.admin_labels(uid, int(p[1]))
        elif cmd == "label":
            if p[1] not in LABELS:
                raise ValueError("Кнопка не найдена")
            await self.edit_prompt(uid, {"scope": "label", "key": p[1], "field": "text"})
        elif cmd == "setting":
            if p[1] not in ("support", "shop_name"):
                raise ValueError("Поле не найдено")
            await self.edit_prompt(uid, {"scope": "setting", "key": p[1], "field": "text"})
        elif cmd == "settings":
            await self.settings(uid)
        elif cmd == "enable":
            if self.s.get("checkout_enabled", "0") == "0":
                if not self.config.allow_manual_checkout:
                    raise ValueError(
                        "Сначала задайте ALLOW_MANUAL_CRYPTO_CHECKOUT=true в Railway Variables и перезапустите сервис. Учтите правила Telegram из DEPLOY.md."
                    )
                if not self.s.get("support") or not self.s.get("page:terms:text"):
                    raise ValueError("Сначала настройте поддержку и реальные условия покупки")
                if not self.s.list_entities("methods", active_only=True):
                    raise ValueError("Сначала настройте и включите способ оплаты")
                self.s.set("checkout_enabled", "1")
            else:
                self.s.set("checkout_enabled", "0")
            self.s.audit(uid, "toggle_checkout", "settings", "checkout")
            await self.settings(uid)
        elif cmd == "stats":
            x = self.s.stats()
            await self.screen(
                uid,
                "admin",
                f"Пользователи: {x['users']}\nЗаказы: {x['orders']}\nВ работе: {x['pending']}\nВыполнено: {x['delivered']}\nВыручка выданных заказов: {money(x['revenue_cents'])} USD",
                [self.admin_row()],
            )
        elif cmd == "backup":
            target = Path(self.config.db_path).parent / "shop-backup.sqlite3"
            self.s.backup(str(target))
            await self.bot.send_document(
                uid,
                FSInputFile(target),
                caption="Резервная копия. Содержит персональные данные — храните приватно.",
                protect_content=True,
            )
        elif cmd == "order":
            await self.admin_order(uid, int(p[1]), int(p[2]))
        elif cmd == "approveask":
            oid = int(p[1])
            o = self.reviewable(oid)
            self.s.clear_session(uid)
            await self.screen(
                uid,
                "admin_queue",
                "Вы лично проверили сумму, сеть и фактическое поступление средств?",
                self.rows(("confirm", f"a:approve:{oid}"), ("back", f"a:order:{oid}:0")),
                o["proof_file_id"],
                text=f"Заказ #{oid}",
            )
        elif cmd in ("rejectask", "reject"):
            # Legacy rejection buttons also enter comment input; they cannot reject silently.
            oid = int(p[1])
            self.reviewable(oid)
            self.s.set_session(uid, "reject_reason", {"id": oid})
            await self.screen(
                uid,
                "admin_reject",
                f"Заказ <b>#{oid}</b>",
                self.rows(("back", f"a:order:{oid}:0"), ("later", "a:queue:0")),
            )
        elif cmd == "rejectcommit":
            oid = int(p[1])
            session = self.s.session(uid)
            if not session or session["mode"] != "reject_confirm" or session["data"]["id"] != oid:
                raise ValueError("Подтверждение устарело. Откройте заказ и напишите причину заново.")
            self.s.review(oid, uid, False, session["data"]["reason"])
            self.s.audit(uid, "reject", "orders", oid)
            self.s.clear_session(uid)
            await self.after_order(
                uid, f"Заказ #{oid}: отклонён. Причина сохранена, уведомление покупателю в очереди."
            )
        elif cmd == "approve":
            oid = int(p[1])
            self.s.review(oid, uid, True)
            self.s.audit(uid, "approve", "orders", oid)
            await self.start_delivery_prompt(uid, oid)
        elif cmd == "deliver":
            await self.start_delivery_prompt(uid, int(p[1]))
        elif cmd == "release":
            self.s.release_claim(int(p[1]), uid)
            self.s.clear_session(uid)
            await self.admin_queue(uid)
        elif cmd == "send":
            oid = int(p[1])
            session = self.s.session(uid)
            if not session or session["mode"] != "delivery_confirm" or session["data"]["id"] != oid:
                raise ValueError("Подтверждение устарело. Откройте заказ заново.")
            x = session["data"]
            self.s.queue_delivery(oid, uid, uid, x["message_id"])
            self.s.audit(uid, "queue_delivery", "orders", oid)
            self.s.clear_session(uid)
            await self.after_order(
                uid, f"Данные заказа #{oid} в очереди отправки. Бот отдельно подтвердит доставку."
            )
        elif cmd == "resolveask":
            oid, delivered = int(p[1]), int(p[2])
            await self.screen(
                uid,
                "admin_queue",
                "Подтверждайте только после проверки у покупателя. Повторная выдача может привести к дублю.",
                self.rows(("confirm", f"a:resolve:{oid}:{delivered}"), ("back", f"a:order:{oid}:0")),
            )
        elif cmd == "resolve":
            self.s.resolve_delivery(int(p[1]), uid, p[2] == "1")
            await self.admin_order(uid, int(p[1]))
        elif cmd == "next":
            await self.process_next(uid)
        elif cmd == "abort":
            self.s.clear_session(uid)
            await self.admin_home(uid)
        elif cmd == "resetphoto":
            session = self.s.session(uid)
            if not session or session["mode"] != "edit" or session["data"]["field"] != "photo":
                raise ValueError("Редактор фотографии не открыт")
            await self.save_edit(uid, session["data"], "")
        else:
            raise ValueError("Неизвестная команда администратора")

    def reviewable(self, oid):
        order = self.s.order(oid)
        if not order or order["status"] != "review":
            raise ValueError("Заказ уже обработан или недоступен")
        return order

    def rejection_extra(self, order):
        reason = order.get("rejection_reason", "")
        return "\n\n<b>Причина отказа:</b>\n" + esc(reason) if reason else ""

    async def start_delivery_prompt(self, uid, oid):
        o = self.s.claim(oid, uid)
        self.s.set_session(uid, "delivery", {"id": oid})
        who = "@" + o["username"] if o["username"] else str(o["user_id"])
        await self.screen(
            uid,
            "admin_delivery",
            f"Заказ <b>#{oid}</b>\nПолучатель: {esc(who)} · ID {o['user_id']}",
            self.rows(("later", "a:queue:0"), ("release", f"a:release:{oid}")),
        )

    async def after_order(self, uid, text):
        await self.screen(
            uid,
            "admin",
            esc(text),
            self.rows(("process", "a:next"), ("later", "a:queue:0")) + [self.admin_row()],
        )

    async def save_edit(self, uid, data, value):
        self.require_admin(uid)
        scope, field = data["scope"], data["field"]
        if scope == "entity":
            if field == "price_cents":
                value = parse_price(value)
            if field in ("name", "wallet", "network") and not value.strip():
                raise ValueError("Это поле не может быть пустым")
            if field in ("name", "description", "wallet", "network"):
                limit = {"name": 60, "description": 550, "wallet": 250, "network": 50}[field]
                if len(value.encode("utf-16-le")) // 2 > limit:
                    raise ValueError(
                        f"Поле слишком длинное: максимум {limit} символов; эмодзи считаются за два"
                    )
            if field == "description" and data["kind"] == "methods" and len(value) > 220:
                raise ValueError("Описание способа оплаты: максимум 220 символов")
            self.s.update_entity(data["kind"], data["id"], field, value)
            self.s.audit(uid, "edit:" + field, data["kind"], data["id"])
        elif scope == "page":
            # Dynamic pages need space for order IDs, totals and wallet addresses.
            if field == "text" and (not value.strip() or len(value.encode("utf-16-le")) // 2 > 320):
                raise ValueError("Текст экрана: 1–320 символов (эмодзи считаются за два)")
            self.s.set(f"page:{data['key']}:{field}", value)
            self.s.audit(uid, "page:" + field, "pages", data["key"])
        elif scope == "label":
            if not value.strip() or len(value) > 48:
                raise ValueError("Название кнопки: 1–48 символов")
            self.s.set("label:" + data["key"], value.strip())
            self.s.audit(uid, "label", "labels", data["key"])
        elif scope == "setting":
            if data["key"] == "support":
                value = validate_support(value)
            elif not value.strip() or len(value) > 60:
                raise ValueError("Название: 1–60 символов")
            self.s.set(data["key"], value.strip())
            self.s.audit(uid, "setting", "settings", data["key"])
        self.s.clear_session(uid)
        if scope == "entity":
            await self.admin_item(uid, data["kind"], data["id"])
        elif scope == "page":
            await self.admin_pages(uid)
        elif scope == "label":
            await self.admin_labels(uid)
        else:
            await self.settings(uid)

    async def message(self, msg):
        uid = msg.from_user.id
        text = msg.text or ""
        if text.startswith("/start"):
            ref = None
            payload = text.split(maxsplit=1)
            if len(payload) == 2 and payload[1].startswith("ref_"):
                try:
                    ref = int(payload[1][4:])
                except ValueError:
                    pass
            self.s.register(uid, msg.from_user.username, msg.from_user.first_name, ref)
            self.s.clear_session(uid)
            # Opening /start creates a fresh screen at the bottom; navigation edits it.
            self.s.set(f"screen:{uid}", "")
            await self.home(uid)
            return
        self.s.register(uid, msg.from_user.username, msg.from_user.first_name)
        command = text.split(maxsplit=1)[0].split("@")[0] if text.strip() else ""
        if command in ("/cancel", "/menu"):
            self.s.clear_session(uid)
            await self.home(uid)
            return
        if command == "/id":
            await self.bot.send_message(uid, f"Ваш Telegram ID: {uid}")
            return
        if command == "/admin":
            self.require_admin(uid)
            self.s.clear_session(uid)
            await self.admin_home(uid)
            return
        if command in ("/support", "/paysupport", "/terms", "/orders"):
            await self.callback(
                uid,
                {"/support": "support", "/paysupport": "support", "/terms": "terms", "/orders": "orders:0"}[
                    command
                ],
            )
            return
        session = self.s.session(uid)
        if not session:
            await self.home(uid)
            return
        mode, data = session["mode"], session["data"]
        if mode in ("edit", "delivery", "delivery_confirm", "find", "reject_reason", "reject_confirm"):
            self.require_admin(uid)
        if msg.media_group_id:
            raise ValueError("Отправьте одно фото или одно сообщение, не альбом")
        if mode == "edit":
            if data["field"] == "photo":
                if not msg.photo:
                    raise ValueError("Отправьте фотографию, не файл")
                value = msg.photo[-1].file_id
            else:
                if not msg.text:
                    raise ValueError("Отправьте текст")
                value = "" if text == "/empty" and data["field"] == "description" else text
            await self.save_edit(uid, data, value)
        elif mode in ("reject_reason", "reject_confirm"):
            oid = data["id"]
            self.reviewable(oid)
            if not msg.text:
                raise ValueError("Отправьте причину отказа текстом")
            reason = self.s.validate_rejection_reason(msg.text)
            self.s.set_session(uid, "reject_confirm", {"id": oid, "reason": reason})
            await self.screen(
                uid,
                "admin_reject",
                f"Заказ <b>#{oid}</b>\n\n<b>Комментарий покупателю:</b>\n{esc(reason)}",
                self.rows(
                    ("reject_send", f"a:rejectcommit:{oid}"),
                    ("reject_edit", f"a:rejectask:{oid}"),
                    ("back", f"a:order:{oid}:0"),
                ),
            )
        elif mode == "proof":
            if not msg.photo:
                raise ValueError("Пришлите скриншот как фотографию, не документ")
            if msg.photo[-1].file_size and msg.photo[-1].file_size > 10 * 1024 * 1024:
                raise ValueError("Фотография должна быть меньше 10 МБ")
            self.s.submit_proof(data["id"], uid, msg.photo[-1].file_id, msg.photo[-1].file_unique_id)
            self.s.clear_session(uid)
            await self.order(uid, data["id"])
        elif mode in ("delivery", "delivery_confirm"):
            if not (msg.text or msg.photo or msg.document):
                raise ValueError("Допустимы текст, фото с подписью или документ")
            if msg.has_protected_content:
                raise ValueError("Это сообщение защищено от копирования. Отправьте данные напрямую.")
            oid = data["id"]
            self.s.claim(oid, uid)
            self.s.set_session(uid, "delivery_confirm", {"id": oid, "message_id": msg.message_id})
            await self.screen(
                uid,
                "admin_delivery",
                f"Заказ <b>#{oid}</b>\nОтправить покупателю ваше сообщение выше? Можно прислать новое сообщение вместо него.",
                self.rows(("send", f"a:send:{oid}"), ("later", "a:queue:0")),
            )
        elif mode == "find":
            try:
                oid = int(text.strip().lstrip("#"))
            except ValueError:
                raise ValueError("Нужен числовой номер заказа") from None
            self.s.clear_session(uid)
            await self.admin_order(uid, oid)
        else:
            await self.home(uid)

    async def on_callback(self, query):
        if not query.message or query.message.chat.type != "private":
            await query.answer("Откройте личный чат с ботом", show_alert=True)
            return
        uid = query.from_user.id
        # Always answer promptly to stop Telegram's loading indicator.
        now = time.monotonic()
        if now - self.throttle.get(uid, 0) < 0.35:
            await query.answer()
            return
        if len(self.throttle) > 10000:
            self.throttle = {k: v for k, v in self.throttle.items() if now - v < 60}
        self.throttle[uid] = now
        await query.answer()
        async with self.locks[uid % len(self.locks)]:
            try:
                self.s.register(uid, query.from_user.username, query.from_user.first_name)
                await self.callback(uid, query.data or "")
            except (ValueError, IndexError, KeyError) as e:
                await self.bot.send_message(
                    uid, str(e) if isinstance(e, ValueError) else "Кнопка устарела. Откройте /start."
                )
            except TelegramRetryAfter as e:
                await asyncio.sleep(min(e.retry_after, 5))
            except TelegramForbiddenError:
                pass
            except Exception as e:
                log.error("Callback failed: %s", type(e).__name__)
                await self.bot.send_message(
                    uid, "Не удалось выполнить действие. Повторите или откройте /start. Заказы сохранены."
                )

    async def on_message(self, msg):
        if not msg.from_user or msg.from_user.is_bot:
            return
        uid = msg.from_user.id
        async with self.locks[uid % len(self.locks)]:
            try:
                await self.message(msg)
            except ValueError as e:
                await self.bot.send_message(uid, str(e))
            except TelegramForbiddenError:
                pass
            except Exception as e:
                log.error("Message failed: %s", type(e).__name__)
                await self.bot.send_message(
                    uid, "Не удалось выполнить действие. Попробуйте ещё раз или /cancel."
                )

    async def background_once(self):
        async with self.background_lock:
            for queued in self.s.delivery_orders():
                o = self.s.start_delivery(queued["id"])
                if not o:
                    continue
                try:
                    await self.bot.copy_message(
                        chat_id=o["user_id"],
                        from_chat_id=o["delivery_chat_id"],
                        message_id=o["delivery_message_id"],
                        protect_content=True,
                    )
                except Exception as e:
                    # Telegram has no idempotency key for copyMessage. Never blindly retry:
                    # a timeout may mean the message DID arrive.
                    self.s.delivery_result(o["id"], False, type(e).__name__)
                    log.warning("Delivery requires manual reconciliation for order %s", o["id"])
                    try:
                        await self.notice(
                            o["claimed_by"],
                            "admin_queue",
                            f"Заказ #{o['id']}: проверьте доставку вручную.",
                            self.rows(("queue", f"a:order:{o['id']}:0")),
                        )
                    except Exception:
                        pass
                else:
                    self.s.delivery_result(o["id"], True)
            for o in self.s.notification_orders():
                key = (o["id"], o["status"])
                if time.monotonic() - self.notice_attempt.get(key, -999) < 60:
                    continue
                self.notice_attempt[key] = time.monotonic()
                try:
                    if o["status"] == "review":
                        ok = True
                        for aid in sorted(self.config.admins):
                            # Durable per-admin flags avoid duplicate notifications on partial failure.
                            sent_key = f"review_notice:{o['id']}:{aid}"
                            if self.s.get(sent_key):
                                continue
                            try:
                                who = "@" + o["username"] if o["username"] else str(o["user_id"])
                                await self.bot.send_photo(
                                    aid,
                                    o["proof_file_id"],
                                    caption=f"Проверка оплаты · #{o['id']}\n{who}\n{money(o['total_cents'])} USD\nПроверьте поступление в кошельке вручную.",
                                    reply_markup=InlineKeyboardMarkup(
                                        inline_keyboard=self.rows(("queue", f"a:order:{o['id']}:0"))
                                    ),
                                )
                                self.s.set(sent_key, "1")
                            except Exception as e:
                                ok = False
                                log.warning("Admin notice pending: %s", type(e).__name__)
                        if ok:
                            self.s.mark_notified(o["id"], "review")
                    elif o["status"] == "rejected":
                        await self.notice(
                            o["user_id"],
                            "rejected",
                            f"Заказ <b>#{o['id']}</b>" + self.rejection_extra(o),
                            [self.support_row(), self.home_row()],
                        )
                        self.s.mark_notified(o["id"], "reject")
                    elif o["status"] == "delivered":
                        # Retry each recipient independently; never repeat the purchased data.
                        who = "@" + o["username"] if o["username"] else str(o["user_id"])
                        recipients = [
                            (
                                "admin",
                                o["claimed_by"],
                                "admin",
                                f"Заказ <b>#{o['id']}</b>: сообщение отправлено {esc(who)} (ID {o['user_id']}).",
                                self.rows(("process", "a:next"), ("later", "a:queue:0")),
                            ),
                            (
                                "buyer",
                                o["user_id"],
                                "delivered",
                                f"Заказ <b>#{o['id']}</b>",
                                [self.support_row(), self.home_row()],
                            ),
                        ]
                        all_sent = True
                        for role, recipient, page, extra, rows in recipients:
                            sent_key = f"delivery_notice:{o['id']}:{role}"
                            if self.s.get(sent_key):
                                continue
                            try:
                                await self.notice(recipient, page, extra, rows)
                                self.s.set(sent_key, "1")
                            except Exception as e:
                                all_sent = False
                                log.warning("Completion notice pending for %s: %s", role, type(e).__name__)
                        if all_sent:
                            self.s.mark_notified(o["id"], "delivery")
                except Exception as e:
                    log.warning("Notification remains pending: %s", type(e).__name__)
            if len(self.notice_attempt) > 5000:
                now = time.monotonic()
                self.notice_attempt = {k: v for k, v in self.notice_attempt.items() if now - v < 120}

    async def worker(self):
        while True:
            try:
                await self.background_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error("Worker iteration failed: %s", type(e).__name__)
            await asyncio.sleep(3)
