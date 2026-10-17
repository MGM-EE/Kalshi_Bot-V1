import json
from pathlib import Path
import sqlite3

import pytest

from kalshi_bot.cli import main
from kalshi_bot.config import BotConfig
from kalshi_bot.demo import START, demo_events
from kalshi_bot.engine import Engine
from kalshi_bot.models import D


@pytest.fixture(scope="module")
def events():
    return list(demo_events())


def btc_events(events, until=300):
    for event in events:
        if event["received_at"] > START + until:
            break
        if event["type"] == "market" and event["market"]["asset"] != "BTC":
            continue
        if event.get("asset", "BTC") != "BTC":
            continue
        if event.get("ticker", "DEMO-BTC-ROUND-1") != "DEMO-BTC-ROUND-1":
            continue
        yield event


def clock(at, identifier="extra-clock"):
    return {"type": "clock", "received_at": at, "event_id": identifier, "dataset": "demo"}


def test_arrival_uses_only_book_received_before_arrival(tmp_path, events):
    engine = Engine(tmp_path, BotConfig(clock_uncertainty_ms=0), mode="demo")
    try:
        for event in btc_events(events):
            engine.process(event)
        assert len(engine.ledger.pending_orders()) == 1
        # This expensive quote is received AFTER the scheduled arrival. Applying
        # it first would incorrectly cancel the fill or buy using future depth.
        engine.process({"type": "book", "ticker": "DEMO-BTC-ROUND-1", "received_at": START + 301,
                        "yes_bids": [["0.89", 100]], "no_bids": [["0.10", 100]], "dataset": "demo"})
        position = engine.ledger.positions()[0]
        assert position["opened_at"] == START + 300.5
        assert D(position["entry_cost"]) == D("4.758")
        assert engine.conn.execute("SELECT COUNT(*) FROM execution_context").fetchone()[0] == 1
    finally:
        engine.close()


def test_live_restart_cancels_orders_instead_of_filling_through_outage(tmp_path, events):
    config = BotConfig(clock_uncertainty_ms=0)
    engine = Engine(tmp_path, config, mode="paper", dataset="demo")
    for event in btc_events(events):
        engine.process(event)
    assert len(engine.ledger.pending_orders()) == 1
    engine.close()
    resumed = Engine(tmp_path, config, mode="paper", dataset="demo")
    try:
        resumed.process(clock(START + 310))
        assert resumed.ledger.positions() == []
        assert resumed.ledger.pending_orders() == []
        assert D(resumed.ledger.account(START + 310)["cash"]) == 250
        assert not resumed.books["DEMO-BTC-ROUND-1"].healthy
        assert not resumed.references["BTC"]
    finally:
        resumed.close()
    # A restart is part of the event journal, so a fresh replay must reproduce
    # the canceled orders rather than filling them using the pre-outage book.
    target = tmp_path / "replayed"
    main(["replay", str(tmp_path / "paper.sqlite3"), "--data-dir", str(target)])
    with sqlite3.connect(target / "paper.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 0
        assert D(connection.execute("SELECT cash FROM accounts").fetchone()[0]) == 250


def test_feed_gap_cancels_inflight_and_requires_new_history(tmp_path, events):
    engine = Engine(tmp_path, BotConfig(clock_uncertainty_ms=0), mode="demo")
    try:
        for event in btc_events(events):
            engine.process(event)
        engine.process({"type": "health", "received_at": START + 301, "dataset": "demo",
                        "details": {"component": "kalshi_ws", "status": "disconnected", "reason": "sequence_gap"}})
        assert not engine.ledger.positions()
        assert not engine.ledger.pending_orders()
        assert not engine.references["BTC"]
    finally:
        engine.close()


def test_arrival_after_120_second_boundary_expires(tmp_path, events):
    engine = Engine(tmp_path, BotConfig(clock_uncertainty_ms=0), mode="demo")
    try:
        market = next(event for event in events if event["type"] == "market")
        engine.process(market)
        for event in btc_events(events, until=780):
            if event["type"] in {"reference", "book"} and event["received_at"] >= START + 750:
                engine.process(event)
        engine.process(clock(START + 780))
        assert len(engine.ledger.pending_orders()) == 1
        engine.process(clock(START + 781, "expire"))
        assert not engine.ledger.positions()
        assert not engine.ledger.pending_orders()
        assert "outside_entry_window" in engine.conn.execute("SELECT cancel_reason FROM orders").fetchone()[0]
    finally:
        engine.close()


def test_event_and_fill_roll_back_together_on_crash(tmp_path, events, monkeypatch):
    engine = Engine(tmp_path, BotConfig(clock_uncertainty_ms=0), mode="demo")
    try:
        for event in btc_events(events):
            engine.process(event)
        original_count = engine.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        def crash(*args, **kwargs):
            raise RuntimeError("simulated process failure inside transaction")
        monkeypatch.setattr(engine.ledger, "fill_entry", crash)
        with pytest.raises(RuntimeError, match="simulated process failure"):
            engine.process(clock(START + 301))
        assert engine.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == original_count
        assert not engine.ledger.positions()
        assert len(engine.ledger.pending_orders()) == 1
        assert D(engine.ledger.account(START + 301)["cash"]) == 250
        with pytest.raises(RuntimeError, match="close and restart"):
            engine.process(clock(START + 302))
    finally:
        engine.close()


def test_unverified_clock_blocks_real_data_paper_entries(tmp_path, events):
    engine = Engine(tmp_path, BotConfig(), mode="paper", dataset="real")
    try:
        for event in btc_events(events):
            engine.process({**event, "dataset": "real"})
        assert not engine.ledger.pending_orders()
        latest = json.loads(engine.conn.execute("SELECT payload FROM decisions ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        assert "clock_uncertainty_unverified" in latest["reasons"]
    finally:
        engine.close()


def test_dataset_and_frozen_configuration_cannot_mix(tmp_path):
    engine = Engine(tmp_path, BotConfig(), mode="demo")
    with pytest.raises(ValueError, match="mix synthetic"):
        engine.process({"type": "clock", "received_at": START, "dataset": "real"})
    engine.close()
    with pytest.raises(ValueError, match="frozen configuration"):
        Engine(tmp_path, BotConfig(depth_fraction="1"), mode="demo")


def portfolio(database):
    with sqlite3.connect(database) as connection:
        return (connection.execute("SELECT cash FROM accounts").fetchall(),
                connection.execute("SELECT market_ticker,side,quantity,entry_cost,realized_pnl,opened_at,closed_at "
                                   "FROM positions ORDER BY market_ticker").fetchall(),
                connection.execute("SELECT kind,price,quantity,fee,created_at FROM fills ORDER BY created_at,kind,price,quantity").fetchall())


def test_cli_demo_restart_and_sqlite_replay_are_deterministic(tmp_path, events, capsys):
    source = tmp_path / "demo"
    main(["demo", "--data-dir", str(source)])
    first = json.loads(capsys.readouterr().out)
    assert first["events_processed"] == len(events)
    assert first["dataset"] == "demo"
    before = portfolio(source / "paper.sqlite3")
    assert len(before[1]) == 3
    assert all(D(position[4]) != 0 for position in before[1])
    main(["demo", "--data-dir", str(source)])
    assert json.loads(capsys.readouterr().out)["events_processed"] == 0
    assert portfolio(source / "paper.sqlite3") == before
    target = tmp_path / "replay"
    main(["replay", str(source / "paper.sqlite3"), "--data-dir", str(target)])
    assert json.loads(capsys.readouterr().out)["events_processed"] == len(events)
    assert portfolio(target / "paper.sqlite3") == before


def test_settled_round_stops_adding_decisions(tmp_path, events):
    engine = Engine(tmp_path, BotConfig(clock_uncertainty_ms=0), mode="demo")
    try:
        for event in btc_events(events, until=910):
            engine.process(event)
        count = engine.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
        engine.process(clock(START + 1200))
        assert engine.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == count
    finally:
        engine.close()
