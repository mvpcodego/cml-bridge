"""Сверка каталога: отчёт, который умеет сказать, чего в нём НЕ хватает.

Это ядро всей затеи. Обмен, который просто «прошёл успешно», ничего не
доказывает: инкрементальная выгрузка структурно не видит архивации
и удаления, поэтому товар может исчезнуть из 1С и навсегда остаться
висеть на сайте — либо наоборот, пропасть с витрины при живом обмене.

Отчёт отвечает на восемь вопросов, каждый из которых в реальной работе
оборачивался потерянными деньгами:

  1. Что перестало приходить из 1С (пропало между обменами).
  2. Что помечено на удаление прямо в выгрузке.
  3. У чего нет цены — такой товар нельзя продавать.
  4. У чего нет остатка ни на одном складе.
  5. У чего нет ни одной картинки.
  6. Дубли артикулов — один артикул у разных позиций.
  7. Предложения-сироты: цена есть, товара нет.
  8. Товары без предложений: товар есть, продать нечего.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Finding:
    code: str
    title: str
    count: int
    samples: list[str] = field(default_factory=list)
    hint: str = ""


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)

    @property
    def problems(self) -> int:
        return sum(f.count for f in self.findings)


def _samples(rows, limit: int = 5) -> list[str]:
    out = []
    for r in rows[:limit]:
        name = (r["name"] or "").strip() or r["ident"]
        art = (r["article"] or "").strip()
        out.append(f"{name}" + (f" [{art}]" if art else ""))
    return out


def build(store, stale_after_sessions: int = 1) -> Report:
    """Собрать отчёт. `stale_after_sessions` — сколько последних обменов
    считать актуальными: позиция, не приходившая дольше, считается пропавшей."""
    conn = store.conn
    rep = Report()

    last = conn.execute("SELECT id FROM sessions WHERE kind='catalog' ORDER BY id DESC LIMIT 1").fetchone()
    last_id = last["id"] if last else 0
    threshold = max(0, last_id - stale_after_sessions + 1)

    rep.totals = store.counts()

    # 1. Перестало приходить из 1С.
    rows = conn.execute(
        "SELECT ident,name,article FROM products WHERE session_id < ? ORDER BY name", (threshold,)
    ).fetchall()
    if rows:
        rep.findings.append(
            Finding(
                "vanished",
                "Перестали приходить из 1С (возможна архивация или удаление)",
                len(rows),
                _samples(rows),
                "Инкрементальный обмен не сообщает об архивации: позиция просто "
                "исчезает из выгрузки. Без этой проверки она навсегда останется "
                "на сайте как живая.",
            )
        )

    # 2. Помечено на удаление в самой выгрузке.
    rows = conn.execute(
        "SELECT ident,name,article FROM products WHERE deleted=1 ORDER BY name"
    ).fetchall()
    if rows:
        rep.findings.append(
            Finding("marked_deleted", "Помечены на удаление в выгрузке", len(rows), _samples(rows),
                    "1С передаёт это реквизитом «ПометкаУдаления», а не отдельным полем.")
        )

    # 3. Без цены.
    rows = conn.execute(
        "SELECT o.ident, o.name, o.article FROM offers o "
        "WHERE o.price IS NULL OR o.price = 0 ORDER BY o.name"
    ).fetchall()
    if rows:
        rep.findings.append(
            Finding("no_price", "Предложения без цены", len(rows), _samples(rows),
                    "Такую позицию нельзя показывать в продаже: клиент увидит 0 ₽ "
                    "или пустое место.")
        )

    # 4. Без остатка ни на одном складе.
    rows = conn.execute(
        "SELECT o.ident, o.name, o.article FROM offers o "
        "LEFT JOIN (SELECT offer_ident, SUM(quantity) q FROM stock GROUP BY offer_ident) s "
        "  ON s.offer_ident = o.ident "
        "WHERE COALESCE(s.q, o.quantity, 0) <= 0 ORDER BY o.name"
    ).fetchall()
    if rows:
        rep.findings.append(
            Finding("no_stock", "Предложения без остатка", len(rows), _samples(rows),
                    "Отдельно проверьте комплекты: у них остаток в учётной системе "
                    "всегда нулевой, потому что комплект собирается под заказ — "
                    "судить о наличии надо по главной части, а не по комплекту.")
        )

    # 5. Без картинок.
    rows = conn.execute(
        "SELECT ident,name,article FROM products WHERE images='' ORDER BY name"
    ).fetchall()
    if rows:
        rep.findings.append(
            Finding("no_image", "Товары без фотографий", len(rows), _samples(rows),
                    "Выгрузка ссылается на файлы из архива обмена; если картинки "
                    "не доехали, товар выглядит пустым.")
        )

    # 6. Дубли артикулов.
    rows = conn.execute(
        "SELECT article_key, COUNT(*) c, GROUP_CONCAT(name, ' | ') names FROM products "
        "WHERE article_key <> '' GROUP BY article_key HAVING c > 1 ORDER BY c DESC"
    ).fetchall()
    if rows:
        rep.findings.append(
            Finding(
                "dup_article",
                "Один артикул у разных позиций",
                len(rows),
                [f"{r['article_key']} × {r['c']}: {(r['names'] or '')[:70]}" for r in rows[:5]],
                "Сопоставление по артикулу в такой ситуации молча склеит разные "
                "товары. Ключ нормализован (пробелы и тире), поэтому «Б- 130005» "
                "и «Б-130005» здесь видны как один артикул.",
            )
        )

    # 7. Предложения-сироты.
    rows = conn.execute(
        "SELECT o.ident, o.name, o.article FROM offers o "
        "LEFT JOIN products p ON p.ident = o.product_ident WHERE p.ident IS NULL"
    ).fetchall()
    if rows:
        rep.findings.append(
            Finding("orphan_offer", "Предложения без товара", len(rows), _samples(rows),
                    "Цена и остаток пришли, а карточки товара нет. Обычно значит, "
                    "что offers.xml приняли, а import.xml — нет.")
        )

    # 8. Товары без предложений.
    rows = conn.execute(
        "SELECT p.ident, p.name, p.article FROM products p "
        "LEFT JOIN offers o ON o.product_ident = p.ident WHERE o.ident IS NULL ORDER BY p.name"
    ).fetchall()
    if rows:
        rep.findings.append(
            Finding("no_offer", "Товары без предложений (нет цены и остатка)", len(rows), _samples(rows),
                    "Карточка есть, продавать нечего. Частый случай при обмене, "
                    "где import.xml приходит чаще offers.xml.")
        )

    return rep


def render_text(rep: Report) -> str:
    """Человекочитаемый отчёт: сначала итог, потом находки с примерами."""
    lines = []
    t = rep.totals
    lines.append("СВЕРКА КАТАЛОГА")
    lines.append(
        f"В базе: групп {t.get('groups', 0)}, товаров {t.get('products', 0)}, "
        f"предложений {t.get('offers', 0)}, записей об остатках {t.get('stock', 0)}, "
        f"обменов {t.get('sessions', 0)}"
    )
    if not rep.findings:
        lines.append("Расхождений не найдено.")
        return "\n".join(lines)
    lines.append(f"Найдено расхождений: {rep.problems}")
    for f in rep.findings:
        lines.append("")
        lines.append(f"— {f.title}: {f.count}")
        for s in f.samples:
            lines.append(f"    · {s}")
        if f.hint:
            lines.append(f"    Почему важно: {f.hint}")
    return "\n".join(lines)
