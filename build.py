"""Однократный расчёт для публичной версии сайта.

Запускается по расписанию на стороне GitHub Actions: тянет цены, считает все
четыре сочетания (Чёрный рынок / на месте) x (с фокусом / без) и складывает
результат в docs/data.json. Дальше статическая страница просто читает этот файл,
поэтому сервер для сайта не нужен вовсе.

Бюджет на клиенте применяется в браузере — он только фильтрует готовые строки,
так что пересчитывать ради него ничего не надо.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

from albion import engine, recipes as recipes_mod
from albion.prices import Prices

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "docs", "data.json")

DUMPS = {
    "items.json": "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/items.json",
    "items_formatted.json": "https://raw.githubusercontent.com/ao-data/ao-bin-dumps/master/formatted/items.json",
}

# Публичная сборка отдаёт ПОЛНОЕ покрытие: у платных аналогов широкий каталог —
# как раз то, что прячут за подпиской. Пороги здесь заметно мягче локальных,
# а решение «показывать ли неприбыльное» принимает уже посетитель фильтрами.
PUBLIC_FILTERS = {
    "min_profit_silver": 50,
    "min_margin_pct": 3,
    "min_sold_per_day": 3,
}
# Сколько позиций проверять на объёмы. Это сетевые запросы, но история торгов
# кэшируется между режимами, поэтому цена охвата почти вся платится один раз.
CANDIDATES = 2000

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def ensure_dumps() -> None:
    """На чистой машине сборки дампов игры нет — качаем и пересобираем рецепты."""
    os.makedirs(DATA, exist_ok=True)
    missing = False
    for name, url in DUMPS.items():
        path = os.path.join(DATA, name)
        if os.path.exists(path):
            continue
        missing = True
        print(f"качаю {name}...", flush=True)
        urllib.request.urlretrieve(url, path)

    if missing or not os.path.exists(os.path.join(DATA, "recipes.json")):
        print("собираю базу рецептов...", flush=True)
        built = recipes_mod.build()
        with open(os.path.join(DATA, "recipes.json"), "w", encoding="utf-8") as fh:
            json.dump(built, fh, ensure_ascii=False)
        print(f"рецептов: {len(built)}", flush=True)


def classify(item_id: str, section: str) -> str:
    """Категория для фильтра в интерфейсе: снаряжение / переработка / расходники."""
    base = item_id.split("@")[0]
    if base.endswith(engine.REFINED):
        return "refine"
    if section == "consumableitem":
        return "consumable"
    return "gear"


def row_json(r, recs: dict) -> dict:
    section = recs.get(r.item_id, {}).get("section", "")
    return {
        "id": r.item_id,
        "name": r.name,
        "cat": classify(r.item_id, section),
        "tier": r.tier,
        "ench": r.enchant,
        "method": r.method,
        "buy": r.buy_city,
        "sell": r.sell_city,
        "cost": round(r.cost),
        "profit": round(r.profit),
        "margin": round(r.margin_pct, 1),
        "demand": r.sold_per_day,
        "supply": r.supply_per_day,
        "cap": r.realistic_per_day,
        "unknown": bool(r.unknown_supply),
        "mode": r.sell_mode,
        "capped": r.price_capped,
        "focus": round(r.focus_cost),
        "age": round(r.data_age_h, 1),
        "recipe": [
            {"id": m["id"], "n": m["count"], "p": m["price"], "ret": m["returnable"]}
            for m in r.recipe
        ],
    }


def main() -> None:
    ensure_dumps()
    cfg = engine.load_config()
    recs = engine.load_recipes(cfg)
    prices = Prices(cfg["server"])

    print(f"сервер {cfg['server']}, рецептов {len(recs)}", flush=True)
    engine.refresh_prices(cfg, recs, prices, log=lambda m: print(" ", m, flush=True))

    # Планка отбора берётся заведомо с запасом: посетители приходят с разными
    # кошельками, а пересчитать под каждый мы уже не сможем.
    scope = 20_000_000.0

    cfg["filters"].update(PUBLIC_FILTERS)

    modes: dict[str, list] = {}
    for place, sell_at in (("bm", None), ("local", "same")):
        for focus in (False, True):
            key = f"{place}|{int(focus)}"
            print(f"считаю {key}...", flush=True)
            rows = engine.compute(cfg, recs, prices, focus, sell_at=sell_at)
            rows = engine.enrich_liquidity(rows, prices, cfg, budget=scope,
                                           limit=CANDIDATES, log=lambda m: None)
            rows.sort(key=lambda r: r.profit, reverse=True)
            modes[key] = [row_json(r, recs) for r in rows]
            print(f"  -> {len(modes[key])}", flush=True)

    # Флиппинг: купить готовую вещь дешёво в городе, продать на Чёрном рынке —
    # без крафта вообще. Фокус тут ни при чём (нечего возвращать), поэтому
    # считаем один раз и кладём под ключ "flip|0" для единообразия с остальными.
    print("считаю flip|0...", flush=True)
    flip_recs = engine.build_flip_recipes(recs)
    flip_rows = engine.compute(cfg, flip_recs, prices, focus=False)
    flip_rows = engine.enrich_liquidity(flip_rows, prices, cfg, budget=scope,
                                        limit=CANDIDATES, log=lambda m: None)
    flip_rows.sort(key=lambda r: r.profit, reverse=True)
    modes["flip|0"] = [row_json(r, recs) for r in flip_rows]
    print(f"  -> {len(modes['flip|0'])}", flush=True)

    names = material_names(modes)
    stamp = _stamp()
    out_dir = os.path.dirname(OUT)
    os.makedirs(out_dir, exist_ok=True)

    # Общая часть — маленькая, грузится сразу; таблицы — по требованию.
    index = {
        "generated_at": stamp,
        "server": cfg["server"],
        "premium": cfg["taxes"].get("has_premium", False),
        "return_rate": cfg["return_rate"],
        "taxes": {k: v for k, v in cfg["taxes"].items() if not k.startswith("_")},
        "counts": {k: len(v) for k, v in modes.items()},
        "names": names,
    }
    _write(os.path.join(out_dir, "index.json"), index)
    for key, rows in modes.items():
        _write(os.path.join(out_dir, f"mode-{key.replace('|', '-')}.json"),
               {"generated_at": stamp, "rows": rows})

    total = sum(os.path.getsize(os.path.join(out_dir, f)) for f in os.listdir(out_dir)
                if f.endswith(".json") and f != "donate.json")
    print(f"записано в {out_dir}: {total / 1024:.0f} КБ суммарно")


def _write(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"  {os.path.basename(path)}: {os.path.getsize(path) / 1024:.0f} КБ")


def material_names(modes: dict) -> dict:
    """Русские названия материалов — чтобы в составе не светились игровые коды."""
    need = {m["id"] for rows in modes.values() for r in rows for m in r["recipe"]}
    if not need:
        return {}
    loc = recipes_mod._load_names(os.path.join(DATA, "items_formatted.json"))
    out = {}
    for market_id in need:
        base = market_id.split("@")[0]
        level = market_id.split("@")[1] if "@" in market_id else ""
        title = loc.get(base, base)
        out[market_id] = f"{title} .{level}" if level else title
    return out


def _stamp() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    main()
