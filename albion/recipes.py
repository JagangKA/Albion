"""Построение базы рецептов крафта из игрового дампа ao-bin-dumps.

Вход : data/items.json          (дамп предметов из клиента игры)
       data/items_formatted.json (локализованные имена)
Выход: data/recipes.json        (компактная база: что из чего крафтится)

Что важно в игровых данных:
  * craftingrequirements может быть списком — это АЛЬТЕРНАТИВНЫЕ рецепты
    (например, артефакт можно заменить на токен благосклонности).
    Держим все варианты, выбор дешёвого делает калькулятор по ценам.
  * @maxreturnamount="0" у ресурса означает, что он НЕ возвращается
    через return rate. Так помечены артефакты. Это критично для себестоимости.
  * enchantments содержит отдельные рецепты для .1 / .2 / .3 / .4 —
    там свои ресурсы (T4_PLANKS_LEVEL1 и т.п.).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Ветки дампа, в которых лежат крафтимые игроками предметы.
CRAFTABLE_SECTIONS = (
    "weapon",
    "equipmentitem",
    "consumableitem",
    "furnitureitem",
    "mount",
    "journalitem",
    "simpleitem",
)


def _as_list(value: Any) -> list:
    """В дампе одиночный элемент — dict, несколько — list. Приводим к списку."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _parse_resources(req: dict) -> list[dict]:
    out = []
    for res in _as_list(req.get("craftresource")):
        name = res.get("@uniquename")
        if not name:
            continue
        # Зачарованный ресурс в дампе — T4_PLANKS_LEVEL1, а рынок знает его
        # как T4_PLANKS_LEVEL1@1. Без суффикса цена не найдётся вообще.
        level = int(res.get("@enchantmentlevel", 0) or 0)
        market_id = f"{name}@{level}" if level else name
        out.append(
            {
                "id": market_id,
                "count": int(res.get("@count", 1)),
                # maxreturnamount == "0" -> ресурс не возвращается (артефакты)
                "returnable": res.get("@maxreturnamount") != "0",
            }
        )
    return out


def _parse_variants(req_node: Any) -> list[dict]:
    """Возвращает список альтернативных рецептов одного предмета."""
    variants = []
    for req in _as_list(req_node):
        resources = _parse_resources(req)
        if not resources:
            continue
        variants.append(
            {
                "kind": "craft",
                "silver": float(req.get("@silver", 0) or 0),
                "focus": float(req.get("@craftingfocus", 0) or 0),
                "resources": resources,
            }
        )
    return variants


def _parse_upgrade(ench: dict, base_id: str, level: int) -> dict | None:
    """Зачарование готовой вещи рунами вместо крафта из зачарованных материалов.

    В игре к уровню .N можно прийти двумя путями: скрафтить сразу из
    зачарованных материалов либо взять вещь на уровень ниже и поднять её
    рунами (душами, реликвиями — по уровню). Пути стоят по-разному, и какой
    выгоднее сегодня — решают текущие цены, поэтому храним оба.

    Возврата материалов при улучшении нет, поэтому всё помечено как
    невозвращаемое.
    """
    node = ench.get("upgraderequirements") or {}
    res = node.get("upgraderesource")
    if isinstance(res, list):
        res = res[0] if res else None
    if not res or not res.get("@uniquename"):
        return None

    prev = f"{base_id}@{level - 1}" if level > 1 else base_id
    return {
        "kind": "upgrade",
        "silver": 0.0,
        "focus": 0.0,
        "resources": [
            {"id": prev, "count": 1, "returnable": False},
            {"id": res["@uniquename"], "count": int(res.get("@count", 1)),
             "returnable": False},
        ],
    }


def build(dump_path: str | None = None, names_path: str | None = None) -> dict:
    dump_path = dump_path or os.path.join(DATA, "items.json")
    names_path = names_path or os.path.join(DATA, "items_formatted.json")

    with open(dump_path, encoding="utf-8") as fh:
        root = json.load(fh)["items"]

    names = _load_names(names_path)
    recipes: dict[str, dict] = {}

    for section in CRAFTABLE_SECTIONS:
        for item in _as_list(root.get(section)):
            if not isinstance(item, dict):
                continue
            base_id = item.get("@uniquename")
            if not base_id:
                continue

            meta = {
                "tier": int(item.get("@tier", 0) or 0),
                "section": section,
                "shop_category": item.get("@shopcategory", ""),
                "shop_sub": item.get("@shopsubcategory1", ""),
                "craft_category": item.get("@craftingcategory", ""),
                "slot": item.get("@slottype", ""),
            }

            base_variants = _parse_variants(item.get("craftingrequirements"))
            if base_variants:
                recipes[base_id] = dict(
                    meta, id=base_id, enchant=0, name=names.get(base_id) or _humanize(base_id),
                    variants=base_variants,
                )

            # Зачарованные версии: .1 .2 .3 .4 — у каждой свой рецепт
            ench_node = item.get("enchantments") or {}
            for ench in _as_list(ench_node.get("enchantment")):
                level = int(ench.get("@enchantmentlevel", 0) or 0)
                if not level:
                    continue
                variants = _parse_variants(ench.get("craftingrequirements"))
                upgrade = _parse_upgrade(ench, base_id, level)
                if upgrade:
                    variants.append(upgrade)
                if not variants:
                    continue
                ench_id = f"{base_id}@{level}"
                recipes[ench_id] = dict(
                    meta, id=ench_id, enchant=level,
                    name=names.get(base_id) or _humanize(base_id), variants=variants,
                )

    return recipes


def _humanize(item_id: str) -> str:
    """Читаемая заглушка, когда для реального предмета нет перевода.

    В базе локализации community-проекта есть пробелы: часть настоящих
    вещей (редкие фракционные маунты, сезонные скины) просто не переведена,
    хотя это не мусор — мусор (тестовые/прототипные/квестовые записи)
    отсеивается ещё в engine._trusted() до того, как имя вообще понадобится.
    Показывать игроку сырой код вроде T8_MOUNT_ARMORED_HORSE_MORGANA хуже,
    чем английскую расшифровку "Mount Armored Horse Morgana".
    """
    base = re.sub(r"^T\d_", "", item_id.split("@")[0])
    words = [w.capitalize() for w in base.split("_") if w]
    return " ".join(words) or item_id


def _load_names(path: str) -> dict[str, str]:
    """Локализованные имена: предпочитаем русские, откатываемся на английские."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    out: dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        uid = entry.get("UniqueName")
        loc = entry.get("LocalizedNames") or {}
        if not uid or not isinstance(loc, dict):
            continue
        out[uid] = loc.get("RU-RU") or loc.get("EN-US") or uid
    return out


def main() -> None:
    recipes = build()
    out_path = os.path.join(DATA, "recipes.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(recipes, fh, ensure_ascii=False)

    enchanted = sum(1 for r in recipes.values() if r["enchant"])
    multi = sum(1 for r in recipes.values() if len(r["variants"]) > 1)
    print(f"рецептов: {len(recipes)}  (из них зачарованных: {enchanted})")
    print(f"с альтернативными вариантами крафта: {multi}")
    print(f"сохранено: {out_path}")


if __name__ == "__main__":
    main()
