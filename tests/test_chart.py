import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from kalshi_bot.config import BotConfig
from kalshi_bot.dashboard import create_app
from kalshi_bot.engine import Engine
from kalshi_bot.models import D, FeeConfig, Fill, Market, Quote, jsonable


@pytest.fixture
def chart(tmp_path):
    engine = Engine(tmp_path, BotConfig(clock_uncertainty_ms=0), mode="demo")
    yield engine, TestClient(create_app(tmp_path / "paper.sqlite3"))
    engine.close()


def seed_market(engine, ticker="BTC-CHART", asset="BTC", yes_up=True):
    market = Market(ticker, asset, D("100"), 900, 1300, 1300, 1400,
                    fee=FeeConfig(D(1), verified=True), verified=True, yes_means_up=yes_up)
    engine.conn.execute("INSERT OR REPLACE INTO markets VALUES (?,?)", (ticker, json.dumps(jsonable(market))))
    return market


def event(engine, kind, at, **payload):
    engine.conn.execute("INSERT INTO events(event_id,run_id,type,received_at,payload) VALUES (?,?,?,?,?)",
                        (str(engine.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]), engine.run_id,
                         kind, at, json.dumps({"type": kind, "received_at": at, **payload})))


def test_missing_database_chart_does_not_fabricate_quotes(tmp_path):
    with TestClient(create_app(tmp_path / "missing.db")) as client:
        assert client.get("/api/chart").status_code == 503
    assert not (tmp_path / "missing.db").exists()


def test_no_market_waits_without_an_invented_line(chart):
    engine, client = chart
    response = client.get("/api/chart").json()
    assert response["market"] is None
    assert response["points"] == []
    assert response["status"] == "waiting"


def test_chart_samples_per_second_and_preserves_missing_depth_and_mapping(chart):
    engine, client = chart
    seed_market(engine)
    event(engine, "book", 1000.1, ticker="BTC-CHART", yes_bids=[[".3", 10]], no_bids=[[".6", 10]], healthy=True)
    event(engine, "book", 1000.8, ticker="BTC-CHART", yes_bids=[[".4", 10]], no_bids=[[".5", 10]], healthy=True)
    event(engine, "book", 1001.2, ticker="BTC-CHART", yes_bids=[], no_bids=[], healthy=False)
    event(engine, "reference", 1000.5, asset="BTC", price="99.9", source_at=1000.4, source="cfbenchmarks_value")
    event(engine, "reference", 1000.5, asset="SOL", price="200", source_at=1000.4, source="cfbenchmarks_value")
    response = client.get("/api/chart?asset=BTC").json()
    assert len(response["points"]) == 2
    assert response["points"][0]["up_bid"] == "0.4"
    assert response["points"][0]["up_ask"] == "0.5"
    assert response["points"][0]["down_bid"] == "0.5"
    assert response["points"][1]["up_bid"] is None
    assert len(response["references"]) == 1
    assert response["references"][0]["price"] == "99.9"
    assert response["dataset"] == "demo"
    assert "package_versions" not in response


@pytest.mark.parametrize("yes_up,side,direction", [(True,"yes","UP"),(True,"no","DOWN"),
                                                   (False,"yes","DOWN"),(False,"no","UP")])
def test_actual_buy_and_sell_markers_keep_outcome_price_quantity_and_fees(chart, yes_up, side, direction):
    engine, client = chart
    market = seed_market(engine, yes_up=yes_up)
    order = engine.ledger.reserve_entry(engine.run_id, market, side, 1000, D("3.96"), 1000.5, 10)
    position = engine.ledger.fill_entry(order, Quote((Fill(D(".38"), 10, D(".16")),)), 1000.5)
    engine.ledger.record_exit(position, Quote((Fill(D(".96"), 10, D(".1")),)), 1100)
    event(engine, "book", 1200, ticker=market.ticker, yes_bids=[[".3", 10]], no_bids=[[".6", 10]], healthy=True)
    response = client.get("/api/chart").json()
    assert [fill["label"] for fill in response["fills"]] == [f"BUY {direction}", f"SELL {direction}"]
    assert response["fills"][0]["price"] == "0.38"  # NO fills are not mislabeled as a YES price.
    assert response["fills"][0]["quantity"] == 10
    assert response["fills"][0]["fee"] == "0.16"


def test_settlement_is_distinct_from_a_sell_and_demo_requires_completed_round(chart):
    engine, client = chart
    market = seed_market(engine)
    order = engine.ledger.reserve_entry(engine.run_id, market, "no", 1000, D("3.96"), 1000.5, 10)
    engine.ledger.fill_entry(order, Quote((Fill(D(".38"), 10, D(".16")),)), 1000.5)
    engine.ledger.settle(market.ticker, "yes", 1310)
    event(engine, "settlement", 1310, ticker=market.ticker, result="yes", dataset="demo")
    response = client.get("/api/chart").json()
    assert response["demo_complete"]
    assert response["fills"][-1]["label"] == "SETTLED DOWN"
    assert response["fills"][-1]["price"] == "0"


def test_exchange_prices_cannot_be_presented_as_reference(chart):
    engine, client = chart
    seed_market(engine)
    event(engine, "reference", 1000, asset="BTC", price="99.9", source_at=1000, source="coinbase")
    assert client.get("/api/chart").json()["references"] == []


def test_chart_only_reads_the_database_and_validates_filters(chart):
    engine, client = chart
    seed_market(engine)
    before = engine.conn.total_changes
    for query in ("asset=BTC", "asset=BTC&window=1m", "asset=BTC&window=round"):
        assert client.get("/api/chart?"+query).status_code == 200
    assert engine.conn.total_changes == before
    assert client.post("/api/chart", json={"buy": True}).status_code == 405
    assert client.get("/api/chart?asset=DOGE").status_code == 422
    assert client.get("/api/chart?window=all").status_code == 422
    assert client.get("/api/chart?ticker=x%27%20OR%201=1--").status_code == 404
    assert client.get("/api/chart?asset=ETH&ticker=BTC-CHART").status_code == 404


def test_closed_feed_is_stale_even_with_a_recent_poll(chart, monkeypatch):
    engine, client = chart
    seed_market(engine)
    engine.conn.execute("UPDATE run_config SET config_json=?", (json.dumps({"mode":"paper","dataset":"real"}),))
    event(engine, "book", 1000, ticker="BTC-CHART", yes_bids=[[".3",10]], no_bids=[[".6",10]], healthy=False)
    monkeypatch.setattr("kalshi_bot.dashboard.time.time", lambda: 1001)
    response = client.get("/api/chart").json()
    assert response["status"] == "polling"
    assert response["dataset"] == "real"
    monkeypatch.setattr("kalshi_bot.dashboard.time.time", lambda: 1010)
    assert client.get("/api/chart").json()["status"] == "stale"


def test_live_chart_reads_new_quotes_without_inventing_a_trade(chart, monkeypatch):
    engine, client = chart
    seed_market(engine)
    engine.conn.execute("UPDATE run_config SET config_json=?", (json.dumps({"mode":"paper","dataset":"real"}),))
    monkeypatch.setattr("kalshi_bot.dashboard.time.time", lambda: 1050)
    event(engine, "book", 1049, ticker="BTC-CHART", yes_bids=[[".3",10]], no_bids=[[".6",10]], healthy=True)
    first = client.get("/api/chart").json()
    assert first["status"] == "live"
    event(engine, "book", 1050, ticker="BTC-CHART", yes_bids=[[".4",10]], no_bids=[[".5",10]], healthy=True)
    updated = client.get("/api/chart").json()
    assert first["points"][-1]["up_bid"] == "0.3"
    assert updated["points"][-1]["up_bid"] == "0.4"
    assert updated["latest_received_at"] == 1050
    assert updated["fills"] == []
