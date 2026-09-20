"""Точка входа обмена: то, что вызывает сама 1С.

1С ходит по протоколу CommerceML 2 (он же «обмен с сайтом»). Порядок такой:

  каталог:  checkauth → init → file (много раз) → import (по файлу)
  заказы:   checkauth → init → query → success

Тонкости, без которых обмен не работает, хотя код выглядит правильным:

1. **Ответы 1С читает в windows-1251.** Отдать UTF-8 — получить кракозябры
   в служебных сообщениях и непонятные ошибки на стороне 1С.
2. **Первая строка ответа — это протокол**: success / failure / progress.
   Тело после неё — для человека. Никакого JSON.
3. **Файл приходит частями.** При большом каталоге 1С режет файл на куски
   по `file_limit` байт и делает несколько POST с ОДНИМ И ТЕМ ЖЕ именем.
   Части нужно дописывать в конец, а не перезаписывать — иначе приедет
   только последний кусок, и XML окажется битым.
4. **`mode=import` может выполняться дольше таймаута.** Тогда отвечают
   `progress`, и 1С повторит запрос. Здесь импорт быстрый, но ответ
   предусмотрен.
5. **Повторный обмен не должен дублировать работу.** Каждая принятая часть
   считается по содержимому (sha256), и уже применённый файл не применяется
   второй раз — тот же приём идемпотентности, что и в обмене заказами.
"""

from __future__ import annotations

import hashlib
import http.server
import time
import os
import secrets
import urllib.parse
from typing import Callable

from . import cml, orders as orders_mod, reconcile, store as store_mod

RESPONSE_ENCODING = "windows-1251"
# Куда отправлять человека, попавшего на служебный адрес обмена
SITE_URL = os.environ.get("CML_SITE_URL", "https://mvp-code.ru/1c")
COOKIE_NAME = "cml_session"


class Exchange:
    """Состояние обмена: сессии, каталог частей, авторизация."""

    def __init__(self, store: store_mod.Store, login: str, password: str, spool: str = "spool") -> None:
        self.store = store
        self.login = login
        self.password = password
        self.spool = spool
        os.makedirs(spool, exist_ok=True)
        self.tokens: dict[str, str] = {}
        self.sessions: dict[str, int] = {}

    # --- авторизация ----------------------------------------------------
    def checkauth(self, auth_header: str | None) -> tuple[bool, str]:
        import base64

        if not auth_header or not auth_header.lower().startswith("basic "):
            return False, "failure\nНужна базовая авторизация"
        try:
            raw = base64.b64decode(auth_header.split(None, 1)[1]).decode("utf-8", "replace")
        except Exception:
            return False, "failure\nНе разобрал заголовок авторизации"
        login, _, password = raw.partition(":")
        if login != self.login or password != self.password:
            return False, "failure\nНеверный логин или пароль"
        token = secrets.token_hex(16)
        self.tokens[token] = login
        # Ровно три строки: признак, имя cookie, значение cookie.
        return True, f"success\n{COOKIE_NAME}\n{token}"

    def authorized(self, cookie: str | None) -> bool:
        if not cookie:
            return False
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value in self.tokens:
                return True
        return False

    # --- каталог --------------------------------------------------------
    def init(self, kind: str) -> str:
        session_id = self.store.start_session(kind)
        self.sessions[kind] = session_id
        # zip=yes — 1С шлёт архив: внутри и XML, и картинки товаров
        # (в XML лежат только пути вида import_files/xx/yy.jpg, сами файлы
        # приезжают в этом же архиве). file_limit — размер части одного POST.
        return "zip=yes\nfile_limit=10485760"

    def _spool_path(self, filename: str) -> str:
        safe = os.path.basename(filename or "part.xml").replace("\\", "_")
        return os.path.join(self.spool, safe)

    def handle_file(self, filename: str, chunk: bytes, first_chunk: bool) -> str:
        """Принять часть файла. Части дописываются в конец."""
        path = self._spool_path(filename)
        mode = "wb" if first_chunk else "ab"
        with open(path, mode) as fh:
            fh.write(chunk)
        return "success"

    def unpack_archive(self, path: str) -> list[str]:
        """Распаковать архив обмена: XML в очередь, картинки — в хранилище.

        Картинки лежат внутри того же архива, что и XML, по путям, на которые
        ссылается каталог (`import_files/...`). Если их не сохранить, товары
        останутся без фотографий, а в XML будут ссылки в никуда.
        """
        import zipfile

        xmls: list[str] = []
        images_root = os.path.join(self.spool, "images")
        os.makedirs(images_root, exist_ok=True)
        with zipfile.ZipFile(path) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                name = member.filename.replace("\\", "/")
                low = name.lower()
                if low.endswith(".xml"):
                    target = os.path.join(self.spool, os.path.basename(name))
                    with zf.open(member) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    xmls.append(os.path.basename(name))
                elif low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                    # Путь сохраняем как есть — по нему на картинку ссылается XML.
                    safe = "/".join(
                        part for part in name.split("/") if part not in ("", ".", "..")
                    )
                    target = os.path.join(images_root, safe)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        dst.write(src.read())
        return xmls

    def image_path(self, rel: str) -> str | None:
        """Абсолютный путь к картинке по ссылке из XML, если файл приехал."""
        rel = rel.replace("\\", "/").strip().lstrip("/")
        safe = "/".join(part for part in rel.split("/") if part not in ("", ".", ".."))
        if not safe:
            return None
        full = os.path.join(self.spool, "images", safe)
        root = os.path.realpath(os.path.join(self.spool, "images"))
        if not os.path.realpath(full).startswith(root):
            return None
        return full if os.path.isfile(full) else None

    def handle_import(self, filename: str, kind: str = "catalog") -> str:
        path = self._spool_path(filename)
        if not os.path.exists(path):
            return f"failure\nФайл {filename} не найден в очереди"
        raw = open(path, "rb").read()
        digest = hashlib.sha256(raw).hexdigest()

        # Архив: внутри XML и картинки. Распаковываем и импортируем все XML.
        if raw[:2] == b"PK":
            try:
                names = self.unpack_archive(path)
            except Exception as exc:
                return f"failure\nНе смог распаковать {filename}: {exc}"
            if not names:
                return f"failure\nВ архиве {filename} нет XML"
            results = [self.handle_import(name, kind) for name in names]
            body = "; ".join(r.replace("success\n", "") for r in results)
            return f"success\n{filename}: {body}"
        session_id = self.sessions.get(kind) or self.store.start_session(kind)
        self.sessions[kind] = session_id

        if self.store.already_applied(digest):
            self.store.bump_session(session_id, os.path.basename(path), 0, 0)
            return "success\nЭтот файл уже применялся, повторно не импортирую"

        try:
            root = cml.parse_bytes(raw)
        except Exception as exc:  # битый XML — говорим внятно, а не падаем
            self.store.finish_session(session_id, "error", f"{filename}: {exc}")
            return f"failure\nНе разобрал {filename}: {exc}"

        groups = cml.parse_groups(root)
        products = cml.parse_products(root)
        offers = cml.parse_offers(root)

        if groups:
            self.store.save_groups(groups)
        saved_p = self.store.save_products(products, session_id) if products else 0
        saved_o = self.store.save_offers(offers, session_id) if offers else 0

        self.store.mark_applied(digest, os.path.basename(path), session_id)
        self.store.bump_session(session_id, os.path.basename(path), saved_p, saved_o)
        return (
            "success\n"
            f"{os.path.basename(path)}: групп {len(groups)}, товаров {saved_p}, предложений {saved_o}"
        )

    # --- заказы ---------------------------------------------------------
    def query_orders(self) -> bytes:
        """Отдать 1С новые заказы. Ответ — XML в windows-1251, как ждёт 1С."""
        return orders_mod.export_orders(self.store)

    def mark_orders_exported(self) -> str:
        self.store.conn.execute("UPDATE orders_out SET exported=1 WHERE exported=0")
        self.store.conn.commit()
        return "success"


class Handler(http.server.BaseHTTPRequestHandler):
    exchange: Exchange | None = None
    server_version = "cml-bridge"

    def log_message(self, fmt: str, *args) -> None:  # тише в консоли
        print(f"[обмен] {self.address_string()} {fmt % args}")

    # --- служебное -------------------------------------------------------
    def _send(self, body: str, status: int = 200) -> None:
        data = body.encode(RESPONSE_ENCODING, "replace")
        self.send_response(status)
        self.send_header("Content-Type", f"text/plain; charset={RESPONSE_ENCODING}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _params(self) -> dict[str, str]:
        query = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}

    # --- маршруты --------------------------------------------------------
    def do_GET(self) -> None:
        ex = self.exchange
        assert ex is not None
        path = urllib.parse.urlparse(self.path).path
        params = self._params()

        # Страницы для человека живут на сайте mvp-code.ru/1c — там общая шапка,
        # подвал и переключатель языков. Здесь остались только обмен для 1С,
        # выдача данных сайту и раздача картинок.
        if path in ("/", "/catalog", "/report", "/report.txt") or path.startswith("/catalog/"):
            self.send_response(302)
            self.send_header("Location", SITE_URL)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if path == "/health":
            return self._send("ok")
        if path.startswith("/api/"):
            import json as _json

            def out(payload, code=200):
                data = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                # Данные забирает сайт mvp-code.ru — отдаём их открыто:
                # это демонстрационный контур, секретов в каталоге нет.
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=60")
                self.end_headers()
                self.wfile.write(data)

            if path == "/api/state":
                last = ex.store.last_session("catalog")
                return out({
                    "counts": ex.store.counts(),
                    "last": (dict(last) if last is not None else None),
                })

            if path == "/api/catalog":
                query = params.get("q", "")
                try:
                    limit = min(200, max(1, int(params.get("limit", "50"))))
                    offset = max(0, int(params.get("offset", "0")))
                except ValueError:
                    limit, offset = 50, 0
                rows, total = ex.store.search_products(query, limit, offset)
                items = []
                for r in rows:
                    rel = next((i for i in (r["images"] or "").split("\n") if i.strip()), None)
                    items.append({
                        "ident": r["ident"], "name": r["name"], "article": r["article"],
                        "price": r["price_min"], "quantity": r["qty_total"],
                        "offers": r["offers_count"],
                        "image": ("/img/" + urllib.parse.quote(rel)) if (rel and ex.image_path(rel)) else None,
                        "image_declared": bool(rel),
                    })
                return out({"total": total, "offset": offset, "limit": limit, "items": items})

            if path.startswith("/api/product/"):
                ident = urllib.parse.unquote(path[len("/api/product/"):])
                row, offers, stock = ex.store.product(ident)
                if row is None:
                    return out({"error": "not found"}, 404)
                by_offer: dict = {}
                for st in stock:
                    by_offer.setdefault(st["offer_ident"], []).append(
                        {"warehouse": st["warehouse"], "quantity": st["quantity"]}
                    )
                props = {}
                for line in (row["props"] or "").split("\n"):
                    if "=" in line:
                        k, _, v = line.partition("=")
                        props[k] = v
                images = []
                for rel in (row["images"] or "").split("\n"):
                    rel = rel.strip()
                    if not rel:
                        continue
                    images.append({
                        "path": rel,
                        "url": ("/img/" + urllib.parse.quote(rel)) if ex.image_path(rel) else None,
                    })
                return out({
                    "ident": row["ident"], "name": row["name"], "article": row["article"],
                    "groups": ex.store.group_names(
                        [g for g in (row["groups"] or "").split(",") if g]
                    ),
                    "props": props, "images": images,
                    "offers": [
                        {
                            "ident": o["ident"], "name": o["name"], "price": o["price"],
                            "currency": o["currency"], "quantity": o["quantity"],
                            "features": dict(
                                line.split("=", 1) for line in (o["features"] or "").split("\n")
                                if "=" in line
                            ),
                            "stock": by_offer.get(o["ident"], []),
                        }
                        for o in offers
                    ],
                })

            if path == "/api/report":
                rep = reconcile.build(ex.store)
                return out({
                    "totals": rep.totals,
                    "problems": rep.problems,
                    "findings": [
                        {"code": f.code, "title": f.title, "count": f.count,
                         "samples": f.samples, "hint": f.hint}
                        for f in rep.findings
                    ],
                })

            return out({"error": "unknown endpoint"}, 404)

        if path.startswith("/img/"):
            rel = urllib.parse.unquote(path[len("/img/"):])
            full = ex.image_path(rel)
            if not full:
                return self._send("failure\nКартинка не приехала", 404)
            ctype = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".webp": "image/webp", ".gif": "image/gif",
            }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
            with open(full, "rb") as fh:
                data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
            return None
        if path == "/report.txt":
            return self._send(reconcile.render_text(reconcile.build(ex.store)))

        if path not in ("/exchange", "/bitrix/admin/1c_exchange.php"):
            return self._send("failure\nНеизвестный адрес", 404)

        kind = params.get("type", "catalog")
        mode = params.get("mode", "")

        if mode == "checkauth":
            ok, body = ex.checkauth(self.headers.get("Authorization"))
            return self._send(body, 200 if ok else 401)

        if not ex.authorized(self.headers.get("Cookie")):
            return self._send("failure\nНет сессии, начните с checkauth", 401)

        if mode == "init":
            return self._send(ex.init(kind))
        if mode == "import":
            return self._send(ex.handle_import(params.get("filename", ""), kind))
        if mode == "query":
            return self._send_bytes(ex.query_orders(), f"text/xml; charset={RESPONSE_ENCODING}")
        if mode == "success":
            return self._send(ex.mark_orders_exported())
        if mode == "deactivate":
            return self._send("success")
        return self._send(f"failure\nНеизвестный режим {mode}")

    def do_HEAD(self) -> None:
        """Мониторинг и прокси часто проверяют доступность именно HEAD-запросом.
        Без этого обработчика базовый класс отвечает 501, и снаружи сервис
        выглядит нерабочим при живом GET."""
        path = urllib.parse.urlparse(self.path).path
        status = 200 if (path in ("/health", "/exchange", "/bitrix/admin/1c_exchange.php")
                         or path.startswith(("/api/", "/img/"))) else 404
        self.send_response(status)
        self.send_header("Content-Type", f"text/plain; charset={RESPONSE_ENCODING}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        ex = self.exchange
        assert ex is not None
        path = urllib.parse.urlparse(self.path).path
        if path not in ("/exchange", "/bitrix/admin/1c_exchange.php"):
            return self._send("failure\nНеизвестный адрес", 404)
        if not ex.authorized(self.headers.get("Cookie")):
            return self._send("failure\nНет сессии, начните с checkauth", 401)

        params = self._params()
        if params.get("mode") != "file":
            return self._send(f"failure\nPOST поддерживается только для mode=file")

        length = int(self.headers.get("Content-Length") or 0)
        chunk = self.rfile.read(length) if length else b""
        # 1С не сообщает, какая это часть; признак первой — заголовок Content-Range
        # отсутствует и файла ещё нет.
        filename = params.get("filename", "")
        first = not os.path.exists(ex._spool_path(filename))
        return self._send(ex.handle_file(filename, chunk, first))


def serve(store: store_mod.Store, login: str, password: str, host: str = "0.0.0.0",
          port: int = 8021, spool: str = "spool") -> None:
    Handler.exchange = Exchange(store, login, password, spool)
    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    print(f"точка обмена: http://{host}:{port}/exchange  (адрес для 1С)")
    print(f"сверка:       http://{host}:{port}/report")
    httpd.serve_forever()
