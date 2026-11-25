import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

API_BASE = "https://bynex.io/trading/ru/api/symbolOrderBook/pair/"
GITHUB_ORIGIN = "https://romja85.github.io"
BYNEX_ORIGIN = "https://bynex.io"
HOST = "127.0.0.1"
PORT = 8000

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SECRETS_FILE = os.path.join(BASE_DIR, "secrets.json")
CONFIG_FILE = os.path.join(BASE_DIR, "monitor_config.json")
STATE_FILE = os.path.join(BASE_DIR, "monitor_state.json")

PAIRS = {
    "USDT-BYN", "USDT-USD",
    "LTC-USDT", "LTC-BYN", "LTC-USD",
    "BTC-USDT", "BTC-BYN", "BTC-USD",
    "SOL-USDT", "SOL-BYN", "SOL-USD",
    "ETH-USDT", "ETH-BYN", "ETH-USD",
}

DEFAULT_CONFIG = {
    "revision": 0,
    "auto_enabled": False,
    "interval": 2,
    "cards": [
        {
            "id": "1",
            "pair": "USDT-BYN",
            "bid": {"value": "", "mode": "order"},
            "ask": {"value": "", "mode": "order"},
        }
    ],
    "arbitrage": {
        "enabled": False,
        "currency": "USDT",
        "amount": 100,
        "fee": 0.1,
        "min_margin": 0.3,
    },
}

lock = threading.RLock()
config = None
fired_state = {}
status_state = {}
wallet_balance_state = {
    "currency": None,
    "balance": None,
    "received_at": None,
}
monitor_thread = None
stop_event = threading.Event()
wake_event = threading.Event()
# Ограничиваем суммарное число одновременных обращений к BYNEX
# независимо от количества вкладок и фоновых/браузерных запросов.
orderbook_upstream_semaphore = threading.BoundedSemaphore(3)


# Диагностика фонового мониторинга. Счётчики действуют с момента запуска сервера.
monitor_metrics = {
    "thread_started": False,
    "last_cycle_started_at": None,
    "last_cycle_completed_at": None,
    "last_success_at": None,
    "last_error_at": None,
    "last_error": None,
    "last_pair": None,
    "cycles": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "last_cycle_duration_seconds": None,
    "active_pairs": 0,
    "active_controllers": 0,
    "telegram_sent": 0,
    "telegram_failed": 0,
    "arbitrage_signals": 0,
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def update_metrics(**values):
    with lock:
        monitor_metrics.update(values)


def record_orderbook_result(pair, success, error=None):
    now = utc_now()
    with lock:
        monitor_metrics["last_pair"] = pair
        if success:
            monitor_metrics["successful_requests"] += 1
            monitor_metrics["last_success_at"] = now
        else:
            monitor_metrics["failed_requests"] += 1
            monitor_metrics["last_error_at"] = now
            monitor_metrics["last_error"] = "{}: {}".format(pair, str(error)[:300])


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        print("Warning: cannot read", os.path.basename(path), "- using defaults")
        return default


def atomic_write_json(path, data):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def load_secrets():
    data = read_json(SECRETS_FILE, {})
    return {
        "telegram_token": str(data.get("telegram_token", "")).strip(),
        "chat_id": str(data.get("chat_id", "")).strip(),
    }



def validate_config(candidate):
    if not isinstance(candidate, dict):
        raise ValueError("Некорректный формат конфигурации")

    interval = candidate.get("interval", 2)
    if (
        isinstance(interval, bool)
        or not isinstance(interval, (int, float))
        or not math.isfinite(interval)
    ):
        raise ValueError("Интервал должен быть числом")
    if int(interval) != interval or not 1 <= int(interval) <= 3600:
        raise ValueError("Интервал должен быть от 1 до 3600 секунд")

    auto_enabled = candidate.get("auto_enabled", False)
    if not isinstance(auto_enabled, bool):
        raise ValueError("Некорректное значение автообновления")

    cards = candidate.get("cards", [])
    if not isinstance(cards, list) or len(cards) > 100:
        raise ValueError("Некорректный список карточек")

    cleaned_cards = []
    seen_ids = set()
    for card in cards:
        if not isinstance(card, dict):
            raise ValueError("Некорректная карточка")
        card_id = str(card.get("id", "")).strip()
        pair = str(card.get("pair", "")).upper().strip()
        if not card_id or len(card_id) > 64 or card_id in seen_ids:
            raise ValueError("Идентификаторы карточек должны быть уникальными")
        if pair not in PAIRS:
            raise ValueError("Неподдерживаемая торговая пара: " + pair)
        seen_ids.add(card_id)

        clean_card = {"id": card_id, "pair": pair}
        for side in ("bid", "ask"):
            control = card.get(side, {})
            if not isinstance(control, dict):
                raise ValueError("Некорректный контроллер " + side.upper())
            raw_value = str(control.get("value", "")).strip()
            mode = str(control.get("mode", "order"))
            if mode not in ("order", "threshold"):
                raise ValueError("Некорректный режим " + side.upper())
            if raw_value:
                try:
                    value = float(raw_value.replace(",", "."))
                except ValueError:
                    raise ValueError("Некорректная контрольная цена " + side.upper())
                if not (value > 0 and value < float("inf")):
                    raise ValueError("Контрольная цена должна быть положительной")
            clean_card[side] = {"value": raw_value, "mode": mode}
        cleaned_cards.append(clean_card)

    result = {
        "revision": int(candidate.get("revision", 0)),
        "auto_enabled": auto_enabled,
        "interval": int(interval),
        "cards": cleaned_cards,
    }

    arb = candidate.get("arbitrage") or {}
    if not isinstance(arb, dict):
        arb = {}
    raw_assets = arb.get("starting_assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raw_assets = [{
            "currency": arb.get("currency", "USDT"),
            "amount": arb.get("amount", 100),
        }]
    starting_assets = []
    allowed_currencies = {"USDT", "BTC", "LTC", "ETH", "SOL", "BYN", "USD"}
    for asset in raw_assets[:20]:
        if not isinstance(asset, dict):
            continue
        currency = str(asset.get("currency") or "").upper().strip()
        try:
            amount = float(asset.get("amount"))
        except (TypeError, ValueError):
            continue
        if currency in allowed_currencies and math.isfinite(amount) and amount > 0:
            starting_assets.append({"currency": currency, "amount": amount})
    if not starting_assets:
        starting_assets = [{"currency": "USDT", "amount": 100.0}]

    result["arbitrage"] = {
        "enabled": bool(arb.get("enabled", False)),
        "starting_assets": starting_assets,
        "fee": float(arb.get("fee") if arb.get("fee") is not None else 0.1),
        "min_margin": float(arb.get("min_margin") if arb.get("min_margin") is not None else 0.3),
    }
    if (
        not math.isfinite(result["arbitrage"]["fee"])
        or not (0 <= result["arbitrage"]["fee"] < 100)
    ):
        result["arbitrage"]["fee"] = 0.1
    if (
        not math.isfinite(result["arbitrage"]["min_margin"])
        or not (0 <= result["arbitrage"]["min_margin"] <= 100)
    ):
        result["arbitrage"]["min_margin"] = 0.3

    return result


def fetch_orderbook(pair):
    request = Request(
        API_BASE + pair,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Referer": "https://bynex.io/",
        },
    )
    with urlopen(request, timeout=15) as response:
        data = json.loads(response.read().decode("utf-8"))

    bids = data.get("bids") or []
    asks = data.get("asks") or []
    if not bids or not asks:
        raise ValueError("Стакан пуст")

    bid_prices = [float(item["price"]) for item in bids]
    ask_prices = [float(item["price"]) for item in asks]
    best_bid = max(bid_prices)
    best_ask = min(ask_prices)
    if best_bid <= 0 or best_ask <= 0:
        raise ValueError("Некорректная цена в стакане")
    return best_bid, best_ask



def send_telegram_message(message):
    secret_data = load_secrets()
    token = secret_data["telegram_token"]
    chat_id = secret_data["chat_id"]
    if not token or not chat_id:
        print("TELEGRAM: не настроен, уведомление не отправлено.")
        with lock:
            monitor_metrics["telegram_failed"] += 1
        return False

    import urllib.parse
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = Request(
        "https://api.telegram.org/bot" + token + "/sendMessage",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("ok"):
            with lock:
                monitor_metrics["telegram_sent"] += 1
            return True

        with lock:
            monitor_metrics["telegram_failed"] += 1
        print("TELEGRAM: API вернул отказ; токен и URL скрыты.")
        return False
    except Exception as error:
        with lock:
            monitor_metrics["telegram_failed"] += 1
        # Не выводим str(error): исключение может содержать URL с токеном.
        print("TELEGRAM: ошибка отправки ({})".format(type(error).__name__))
        return False


def control_condition(side, mode, current_price, target):
    if side == "bid":
        return current_price < target if mode == "order" else current_price > target
    return current_price > target if mode == "order" else current_price < target


def fetch_full_orderbook(pair):
    request = Request(
        API_BASE + pair,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Referer": "https://bynex.io/",
        },
    )
    with orderbook_upstream_semaphore:
        with urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
    return data.get("bids") or [], data.get("asks") or []


def build_arbitrage_edges(order_books, fee):
    edges = []
    for item in order_books:
        pair = item["pair"]
        book = item["book"]
        try:
            base, quote = pair.split("-")
        except ValueError:
            continue

        bids = sorted(
            [
                {"price": float(x["price"]), "volume": float(x["volume"])}
                for x in (book.get("bids") or [])
                if float(x.get("price", 0)) > 0 and float(x.get("volume", 0)) > 0
            ],
            key=lambda x: -x["price"],
        )
        asks = sorted(
            [
                {"price": float(x["price"]), "volume": float(x["volume"])}
                for x in (book.get("asks") or [])
                if float(x.get("price", 0)) > 0 and float(x.get("volume", 0)) > 0
            ],
            key=lambda x: x["price"],
        )
        if not bids or not asks:
            continue

        edges.append({
            "from": base, "to": quote, "pair": pair,
            "direction": "Продажа по BID", "levels": bids, "side": "sell", "fee": fee,
        })
        edges.append({
            "from": quote, "to": base, "pair": pair,
            "direction": "Покупка по ASK", "levels": asks, "side": "buy", "fee": fee,
        })
    return edges


def execute_arbitrage_edge(edge, amount):
    if not amount or amount <= 0:
        return None
    remaining = float(amount)
    received = 0.0
    for level in edge["levels"]:
        if remaining <= 1e-12:
            break
        if edge["side"] == "sell":
            can = min(remaining, level["volume"])
            received += can * level["price"] * (1 - edge["fee"])
            remaining -= can
        else:
            cost = level["volume"] * level["price"]
            can = min(remaining, cost)
            received += (can / level["price"]) * (1 - edge["fee"])
            remaining -= can
    if remaining > amount * 1e-8:
        return None
    return received


def find_arbitrage(edges, start_currency, start_amount, min_margin):
    routes = []
    for first in edges:
        if first["from"] != start_currency:
            continue
        for second in edges:
            if second["from"] != first["to"] or second["pair"] == first["pair"]:
                continue
            if second["to"] == start_currency:
                continue
            for third in edges:
                if third["from"] != second["to"] or third["to"] != start_currency:
                    continue
                if third["pair"] in (first["pair"], second["pair"]):
                    continue

                a1 = execute_arbitrage_edge(first, start_amount)
                if a1 is None:
                    continue
                a2 = execute_arbitrage_edge(second, a1)
                if a2 is None:
                    continue
                final = execute_arbitrage_edge(third, a2)
                if final is None:
                    continue

                profit = final - start_amount
                margin = (final / start_amount - 1) * 100
                if profit > 0 and margin >= min_margin:
                    routes.append({
                        "margin": margin,
                        "profit": profit,
                        "final": final,
                        "currency": start_currency,
                        "amount": start_amount,
                        "steps": [
                            {"edge": first, "in": start_amount, "out": a1,
                             "price": (a1 / (start_amount * (1 - first["fee"]))) if first["side"] == "sell" else (start_amount * (1 - first["fee"]) / a1)},
                            {"edge": second, "in": a1, "out": a2,
                             "price": (a2 / (a1 * (1 - second["fee"]))) if second["side"] == "sell" else (a1 * (1 - second["fee"]) / a2)},
                            {"edge": third, "in": a2, "out": final,
                             "price": (final / (a2 * (1 - third["fee"]))) if third["side"] == "sell" else (a2 * (1 - third["fee"]) / final)},
                        ],
                    })
    routes.sort(key=lambda r: r["margin"], reverse=True)
    return routes



def run_server_arbitrage(current_config, order_books=None):
    arb = current_config.get("arbitrage") or {}
    if not arb.get("enabled"):
        return None

    if order_books is None:
        pairs = sorted({card["pair"] for card in current_config.get("cards", [])})
        order_books = []
        for pair in pairs:
            try:
                bids, asks = fetch_full_orderbook(pair)
                if bids and asks:
                    order_books.append({
                        "pair": pair,
                        "book": {"bids": bids, "asks": asks},
                    })
            except Exception as error:
                print("ARBITRAGE: ошибка стакана {} ({})".format(
                    pair, type(error).__name__
                ))

    if len(order_books) < 3:
        return None

    fee = float(arb.get("fee", 0.1)) / 100.0
    edges = build_arbitrage_edges(order_books, fee)
    assets = arb.get("starting_assets") or [{
        "currency": arb.get("currency", "USDT"),
        "amount": arb.get("amount", 100),
    }]
    routes = []
    for asset in assets:
        routes.extend(find_arbitrage(
            edges,
            asset["currency"],
            float(asset["amount"]),
            float(arb.get("min_margin", 0.3)),
        ))
    routes.sort(key=lambda route: route["margin"], reverse=True)
    return routes[0] if routes else None


def format_arbitrage_message(route, currency=None, amount=None):
    currency = route.get("currency", currency or "USDT")
    amount = route.get("amount", amount if amount is not None else 100)
    lines = [
        "BYNEX — арбитраж",
        "Маржа: +{:.4f}%".format(route["margin"]),
        "Прибыль: +{:.6g} {}".format(route["profit"], currency),
        "Сумма: {} {}".format(amount, currency),
    ]
    path = [currency] + [s["edge"]["to"] for s in route["steps"]]
    lines.append(" → ".join(path))
    lines.append("")
    for i, step in enumerate(route["steps"], 1):
        lines.append(
            "{}. {} — {} @ {:.10g} ({:.6g} → {:.6g})".format(
                i, step["edge"]["pair"], step["edge"]["direction"],
                step["price"], step["in"], step["out"],
            )
        )
    return "\n".join(lines)



def monitor_loop():
    print("MONITOR: фоновый поток запущен.")
    update_metrics(thread_started=True)

    while not stop_event.is_set():
        # Сбрасываем событие до чтения конфигурации: изменения во время цикла
        # останутся установленными и разбудят поток после завершения цикла.
        wake_event.clear()
        cycle_started = time.time()

        try:
            with lock:
                current_config = json.loads(json.dumps(config))

            price_monitor_enabled = bool(current_config["auto_enabled"])
            arb = current_config.get("arbitrage") or {}
            arbitrage_enabled = bool(arb.get("enabled"))
            cards = current_config["cards"]

            configured_controllers = sum(
                1
                for card in cards
                for side in ("bid", "ask")
                if str((card.get(side) or {}).get("value", "")).strip()
            )
            pairs = sorted({card["pair"] for card in cards}) if (
                price_monitor_enabled or arbitrage_enabled
            ) else []

            update_metrics(
                last_cycle_started_at=utc_now(),
                active_pairs=len(pairs),
                active_controllers=(
                    configured_controllers if price_monitor_enabled else 0
                ),
            )

            # Оба серверных режима независимы: автоарбитраж может работать,
            # даже если обычный авто-мониторинг BID/ASK выключен.
            if not price_monitor_enabled and not arbitrage_enabled:
                update_metrics(
                    last_cycle_completed_at=utc_now(),
                    last_cycle_duration_seconds=round(time.time() - cycle_started, 3),
                )
                wake_event.wait(1)
                continue

            order_books = []
            prices = {}

            # Получаем каждый уникальный стакан один раз. Эти же данные
            # используются и для BID/ASK, и для автоарбитража.
            for pair in pairs:
                if stop_event.is_set():
                    break
                try:
                    bids, asks = fetch_full_orderbook(pair)
                    if not bids or not asks:
                        raise ValueError("Стакан пуст")

                    book = {"bids": bids, "asks": asks}
                    order_books.append({"pair": pair, "book": book})

                    if price_monitor_enabled:
                        bid_prices = [float(item["price"]) for item in bids]
                        ask_prices = [float(item["price"]) for item in asks]
                        best_bid = max(bid_prices)
                        best_ask = min(ask_prices)
                        if best_bid <= 0 or best_ask <= 0:
                            raise ValueError("Некорректная цена в стакане")
                        prices[pair] = (best_bid, best_ask)

                    record_orderbook_result(pair, True)
                except Exception as error:
                    record_orderbook_result(pair, False, error)
                    print("MONITOR: ошибка получения {} ({})".format(
                        pair, type(error).__name__
                    ))

            notifications = []

            if price_monitor_enabled:
                with lock:
                    # Не применяем цены, если настройки успели измениться
                    # во время получения стаканов.
                    if current_config["revision"] != config["revision"]:
                        update_metrics(
                            last_cycle_completed_at=utc_now(),
                            last_cycle_duration_seconds=round(
                                time.time() - cycle_started, 3
                            ),
                        )
                        continue

                    active_keys = {
                        card["id"] + ":" + side
                        for card in cards for side in ("bid", "ask")
                    }
                    for stale_key in list(fired_state):
                        if stale_key not in active_keys:
                            fired_state.pop(stale_key, None)
                            status_state.pop(stale_key, None)

                    for card in cards:
                        pair = card["pair"]
                        if pair not in prices:
                            continue
                        bid_price, ask_price = prices[pair]

                        for side, current_price in (
                            ("bid", bid_price), ("ask", ask_price)
                        ):
                            control = card[side]
                            raw_target = control["value"]
                            key = card["id"] + ":" + side
                            signature = "|".join((
                                pair, raw_target, control["mode"]
                            ))

                            if not raw_target:
                                fired_state.pop(key, None)
                                status_state[key] = {
                                    "fired": False, "message": "Не задано"
                                }
                                continue

                            target = float(raw_target.replace(",", "."))
                            previous = fired_state.get(key)
                            if previous != signature:
                                fired_state.pop(key, None)

                            condition = control_condition(
                                side, control["mode"], current_price, target
                            )
                            if condition and key not in fired_state:
                                fired_state[key] = signature
                                side_name = side.upper()
                                mode_name = (
                                    "Контроль ордера"
                                    if control["mode"] == "order"
                                    else "Контроль порога"
                                )
                                message = (
                                    "BYNEX Monitor — сигнал " + side_name + "\n"
                                    "Пара: " + pair + "\n"
                                    + mode_name + "\n"
                                    "Контрольная цена: " + raw_target + "\n"
                                    "Текущая цена: " + format(current_price, ".10g")
                                )
                                notifications.append(message)
                                status_state[key] = {
                                    "fired": True,
                                    "message": "Сигнал! Условие выполнено",
                                    "price": current_price,
                                }
                            elif key in fired_state:
                                status_state[key] = {
                                    "fired": True,
                                    "message": "Сигнал! Условие выполнено",
                                    "price": current_price,
                                }
                            else:
                                status_state[key] = {
                                    "fired": False,
                                    "message": "Контроль активен",
                                    "price": current_price,
                                }

                    atomic_write_json(STATE_FILE, fired_state)

            for message in notifications:
                send_telegram_message(message)

            if arbitrage_enabled:
                best = run_server_arbitrage(current_config, order_books)
                if best:
                    msg = format_arbitrage_message(
                        best,
                        arb.get("currency", "USDT"),
                        arb.get("amount", 100),
                    )
                    if send_telegram_message(msg):
                        with lock:
                            monitor_metrics["arbitrage_signals"] += 1
                        print(
                            "ARBITRAGE: сигнал отправлен, маржа:",
                            round(best["margin"], 4), "%"
                        )

            with lock:
                monitor_metrics["cycles"] += 1
                monitor_metrics["last_cycle_completed_at"] = utc_now()
                monitor_metrics["last_cycle_duration_seconds"] = round(
                    time.time() - cycle_started, 3
                )
                successful = monitor_metrics["successful_requests"]
                failed = monitor_metrics["failed_requests"]
                cycles = monitor_metrics["cycles"]

            print(
                "MONITOR: цикл #{}, пар: {}, успешных запросов: {}, "
                "ошибок: {}, контроллеров: {}, автоарбитраж: {}, время: {} сек.".format(
                    cycles, len(pairs), successful, failed,
                    configured_controllers if price_monitor_enabled else 0,
                    "вкл." if arbitrage_enabled else "выкл.",
                    round(time.time() - cycle_started, 2),
                )
            )

        except Exception as error:
            now = utc_now()
            update_metrics(
                last_error_at=now,
                last_error="Ошибка фонового цикла: {}: {}".format(
                    type(error).__name__, str(error)[:300]
                ),
                last_cycle_completed_at=now,
                last_cycle_duration_seconds=round(time.time() - cycle_started, 3),
            )
            print("MONITOR: ошибка фонового цикла ({})".format(
                type(error).__name__
            ))

        with lock:
            interval = int(config.get("interval", 2)) if config else 2
            active = bool(config and (
                config.get("auto_enabled") or
                (config.get("arbitrage") or {}).get("enabled")
            ))
        wake_event.wait(interval if active else 1)



class BynexHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        origin = self.headers.get("Origin")
        path = urlparse(self.path).path
        origin_allowed = (
            origin == GITHUB_ORIGIN
            or (origin == BYNEX_ORIGIN and path == "/api/wallet-balance")
        )
        if origin_allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header(
                "Access-Control-Allow-Methods", "GET, POST, OPTIONS"
            )
            self.send_header(
                "Access-Control-Allow-Headers", "Content-Type"
            )
        super().end_headers()

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        parsed = urlparse(self.path)
        allowed_paths = (
            "/api/orderbook",
            "/api/config",
            "/api/status",
            "/api/wallet-balance",
        )
        if parsed.path not in allowed_paths:
            self.send_error(404, "Not found")
            return
        origin = self.headers.get("Origin")
        origin_allowed = (
            origin == GITHUB_ORIGIN
            or (
                parsed.path == "/api/wallet-balance"
                and origin == BYNEX_ORIGIN
            )
        )
        if not origin_allowed:
            self.send_error(403, "Origin not allowed")
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/orderbook":
            params = parse_qs(parsed.query)
            pair = params.get("pair", ["USDT-BYN"])[0].upper()
            if pair not in PAIRS:
                self.send_error(400, "Unsupported trading pair")
                return
            try:
                request = Request(
                    API_BASE + pair,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json",
                        "Referer": "https://bynex.io/",
                    },
                )
                with orderbook_upstream_semaphore:
                    with urlopen(request, timeout=15) as response:
                        data = response.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except HTTPError as error:
                self.send_error(error.code, "BYNEX API returned an HTTP error")
            except (URLError, TimeoutError, OSError) as error:
                self.send_error(502, "Cannot connect to BYNEX: " + str(error))
            return

        if parsed.path == "/api/wallet-balance":
            with lock:
                self.send_json(200, dict(wallet_balance_state))
            return

        if parsed.path == "/api/config":
            with lock:
                self.send_json(200, config)
            return

        if parsed.path == "/api/status":
            with lock:
                diagnostics = dict(monitor_metrics)
                diagnostics["thread_alive"] = bool(
                    monitor_thread and monitor_thread.is_alive()
                )
                diagnostics["auto_arbitrage_enabled"] = bool(
                    (config.get("arbitrage") or {}).get("enabled")
                )
                self.send_json(200, {
                    "auto_enabled": config["auto_enabled"],
                    "revision": config["revision"],
                    "controllers": dict(status_state),
                    "wallet_balance": dict(wallet_balance_state),
                    "diagnostics": diagnostics,
                    "server_time": utc_now(),
                })
            return

        super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/wallet-balance":
            if self.headers.get("Origin") != BYNEX_ORIGIN:
                self.send_json(403, {"error": "Origin not allowed"})
                return

            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 4096:
                    self.send_json(400, {"error": "Некорректный размер запроса"})
                    return

                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Ожидался JSON-объект")

                currency = str(payload.get("currency", "")).upper().strip()
                balance = payload.get("balance")

                if currency not in ("BYN", "USD", "BTC"):
                    raise ValueError("Неподдерживаемая валюта баланса")
                if (
                    isinstance(balance, bool)
                    or not isinstance(balance, (int, float))
                    or not math.isfinite(balance)
                    or balance < 0
                ):
                    raise ValueError("Некорректное значение баланса")

                with lock:
                    wallet_balance_state.update({
                        "currency": currency,
                        "balance": float(balance),
                        "received_at": utc_now(),
                    })
                    self.send_json(200, {
                        "ok": True,
                        "wallet_balance": dict(wallet_balance_state),
                    })

            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            return

        if parsed.path != "/api/config":
            self.send_error(404, "Not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 1024 * 256:
                self.send_json(400, {"error": "Некорректный размер запроса"})
                return
            body = self.rfile.read(length)
            candidate = validate_config(json.loads(body.decode("utf-8")))

            with lock:
                client_revision = candidate["revision"]
                if client_revision != config["revision"]:
                    self.send_json(409, {
                        "error": "Конфигурация уже изменена на другом устройстве",
                        "current": config,
                    })
                    return
                candidate["revision"] = config["revision"] + 1
                atomic_write_json(CONFIG_FILE, candidate)
                config.clear()
                config.update(candidate)
                wake_event.set()
                self.send_json(200, config)

        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})


def main():
    global config, fired_state, monitor_thread

    stop_event.clear()
    wake_event.clear()
    config = validate_config(read_json(CONFIG_FILE, DEFAULT_CONFIG))
    if not os.path.exists(CONFIG_FILE):
        atomic_write_json(CONFIG_FILE, config)

    loaded_state = read_json(STATE_FILE, {})
    if isinstance(loaded_state, dict):
        fired_state = loaded_state

    secrets_data = load_secrets()
    if not secrets_data["telegram_token"] or not secrets_data["chat_id"]:
        print("WARNING: Telegram is not configured. Check secrets.json.")

    monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
    monitor_thread.start()

    server = ThreadingHTTPServer((HOST, PORT), BynexHandler)
    print("BYNEX Monitor server started")
    print("Open http://127.0.0.1:8000/bynex_monitor.html")
    print("GitHub Pages origin:", GITHUB_ORIGIN)
    print("API config: POST /api/config без пароля; revision conflict protection включена.")
    print("Диагностика фонового цикла: GET /api/status")
    print("Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping BYNEX Monitor...")
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
