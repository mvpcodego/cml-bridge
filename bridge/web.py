"""Страницы для человека: главная и отчёт сверки.

1С читает только текстовые ответы точки обмена; всё, что здесь — для людей,
которые откроют адрес в браузере: заказчика, его подрядчика, меня самого.
Поэтому здесь обычный HTML в UTF-8, без библиотек и без сборки.
"""

from __future__ import annotations

import html
import time
import urllib.parse

CSS = """
*, *::before, *::after { box-sizing: border-box; }
:root {
  --bg: #f7f7f5; --panel: #ffffff; --ink: #14161a; --muted: #6b7078;
  --line: #e3e3df; --accent: #1f6feb; --warn: #b54708; --ok: #12704a;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #121316; --panel: #1a1c20; --ink: #e8e9ea; --muted: #9aa0a8;
    --line: #2a2d33; --accent: #6ea8fe; --warn: #f0a868; --ok: #6fd0a5;
  }
}
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0; background: var(--bg); color: var(--ink);
  font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
.wrap { max-width: 860px; margin: 0 auto; padding: 48px 20px 80px; }
header { border-bottom: 1px solid var(--line); padding-bottom: 26px; margin-bottom: 34px; }
h1 { font-size: clamp(1.45rem, 3.4vw, 1.95rem); line-height: 1.25; margin: 0 0 8px; letter-spacing: -.02em; }
.lead { color: var(--muted); margin: 0; max-width: 62ch; }
h2 { font-size: .8rem; text-transform: uppercase; letter-spacing: .09em;
     color: var(--muted); margin: 38px 0 14px; font-weight: 600; }
.panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 20px 22px; }
.panel + .panel { margin-top: 14px; }
code, .mono { font-family: var(--mono); font-size: .92em; }
.addr { display: block; font-family: var(--mono); font-size: 1.02rem; word-break: break-all;
        background: var(--bg); border: 1px solid var(--line); border-radius: 8px;
        padding: 12px 14px; margin: 4px 0 14px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); gap: 12px; }
.stat { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; }
.stat b { display: block; font-size: 1.7rem; line-height: 1.1; font-variant-numeric: tabular-nums; letter-spacing: -.02em; }
.stat span { color: var(--muted); font-size: .85rem; }
.rows { width: 100%; border-collapse: collapse; }
.rows td { padding: 9px 0; border-bottom: 1px solid var(--line); vertical-align: top; }
.rows tr:last-child td { border-bottom: 0; }
.rows td:last-child { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; padding-left: 16px; }
.muted { color: var(--muted); }
.finding { border-left: 3px solid var(--warn); padding-left: 16px; margin: 0 0 26px; }
.finding h3 { margin: 0 0 6px; font-size: 1rem; font-weight: 600; }
.finding .num { color: var(--warn); font-variant-numeric: tabular-nums; }
.finding ul { margin: 8px 0; padding-left: 18px; }
.finding li { font-family: var(--mono); font-size: .88rem; color: var(--ink); }
.finding p { margin: 8px 0 0; color: var(--muted); font-size: .92rem; max-width: 70ch; }
.ok-box { border-left: 3px solid var(--ok); padding-left: 16px; }
a { color: var(--accent); }
nav { margin-top: 30px; display: flex; gap: 18px; flex-wrap: wrap; font-size: .94rem; }

.search { display: flex; gap: 10px; margin: 0 0 20px; }
.search input { flex: 1; min-width: 0; padding: 11px 14px; font: inherit;
  background: var(--panel); color: var(--ink); border: 1px solid var(--line); border-radius: 9px; }
.search button { padding: 11px 20px; font: inherit; cursor: pointer; border-radius: 9px;
  border: 1px solid var(--accent); background: var(--accent); color: #fff; }
.items { border: 1px solid var(--line); border-radius: 12px; overflow: hidden; background: var(--panel); }
.item { display: grid; grid-template-columns: 1fr auto auto; gap: 8px 20px;
        padding: 13px 18px; border-bottom: 1px solid var(--line); align-items: baseline; }
.item:last-child { border-bottom: 0; }
.item a { text-decoration: none; font-weight: 500; }
.item .art { display: block; font-family: var(--mono); font-size: .82rem; color: var(--muted); margin-top: 2px; }
.item .price, .item .qty { font-variant-numeric: tabular-nums; white-space: nowrap; }
.item .qty { color: var(--muted); font-size: .9rem; }
.zero { color: var(--warn); }
.pager { display: flex; gap: 14px; margin: 20px 0 0; align-items: center; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; margin: 6px 0 0; }
.tag { font-size: .8rem; border: 1px solid var(--line); border-radius: 100px; padding: 2px 10px; color: var(--muted); }
@media (max-width: 560px) { .item { grid-template-columns: 1fr; } }
footer { margin-top: 46px; padding-top: 22px; border-top: 1px solid var(--line);
         color: var(--muted); font-size: .9rem; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)}</title><style>{CSS}</style></head>"
        f"<body><div class=\"wrap\">{body}</div></body></html>"
    )


def index(counts: dict, last_session, host: str) -> str:
    if last_session is not None:
        when = time.strftime("%d.%m.%Y в %H:%M", time.localtime(last_session["started_at"]))
        files = html.escape(last_session["files"] or "—")
        status = last_session["status"]
        last_html = (
            f'<table class="rows">'
            f'<tr><td>Когда</td><td>{when}</td></tr>'
            f'<tr><td>Файлы</td><td class="mono">{files}</td></tr>'
            f'<tr><td>Принято товаров</td><td>{last_session["products"]}</td></tr>'
            f'<tr><td>Принято предложений</td><td>{last_session["offers"]}</td></tr>'
            f'<tr><td>Результат</td><td>{html.escape(status)}</td></tr>'
            f'</table>'
        )
    else:
        last_html = '<p class="muted" style="margin:0">Обменов ещё не было.</p>'

    stats = "".join(
        f'<div class="stat"><b>{counts.get(key, 0)}</b><span>{label}</span></div>'
        for key, label in (
            ("groups", "групп"),
            ("products", "товаров"),
            ("offers", "предложений"),
            ("stock", "записей об остатках"),
            ("sessions", "обменов"),
        )
    )

    body = f"""
<header>
  <h1>Мост 1С ↔ каталог</h1>
  <p class="lead">Принимает обмен из 1С по протоколу CommerceML 2, сверяет каталог
  и отдаёт заказы обратно. Показывает не только «обмен прошёл», но и то, чего
  в каталоге не хватает.</p>
</header>

<h2>Подключение 1С</h2>
<div class="panel">
  <span class="muted">Адрес обмена</span>
  <code class="addr">https://{html.escape(host)}/exchange</code>
  <p class="muted" style="margin:0">Работает и привычный для 1С путь
  <code>/bitrix/admin/1c_exchange.php</code> — настройку в 1С менять не придётся.
  Доступ выдаётся отдельно, базовая авторизация.</p>
</div>

<h2>В базе сейчас</h2>
<div class="grid">{stats}</div>

<h2>Последний обмен</h2>
<div class="panel">{last_html}</div>

<h2>Каталог</h2>
<div class="panel">
  <p style="margin:0 0 10px">Товары, которые приехали обменом: названия, артикулы,
  цены и остатки по складам. Поиск по названию и артикулу.</p>
  <a href="/catalog">Открыть каталог →</a>
</div>

<h2>Сверка</h2>
<div class="panel">
  <p style="margin:0 0 10px">Отвечает на вопрос, которого нет в сообщении «обмен прошёл
  успешно»: что перестало приходить из 1С, у чего нет цены, остатка или фотографий,
  где один артикул у разных позиций, какие предложения остались без товара.</p>
  <a href="/report">Открыть отчёт →</a>
</div>

<nav>
  <a href="/catalog">Каталог</a>
  <a href="https://github.com/mvpcodego/cml-bridge">Исходный код и разбор граблей обмена</a>
  <a href="/report.txt">Отчёт текстом</a>
</nav>

<footer>Павел Чертинов · <a href="https://mvp-code.ru">mvp-code.ru</a></footer>
"""
    return _page("Мост 1С", body)


def report(rep) -> str:
    t = rep.totals
    head = (
        f'<div class="panel"><table class="rows">'
        f'<tr><td>Групп</td><td>{t.get("groups", 0)}</td></tr>'
        f'<tr><td>Товаров</td><td>{t.get("products", 0)}</td></tr>'
        f'<tr><td>Предложений</td><td>{t.get("offers", 0)}</td></tr>'
        f'<tr><td>Записей об остатках</td><td>{t.get("stock", 0)}</td></tr>'
        f'<tr><td>Обменов</td><td>{t.get("sessions", 0)}</td></tr>'
        f'</table></div>'
    )

    if not rep.findings:
        blocks = ('<div class="ok-box"><h3>Расхождений не найдено</h3>'
                  '<p class="muted">Каталог целостный: у всех позиций есть цена, остаток '
                  'и связанные предложения.</p></div>')
    else:
        parts = []
        for f in rep.findings:
            items = "".join(f"<li>{html.escape(s)}</li>" for s in f.samples)
            more = ""
            if f.count > len(f.samples):
                more = f'<li class="muted">…и ещё {f.count - len(f.samples)}</li>'
            parts.append(
                f'<div class="finding"><h3>{html.escape(f.title)} '
                f'<span class="num">· {f.count}</span></h3>'
                f'<ul>{items}{more}</ul>'
                f'<p>{html.escape(f.hint)}</p></div>'
            )
        blocks = "".join(parts)

    body = f"""
<header>
  <h1>Сверка каталога</h1>
  <p class="lead">Найдено расхождений: <b>{rep.problems}</b>.
  Проверено восемь вещей, каждая из которых в работе оборачивалась потерянными деньгами.</p>
</header>

<h2>В базе</h2>
{head}

<h2>Находки</h2>
{blocks}

<nav><a href="/">← На главную</a><a href="/catalog">Каталог</a><a href="/report.txt">Тот же отчёт текстом</a></nav>
<footer>Павел Чертинов · <a href="https://mvp-code.ru">mvp-code.ru</a></footer>
"""
    return _page("Сверка каталога", body)


def _money(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} ₽".replace(",", " ")


def _qty(value) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f}".replace(",", " ")


def catalog(rows, total: int, query: str, offset: int, limit: int) -> str:
    """Список товаров: то, что реально приехало из 1С."""
    if rows:
        items = []
        for r in rows:
            qty = r["qty_total"]
            qty_cls = ' class="qty zero"' if (qty or 0) <= 0 else ' class="qty"'
            art = f'<span class="art">{html.escape(r["article"])}</span>' if r["article"] else ""
            items.append(
                f'<div class="item">'
                f'<div><a href="/catalog/{html.escape(r["ident"])}">{html.escape(r["name"] or r["ident"])}</a>{art}</div>'
                f'<span class="price">{_money(r["price_min"])}</span>'
                f'<span{qty_cls}>{_qty(qty)} шт</span>'
                f'</div>'
            )
        body_items = f'<div class="items">{"".join(items)}</div>'
    else:
        body_items = '<div class="panel"><p class="muted" style="margin:0">Ничего не найдено.</p></div>'

    pager = []
    q = f"&q={urllib.parse.quote(query)}" if query else ""
    if offset > 0:
        pager.append(f'<a href="/catalog?offset={max(0, offset - limit)}{q}">← назад</a>')
    if offset + limit < total:
        pager.append(f'<a href="/catalog?offset={offset + limit}{q}">вперёд →</a>')
    shown = f"{offset + 1}–{min(offset + limit, total)} из {total}" if total else "0"
    pager.append(f'<span class="muted">{shown}</span>')

    body = f"""
<header>
  <h1>Каталог из 1С</h1>
  <p class="lead">То, что реально приехало обменом: названия, артикулы, минимальная цена
  и суммарный остаток по складам. Нулевой остаток подсвечен — именно такие позиции
  обычно и висят на сайте как живые.</p>
</header>

<form class="search" method="get" action="/catalog">
  <input type="search" name="q" value="{html.escape(query)}" placeholder="Название или артикул" autofocus>
  <button type="submit">Найти</button>
</form>

{body_items}
<div class="pager">{" ".join(pager)}</div>

<nav><a href="/">← На главную</a><a href="/report">Сверка каталога</a></nav>
<footer>Павел Чертинов · <a href="https://mvp-code.ru">mvp-code.ru</a></footer>
"""
    return _page("Каталог из 1С", body)


def product(row, offers, stock, group_names) -> str:
    """Карточка позиции: свойства из 1С, предложения, остатки по складам."""
    by_offer: dict[str, list] = {}
    for s in stock:
        by_offer.setdefault(s["offer_ident"], []).append(s)

    if offers:
        parts = []
        for o in offers:
            feats = ""
            if o["features"]:
                chips = "".join(
                    f'<span class="tag">{html.escape(line)}</span>'
                    for line in o["features"].split("\n") if line.strip()
                )
                feats = f'<div class="tags">{chips}</div>'
            wh = by_offer.get(o["ident"], [])
            wh_html = "".join(
                f'<tr><td class="muted">склад {html.escape(w["warehouse"][:8])}…</td>'
                f'<td>{_qty(w["quantity"])} шт</td></tr>'
                for w in wh
            )
            parts.append(
                f'<div class="panel">'
                f'<b>{html.escape(o["name"] or o["ident"])}</b>{feats}'
                f'<table class="rows" style="margin-top:10px">'
                f'<tr><td>Цена</td><td>{_money(o["price"])} {html.escape(o["currency"] or "")}</td></tr>'
                f'<tr><td>Остаток всего</td><td>{_qty(o["quantity"])} шт</td></tr>'
                f'{wh_html}</table></div>'
            )
        offers_html = "".join(parts)
    else:
        offers_html = ('<div class="panel"><p class="muted" style="margin:0">'
                       'Предложений нет — значит не приехали цена и остаток, '
                       'продавать эту позицию нечем.</p></div>')

    props_rows = ""
    if row["props"]:
        for line in row["props"].split("\n"):
            if "=" in line:
                k, _, v = line.partition("=")
                props_rows += f'<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>'
    props_html = (f'<div class="panel"><table class="rows">{props_rows}</table></div>'
                  if props_rows else "")

    images = [i for i in (row["images"] or "").split("\n") if i.strip()]
    img_html = ""
    if images:
        chips = "".join(f'<span class="tag">{html.escape(i)}</span>' for i in images)
        img_html = f'<h2>Файлы изображений</h2><div class="panel"><div class="tags">{chips}</div></div>'

    groups_html = ""
    if group_names:
        chips = "".join(f'<span class="tag">{html.escape(g)}</span>' for g in group_names)
        groups_html = f'<div class="tags" style="margin-top:10px">{chips}</div>'

    body = f"""
<header>
  <h1>{html.escape(row["name"] or row["ident"])}</h1>
  <p class="lead mono">{html.escape(row["article"] or "без артикула")}</p>
  {groups_html}
</header>

<h2>Предложения и остатки</h2>
{offers_html}

{"<h2>Свойства из 1С</h2>" + props_html if props_html else ""}
{img_html}

<nav><a href="/catalog">← К каталогу</a><a href="/report">Сверка</a></nav>
<footer>Павел Чертинов · <a href="https://mvp-code.ru">mvp-code.ru</a></footer>
"""
    return _page(row["name"] or "Позиция", body)
