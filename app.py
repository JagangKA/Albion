"""Мини-приложение: локальный сервер + веб-интерфейс.

Запускается одним кликом по Albion.cmd. В фоне сам обновляет цены,
в браузере показывает, что выгодно крафтить под твой бюджет.

Обновление цен идёт в отдельном потоке, чтобы интерфейс не подвисал:
сеть занимает минуты (лимиты API), а пересчёт под новый бюджет — мгновенный,
поэтому он делается прямо на запрос.
"""

from __future__ import annotations

import json
import os
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from albion import engine
from albion.prices import Prices

ROOT = os.path.dirname(os.path.abspath(__file__))


class State:
    """Общее состояние: считает фоновый поток, читают запросы."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cfg = engine.load_config()
        self.recipes = engine.load_recipes(self.cfg)
        self.prices = Prices(self.cfg["server"])
        # ключ — (место сбыта, фокус): "bm" — Чёрный рынок, "local" — продажа на месте
        self.rows: dict[tuple, list] = {}
        self.updated_at: str | None = None
        self.status = "запуск"
        self.busy = False
        self.error: str | None = None

    def log(self, msg: str) -> None:
        with self.lock:
            self.status = msg
        print(f"  [{datetime.now():%H:%M:%S}] {msg}", flush=True)

    def refresh(self, fetch: bool = True) -> None:
        if self.busy:
            return
        self.busy = True
        self.error = None
        try:
            if fetch:
                self.log("обновляю цены с сервера...")
                self.prices.drop_history_cache()
                engine.refresh_prices(self.cfg, self.recipes, self.prices, self.log)

            # Потолок отбора: считаем с запасом к заявленному бюджету, чтобы
            # ползунок в интерфейсе можно было двигать вверх без пересчёта сети.
            scope = float(self.cfg.get("budget", 300000)) * 4

            fresh: dict[tuple, list] = {}
            for place, sell_at in (("bm", None), ("local", "same")):
                where = "Чёрный рынок" if place == "bm" else "продажа на месте"
                for focus in (False, True):
                    label = "с фокусом" if focus else "без фокуса"
                    self.log(f"считаю: {where}, {label}...")
                    rows = engine.compute(self.cfg, self.recipes, self.prices,
                                          focus, sell_at=sell_at)
                    fresh[(place, focus)] = engine.enrich_liquidity(
                        rows, self.prices, self.cfg, budget=scope, log=lambda m: None
                    )

            with self.lock:
                self.rows = fresh
                self.updated_at = datetime.now().strftime("%H:%M:%S")
            self.log(
                f"готово — Чёрный рынок: {len(fresh[('bm', False)])}, "
                f"на месте: {len(fresh[('local', False)])}"
            )
        except Exception as exc:
            self.error = str(exc)
            self.log(f"ошибка: {exc}")
        finally:
            self.busy = False

    def snapshot(self, budget: float, focus: bool, top: int,
                 place: str = "bm") -> dict:
        with self.lock:
            rows = list(self.rows.get((place, focus), []))
            updated, status, busy, err = self.updated_at, self.status, self.busy, self.error

        priced = engine.apply_budget(rows, budget)
        return {
            "updated_at": updated,
            "status": status,
            "busy": busy,
            "error": err,
            "budget": budget,
            "focus": focus,
            "place": place,
            "total": len(priced),
            "server": self.cfg["server"],
            "premium": self.cfg["taxes"].get("has_premium", False),
            "rows": [_row_json(r) for r in priced[:top]],
        }


def _row_json(r) -> dict:
    return {
        "item_id": r.item_id,
        "name": r.name,
        "tier": r.tier,
        "enchant": r.enchant,
        "buy_city": r.buy_city,
        "cost": round(r.cost),
        "sell_price": round(r.sell_price),
        "profit": round(r.profit),
        "margin_pct": round(r.margin_pct, 1),
        "sell_mode": r.sell_mode,
        "price_capped": r.price_capped,
        "method": r.method,
        "sell_city": r.sell_city,
        "sold_per_day": r.sold_per_day,
        "supply_per_day": r.supply_per_day,
        "realistic_per_day": r.realistic_per_day,
        "unknown_supply": bool(r.unknown_supply),
        "bottleneck": r.bottleneck,
        "data_age_h": round(r.data_age_h, 1),
        "batch_qty": r.batch_qty,
        "batch_cost": r.batch_cost,
        "batch_profit": r.batch_profit,
        "batch_focus": r.batch_focus,
        "recipe": r.recipe,
    }


STATE: State | None = None


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            return self._file("ui/index.html", "text/html; charset=utf-8")
        if url.path == "/guide":
            return self._file("ui/guide.html", "text/html; charset=utf-8")
        if url.path == "/api/data":
            q = parse_qs(url.query)
            budget = float(q.get("budget", [STATE.cfg.get("budget", 300000)])[0])
            focus = q.get("focus", ["0"])[0] in ("1", "true")
            top = int(q.get("top", ["40"])[0])
            place = q.get("place", ["bm"])[0]
            return self._json(STATE.snapshot(budget, focus, top, place))
        if url.path == "/api/refresh":
            threading.Thread(target=STATE.refresh, daemon=True).start()
            return self._json({"ok": True})
        self.send_error(404)

    def _file(self, rel: str, ctype: str):
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            return self.send_error(404)
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # тишина в консоли, там свой лог
        pass


def background_loop(state: State) -> None:
    """Первый прогон — сразу, дальше по расписанию из конфига."""
    cached_age = state.prices.load()
    if cached_age is not None and cached_age < 1.0:
        state.log(f"беру кэш цен (возраст {cached_age * 60:.0f} мин)")
        state.refresh(fetch=False)
    else:
        state.refresh(fetch=True)

    minutes = max(int(state.cfg.get("auto_refresh_minutes", 20)), 5)
    while True:
        time.sleep(minutes * 60)
        state.refresh(fetch=True)


def main() -> None:
    global STATE
    STATE = State()
    port = int(STATE.cfg.get("port", 8765))

    threading.Thread(target=background_loop, args=(STATE,), daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print("=" * 62)
    print("  Albion — анализатор рынка")
    print(f"  открыто: {url}")
    print("  закрыть — закрой это окно")
    print("=" * 62)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
