"""Receipt-ordered event processing with atomic paper account transitions."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
import gzip
import hashlib
from importlib.metadata import version
import json
import math
import platform
from pathlib import Path
from uuid import uuid4

from .config import BotConfig, config_hash
from .ledger import Ledger
from .models import D, ReferenceTick, book_from_dict, jsonable, market_from_dict
from .strategy import bid_levels, evaluate, plan_entry, plan_exit


class Engine:
    def __init__(self, directory: str | Path, config: BotConfig, mode: str = "paper",
                 dataset: str | None = None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.mode = mode
        self.dataset = dataset or ("demo" if mode == "demo" else "real")
        self.ledger = Ledger(self.directory / "paper.sqlite3")
        self.conn = self.ledger.conn
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS run_config (
                run_id TEXT PRIMARY KEY, started_at REAL NOT NULL, config_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, run_id TEXT NOT NULL,
                type TEXT NOT NULL, received_at REAL NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_kind ON events(type, received_at);
            CREATE INDEX IF NOT EXISTS events_asset ON events(type,json_extract(payload,'$.asset'),id DESC);
            CREATE INDEX IF NOT EXISTS events_ticker ON events(type,json_extract(payload,'$.ticker'),id DESC);
            CREATE TABLE IF NOT EXISTS markets (ticker TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, market_ticker TEXT NOT NULL,
                received_at REAL NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS decisions_latest ON decisions(market_ticker,received_at);
            CREATE TABLE IF NOT EXISTS trajectories (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, position_id TEXT NOT NULL,
                received_at REAL NOT NULL, payload TEXT NOT NULL,
                UNIQUE(run_id, position_id, received_at)
            );
            CREATE TABLE IF NOT EXISTS opening_references (
                market_ticker TEXT NOT NULL, asset TEXT NOT NULL, price TEXT NOT NULL,
                PRIMARY KEY(market_ticker,asset)
            );
            CREATE TABLE IF NOT EXISTS exit_intents (
                position_id TEXT PRIMARY KEY, arrival_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_context (
                id INTEGER PRIMARY KEY, order_id TEXT, position_id TEXT,
                received_at REAL NOT NULL, kind TEXT NOT NULL,
                market_json TEXT NOT NULL, quote_json TEXT NOT NULL
            );
        """)
        code_hash = hashlib.sha256()
        for file in sorted(Path(__file__).parent.glob("*.py")):
            code_hash.update(file.name.encode() + file.read_bytes())
        self.run_settings = {**config.to_dict(), "mode": mode, "dataset": self.dataset,
                             "strategy_version": "baseline-1", "execution_version": "ioc-1",
                             "fee_model_version": "quadratic-rounding-scenario-1",
                             "code_hash": code_hash.hexdigest(),
                             "paper_only": True}
        lockfile = Path(__file__).parent.parent / "uv.lock"
        self.run_settings["dependency_lock_sha256"] = (hashlib.sha256(lockfile.read_bytes()).hexdigest()
                                                       if lockfile.is_file() else None)
        self.run_settings["python_version"] = platform.python_version()
        self.run_settings["package_versions"] = {name: version(name) for name in
                                                ("httpx", "websockets", "cryptography", "fastapi", "uvicorn", "pyarrow")}
        self.frozen_overrides = None
        overrides_bytes = None
        if config.market_overrides_file:
            overrides_bytes = Path(config.market_overrides_file).read_bytes()
            self.run_settings["market_overrides_sha256"] = hashlib.sha256(overrides_bytes).hexdigest()
        self.run_settings["config_hash"] = config_hash(self.run_settings)
        prior = self.conn.execute("SELECT * FROM run_config ORDER BY rowid DESC LIMIT 1").fetchone()
        if prior:
            previous = json.loads(prior["config_json"])
            if previous.get("config_hash") != self.run_settings["config_hash"]:
                self.ledger.close()
                raise ValueError("This data directory belongs to a different frozen configuration/code version. "
                                 "Use a new --data-dir; keep the previous run for research.")
            self.run_id = prior["run_id"]
        else:
            self.run_id = uuid4().hex
            self.conn.execute("INSERT INTO run_config VALUES (?,?,?)",
                              (self.run_id, 0, json.dumps(self.run_settings, sort_keys=True)))
        self.markets = {}
        self.books = {}
        self.references = defaultdict(lambda: deque(maxlen=12000))
        self.last_clock = -1
        self.last_received = 0.0
        self.processing_delay_ms = 0.0
        self.settled_tickers = set()
        for row in self.conn.execute("SELECT payload FROM markets"):
            market = market_from_dict(json.loads(row[0]))
            self.markets[market.ticker] = market
        # Restore observation state only. Cash/order transitions are already committed.
        for row in self.conn.execute("SELECT payload FROM events ORDER BY id"):
            self._apply_data(json.loads(row[0]), restoring=True)
        self.resume_reset = bool(prior and mode == "paper" and self.last_received)
        if self.resume_reset:
            self.books = {ticker: replace(book, healthy=False) for ticker, book in self.books.items()}
            self.references.clear()
        self.raw_directory = self.directory / "raw"
        self.raw_directory.mkdir(exist_ok=True)
        (self.directory / "run.json").write_text(json.dumps(self.run_settings, indent=2, sort_keys=True) + "\n")
        if overrides_bytes is not None:
            self.frozen_overrides = self.directory / "market-overrides.json"
            self.frozen_overrides.write_bytes(overrides_bytes)
        self.failed = False

    def close(self):
        self.ledger.close()

    def _apply_data(self, event: dict, restoring: bool = False):
        kind = event["type"]
        now = float(event["received_at"])
        if event.get("process_restart"):
            self.books = {ticker: replace(book, healthy=False) for ticker, book in self.books.items()}
            self.references.clear()
        self.processing_delay_ms = float(event.get("processing_delay_ms", 0))
        self.last_received = max(self.last_received, now)
        if kind == "market":
            market = market_from_dict(event["market"])
            if market.ticker not in self.settled_tickers:
                self.markets[market.ticker] = market
            if not restoring:
                self.conn.execute("INSERT INTO markets VALUES (?,?) ON CONFLICT(ticker) "
                                  "DO UPDATE SET payload=excluded.payload",
                                  (market.ticker, json.dumps(jsonable(market), sort_keys=True)))
        elif kind == "reference":
            price = D(event["price"])
            if not price.is_finite() or price <= 0:
                return
            tick = ReferenceTick(event["asset"], price, float(event["source_at"]), now,
                                 event.get("source", "unknown"))
            self.references[tick.asset].append(tick)
        elif kind == "book":
            book = book_from_dict(event)
            prior = self.books.get(book.ticker)
            if prior is None or book.received_at >= prior.received_at:
                self.books[book.ticker] = book
        elif kind == "health" and event.get("details", {}).get("status") in {
                "disconnected", "gap", "error", "unavailable"}:
            component = event.get("details", {}).get("component", "")
            if "kalshi" in component or "websocket" in component:
                self.books = {ticker: replace(book, healthy=False)
                              for ticker, book in self.books.items()}
                self.references.clear()
        elif kind == "settlement" and (event.get("official") is True or self.dataset == "demo"):
            self.settled_tickers.add(event["ticker"])
            self.markets.pop(event["ticker"], None)
            self.books.pop(event["ticker"], None)
        elif kind == "clock" and restoring:
            self.last_clock = max(self.last_clock, int(now))
        if not restoring and kind in {"market", "reference"}:
            self._freeze_opening_references(now)

    def _freeze_opening_references(self, now):
        for market in self.markets.values():
            if now < market.open_time or now > market.cutoff or market.ticker in self.settled_tickers:
                continue
            for asset in {"BTC", market.asset}:
                if self.conn.execute("SELECT 1 FROM opening_references WHERE market_ticker=? "
                                     "AND asset=?", (market.ticker, asset)).fetchone():
                    continue
                candidates = [tick for tick in self.references[asset]
                              if tick.source == "cfbenchmarks_value"
                              and tick.received_at <= market.open_time
                              and 0 <= market.open_time - tick.source_at <= 2]
                if candidates:
                    price = max(candidates, key=lambda tick: (tick.source_at, tick.received_at)).price
                    self.conn.execute("INSERT OR IGNORE INTO opening_references VALUES (?,?,?)",
                                      (market.ticker, asset, str(price)))

    def _threshold(self, market):
        if market.asset == "BTC":
            return D(5)
        values = {row[0]: D(row[1]) for row in self.conn.execute(
            "SELECT asset,price FROM opening_references WHERE market_ticker=?", (market.ticker,))}
        if "BTC" not in values or market.asset not in values:
            return None
        return D(5) / values["BTC"] * values[market.asset]

    def _external_reasons(self, now):
        reasons = []
        uncertainty = self.config.clock_uncertainty_ms
        if self.dataset != "demo":
            if self.processing_delay_ms > 250:
                reasons.append("processing_backlog_exceeds_250ms")
            if uncertainty is None:
                reasons.append("clock_uncertainty_unverified")
            elif uncertainty > 100:
                reasons.append("clock_uncertainty_exceeds_100ms")
        account = self.ledger.account(now)
        reasons.extend(account["stop_reasons"])
        return reasons

    def _entry_decision(self, market, now, budget):
        history = [tick for tick in self.references[market.asset] if tick.source_at >= now - 32]
        decision = evaluate(market, history, self.books.get(market.ticker),
                            now, self._threshold(market), budget, D(self.config.depth_fraction))
        reasons = list(decision.reasons) + self._external_reasons(now)
        if self.conn.execute("SELECT 1 FROM positions WHERE market_ticker=?", (market.ticker,)).fetchone():
            reasons.append("already_entered_round")
        return replace(decision, reasons=tuple(dict.fromkeys(reasons)))

    def _execute_entry(self, order, now):
        market = self.markets.get(order["market_ticker"], market_from_dict(json.loads(order["market_json"])))
        account = self.ledger.account(now)
        budget = min(D(order["amount"]), D(5),
                     D("0.02") * (D(account["available"]) + D(order["amount"])))
        decision = self._entry_decision(market, now, budget)
        if not decision.eligible or decision.side != order["side"]:
            self.ledger.cancel_order(order["id"], now, "arrival:" + ",".join(decision.reasons or ("side_changed",)))
            return
        quote = plan_entry(self.books[market.ticker], order["side"], budget, market.fee,
                           D(self.config.depth_fraction), order["max_quantity"],
                           market.tick_size, market.price_ranges)
        if quote:
            # The fill retains the effective arrival-time fee/rule configuration.
            self.conn.execute("UPDATE orders SET market_json=? WHERE id=?",
                              (json.dumps(jsonable(market), sort_keys=True), order["id"]))
            position_id = self.ledger.fill_entry(order["id"], quote, now)
            if position_id:
                self.conn.execute("INSERT INTO execution_context(order_id,position_id,received_at,kind,market_json,quote_json) "
                                  "VALUES (?,?,?,'entry',?,?)", (order["id"], position_id, now,
                                  json.dumps(jsonable(market), sort_keys=True), json.dumps(jsonable(quote), sort_keys=True)))
        else:
            self.ledger.cancel_order(order["id"], now, "arrival:no_safe_executable_depth")

    def _execute_exit(self, position_id, now):
        position = self.conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        if position and position["remaining_quantity"]:
            original = market_from_dict(json.loads(position["market_json"]))
            market = self.markets.get(original.ticker, original)
            book = self.books.get(market.ticker)
            if market.verified and book and 0 <= now - book.received_at <= 5 and now < market.cutoff:
                quote = plan_exit(book, position["side"], position["remaining_quantity"], market.fee,
                                  D(self.config.depth_fraction), D("0.95"), market.tick_size,
                                  market.price_ranges)
                if quote:
                    self.ledger.record_exit(position_id, quote, now, effective_market=market)
                    remaining = self.conn.execute("SELECT remaining_quantity FROM positions WHERE id=?",
                                                  (position_id,)).fetchone()[0]
                    if remaining < position["remaining_quantity"]:
                        self.conn.execute("INSERT INTO execution_context(order_id,position_id,received_at,kind,market_json,quote_json) "
                                          "VALUES (?,?,?,'exit',?,?)", (position["order_id"], position_id, now,
                                          json.dumps(jsonable(market), sort_keys=True), json.dumps(jsonable(quote), sort_keys=True)))
        self.conn.execute("DELETE FROM exit_intents WHERE position_id=?", (position_id,))

    def _arrivals(self, now, inclusive):
        due = []
        for order in self.ledger.pending_orders():
            arrival = float(order["arrival_at"])
            if arrival < now or (inclusive and arrival <= now):
                due.append((arrival, order["market_ticker"], "entry", order))
        for row in self.conn.execute("SELECT e.*,p.market_ticker FROM exit_intents e "
                                     "JOIN positions p ON p.id=e.position_id"):
            if row["arrival_at"] < now or (inclusive and row["arrival_at"] <= now):
                due.append((row["arrival_at"], row["market_ticker"], "exit", dict(row)))
        for arrival, _, kind, value in sorted(due, key=lambda item: item[:3]):
            if kind == "entry":
                self._execute_entry(value, arrival)
            else:
                self._execute_exit(value["position_id"], arrival)

    def _tick(self, now, event_id):
        delay = self.config.execution_delay_ms / 1000
        for market in sorted(self.markets.values(), key=lambda m: m.ticker):
            if market.ticker in self.settled_tickers:
                continue
            # Keep complete eligibility history, including outside the entry window.
            account = self.ledger.account(now)
            budget = min(D(5), D("0.02") * D(account["available"]))
            decision = self._entry_decision(market, now, budget)
            identifier = hashlib.sha256(f"{event_id}:{market.ticker}".encode()).hexdigest()
            payload = {**jsonable(decision), "eligible": decision.eligible,
                       "dataset": self.dataset, "budget": str(budget),
                       "fee_scenario": market.fee.scenario, "fee_verified": market.fee.verified}
            if decision.eligible:
                order_id = self.ledger.reserve_entry(self.run_id, market, decision.side, now,
                                                     decision.quote.cost, now + delay,
                                                     decision.quote.quantity, identifier)
                if order_id:
                    payload["paper_order_id"] = order_id
                else:
                    payload["eligible"] = False
                    payload["reasons"].append("reservation_or_retry_gate")
            self.conn.execute("INSERT OR IGNORE INTO decisions VALUES (?,?,?,?,?)",
                              (identifier, self.run_id, market.ticker, now,
                               json.dumps(payload, sort_keys=True)))

        for position in self.conn.execute("SELECT p.* FROM positions p LEFT JOIN markets m ON m.ticker=p.market_ticker "
                                           "WHERE p.opened_at<=? AND COALESCE(json_extract(m.payload,'$.cutoff'), "
                                           "json_extract(p.market_json,'$.cutoff'))>=? "
                                           "ORDER BY p.opened_at,p.market_ticker,p.id", (now, now)):
            original = market_from_dict(json.loads(position["market_json"]))
            market = self.markets.get(original.ticker, original)
            if now > market.cutoff or now < position["opened_at"]:
                continue
            book = self.books.get(market.ticker)
            fresh = market.verified and book is not None and book.healthy and 0 <= now - book.received_at <= 5
            quote = (plan_exit(book, position["side"], position["quantity"], market.fee,
                               D(self.config.depth_fraction), D(0), market.tick_size,
                               market.price_ranges) if fresh else None)
            complete = bool(quote and quote.quantity == position["quantity"])
            refs = [tick for tick in self.references[market.asset]
                    if tick.source == "cfbenchmarks_value" and tick.received_at <= now
                    and 0 <= now - tick.source_at <= 2]
            reference = max(refs, key=lambda t: (t.source_at, t.received_at)).price if refs else None
            direction = 1 if (position["side"] == "yes") == market.yes_means_up else -1
            prior_refs = [tick for tick in self.references[market.asset]
                          if tick.source == "cfbenchmarks_value" and tick.received_at <= now - 5
                          and 0 <= now - 5 - tick.source_at <= 2]
            prior = max(prior_refs, key=lambda t: (t.source_at, t.received_at)).price if prior_refs else None
            selected_bids = bid_levels(book, position["side"]) if fresh else ()
            trajectory = {"market_ticker": market.ticker, "position_id": position["id"],
                          "side": position["side"],
                          "dataset": self.dataset, "quantity": position["quantity"],
                          "entry_cost": position["entry_cost"],
                          "entry_price": str(D(position["entry_cost"]) / position["quantity"]),
                          "opened_at": position["opened_at"], "market_cutoff": market.cutoff,
                          "depth_available": complete,
                          "executable_net_liquidation": str(quote.proceeds) if complete else None,
                          "executable_quantity": quote.quantity if quote else 0,
                          "partial_net_liquidation": str(quote.proceeds) if quote else None,
                          "exit_fee": str(quote.fees) if quote else None,
                          "liquidation_min_price": str(min(f.price for f in quote.fills)) if complete else None,
                          "best_bid": str(selected_bids[0].price) if selected_bids else None,
                          "reference_price": str(reference) if reference else None,
                          "signed_reference_change": str(direction * (reference - prior))
                              if reference is not None and prior is not None else None,
                          "reversal": bool(reference is not None and prior is not None
                                           and direction * (reference - prior) < 0),
                          "unusual_flow": None, "volatility_liquidity_risk": None}
            trajectory.update({"book": jsonable(book) if fresh else None,
                               "market": jsonable(market), "fee": jsonable(market.fee),
                               "tick_size": str(market.tick_size),
                               "price_ranges": jsonable(market.price_ranges),
                               "depth_fraction": self.config.depth_fraction})
            self.conn.execute("INSERT OR IGNORE INTO trajectories(run_id,position_id,received_at,payload) "
                              "VALUES (?,?,?,?)", (self.run_id, position["id"], now,
                                                   json.dumps(trajectory, sort_keys=True)))
            if (position["remaining_quantity"] > 0 and now < market.cutoff and fresh
                    and any(level.price >= D("0.95") for level in selected_bids)):
                self.conn.execute("INSERT OR IGNORE INTO exit_intents VALUES (?,?)",
                                  (position["id"], now + delay))

    def process(self, event: dict) -> bool:
        """A failed processor must restart from its committed observation journal."""
        if self.failed:
            raise RuntimeError("Engine processing failed; close and restart from committed state")
        try:
            return self._process_event(event)
        except Exception:
            self.failed = True
            raise

    def _process_event(self, event: dict) -> bool:
        """Commit one raw event and every resulting ledger transition atomically."""
        event = jsonable(dict(event))
        if self.resume_reset:
            event["process_restart"] = True
        now = float(event["received_at"])
        if not math.isfinite(now) or now < 0:
            raise ValueError("Event receipt timestamp must be finite and nonnegative")
        backlog = float(event.get("processing_delay_ms", 0))
        if not math.isfinite(backlog) or backlog < 0:
            raise ValueError("Event processing delay must be finite and nonnegative")
        supplied_dataset = event.get("dataset")
        if supplied_dataset is not None and supplied_dataset != self.dataset:
            raise ValueError("Cannot mix synthetic and real events in one research run")
        event["dataset"] = self.dataset
        payload = json.dumps(event, sort_keys=True, separators=(",", ":"))
        event_id = event.get("event_id") or hashlib.sha256(payload.encode()).hexdigest()
        event["event_id"] = event_id
        if self.conn.execute("SELECT 1 FROM events WHERE event_id=?", (event_id,)).fetchone():
            return False
        if now < self.last_received:
            # A late queue item becomes available at processing order, never in a
            # previous decision. Preserve its measured original receipt for audit.
            event["original_received_at"] = now
            now = self.last_received
            event["received_at"] = now
        with self.ledger.transaction():
            details = event.get("details", {})
            interrupted = event.get("process_restart") or (event["type"] == "health" and details.get("status") in
                           {"disconnected", "gap", "error", "unavailable"} and
                           ("kalshi" in details.get("component", "") or "websocket" in details.get("component", "")))
            if self.resume_reset or interrupted:
                for order in self.ledger.pending_orders():
                    self.ledger.cancel_order(order["id"], now, "restart_or_feed_gap")
                self.conn.execute("DELETE FROM exit_intents")
                self.resume_reset = False
            # Process timers before receiving later market data: no arrival-time lookahead.
            self._arrivals(now, inclusive=False)
            self._apply_data(event)
            self.conn.execute("INSERT INTO events(event_id,run_id,type,received_at,payload) VALUES (?,?,?,?,?)",
                              (event_id, self.run_id, event["type"], now, json.dumps(event, sort_keys=True)))
            if self.conn.execute("SELECT started_at FROM run_config WHERE run_id=?",
                                 (self.run_id,)).fetchone()[0] == 0:
                self.conn.execute("UPDATE run_config SET started_at=? WHERE run_id=?", (now, self.run_id))
            if event["type"] == "settlement":
                if event.get("official") is True or self.dataset == "demo":
                    self.ledger.settle(event["ticker"], event["result"], now)
            self._arrivals(now, inclusive=True)
            if event["type"] == "clock" and int(now) > self.last_clock:
                self.last_clock = int(now)
                self._tick(now, event_id)
                self._arrivals(now, inclusive=True)
        # SQLite's committed events are the canonical journal. Gzip is an audit mirror;
        # export/replay can always recover from the database if this append is interrupted.
        with gzip.open(self.raw_directory / f"{self.run_id}.jsonl.gz", "at", encoding="utf-8") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
        return True
