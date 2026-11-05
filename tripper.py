"""Check flexible-date fares and send Telegram alerts for new observed lows."""

import argparse
from contextlib import closing
from datetime import date, timedelta
import hashlib
from itertools import product
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/state")) / "Tripper"
SEARCH_CAP = 240
WINDOW_SECONDS = 31 * 86400


def load_watches(path):
    watches = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(watches, list) or not watches:
        raise ValueError("Add at least one watch to the configuration.")

    names = set()
    for watch in watches:
        name = watch["name"].strip()
        if not name or len(name) > 100 or name in names:
            raise ValueError("Use a unique name of 1 to 100 characters for each watch.")
        names.add(name)
        watch["name"] = name
        for field in ("origins", "destinations"):
            codes = watch[field]
            if not isinstance(codes, list) or not codes or any(
                not re.fullmatch(r"[A-Z]{3}", code) for code in codes
            ):
                raise ValueError(f"{name}: {field} must be a list of IATA airport codes.")
            watch[field] = sorted(set(codes))

        if set(watch["origins"]) & set(watch["destinations"]):
            raise ValueError(f"{name}: origin and destination airports must be distinct.")
        if date.fromisoformat(watch["depart_from"]) > date.fromisoformat(watch["depart_through"]):
            raise ValueError(f"{name}: departure window is reversed.")
        days = watch["trip_days"]
        if not isinstance(days, list) or not days or any(
            type(day) is not int or not 0 <= day <= 365 for day in days
        ) or (0 in days and any(days)):
            raise ValueError(f"{name}: use [0] for one way or a list of positive trip lengths.")
        watch["trip_days"] = sorted(set(days))
        adults = watch.setdefault("adults", 1)
        if type(adults) is not int or not 1 <= adults <= 9:
            raise ValueError(f"{name}: adults must be between 1 and 9.")
        unknown = watch.keys() - {
            "name", "origins", "destinations", "depart_from", "depart_through", "trip_days", "adults"
        }
        if unknown:
            raise ValueError(f"{name}: unsupported settings: {', '.join(sorted(unknown))}.")
    return watches


def request_json(service, url, data=None):
    try:
        with urlopen(Request(url, data=data), timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"{service} returned HTTP {error.code}.") from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError(f"{service} request failed or returned invalid JSON.") from None
    if not isinstance(result, dict):
        raise RuntimeError(f"{service} returned an unexpected response.")
    return result


def check_calendar(key, origin, destination, days, start, end, adults):
    params = {
        "engine": "google_flights_calendar",
        "departure_id": origin,
        "arrival_id": destination,
        "date_range_start": start.isoformat(),
        "date_range_end": end.isoformat(),
        "type": "1" if days else "2",
        "adults": adults,
        "currency": "USD",
        "gl": "us",
        "api_key": key,
    }
    if days:
        params["round_trip_days"] = days
    result = request_json("SerpApi", "https://serpapi.com/search.json?" + urlencode(params))
    if result.get("error") or result.get("search_metadata", {}).get("status") != "Success":
        raise RuntimeError(f"SerpApi could not complete {origin} to {destination}.")
    prices = result.get("prices", [])
    if not isinstance(prices, list):
        raise RuntimeError("SerpApi returned an unexpected price list.")

    offers = []
    for row in prices:
        try:
            price = row["price"]
            outbound = date.fromisoformat(row["outbound_date"])
            inbound = date.fromisoformat(row["return_date"]) if days else None
        except (KeyError, TypeError, ValueError):
            continue
        if type(price) is not int or price <= 0 or not start <= outbound <= end:
            continue
        if inbound and (inbound - outbound).days != days:
            continue
        offers.append((price, origin, destination, outbound.isoformat(), str(inbound or "")))
    return min(offers, default=None)


def telegram_send(token, chat, message):
    data = urlencode({"chat_id": chat, "text": message}).encode()
    result = request_json("Telegram", f"https://api.telegram.org/bot{token}/sendMessage", data)
    if result.get("ok") is not True:
        raise RuntimeError("Telegram declined the message.")


def notify_pending(db, token, chat):
    for alert_id, message in db.execute("SELECT id, message FROM alerts ORDER BY id").fetchall():
        telegram_send(token, chat, message)
        with db:
            db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))


def record_price(db, watch, offer):
    watch_id = hashlib.sha256(json.dumps(watch, sort_keys=True).encode()).hexdigest()
    previous = db.execute("SELECT price FROM lows WHERE watch = ?", (watch_id,)).fetchone()
    price, origin, destination, outbound, inbound = offer
    print(f"{watch['name']}: USD {price} ({origin} to {destination}, {outbound}"
          f"{' to ' + inbound if inbound else ''})")
    if previous and price >= previous[0]:
        return
    with db:
        db.execute("INSERT OR REPLACE INTO lows VALUES (?, ?)", (watch_id, price))
        if previous:
            dates = f"{outbound} to {inbound}" if inbound else outbound
            query = urlencode({"q": f"flights from {origin} to {destination} {dates}"})
            message = (
                f"New fare low for {watch['name']}\n"
                f"USD {price} (previous low USD {previous[0]})\n"
                f"{origin} to {destination} | {dates} | {watch['adults']} adult(s)\n"
                f"Calendar quote. Check availability and final price.\n"
                f"https://www.google.com/search?{query}"
            )
            db.execute("INSERT INTO alerts (message) VALUES (?)", (message,))


def check_watches(db, watches, key):
    now = int(time.time())
    with db:
        db.execute("DELETE FROM usage WHERE attempted_at <= ?", (now - WINDOW_SECONDS,))
    remaining = SEARCH_CAP - db.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    failed = False
    for watch in watches:
        start = max(date.today(), date.fromisoformat(watch["depart_from"]))
        end = date.fromisoformat(watch["depart_through"])
        if start > end:
            print(f"{watch['name']}: departure window has ended.")
            continue
        queries = list(product(watch["origins"], watch["destinations"], watch["trip_days"]))
        if len(queries) > remaining:
            print(f"{watch['name']}: skipped to stay within the free search budget.")
            continue
        best = None
        try:
            for origin, destination, days in queries:
                # Count every attempt before sending it, even if the response is lost.
                with db:
                    db.execute("INSERT INTO usage VALUES (?)", (int(time.time()),))
                remaining -= 1
                offer = check_calendar(key, origin, destination, days, start, end, watch["adults"])
                if offer is not None and (best is None or offer < best):
                    best = offer
        except RuntimeError as error:
            print(f"{watch['name']}: {error}", file=sys.stderr)
            failed = True
            continue
        if best is None:
            print(f"{watch['name']}: no fares returned for the selected dates.")
        else:
            record_price(db, watch, best)
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "watches.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", type=int, metavar="CHECKS_PER_DAY",
                       help="show the search budget without calling either service")
    modes.add_argument("--test-alert", action="store_true", help="send a test to your Telegram chat")
    args = parser.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if args.test_alert:
        if not token or not chat:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to test alerts.")
        telegram_send(token, chat, "Tripper test alert. Phone alerts are connected.")
        print("Telegram accepted the test message.")
        return 0

    watches = load_watches(args.config)
    if args.plan is not None:
        if args.plan < 1:
            raise ValueError("Checks per day must be at least one.")
        queries = sum(len(w["origins"]) * len(w["destinations"]) * len(w["trip_days"]) for w in watches)
        needed = queries * args.plan * 31
        print(f"{queries} searches per scan; {needed} per 31 days at {args.plan} scan(s) per day.")
        print(f"Tripper caps itself at {SEARCH_CAP} search attempts per rolling 31 days.")
        return int(needed > SEARCH_CAP)

    key = os.environ.get("SERPAPI_KEY")
    if not all((key, token, chat)):
        raise ValueError("Set SERPAPI_KEY, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID before running.")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(STATE_DIR / "run.lock", timeout=0)) as lock:
        try:
            lock.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                raise
            print("Another Tripper run is active.")
            return 0
        with closing(sqlite3.connect(STATE_DIR / "tripper.sqlite3")) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS usage (attempted_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS lows (watch TEXT PRIMARY KEY, price INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, message TEXT NOT NULL);
            """)
            failed = check_watches(db, watches, key)
            notify_pending(db, token, chat)
            return int(failed)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
        sys.exit(f"Tripper: {error}")
