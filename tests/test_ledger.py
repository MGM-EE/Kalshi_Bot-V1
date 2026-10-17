from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest

from kalshi_bot.ledger import Ledger
from kalshi_bot.models import D, Fill, Market, Quote


def market(ticker="BTC-1", now=1000, cutoff=None):
    return Market(ticker, ticker.split("-")[0], D("80000"), now - 600,
                  cutoff or now + 300, now + 300, now + 600)


def quote(quantity=10, price="0.38", fee="0.16"):
    return Quote((Fill(D(price), quantity, D(fee)),))


def enter(ledger, ticker="BTC-1", now=1000, amount="4", quantity=10,
          price="0.38", fee="0.16", side="yes"):
    order = ledger.reserve_entry("baseline", market(ticker, now), side, now,
                                 D(amount), now + .5, quantity)
    assert order
    position = ledger.fill_entry(order, quote(quantity, price, fee), now + .5)
    assert position
    return order, position


def test_shared_sequential_budget_and_no_float_money(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    first = ledger.reserve_entry("run", market(), "yes", 1000, D("5"), 1000.5, 12)
    assert first
    assert ledger.account(1000)["reserved"] == "5"
    assert ledger.reserve_entry("run", market("ETH-1"), "no", 1000,
                                D("5"), 1000.5, 12) is None
    second = ledger.reserve_entry("run", market("ETH-1"), "no", 1000,
                                  D("4.90"), 1000.5, 12)
    assert second
    assert ledger.account(1000)["available"] == "240.10"
    assert ledger.reserve_entry("run", market("SOL-1"), "yes", 1000,
                                D("4.802"), 1000.5, 12)
    assert ledger.conn.execute("SELECT typeof(amount) FROM orders").fetchone()[0] == "text"
    assert ledger.conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert ledger.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_fill_revalidates_shared_cash_and_releases_rejected_reservation(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    one = ledger.reserve_entry("run", market(), "yes", 1000, D("5"), 1000.5, 13)
    two = ledger.reserve_entry("run", market("ETH-1"), "yes", 1000, D("4.90"), 1000.5, 12)
    # First arrival has only $245.10 unreserved cash once the other order's
    # reservation is respected: its 2% ceiling is $4.902, not $5.
    assert ledger.fill_entry(one, quote(13, "0.37", "0.13"), 1000.5) == ""
    assert ledger.account(1000.5)["reserved"] == "4.90"
    ledger.cancel_order(two, 1000.6, "test")
    assert ledger.account(1000.6)["available"] == "250"


def test_partial_entry_permanently_consumes_slot_and_is_idempotent_after_restart(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = Ledger(path)
    order = ledger.reserve_entry("run", market(), "yes", 1000, D("5"), 1000.5, 12)
    position = ledger.fill_entry(order, quote(3, "0.38", "0.05"), 1000.5)
    assert position
    assert ledger.positions()[0]["quantity"] == 3
    assert D(ledger.account(1000.5)["cash"]) == D("248.81")
    ledger.close()
    ledger = Ledger(path, D("9999"))
    assert ledger.fill_entry(order, quote(), 1001) == position
    assert ledger.account(1001)["cash"] == "248.81"
    assert ledger.reserve_entry("run", market(), "yes", 1004, D("4"), 1004.5, 10) is None
    assert ledger.reserve_entry("new-configuration", market(), "yes", 1004,
                                D("4"), 1004.5, 10) is None
    assert ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_nested_transactions_rollback_account_positions_and_event_cursor(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    ledger.conn.execute("CREATE TABLE cursor (value INTEGER)")
    ledger.conn.execute("INSERT INTO cursor VALUES (0)")
    with pytest.raises(RuntimeError):
        with ledger.transaction():
            enter(ledger)
            ledger.conn.execute("UPDATE cursor SET value=1")
            raise RuntimeError("simulated process failure before commit")
    assert ledger.positions() == []
    assert ledger.pending_orders() == []
    assert ledger.account(1001)["cash"] == "250"
    assert ledger.conn.execute("SELECT value FROM cursor").fetchone()[0] == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    # A failed inner savepoint does not destroy valid outer work.
    with ledger.transaction():
        ledger.conn.execute("UPDATE cursor SET value=2")
        try:
            with ledger.transaction():
                ledger.conn.execute("UPDATE cursor SET value=3")
                raise ValueError("inner failure")
        except ValueError:
            pass
    assert ledger.conn.execute("SELECT value FROM cursor").fetchone()[0] == 2


def test_concurrent_connections_reserve_only_once_and_respect_shared_budget(tmp_path):
    path = tmp_path / "ledger.sqlite"
    Ledger(path).close()

    def attempt(index):
        ledger = Ledger(path)
        try:
            return ledger.reserve_entry("run", market(), "yes", 1000,
                                        D("5"), 1000.5, 12)
        finally:
            ledger.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert sum(result is not None for result in results) == 1
    assert Ledger(path).account(1000)["reserved"] == "5"


def test_rejects_bad_partial_prefix_even_when_total_average_is_cheap(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    order = ledger.reserve_entry("run", market(), "yes", 1000, D("1"), 1000.5, 2)
    bad = Quote((Fill(D("0.39"), 1, D("0.03")), Fill(D("0.10"), 1, D("0.01"))))
    assert bad.cost / bad.quantity < D("0.4")
    assert ledger.fill_entry(order, bad, 1000.5) == ""
    assert ledger.positions() == []
    assert ledger.account(1001)["cash"] == "250"
    assert ledger.reserve_entry("run", market(), "yes", 1003.49,
                                D("1"), 1003.99, 2) is None
    assert ledger.reserve_entry("run", market(), "yes", 1003.5, D("1"), 1004, 2)


def test_arrival_window_boundaries_and_zero_fill_retry(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    exact = market("BTC-boundary", cutoff=1120)
    order = ledger.reserve_entry("run", exact, "yes", 1000, D("1"), 1000, 2)
    assert ledger.fill_entry(order, quote(2, "0.30", "0.02"), 1000)
    late = market("ETH-late", cutoff=1120.4)
    order = ledger.reserve_entry("run", late, "yes", 1000, D("1"), 1000.5, 2)
    assert ledger.fill_entry(order, quote(2, "0.30", "0.02"), 1000.5) == ""
    assert ledger.conn.execute("SELECT cancel_reason FROM orders WHERE id=?",
                               (order,)).fetchone()[0] == "arrival_outside_entry_window"
    assert ledger.reserve_entry("run", market("SOL-too-early", cutoff=1600.01),
                                "yes", 1000, D("1"), 1000.5, 2) is None
    early = ledger.reserve_entry("run", market("SOL-early"), "yes", 1000, D("1"), 1001, 2)
    with pytest.raises(ValueError, match="before"):
        ledger.fill_entry(early, quote(2, "0.30", "0.02"), 1000.9)
    assert len(ledger.pending_orders()) == 1


def test_partial_exits_allocate_cost_and_credit_only_actual_proceeds(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    order, position = enter(ledger)
    first = quote(4, "0.95", "0.02")
    assert ledger.record_exit(position, first, 1010) == D("2.196")
    assert ledger.record_exit(position, first, 1010) == D("0")
    row = ledger.positions()[0]
    assert row["remaining_quantity"] == 6
    assert D(row["remaining_cost"]) == D("2.376")
    assert D(ledger.account(1010)["daily_pnl"]) == D("2.196")
    assert ledger.account(1010)["loss_streak"] == 0
    # A touch below executable 95 cents and a quote at cutoff cannot sell.
    assert ledger.record_exit(position, quote(6, "0.94", "0"), 1020) == D("0")
    assert ledger.record_exit(position, quote(6, "0.99", "0"), 1300) == D("0")
    before = ledger.account(1400)["cash"]
    ledger.settle("BTC-1", "yes", 1450)
    ledger.settle("BTC-1", "yes", 1451)
    assert D(ledger.account(1451)["cash"]) == D(before) + 6
    assert ledger.positions() == []
    assert D(ledger.positions(False)[0]["realized_pnl"]) == D("5.820")
    assert ledger.positions(False)[0]["settlement_status"] == "settled"
    assert ledger.account(1451)["loss_streak"] == 0


def test_delayed_losing_settlement_does_not_credit_cash_at_cutoff(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    _, position = enter(ledger)
    assert D(ledger.account(2000)["cash"]) == D("246.04")
    assert ledger.positions()[0]["remaining_quantity"] == 10
    ledger.settle("BTC-1", "no", 3000)
    assert ledger.positions() == []
    assert D(ledger.account(3000)["realized_pnl"]) == D("-3.96")
    assert ledger.account(3000)["loss_streak"] == 1
    ledger.settle("BTC-1", "yes", 3001)
    assert D(ledger.account(3001)["cash"]) == D("246.04")


def test_shared_stops_latch_and_reset_at_local_midnight_with_dst(tmp_path):
    ny = ZoneInfo("America/New_York")
    # DST ends this morning; local midnight is UTC-4, next midnight UTC-5.
    now = datetime(2026, 11, 1, 0, 1, tzinfo=ny).timestamp()
    ledger = Ledger(tmp_path / "ledger.sqlite", D("1000"))
    for index in range(8):
        enter(ledger, f"{['BTC','ETH','SOL'][index % 3]}-{index}", now,
              "5", 12, "0.39", "0.12")  # $4.80 each, all-in .40
    for index in range(6):
        ledger.settle(f"{['BTC','ETH','SOL'][index % 3]}-{index}", "no", now + 1000 + index)
    account = ledger.account(now + 1006)
    assert account["stopped"] is True
    assert account["loss_streak"] == 6
    assert set(account["stop_reasons"]) == {"daily_loss_limit", "consecutive_loss_limit"}
    assert D(account["daily_pnl"]) == D("-28.80")
    assert ledger.reserve_entry("run", market("BTC-new", now + 1010), "yes",
                                now + 1010, D("5"), now + 1010.5, 12) is None
    # Existing winning positions can recover P&L and reset the current streak;
    # the stop remains latched through local midnight.
    ledger.settle("BTC-6", "yes", now + 1020)
    ledger.settle("ETH-7", "yes", now + 1021)
    recovered = ledger.account(now + 1022)
    assert D(recovered["daily_pnl"]) > D("-25")
    assert recovered["loss_streak"] == 0
    assert recovered["stopped"] is True
    next_midnight = datetime(2026, 11, 2, tzinfo=ny).timestamp()
    assert ledger.account(next_midnight - 1)["stopped"] is True
    reset = ledger.account(next_midnight)
    assert reset["daily_pnl"] == "0"
    assert reset["loss_streak"] == 0
    assert reset["stopped"] is False
    assert ledger.reserve_entry("run", market("BTC-reset", next_midnight), "yes",
                                next_midnight, D("5"), next_midnight + .5, 12)


def test_simultaneous_completions_use_ticker_tie_break(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", D("1000"))
    for ticker in ("BTC-a", "BTC-b", "BTC-c", "BTC-d", "ETH-winner"):
        enter(ledger, ticker)
    # Deliberately ingest in reverse order. The event-time/ticker ordering,
    # rather than incidental insertion order, determines the risk state.
    ledger.settle("ETH-winner", "yes", 1500)
    for ticker in ("BTC-d", "BTC-c", "BTC-b", "BTC-a"):
        ledger.settle(ticker, "no", 1500)
    account = ledger.account(1500)
    assert account["loss_streak"] == 0
    assert account["stopped"] is True
    assert account["stop_reasons"] == ["consecutive_loss_limit"]


def test_partial_profit_changes_daily_pnl_but_streak_updates_only_on_close(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite", D("1000"))
    enter(ledger, "BTC-loss")
    _, zero_position = enter(ledger, "ETH-flat", price="0.36", fee="0.20")
    enter(ledger, "SOL-winner")
    # This position realizes a gain now and a matching residual loss at
    # settlement, yielding exactly zero for its complete lifetime.
    assert ledger.record_exit(zero_position, quote(4, "0.95", "0"), 1200) == D("2.28")
    assert D(ledger.account(1200)["daily_pnl"]) == D("2.28")
    assert ledger.account(1200)["loss_streak"] == 0
    ledger.settle("BTC-loss", "no", 1400)
    assert ledger.account(1401)["loss_streak"] == 1
    ledger.settle("ETH-flat", "no", 1450)
    assert D(next(p for p in ledger.positions(False) if p["id"] == zero_position)["realized_pnl"]) == 0
    assert ledger.account(1450)["loss_streak"] == 1
    # A completed positive position resets the existing loss streak.
    ledger.settle("SOL-winner", "yes", 1500)
    assert ledger.account(1500)["loss_streak"] == 0


def test_effective_market_extension_allows_exit_without_changing_entry_metadata(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    _, position = enter(ledger)
    latest = replace(market(), cutoff=1400, observation_end=1400, expiration=1600)
    partial = quote(5, "0.95", "0.02")
    assert ledger.record_exit(position, partial, 1350) == D("0")
    assert ledger.record_exit(position, partial, 1350, effective_market=latest) == D("2.75")
    remaining = ledger.positions()[0]
    assert remaining["remaining_quantity"] == 5
    assert json.loads(remaining["market_json"])["cutoff"] == 1300
    assert ledger.record_exit(position, partial, 1400, effective_market=latest) == D("0")
    assert ledger.positions()[0]["remaining_quantity"] == 5


def test_effective_market_shortening_and_ticker_mismatch(tmp_path):
    ledger = Ledger(tmp_path / "ledger.sqlite")
    _, position = enter(ledger)
    latest = replace(market(), cutoff=1250)
    partial = quote(5, "0.95", "0.02")
    with pytest.raises(ValueError, match="ticker"):
        ledger.record_exit(position, partial, 1200, effective_market=market("ETH-1"))
    assert ledger.positions()[0]["remaining_quantity"] == 10
    assert ledger.record_exit(position, partial, 1249, effective_market=latest) == D("2.75")
    assert ledger.record_exit(position, partial, 1250, effective_market=latest) == D("0")
    remaining = ledger.positions()[0]
    assert remaining["remaining_quantity"] == 5
    assert json.loads(remaining["market_json"])["cutoff"] == 1300


def test_observed_stop_latches_when_same_clock_winner_changes_sorted_streak(tmp_path):
    path = tmp_path / "ledger.sqlite"
    ledger = Ledger(path, D("1000"))
    for ticker in ("BTC-A", "BTC-B", "BTC-C", "BTC-D", "BTC-E"):
        enter(ledger, ticker)
    for ticker in ("BTC-A", "BTC-B", "BTC-D", "BTC-E"):
        ledger.settle(ticker, "no", 1500)
    observed = ledger.account(1500)
    assert observed["loss_streak"] == 4
    assert observed["stop_reasons"] == ["consecutive_loss_limit"]
    # The winner sorts between the four losses. Current streak changes to two,
    # while the stop previously observed at this receipt time remains in force.
    ledger.settle("BTC-C", "yes", 1500)
    latest = ledger.account(1500)
    assert latest["loss_streak"] == 2
    assert latest["stop_reasons"] == ["consecutive_loss_limit"]
    assert latest["stopped"] is True
    ledger.close()
    ledger = Ledger(path)
    assert ledger.account(1501)["stopped"] is True
    assert ledger.reserve_entry("baseline", market("SOL-stopped", now=1501), "yes",
                                1501, D("4"), 1501.5, 10) is None
    next_midnight = datetime(1970, 1, 1, tzinfo=ZoneInfo("America/New_York")).timestamp()
    assert ledger.account(next_midnight)["stopped"] is False
    # Keep the past pause as an audit record, without applying it to a new day.
    assert ledger.conn.execute("SELECT COUNT(*) FROM risk_stops").fetchone()[0] == 1
