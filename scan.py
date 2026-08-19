"""Скан рынка из консоли. Для повседневной работы удобнее приложение (Albion.cmd),
это — быстрый прогон и отладка.

    python scan.py                # свежие цены и топ
    python scan.py --cache        # пересчитать по сохранённым ценам
    python scan.py --focus        # с фокус-очками
    python scan.py --budget 300000
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from albion import engine
from albion.prices import Prices

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", action="store_true", help="считать по сохранённым ценам")
    ap.add_argument("--focus", action="store_true", help="учитывать фокус-очки")
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    cfg = engine.load_config()
    budget = args.budget if args.budget is not None else cfg.get("budget", 300000)
    recipes = engine.load_recipes(cfg)
    print(f"сервер: {cfg['server']}   бюджет: {budget:,.0f}   "
          f"рецептов: {len(recipes)}".replace(",", " "))

    prices = Prices(cfg["server"])
    if args.cache:
        age = prices.load()
        if age is None:
            print("кэша нет — запусти без --cache")
            return
        print(f"кэш цен, возраст {age:.1f} ч")
    else:
        engine.refresh_prices(cfg, recipes, prices, log=lambda m: print(" ", m))

    rows = engine.compute(cfg, recipes, prices, focus=args.focus)
    print(f"прошли фильтр прибыли/маржи: {len(rows)}")
    rows = engine.enrich_liquidity(rows, prices, cfg, budget=budget,
                                   log=lambda m: print(" ", m))
    print(f"прошли фильтр ликвидности: {len(rows)}")
    rows = engine.apply_budget(rows, budget)
    print(f"влезают в бюджет: {len(rows)}")

    report(rows[: args.top], cfg, args, budget)
    dump(rows, cfg, args)


def report(rows, cfg: dict, args, budget: float) -> None:
    if not rows:
        print("\nНичего не прошло фильтры. Подними бюджет или ослабь пороги в config.json.")
        return

    mode = "с фокусом" if args.focus else "без фокуса"
    print(f"\n{'—' * 116}")
    print(f"ЧТО КРАФТИТЬ  ({mode}, бюджет {budget:,.0f}, сбыт: {cfg['sell_location']})"
          .replace(",", " "))
    print(f"{'—' * 116}")
    print(f"{'предмет':<32} {'закупка':<13} {'себест.':>9} {'приб/шт':>8} {'марж':>5} "
          f"{'спрос':>6} {'матер':>7} {'партия':>7} {'вложить':>10} {'снять':>10}")
    print(f"{'—' * 116}")
    for r in rows:
        label = f"{r.name[:25]}" + (f" .{r.enchant}" if r.enchant else "")
        mats = "  н/д" if r.supply_per_day < 0 else f"{r.supply_per_day:>5.0f}"
        flag = "?" if r.unknown_supply else " "
        print(
            f"{label:<32} {r.buy_city:<13} {r.cost:>9,.0f} {r.profit:>8,.0f} "
            f"{r.margin_pct:>4.0f}% {r.sold_per_day:>6.0f} {mats}{flag} "
            f"{r.batch_qty:>7} {r.batch_cost:>10,.0f} {r.batch_profit:>10,.0f}"
            .replace(",", " ")
        )
    print(f"{'—' * 116}")
    print("партия — сколько штук потянет бюджет с оглядкой на ёмкость рынка;")
    print("'?' у матер — по части материалов истории торгов нет, объём проверь в игре.")


def dump(rows, cfg: dict, args) -> None:
    out = os.path.join(engine.DATA, "scan_result.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "server": cfg["server"], "focus": args.focus,
                "rows": [
                    {
                        "item_id": r.item_id, "name": r.name, "tier": r.tier,
                        "enchant": r.enchant, "buy_city": r.buy_city,
                        "cost": round(r.cost), "profit": round(r.profit),
                        "margin_pct": round(r.margin_pct, 1),
                        "sold_per_day": r.sold_per_day,
                        "supply_per_day": r.supply_per_day,
                        "batch_qty": r.batch_qty, "batch_cost": r.batch_cost,
                        "batch_profit": r.batch_profit, "sell_mode": r.sell_mode,
                        "data_age_h": round(r.data_age_h, 1), "recipe": r.recipe,
                    }
                    for r in rows
                ],
            },
            fh, ensure_ascii=False, indent=1,
        )
    print(f"полный результат: {out}")


if __name__ == "__main__":
    main()
