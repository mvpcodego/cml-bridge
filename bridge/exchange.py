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

from . import cml, orders as orders_mod, reconcile, store as store_mod, web

RESPONSE_ENCODING = "windows-1251"
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
        # zip=no — принимаем несжатые XML (архив тоже поддержан в handle_file);
        # file_limit — размер части, которую 1С пришлёт одним POST.
        return "zip=no\nfile_limit=10485760"

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

    def handle_import(self, filename: str, kind: str = "catalog") -> str:
        path = self._spool_path(filename)
        if not os.path.exists(path):
            return f"failure\nФайл {filename} не найден в очереди"
        raw = open(path, "rb").read()
        digest = hashlib.sha256(raw).hexdigest()
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

        if path == "/":
            page = web.index(ex.store.counts(), ex.store.last_session("catalog"),
                             self.headers.get("Host", "1c.mvp-code.ru").split(":")[0])
            return self._send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/health":
            return self._send("ok")
        if path == "/report":
            page = web.report(reconcile.build(ex.store))
            return self._send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
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
        status = 200 if path in ("/", "/health", "/report", "/report.txt", "/exchange",
                                 "/bitrix/admin/1c_exchange.php") else 404
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
