"""Клиент Albion Online Data Project — краудсорсные цены рынка.

Данные собираются самими игроками через Albion Data Client, поэтому:
  * цена может быть устаревшей — всегда смотрим на дату отсчёта;
  * если по предмету в городе никто не заходил на рынок, данных не будет вовсе.
Оба случая калькулятор обязан отсекать, иначе посчитает выгоду по воздуху.

Лимиты API: 180 запросов/мин и 300 за 5 минут. Держим паузу между батчами.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

SERVERS = {
    "europe": "https://europe.albion-online-data.com",
    "west": "https://west.albion-online-data.com",     # Americas
    "east": "https://east.albion-online-data.com",     # Asia
}

CITIES = [
    "Martlock",
    "Bridgewatch",
    "Lymhurst",
    "Fort Sterling",
    "Thetford",
    "Caerleon",
    "Brecilien",
]
BLACK_MARKET = "Black Market"

# Замерено пробой: сервер спокойно принимает 300 id в одном запросе.
# Чем крупнее батч, тем меньше запросов — а именно их и лимитируют.
BATCH = 250
# Предел длины адреса запроса: за ним сервер отвечает 414 и батч теряется
MAX_URL = 3800
# Лимит API — порядка 300 запросов за 5 минут, то есть примерно 1 в секунду.
# Держим интервал с запасом, иначе ловим 429 и теряем данные.
MIN_INTERVAL = 2.0
RETRIES = 4


class Prices:
    def __init__(self, server: str = "europe", cache_file: str | None = None):
        if server not in SERVERS:
            raise ValueError(f"неизвестный сервер: {server}; доступны {list(SERVERS)}")
        self.base = SERVERS[server]
        self.server = server
        self.cache_file = cache_file or os.path.join(DATA, f"prices_{server}.json")
        # (item_id, city, quality) -> запись рынка
        self.data: dict[str, dict] = {}
        # (item_id, location) -> история торгов; значение None = данных нет
        self._hist: dict[tuple, dict | None] = {}
        # (item_id, city) -> честная цена закупки, если ордер оказался «тонким»
        self.fair: dict[tuple, float] = {}

    def drop_history_cache(self) -> None:
        self._hist.clear()

    # ---------------------------------------------------------------- запрос

    def fetch(self, item_ids, locations=None, qualities=(1,), progress=True) -> None:
        locations = locations or (CITIES + [BLACK_MARKET])
        item_ids = sorted(set(item_ids))
        loc = ",".join(urllib.parse.quote(x) for x in locations)
        qual = ",".join(str(q) for q in qualities)

        chunks = self._split(item_ids, len(loc) + len(qual) + len(self.base) + 60)
        total = len(chunks)
        for i, chunk in enumerate(chunks):
            url = (
                f"{self.base}/api/v2/stats/prices/{','.join(chunk)}"
                f"?locations={loc}&qualities={qual}"
            )
            try:
                rows = self._get(url)
            except Exception as exc:  # сеть/лимит — пропускаем батч, не роняем скан
                print(f"  ! батч {i + 1}: {exc}")
                continue

            for row in rows:
                key = f"{row['item_id']}|{row['city']}|{row['quality']}"
                self.data[key] = row

            if progress:
                print(f"\r  цены: батч {i + 1}/{total}", end="", flush=True)

        if progress:
            print()

    def history_cached(self, item_ids, location=BLACK_MARKET, days=7) -> dict:
        """История с памятью в пределах одного обновления.

        Объёмы торгов не зависят от того, считаем мы с фокусом или без, —
        значит и спрашивать их дважды незачем. Кэш чистится вместе с ценами.
        """
        need = [i for i in item_ids if (i, location) not in self._hist]
        if need:
            fresh = self.history(need, location=location, days=days)
            for item_id in need:
                # Запоминаем и отсутствие данных, иначе будем спрашивать снова
                self._hist[(item_id, location)] = fresh.get(item_id)
        return {
            i: self._hist[(i, location)]
            for i in item_ids
            if self._hist.get((i, location)) is not None
        }

    def history(self, item_ids, location=BLACK_MARKET, days=7, qualities=(1,)) -> dict:
        """Объём продаж — прокси ликвидности. Сколько штук рынок реально съедает."""
        out: dict[str, dict] = {}
        item_ids = sorted(set(item_ids))
        loc = urllib.parse.quote(location)
        qual = ",".join(str(q) for q in qualities)

        for chunk in self._split(item_ids, len(loc) + len(qual) + len(self.base) + 70):
            url = (
                f"{self.base}/api/v2/stats/history/{','.join(chunk)}"
                f"?locations={loc}&time-scale=24&qualities={qual}"
            )
            try:
                rows = self._get(url)
            except Exception as exc:
                print(f"  ! история: {exc}")
                continue

            for row in rows:
                points = (row.get("data") or [])[-days:]
                sold = sum(p.get("item_count", 0) for p in points)
                prices = [p["avg_price"] for p in points if p.get("avg_price")]
                out[row["item_id"]] = {
                    "sold": sold,
                    "per_day": round(sold / max(len(points), 1), 1),
                    "avg_price": round(sum(prices) / len(prices)) if prices else 0,
                    "days": len(points),
                }
        return out

    @staticmethod
    def _split(item_ids: list, overhead: int) -> list[list]:
        """Режет список так, чтобы URL гарантированно влез.

        Считать батч в штуках нельзя: id предметов сильно разной длины, а к ним
        добавляются локации. При семи локациях запрос на 250 позиций уже ловит
        414 — и батч теряется целиком, а расчёт молча считает по неполным ценам.
        """
        limit = MAX_URL - overhead
        chunks, current, length = [], [], 0
        for item_id in item_ids:
            piece = len(item_id) + 1
            if current and (len(current) >= BATCH or length + piece > limit):
                chunks.append(current)
                current, length = [], 0
            current.append(item_id)
            length += piece
        if current:
            chunks.append(current)
        return chunks

    _last_call = 0.0

    @classmethod
    def _get(cls, url: str) -> list:
        """Запрос с соблюдением лимита и повтором при 429.

        Молча проглатывать 429 нельзя: батч пропадает, и скан считает выгоду
        по неполным данным — то есть врёт, ничего об этом не сообщая.
        """
        last_error: Exception | None = None
        for attempt in range(RETRIES):
            wait = MIN_INTERVAL - (time.monotonic() - cls._last_call)
            if wait > 0:
                time.sleep(wait)
            cls._last_call = time.monotonic()

            req = urllib.request.Request(
                url, headers={"User-Agent": "albion-craft-scan/1.0"}
            )
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code != 429:
                    raise
                retry_after = exc.headers.get("Retry-After")
                pause = float(retry_after) if retry_after else 15.0 * (attempt + 1)
                print(f"\r  лимит API, жду {pause:.0f}с...", end="", flush=True)
                time.sleep(pause)
            except Exception as exc:
                last_error = exc
                time.sleep(3.0 * (attempt + 1))

        raise last_error if last_error else RuntimeError("не удалось выполнить запрос")

    # ------------------------------------------------------------- доступ

    def get(self, item_id: str, city: str, quality: int = 1) -> dict | None:
        return self.data.get(f"{item_id}|{city}|{quality}")

    def buy_cost(self, item_id: str, city: str, max_age_h: float, quality: int = 1):
        """Во сколько обойдётся МГНОВЕННО купить предмет: минимальный sell-ордер.

        Возвращает (цена, возраст_в_часах) либо None, если данных нет/протухли.
        """
        row = self.get(item_id, city, quality)
        if not row:
            return None
        price = row.get("sell_price_min") or 0
        if price <= 0:
            return None
        age = _age_hours(row.get("sell_price_min_date"))
        if age is None or age > max_age_h:
            return None
        # Если самый дешёвый ордер оказался «тонким» (цена сильно ниже реальных
        # сделок — значит там лежит пара штук), считаем по честной цене.
        return max(price, self.fair.get((item_id, city), 0.0)), age

    def sell_instant(self, item_id: str, city: str, max_age_h: float, quality: int = 1):
        """Сколько дадут МГНОВЕННО: максимальный buy-ордер (Чёрный рынок и т.п.)."""
        row = self.get(item_id, city, quality)
        if not row:
            return None
        price = row.get("buy_price_max") or 0
        if price <= 0:
            return None
        age = _age_hours(row.get("buy_price_max_date"))
        if age is None or age > max_age_h:
            return None
        return price, age

    def sell_order(self, item_id: str, city: str, max_age_h: float, quality: int = 1):
        """Цена, по которой предмет выставлен на продажу — потолок при ожидании."""
        row = self.get(item_id, city, quality)
        if not row:
            return None
        price = row.get("sell_price_min") or 0
        if price <= 0:
            return None
        age = _age_hours(row.get("sell_price_min_date"))
        if age is None or age > max_age_h:
            return None
        return price, age

    # ------------------------------------------------------------- кэш

    def save(self) -> None:
        os.makedirs(DATA, exist_ok=True)
        with open(self.cache_file, "w", encoding="utf-8") as fh:
            json.dump({"fetched": _now_iso(), "data": self.data}, fh, ensure_ascii=False)

    def load(self) -> float | None:
        """Загружает кэш. Возвращает его возраст в часах или None."""
        if not os.path.exists(self.cache_file):
            return None
        with open(self.cache_file, encoding="utf-8") as fh:
            blob = json.load(fh)
        self.data = blob.get("data", {})
        return _age_hours(blob.get("fetched"))


def _age_hours(stamp: str | None) -> float | None:
    """Возраст отметки времени API в часах. API отдаёт UTC без таймзоны."""
    if not stamp or stamp.startswith("0001"):
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", ""))
    except ValueError:
        return None
    dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
