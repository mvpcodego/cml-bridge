#!/usr/bin/env python3
"""Запуск моста 1С ↔ каталог.

  python3 run.py serve                     — поднять точку обмена для 1С
  python3 run.py import <файл.xml> [...]   — принять выгрузку из файла
  python3 run.py report                    — показать сверку
  python3 run.py order <номер> <сумма>      — положить тестовый заказ в очередь
  python3 run.py orders                    — показать XML заказов для 1С

Переменные окружения:
  CML_DB    путь к базе (по умолчанию cml.db)
  CML_LOGIN / CML_PASSWORD  доступ для 1С (по умолчанию exchange / exchange)
  CML_PORT  порт точки обмена (8021)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bridge import cml, exchange, orders as orders_mod, reconcile, store as store_mod

DB = os.environ.get("CML_DB", "cml.db")
LOGIN = os.environ.get("CML_LOGIN", "exchange")
PASSWORD = os.environ.get("CML_PASSWORD", "exchange")
PORT = int(os.environ.get("CML_PORT", "8021"))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    command, rest = argv[0], argv[1:]
    store = store_mod.Store(DB)

    if command == "serve":
        exchange.serve(store, LOGIN, PASSWORD, port=PORT)
        return 0

    if command == "import":
        if not rest:
            print("укажите файлы выгрузки")
            return 1
        session_id = store.start_session("catalog")
        for path in rest:
            raw = open(path, "rb").read()
            root = cml.parse_bytes(raw)
            groups = cml.parse_groups(root)
            products = cml.parse_products(root)
            offers = cml.parse_offers(root)
            if groups:
                store.save_groups(groups)
            saved_p = store.save_products(products, session_id) if products else 0
            saved_o = store.save_offers(offers, session_id) if offers else 0
            store.bump_session(session_id, os.path.basename(path), saved_p, saved_o)
            print(
                f"{os.path.basename(path):18} кодировка {cml.detect_encoding(raw):11} "
                f"групп {len(groups):3} товаров {saved_p:4} предложений {saved_o:3}"
            )
        store.finish_session(session_id, "ok")
        return 0

    if command == "report":
        print(reconcile.render_text(reconcile.build(store)))
        return 0

    if command == "order":
        number = rest[0] if rest else "TEST-1"
        total = float(rest[1]) if len(rest) > 1 else 1000.0
        added = orders_mod.add_order(
            store, number, total,
            [{"ident": "demo", "name": "Тестовая позиция", "price": total, "quantity": 1}],
            {"Комментарий": "заказ из демо"},
        )
        print("добавлен" if added else "такой номер уже есть, дубль не создан")
        return 0

    if command == "orders":
        sys.stdout.buffer.write(orders_mod.export_orders(store))
        return 0

    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
