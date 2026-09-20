"""Страницы для человека: главная и отчёт сверки.

1С читает только текстовые ответы точки обмена; всё, что здесь — для людей,
которые откроют адрес в браузере: заказчика, его подрядчика, меня самого.
Поэтому здесь обычный HTML в UTF-8, без библиотек и без сборки.
"""

from __future__ import annotations

import html
import time

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

<h2>Сверка</h2>
<div class="panel">
  <p style="margin:0 0 10px">Отвечает на вопрос, которого нет в сообщении «обмен прошёл
  успешно»: что перестало приходить из 1С, у чего нет цены, остатка или фотографий,
  где один артикул у разных позиций, какие предложения остались без товара.</p>
  <a href="/report">Открыть отчёт →</a>
</div>

<nav>
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

<nav><a href="/">← На главную</a><a href="/report.txt">Тот же отчёт текстом</a></nav>
<footer>Павел Чертинов · <a href="https://mvp-code.ru">mvp-code.ru</a></footer>
"""
    return _page("Сверка каталога", body)
