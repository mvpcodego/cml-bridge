"""Хранилище каталога и журнал обменов.

SQLite по умолчанию — чтобы кейс запускался одной командой на любой машине,
включая машину заказчика, без установки сервера БД. Схема намеренно простая
и переносится в PostgreSQL один в один.

Главное в этом файле — не таблицы, а две вещи, которых обычно не хватает
самописным обменам:

1. Журнал обменов (`sessions`): что пришло, когда, сколько позиций, чем
   закончилось. Без него на вопрос «почему товара нет на сайте» отвечают
   догадками.
2. Отметка `seen_at` у каждой позиции. Именно она позволяет потом сказать,
   какие товары ПЕРЕСТАЛИ приходить из 1С — то есть поймать архивацию
   и удаление, которых инкрементальный обмен не показывает.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    finished_at REAL,
    kind        TEXT NOT NULL,          -- catalog | sale
    files       TEXT NOT NULL DEFAULT '',
    products    INTEGER NOT NULL DEFAULT 0,
    offers      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'running',
    note        TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS groups (
    ident   TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    parent  TEXT
);

CREATE TABLE IF NOT EXISTS products (
    ident       TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    article     TEXT NOT NULL DEFAULT '',
    article_key TEXT NOT NULL DEFAULT '',   -- нормализованный артикул для сопоставления
    unit        TEXT NOT NULL DEFAULT '',
    groups      TEXT NOT NULL DEFAULT '',
    props       TEXT NOT NULL DEFAULT '',
    images      TEXT NOT NULL DEFAULT '',
    deleted     INTEGER NOT NULL DEFAULT 0,
    seen_at     REAL NOT NULL,
    session_id  INTEGER
);
CREATE INDEX IF NOT EXISTS products_article_key ON products(article_key);
CREATE INDEX IF NOT EXISTS products_seen_at ON products(seen_at);

CREATE TABLE IF NOT EXISTS offers (
    ident          TEXT PRIMARY KEY,
    product_ident  TEXT NOT NULL,
    variant_ident  TEXT NOT NULL DEFAULT '',
    name           TEXT NOT NULL DEFAULT '',
    article        TEXT NOT NULL DEFAULT '',
    price          REAL,
    currency       TEXT NOT NULL DEFAULT '',
    quantity       REAL,
    features       TEXT NOT NULL DEFAULT '',
    seen_at        REAL NOT NULL,
    session_id     INTEGER
);
CREATE INDEX IF NOT EXISTS offers_product ON offers(product_ident);

CREATE TABLE IF NOT EXISTS stock (
    offer_ident TEXT NOT NULL,
    warehouse   TEXT NOT NULL,
    quantity    REAL NOT NULL,
    seen_at     REAL NOT NULL,
    PRIMARY KEY (offer_ident, warehouse)
);

CREATE TABLE IF NOT EXISTS orders_out (
    number     TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    total      REAL NOT NULL,
    payload    TEXT NOT NULL,
    exported   INTEGER NOT NULL DEFAULT 0
);

-- Ключ идемпотентности: повторно принятый тот же файл не должен
-- переписывать каталог второй раз и не должен плодить сессии.
CREATE TABLE IF NOT EXISTS applied_files (
    digest     TEXT PRIMARY KEY,
    filename   TEXT NOT NULL,
    applied_at REAL NOT NULL,
    session_id INTEGER
);
"""


def normalize_article(raw: str) -> str:
    """Артикул для сопоставления.

    В реальных выгрузках встречается «Б- 130005» — с пробелом внутри, хотя
    на сайте тот же товар лежит как «Б-130005». Сопоставление по сырому
    значению даёт «товара нет», хотя он есть. Поэтому для поиска держим
    отдельный нормализованный ключ, а исходное значение не трогаем.
    """
    cleaned = re.sub(r"[\s ]+", "", raw or "").upper()
    return cleaned.replace("—", "-").replace("–", "-")


@dataclass
class ApplyResult:
    session_id: int
    products: int
    offers: int
    skipped_duplicate: bool = False


class Store:
    """Доступ к базе.

    `check_same_thread=False` плюс блокировка — потому что точка обмена
    многопоточная: 1С присылает части файла параллельно, а SQLite по
    умолчанию запрещает использовать соединение из другого потока
    (ошибка «SQLite objects created in a thread can only be used in that
    same thread»). Сериализуем записи сами, одной блокировкой.
    """

    def __init__(self, path: str = "cml.db") -> None:
        self.path = path
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        # Встроенный lower() в SQLite приводит к нижнему регистру ТОЛЬКО латиницу:
        # поиск по «ботинки» не найдёт «Ботинки». Для русского каталога это
        # означает, что поиск просто не работает — без ошибки, просто пустой
        # результат. Поэтому регистр приводим средствами Python.
        self.conn.create_function("rulower", 1, lambda v: v.lower() if isinstance(v, str) else v)
        # WAL — чтобы чтение отчёта не блокировалось идущим обменом.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # --- сессии обмена -------------------------------------------------
    def start_session(self, kind: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO sessions (started_at, kind) VALUES (?, ?)", (time.time(), kind)
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def finish_session(self, session_id: int, status: str, note: str = "") -> None:
        with self.lock:
            self.conn.execute(
                "UPDATE sessions SET finished_at=?, status=?, note=? WHERE id=?",
                (time.time(), status, note, session_id),
            )
            self.conn.commit()

    def bump_session(self, session_id: int, filename: str, products: int, offers: int) -> None:
        with self.lock:
            row = self.conn.execute("SELECT files FROM sessions WHERE id=?", (session_id,)).fetchone()
            files = [f for f in (row["files"] or "").split(",") if f]
            if filename and filename not in files:
                files.append(filename)
            self.conn.execute(
                "UPDATE sessions SET files=?, products=products+?, offers=offers+? WHERE id=?",
                (",".join(files), products, offers, session_id),
            )
            self.conn.commit()

    def already_applied(self, digest: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM applied_files WHERE digest=?", (digest,)
        ).fetchone()
        return row is not None

    def mark_applied(self, digest: str, filename: str, session_id: int) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO applied_files (digest, filename, applied_at, session_id) "
                "VALUES (?,?,?,?)",
                (digest, filename, time.time(), session_id),
            )
            self.conn.commit()

        # --- запись каталога ------------------------------------------------
    def save_groups(self, groups) -> None:
        with self.lock:
            self.conn.executemany(
                "INSERT INTO groups (ident,name,parent) VALUES (?,?,?) "
                "ON CONFLICT(ident) DO UPDATE SET name=excluded.name, parent=excluded.parent",
                [(g.ident, g.name, g.parent) for g in groups],
            )
            self.conn.commit()

    def save_products(self, products, session_id: int) -> int:
        with self.lock:
            now = time.time()
            rows = [
                (
                    p.ident,
                    p.name,
                    p.article,
                    normalize_article(p.article),
                    p.unit,
                    ",".join(p.groups),
                    "\n".join(f"{k}={v}" for k, v in p.props.items()),
                    "\n".join(p.images),
                    1 if p.deleted else 0,
                    now,
                    session_id,
                )
                for p in products
            ]
            self.conn.executemany(
                "INSERT INTO products (ident,name,article,article_key,unit,groups,props,images,deleted,seen_at,session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ident) DO UPDATE SET name=excluded.name, article=excluded.article, "
                "article_key=excluded.article_key, unit=excluded.unit, groups=excluded.groups, "
                "props=excluded.props, images=excluded.images, deleted=excluded.deleted, "
                "seen_at=excluded.seen_at, session_id=excluded.session_id",
                rows,
            )
            self.conn.commit()
            return len(rows)

    def save_offers(self, offers, session_id: int) -> int:
        with self.lock:
            now = time.time()
            self.conn.executemany(
                "INSERT INTO offers (ident,product_ident,variant_ident,name,article,price,currency,quantity,features,seen_at,session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(ident) DO UPDATE SET product_ident=excluded.product_ident, "
                "variant_ident=excluded.variant_ident, name=excluded.name, article=excluded.article, "
                "price=excluded.price, currency=excluded.currency, quantity=excluded.quantity, "
                "features=excluded.features, seen_at=excluded.seen_at, session_id=excluded.session_id",
                [
                    (
                        o.ident,
                        o.product_ident,
                        o.variant_ident,
                        o.name,
                        o.article,
                        o.price,
                        o.currency,
                        o.quantity,
                        "\n".join(f"{k}={v}" for k, v in o.features.items()),
                        now,
                        session_id,
                    )
                    for o in offers
                ],
            )
            stock_rows = [
                (o.ident, wh, qty, now) for o in offers for wh, qty in o.warehouses.items()
            ]
            if stock_rows:
                self.conn.executemany(
                    "INSERT INTO stock (offer_ident,warehouse,quantity,seen_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(offer_ident,warehouse) DO UPDATE SET quantity=excluded.quantity, "
                    "seen_at=excluded.seen_at",
                    stock_rows,
                )
            self.conn.commit()
            return len(offers)

        # --- чтение ---------------------------------------------------------
    def counts(self) -> dict[str, int]:
        out = {}
        for table in ("groups", "products", "offers", "stock", "sessions"):
            out[table] = self.conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        return out

    # --- витрина каталога (для человека) --------------------------------
    def search_products(self, query: str = "", limit: int = 50, offset: int = 0):
        """Список товаров с ценой и остатком. Поиск по названию и артикулу,
        причём артикул ищется по нормализованному ключу — чтобы «Б-130005»
        находил и позицию, записанную как «Б- 130005»."""
        params: list = []
        where = ""
        if query.strip():
            q = f"%{query.strip().lower()}%"
            key = normalize_article(query)
            where = ("WHERE rulower(p.name) LIKE ? OR rulower(p.article) LIKE ? "
                     "OR p.article_key LIKE ?")
            params += [q, q, f"%{key}%"]
        sql = f"""
            SELECT p.ident, p.name, p.article, p.images, p.deleted,
                   (SELECT COUNT(*) FROM offers o WHERE o.product_ident = p.ident) AS offers_count,
                   (SELECT MIN(o.price) FROM offers o
                     WHERE o.product_ident = p.ident AND o.price IS NOT NULL) AS price_min,
                   (SELECT SUM(COALESCE(st.q, o.quantity, 0)) FROM offers o
                      LEFT JOIN (SELECT offer_ident, SUM(quantity) q FROM stock GROUP BY offer_ident) st
                        ON st.offer_ident = o.ident
                     WHERE o.product_ident = p.ident) AS qty_total
            FROM products p
            {where}
            ORDER BY p.name
            LIMIT ? OFFSET ?
        """
        rows = self.conn.execute(sql, params + [limit, offset]).fetchall()
        total = self.conn.execute(
            f"SELECT COUNT(*) c FROM products p {where}", params
        ).fetchone()["c"]
        return rows, total

    def product(self, ident: str):
        row = self.conn.execute("SELECT * FROM products WHERE ident=?", (ident,)).fetchone()
        if row is None:
            return None, [], []
        offers = self.conn.execute(
            "SELECT * FROM offers WHERE product_ident=? ORDER BY name", (ident,)
        ).fetchall()
        stock = self.conn.execute(
            "SELECT s.offer_ident, s.warehouse, s.quantity FROM stock s "
            "JOIN offers o ON o.ident = s.offer_ident WHERE o.product_ident=?", (ident,)
        ).fetchall()
        return row, offers, stock

    def group_names(self, idents: list[str]) -> list[str]:
        if not idents:
            return []
        marks = ",".join("?" * len(idents))
        rows = self.conn.execute(
            f"SELECT name FROM groups WHERE ident IN ({marks})", idents
        ).fetchall()
        return [r["name"] for r in rows]

    def last_session(self, kind: str = "catalog"):
        return self.conn.execute(
            "SELECT * FROM sessions WHERE kind=? ORDER BY id DESC LIMIT 1", (kind,)
        ).fetchone()
