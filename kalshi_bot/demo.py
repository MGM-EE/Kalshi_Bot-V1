"""Deterministic synthetic data, intentionally unrelated to market performance."""
from __future__ import annotations

from .models import D, FeeConfig, Level, Market, OrderBook, jsonable

START = 1792238400.0


def demo_events():
    strikes = {"BTC": D("80100"), "ETH": D("2501"), "SOL": D("100.1")}
    markets = {}
    for asset, strike in strikes.items():
        market = Market(
            ticker=f"DEMO-{asset}-ROUND-1", asset=asset, strike=strike,
            open_time=START, cutoff=START + 900, observation_end=START + 900,
            expiration=START + 960, verified=True,
            fee=FeeConfig(D(1), D("0.0001"), False, "synthetic-demo"),
            rules="SYNTHETIC DEMO ONLY. YES means final-window reference at least the strike.",
        )
        markets[asset] = market
        yield {"type": "market", "event_id": f"demo-market-{asset}",
               "received_at": START, "dataset": "demo", "market": jsonable(market)}

    for second in range(911):
        now = START + second
        prices = {"BTC": D("79940") + D("0.2") * second,
                  "ETH": D("2498") + D("0.0075") * second,
                  "SOL": D("99.94") + D("0.0003") * min(second, 450)
                         - D("0.0005") * max(0, second - 450)}
        for asset, price in prices.items():
            yield {"type": "reference", "event_id": f"demo-reference-{asset}-{second}",
                   "received_at": now, "source_at": now, "asset": asset,
                   "price": str(price), "source": "cfbenchmarks_value", "dataset": "demo"}
            bid = D("0.96") if asset != "SOL" and second >= 840 else D("0.32")
            no_bid = D("0.03") if bid == D("0.96") else D("0.62")
            book = OrderBook(markets[asset].ticker, (Level(bid, 100),),
                             (Level(no_bid, 100),), now, True, second + 1)
            yield {"type": "book", "event_id": f"demo-book-{asset}-{second}",
                   "received_at": now, "dataset": "demo", **jsonable(book)}
        yield {"type": "clock", "event_id": f"demo-clock-{second}",
               "received_at": now, "dataset": "demo"}
        if second == 910:
            for asset, market in markets.items():
                yield {"type": "settlement", "event_id": f"demo-settlement-{asset}",
                       "received_at": now, "dataset": "demo", "ticker": market.ticker,
                       "result": "no" if asset == "SOL" else "yes", "official": False}
