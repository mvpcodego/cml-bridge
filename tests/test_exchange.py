"""Сквозной тест: эмулируем 1С, которая приходит на точку обмена.

Проверяем ровно то, что ломается в жизни:
  · полный цикл checkauth → init → file → import;
  · ответы отдаются в windows-1251, а первая строка — это протокол;
  · файл, приехавший ДВУМЯ частями, собирается в один валидный XML;
  · повторный импорт того же файла не применяется второй раз;
  · битый XML даёт внятный failure, а не падение сервиса;
  · выгрузка заказов уходит в windows-1251 и не дублируется.
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http.server

from bridge import exchange, orders as orders_mod, reconcile, store as store_mod

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
LOGIN, PASSWORD = "1c", "secret"


class Client:
    """Минимальный клиент, ведущий себя как 1С."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.cookie = ""

    def _open(self, url: str, data: bytes | None = None, auth: bool = False):
        req = urllib.request.Request(url, data=data)
        if auth:
            token = base64.b64encode(f"{LOGIN}:{PASSWORD}".encode()).decode()
            req.add_header("Authorization", f"Basic {token}")
        if self.cookie:
            req.add_header("Cookie", self.cookie)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def checkauth(self):
        status, body = self._open(f"{self.base}/exchange?type=catalog&mode=checkauth", auth=True)
        text = body.decode("cp1251")
        if text.startswith("success"):
            lines = text.splitlines()
            self.cookie = f"{lines[1]}={lines[2]}"
        return status, text

    def get(self, query: str):
        status, body = self._open(f"{self.base}/exchange?{query}")
        return status, body

    def post_file(self, filename: str, chunk: bytes):
        status, body = self._open(
            f"{self.base}/exchange?type=catalog&mode=file&filename={filename}", data=chunk
        )
        return status, body.decode("cp1251")


def run() -> int:
    tmp = tempfile.mkdtemp(prefix="cml-test-")
    db = os.path.join(tmp, "test.db")
    spool = os.path.join(tmp, "spool")
    store = store_mod.Store(db)
    exchange.Handler.exchange = exchange.Exchange(store, LOGIN, PASSWORD, spool)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), exchange.Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        mark = "OK  " if condition else "СБОЙ"
        print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not condition else ""))
        if not condition:
            failures.append(name)

    base = f"http://127.0.0.1:{port}"
    client = Client(base)

    print("\n1. Авторизация")
    status, body = client.get("type=catalog&mode=init")
    check("без авторизации init отклоняется", status == 401 and body.decode("cp1251").startswith("failure"))
    bad = Client(base)
    bad_status, bad_body = bad._open(f"{base}/exchange?type=catalog&mode=checkauth")
    check("checkauth без заголовка даёт failure", bad_body.decode("cp1251").startswith("failure"))
    status, text = client.checkauth()
    check("checkauth отдаёт success и cookie тремя строками",
          text.startswith("success") and len(text.splitlines()) == 3, text[:40])

    print("\n2. Инициализация")
    status, body = client.get("type=catalog&mode=init")
    init = body.decode("cp1251")
    check("init отдаёт zip и file_limit", "file_limit=" in init and "zip=" in init, init[:40])

    print("\n3. Файл, приехавший двумя частями")
    raw = open(os.path.join(FIXTURES, "import.xml"), "rb").read()
    half = len(raw) // 2
    client.post_file("import.xml", raw[:half])
    status, text = client.post_file("import.xml", raw[half:])
    check("обе части приняты", text.startswith("success"), text[:40])
    status, body = client.get("type=catalog&mode=import&filename=import.xml")
    imported = body.decode("cp1251")
    check("собранный из частей XML разобран", imported.startswith("success") and "товаров 2" in imported, imported[:90])

    print("\n4. Повторный импорт того же файла")
    status, body = client.get("type=catalog&mode=import&filename=import.xml")
    again = body.decode("cp1251")
    check("второй раз не применяется", "уже применялся" in again, again[:60])

    print("\n5. Предложения: цены и остатки")
    offers_raw = open(os.path.join(FIXTURES, "offers.xml"), "rb").read()
    client.post_file("offers.xml", offers_raw)
    status, body = client.get("type=catalog&mode=import&filename=offers.xml")
    off = body.decode("cp1251")
    check("предложения приняты", off.startswith("success") and "предложений 5" in off, off[:90])
    row = store.conn.execute("SELECT SUM(quantity) q FROM stock").fetchone()
    check("остатки по складам записаны", (row["q"] or 0) == 219.0, f"сумма остатков {row['q']}")
    row = store.conn.execute("SELECT price, currency FROM offers WHERE price IS NOT NULL LIMIT 1").fetchone()
    check("цена и валюта разобраны", row is not None and row["currency"] == "RUB", str(dict(row) if row else {}))

    print("\n6. Битый XML")
    client.post_file("broken.xml", "<Ком не закрыт".encode("utf-8"))
    status, body = client.get("type=catalog&mode=import&filename=broken.xml")
    broken = body.decode("cp1251")
    check("битый файл даёт failure, сервис жив", broken.startswith("failure"), broken[:70])
    status, body = client.get("type=catalog&mode=init")
    check("после битого файла обмен продолжает работать", body.decode("cp1251").startswith("zip="))

    print("\n7. Заказы обратно в 1С")
    orders_mod.add_order(store, "T-1", 2000.0,
                         [{"ident": "demo", "name": "Позиция — 2 000 ₽", "price": 1000, "quantity": 2}])
    dup = orders_mod.add_order(store, "T-1", 999.0, [{"ident": "x", "name": "дубль", "price": 1, "quantity": 1}])
    check("повторный номер заказа не создаёт дубль", dup is False)
    status, body = client.get("type=sale&mode=query")
    check("заказы отданы в windows-1251", b"windows-1251" in body[:80] and b"T-1" in body)
    text = body.decode("cp1251")
    check("непередаваемые символы заменены, XML цел", "руб." in text and text.rstrip().endswith("</КоммерческаяИнформация>"))
    client.get("type=sale&mode=success")
    status, body = client.get("type=sale&mode=query")
    check("после подтверждения заказ не выгружается снова", b"T-1" not in body)

    print("\n8. Большой каталог (634 КБ) и сверка")
    big = open(os.path.join(FIXTURES, "big_import.xml"), "rb").read()
    client.post_file("big_import.xml", big)
    status, body = client.get("type=catalog&mode=import&filename=big_import.xml")
    big_res = body.decode("cp1251")
    check("большой каталог разобран", big_res.startswith("success") and "товаров 282" in big_res, big_res[:90])

    rep = reconcile.build(store)
    codes = {f.code for f in rep.findings}
    check("сверка нашла дубли артикулов", "dup_article" in codes, str(sorted(codes)))
    check("сверка нашла позиции без фото", "no_image" in codes, str(sorted(codes)))
    text = reconcile.render_text(rep)
    check("отчёт читаемый и с итогами", "СВЕРКА КАТАЛОГА" in text and "В базе:" in text)

    print("\n9. Данные для сайта (API) и редирект человека")
    import json as _json
    status, body = client._open(f"{base}/api/state")
    state = _json.loads(body.decode("utf-8"))
    check("api/state отдаёт счётчики", status == 200 and state["counts"]["products"] > 0, str(status))
    status, body = client._open(f"{base}/api/catalog?limit=3")
    cat = _json.loads(body.decode("utf-8"))
    check("api/catalog отдаёт товары", status == 200 and len(cat["items"]) == 3 and cat["total"] > 3)
    q = urllib.parse.quote("ботинки")
    status, body = client._open(f"{base}/api/catalog?q={q}")
    found = _json.loads(body.decode("utf-8"))
    check("поиск по-русски работает через api", found["total"] > 0, str(found["total"]))
    q2 = urllib.parse.quote("Б-130005")
    status, body = client._open(f"{base}/api/catalog?q={q2}")
    by_art = _json.loads(body.decode("utf-8"))
    check("артикул с пробелом внутри находится", by_art["total"] > 0, str(by_art["total"]))
    ident = cat["items"][0]["ident"]
    status, body = client._open(f"{base}/api/product/{urllib.parse.quote(ident)}")
    prod = _json.loads(body.decode("utf-8"))
    check("api/product отдаёт позицию", status == 200 and prod["ident"] == ident)
    status, body = client._open(f"{base}/api/product/" + urllib.parse.quote("нет-такого"))
    check("несуществующая позиция даёт 404", status == 404)
    status, body = client._open(f"{base}/api/report")
    rep_json = _json.loads(body.decode("utf-8"))
    check("api/report отдаёт находки", status == 200 and rep_json["problems"] > 0)
    codes = {f["code"] for f in rep_json["findings"]}
    check("среди находок есть дубли артикулов", "dup_article" in codes, str(sorted(codes)))

    print("\n9б. Человек уходит на сайт")
    import urllib.request as _ur
    class NoRedirect(_ur.HTTPRedirectHandler):
        def redirect_request(self, *a, **kw):
            return None
    opener = _ur.build_opener(NoRedirect)
    for p_ in ("/", "/catalog", "/report"):
        try:
            opener.open(base + p_, timeout=10)
            code = 200
        except urllib.error.HTTPError as exc:
            code = exc.code
            loc = exc.headers.get("Location", "")
        check(f"{p_} отправляет на сайт", code == 302 and "mvp-code.ru/1c" in loc, f"{code} {loc}")

    print("\n10. Архив обмена с картинками")
    import io, zipfile
    # 1x1 PNG — минимальная валидная картинка
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001"
        "0d0a2db40000000049454e44ae426082"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("import.xml", open(os.path.join(FIXTURES, "import.xml"), "rb").read())
        zf.writestr("import_files/aa/photo.jpg", png)
    client.post_file("exchange.zip", buf.getvalue())
    status, body = client.get("type=catalog&mode=import&filename=exchange.zip")
    zip_res = body.decode("cp1251")
    check("архив распакован, XML обработан", zip_res.startswith("success") and "exchange.zip" in zip_res, zip_res[:110])
    check("идемпотентность работает и внутри архива", "уже применялся" in zip_res, zip_res[:110])
    status, body = client._open(f"{base}/img/import_files/aa/photo.jpg")
    check("картинка из архива отдаётся", status == 200 and body[:4] == b"\x89PNG", str(status))
    status, body = client._open(f"{base}/img/import_files/aa/" + urllib.parse.quote("нет.jpg"))
    check("отсутствующая картинка даёт 404", status == 404)
    status, body = client._open(f"{base}/img/../../etc/passwd")
    check("выход за пределы хранилища закрыт", status in (404, 400), str(status))
    status, body = client.get("type=catalog&mode=init")
    check("init теперь разрешает архивы", "zip=yes" in body.decode("cp1251"))

    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"ПРОВАЛЕНО {len(failures)}: {', '.join(failures)}")
        return 1
    print("Все проверки пройдены.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
