"""Read-only local dashboard. This module cannot submit orders or start an engine."""
from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .models import D, book_from_dict, market_from_dict

ASSETS = ("BTC", "ETH", "SOL")
STATIC = Path(__file__).with_name("static")
TRADE_FIELDS = ("id", "run_id", "market_ticker", "asset", "side", "quantity",
                "remaining_quantity", "entry_cost", "remaining_cost", "realized_pnl",
                "opened_at", "closed_at", "settlement_status")


def _json(value: str | bytes | None, fallback: Any = None) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (ValueError, TypeError):
        return fallback


def _decimal(value: Any) -> Decimal:
    try:
        result = D(value)
        return result if result.is_finite() else D(0)
    except (InvalidOperation, TypeError, ValueError):
        return D(0)


def _clean(value: Any) -> Any:
    """JSON cannot carry Infinity/NaN; retain missing observations as null."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, Decimal):
        return str(value) if value.is_finite() else None
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_clean(v) for v in value]
    return value


@contextmanager
def _database(path: Path):
    # mode=ro prevents accidental creation, while as_uri escapes '?' and '#'.
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(connection: sqlite3.Connection, table: str, columns: str = "*", limit: int = 3000) -> list[dict]:
    # Only internal constant table/column identifiers reach this function.
    return [dict(row) for row in connection.execute(f"SELECT {columns} FROM {table} ORDER BY rowid DESC LIMIT ?", (limit,))]


def _status(path: Path) -> dict:
    if not path.is_file():
        return {"status": "uninitialized", "read_only": True, "detail": "Database not created. Start the paper engine first."}
    try:
        with _database(path) as connection:
            tables = _tables(connection)
            required = {"accounts", "orders", "positions", "fills", "ledger", "events", "decisions", "markets", "run_config"}
            missing = sorted(required - tables)
            if missing:
                return {"status": "uninitialized", "read_only": True, "detail": "Paper schema incomplete.", "missing_tables": missing}
            if connection.execute("SELECT 1 FROM accounts WHERE id=1").fetchone() is None:
                return {"status": "uninitialized", "read_only": True, "detail": "Paper account not initialized."}
            return {"status": "ready", "read_only": True}
    except sqlite3.Error:
        return {"status": "unavailable", "read_only": True, "detail": "Database is unreadable or temporarily busy."}


def _event_data(row: dict) -> dict:
    payload = _json(row.get("payload"), {})
    if not isinstance(payload, dict):
        return {}
    # Receipt event envelopes may carry the typed data under data or payload.
    for key in ("data", "payload"):
        if isinstance(payload.get(key), dict):
            return {**payload, **payload[key]}
    return payload


def _asset_for(payload: dict, market_assets: dict[str, str]) -> str | None:
    asset = str(payload.get("asset", "")).upper()
    if asset in ASSETS:
        return asset
    ticker = payload.get("ticker", payload.get("market_ticker", ""))
    return market_assets.get(ticker)


def _best_quotes(book: dict) -> dict:
    try:
        parsed = book_from_dict(book)
        yes_bid = max((level.price for level in parsed.yes_bids if level.quantity > 0), default=None)
        no_bid = max((level.price for level in parsed.no_bids if level.quantity > 0), default=None)
        return {"yes_bid": yes_bid, "yes_ask": 1 - no_bid if no_bid is not None else None,
                "no_bid": no_bid, "no_ask": 1 - yes_bid if yes_bid is not None else None}
    except (KeyError, ValueError, TypeError, InvalidOperation):
        return {"yes_bid": None, "yes_ask": None, "no_bid": None, "no_ask": None}


def _market_state(connection: sqlite3.Connection, now: float) -> tuple[list[dict], dict[str, dict], list[dict]]:
    markets = {row["ticker"]: _json(row["payload"], {}) for row in _rows(connection, "markets")}
    market_assets = {ticker: value.get("asset") for ticker, value in markets.items()}
    latest_decisions: dict[str, dict] = {}
    for row in _rows(connection, "decisions", limit=1800):
        payload = _json(row["payload"], {})
        ticker = row["market_ticker"]
        asset = market_assets.get(ticker) or _asset_for(payload, market_assets)
        if asset in ASSETS and asset not in latest_decisions:
            latest_decisions[asset] = {**payload, "received_at": row["received_at"], "ticker": ticker}
    histories: dict[str, list] = {asset: [] for asset in ASSETS}
    books: dict[str, dict] = {}
    book_histories: dict[str, list] = {asset: [] for asset in ASSETS}
    health: list[dict] = []
    selected_tickers = {}
    for asset in ASSETS:
        candidates = [(ticker, value) for ticker, value in markets.items() if value.get("asset") == asset]
        candidates.sort(key=lambda pair: pair[1].get("cutoff", 0), reverse=True)
        selected_tickers[asset] = latest_decisions[asset]["ticker"] if asset in latest_decisions else (candidates[0][0] if candidates else None)
    events = []
    # Query each stream separately: a busy exchange stream must not hide the last
    # official reference or book observation for a quieter asset.
    for asset in ASSETS:
        events.extend(dict(row) for row in reversed(connection.execute(
            "SELECT * FROM events WHERE type='reference' AND json_valid(payload) "
            "AND json_extract(payload,'$.asset')=? ORDER BY id DESC LIMIT 240", (asset,)).fetchall()))
    tickers = {ticker for ticker in selected_tickers.values() if ticker}
    tickers.update(row[0] for row in connection.execute("SELECT DISTINCT market_ticker FROM positions WHERE remaining_quantity>0"))
    for ticker in tickers:
        events.extend(dict(row) for row in reversed(connection.execute(
            "SELECT * FROM events WHERE type='book' AND json_valid(payload) "
            "AND json_extract(payload,'$.ticker')=? ORDER BY id DESC LIMIT 240", (ticker,)).fetchall()))
    events.extend(dict(row) for row in reversed(connection.execute(
        "SELECT * FROM events WHERE type IN ('health','gap','disconnect','reconnect','heartbeat') ORDER BY id DESC LIMIT 30").fetchall()))
    for row in events:
        payload = _event_data(row)
        kind = str(row["type"]).lower()
        asset = _asset_for(payload, market_assets)
        received_at = row["received_at"]
        if ("reference" in kind or "cfbenchmark" in kind) and asset and "price" in payload:
            histories[asset].append({"at": received_at, "price": payload["price"], "source_at": payload.get("source_at")})
        if "book" in kind and ("yes_bids" in payload or "no_bids" in payload):
            ticker = payload.get("ticker", payload.get("market_ticker"))
            book = {**payload, "ticker": ticker, "received_at": received_at}
            books[ticker] = book
            if asset:
                book_histories[asset].append({"at": received_at, "ticker": ticker, **_best_quotes(book)})
        if any(name in kind for name in ("health", "gap", "disconnect", "reconnect", "heartbeat")):
            health.append({"type": row["type"], "received_at": received_at, "age_seconds": max(0, now - received_at), "details": payload})
    cards = []
    for asset in ASSETS:
        decision = latest_decisions.get(asset)
        ticker = selected_tickers[asset]
        market = markets.get(ticker, {})
        history = histories[asset][-240:]
        reference = history[-1] if history else None
        age = max(0, now - reference["at"]) if reference else None
        book = books.get(ticker)
        book_age = max(0, now - book["received_at"]) if book else None
        reasons = decision.get("reasons", []) if decision else ["no_decision_recorded"]
        cutoff = market.get("cutoff")
        end = market.get("observation_end")
        try:
            settlement_progress = min(1, max(0, (now - (float(end) - 60)) / 60)) if end else None
        except (TypeError, ValueError):
            settlement_progress = None
        settlement_row = connection.execute(
            "SELECT payload,received_at FROM events WHERE type='settlement' AND json_valid(payload) "
            "AND json_extract(payload,'$.ticker')=? "
            "AND (json_extract(payload,'$.official')=1 OR json_extract(payload,'$.dataset')='demo') "
            "ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        settlement = None
        if settlement_row:
            result = _json(settlement_row["payload"], {})
            settlement = {"result": result.get("result"), "dataset": result.get("dataset", "real"),
                          "official": result.get("official") is True, "received_at": settlement_row["received_at"]}
        cards.append({"asset": asset, "ticker": ticker, "market": market, "decision": decision,
                      "recorded_eligible": bool(decision and not reasons and decision.get("side")),
                      "eligible": bool(decision and not reasons and decision.get("side") and now - decision["received_at"] <= 2
                                       and age is not None and age <= 2 and book is not None and book.get("healthy", True) and book_age <= 5),
                      "decision_age_seconds": max(0, now - decision["received_at"]) if decision else None,
                      "reasons": reasons, "reference": reference, "reference_age_seconds": age,
                      "reference_stale": age is None or age > 2,
                      "book_age_seconds": book_age, "book_stale": book is None or not book.get("healthy", True) or book_age > 5,
                      "quotes": _best_quotes(book) if book else {},
                      "reference_history": history,
                      "book_history": [point for point in book_histories[asset] if point["ticker"] == ticker][-240:],
                      "seconds_to_cutoff": float(cutoff) - now if cutoff else None,
                      "settlement": settlement,
                      "settlement_window_progress": settlement_progress})
    return cards, books, health[-30:]


def _mark(position: dict, books: dict, now: float, config: dict) -> dict:
    quantity = position["remaining_quantity"]
    if not quantity:
        return {"liquidation_value": "0", "priced_quantity": 0, "unpriced_quantity": 0, "valuation": "closed"}
    book = books.get(position["market_ticker"])
    unavailable = {"liquidation_value": "0", "priced_quantity": 0, "unpriced_quantity": quantity, "valuation": "unpriced_zero_mark"}
    if not book or not book.get("healthy", True) or now - book["received_at"] > 5:
        return unavailable
    try:
        from .strategy import plan_exit
        market = market_from_dict(_json(position["market_json"], {}))
        if now >= market.cutoff:
            return {**unavailable, "valuation": "awaiting_official_settlement_zero_mark"}
        depth = _decimal(config.get("depth_fraction", ".5"))
        quote = plan_exit(book_from_dict(book), position["side"], quantity, market.fee,
                          depth_fraction=depth, min_price=D(0), tick_size=market.tick_size,
                          price_ranges=market.price_ranges)
        if quote is None:
            return unavailable
        return {"liquidation_value": str(quote.proceeds), "priced_quantity": quote.quantity,
                "unpriced_quantity": quantity - quote.quantity,
                "valuation": "executable_fee_net" if quote.quantity == quantity else "partial_depth_remainder_zero_mark"}
    except (ImportError, KeyError, ValueError, TypeError, InvalidOperation):
        return unavailable


def _api_connection(connection: sqlite3.Connection, data_mode: str, now: float) -> dict:
    """Report received API evidence, never infer a connection from a local DB."""
    base = {"status": "waiting", "transport": None, "last_response_at": None,
            "response_age_seconds": None, "last_book_at": None, "book_age_seconds": None,
            "market_count": 0, "book_observations": 0, "websocket_status": "not_observed"}
    if data_mode != "live_collected":
        return {**base, "status": data_mode}
    rest = connection.execute(
        "SELECT MAX(received_at) FROM events WHERE type='metadata' AND json_valid(payload) "
        "AND json_extract(payload,'$.venue')='kalshi' "
        "AND json_extract(payload,'$.channel')='rest'").fetchone()[0]
    book_count, book_at = connection.execute(
        "SELECT COUNT(*),MAX(received_at) FROM events WHERE type='book' AND json_valid(payload) "
        "AND json_extract(payload,'$.venue')='kalshi'").fetchone()
    ws = connection.execute(
        "SELECT received_at,payload FROM events WHERE type='health' AND json_valid(payload) "
        "AND json_extract(payload,'$.details.component')='kalshi_ws' ORDER BY id DESC LIMIT 1").fetchone()
    ws_details = _json(ws["payload"], {}).get("details", {}) if ws else {}
    ws_ok = bool(ws and 0 <= now - ws["received_at"] <= 10
                 and ws_details.get("status") in {"connected", "ok"})
    response_at = max([value for value in (rest, ws["received_at"] if ws_ok else None)
                       if value is not None], default=None)
    age = max(0, now - response_at) if response_at is not None else None
    return {**base, "status": "connected" if age is not None and age <= 90 else "stale" if age is not None else "waiting",
            "transport": "websocket" if ws_ok else "rest" if rest is not None else None,
            "last_response_at": response_at, "response_age_seconds": age,
            "last_book_at": book_at, "book_age_seconds": max(0, now - book_at) if book_at is not None else None,
            "market_count": connection.execute("SELECT COUNT(*) FROM markets").fetchone()[0],
            "book_observations": book_count, "websocket_status": ws_details.get("status", "not_observed")}


def _state(path: Path) -> dict:
    now = time.time()
    status = _status(path)
    base = {"paper": True, "data_mode": "uninitialized", "generated_at": now, "database": status,
            "summary": None, "assets": [], "positions": [], "feed_health": [], "run": None,
            "api_connection": {"status": "waiting"}}
    if status["status"] != "ready":
        return base
    try:
        with _database(path) as connection:
            row = connection.execute("SELECT * FROM run_config ORDER BY started_at DESC LIMIT 1").fetchone()
            run = dict(row) if row else None
            config = _json(run.get("config_json"), {}) if run else {}
            cards, books, health = _market_state(connection, now)
            fill_markers = [dict(row) for row in connection.execute(
                "SELECT f.kind,f.price,f.quantity,f.created_at,p.asset,p.side,p.market_ticker FROM fills f "
                "JOIN positions p ON p.id=f.position_id ORDER BY f.created_at DESC LIMIT 1000")]
            for card in cards:
                card["fills"] = [fill for fill in fill_markers if fill["asset"] == card["asset"] and fill["market_ticker"] == card["ticker"]]
            positions = [dict(row) for row in connection.execute("SELECT * FROM positions WHERE remaining_quantity>0 ORDER BY opened_at DESC")]
            for position in positions:
                position.update(_mark(position, books, now, config))
                position.pop("market_json", None)
            account = dict(connection.execute("SELECT * FROM accounts WHERE id=1").fetchone())
            cash = _decimal(account["cash"])
            reserved = sum((_decimal(row[0]) for row in connection.execute("SELECT amount FROM orders WHERE status='pending'")), D(0))
            realized = sum((_decimal(row[0]) for row in connection.execute("SELECT realized_pnl FROM ledger")), D(0))
            liquidation = sum((_decimal(position["liquidation_value"]) for position in positions), D(0))
            fees = sum((_decimal(row[0]) for row in connection.execute("SELECT fee FROM fills")), D(0))
            open_positions = [position for position in positions if position["remaining_quantity"] > 0]
            pending = 0
            for position in open_positions:
                market_row = connection.execute("SELECT payload FROM markets WHERE ticker=?", (position["market_ticker"],)).fetchone()
                market = _json(market_row[0], {}) if market_row else {}
                if market.get("cutoff") is not None and now >= float(market["cutoff"]):
                    pending += 1
            summary = {"initial_cash": account["initial_cash"], "cash": str(cash), "cash_reserved": str(reserved),
                       "available_cash": str(cash - reserved), "equity": str(cash + liquidation),
                       "realized_pnl": str(realized), "net_pnl": str(cash + liquidation - _decimal(account["initial_cash"])),
                       "open_positions": len(open_positions), "pending_settlement": pending,
                       "fees_paid": str(fees), "unpriced_quantity": sum(p["unpriced_quantity"] for p in positions),
                       "worst_case_open_loss": str(sum((_decimal(p["remaining_cost"]) for p in open_positions), D(0))),
                       "valuation_policy": "Fee-net executable bids; unavailable remainder marked zero. Marks are not fills."}
            mode = str(config.get("mode", config.get("data_mode", "unknown"))).lower()
            dataset = str(config.get("dataset", "unknown")).lower()
            labels = {_json(row[0], {}).get("dataset", _json(row[0], {}).get("mode", "unknown"))
                      for row in connection.execute("SELECT config_json FROM run_config")}
            mixed = bool(labels & {"demo", "synthetic"}) and bool(labels & {"real", "paper", "live", "collect"})
            data_mode = ("mixed_demo_and_real" if mixed else "demo" if mode in {"demo", "synthetic"} or dataset in {"demo", "synthetic"}
                         else "replay" if mode == "replay" else "live_collected" if mode in {"collect", "live", "paper"} else "unknown")
            # Do not expose complete config: future configs might contain credentials.
            safe_run = {"run_id": run.get("run_id"), "started_at": run.get("started_at")} if run else None
            return _clean({**base, "data_mode": data_mode, "summary": summary, "assets": cards,
                           "positions": positions, "feed_health": health, "run": safe_run,
                           "api_connection": _api_connection(connection, data_mode, now)})
    except (sqlite3.Error, ValueError, TypeError):
        return {**base, "database": {"status": "unavailable", "read_only": True, "detail": "Database snapshot could not be read."}}


def _filters(asset, side, status, started_after, started_before):
    if started_after is not None and started_before is not None and started_after > started_before:
        raise HTTPException(422, "started_after must be no later than started_before")
    conditions, params = [], []
    for column, value in (("asset", asset), ("side", side), ("settlement_status", status)):
        if value is not None:
            conditions.append(f"{column}=?")
            params.append(value)
    if started_after is not None:
        conditions.append("opened_at>=?")
        params.append(started_after)
    if started_before is not None:
        conditions.append("opened_at<=?")
        params.append(started_before)
    return (" WHERE " + " AND ".join(conditions) if conditions else ""), params


def _trades(path, asset, side, status, started_after, started_before, limit, offset):
    status_db = _status(path)
    if status_db["status"] != "ready":
        raise HTTPException(503, status_db["detail"])
    where, params = _filters(asset, side, status, started_after, started_before)
    try:
        with _database(path) as connection:
            total = connection.execute("SELECT COUNT(*) FROM positions" + where, params).fetchone()[0]
            rows = connection.execute("SELECT " + ",".join(TRADE_FIELDS) + " FROM positions" + where + " ORDER BY opened_at DESC,id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
            return {"total": total, "limit": limit, "offset": offset, "items": [dict(row) for row in rows]}
    except sqlite3.Error:
        raise HTTPException(503, "Database snapshot could not be read.") from None


def _csv_cell(value):
    # A leading control/space can hide a spreadsheet formula marker.
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _chart_markets(connection, asset):
    result = []
    for row in connection.execute("SELECT ticker,payload FROM markets WHERE json_valid(payload) "
                                  "AND json_extract(payload,'$.asset')=? "
                                  "ORDER BY json_extract(payload,'$.open_time') DESC LIMIT 100", (asset,)):
        market = _json(row["payload"], {})
        result.append({"ticker": row["ticker"], "open_time": market.get("open_time"),
                       "cutoff": market.get("cutoff"), "strike": market.get("strike")})
    return result


def _chart_data(path: Path, asset: str, ticker: str | None, window: str):
    if _status(path)["status"] != "ready":
        raise HTTPException(503, "The bot is preparing its database. The chart will refresh automatically.")
    now = time.time()
    with _database(path) as connection:
        available = _chart_markets(connection, asset)
        if not available:
            if ticker is not None:
                raise HTTPException(404, "That market is not available for the selected asset.")
            return {"asset": asset, "market": None, "markets": [], "points": [], "references": [],
                    "fills": [], "dataset": "unknown", "status": "waiting", "generated_at": now}
        if ticker is None:
            active = [m for m in available if (m.get("open_time") or 0) <= now < (m.get("cutoff") or 0)]
            past = [m for m in available if (m.get("open_time") or 0) <= now]
            ticker = (active or past or available)[0]["ticker"]
        if ticker not in {m["ticker"] for m in available}:
            raise HTTPException(404, "That market is not available for the selected asset.")
        row = connection.execute("SELECT payload FROM markets WHERE ticker=?", (ticker,)).fetchone()
        market = _json(row[0], {})
        opened, cutoff = market.get("open_time"), market.get("cutoff")
        if (not isinstance(opened, (int, float)) or not isinstance(cutoff, (int, float))
                or not math.isfinite(opened) or not math.isfinite(cutoff) or cutoff <= opened):
            raise HTTPException(422, "This market has no verified chart time range yet.")
        labels = {_json(row[0], {}).get("dataset", _json(row[0], {}).get("mode", "unknown"))
                  for row in connection.execute("SELECT config_json FROM run_config")}
        demo = bool(labels & {"demo", "synthetic"})
        real = bool(labels & {"real", "paper", "live", "collect"})
        dataset = "mixed" if demo and real else "demo" if demo else "real" if real else "unknown"
        latest_row = connection.execute("SELECT received_at,payload FROM events WHERE type='book' "
                                         "AND json_valid(payload) AND json_extract(payload,'$.ticker')=? "
                                         "ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
        latest_at = latest_row["received_at"] if latest_row else None
        end = max(opened + 1, min(cutoff, latest_at if latest_at is not None else now))
        seconds = {"1m": 60, "5m": 300, "15m": 900}.get(window)
        start = max(opened, end - seconds) if seconds else opened

        def sampled(kind, field, identity):
            # One last observation per received second; retain empty books so
            # the frontend breaks the line at known disconnects.
            query = ("SELECT e.received_at,e.payload FROM events e JOIN ("
                     "SELECT MAX(id) AS id FROM events WHERE type=? AND json_valid(payload) "
                     f"AND json_extract(payload,'$.{field}')=? AND received_at>=? AND received_at<=? "
                     "GROUP BY CAST(received_at AS INTEGER)) s ON s.id=e.id "
                     "ORDER BY e.received_at,e.id LIMIT 1200")
            return connection.execute(query, (kind, identity, start, end)).fetchall()

        yes_up = market.get("yes_means_up", True)
        points = []
        for point in sampled("book", "ticker", ticker):
            book = _json(point["payload"], {})
            quotes = _best_quotes(book)
            up, down = ("yes", "no") if yes_up else ("no", "yes")
            points.append({"at": point["received_at"], "up_bid": quotes[up + "_bid"],
                           "up_ask": quotes[up + "_ask"], "down_bid": quotes[down + "_bid"],
                           "down_ask": quotes[down + "_ask"], "healthy": bool(book.get("healthy", False))})
        references = []
        for point in sampled("reference", "asset", asset):
            value = _json(point["payload"], {})
            if value.get("source") == "cfbenchmarks_value":
                references.append({"at": point["received_at"], "price": value.get("price"),
                                   "source_at": value.get("source_at")})
        fills = []
        for fill in connection.execute("SELECT f.*,p.side FROM fills f JOIN positions p ON p.id=f.position_id "
                                       "WHERE p.market_ticker=? AND f.created_at>=? "
                                       "ORDER BY f.created_at,f.id", (ticker, start)):
            direction = "UP" if (fill["side"] == "yes") == yes_up else "DOWN"
            action = {"entry": "BUY", "exit": "SELL", "settlement": "SETTLED"}.get(fill["kind"], "FILL")
            fills.append({"id": fill["id"], "at": fill["created_at"], "kind": fill["kind"],
                          "action": action, "direction": direction, "side": fill["side"],
                          "price": fill["price"], "quantity": fill["quantity"], "fee": fill["fee"],
                          "label": f"{action} {direction}"})
        fresh = latest_at is not None and 0 <= now - latest_at <= 5
        healthy = bool(latest_row and _json(latest_row["payload"], {}).get("healthy", False))
        status = ("demo" if dataset == "demo" else "mixed" if dataset == "mixed" else
                  "live" if fresh and healthy else "polling" if fresh else "stale")
        demo_complete = dataset == "demo" and connection.execute(
            "SELECT 1 FROM events WHERE type='settlement' AND json_extract(payload,'$.ticker')=? "
            "AND json_extract(payload,'$.dataset')='demo' LIMIT 1", (ticker,)).fetchone() is not None
        return _clean({"asset": asset, "market": market, "markets": available, "points": points,
                       "references": references, "fills": fills, "dataset": dataset, "status": status,
                       "range_start": start, "range_end": end, "latest_received_at": latest_at,
                       "generated_at": now, "poll_interval_ms": 1000,
                       "demo_complete": demo_complete,
                       "quote_label": "Observed executable bids and asks; fills include their actual simulated prices."})


def create_app(database: Path | str) -> FastAPI:
    """Serve one fixed database path; no HTTP request can select another file."""
    path = Path(database).expanduser().resolve()
    app = FastAPI(title="Kalshi Paper Research", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/health")
    def health():
        from .desktop import APP_ID, database_id
        database_status = _status(path)
        return JSONResponse({"paper": True, "app_id": APP_ID, "database_id": database_id(path),
                             "database": database_status}, status_code=200 if database_status["status"] == "ready" else 503)

    @app.get("/api/state")
    def state():
        return _state(path)

    @app.get("/api/chart")
    def market_chart(asset: Literal["BTC", "ETH", "SOL"] = "BTC",
                     ticker: str | None = Query(None, min_length=1, max_length=200),
                     window: Literal["round", "1m", "5m", "15m"] = "round"):
        try:
            return _chart_data(path, asset, ticker, window)
        except sqlite3.Error:
            raise HTTPException(503, "Chart data is temporarily unavailable; retrying automatically.") from None

    @app.get("/api/trades")
    def trades(asset: Literal["BTC", "ETH", "SOL"] | None = None,
               side: Literal["yes", "no"] | None = None,
               status: Literal["pending", "sold", "settled"] | None = None,
               started_after: float | None = Query(None, ge=0, allow_inf_nan=False),
               started_before: float | None = Query(None, ge=0, allow_inf_nan=False),
               limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
        return _trades(path, asset, side, status, started_after, started_before, limit, offset)

    @app.get("/api/analytics")
    def analytics():
        if _status(path)["status"] != "ready":
            raise HTTPException(503, "Paper database is not initialized.")
        from .research import analyze
        return _clean(analyze(path))

    @app.get("/api/experiments")
    def experiments():
        if _status(path)["status"] != "ready":
            raise HTTPException(503, "Paper database is not initialized.")
        from .research import compare_exits
        return _clean(compare_exits(path))

    @app.get("/api/export.{format}")
    def export(format: Literal["csv", "json"], asset: Literal["BTC", "ETH", "SOL"] | None = None,
               side: Literal["yes", "no"] | None = None,
               status: Literal["pending", "sold", "settled"] | None = None,
               started_after: float | None = Query(None, ge=0, allow_inf_nan=False),
               started_before: float | None = Query(None, ge=0, allow_inf_nan=False)):
        result = _trades(path, asset, side, status, started_after, started_before, 100001, 0)
        if result["total"] > 100000:
            raise HTTPException(413, "Export exceeds 100,000 trades. Narrow the filters.")
        if format == "json":
            return JSONResponse(result, headers={"Content-Disposition": 'attachment; filename="paper-trades.json"'})
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(TRADE_FIELDS)
        for row in result["items"]:
            writer.writerow([_csv_cell(row.get(field)) for field in TRADE_FIELDS])
        return Response(output.getvalue(), media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="paper-trades.csv"'})

    return app
