import csv
import hashlib
import io
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from kalshi_bot.dashboard import create_app, _database
from kalshi_bot.ledger import Ledger
from kalshi_bot.models import FeeConfig, Market, jsonable, D


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "paper.sqlite"
    ledger = Ledger(path)
    ledger.close()
    with sqlite3.connect(path) as conn:
        conn.executescript("""
        CREATE TABLE events(id INTEGER PRIMARY KEY,event_id TEXT UNIQUE,run_id TEXT,type TEXT,received_at REAL,payload TEXT);
        CREATE TABLE decisions(id INTEGER PRIMARY KEY,run_id TEXT,market_ticker TEXT,received_at REAL,payload TEXT);
        CREATE TABLE markets(ticker TEXT PRIMARY KEY,payload TEXT);
        CREATE TABLE trajectories(id INTEGER PRIMARY KEY,run_id TEXT,position_id TEXT,received_at REAL,payload TEXT);
        CREATE TABLE run_config(run_id TEXT PRIMARY KEY,started_at REAL,config_json TEXT);
        """)
        conn.execute("INSERT INTO run_config VALUES (?,?,?)", ("test", time.time(), json.dumps({"mode": "demo", "dataset": "demo", "secret": "never-return-this"})))
    conn.close()
    return path


def insert_position(path, ticker="BTC-15M", asset="BTC", opened=None, quantity=3, cutoff_offset=-1):
    now = time.time() if opened is None else opened
    market = Market(ticker, asset, D("100"), now - 100, now + cutoff_offset, now + 60, now + 90,
                    FeeConfig(D(1), verified=True), verified=True)
    market_json = json.dumps(jsonable(market))
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT OR REPLACE INTO markets VALUES (?,?)", (ticker, market_json))
        conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ticker, "test", ticker, asset, "yes", "1", now, quantity, market_json, "filled", now, now, None, None,))
        conn.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (ticker, ticker, "test", ticker, asset, "yes", quantity, quantity, "1", "1", "0", market_json, now, None, "pending", 1))
        conn.execute("UPDATE accounts SET cash='249'")
    conn.close()
    return market


def test_missing_database_is_explicit_and_not_created(tmp_path):
    path = tmp_path / "not-created.sqlite"
    with TestClient(create_app(path)) as client:
        response = client.get("/health")
        assert response.status_code == 503
        assert response.json()["database"]["status"] == "uninitialized"
        state = client.get("/api/state").json()
        assert state["paper"] is True
        assert state["summary"] is None
        assert state["positions"] == []
        assert client.get("/api/trades").status_code == 503
    assert not path.exists()


def test_empty_database_is_not_a_fabricated_portfolio(tmp_path):
    path = tmp_path / "empty.sqlite"
    sqlite3.connect(path).close()
    with TestClient(create_app(path)) as client:
        state = client.get("/api/state").json()
        assert state["database"]["status"] == "uninitialized"
        assert "accounts" in state["database"]["missing_tables"]
        assert state["summary"] is None


def test_initialized_state_is_paper_readonly_and_does_not_expose_secrets(database):
    before = hashlib.sha256(database.read_bytes()).digest()
    with TestClient(create_app(database)) as client:
        assert client.get("/health").status_code == 200
        response = client.get("/api/state")
        assert "never-return-this" not in response.text
        state = response.json()
        assert state["data_mode"] == "demo"
        assert state["database"] == {"status": "ready", "read_only": True}
        assert state["summary"]["cash"] == "250"
        assert state["summary"]["net_pnl"] == "0"
        assert len(state["assets"]) == 3
        assert all(card["reference_stale"] for card in state["assets"])
        assert all(card["reasons"] == ["no_decision_recorded"] for card in state["assets"])
        assert client.post("/api/trades", json={"order": "buy"}).status_code == 405
    assert hashlib.sha256(database.read_bytes()).digest() == before


def test_unpriced_open_position_never_creates_cash_at_cutoff(database):
    insert_position(database)
    with TestClient(create_app(database)) as client:
        state = client.get("/api/state").json()
    assert state["summary"]["cash"] == "249"
    assert state["summary"]["equity"] == "249"
    assert state["summary"]["pending_settlement"] == 1
    assert state["summary"]["unpriced_quantity"] == 3
    assert state["positions"][0]["valuation"] == "unpriced_zero_mark"


def test_all_skip_reasons_reference_quotes_and_fresh_marks(database):
    market = insert_position(database, cutoff_offset=300)
    now = time.time()
    book = {"ticker": market.ticker, "yes_bids": [[".6", 10]], "no_bids": [[".3", 10]], "healthy": True, "received_at": now}
    reference = {"asset": "BTC", "price": "99.9", "source_at": now}
    decision = {"ticker": market.ticker, "now": now, "side": "yes", "reasons": ["outside_entry_window", "insufficient_momentum"], "metrics": {}}
    with sqlite3.connect(database) as conn:
        for index, (kind, payload) in enumerate((("reference", reference), ("book", book))):
            conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (index, str(index), "test", kind, now, json.dumps(payload)))
        conn.execute("INSERT INTO decisions VALUES (?,?,?,?,?)", (1, "test", market.ticker, now, json.dumps(decision)))
    with TestClient(create_app(database)) as client:
        state = client.get("/api/state").json()
    card = next(card for card in state["assets"] if card["asset"] == "BTC")
    assert card["reasons"] == decision["reasons"]
    assert card["quotes"]["yes_ask"] == "0.7"
    assert not card["reference_stale"]
    assert not card["book_stale"]
    assert state["positions"][0]["valuation"] == "executable_fee_net"
    assert D(state["summary"]["equity"]) > D("250")


def test_filtered_pagination_and_csv_formula_defense(database):
    insert_position(database, "=HYPERLINK(\"bad\")", opened=time.time()-50)
    insert_position(database, "ETH-15M", "ETH", opened=time.time()-10)
    with TestClient(create_app(database)) as client:
        page = client.get("/api/trades?limit=1&offset=1").json()
        assert page["total"] == 2
        assert len(page["items"]) == 1
        assert page["items"][0]["asset"] == "BTC"
        filtered = client.get("/api/trades?asset=ETH&side=yes&status=pending").json()
        assert filtered["total"] == 1
        assert filtered["items"][0]["market_ticker"] == "ETH-15M"
        download = client.get("/api/export.csv?asset=BTC")
        rows = list(csv.DictReader(io.StringIO(download.text)))
        assert len(rows) == 1
        assert rows[0]["market_ticker"].startswith("'=")
        assert "attachment" in download.headers["content-disposition"]
        assert client.get("/api/export.json?asset=ETH").json()["total"] == 1


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "asset=BTC%27%20OR%201=1--", "side=up", "status=won", "started_after=20&started_before=10", "started_after=nan", "started_before=inf", "started_after=-1"])
def test_filters_are_validated(database, query):
    with TestClient(create_app(database)) as client:
        assert client.get("/api/trades?" + query).status_code == 422


def test_static_ui_is_local_and_paths_are_fixed(database):
    with TestClient(create_app(database)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "PAPER TRADING ONLY" in page.text
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/static/%2E%2E/dashboard.py").status_code == 404
        assert client.get("/api/export.yaml").status_code == 422


def test_connection_rejects_mutation(database):
    with _database(database) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("UPDATE accounts SET cash='999'")


def test_busy_unrelated_stream_does_not_hide_reference(database):
    now = time.time()
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO events(event_id,run_id,type,received_at,payload) VALUES (?,?,?,?,?)",
                     ("reference", "test", "reference", now, json.dumps({"asset": "BTC", "price": "100"})))
        conn.executemany("INSERT INTO events(event_id,run_id,type,received_at,payload) VALUES (?,?,?,?,?)",
                         [(str(i), "test", "exchange_trade", now, "{}") for i in range(12001)])
    conn.close()
    with TestClient(create_app(database)) as client:
        card = client.get("/api/state").json()["assets"][0]
    assert card["reference"]["price"] == "100"
    assert not card["reference_stale"]


def test_fresh_book_after_cutoff_is_not_executable_or_settlement(database):
    market = insert_position(database)
    now = time.time()
    payload = {"ticker": market.ticker, "yes_bids": [[".6", 10]], "no_bids": [[".3", 10]], "healthy": True}
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO events(event_id,run_id,type,received_at,payload) VALUES (?,?,?,?,?)",
                     ("book", "test", "book", now, json.dumps(payload)))
    conn.close()
    with TestClient(create_app(database)) as client:
        result = client.get("/api/state").json()
    assert result["positions"][0]["valuation"] == "awaiting_official_settlement_zero_mark"
    assert result["summary"]["cash"] == result["summary"]["equity"] == "249"


def test_mixed_demo_and_real_runs_are_labeled(database):
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO run_config VALUES (?,?,?)", ("real", time.time(), json.dumps({"mode": "paper", "dataset": "real"})))
    conn.close()
    with TestClient(create_app(database)) as client:
        assert client.get("/api/state").json()["data_mode"] == "mixed_demo_and_real"


@pytest.mark.parametrize("official,dataset,accepted", [(True, "real", True), (False, "demo", True), (False, "real", False)])
def test_asset_settlement_requires_official_or_labeled_demo(database, official, dataset, accepted):
    market = insert_position(database)
    now = time.time()
    event = {"ticker": market.ticker, "result": "no", "official": official, "dataset": dataset}
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO events(event_id,run_id,type,received_at,payload) VALUES (?,?,?,?,?)",
                     ("settlement", "test", "settlement", now, json.dumps(event)))
    conn.close()
    with TestClient(create_app(database)) as client:
        settlement = client.get("/api/state").json()["assets"][0]["settlement"]
    if accepted:
        assert settlement == {"result": "no", "dataset": dataset, "official": official, "received_at": now}
    else:
        assert settlement is None


def test_no_fill_marker_retains_outcome_side(database):
    market = insert_position(database)
    now = time.time()
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE positions SET side='no' WHERE id=?", (market.ticker,))
        conn.execute("UPDATE orders SET side='no' WHERE id=?", (market.ticker,))
        conn.execute("INSERT INTO fills VALUES (?,?,?,?,?,?,?,?)",
                     ("no-fill", market.ticker, market.ticker, "entry", ".3", 3, "0", now))
    conn.close()
    with TestClient(create_app(database)) as client:
        marker = client.get("/api/state").json()["assets"][0]["fills"][0]
    assert marker["side"] == "no"
    assert marker["price"] == ".3"
