"""Общий движок скана: им пользуются и консольный scan.py, и приложение app.py.

Разделение по стоимости операций:
  * refresh_prices()   — сеть, минуты, лимиты API. Делается редко.
  * compute()          — чистый расчёт по кэшу, доли секунды. Делается на каждый
                         чих пользователя (сменил бюджет, включил фокус).
Поэтому бюджет НЕ участвует в расчёте себестоимости — он только фильтрует
готовые строки и считает размер партии. Иначе смена ползунка тянула бы сеть.
"""

from __future__ import annotations

import json
import os
import re

from . import craft
from .prices import Prices, BLACK_MARKET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# Насколько дешевле средней сделки должен быть ордер, чтобы счесть его «тонким»
THIN_ORDER_RATIO = 0.6


def load_config() -> dict:
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        return json.load(fh)


def load_recipes(cfg: dict) -> dict:
    with open(os.path.join(DATA, "recipes.json"), encoding="utf-8") as fh:
        recipes = json.load(fh)

    f = cfg["filters"]
    tiers, enchants, sections = set(f["tiers"]), set(f["enchants"]), set(f["sections"])
    return {
        k: v for k, v in recipes.items()
        if v["tier"] in tiers and v["enchant"] in enchants and v["section"] in sections
        and _trusted(k, v)
    }


# Из простых предметов считаем только переработку — сырьё в материалы.
REFINED = ("_METALBAR", "_PLANKS", "_LEATHER", "_CLOTH", "_STONEBLOCK")


# Технические/квестовые/дев-предметы, просочившиеся в дамп игры:
#   PROTOTYPE — неизданные варианты снаряжения, недоступные игрокам;
#   _TEST     — буквально тестовые записи разработчиков;
#   UNIQUE_   — уникальные квестовые/ивентовые объекты, не крафтятся массово;
#   QUESTITEM_ — квестовые предметы (например, тюки каравана). У них может
#                НАЙТИСЬ обычный торгуемый ресурс в рецепте (мы проверяли на
#                тюке каравана — там простой фракционный токен), поэтому
#                расчёт по цене их не отсеет сам: продать такое на рынке
#                нельзя в принципе, это не связано с ценой вообще.
_UNTRADEABLE = re.compile(r"PROTOTYPE|_TEST\b|^UNIQUE_|^QUESTITEM_", re.IGNORECASE)


def _trusted(item_id: str, rec: dict) -> bool:
    """Отсеять рецепты, которым нельзя верить.

    Два независимых источника мусора в игровом дампе:
    1. Артефакты «крафтятся» из 50 рун — маржа в тысячи процентов, в игре
       так не работает, иначе руны давно стоили бы дороже. Модель неполная,
       такие позиции убираем, чтобы не выдавать их за заработок.
    2. Дев-предметы и квестовый инвентарь (см. _UNTRADEABLE) — они физически
       не продаются на рынках, независимо от того, нашлась ли для них цена.
    """
    base = item_id.split("@")[0]
    if _UNTRADEABLE.search(base):
        return False
    if rec["section"] != "simpleitem":
        return True
    return base.endswith(REFINED)


def collect_ids(recipes: dict) -> tuple[list, list]:
    items = list(recipes.keys())
    resources = set()
    for rec in recipes.values():
        for variant in rec["variants"]:
            for res in variant["resources"]:
                resources.add(res["id"])
    return items, sorted(resources)


def build_flip_recipes(recipes: dict) -> dict:
    """Готовые вещи как флип: купить дешёво в городе, продать на Чёрном рынке.

    Не крафт — item_id ставится единственным «материалом» самого рецепта,
    без возврата. Это позволяет прогнать флиппинг через тот же craft.evaluate()
    и получить бесплатно всю уже проверенную защиту: сверку цены продажи
    со средними сделками и отсев «тонких» ордеров закупки — их для флипа
    даже важнее, чем для крафта, потому что тут вся стоимость лежит в одной
    позиции, а не размазана по нескольким материалам.

    Сборные предметы вроде зачарованных материалов сюда не идут — флипуются
    только сами рецепты верхнего уровня (recipes.keys()), ровно то, что видно
    в таблице крафта.
    """
    out = {}
    for item_id, rec in recipes.items():
        out[item_id] = {
            "id": item_id,
            "name": rec["name"],
            "tier": rec["tier"],
            "enchant": rec["enchant"],
            "variants": [{
                "kind": "flip",
                "silver": 0.0,
                "focus": 0.0,
                "resources": [{"id": item_id, "count": 1, "returnable": False}],
            }],
        }
    return out


def refresh_prices(cfg: dict, recipes: dict, prices: Prices, log=print) -> None:
    items, resources = collect_ids(recipes)
    log(f"цены ресурсов по городам ({len(resources)} шт)")
    prices.fetch(resources, locations=cfg["buy_cities"], progress=False)
    # Готовые вещи нужны и на Чёрном рынке, и в самих городах — второе даёт
    # режим «продать на месте», без поездки в Кэрлеон.
    log(f"цены предметов, рынок и города ({len(items)} шт)")
    prices.fetch(items, locations=[cfg["sell_location"]] + cfg["buy_cities"],
                 progress=False)
    prices.save()


def compute(cfg: dict, recipes: dict, prices: Prices, focus: bool,
            sell_at: str | None = None) -> list:
    """Себестоимость и выгода по всем рецептам. Без сети.

    sell_at="same" — режим торговли на месте: покупка и продажа в одном городе.
    """
    f = cfg["filters"]
    out = []
    for rec in recipes.values():
        res = craft.evaluate(rec, prices, cfg, use_focus=focus, sell_at=sell_at)
        if res and res.profit >= f["min_profit_silver"] and res.margin_pct >= f["min_margin_pct"]:
            out.append(res)
    out.sort(key=lambda r: r.profit, reverse=True)
    return out


def pick_candidates(rows: list, budget: float, limit: int = 350) -> list:
    """Кого вообще проверять на объёмы (это сетевые запросы, они не бесплатны).

    Отбирать просто по абсолютной прибыли нельзя: топ забивают вещи
    с себестоимостью в миллионы, которые вытесняют дешёвые массовые позиции —
    именно те, что нужны при ограниченном капитале. Поэтому берём объединение
    лучших по прибыли за штуку и лучших по отдаче на вложенный серебряный,
    причём сначала отсекаем всё, что заведомо не по карману.
    """
    affordable = [r for r in rows if 0 < r.cost <= budget]
    if not affordable:
        return []

    by_profit = sorted(affordable, key=lambda r: r.profit, reverse=True)
    by_roi = sorted(affordable, key=lambda r: r.profit / r.cost, reverse=True)

    seen, out = set(), []
    for r in by_profit[:limit] + by_roi[:limit]:
        if r.item_id not in seen:
            seen.add(r.item_id)
            out.append(r)
    return out


def enrich_liquidity(rows: list, prices: Prices, cfg: dict, budget: float,
                     limit: int = 350, log=print) -> list:
    """Спрос Чёрного рынка + реальная глубина закупки материалов."""
    head = pick_candidates(rows, budget, limit)
    if not head:
        return []

    # Спрос смотрим там, где реально продаём: на Чёрном рынке или в самом городе
    log("спрос в точке сбыта")
    by_sell: dict[str, list] = {}
    for r in head:
        by_sell.setdefault(r.sell_city or BLACK_MARKET, []).append(r)

    sell_avg: dict[str, float] = {}
    for location, group in by_sell.items():
        hist = prices.history_cached([r.item_id for r in group], location=location)
        for r in group:
            info = hist.get(r.item_id, {})
            r.sold_per_day = info.get("per_day", 0.0)
            r.trend = info.get("series", [])
            sell_avg[r.item_id] = info.get("avg_price", 0)

    # После сверки цен с историей часть позиций теряет смысл — пересеиваем
    f = cfg["filters"]
    head = [
        r for r in head
        if r.sold_per_day >= f["min_sold_per_day"]
        and r.profit >= f["min_profit_silver"]
        and r.margin_pct >= f["min_margin_pct"]
    ]
    if not head:
        return []
    head.sort(key=lambda r: r.profit, reverse=True)

    log("глубина закупки материалов")
    by_city: dict[str, set] = {}
    for r in head:
        by_city.setdefault(r.buy_city, set()).update(m["id"] for m in r.recipe)

    supply: dict[tuple, float] = {}
    for city, mats in by_city.items():
        for mat_id, info in prices.history_cached(sorted(mats), location=city).items():
            supply[(mat_id, city)] = info.get("per_day", 0.0)
            _mark_thin_order(prices, mat_id, city, info.get("avg_price", 0))

    # Часть себестоимостей была занижена «тонкими» ордерами — пересчитываем.
    # Пересчёт делается ДО сверки цен продажи: он пересоздаёт строки, и если
    # прижать цену раньше, результат просто затрётся.
    if prices.fair:
        head = _revalue(head, prices, cfg, budget)

    for r in head:
        _sanity_check_price(r, sell_avg.get(r.item_id, 0), cfg)

    for r in head:
        limit_qty, bottleneck, unknown = None, "", []
        for mat in r.recipe:
            per_day = supply.get((mat["id"], r.buy_city))
            if per_day is None:
                # Нет истории торгов не значит, что материала нет в продаже.
                unknown.append(mat["id"])
                continue
            capacity = per_day / mat["count"]
            if limit_qty is None or capacity < limit_qty:
                limit_qty, bottleneck = capacity, mat["id"]

        r.unknown_supply = unknown
        r.supply_per_day = round(limit_qty, 1) if limit_qty is not None else -1.0
        r.bottleneck = bottleneck
        r.realistic_per_day = (
            round(min(r.sold_per_day, r.supply_per_day), 1)
            if limit_qty is not None else r.sold_per_day
        )
    return head


def _mark_thin_order(prices: Prices, mat_id: str, city: str, avg_price: float) -> None:
    """Отметить материал, чей дешёвый ордер не отражает реальную цену закупки.

    Пример с рунами: минимальный ордер 7 серебра при средней цене сделок 29
    и обороте в миллионы штук за день. По семёрке лежит пара штук — набрать
    партию можно только вчетверо дороже. Считать себестоимость по такому
    ордеру значит занижать её в разы.
    """
    if avg_price <= 0:
        return
    row = prices.get(mat_id, city)
    if not row:
        return
    listed = row.get("sell_price_min") or 0
    if 0 < listed < avg_price * THIN_ORDER_RATIO:
        prices.fair[(mat_id, city)] = avg_price


def _revalue(rows: list, prices: Prices, cfg: dict, budget: float) -> list:
    """Пересчитать кандидатов с учётом честных цен и отсеять потерявших смысл."""
    f = cfg["filters"]
    kept = []
    for r in rows:
        fresh = craft.evaluate(
            {"id": r.item_id, "name": r.name, "tier": r.tier, "enchant": r.enchant,
             "variants": r.variants},
            prices, cfg, use_focus=r.focus_cost > 0,
            sell_at="same" if r.sell_city in cfg["buy_cities"] else None,
        )
        if not fresh:
            continue
        # переносим уже собранные сведения о спросе — craft.evaluate() создаёт
        # СВЕЖИЙ объект с пустыми полями по умолчанию, включая trend, поэтому
        # без явного переноса график цены пропадал бы у любой позиции,
        # прошедшей через пересчёт тонких ордеров (а в полном прогоне это
        # почти всегда).
        fresh.sold_per_day = r.sold_per_day
        fresh.trend = r.trend
        fresh.variants = r.variants
        if fresh.profit >= f["min_profit_silver"] and fresh.margin_pct >= f["min_margin_pct"]:
            kept.append(fresh)
    return kept


def _sanity_check_price(r, avg_price: float, cfg: dict) -> None:
    """Не верить выставленным ордерам выше реальных сделок.

    На Чёрном рынке кто угодно может выставить вещь за 2 300 000, когда рынок
    её берёт по 196 000. Такой ордер просто никогда не исполнится, а скан
    посчитает по нему миллионную прибыль. Поэтому цену режима 'order'
    прижимаем к средней цене фактических сделок за неделю.

    Режим 'instant' не трогаем: там цена — это реальный ордер на закупку.
    """
    if r.sell_mode != "order" or avg_price <= 0 or r.sell_price <= avg_price:
        return

    r.price_capped = True
    r.sell_price = avg_price
    r.revenue = avg_price * (1 - craft.sales_tax(cfg, r.sell_mode))
    r.profit = r.revenue - r.cost
    r.margin_pct = r.profit / r.cost * 100 if r.cost > 0 else 0.0


def apply_budget(rows: list, budget: float) -> list:
    """Что реально потянуть на имеющиеся деньги.

    Главная метрика для ограниченного капитала — не прибыль с одной вещи,
    а прибыль за один оборот: сколько штук влезает в кошелёк, с оглядкой
    на то, столько ли рынок вообще способен переварить.
    """
    out = []
    for r in rows:
        if r.cost <= 0 or r.cost > budget:
            continue
        affordable = int(budget // r.cost)
        cap = r.realistic_per_day if r.realistic_per_day > 0 else r.sold_per_day
        qty = max(1, min(affordable, int(cap) if cap > 0 else affordable))
        r.batch_qty = qty
        r.batch_cost = round(r.cost * qty)
        r.batch_profit = round(r.profit * qty)
        r.batch_focus = round(r.focus_cost * qty)
        out.append(r)
    out.sort(key=lambda r: r.batch_profit, reverse=True)
    return out
