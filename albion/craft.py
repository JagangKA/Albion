"""Расчёт себестоимости крафта и выгоды продажи на Чёрный рынок.

Себестоимость считается так (и это ровно та математика, что в видео,
только автоматом и без ручного перебора):

    материалы_возвращаемые × (1 − return_rate)
  + материалы_невозвращаемые           <- артефакты, помечены в дампе игры
  + серебро по рецепту
  + сбор мастерской
  = себестоимость единицы

    цена продажи × (1 − налог) − себестоимость = прибыль

Если хотя бы одного ресурса нет в продаже в выбранном городе (или цена
протухла), вариант рецепта отбрасывается целиком: считать выгоду по
недостающей цене — самый быстрый способ уехать в минус.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Result:
    item_id: str
    name: str
    tier: int
    enchant: int
    buy_city: str
    materials: float          # чистая стоимость закупки, без учёта возврата
    cost: float               # себестоимость единицы с учётом возврата и сборов
    revenue: float            # выручка после налога
    profit: float
    margin_pct: float
    sell_price: float         # цена до налога
    sell_mode: str            # instant | order
    data_age_h: float         # худший возраст использованных котировок
    sold_per_day: float = 0.0        # спрос: сколько штук в день съедает рынок сбыта
    supply_per_day: float = 0.0      # предложение: на сколько штук хватит материалов
    realistic_per_day: float = 0.0   # минимум из двух — реальный потолок
    bottleneck: str = ""             # какой материал ограничивает
    unknown_supply: list = field(default_factory=list)  # материалы без истории торгов
    price_capped: bool = False  # цену прижали к средней по реальным сделкам
    focus_cost: float = 0.0     # расход фокуса на штуку (0, если считаем без фокуса)
    method: str = "craft"       # craft — собрать из материалов; upgrade — поднять рунами
    sell_city: str = ""         # где продаём
    recipe: list = field(default_factory=list)      # состав выбранного варианта
    variants: list = field(default_factory=list)    # все варианты, нужны для пересчёта
    # Заполняется под конкретный бюджет (engine.apply_budget)
    batch_qty: int = 0        # сколько штук влезает в кошелёк и переварит рынок
    batch_cost: float = 0.0   # вложить за рейс
    batch_profit: float = 0.0 # снять за рейс
    batch_focus: float = 0.0  # сколько фокуса съест партия
    trend: list = field(default_factory=list)  # цена по дням для спарклайна

    @property
    def profit_per_day(self) -> float:
        """Сколько теоретически даст позиция, если выкупать весь дневной спрос."""
        return self.profit * self.sold_per_day

    @property
    def realistic_profit_per_day(self) -> float:
        """То же, но с оглядкой на то, хватит ли материалов физически."""
        return self.profit * self.realistic_per_day


def effective_return(cfg: dict, use_focus: bool) -> float:
    rr = cfg["return_rate"]
    base = rr["with_focus"] if use_focus else rr["no_focus"]
    return min(base + rr.get("city_bonus", 0.0), 0.95)


def sales_tax(cfg: dict, sell_mode: str) -> float:
    t = cfg["taxes"]
    tax = t["sales_tax_premium"] if t.get("has_premium") else t["sales_tax_no_premium"]
    if sell_mode == "order":
        tax += t.get("setup_fee", 0.0)
    return tax


def cost_of_variant(variant: dict, prices, city: str, cfg: dict, use_focus: bool):
    """Себестоимость одного варианта рецепта в конкретном городе.

    Возвращает (себестоимость, стоимость_материалов, возраст_данных, состав)
    либо None, если хоть один ресурс недоступен.
    """
    max_age = cfg["filters"]["max_price_age_hours"]
    rr = effective_return(cfg, use_focus)

    returnable = 0.0
    fixed = 0.0
    worst_age = 0.0
    breakdown = []

    for res in variant["resources"]:
        quote = prices.buy_cost(res["id"], city, max_age)
        if quote is None:
            return None
        price, age = quote
        line = price * res["count"]
        worst_age = max(worst_age, age)
        breakdown.append(
            {"id": res["id"], "count": res["count"], "price": price,
             "returnable": res["returnable"]}
        )
        if res["returnable"]:
            returnable += line
        else:
            fixed += line

    materials = returnable + fixed
    cost = returnable * (1 - rr) + fixed
    cost += variant.get("silver", 0.0)
    cost += materials * cfg.get("station_fee_pct", 0.0)
    return cost, materials, worst_age, breakdown, variant.get("focus", 0.0)


def _sell_options(prices, item_id: str, location: str, max_age: float) -> list:
    """Куда можно деть готовую вещь в этой точке: сразу в ордер или своим ордером."""
    options = []
    instant = prices.sell_instant(item_id, location, max_age)
    if instant:
        options.append(("instant", instant[0], instant[1]))
    order = prices.sell_order(item_id, location, max_age)
    if order:
        options.append(("order", order[0], order[1]))
    return options


def evaluate(recipe: dict, prices, cfg: dict, use_focus: bool,
             sell_at: str | None = None) -> Result | None:
    """Лучший способ сделать и продать вещь.

    sell_at=None    — сбыт туда, где указано в настройках (Чёрный рынок).
    sell_at="same"  — продажа в том же городе, где куплены материалы:
                      никуда не едешь, риска перевозки нет вообще.
    """
    max_age = cfg["filters"]["max_price_age_hours"]
    item_id = recipe["id"]

    fixed_options = None
    if sell_at != "same":
        fixed_options = _sell_options(prices, item_id, sell_at or cfg["sell_location"],
                                      max_age)
        if not fixed_options:
            return None

    best: Result | None = None
    for city in cfg["buy_cities"]:
        options = (fixed_options if fixed_options is not None
                   else _sell_options(prices, item_id, city, max_age))
        if not options:
            continue

        for variant in recipe["variants"]:
            priced = cost_of_variant(variant, prices, city, cfg, use_focus)
            if priced is None:
                continue
            cost, materials, cost_age, breakdown, focus_cost = priced

            for mode, price, price_age in options:
                revenue = price * (1 - sales_tax(cfg, mode))
                profit = revenue - cost
                if cost <= 0:
                    continue
                candidate = Result(
                    focus_cost=focus_cost if use_focus else 0.0,
                    method=variant.get("kind", "craft"),
                    sell_city=city if fixed_options is None
                    else (sell_at or cfg["sell_location"]),
                    item_id=item_id,
                    name=recipe["name"],
                    tier=recipe["tier"],
                    enchant=recipe["enchant"],
                    buy_city=city,
                    materials=materials,
                    cost=cost,
                    revenue=revenue,
                    profit=profit,
                    margin_pct=profit / cost * 100,
                    sell_price=price,
                    sell_mode=mode,
                    data_age_h=max(cost_age, price_age),
                    recipe=breakdown,
                    variants=recipe["variants"],
                )
                if best is None or candidate.profit > best.profit:
                    best = candidate
    return best
