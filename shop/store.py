"""Transactional SQLite persistence for the shop."""

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

_FIELDS = {
    "categories": {"name", "description", "photo", "active", "sort_order"},
    "products": {"category_id", "name", "description", "photo", "price_cents", "active", "sort_order"},
    "methods": {"name", "description", "photo", "wallet", "asset", "network", "active", "sort_order"},
}
FIELDS = _FIELDS
_LENGTHS = {"name": 60, "description": 550, "wallet": 250, "photo": 512}
_UNPAID = ("awaiting_payment", "awaiting_proof")
_PENDING = ("review", "approved", "delivery_queued", "delivering", "delivery_uncertain")
_MAX_INT = 2**63 - 1


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _integer(value, label, minimum=1, maximum=_MAX_INT):
    if isinstance(value, bool):
        raise ValueError(f"Некорректное значение: {label}")
    if isinstance(value, str):
        if not re.fullmatch(r"-?[0-9]{1,20}", value):
            raise ValueError(f"Некорректное значение: {label}")
        value = int(value)
    if not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Некорректное значение: {label}")
    return value


def _text(value, label, maximum=None, nonempty=False):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"Некорректное значение: {label}")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"Слишком длинное значение: {label}, максимум {maximum}")
    if nonempty and not value.strip():
        raise ValueError(f"Не заполнено поле: {label}")
    return value


def _boolean(value):
    if not isinstance(value, bool):
        raise ValueError("Ожидается логическое значение")
    return value


class Store:
    def __init__(self, path):
        self.path = os.fspath(path)
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 10000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._initialize()
        except BaseException:
            self._conn.close()
            raise

    @contextmanager
    def _transaction(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.commit()
            except BaseException as exc:
                self._conn.rollback()
                if isinstance(exc, sqlite3.IntegrityError):
                    raise ValueError("Операция нарушает целостность данных") from exc
                raise

    def _initialize(self):
        statements = [
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            """CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '' CHECK(length(name)<=60),
                description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=550),
                photo TEXT NOT NULL DEFAULT '' CHECK(length(photo)<=512),
                active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)), sort_order INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER NOT NULL REFERENCES categories(id),
                name TEXT NOT NULL DEFAULT '' CHECK(length(name)<=60),
                description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=550),
                photo TEXT NOT NULL DEFAULT '' CHECK(length(photo)<=512),
                price_cents INTEGER NOT NULL DEFAULT 1 CHECK(price_cents BETWEEN 1 AND 100000000),
                active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)), sort_order INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS methods (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL DEFAULT '' CHECK(length(name)<=60),
                description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=550),
                photo TEXT NOT NULL DEFAULT '' CHECK(length(photo)<=512),
                wallet TEXT NOT NULL DEFAULT '' CHECK(length(wallet)<=250),
                asset TEXT NOT NULL DEFAULT 'USDT' CHECK(asset IN ('USDT','USDC')),
                network TEXT NOT NULL DEFAULT 'BEP20',
                active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)), sort_order INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', first_name TEXT NOT NULL DEFAULT '',
                referrer_id INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
                cart_additions INTEGER NOT NULL DEFAULT 0 CHECK(cart_additions>=0),
                CHECK(referrer_id IS NULL OR referrer_id != id))""",
            "CREATE TABLE IF NOT EXISTS sessions (user_id INTEGER PRIMARY KEY, mode TEXT NOT NULL, data TEXT NOT NULL)",
            """CREATE TABLE IF NOT EXISTS cart (
                user_id INTEGER NOT NULL REFERENCES users(id), product_id INTEGER NOT NULL REFERENCES products(id),
                quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 99), PRIMARY KEY(user_id,product_id))""",
            """CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id),
                username TEXT NOT NULL, first_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('awaiting_payment','awaiting_proof','review','approved',
                    'rejected','cancelled','delivery_queued','delivering','delivery_uncertain','delivered')),
                total_cents INTEGER NOT NULL CHECK(total_cents>0), method_snapshot TEXT,
                proof_file_id TEXT, proof_unique_id TEXT UNIQUE, claimed_by INTEGER,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                delivery_chat_id INTEGER, delivery_message_id INTEGER, delivery_error TEXT NOT NULL DEFAULT '',
                review_notified INTEGER NOT NULL DEFAULT 0 CHECK(review_notified IN (0,1)),
                reject_notified INTEGER NOT NULL DEFAULT 0 CHECK(reject_notified IN (0,1)),
                delivery_notified INTEGER NOT NULL DEFAULT 0 CHECK(delivery_notified IN (0,1)))""",
            """CREATE TABLE IF NOT EXISTS order_items (
                order_id INTEGER NOT NULL REFERENCES orders(id), product_id INTEGER NOT NULL REFERENCES products(id),
                name TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 99),
                price_cents INTEGER NOT NULL CHECK(price_cents BETWEEN 1 AND 100000000),
                PRIMARY KEY(order_id,product_id))""",
            """CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER NOT NULL, action TEXT NOT NULL,
                entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS orders_user_status ON orders(user_id,status)",
            "CREATE INDEX IF NOT EXISTS orders_status_created ON orders(status,created_at,id)",
            "CREATE INDEX IF NOT EXISTS users_referrer ON users(referrer_id)",
        ]
        with self._transaction() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] > 2:
                raise RuntimeError("Database version is newer than this application")
            for statement in statements:
                db.execute(statement)
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                cid = db.execute("INSERT INTO categories(name,active) VALUES (?,?)", ("GPT", 1)).lastrowid
                db.execute(
                    "INSERT INTO products(category_id,name,price_cents,active) VALUES (?,?,?,?)",
                    (cid, "Демо-товар GPT", 100, 0),
                )
                db.execute(
                    "INSERT INTO methods(name,wallet,asset,network,active) VALUES (?,?,?,?,?)",
                    ("BEP20 USDT", "", "USDT", "BEP20", 0),
                )
                db.execute("PRAGMA user_version = 1")
            if db.execute("PRAGMA user_version").fetchone()[0] < 2:
                db.execute("ALTER TABLE orders ADD COLUMN rejection_reason TEXT NOT NULL DEFAULT ''")
                db.execute("PRAGMA user_version = 2")

    @staticmethod
    def validate_rejection_reason(value):
        value = _text(value, "причина отказа", nonempty=True).strip()
        if len(value.encode("utf-16-le")) // 2 > 320:
            raise ValueError("Причина отказа: максимум 320 символов; эмодзи считаются за два")
        return value

    @property
    def db(self):
        return self._conn

    def _product(self, product_id):
        with self._lock:
            return self._available_product(self._conn, _integer(product_id, "товар"))

    def close(self):
        with self._lock:
            self._conn.close()

    def get(self, key, default=""):
        key = _text(key, "ключ")
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row is not None else str(default)

    def set(self, key, value):
        key = _text(key, "ключ")
        value = _text(str(value), "значение")
        with self._transaction() as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    @staticmethod
    def _kind(kind):
        if not isinstance(kind, str) or kind not in _FIELDS:
            raise ValueError("Неизвестный тип сущности")
        return kind

    def list_entities(self, kind, active_only=False, category_id=None):
        kind = self._kind(kind)
        conditions, params = [], []
        if active_only:
            conditions.append("active=1")
            if kind == "products":
                conditions.append("category_id IN (SELECT id FROM categories WHERE active=1)")
        if category_id is not None:
            if kind != "products":
                raise ValueError("Фильтр категории доступен только для товаров")
            conditions.append("category_id=?")
            params.append(_integer(category_id, "категория"))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    f"SELECT * FROM {kind}{where} ORDER BY sort_order,id", params
                ).fetchall()
            ]

    def entity(self, kind, id):
        kind = self._kind(kind)
        id = _integer(id, "идентификатор")
        with self._lock:
            row = self._conn.execute(f"SELECT * FROM {kind} WHERE id=?", (id,)).fetchone()
            return dict(row) if row is not None else None

    def create_entity(self, kind):
        kind = self._kind(kind)
        with self._transaction() as db:
            if kind == "products":
                category = db.execute("SELECT id FROM categories ORDER BY sort_order,id LIMIT 1").fetchone()
                if category is None:
                    raise ValueError("Сначала создайте категорию")
                cursor = db.execute(
                    "INSERT INTO products(category_id,name) VALUES (?,?)", (category["id"], "Новый товар")
                )
            else:
                cursor = db.execute(f"INSERT INTO {kind}(name) VALUES (?)", ("Новая запись",))
            return cursor.lastrowid

    def update_entity(self, kind, id, field, value):
        kind = self._kind(kind)
        id = _integer(id, "идентификатор")
        if not isinstance(field, str) or field not in _FIELDS[kind]:
            raise ValueError("Недопустимое поле")
        if field in _LENGTHS:
            value = _text(value, "текст", _LENGTHS[field])
        elif field == "active":
            value = int(value) if isinstance(value, bool) else _integer(value, "активность", 0, 1)
        elif field == "price_cents":
            value = _integer(value, "цена в центах", 1, 100000000)
        elif field == "category_id":
            value = _integer(value, "категория")
        elif field == "sort_order":
            value = _integer(value, "порядок сортировки", -_MAX_INT - 1)
        elif field == "asset":
            if not isinstance(value, str) or value not in ("USDT", "USDC"):
                raise ValueError("Допустимы только USDT и USDC")
        else:
            value = _text(value, "сеть", 50, nonempty=True)
        with self._transaction() as db:
            if (
                field == "category_id"
                and db.execute("SELECT id FROM categories WHERE id=?", (value,)).fetchone() is None
            ):
                raise ValueError("Категория не найдена")
            if db.execute(f"UPDATE {kind} SET {field}=? WHERE id=?", (value, id)).rowcount != 1:
                raise ValueError("Сущность не найдена")

    def session(self, uid):
        uid = _integer(uid, "пользователь")
        with self._lock:
            row = self._conn.execute("SELECT mode,data FROM sessions WHERE user_id=?", (uid,)).fetchone()
            return {"mode": row["mode"], "data": json.loads(row["data"])} if row else None

    def set_session(self, uid, mode, data):
        uid = _integer(uid, "пользователь")
        mode = _text(mode, "режим", nonempty=True)
        try:
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ValueError("Некорректные данные сессии") from exc
        with self._transaction() as db:
            db.execute(
                "INSERT INTO sessions(user_id,mode,data) VALUES (?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET mode=excluded.mode,data=excluded.data",
                (uid, mode, payload),
            )

    def clear_session(self, uid):
        uid = _integer(uid, "пользователь")
        with self._transaction() as db:
            db.execute("DELETE FROM sessions WHERE user_id=?", (uid,))

    def register(self, uid, username, first_name, referrer_id=None):
        uid = _integer(uid, "пользователь")
        username = _text(username if username is not None else "", "имя пользователя")
        first_name = _text(first_name if first_name is not None else "", "имя")
        try:
            referrer_id = _integer(referrer_id, "пригласивший") if referrer_id is not None else None
        except ValueError:
            referrer_id = None
        with self._transaction() as db:
            if referrer_id == uid or (
                referrer_id is not None
                and db.execute("SELECT id FROM users WHERE id=?", (referrer_id,)).fetchone() is None
            ):
                referrer_id = None
            db.execute(
                "INSERT INTO users(id,username,first_name,referrer_id,created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET username=excluded.username,first_name=excluded.first_name",
                (uid, username, first_name, referrer_id, _now()),
            )
            return dict(db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def user(self, uid):
        uid = _integer(uid, "пользователь")
        with self._lock:
            row = self._conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def _require_user(db, uid):
        user = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if user is None:
            raise ValueError("Пользователь не зарегистрирован")
        return user

    def profile(self, uid):
        uid = _integer(uid, "пользователь")
        with self._lock:
            row = self._conn.execute(
                """SELECT cart_additions,
                (SELECT COUNT(*) FROM users WHERE referrer_id=?) AS referrals,
                (SELECT COUNT(*) FROM orders WHERE user_id=? AND status='delivered') AS purchases,
                (SELECT COALESCE(SUM(quantity),0) FROM cart WHERE user_id=?) AS cart_quantity
                FROM users WHERE id=?""",
                (uid, uid, uid, uid),
            ).fetchone()
            if row is None:
                raise ValueError("Пользователь не зарегистрирован")
            return dict(row)

    @staticmethod
    def _cart(db, uid):
        return [
            dict(row)
            for row in db.execute(
                "SELECT p.*,c.quantity FROM cart c "
                "JOIN products p ON p.id=c.product_id WHERE c.user_id=? ORDER BY p.sort_order,p.id",
                (uid,),
            ).fetchall()
        ]

    def cart(self, uid):
        uid = _integer(uid, "пользователь")
        with self._lock:
            return self._cart(self._conn, uid)

    @staticmethod
    def _available_product(db, pid):
        product = db.execute(
            "SELECT p.* FROM products p JOIN categories c ON c.id=p.category_id "
            "WHERE p.id=? AND p.active=1 AND c.active=1",
            (pid,),
        ).fetchone()
        if product is None:
            raise ValueError("Товар или категория недоступны")
        return dict(product)

    def _change_cart(self, uid, pid, qty, add):
        uid = _integer(uid, "пользователь")
        pid = _integer(pid, "товар")
        qty = _integer(qty, "количество", 1 if add else 0, 99)
        with self._transaction() as db:
            self._require_user(db, uid)
            if qty == 0:
                db.execute("DELETE FROM cart WHERE user_id=? AND product_id=?", (uid, pid))
                return
            self._available_product(db, pid)
            existing = db.execute(
                "SELECT quantity FROM cart WHERE user_id=? AND product_id=?", (uid, pid)
            ).fetchone()
            old = existing["quantity"] if existing else 0
            new = _integer(old + qty if add else qty, "количество", 1, 99)
            if (
                existing is None
                and db.execute("SELECT COUNT(*) FROM cart WHERE user_id=?", (uid,)).fetchone()[0] >= 20
            ):
                raise ValueError("В корзине может быть не более 20 товаров")
            db.execute(
                "INSERT INTO cart(user_id,product_id,quantity) VALUES (?,?,?) "
                "ON CONFLICT(user_id,product_id) DO UPDATE SET quantity=excluded.quantity",
                (uid, pid, new),
            )
            # Count units added, including increases through cart_set; never decrease the lifetime counter.
            db.execute(
                "UPDATE users SET cart_additions=cart_additions+? WHERE id=?", (max(new - old, 0), uid)
            )

    def cart_add(self, uid, pid, qty):
        self._change_cart(uid, pid, qty, True)

    def cart_set(self, uid, pid, qty):
        self._change_cart(uid, pid, qty, False)

    def cart_clear(self, uid):
        uid = _integer(uid, "пользователь")
        with self._transaction() as db:
            db.execute("DELETE FROM cart WHERE user_id=?", (uid,))

    def cart_total(self, uid):
        uid = _integer(uid, "пользователь")
        with self._lock:
            return self._conn.execute(
                "SELECT COALESCE(SUM(p.price_cents*c.quantity),0) FROM cart c "
                "JOIN products p ON p.id=c.product_id WHERE c.user_id=?",
                (uid,),
            ).fetchone()[0]

    @staticmethod
    def _order(db, oid, uid=None):
        sql, params = "SELECT * FROM orders WHERE id=?", [oid]
        if uid is not None:
            sql += " AND user_id=?"
            params.append(uid)
        row = db.execute(sql, params).fetchone()
        if row is None:
            return None
        result = dict(row)
        snapshot = result.pop("method_snapshot")
        result["method"] = json.loads(snapshot) if snapshot is not None else None
        result["items"] = [
            dict(item)
            for item in db.execute(
                "SELECT product_id,name,quantity,price_cents FROM order_items WHERE order_id=? ORDER BY product_id",
                (oid,),
            )
        ]
        return result

    @staticmethod
    def _owned(db, oid, uid):
        row = db.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, uid)).fetchone()
        if row is None:
            raise ValueError("Заказ не найден или принадлежит другому пользователю")
        return row

    @staticmethod
    def _state(row, statuses):
        if row is None or row["status"] not in statuses:
            raise ValueError("Операция недоступна в текущем состоянии заказа")

    def checkout(self, uid, product_id=None, quantity=1):
        uid = _integer(uid, "пользователь")
        quantity = _integer(quantity, "количество", 1, 99)
        if product_id is not None:
            product_id = _integer(product_id, "товар")
        with self._transaction() as db:
            user = self._require_user(db, uid)
            if (
                db.execute(
                    "SELECT COUNT(*) FROM orders WHERE user_id=? AND status IN (?,?)", (uid, *_UNPAID)
                ).fetchone()[0]
                >= 5
            ):
                raise ValueError("Допускается не более 5 неоплаченных заказов")
            if product_id is None:
                cart = self._cart(db, uid)
                if not cart:
                    raise ValueError("Корзина пуста")
                if len(cart) > 20:
                    raise ValueError("В корзине может быть не более 20 товаров")
                products = []
                for item in cart:
                    product = self._available_product(db, item["id"])
                    product["quantity"] = _integer(item["quantity"], "количество", 1, 99)
                    products.append(product)
            else:
                product = self._available_product(db, product_id)
                product["quantity"] = quantity
                products = [product]
            total = sum(item["price_cents"] * item["quantity"] for item in products)
            now = _now()
            oid = db.execute(
                "INSERT INTO orders(user_id,username,first_name,status,total_cents,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (uid, user["username"], user["first_name"], "awaiting_payment", total, now, now),
            ).lastrowid
            db.executemany(
                "INSERT INTO order_items(order_id,product_id,name,quantity,price_cents) VALUES (?,?,?,?,?)",
                [(oid, item["id"], item["name"], item["quantity"], item["price_cents"]) for item in products],
            )
            if product_id is None:
                db.execute("DELETE FROM cart WHERE user_id=?", (uid,))
            return self._order(db, oid)

    def choose_method(self, oid, uid, mid):
        oid, uid, mid = (
            _integer(value, label)
            for value, label in ((oid, "заказ"), (uid, "пользователь"), (mid, "способ оплаты"))
        )
        with self._transaction() as db:
            row = self._owned(db, oid, uid)
            self._state(row, _UNPAID)
            method = db.execute("SELECT * FROM methods WHERE id=? AND active=1", (mid,)).fetchone()
            if method is None or not method["wallet"].strip():
                raise ValueError("Способ оплаты недоступен или кошелёк не указан")
            db.execute(
                "UPDATE orders SET method_snapshot=?,updated_at=? WHERE id=?",
                (json.dumps(dict(method), ensure_ascii=False), _now(), oid),
            )
            return self._order(db, oid)

    def request_proof(self, oid, uid):
        oid, uid = _integer(oid, "заказ"), _integer(uid, "пользователь")
        with self._transaction() as db:
            row = self._owned(db, oid, uid)
            self._state(row, _UNPAID)
            if row["method_snapshot"] is None:
                raise ValueError("Сначала выберите способ оплаты")
            db.execute(
                "UPDATE orders SET status='awaiting_proof',updated_at=? WHERE id=? AND status IN (?,?)",
                (_now(), oid, *_UNPAID),
            )
            return self._order(db, oid)

    def submit_proof(self, oid, uid, file_id, unique_id):
        oid, uid = _integer(oid, "заказ"), _integer(uid, "пользователь")
        file_id = _text(file_id, "файл подтверждения", nonempty=True)
        unique_id = _text(unique_id, "идентификатор подтверждения", nonempty=True)
        with self._transaction() as db:
            row = self._owned(db, oid, uid)
            self._state(row, ("awaiting_proof",))
            if row["method_snapshot"] is None:
                raise ValueError("Сначала выберите способ оплаты")
            if (
                db.execute("SELECT id FROM orders WHERE proof_unique_id=?", (unique_id,)).fetchone()
                is not None
            ):
                raise ValueError("Это подтверждение уже использовано для другого заказа")
            db.execute(
                "UPDATE orders SET status='review',proof_file_id=?,proof_unique_id=?,updated_at=? "
                "WHERE id=? AND status='awaiting_proof'",
                (file_id, unique_id, _now(), oid),
            )
            return self._order(db, oid)

    def cancel_order(self, oid, uid):
        oid, uid = _integer(oid, "заказ"), _integer(uid, "пользователь")
        with self._transaction() as db:
            row = self._owned(db, oid, uid)
            self._state(row, _UNPAID)
            db.execute(
                "UPDATE orders SET status='cancelled',updated_at=? WHERE id=? AND status IN (?,?)",
                (_now(), oid, *_UNPAID),
            )
            return self._order(db, oid)

    def order(self, oid, user_id=None):
        oid = _integer(oid, "заказ")
        if user_id is not None:
            user_id = _integer(user_id, "пользователь")
        with self._lock:
            return self._order(self._conn, oid, user_id)

    def _orders(self, db, where="", params=(), oldest=False):
        direction = "ASC" if oldest else "DESC"
        ids = db.execute(
            f"SELECT id FROM orders {where} ORDER BY created_at {direction},id {direction}", params
        ).fetchall()
        return [self._order(db, row["id"]) for row in ids]

    def orders(self, user_id=None, pending=False):
        conditions, params = [], []
        if user_id is not None:
            conditions.append("user_id=?")
            params.append(_integer(user_id, "пользователь"))
        if pending:
            conditions.append("status IN (?,?,?,?,?)")
            params.extend(_PENDING)
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        with self._transaction() as db:
            return self._orders(db, where, params, oldest=pending)

    def review(self, oid, aid, approve, rejection_reason="Поступление платежа не подтверждено."):
        oid, aid = _integer(oid, "заказ"), _integer(aid, "администратор")
        status = "approved" if _boolean(approve) else "rejected"
        reason = "" if approve else self.validate_rejection_reason(rejection_reason)
        with self._transaction() as db:
            if (
                db.execute(
                    "UPDATE orders SET status=?,claimed_by=?,updated_at=?,rejection_reason=? WHERE id=? AND status='review'",
                    (status, aid, _now(), reason, oid),
                ).rowcount
                != 1
            ):
                raise ValueError("Заказ уже рассмотрен или недоступен")
            return self._order(db, oid)

    def release_claim(self, oid, aid):
        oid, aid = _integer(oid, "заказ"), _integer(aid, "администратор")
        with self._transaction() as db:
            if (
                db.execute(
                    "UPDATE orders SET claimed_by=NULL,updated_at=? WHERE id=? AND status='approved' AND claimed_by=?",
                    (_now(), oid, aid),
                ).rowcount
                != 1
            ):
                raise ValueError("Нельзя освободить чужой или недоступный заказ")
            return self._order(db, oid)

    def claim(self, oid, aid):
        oid, aid = _integer(oid, "заказ"), _integer(aid, "администратор")
        with self._transaction() as db:
            if (
                db.execute(
                    "UPDATE orders SET claimed_by=?,updated_at=? WHERE id=? AND status='approved' "
                    "AND (claimed_by IS NULL OR claimed_by=?)",
                    (aid, _now(), oid, aid),
                ).rowcount
                != 1
            ):
                raise ValueError("Заказ занят другим администратором или недоступен")
            return self._order(db, oid)

    def queue_delivery(self, oid, aid, chat_id, message_id):
        oid, aid = _integer(oid, "заказ"), _integer(aid, "администратор")
        chat_id = _integer(chat_id, "чат", -_MAX_INT - 1)
        message_id = _integer(message_id, "сообщение")
        if chat_id == 0:
            raise ValueError("Некорректный идентификатор чата")
        with self._transaction() as db:
            if (
                db.execute(
                    "UPDATE orders SET status='delivery_queued',delivery_chat_id=?,delivery_message_id=?,"
                    "delivery_error='',updated_at=? WHERE id=? AND status='approved' AND claimed_by=?",
                    (chat_id, message_id, _now(), oid, aid),
                ).rowcount
                != 1
            ):
                raise ValueError("Выдача доступна только ответственному администратору одобренного заказа")
            return self._order(db, oid)

    def start_delivery(self, oid):
        oid = _integer(oid, "заказ")
        with self._transaction() as db:
            if (
                db.execute(
                    "UPDATE orders SET status='delivering',updated_at=? WHERE id=? AND status='delivery_queued'",
                    (_now(), oid),
                ).rowcount
                != 1
            ):
                return None
            return self._order(db, oid)

    def delivery_result(self, oid, success, error=""):
        oid = _integer(oid, "заказ")
        status = "delivered" if _boolean(success) else "delivery_uncertain"
        error = _text(error, "ошибка выдачи")
        with self._transaction() as db:
            if (
                db.execute(
                    "UPDATE orders SET status=?,delivery_error=?,updated_at=? WHERE id=? AND status='delivering'",
                    (status, "" if success else error, _now(), oid),
                ).rowcount
                != 1
            ):
                raise ValueError("Выдача заказа не выполняется")
            return self._order(db, oid)

    def resolve_delivery(self, oid, aid, delivered):
        oid, aid = _integer(oid, "заказ"), _integer(aid, "администратор")
        status = "delivered" if _boolean(delivered) else "approved"
        with self._transaction() as db:
            if delivered:
                cursor = db.execute(
                    "UPDATE orders SET status=?,claimed_by=?,delivery_error='',updated_at=? "
                    "WHERE id=? AND status='delivery_uncertain'",
                    (status, aid, _now(), oid),
                )
            else:
                cursor = db.execute(
                    "UPDATE orders SET status=?,claimed_by=?,delivery_error='',delivery_chat_id=NULL,"
                    "delivery_message_id=NULL,updated_at=? WHERE id=? AND status='delivery_uncertain'",
                    (status, aid, _now(), oid),
                )
            if cursor.rowcount != 1:
                raise ValueError("Заказ не ожидает уточнения результата выдачи")
            return self._order(db, oid)

    def recover_deliveries(self):
        with self._transaction() as db:
            return db.execute(
                "UPDATE orders SET status='delivery_uncertain',delivery_error=?,updated_at=? "
                "WHERE status='delivering'",
                ("Выдача прервана; требуется ручная проверка", _now()),
            ).rowcount

    def mark_notified(self, oid, kind):
        oid = _integer(oid, "заказ")
        fields = {"review": "review_notified", "reject": "reject_notified", "delivery": "delivery_notified"}
        if not isinstance(kind, str) or kind not in fields:
            raise ValueError("Неизвестный вид уведомления")
        with self._transaction() as db:
            if (
                db.execute(
                    f"UPDATE orders SET {fields[kind]}=1,updated_at=? WHERE id=?", (_now(), oid)
                ).rowcount
                != 1
            ):
                raise ValueError("Заказ не найден")

    def notification_orders(self):
        with self._transaction() as db:
            return self._orders(
                db,
                "WHERE (status='review' AND review_notified=0) "
                "OR (status='rejected' AND reject_notified=0) "
                "OR (status='delivered' AND delivery_notified=0)",
                oldest=True,
            )

    def delivery_orders(self):
        with self._transaction() as db:
            return self._orders(db, "WHERE status='delivery_queued'", oldest=True)

    def stats(self):
        with self._lock:
            return dict(
                self._conn.execute(
                    """SELECT (SELECT COUNT(*) FROM users) AS users,
                COUNT(*) AS orders, COALESCE(SUM(status IN (?,?,?,?,?)),0) AS pending,
                COALESCE(SUM(status='delivered'),0) AS delivered,
                COALESCE(SUM(CASE WHEN status='delivered' THEN total_cents ELSE 0 END),0) AS revenue_cents
                FROM orders""",
                    _PENDING,
                ).fetchone()
            )

    def audit(self, admin_id, action, entity_type, entity_id, detail=""):
        admin_id = _integer(admin_id, "администратор")
        action = _text(action, "действие", nonempty=True)
        entity_type = _text(entity_type, "тип сущности", nonempty=True)
        entity_id = _text(str(entity_id), "идентификатор сущности")
        detail = _text(detail, "подробности")
        with self._transaction() as db:
            return db.execute(
                "INSERT INTO audit_log(admin_id,action,entity_type,entity_id,detail,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (admin_id, action, entity_type, entity_id, detail, _now()),
            ).lastrowid

    def backup(self, target):
        target = os.fspath(target)
        source_name = os.path.normcase(os.path.realpath(self.path))
        target_name = os.path.normcase(os.path.realpath(target))
        if target_name == source_name or (
            os.path.exists(self.path) and os.path.exists(target) and os.path.samefile(self.path, target)
        ):
            raise ValueError("Резервная копия должна отличаться от исходной базы")
        with self._lock:
            destination = sqlite3.connect(target, timeout=10)
            try:
                self._conn.backup(destination)
            finally:
                destination.close()
