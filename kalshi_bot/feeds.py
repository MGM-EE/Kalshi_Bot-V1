"""Read-only market data adapters. There is deliberately no order client.

REST can collect without credentials. Authenticated streaming is required for
healthy execution books and the CF reference; public exchange prices are never
substituted for that reference. Unknown contract semantics fail closed.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .models import D, FeeConfig, Market, PriceRange, jsonable

API_URL = "https://external-api.kalshi.com/trade-api/v2"
WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M"}
INDICES = {"BTC": "BRTI", "ETH": "ETHUSD_RTI", "SOL": "SOLUSD_RTI"}
API_HOSTS = {"external-api.kalshi.com", "api.elections.kalshi.com",
             "external-api.demo.kalshi.co", "demo-api.kalshi.co"}
WS_HOSTS = {"external-api-ws.kalshi.com", "api.elections.kalshi.com",
            "external-api-ws.demo.kalshi.co", "demo-api.kalshi.co"}


def event(kind: str, **fields) -> dict:
    return {"type": kind, "event_id": str(uuid.uuid4()), "received_at": time.time(),
            "monotonic_at": time.monotonic(), **jsonable(fields)}


def iso_time(value) -> float:
    if not isinstance(value, str):
        raise ValueError("An explicit timezone-aware timestamp is required")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Naive timestamps are not supported")
    return dt.timestamp()


def rule_hash(raw: dict) -> str:
    return hashlib.sha256((raw.get("rules_primary", "") + "\n" +
                           raw.get("rules_secondary", "")).encode()).hexdigest()


def _rule_datetime(value: str) -> float:
    # Exact English market wording, with DST checked against the named zone.
    match = re.fullmatch(r"(\d{1,2}:\d\d [AP]M) (EDT|EST) on ([A-Za-z]+ \d{1,2}, \d{4})", value)
    if not match:
        raise ValueError("Unrecognized rule timestamp")
    clock, abbreviation, date = match.groups()
    dt = None
    for fmt in ("%I:%M %p %b %d, %Y", "%I:%M %p %B %d, %Y"):
        try:
            dt = datetime.strptime(clock + " " + date, fmt).replace(tzinfo=ZoneInfo("America/New_York"))
            break
        except ValueError:
            pass
    if dt is None or dt.tzname() != abbreviation:
        raise ValueError("Rule date and timezone disagree")
    return dt.timestamp()


def load_overrides(path: str | None) -> dict:
    """User-reviewed semantics are bound to an exact ticker and rule hash.

    JSON: {"markets": {"TICKER": {"rules_sha256": "...", "observation_end":
    "2026-10-17T16:30:00Z", "rounding_digits": 4, "evidence": "review URL"}}}.
    Overrides cannot change strikes, cutoff, outcome mapping, fees, or tick sizes.
    """
    if not path:
        return {}
    value = json.loads(Path(path).read_text())
    if set(value) != {"markets"} or not isinstance(value["markets"], dict):
        raise ValueError("Market overrides require one 'markets' object")
    for ticker, override in value["markets"].items():
        if not re.fullmatch(r"[A-Z0-9-]+", ticker) or not isinstance(override, dict):
            raise ValueError("Invalid market override")
        if set(override) != {"rules_sha256", "observation_end", "rounding_digits", "evidence"}:
            raise ValueError("Override requires hash, observation_end, rounding_digits, evidence")
        if not re.fullmatch(r"[0-9a-f]{64}", override["rules_sha256"]):
            raise ValueError("Invalid rules SHA256")
        iso_time(override["observation_end"])
        if type(override["rounding_digits"]) is not int or not 0 <= override["rounding_digits"] <= 12:
            raise ValueError("Invalid rounding_digits")
        if not isinstance(override["evidence"], str) or not override["evidence"].strip():
            raise ValueError("Review evidence is required")
    return value["markets"]


def normalize_market(raw: dict, series: dict, fee_changes: list | None,
                     config: dict, asset: str, now: float,
                     overrides: dict | None = None) -> Market:
    """Interpret official fields, retaining invalid markets for collection."""
    invalid: list[str] = []
    ticker = str(raw.get("ticker", ""))
    if not re.fullmatch(r"[A-Z0-9-]+", ticker) or not ticker.startswith(SERIES[asset] + "-"):
        invalid.append("market_ticker_unrecognized")
    primary, secondary = raw.get("rules_primary", ""), raw.get("rules_secondary", "")
    rules = primary + "\n" + secondary
    try:
        strike = D(raw["floor_strike"])
        if not strike.is_finite() or strike <= 0:
            raise ValueError
    except (KeyError, ValueError, ArithmeticError):
        strike = D(0)
        invalid.append("official_strike_unresolved")
    if raw.get("strike_type") != "greater_or_equal" or raw.get("market_type") != "binary":
        invalid.append("outcome_mapping_unrecognized")
    times: dict[str, float] = {}
    for field in ("open_time", "close_time", "expiration_time"):
        try:
            times[field] = iso_time(raw[field])
        except (KeyError, ValueError, TypeError):
            times[field] = 0.0
            invalid.append(field + "_unresolved")
    observation_end = 0.0
    expected_index = INDICES[asset].replace("_", "")
    pattern = (r"If the simple average of the sixty seconds of CF Benchmarks' "
               r"([A-Z0-9_]+) before (.+?) is at least the simple average of the sixty seconds "
               r"of CF Benchmarks' ([A-Z0-9_]+) before (.+?), then the market resolves to Yes\.")
    match = re.fullmatch(pattern, primary)
    try:
        if not match:
            raise ValueError
        index_end, end_text, index_start, start_text = match.groups()
        if index_end.replace("_", "") != expected_index or index_start != index_end:
            raise ValueError
        observation_end = _rule_datetime(end_text)
        observation_start = _rule_datetime(start_text)
        if observation_start != times["open_time"] or observation_end - observation_start != 900:
            raise ValueError
    except ValueError:
        invalid.append("exact_rule_observation_window_unresolved")
    secondary_pattern = (r"Not all cryptocurrency price data is the same\. While checking a source like "
                         r"Google or Coinbase may help guide your decision, the price used to determine "
                         r"this market is based on CF Benchmarks' corresponding Real Time Index \(RTI\)\. "
                         r"At the last minute before expiration, 60 RTI prices are collected\. "
                         r"The official and final value is the average of these prices, rounded to the nearest "
                         r"(\d+) decimal places\.")
    rounding = re.fullmatch(secondary_pattern, secondary)
    try:
        if not rounding or int(rounding.group(1)) != int(raw["custom_strike"]["round_digits"]):
            raise ValueError
        rounding_digits = int(rounding.group(1))
        if strike != strike.quantize(D(1).scaleb(-rounding_digits)):
            raise ValueError
    except (KeyError, ValueError, TypeError, ArithmeticError):
        invalid.append("reference_rounding_unresolved")
    override = (overrides or {}).get(ticker)
    if override:
        if override["rules_sha256"] != rule_hash(raw):
            invalid.append("reviewed_rule_hash_changed")
        else:
            observation_end = iso_time(override["observation_end"])
            digits = override["rounding_digits"]
            if strike == strike.quantize(D(1).scaleb(-digits)):
                invalid = [r for r in invalid if r not in {
                    "exact_rule_observation_window_unresolved", "reference_rounding_unresolved"}]
    if not (0 < times["open_time"] < times["close_time"] <= observation_end <= times["expiration_time"]):
        invalid.append("contract_timestamps_conflict")
    if series.get("ticker") != SERIES[asset] or series.get("frequency") != "fifteen_min":
        invalid.append("series_metadata_unrecognized")
    ranges: list[PriceRange] = []
    try:
        for item in raw["price_ranges"]:
            r = PriceRange(D(item["start"]), D(item["end"]), D(item["step"]))
            if not all(n.is_finite() for n in (r.start, r.end, r.step)):
                raise ValueError
            if not (D(0) <= r.start < r.end <= D(1) and D(0) < r.step <= r.end - r.start):
                raise ValueError
            if (r.end - r.start) % r.step:
                raise ValueError
            ranges.append(r)
        if not ranges or ranges[0].start != 0 or ranges[-1].end != 1:
            raise ValueError
        if any(a.end != b.start for a, b in zip(ranges, ranges[1:])):
            raise ValueError
    except (KeyError, ValueError, TypeError, ArithmeticError):
        ranges = []
        invalid.append("tick_structure_unresolved")
    fee_model, multiplier = series.get("fee_type"), series.get("fee_multiplier")
    if fee_changes is not None:
        try:
            applicable = [f for f in fee_changes if f["event_ticker"] == raw["event_ticker"]
                          and iso_time(f["scheduled_ts"]) <= now]
            for change in sorted(applicable, key=lambda f: (iso_time(f["scheduled_ts"]), f["id"])):
                fee_model = change.get("fee_type_override") or series.get("fee_type")
                multiplier = (change["fee_multiplier_override"] if change.get("fee_multiplier_override") is not None
                              else series.get("fee_multiplier"))
        except (KeyError, TypeError, ValueError):
            fee_changes = None
    # Account precision class is not discoverable from public market metadata.
    scenario = config.get("fee_scenario")
    fee = FeeConfig()
    if isinstance(scenario, dict):
        try:
            m, quantum = D(scenario["multiplier"]), D(scenario["balance_quantum"])
            name = scenario.get("name", "explicit-unverified")
            if not (m.is_finite() and m >= 0 and quantum in (D("0.0001"), D("0.01")) and name):
                raise ValueError
            fee = FeeConfig(m, quantum, False, str(name), "quadratic")
        except (KeyError, ValueError, TypeError, ArithmeticError):
            invalid.append("fee_scenario_invalid")
    elif fee_model in ("quadratic", "quadratic_with_maker_fees") and fee_changes is not None:
        try:
            m = D(multiplier)
            if m.is_finite() and m >= 0:
                fee = FeeConfig(multiplier=m, verified=False, model="quadratic")
        except (ValueError, TypeError, ArithmeticError):
            pass
    def optional_time(field):
        try:
            return iso_time(raw[field]) if raw.get(field) else None
        except (TypeError, ValueError):
            return None
    return Market(ticker=ticker, asset=asset, strike=strike,
                  open_time=times["open_time"], cutoff=times["close_time"],
                  observation_end=observation_end, expiration=times["expiration_time"],
                  fee=fee, verified=not invalid, rules=rules,
                  invalid_reasons=tuple(invalid), determination=optional_time("determination_time"),
                  settlement=optional_time("settlement_time"),
                  tick_size=min((r.step for r in ranges), default=D("0.01")),
                  price_ranges=tuple(ranges))


class Auth:
    """Signing material stays in memory; no headers or key contents enter events."""
    def __init__(self, key_id: str, private_key):
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("Kalshi requires an RSA private key")
        self._key_id, self._key = key_id, private_key

    @classmethod
    def from_env(cls) -> Auth | None:
        key_id, path = os.environ.get("KALSHI_API_KEY_ID"), os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not key_id or not path:
            return None
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
        return cls(key_id, key)

    def headers(self, path: str, timestamp_ms: int | None = None) -> dict:
        timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        # Both APIs sign only the path, never the query string or hostname.
        path = urlparse(path).path
        signature = self._key.sign((timestamp + "GET" + path).encode(),
                                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                               salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self._key_id,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
                "KALSHI-ACCESS-TIMESTAMP": timestamp}

    def __repr__(self):
        return "Auth(<redacted>)"


class FeedGap(ValueError):
    """Reconnection is required before any more incremental updates are valid."""


class BookReconstructor:
    def __init__(self):
        self.books: dict[str, dict[str, dict[Decimal, Decimal]]] = {}
        self.sequences: dict[int, int] = {}
        self.book_subscriptions: dict[str, int] = {}

    def apply(self, payload: dict, received_at: float) -> dict | None:
        try:
            return self._apply(payload, received_at)
        except (KeyError, ValueError, TypeError, ArithmeticError) as exc:
            self.books.clear()
            self.sequences.clear()
            self.book_subscriptions.clear()
            if isinstance(exc, FeedGap):
                raise
            raise FeedGap("invalid_book_payload") from None

    def _apply(self, payload: dict, received_at: float) -> dict | None:
        kind = payload.get("type")
        if kind not in ("orderbook_snapshot", "orderbook_delta"):
            return None
        sid, seq, msg = int(payload["sid"]), int(payload["seq"]), payload["msg"]
        if sid < 1 or seq < 1:
            raise FeedGap("invalid_book_sequence")
        previous = self.sequences.get(sid)
        if previous is not None:
            if seq <= previous:
                return None  # Duplicate/out-of-order payload remains in the raw audit stream.
            if seq != previous + 1:
                self.books.clear()
                raise FeedGap("orderbook_sequence_gap")
        self.sequences[sid] = seq
        ticker = msg["market_ticker"]
        if kind == "orderbook_snapshot":
            book = {}
            for side in ("yes", "no"):
                book[side] = {}
                for price, quantity in msg.get(side + "_dollars_fp", []):
                    p, q = D(price), D(quantity)
                    if not (p.is_finite() and q.is_finite() and 0 <= p <= 1 and q >= 0):
                        raise FeedGap("invalid_book_level")
                    if p in book[side]:
                        raise FeedGap("duplicate_snapshot_level")
                    book[side][p] = q
            self.books[ticker] = book
            self.book_subscriptions[ticker] = sid
        else:
            if ticker not in self.books or self.book_subscriptions.get(ticker) != sid:
                raise FeedGap("delta_before_snapshot")
            side = msg["side"]
            if side not in ("yes", "no"):
                raise FeedGap("invalid_book_side")
            price, delta = D(msg["price_dollars"]), D(msg["delta_fp"])
            if not (price.is_finite() and delta.is_finite() and 0 <= price <= 1):
                raise FeedGap("invalid_book_delta")
            levels = self.books[ticker][side]
            quantity = levels.get(price, D(0)) + delta
            if quantity < 0:
                raise FeedGap("negative_book_quantity")
            if quantity:
                levels[price] = quantity
            else:
                levels.pop(price, None)
        book = self.books[ticker]
        healthy = not (book["yes"] and book["no"] and max(book["yes"]) + max(book["no"]) > 1)
        def ladder(side):
            # Keep fractional accumulated size internally, expose only integer executable q.
            return [[str(p), int(q.to_integral_value(rounding=ROUND_FLOOR))]
                    for p, q in sorted(book[side].items(), reverse=True) if q >= 1]
        return event("book", ticker=ticker, yes_bids=ladder("yes"), no_bids=ladder("no"),
                     healthy=healthy, sequence=seq, received_at=received_at,
                     venue="kalshi", channel="orderbook_delta", raw=payload)


def normalize_reference(payload: dict, received_at: float) -> dict | None:
    if payload.get("type") != "cfbenchmarks_value":
        return None
    msg = payload["msg"]
    upstream = json.loads(msg["data"])
    index_id = msg["index_id"]
    if upstream.get("type") != "value" or upstream.get("id") != index_id:
        raise ValueError("CF reference index mismatch")
    asset = next((asset for asset, index in INDICES.items() if index == index_id), None)
    if asset is None:
        return None
    price, source_at = D(upstream["value"]), float(upstream["time"]) / 1000
    if not price.is_finite() or price <= 0 or not source_at > 0:
        raise ValueError("Invalid CF reference value")
    return event("reference", asset=asset, price=str(price), source_at=source_at,
                 received_at=received_at, source="cfbenchmarks_value", venue="kalshi",
                 channel="cfbenchmarks_value", raw=payload,
                 sending_at=(payload["sending_ts_ms"] / 1000 if payload.get("sending_ts_ms") else None))


def _validate_url(url: str, hosts: set, scheme: str, path: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme != scheme or parsed.hostname not in hosts or parsed.port not in (None, 443)
            or parsed.path.rstrip("/") != path or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("Only documented Kalshi endpoints are accepted")
    return url.rstrip("/")


async def live_events(config: dict) -> AsyncIterator[dict]:
    """Concurrent read-only collectors, receipt clock, and durable health events.

    `track_tickers` lets the engine resume settlement polling for persisted open
    positions. Coinbase/Kraken raw research collectors are opt-in via
    `exchange_feeds: ["coinbase", "kraken"]`. Cancellation closes all collectors.
    """
    cfg = {**config, **config.get("feeds", {})}
    api_url = _validate_url(cfg.get("api_url", API_URL), API_HOSTS, "https", "/trade-api/v2")
    ws_url = _validate_url(cfg.get("ws_url", WS_URL), WS_HOSTS, "wss", "/trade-api/ws/v2")
    assets = cfg.get("assets", list(SERIES))
    if not assets or any(a not in SERIES for a in assets):
        raise ValueError("Supported assets are BTC, ETH, and SOL")
    overrides = load_overrides(cfg.get("market_overrides_file"))
    queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)
    known: dict[str, dict] = {}
    tracked = set(cfg.get("track_tickers", []))
    desired: set[str] = set()
    changed = asyncio.Event()
    settled: set[str] = set()
    websocket_connected = False
    stopping = False
    refresh = max(5, float(cfg.get("refresh_seconds", 30)))

    async def emit(value):
        await queue.put(value)

    async def health(component, status, reason, **details):
        await emit(event("health", details={"component": component, "status": status,
                                            "reason": reason, **details}))

    auth = None
    try:
        auth = Auth.from_env()
    except Exception as exc:
        await health("authentication", "unavailable", "invalid_local_credentials", error_type=type(exc).__name__)
    if auth is None:
        await health("kalshi_ws", "collect_only", "credentials_not_configured",
                     required_env=["KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH"])

    async def get(client, path, params=None):
        response = await client.get(api_url + path, params=params)
        response.raise_for_status()
        body = response.json()
        await emit(event("metadata", venue="kalshi", channel="rest", path=path, params=params or {}, raw=body))
        return body

    async def pages(client, path, key, params):
        cursor, output, cursors = "", [], set()
        for _ in range(100):
            body = await get(client, path, {**params, "limit": 1000, **({"cursor": cursor} if cursor else {})})
            output.extend(body[key])
            cursor = body.get("cursor", "")
            if not cursor:
                return output
            if cursor in cursors:
                raise ValueError("Repeated pagination cursor")
            cursors.add(cursor)
        raise ValueError("Pagination limit exceeded")

    async def discovery():
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            while True:
                for asset in assets:
                    try:
                        series = (await get(client, "/series/" + SERIES[asset]))["series"]
                        markets = await pages(client, "/markets", "markets",
                                              {"series_ticker": SERIES[asset], "status": "open"})
                        for ticker in sorted(tracked | set(known)):
                            if ticker in settled or not ticker.startswith(SERIES[asset] + "-"):
                                continue
                            if not any(m.get("ticker") == ticker for m in markets):
                                try:
                                    markets.append((await get(client, "/markets/" + ticker))["market"])
                                except (httpx.HTTPError, ValueError, KeyError):
                                    await health("settlement", "unavailable", "market_poll_failed", ticker=ticker)
                        for raw in markets:
                            ticker, event_ticker = raw["ticker"], raw["event_ticker"]
                            fees = None
                            try:
                                fees = await pages(client, "/events/fee_changes", "event_fee_changes", {"event_ticker": event_ticker})
                            except (httpx.HTTPError, ValueError, KeyError):
                                await health("fees", "unverified", "event_fee_metadata_unavailable", ticker=ticker)
                            market = normalize_market(raw, series, fees, cfg, asset, time.time(), overrides)
                            known[ticker] = raw
                            await emit(event("market", market=jsonable(market), venue="kalshi", channel="rest",
                                             raw={"market": raw, "series": series, "event_fee_changes": fees},
                                             rules_sha256=rule_hash(raw)))
                            if raw.get("status") in ("settled", "finalized") and raw.get("result") in ("yes", "no"):
                                if ticker not in settled:
                                    settled.add(ticker)
                                    await emit(event("settlement", ticker=ticker, result=raw["result"],
                                                     official=True, source="kalshi", source_at=market.settlement, raw=raw))
                        new_desired = {t for t, m in known.items() if t not in settled and
                                       iso_time(m["close_time"]) > time.time()}
                        if new_desired != desired:
                            desired.clear()
                            desired.update(new_desired)
                            changed.set()
                        await health("discovery", "ok", "metadata_refreshed", asset=asset, markets=len(markets))
                    except Exception as exc:
                        await health("discovery", "unavailable", "request_failed", asset=asset,
                                     error_type=type(exc).__name__,
                                     status_code=exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
                await asyncio.sleep(refresh)

    async def rest_books():
        # This fallback provides useful collection without claiming stream continuity.
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            while True:
                if not websocket_connected:
                    for ticker in sorted(desired):
                        try:
                            raw = await get(client, "/markets/" + ticker + "/orderbook")
                            book = raw.get("orderbook_fp", {})
                            payload = {"type": "orderbook_snapshot", "sid": 1, "seq": 1,
                                       "msg": {"market_ticker": ticker,
                                               "yes_dollars_fp": book.get("yes_dollars", []),
                                               "no_dollars_fp": book.get("no_dollars", [])}}
                            value = BookReconstructor().apply(payload, time.time())
                            value.update(healthy=False, raw=raw, channel="rest_orderbook",
                                         quality_flags=["polling_collect_only", "stream_continuity_unverified"])
                            await emit(value)
                        except Exception as exc:
                            await health("rest_books", "unavailable", "snapshot_failed", ticker=ticker,
                                         error_type=type(exc).__name__)
                await asyncio.sleep(max(2, float(cfg.get("rest_book_seconds", 5))))

    async def stream():
        nonlocal websocket_connected
        backoff = 1.0
        last_source: dict[str, float] = {}
        while True:
            connection_id = str(uuid.uuid4())
            reconstruction = BookReconstructor()
            connection_tickers = set(desired)
            last_book: dict[str, float] = {}
            snapshot_pending: dict[str, float] = {}
            reference_sequences: dict[int, int] = {}
            book_sid = None
            changed.clear()
            try:
                headers = auth.headers(urlparse(ws_url).path)
                async with websockets.connect(ws_url, additional_headers=headers,
                                              ping_interval=15, ping_timeout=10,
                                              open_timeout=15, max_queue=1024) as ws:
                    websocket_connected = True
                    backoff = 1
                    await health("kalshi_ws", "connected", "awaiting_snapshots", connection_id=connection_id)
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                        "channels": ["cfbenchmarks_value"], "index_ids": [INDICES[a] for a in assets]}}))
                    if connection_tickers:
                        await ws.send(json.dumps({"id": 2, "cmd": "subscribe", "params": {
                            "channels": ["orderbook_delta"], "market_tickers": sorted(connection_tickers)}}))
                    command_id = 3
                    while True:
                        if changed.is_set() and desired != connection_tickers:
                            raise FeedGap("market_subscriptions_changed")
                        try:
                            packet = await asyncio.wait_for(ws.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            raise FeedGap("stream_relevant_traffic_timeout")
                        received_at = time.time()
                        payload = json.loads(packet)
                        await emit(event("raw", venue="kalshi", channel=payload.get("type", "unknown"),
                                         connection_id=connection_id, raw=payload, received_at=received_at))
                        if payload.get("type") == "subscribed" and payload.get("msg", {}).get("channel") == "cfbenchmarks_value":
                            await ws.send(json.dumps({"id": command_id, "cmd": "update_subscription", "params": {
                                "sid": payload["msg"]["sid"], "action": "indexlist"}}))
                            command_id += 1
                        if payload.get("type") == "subscribed" and payload.get("msg", {}).get("channel") == "orderbook_delta":
                            book_sid = payload["msg"]["sid"]
                            snapshot_pending.update({t: received_at for t in connection_tickers})
                        if payload.get("type") == "cfbenchmarks_value_indexlist":
                            available = payload["msg"]["index_ids"]
                            await health("reference_entitlement", "observed", "indexlist_received",
                                         available_indices=available,
                                         missing_indices=[INDICES[a] for a in assets if INDICES[a] not in available])
                        if payload.get("type") == "error":
                            await health("kalshi_ws", "subscription_error", "server_rejected_subscription",
                                         code=payload.get("msg", {}).get("code"))
                        duplicate_reference = False
                        if payload.get("type") in {"cfbenchmarks_value", "cfbenchmarks_value_indexlist"}:
                            sid, seq = int(payload["sid"]), int(payload["seq"])
                            previous = reference_sequences.get(sid)
                            if previous is not None and seq > previous + 1:
                                raise FeedGap("reference_sequence_gap")
                            duplicate_reference = previous is not None and seq <= previous
                            if not duplicate_reference:
                                reference_sequences[sid] = seq
                        value = reconstruction.apply(payload, received_at)
                        if value:
                            ticker = value["ticker"]
                            last_book[ticker] = received_at
                            if payload.get("type") == "orderbook_snapshot":
                                snapshot_pending.pop(ticker, None)
                            if ticker in snapshot_pending:
                                value["healthy"] = False
                            value["connection_id"] = connection_id
                            await emit(value)
                        try:
                            reference = normalize_reference(payload, received_at)
                        except (ValueError, KeyError, TypeError):
                            await health("reference", "invalid", "unrecognized_reference_payload")
                            reference = None
                        if reference and not duplicate_reference and reference["source_at"] > last_source.get(reference["asset"], 0):
                            last_source[reference["asset"]] = reference["source_at"]
                            reference["connection_id"] = connection_id
                            await emit(reference)
                        # An active CF channel cannot establish book continuity. Verify
                        # each quiet book with an official snapshot; pause until received.
                        quiet = [t for t in connection_tickers if received_at - last_book.get(t, received_at) > 5
                                 and t not in snapshot_pending]
                        if quiet and book_sid:
                            for ticker in quiet:
                                snapshot_pending[ticker] = received_at
                                await emit(event("book", ticker=ticker, yes_bids=[], no_bids=[], healthy=False,
                                                 sequence=None, connection_id=connection_id,
                                                 quality_flags=["awaiting_quiet_book_verification"]))
                            await ws.send(json.dumps({"id": command_id, "cmd": "update_subscription", "params": {
                                "sid": book_sid, "action": "get_snapshot", "market_tickers": sorted(quiet)}}))
                            command_id += 1
                        if any(received_at - requested > 5 for requested in snapshot_pending.values()):
                            raise FeedGap("book_snapshot_timeout")
                        await health("kalshi_ws", "ok", "message_received", connection_id=connection_id)
            except Exception as exc:
                await health("kalshi_ws", "disconnected", str(exc) if isinstance(exc, FeedGap) else "connection_failed",
                             error_type=type(exc).__name__, connection_id=connection_id)
            finally:
                websocket_connected = False
                if not stopping:
                    for ticker in sorted(connection_tickers):
                        await emit(event("book", ticker=ticker, yes_bids=[], no_bids=[], healthy=False,
                                         sequence=None, connection_id=connection_id,
                                         quality_flags=["stream_disconnected"]))
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)

    async def clock():
        while True:
            await asyncio.sleep(max(0.001, 1 - time.time() % 1))
            await emit(event("clock"))

    async def exchange(venue):
        # Raw side-channel only: no reference events and no baseline signal substitution.
        urls = {"coinbase": "wss://advanced-trade-ws.coinbase.com", "kraken": "wss://ws.kraken.com/v2"}
        backoff = 1
        while True:
            connection_id = str(uuid.uuid4())
            try:
                async with websockets.connect(urls[venue], ping_interval=20, ping_timeout=10, open_timeout=15) as ws:
                    products = [a + "-USD" for a in assets] if venue == "coinbase" else [a + "/USD" for a in assets]
                    channels = ("market_trades", "level2", "heartbeats") if venue == "coinbase" else ("trade", "book")
                    for channel in channels:
                        subscription = ({"type": "subscribe", "product_ids": products, "channel": channel}
                                        if venue == "coinbase" else {"method": "subscribe", "params": {
                                            "channel": channel, "symbol": products, "snapshot": True}})
                        await ws.send(json.dumps(subscription))
                    backoff = 1
                    await health(venue, "connected", "research_collection_only", connection_id=connection_id)
                    async for packet in ws:
                        payload = json.loads(packet)
                        await emit(event("exchange", venue=venue, channel=payload.get("channel", "unknown"),
                                         connection_id=connection_id, raw=payload,
                                         quality_flags=["research_raw_only", "integrity_unvalidated"]))
            except Exception as exc:
                await health(venue, "disconnected", "connection_failed", error_type=type(exc).__name__)
            await asyncio.sleep(backoff)
            backoff = min(60, backoff * 2)

    venues = cfg.get("exchange_feeds", [])
    if any(venue not in ("coinbase", "kraken") for venue in venues):
        raise ValueError("Supported research feeds: coinbase, kraken")
    collectors = [asyncio.create_task(discovery()), asyncio.create_task(rest_books()), asyncio.create_task(clock())]
    if auth:
        collectors.append(asyncio.create_task(stream()))
    for venue in venues:
        collectors.append(asyncio.create_task(exchange(venue)))
    try:
        while True:
            value = await queue.get()
            value["processing_at"] = time.time()
            value["processing_delay_ms"] = max(0, (time.monotonic() - value["monotonic_at"]) * 1000)
            yield value
    finally:
        stopping = True
        for task in collectors:
            task.cancel()
        for task in collectors:
            with contextlib.suppress(asyncio.CancelledError):
                await task
