import base64
import copy
import json
import asyncio

import pytest
import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.feeds import (API_HOSTS, Auth, BookReconstructor, FeedGap,
                              _validate_url, iso_time, load_overrides,
                              normalize_market, normalize_reference, rule_hash, live_events)
from kalshi_bot.models import D


@pytest.fixture
def market():
    return {
        "ticker": "KXSOL15M-26OCT171230-30", "event_ticker": "KXSOL15M-26OCT171230",
        "market_type": "binary", "strike_type": "greater_or_equal", "floor_strike": 103.767,
        "open_time": "2026-10-17T16:15:00Z", "close_time": "2026-10-17T16:30:00Z",
        "expiration_time": "2026-10-24T16:30:00Z", "custom_strike": {"round_digits": "4"},
        "price_level_structure": "tapered_deci_cent",
        "price_ranges": [{"start": "0", "end": "0.1", "step": "0.001"},
                         {"start": "0.1", "end": "0.9", "step": "0.01"},
                         {"start": "0.9", "end": "1", "step": "0.001"}],
        "rules_primary": "If the simple average of the sixty seconds of CF Benchmarks' SOLUSDRTI before 12:30 PM EDT on Oct 17, 2026 is at least the simple average of the sixty seconds of CF Benchmarks' SOLUSDRTI before 12:15 PM EDT on October 17, 2026, then the market resolves to Yes.",
        "rules_secondary": "Not all cryptocurrency price data is the same. While checking a source like Google or Coinbase may help guide your decision, the price used to determine this market is based on CF Benchmarks' corresponding Real Time Index (RTI). At the last minute before expiration, 60 RTI prices are collected. The official and final value is the average of these prices, rounded to the nearest 4 decimal places.",
    }


@pytest.fixture
def series():
    return {"ticker": "KXSOL15M", "frequency": "fifteen_min", "fee_type": "quadratic", "fee_multiplier": 1}


def normalized(market, series, fees=None, config=None, overrides=None):
    return normalize_market(market, series, [] if fees is None else fees, config or {}, "SOL",
                            iso_time("2026-10-17T16:20:00Z"), overrides)


def test_actual_official_fields_and_rounding(market, series):
    m = normalized(market, series)
    assert m.verified, m.invalid_reasons
    assert m.strike == D("103.7670")
    assert m.cutoff == m.observation_end == iso_time("2026-10-17T16:30:00Z")
    assert m.expiration != m.cutoff
    assert [r.step for r in m.price_ranges] == [D(".001"), D(".01"), D(".001")]
    assert m.fee.multiplier == 1
    assert not m.fee.verified and m.fee.scenario is None


@pytest.mark.parametrize("mutate,reason", [
    (lambda m: m.pop("floor_strike"), "official_strike_unresolved"),
    (lambda m: m.update(strike_type="greater"), "outcome_mapping_unrecognized"),
    (lambda m: m.update(rules_primary=m["rules_primary"].replace("SOLUSDRTI", "BRTI")), "exact_rule_observation_window_unresolved"),
    (lambda m: m.update(rules_primary=m["rules_primary"].replace("EDT", "EST")), "exact_rule_observation_window_unresolved"),
    (lambda m: m.update(rules_secondary=m["rules_secondary"] + " New settlement exception."), "reference_rounding_unresolved"),
    (lambda m: m.update(custom_strike={"round_digits": "2"}), "reference_rounding_unresolved"),
    (lambda m: m.update(close_time="2026-10-17T16:35:00Z"), "contract_timestamps_conflict"),
    (lambda m: m.update(open_time="2026-10-17T16:15:00"), "open_time_unresolved"),
    (lambda m: m.update(price_ranges=[{"start": "0", "end": "1", "step": "0.003"}]), "tick_structure_unresolved"),
])
def test_unknown_contract_semantics_disable_entries(market, series, mutate, reason):
    mutate(market)
    m = normalized(market, series)
    assert not m.verified
    assert reason in m.invalid_reasons


def test_market_strike_never_inferred_from_quote_or_title(market, series):
    market.pop("floor_strike")
    market.update(yes_sub_title="Target Price: $103.7670", last_price_dollars="0.36")
    m = normalized(market, series)
    assert not m.verified and m.strike == 0


def test_event_fee_overrides_are_time_and_event_specific(market, series):
    def change(id, multiplier, when, ticker=None):
        return {"id": id, "event_ticker": ticker or market["event_ticker"], "scheduled_ts": when,
                "fee_type_override": "quadratic" if multiplier is not None else None,
                "fee_multiplier_override": multiplier}
    changes = [change("old", 2, "2026-10-17T16:00:00Z"),
               change("future", 4, "2026-10-17T16:25:00Z"),
               change("other", 9, "2026-10-17T16:10:00Z", "OTHER")]
    assert normalized(market, series, changes).fee.multiplier == 2
    changes.append(change("clear", None, "2026-10-17T16:19:00Z"))
    assert normalized(market, series, changes).fee.multiplier == 1


def test_explicit_scenario_is_never_fee_verified(market, series):
    m = normalized(market, series, config={"fee_scenario": {
        "name": "M2-rounding-unverified", "multiplier": "2", "balance_quantum": "0.01"}})
    assert m.fee.multiplier == 2 and m.fee.balance_quantum == D(".01")
    assert not m.fee.verified and m.fee.scenario == "M2-rounding-unverified"
    m = normalized(market, series, config={"fee_scenario": {"multiplier": "1"}})
    assert not m.verified and "fee_scenario_invalid" in m.invalid_reasons


def test_exact_review_hash_required_for_override(tmp_path, market, series):
    market["rules_primary"] = "A separately reviewed exact rule."
    override = {"rules_sha256": rule_hash(market), "observation_end": "2026-10-17T16:30:00Z",
                "rounding_digits": 4, "evidence": "https://assets.kalshi.com/contract_terms/CRYPTO.pdf"}
    path = tmp_path / "review.json"
    path.write_text(json.dumps({"markets": {market["ticker"]: override}}))
    overrides = load_overrides(str(path))
    assert normalized(market, series, overrides=overrides).verified
    market["rules_primary"] += " Changed after review."
    m = normalized(market, series, overrides=overrides)
    assert not m.verified and "reviewed_rule_hash_changed" in m.invalid_reasons
    override["strike"] = "100"
    path.write_text(json.dumps({"markets": {market["ticker"]: override}}))
    with pytest.raises(ValueError):
        load_overrides(str(path))


def snapshot(ticker="KXSOL15M-T", seq=1, sid=2):
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker, "yes_dollars_fp": [["0.36", "10.75"]],
                    "no_dollars_fp": [["0.60", "20.25"]]}}


def delta(ticker="KXSOL15M-T", seq=2, sid=2, **fields):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker, "side": "yes", "price_dollars": "0.36", "delta_fp": "-0.5", **fields}}


def test_fractional_depth_accumulates_before_integer_floor():
    books = BookReconstructor()
    assert books.apply(snapshot(), 10)["yes_bids"] == [["0.36", 10]]
    assert books.apply(delta(), 11)["yes_bids"] == [["0.36", 10]]
    assert books.apply(delta(seq=3), 12)["yes_bids"] == [["0.36", 9]]
    assert books.apply(delta(seq=4, delta_fp="-9.75"), 13)["yes_bids"] == []


def test_interleaved_tickers_share_subscription_sequence():
    books = BookReconstructor()
    books.apply(snapshot(ticker="A"), 10)
    books.apply(snapshot(ticker="B", seq=2), 11)
    books.apply(delta(ticker="A", seq=3), 12)
    assert books.apply(delta(ticker="B", seq=4), 13)["healthy"]


def test_duplicates_do_not_reapply_and_gaps_require_new_snapshot():
    books = BookReconstructor()
    books.apply(snapshot(), 10)
    books.apply(delta(), 11)
    assert books.apply(delta(), 12) is None
    with pytest.raises(FeedGap, match="sequence_gap"):
        books.apply(delta(seq=4), 13)
    assert not books.books
    with pytest.raises(FeedGap, match="before_snapshot"):
        books.apply(delta(seq=5), 14)
    assert books.apply(snapshot(seq=10), 15)["healthy"]


def test_bad_delta_also_invalidates_previous_state():
    books = BookReconstructor()
    books.apply(snapshot(), 10)
    with pytest.raises(FeedGap, match="negative_book_quantity"):
        books.apply(delta(delta_fp="-99"), 11)
    assert not books.books
    with pytest.raises(FeedGap):
        books.apply(delta(seq=3), 12)


def test_delta_before_snapshot_and_crossed_book():
    books = BookReconstructor()
    with pytest.raises(FeedGap, match="before_snapshot"):
        books.apply(delta(), 10)
    payload = snapshot()
    payload["msg"]["no_dollars_fp"] = [["0.70", "10"]]
    assert not books.apply(payload, 11)["healthy"]


def test_subscription_needs_its_own_snapshot():
    books = BookReconstructor()
    books.apply(snapshot(sid=1), 10)
    with pytest.raises(FeedGap, match="before_snapshot"):
        books.apply(delta(sid=2), 11)


def test_reference_uses_raw_official_value_not_average():
    payload = {"type": "cfbenchmarks_value", "msg": {"index_id": "BRTI",
        "data": json.dumps({"type": "value", "id": "BRTI", "time": 1710000000123, "value": "68000.12"}),
        "avg_60s_data": {"value": "67999.99"}, "received_at": 1710000000124}}
    value = normalize_reference(payload, 1710000001)
    assert value["price"] == "68000.12" and value["source_at"] == 1710000000.123
    assert value["source"] == "cfbenchmarks_value" and value["asset"] == "BTC"
    assert value["received_at"] == 1710000001
    bad = copy.deepcopy(payload)
    bad["msg"]["data"] = json.dumps({"type": "value", "id": "ETHUSD_RTI", "time": 1, "value": "1"})
    with pytest.raises(ValueError, match="mismatch"):
        normalize_reference(bad, 1710000001)
    assert normalize_reference({"type": "ticker", "msg": {"price": 60000}}, 1) is None


def test_rsa_pss_get_signature_and_secret_safe_representation():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    auth = Auth("sensitive-key-identifier", private_key)
    headers = auth.headers("https://external-api.kalshi.com/trade-api/v2/markets?limit=5", 123456)
    private_key.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        b"123456GET/trade-api/v2/markets",
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    assert "sensitive-key-identifier" not in repr(auth)
    assert "PRIVATE" not in repr(auth)


def test_credentials_not_required_for_public_collection(monkeypatch):
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    assert Auth.from_env() is None


@pytest.mark.parametrize("url", ["http://external-api.kalshi.com/trade-api/v2",
    "https://attacker.example/trade-api/v2", "https://external-api.kalshi.com/trade-api/v2?secret=x",
    "https://username:password@external-api.kalshi.com/trade-api/v2"])
def test_configuration_cannot_send_signatures_to_arbitrary_destination(url):
    with pytest.raises(ValueError):
        _validate_url(url, API_HOSTS, "https", "/trade-api/v2")


@pytest.mark.asyncio
async def test_no_credentials_collects_real_shaped_rest_books_without_reference(monkeypatch, market, series):
    import kalshi_bot.feeds as feeds
    original_client = httpx.AsyncClient
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setattr(feeds.time, "time", lambda: iso_time("2026-10-17T16:20:00Z"))
    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/series/KXSOL15M"):
            body = {"series": series}
        elif request.url.path.endswith("/markets"):
            body = {"markets": [market], "cursor": ""}
        elif request.url.path.endswith("/events/fee_changes"):
            assert request.url.params["event_ticker"] == market["event_ticker"]
            body = {"event_fee_changes": [], "cursor": ""}
        elif request.url.path.endswith("/orderbook"):
            body = {"orderbook_fp": {"yes_dollars": [["0.30", "10.25"]], "no_dollars": [["0.65", "11.25"]]}}
        else:
            raise AssertionError(str(request.url))
        return httpx.Response(200, json=body)
    monkeypatch.setattr(feeds.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))
    values = []
    feed = live_events({"assets": ["SOL"]})
    try:
        async with asyncio.timeout(3):
            async for value in feed:
                values.append(value)
                if value["type"] == "book":
                    break
    finally:
        await feed.aclose()
    book = values[-1]
    assert book["yes_bids"] == [["0.30", 10]] and not book["healthy"]
    assert "stream_continuity_unverified" in book["quality_flags"]
    assert not any(v["type"] == "reference" for v in values)
    assert any(v["type"] == "market" for v in values)


@pytest.mark.asyncio
async def test_restart_polling_observes_official_settlement_only(monkeypatch, market, series):
    import kalshi_bot.feeds as feeds
    original_client = httpx.AsyncClient
    monkeypatch.delenv("KALSHI_API_KEY_ID", raising=False)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    market.update(status="finalized", result="yes", settlement_time="2026-10-17T16:35:01Z")
    def handler(request):
        assert request.method == "GET"
        if "/series/" in request.url.path:
            body = {"series": series}
        elif request.url.path.endswith("/markets"):
            body = {"markets": [], "cursor": ""}
        elif request.url.path.endswith("/events/fee_changes"):
            body = {"event_fee_changes": [], "cursor": ""}
        elif request.url.path.endswith("/" + market["ticker"]):
            body = {"market": market}
        else:
            raise AssertionError(str(request.url))
        return httpx.Response(200, json=body)
    monkeypatch.setattr(feeds.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))
    feed = live_events({"assets": ["SOL"], "track_tickers": [market["ticker"]]})
    try:
        async with asyncio.timeout(3):
            async for value in feed:
                if value["type"] == "settlement":
                    assert value["official"] is True and value["source"] == "kalshi"
                    assert value["result"] == "yes" and value["ticker"] == market["ticker"]
                    break
    finally:
        await feed.aclose()


@pytest.mark.asyncio
async def test_cf_sequence_gap_disconnects_and_no_secrets_enter_events(monkeypatch, market, series):
    import kalshi_bot.feeds as feeds
    original_client = httpx.AsyncClient
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(Auth, "from_env", lambda: Auth("private-key-identifier-never-export", private_key))
    def handler(request):
        if "/series/" in request.url.path:
            body = {"series": series}
        elif request.url.path.endswith("/markets"):
            body = {"markets": [market], "cursor": ""}
        else:
            body = {"event_fee_changes": [], "cursor": ""}
        return httpx.Response(200, json=body)
    monkeypatch.setattr(feeds.httpx, "AsyncClient", lambda **kwargs: original_client(
        transport=httpx.MockTransport(handler), **kwargs))
    class Socket:
        def __init__(self):
            self.packets = [
                {"type": "subscribed", "msg": {"channel": "cfbenchmarks_value", "sid": 1}},
                {"type": "cfbenchmarks_value", "sid": 1, "seq": 1, "msg": {"index_id": "SOLUSD_RTI",
                    "data": json.dumps({"type": "value", "id": "SOLUSD_RTI", "time": 1710000000000, "value": "100"})}},
                {"type": "cfbenchmarks_value", "sid": 1, "seq": 3, "msg": {"index_id": "SOLUSD_RTI",
                    "data": json.dumps({"type": "value", "id": "SOLUSD_RTI", "time": 1710000002000, "value": "101"})}},
            ]
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def send(self, data): pass
        async def recv(self):
            if self.packets:
                return json.dumps(self.packets.pop(0))
            await asyncio.sleep(20)
    def connect(url, **kwargs):
        assert kwargs["additional_headers"]["KALSHI-ACCESS-KEY"] == "private-key-identifier-never-export"
        return Socket()
    monkeypatch.setattr(feeds.websockets, "connect", connect)
    values = []
    feed = live_events({"assets": ["SOL"]})
    try:
        async with asyncio.timeout(3):
            async for value in feed:
                values.append(value)
                if value["type"] == "health" and value["details"]["reason"] == "reference_sequence_gap":
                    assert value["details"]["status"] == "disconnected"
                    break
    finally:
        await feed.aclose()
    assert len([v for v in values if v["type"] == "reference"]) == 1
    assert "private-key-identifier-never-export" not in json.dumps(values)
    assert "KALSHI-ACCESS-SIGNATURE" not in json.dumps(values)
