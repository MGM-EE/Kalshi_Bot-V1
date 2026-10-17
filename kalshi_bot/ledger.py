"""Transactional shared paper account. No network or live-order functionality.

Amounts are decimal strings in SQLite; SQLite's floating-point SUM is deliberately
not used for money. Every public mutation joins an existing caller transaction via
a savepoint, so an event cursor and its resulting account changes commit together.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
from typing import Iterator
from uuid import uuid4
from zoneinfo import ZoneInfo

from .models import D, ZERO, Market, Quote, jsonable


NY = ZoneInfo("America/New_York")


class Ledger:
    def __init__(self, path: str | Path, initial_cash: Decimal = D("250")):
        initial_cash = D(initial_cash)
        if not initial_cash.is_finite() or initial_cash < ZERO:
            raise ValueError("initial cash must be a finite nonnegative amount")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._lock = threading.RLock()
        self._savepoint = 0
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY CHECK(id=1), initial_cash TEXT NOT NULL,
                cash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                market_ticker TEXT NOT NULL, asset TEXT NOT NULL, side TEXT NOT NULL,
                amount TEXT NOT NULL, arrival_at REAL NOT NULL,
                max_quantity INTEGER NOT NULL, market_json TEXT NOT NULL,
                status TEXT NOT NULL, created_at REAL NOT NULL,
                completed_at REAL, cancel_reason TEXT, decision_id TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_market
                ON orders(market_ticker) WHERE status='pending';
            CREATE INDEX IF NOT EXISTS orders_retry ON orders(market_ticker,completed_at);
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES orders(id),
                run_id TEXT NOT NULL, market_ticker TEXT NOT NULL, asset TEXT NOT NULL,
                side TEXT NOT NULL, quantity INTEGER NOT NULL,
                remaining_quantity INTEGER NOT NULL, entry_cost TEXT NOT NULL,
                remaining_cost TEXT NOT NULL, realized_pnl TEXT NOT NULL,
                market_json TEXT NOT NULL, opened_at REAL NOT NULL, closed_at REAL,
                settlement_status TEXT NOT NULL DEFAULT 'pending',
                entry_slot INTEGER NOT NULL DEFAULT 1,
                UNIQUE(run_id, market_ticker, entry_slot)
            );
            CREATE TABLE IF NOT EXISTS fills (
                id TEXT PRIMARY KEY, order_id TEXT REFERENCES orders(id),
                position_id TEXT NOT NULL REFERENCES positions(id), kind TEXT NOT NULL,
                price TEXT NOT NULL, quantity INTEGER NOT NULL, fee TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ledger (
                id TEXT PRIMARY KEY, position_id TEXT REFERENCES positions(id),
                order_id TEXT REFERENCES orders(id), kind TEXT NOT NULL,
                cash_delta TEXT NOT NULL, realized_pnl TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ledger_created ON ledger(created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS one_account_entry_per_market
                ON positions(market_ticker);
            CREATE TABLE IF NOT EXISTS ledger_operations (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL,
                position_id TEXT NOT NULL REFERENCES positions(id), created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS risk_stops (
                local_date TEXT NOT NULL, reason TEXT NOT NULL, observed_at REAL NOT NULL,
                PRIMARY KEY(local_date,reason)
            );
        """)
        with self.transaction():
            self.conn.execute("INSERT OR IGNORE INTO accounts VALUES (1,?,?)",
                              (str(initial_cash), str(initial_cash)))

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize writers, preserving a caller's outer transaction on failure."""
        with self._lock:
            nested = self.conn.in_transaction
            self._savepoint += 1
            name = f"ledger_{self._savepoint}"
            self.conn.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                if nested:
                    self.conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    self.conn.execute(f"RELEASE SAVEPOINT {name}")
                else:
                    self.conn.rollback()
                raise
            else:
                if nested:
                    self.conn.execute(f"RELEASE SAVEPOINT {name}")
                else:
                    self.conn.commit()

    def _cash(self) -> Decimal:
        return D(self.conn.execute("SELECT cash FROM accounts WHERE id=1").fetchone()[0])

    def _reserved(self, excluding: str | None = None) -> Decimal:
        return sum((D(row["amount"]) for row in self.conn.execute(
            "SELECT id,amount FROM orders WHERE status='pending'")
                    if row["id"] != excluding), ZERO)

    def _move_cash(self, delta: Decimal, *, kind: str, now: float,
                   position_id: str, order_id: str, pnl: Decimal = ZERO) -> None:
        cash = self._cash() + delta
        if cash < ZERO:
            raise ValueError("cash movement would overdraw the shared account")
        self.conn.execute("UPDATE accounts SET cash=? WHERE id=1", (str(cash),))
        self.conn.execute("INSERT INTO ledger VALUES (?,?,?,?,?,?,?)",
                          (uuid4().hex, position_id, order_id, kind, str(delta), str(pnl), now))

    def account(self, now: float) -> dict:
        # A read transaction gives dashboards a consistent account snapshot too.
        with self.transaction():
            cash = self._cash()
            reserved = self._reserved()
            local_date = datetime.fromtimestamp(now, NY).date()
            midnight = datetime.combine(local_date, time.min, tzinfo=NY).timestamp()
            movements = self.conn.execute(
                "SELECT l.*, COALESCE(p.market_ticker,'') AS ticker FROM ledger l "
                "LEFT JOIN positions p ON p.id=l.position_id "
                "WHERE l.created_at<=? ORDER BY l.created_at,ticker,l.id", (now,)).fetchall()
            realized = sum((D(row["realized_pnl"]) for row in movements), ZERO)
            gross_losses = -sum((min(ZERO, D(row["realized_pnl"]))
                                for row in movements), ZERO)
            daily = ZERO
            daily_stopped = False
            for row in movements:
                if row["created_at"] >= midnight:
                    daily += D(row["realized_pnl"])
                    daily_stopped |= daily <= D("-25")
            streak = 0
            streak_stopped = False
            for row in self.conn.execute(
                "SELECT realized_pnl FROM positions WHERE closed_at>=? AND closed_at<=? "
                "ORDER BY closed_at,market_ticker,id", (midnight, now)):
                pnl = D(row[0])
                if pnl < ZERO:
                    streak += 1
                elif pnl > ZERO:
                    streak = 0
                streak_stopped |= streak >= 4
            observed = {"daily_loss_limit": daily_stopped,
                        "consecutive_loss_limit": streak_stopped}
            # An observed pause is irreversible until local midnight. A later
            # completion with the same timestamp can change the final sorted
            # streak, but cannot erase the pause already applied to new entries.
            for reason, triggered in observed.items():
                if triggered:
                    self.conn.execute(
                        "INSERT INTO risk_stops VALUES (?,?,?) "
                        "ON CONFLICT(local_date,reason) DO UPDATE SET "
                        "observed_at=MIN(risk_stops.observed_at,excluded.observed_at)",
                        (local_date.isoformat(), reason, now))
            latched = {row[0] for row in self.conn.execute(
                "SELECT reason FROM risk_stops WHERE local_date=? AND observed_at<=?",
                (local_date.isoformat(), now))}
            reasons = [reason for reason in observed if reason in latched]
            opened = self.conn.execute(
                "SELECT remaining_cost,remaining_quantity,side,market_json FROM positions "
                "WHERE remaining_quantity>0").fetchall()
            committed = sum((D(row[0]) for row in opened), ZERO)
            exposure = sum(row[1] for row in opened)
            directional_exposure = {"up": 0, "down": 0}
            for row in opened:
                yes_means_up = json.loads(row["market_json"]).get("yes_means_up", True)
                direction = "up" if (row["side"] == "yes") == yes_means_up else "down"
                directional_exposure[direction] += row["remaining_quantity"]
            return {"cash": str(cash), "reserved": str(reserved),
                    "available": str(cash - reserved), "realized_pnl": str(realized),
                    "daily_pnl": str(daily), "loss_streak": streak,
                    "stopped": bool(reasons), "stop_reasons": reasons,
                    "gross_realized_losses": str(gross_losses),
                    "worst_case_open_loss": str(committed),
                    "cash_committed": str(committed),
                    "gross_outcome_exposure": exposure,
                    "same_direction_exposure": directional_exposure,
                    "cost_basis_equity": str(cash + committed)}

    def reserve_entry(self, run_id: str, market: Market, side: str, now: float,
                      amount: Decimal, arrival_at: float, max_quantity: int,
                      decision_id: str | None = None) -> str | None:
        amount = D(amount)
        if side not in ("yes", "no"):
            raise ValueError("outcome side must be yes or no")
        if (not amount.is_finite() or amount <= ZERO or max_quantity <= 0
                or int(max_quantity) != max_quantity or arrival_at < now):
            return None
        with self.transaction():
            if decision_id:
                previous = self.conn.execute(
                    "SELECT id FROM orders WHERE decision_id=? AND run_id=?",
                    (decision_id, run_id)).fetchone()
                if previous:
                    return previous[0]
            account = self.account(now)
            if account["stopped"] or not 120 <= market.cutoff - now <= 600:
                return None
            if amount > min(D("5"), D("0.02") * D(account["available"])):
                return None
            if self.conn.execute(
                "SELECT 1 FROM positions WHERE market_ticker=?",
                (market.ticker,)).fetchone():
                return None
            if self.conn.execute("SELECT 1 FROM orders WHERE market_ticker=? "
                                 "AND status='pending'", (market.ticker,)).fetchone():
                return None
            last = self.conn.execute(
                "SELECT MAX(completed_at) FROM orders WHERE market_ticker=? "
                "AND status='cancelled'", (market.ticker,)).fetchone()[0]
            if last is not None and now - last < 3:
                return None
            order_id = uuid4().hex
            self.conn.execute(
                "INSERT INTO orders (id,run_id,market_ticker,asset,side,amount,arrival_at,"
                "max_quantity,market_json,status,created_at,decision_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (order_id, run_id, market.ticker, market.asset, side, str(amount), arrival_at,
                 max_quantity, json.dumps(jsonable(market), sort_keys=True), now, decision_id))
            return order_id

    def cancel_order(self, order_id: str, now: float, reason: str) -> None:
        with self.transaction():
            self.conn.execute("UPDATE orders SET status='cancelled',completed_at=?,"
                              "cancel_reason=? WHERE id=? AND status='pending'",
                              (now, reason, order_id))

    @staticmethod
    def _valid_quote(quote: Quote) -> bool:
        return bool(quote.fills) and all(
            isinstance(fill.quantity, int) and not isinstance(fill.quantity, bool)
            and fill.quantity > 0 and fill.price.is_finite() and fill.fee.is_finite()
            and ZERO <= fill.price <= D(1) and fill.fee >= ZERO for fill in quote.fills)

    def fill_entry(self, order_id: str, quote: Quote, now: float) -> str:
        with self.transaction():
            order = self.conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not order:
                raise KeyError(order_id)
            if order["status"] == "filled":
                return self.conn.execute("SELECT id FROM positions WHERE order_id=?",
                                         (order_id,)).fetchone()[0]
            if order["status"] != "pending":
                return ""
            if now < order["arrival_at"]:
                raise ValueError("cannot fill a paper order before its simulated arrival")
            market = json.loads(order["market_json"])
            budget = min(D(order["amount"]), D("5"),
                         D("0.02") * (self._cash() - self._reserved(excluding=order_id)))
            reason = None
            if not 120 <= market["cutoff"] - now <= 600:
                reason = "arrival_outside_entry_window"
            elif self.account(now)["stopped"]:
                reason = "risk_stop_at_arrival"
            elif not self._valid_quote(quote) or quote.quantity > order["max_quantity"]:
                reason = "invalid_or_empty_fill"
            else:
                cost, quantity = ZERO, 0
                for fill in quote.fills:
                    cost += fill.price * fill.quantity + fill.fee
                    quantity += fill.quantity
                    if cost > budget or cost / quantity > D("0.40"):
                        reason = "arrival_cost_limit"
                        break
            if reason:
                self.cancel_order(order_id, now, reason)
                return ""
            # Unique entry slot survives both process restarts and independent writers.
            if self.conn.execute("SELECT 1 FROM positions WHERE market_ticker=?",
                                 (order["market_ticker"],)).fetchone():
                self.cancel_order(order_id, now, "entry_slot_consumed")
                return ""
            position_id = uuid4().hex
            self.conn.execute(
                "INSERT INTO positions (id,order_id,run_id,market_ticker,asset,side,quantity,"
                "remaining_quantity,entry_cost,remaining_cost,realized_pnl,market_json,opened_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?, ?,?)",
                (position_id, order_id, order["run_id"], order["market_ticker"], order["asset"],
                 order["side"], quote.quantity, quote.quantity, str(quote.cost), str(quote.cost),
                 "0", order["market_json"], now))
            for fill in quote.fills:
                self.conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)",
                                  (uuid4().hex, order_id, position_id, "entry", str(fill.price),
                                   fill.quantity, str(fill.fee), now))
            self._move_cash(-quote.cost, kind="entry", now=now, position_id=position_id,
                            order_id=order_id)
            self.conn.execute("UPDATE orders SET status='filled',completed_at=? WHERE id=?",
                              (now, order_id))
            return position_id

    def pending_orders(self) -> list[dict]:
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM orders WHERE status='pending' ORDER BY arrival_at,market_ticker,id")]

    def positions(self, open_only: bool = True) -> list[dict]:
        where = "WHERE remaining_quantity>0" if open_only else ""
        return [dict(row) for row in self.conn.execute(
            f"SELECT * FROM positions {where} ORDER BY opened_at,market_ticker,id")]

    def record_exit(self, position_id: str, quote: Quote, now: float, *,
                    effective_market: Market | None = None) -> Decimal:
        with self.transaction():
            position = self.conn.execute("SELECT * FROM positions WHERE id=?",
                                         (position_id,)).fetchone()
            if not position:
                raise KeyError(position_id)
            if effective_market is not None and effective_market.ticker != position["market_ticker"]:
                raise ValueError("effective market ticker does not match position")
            cutoff = (effective_market.cutoff if effective_market is not None else
                      json.loads(position["market_json"])["cutoff"])
            if not math.isfinite(cutoff):
                raise ValueError("effective trading cutoff must be finite")
            # At cutoff only an observed official settlement can release proceeds.
            # A revised market governs execution; entry metadata remains immutable.
            if not position["remaining_quantity"] or now >= cutoff:
                return ZERO
            if not self._valid_quote(quote) or any(fill.price < D("0.95") for fill in quote.fills):
                return ZERO
            key = hashlib.sha256(json.dumps([position_id, now, jsonable(quote)],
                                           sort_keys=True).encode()).hexdigest()
            if self.conn.execute("SELECT 1 FROM ledger_operations WHERE id=?", (key,)).fetchone():
                return ZERO
            if quote.quantity > position["remaining_quantity"]:
                raise ValueError("exit quantity exceeds the remaining position")
            if quote.proceeds < ZERO:
                raise ValueError("exit fees exceed proceeds")
            quantity_left = position["remaining_quantity"] - quote.quantity
            allocated = (D(position["remaining_cost"]) if not quantity_left else
                         D(position["remaining_cost"]) * quote.quantity / position["remaining_quantity"])
            pnl = quote.proceeds - allocated
            self.conn.execute(
                "UPDATE positions SET remaining_quantity=?,remaining_cost=?,realized_pnl=?,"
                "closed_at=?,settlement_status=? WHERE id=?",
                (quantity_left, str(D(position["remaining_cost"]) - allocated),
                 str(D(position["realized_pnl"]) + pnl), now if not quantity_left else None,
                 "sold" if not quantity_left else "pending", position_id))
            for fill in quote.fills:
                self.conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)",
                                  (uuid4().hex, position["order_id"], position_id, "exit",
                                   str(fill.price), fill.quantity, str(fill.fee), now))
            self._move_cash(quote.proceeds, kind="exit", now=now, position_id=position_id,
                            order_id=position["order_id"], pnl=pnl)
            self.conn.execute("INSERT INTO ledger_operations VALUES (?,?,?,?)",
                              (key, "exit", position_id, now))
            return pnl

    def settle(self, market_ticker: str, result: str, now: float) -> None:
        if result not in ("yes", "no"):
            raise ValueError("official settlement result must be yes or no")
        with self.transaction():
            for position in self.conn.execute(
                "SELECT * FROM positions WHERE market_ticker=? AND remaining_quantity>0 "
                "ORDER BY run_id,id", (market_ticker,)).fetchall():
                payout = D(position["remaining_quantity"]) if position["side"] == result else ZERO
                pnl = payout - D(position["remaining_cost"])
                self._move_cash(payout, kind="settlement", now=now,
                                position_id=position["id"], order_id=position["order_id"], pnl=pnl)
                self.conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)",
                                  (uuid4().hex, position["order_id"], position["id"], "settlement",
                                   "1" if position["side"] == result else "0",
                                   position["remaining_quantity"], "0", now))
                self.conn.execute(
                    "UPDATE positions SET remaining_quantity=0,remaining_cost='0',"
                    "realized_pnl=?,closed_at=?,settlement_status='settled' WHERE id=?",
                    (str(D(position["realized_pnl"]) + pnl), now, position["id"]))
            # An official resolution also invalidates any outstanding entry.
            self.conn.execute("UPDATE orders SET status='cancelled',completed_at=?,"
                              "cancel_reason='market_settled' WHERE market_ticker=? AND status='pending'",
                              (now, market_ticker))
