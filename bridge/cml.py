"""Разбор файлов обмена CommerceML 2 (то, что выгружает 1С для сайта).

Три вещи, на которых ломаются самописные обмены, и которые здесь учтены:

1. Кодировка. 1С выгружает заказы в windows-1251, а каталог обычно в UTF-8.
   Кодировку нельзя предполагать — её надо читать из XML-декларации.
2. BOM. UTF-8 с BOM ломает парсер, если файл открыт как текст: первый символ
   становится невидимым мусором перед `<?xml`.
3. Имена узлов. Все теги на русском, и у разных версий 1С они отличаются
   («Склад» против «Склады», «Цены» против «ЦенаЗаНомПоз»), поэтому поиск
   идёт по нескольким вариантам, а не по одному жёсткому пути.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

# 1С называет кодировку по-разному; приводим к тому, что понимает Python
_ENC_ALIASES = {
    "windows-1251": "cp1251",
    "cp1251": "cp1251",
    "utf-8": "utf-8",
    "utf8": "utf-8",
}


def detect_encoding(raw: bytes) -> str:
    """Кодировка файла: из XML-декларации, иначе по BOM, иначе UTF-8."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    head = raw[:200].decode("latin-1", "ignore")
    m = re.search(r'encoding\s*=\s*"([^"]+)"', head)
    if m:
        return _ENC_ALIASES.get(m.group(1).strip().lower(), m.group(1))
    return "utf-8"


def strip_namespaces(root: ET.Element) -> ET.Element:
    """Снять пространство имён со всех узлов.

    Четвёртая грабля, и самая тихая: в схеме 2.07 корень объявляет
    xmlns="urn:1C.ru:commerceml_2", а в более старых выгрузках его нет.
    С ним имя тега становится "{urn:1C.ru:commerceml_2}Товар", и поиск по
    "Товар" возвращает пусто — БЕЗ ошибки. Обмен отчитывается об успехе,
    товаров ноль. Поэтому имена нормализуем сразу после разбора.
    """
    for node in root.iter():
        if isinstance(node.tag, str) and node.tag.startswith("{"):
            node.tag = node.tag.rpartition("}")[2]
        for key in list(node.attrib):
            if key.startswith("{"):
                node.attrib[key.rpartition("}")[2]] = node.attrib.pop(key)
    return root


def parse_bytes(raw: bytes) -> ET.Element:
    """Корневой узел документа, независимо от кодировки, BOM и namespace."""
    enc = detect_encoding(raw)
    text = raw.decode(enc, "replace")
    # Декларация могла объявлять другую кодировку, чем та, в которой мы уже
    # получили строку — убираем её, иначе ElementTree попытается перекодировать.
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1).lstrip("﻿").lstrip()
    return strip_namespaces(ET.fromstring(text))


def parse_file(path: str) -> ET.Element:
    with open(path, "rb") as fh:
        return parse_bytes(fh.read())


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _first(node: ET.Element, *names: str) -> ET.Element | None:
    """Первый дочерний узел с одним из имён (1С меняет имена между версиями)."""
    for name in names:
        found = node.find(name)
        if found is not None:
            return found
    return None


def _iter_deep(root: ET.Element, name: str):
    """Все узлы с таким именем на любой глубине."""
    return root.iter(name)


@dataclass
class Group:
    ident: str
    name: str
    parent: str | None = None


@dataclass
class Product:
    ident: str
    name: str
    article: str = ""
    groups: list[str] = field(default_factory=list)
    props: dict[str, str] = field(default_factory=dict)
    images: list[str] = field(default_factory=list)
    unit: str = ""
    deleted: bool = False


@dataclass
class Offer:
    ident: str
    product_ident: str
    name: str = ""
    article: str = ""
    price: float | None = None
    currency: str = ""
    quantity: float | None = None
    warehouses: dict[str, float] = field(default_factory=dict)
    # Характеристики («Размер: 40», «Цвет: Коричневый») — то, чем предложения
    # одного товара отличаются друг от друга.
    features: dict[str, str] = field(default_factory=dict)
    variant_ident: str = ""


@dataclass
class OrderItem:
    product_ident: str
    name: str
    quantity: float
    price: float


@dataclass
class Order:
    number: str
    date: str
    total: float
    items: list[OrderItem] = field(default_factory=list)
    props: dict[str, str] = field(default_factory=dict)


def _split_ident(raw: str) -> tuple[str, str]:
    """Ид предложения вида '<товар>#<характеристика>' → (товар, характеристика)."""
    if "#" in raw:
        product, _, variant = raw.partition("#")
        return product, variant
    return raw, ""


def parse_groups(root: ET.Element) -> list[Group]:
    """Дерево групп каталога. Группы вложенные, разбираем рекурсивно."""
    out: list[Group] = []

    def walk(container: ET.Element, parent: str | None) -> None:
        for node in container.findall("Группа"):
            ident = _text(_first(node, "Ид"))
            if not ident:
                continue
            out.append(Group(ident, _text(_first(node, "Наименование")), parent))
            nested = _first(node, "Группы")
            if nested is not None:
                walk(nested, ident)

    for groups in _iter_deep(root, "Группы"):
        # верхний уровень обрабатываем один раз: вложенные заберёт walk
        if any(p is groups for p in root.iter("Группа")):
            continue
        walk(groups, None)
    # убираем дубли, сохраняя порядок
    seen: set[str] = set()
    unique: list[Group] = []
    for g in out:
        if g.ident in seen:
            continue
        seen.add(g.ident)
        unique.append(g)
    return unique


def parse_products(root: ET.Element) -> list[Product]:
    out: list[Product] = []
    for node in _iter_deep(root, "Товар"):
        ident_raw = _text(_first(node, "Ид"))
        if not ident_raw:
            continue
        product_ident, _ = _split_ident(ident_raw)
        prod = Product(
            ident=product_ident,
            name=_text(_first(node, "Наименование")),
            article=_text(_first(node, "Артикул")),
            unit=_text(_first(node, "БазоваяЕдиница")),
        )
        groups = _first(node, "Группы")
        if groups is not None:
            prod.groups = [t.strip() for t in (g.text or "" for g in groups.findall("Ид")) if t.strip()]
        for img in node.findall("Картинка"):
            if (img.text or "").strip():
                prod.images.append(img.text.strip())
        values = _first(node, "ЗначенияСвойств")
        if values is not None:
            for v in values.findall("ЗначенияСвойства"):
                key = _text(_first(v, "Ид"))
                val = _text(_first(v, "Значение"))
                if key:
                    prod.props[key] = val
        req = _first(node, "ЗначенияРеквизитов")
        if req is not None:
            for v in req.findall("ЗначениеРеквизита"):
                name = _text(_first(v, "Наименование"))
                val = _text(_first(v, "Значение"))
                if name:
                    prod.props[name] = val
                # 1С помечает удаление реквизитом, а не отдельным признаком
                if name == "ПометкаУдаления" and val.lower() in ("true", "истина", "1"):
                    prod.deleted = True
        out.append(prod)
    return out


def _to_float(raw: str) -> float | None:
    raw = raw.replace(",", ".").replace(" ", "").replace("\xa0", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_offers(root: ET.Element) -> list[Offer]:
    out: list[Offer] = []
    for node in _iter_deep(root, "Предложение"):
        ident_raw = _text(_first(node, "Ид"))
        if not ident_raw:
            continue
        product_ident, variant = _split_ident(ident_raw)
        offer = Offer(
            ident=ident_raw,
            product_ident=product_ident,
            name=_text(_first(node, "Наименование")),
            article=_text(_first(node, "Артикул")),
            variant_ident=variant,
        )
        prices = _first(node, "Цены")
        if prices is not None:
            first_price = prices.find("Цена")
            if first_price is not None:
                offer.price = _to_float(_text(_first(first_price, "ЦенаЗаЕдиницу", "Цена")))
                offer.currency = _text(_first(first_price, "Валюта"))
        offer.quantity = _to_float(_text(_first(node, "Количество")))
        # Характеристики предложения: именно они отличают «(38)» от «(40)».
        feats = _first(node, "ХарактеристикиТовара")
        if feats is not None:
            for f in feats.findall("ХарактеристикаТовара"):
                key = _text(_first(f, "Наименование"))
                if key:
                    offer.features[key] = _text(_first(f, "Значение"))
        # Остатки по складам. Здесь две ловушки сразу:
        # 1. Узлы «Склад» лежат либо прямо в предложении, либо в «Склады»,
        #    причём «Склады» на верхнем уровне файла — это СПРАВОЧНИК складов
        #    (с адресом и контактами), а не остатки; его брать нельзя.
        # 2. Атрибут с количеством называется «КоличествоНаСкладе», а не
        #    «Количество». Парсер, ищущий «Количество», молча вернёт пустые
        #    остатки — ошибки не будет, товар просто окажется «не в наличии».
        nodes = list(node.findall("Склад"))
        inner = _first(node, "Склады")
        if inner is not None:
            nodes += inner.findall("Склад")
        for wh in nodes:
            wid = wh.get("ИдСклада") or _text(_first(wh, "Ид"))
            qty = _to_float(
                wh.get("КоличествоНаСкладе")
                or wh.get("Количество")
                or _text(_first(wh, "Количество"))
            )
            if wid and qty is not None:
                offer.warehouses[wid] = offer.warehouses.get(wid, 0.0) + qty
        if offer.quantity is None and offer.warehouses:
            offer.quantity = sum(offer.warehouses.values())
        out.append(offer)
    return out


def parse_orders(root: ET.Element) -> list[Order]:
    out: list[Order] = []
    for node in _iter_deep(root, "Документ"):
        number = _text(_first(node, "Номер"))
        order = Order(
            number=number,
            date=_text(_first(node, "Дата")),
            total=_to_float(_text(_first(node, "Сумма"))) or 0.0,
        )
        items = _first(node, "Товары")
        if items is not None:
            for it in items.findall("Товар"):
                ident_raw = _text(_first(it, "Ид"))
                product_ident, _ = _split_ident(ident_raw)
                order.items.append(
                    OrderItem(
                        product_ident=product_ident,
                        name=_text(_first(it, "Наименование")),
                        quantity=_to_float(_text(_first(it, "Количество"))) or 0.0,
                        price=_to_float(_text(_first(it, "ЦенаЗаЕдиницу", "Цена"))) or 0.0,
                    )
                )
        req = _first(node, "ЗначенияРеквизитов")
        if req is not None:
            for v in req.findall("ЗначениеРеквизита"):
                name = _text(_first(v, "Наименование"))
                if name:
                    order.props[name] = _text(_first(v, "Значение"))
        out.append(order)
    return out
