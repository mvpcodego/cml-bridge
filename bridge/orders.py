"""Выгрузка заказов обратно в 1С (CommerceML, windows-1251).

1С забирает заказы с сайта запросом `?type=sale&mode=query` и ждёт XML
ровно той же схемы CommerceML — то есть документы «Заказ товаров».

Две вещи, на которых ломается обратный поток:

1. **Кодировка.** Заказы 1С читает в windows-1251. Символы, которых
   в 1251 нет (например, «₽» или эмодзи из комментария покупателя),
   надо заменять до записи, иначе выгрузка обрывается на середине.
2. **Повторная выгрузка.** Если после `query` не пришёл `success`, 1С
   попросит заказы снова. Поэтому отметка «выгружено» ставится только
   после подтверждения, а сам номер заказа остаётся уникальным ключом —
   повторный приём того же номера не создаёт второй заказ.
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from xml.dom import minidom

ENCODING = "windows-1251"


def add_order(store, number: str, total: float, items: list[dict], props: dict | None = None) -> bool:
    """Положить заказ в очередь на выгрузку. Возвращает False, если такой
    номер уже есть — это и есть защита от дублей при повторной отправке."""
    exists = store.conn.execute(
        "SELECT 1 FROM orders_out WHERE number=?", (number,)
    ).fetchone()
    if exists:
        return False
    payload = json.dumps({"items": items, "props": props or {}}, ensure_ascii=False)
    store.conn.execute(
        "INSERT INTO orders_out (number, created_at, total, payload) VALUES (?,?,?,?)",
        (number, time.time(), total, payload),
    )
    store.conn.commit()
    return True


def _safe(text: str) -> str:
    """Убрать то, что не кодируется в windows-1251, сохранив читаемость."""
    replacements = {"₽": "руб.", "—": "-", "–": "-", "«": '"', "»": '"', " ": " "}
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text.encode(ENCODING, "replace").decode(ENCODING)


def export_orders(store) -> bytes:
    """Собрать XML со всеми невыгруженными заказами."""
    rows = store.conn.execute(
        "SELECT number, created_at, total, payload FROM orders_out WHERE exported=0 ORDER BY created_at"
    ).fetchall()

    root = ET.Element("КоммерческаяИнформация", {
        "ВерсияСхемы": "2.05",
        "ДатаФормирования": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })

    for row in rows:
        data = json.loads(row["payload"])
        doc = ET.SubElement(root, "Документ")
        ET.SubElement(doc, "Ид").text = _safe(row["number"])
        ET.SubElement(doc, "Номер").text = _safe(row["number"])
        ET.SubElement(doc, "Дата").text = time.strftime("%Y-%m-%d", time.localtime(row["created_at"]))
        ET.SubElement(doc, "Время").text = time.strftime("%H:%M:%S", time.localtime(row["created_at"]))
        ET.SubElement(doc, "ХозОперация").text = "Заказ товара"
        ET.SubElement(doc, "Роль").text = "Продавец"
        ET.SubElement(doc, "Валюта").text = "руб"
        ET.SubElement(doc, "Курс").text = "1"
        ET.SubElement(doc, "Сумма").text = f"{row['total']:.2f}"

        goods = ET.SubElement(doc, "Товары")
        for item in data.get("items", []):
            g = ET.SubElement(goods, "Товар")
            ET.SubElement(g, "Ид").text = _safe(str(item.get("ident", "")))
            ET.SubElement(g, "Наименование").text = _safe(str(item.get("name", "")))
            base = ET.SubElement(g, "БазоваяЕдиница", {
                "Код": "796", "НаименованиеПолное": "Штука", "МеждународноеСокращение": "PCE",
            })
            base.text = "шт"
            ET.SubElement(g, "ЦенаЗаЕдиницу").text = f"{float(item.get('price', 0)):.2f}"
            ET.SubElement(g, "Количество").text = str(item.get("quantity", 0))
            ET.SubElement(g, "Сумма").text = (
                f"{float(item.get('price', 0)) * float(item.get('quantity', 0)):.2f}"
            )

        props = data.get("props") or {}
        if props:
            block = ET.SubElement(doc, "ЗначенияРеквизитов")
            for key, value in props.items():
                node = ET.SubElement(block, "ЗначениеРеквизита")
                ET.SubElement(node, "Наименование").text = _safe(str(key))
                ET.SubElement(node, "Значение").text = _safe(str(value))

    xml = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(xml).toprettyxml(indent="  ")
    # Декларация должна объявлять ту кодировку, в которой файл реально отдан.
    pretty = pretty.replace('<?xml version="1.0" ?>', f'<?xml version="1.0" encoding="{ENCODING}"?>', 1)
    return pretty.encode(ENCODING, "replace")
