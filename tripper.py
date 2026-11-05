"""Check flexible-date fares and send Telegram alerts for new observed lows."""

import argparse
from contextlib import closing
from datetime import date, timedelta
import hashlib
from itertools import product
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/state")) / "Tripper"
SEARCH_CAP = 240
WINDOW_SECONDS = 31 * 86400
APIFY_CAP = 4_500_000  # Microdollars in a rolling 31 days
APIFY_START_COST = 5_000
APIFY_ROW_COST = 200
APIFY_ACTOR_URL = (
    "https://api.apify.com/v2/actors/alom~google-flights-scraper/run-sync-get-dataset-items"
)
CREDENTIAL_NAMES = ("SERPAPI_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "APIFY_TOKEN")


def load_credentials(path):
    values = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            name = name.strip()
            if separator and name in CREDENTIAL_NAMES:
                value = value.strip()
                if len(value) >= 2 and value[0] in ("'", '"') and value[-1] == value[0]:
                    value = value[1:-1]
                values[name] = value
    return tuple(os.environ.get(name, values.get(name)) for name in CREDENTIAL_NAMES)


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
        if "return_through" in watch:
            if 0 in days:
                raise ValueError(f"{name}: return_through requires a round trip.")
            watch["return_through"] = date.fromisoformat(watch["return_through"]).isoformat()
        adults = watch.setdefault("adults", 1)
        if type(adults) is not int or not 1 <= adults <= 9:
            raise ValueError(f"{name}: adults must be between 1 and 9.")
        unknown = watch.keys() - {
            "name", "origins", "destinations", "depart_from", "depart_through", "trip_days",
            "return_through", "adults",
        }
        if unknown:
            raise ValueError(f"{name}: unsupported settings: {', '.join(sorted(unknown))}.")
    return watches


def calendar_windows(watch, today):
    start = max(today, date.fromisoformat(watch["depart_from"]))
    last_departure = date.fromisoformat(watch["depart_through"]).toordinal()
    return_deadline = watch.get("return_through")
    windows = []
    for days in watch["trip_days"]:
        end = last_departure
        if return_deadline:
            end = min(end, date.fromisoformat(return_deadline).toordinal() - days)
        if end >= start.toordinal():
            windows.append((days, start, date.fromordinal(end)))
    return windows


def request_json(service, url, data=None, *, headers=None, timeout=30, response_type=dict):
    try:
        with urlopen(Request(url, data=data, headers=headers or {}), timeout=timeout) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"{service} returned HTTP {error.code}.") from None
    except (URLError, TimeoutError, ValueError):
        raise RuntimeError(f"{service} request failed or returned invalid JSON.") from None
    if not isinstance(result, response_type):
        raise RuntimeError(f"{service} returned an unexpected response.")
    return result


def calendar_queries(watch, today):
    return list(product(watch["origins"], watch["destinations"], calendar_windows(watch, today)))


def apify_budget(queries):
    calendar_days = max((end - start).days + 1 for _, _, (_, start, end) in queries)
    if len(queries) > 100 or calendar_days > 330:
        raise RuntimeError("Apify supports at most 100 route windows of up to 330 days per watch.")
    return calendar_days, APIFY_START_COST + len(queries) * calendar_days * APIFY_ROW_COST


def scan_apify(db, token, queries, adults):
    calendar_days, cost = apify_budget(queries)
    used = db.execute("SELECT COALESCE(SUM(cost), 0) FROM apify_usage").fetchone()[0]
    if used + cost > APIFY_CAP:
        raise RuntimeError("Apify has reached Tripper's free usage cap.")

    routes = []
    expected = set()
    for origin, destination, (days, start, end) in queries:
        route = {"origin": origin, "destination": destination, "departureDate": start.isoformat()}
        if days:
            route["returnDate"] = (start + timedelta(days=days)).isoformat()
        routes.append(route)
        for offset in range((end - start).days + 1):
            outbound = start + timedelta(days=offset)
            inbound = (outbound + timedelta(days=days)).isoformat() if days else ""
            expected.add((origin, destination, outbound.isoformat(), inbound))

    payload = {
        **routes[0],
        "routes": routes[1:],
        "mode": "calendar",
        "calendarDays": calendar_days,
        "adults": adults,
        "cabinClass": "economy",
        "maxStops": "any",
        "currency": "USD",
        "country": "US",
        "language": "en",
    }
    # Reserve the entire run limit even if the request times out after the Actor starts.
    with db:
        db.execute("INSERT INTO apify_usage VALUES (?, ?)", (int(time.time()), cost))
    params = urlencode({"maxTotalChargeUsd": f"{cost / 1_000_000:.6f}", "timeout": 270})
    rows = request_json(
        "Apify",
        f"{APIFY_ACTOR_URL}?{params}",
        json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=300,
        response_type=list,
    )
    offers = {}
    covered = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("route"), str):
            continue
        airports = re.findall(r"\b[A-Z]{3}\b", row["route"].upper())
        outbound, inbound = row.get("departureDate"), row.get("returnDate") or ""
        if len(airports) != 2 or not isinstance(outbound, str) or not isinstance(inbound, str):
            continue
        pair = (*airports, outbound, inbound)
        if pair not in expected or row.get("currency") != "USD" or "price" not in row:
            continue
        price = row["price"]
        if price is None:
            covered.add(pair)
        elif type(price) in (int, float) and math.isfinite(price) and price > 0:
            covered.add(pair)
            link = row.get("googleFlightsUrl")
            if isinstance(link, str):
                try:
                    url = urlsplit(link)
                    link = link if url.scheme == "https" and url.netloc == "www.google.com" else ""
                except ValueError:
                    link = ""
            else:
                link = ""
            offer = (price, *pair, link)
            if pair not in offers or offer < offers[pair]:
                offers[pair] = offer
    return offers, len(covered), len(expected)


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

    offers = {}
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
        pair = (origin, destination, outbound.isoformat(), str(inbound or ""))
        offer = (price, *pair, "")
        if pair not in offers or offer < offers[pair]:
            offers[pair] = offer
    return offers


def scan_serpapi(db, key, queries, adults):
    used = db.execute("SELECT COUNT(*) FROM usage").fetchone()[0]
    if used + len(queries) > SEARCH_CAP:
        raise RuntimeError("SerpApi has reached Tripper's free search cap.")

    offers = {}
    for origin, destination, (days, start, end) in queries:
        # Count every attempt before sending it, even if the response is lost.
        with db:
            db.execute("INSERT INTO usage VALUES (?)", (int(time.time()),))
        offers.update(check_calendar(key, origin, destination, days, start, end, adults))
    return offers


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


def offer_link(offer):
    _, origin, destination, outbound, inbound, link = offer
    dates = f"{outbound} to {inbound}" if inbound else outbound
    query = urlencode({"q": f"flights from {origin} to {destination} {dates}"})
    return link or f"https://www.google.com/search?{query}"


def record_price(db, watch, offer, source):
    watch_id = hashlib.sha256(json.dumps(watch, sort_keys=True).encode()).hexdigest()
    previous = db.execute("SELECT price FROM lows WHERE watch = ?", (watch_id,)).fetchone()
    price, origin, destination, outbound, inbound, _ = offer
    print(f"{watch['name']} [{source}]: USD {price} ({origin} to {destination}, {outbound}"
          f"{' to ' + inbound if inbound else ''})")
    if previous and price >= previous[0]:
        return
    with db:
        db.execute("INSERT OR REPLACE INTO lows VALUES (?, ?)", (watch_id, price))
        if previous:
            dates = f"{outbound} to {inbound}" if inbound else outbound
            message = (
                f"New fare low for {watch['name']}\n"
                f"USD {price} (previous low USD {previous[0]})\n"
                f"{origin} to {destination} | {dates} | {watch['adults']} adult(s)\n"
                f"Calendar quote via {source}. Check availability and final price.\n"
                f"{offer_link(offer)}"
            )
            db.execute("INSERT INTO alerts (message) VALUES (?)", (message,))


def print_pilot_result(source, offers, expected):
    print(f"  {source}: {len(offers)}/{expected} eligible date pairs priced.")
    if offers:
        best = min(offers.values())
        price, origin, destination, outbound, inbound, _ = best
        print(f"  Lowest: USD {price} | {origin} to {destination} | {outbound} {inbound}".rstrip())
        print(f"  {offer_link(best)}")


def check_watches(db, watches, key, apify_token=None, *, pilot=False):
    now = int(time.time())
    today = date.today()
    with db:
        db.execute("DELETE FROM usage WHERE attempted_at <= ?", (now - WINDOW_SECONDS,))
        db.execute("DELETE FROM apify_usage WHERE attempted_at <= ?", (now - WINDOW_SECONDS,))
    failed = False
    for watch in watches:
        queries = calendar_queries(watch, today)
        if not queries:
            print(f"{watch['name']}: no departure dates fit the watch.")
            continue
        if pilot:
            print(f"{watch['name']} (pilot)")
        apify_offers = None
        expected = sum((end - start).days + 1 for _, _, (_, start, end) in queries)
        if apify_token:
            try:
                apify_offers, covered, expected = scan_apify(db, apify_token, queries, watch["adults"])
                if pilot:
                    print_pilot_result("Apify", apify_offers, expected)
                if covered != expected:
                    raise RuntimeError(f"Apify accounted for {covered}/{expected} eligible date pairs.")
                if not pilot:
                    if apify_offers:
                        record_price(db, watch, min(apify_offers.values()), "Apify")
                    else:
                        print(f"{watch['name']} [Apify]: no fares for the selected dates.")
                    continue
            except RuntimeError as error:
                next_step = "Trying SerpApi." if not pilot else ""
                print(f"{watch['name']}: {error} {next_step}".rstrip(), file=sys.stderr)
                failed |= pilot
        try:
            offers = scan_serpapi(db, key, queries, watch["adults"])
        except RuntimeError as error:
            print(f"{watch['name']}: {error}", file=sys.stderr)
            failed = True
            continue
        if pilot:
            print_pilot_result("SerpApi", offers, expected)
            if apify_offers is not None:
                common = apify_offers.keys() & offers.keys()
                same_price = sum(apify_offers[pair][0] == offers[pair][0] for pair in common)
                print(f"  Both priced {len(common)} pairs, agreeing on {same_price}. "
                      f"Apify-only: {len(apify_offers) - len(common)}. "
                      f"SerpApi-only: {len(offers) - len(common)}.")
        elif offers:
            record_price(db, watch, min(offers.values()), "SerpApi")
        else:
            print(f"{watch['name']} [SerpApi]: no fares returned for the selected dates.")
    return failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "watches.json")
    parser.add_argument("--source", choices=("serpapi", "apify"), default="serpapi",
                        help="primary provider for a scan or budget preview (default: serpapi)")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--plan", type=int, metavar="CHECKS_PER_DAY",
                       help="show the search budget without calling any service")
    modes.add_argument("--pilot", action="store_true",
                       help="compare both providers without changing price records or sending alerts")
    modes.add_argument("--test-alert", action="store_true", help="send a test to your Telegram chat")
    args = parser.parse_args()
    if not args.test_alert:
        watches = load_watches(args.config)
    if args.plan is not None:
        if args.plan < 1:
            raise ValueError("Checks per day must be at least one.")
        today = date.today()
        batches = [calendar_queries(watch, today) for watch in watches]
        if args.source == "apify":
            active = [queries for queries in batches if queries]
            cost = sum(apify_budget(queries)[1] for queries in active)
            needed = cost * args.plan * 31
            print(f"Apify runs per scan: {len(active)}. Up to USD {needed / 1_000_000:.2f} "
                  f"reserved per 31 days at {args.plan} scan(s) per day.")
            print(f"Tripper caps Apify reservations at USD {APIFY_CAP / 1_000_000:.2f} per rolling 31 days.")
            print(f"SerpApi fallback uses {sum(map(len, batches))} searches for a full scan of these watches.")
            return int(needed > APIFY_CAP)
        queries = sum(map(len, batches))
        needed = queries * args.plan * 31
        print(f"{queries} searches per scan; {needed} per 31 days at {args.plan} scan(s) per day.")
        print(f"Tripper caps itself at {SEARCH_CAP} search attempts per rolling 31 days.")
        return int(needed > SEARCH_CAP)

    key, token, chat, apify_token = load_credentials(ROOT / ".env")
    if args.test_alert:
        if not token or not chat:
            raise ValueError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env or your environment.")
        telegram_send(token, chat, "Tripper test alert. Phone alerts are connected.")
        print("Telegram accepted the test message.")
        return 0

    use_apify = args.pilot or args.source == "apify"
    if use_apify and not all((key, apify_token)):
        raise ValueError("Set APIFY_TOKEN and SERPAPI_KEY in .env or your environment.")
    if not args.pilot and not all((key, token, chat)):
        raise ValueError("Set SERPAPI_KEY, TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env or your environment.")
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
                CREATE TABLE IF NOT EXISTS apify_usage (attempted_at INTEGER NOT NULL, cost INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS lows (watch TEXT PRIMARY KEY, price INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, message TEXT NOT NULL);
            """)
            failed = check_watches(db, watches, key, apify_token if use_apify else None, pilot=args.pilot)
            if not args.pilot:
                notify_pending(db, token, chat)
            return int(failed)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
        sys.exit(f"Tripper: {error}")
