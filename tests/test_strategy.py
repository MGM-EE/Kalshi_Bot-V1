from dataclasses import replace

import pytest

from kalshi_bot.models import D, FeeConfig, Level, Market, OrderBook, PriceRange, ReferenceTick
from kalshi_bot.strategy import (
    FeeAccumulator, ask_levels, bid_levels, evaluate, fee_available, fee_for_fill,
    plan_entry, plan_exit, validate_book,
)


FEE = FeeConfig(multiplier=D(1), verified=True)
NOW = 1000.0


def book(yes=((".35", 100),), no=((".62", 100),), **kwargs):
    return OrderBook("BTC-ROUND", tuple(Level(D(price), count) for price, count in yes),
                     tuple(Level(D(price), count) for price, count in no),
                     kwargs.pop("received_at", NOW), **kwargs)


def market(**kwargs):
    defaults = dict(ticker="BTC-ROUND", asset="BTC", strike=D(100000), open_time=400,
                    cutoff=1120, observation_end=1120, expiration=1180, fee=FEE, verified=True)
    defaults.update(kwargs)
    return Market(**defaults)


def history(prices=None):
    prices = prices or [D(99984) + D(i) / 5 for i in range(31)]
    return [ReferenceTick("BTC", price, NOW - 30 + i, NOW - 30 + i)
            for i, price in enumerate(prices)]


def test_documented_quadratic_example_and_balance_rounding():
    assert fee_for_fill(D(".38"), 12, FEE) == D(".1980")
    quote = plan_entry(book(), "yes", D(5), FEE)
    assert quote.quantity == 12
    assert quote.cost == D("4.7580")
    assert quote.cost / quote.quantity == D(".3965")


def test_order_accumulator_reuses_previous_rounding_overpayment():
    fee = replace(FEE, balance_quantum=D(".01"))
    accumulator = FeeAccumulator(fee)
    assert accumulator.charge(D(".38"), 1) == D(".02")
    assert accumulator.model_fees == D(".016492")
    assert accumulator.rounding_overpayment == D(".003508")
    assert accumulator.charge(D(".39"), 1) == D(".02")
    assert accumulator.charge(D(".37"), 1) == D(".01")
    assert accumulator.charged == D(".05")
    assert accumulator.rounding_overpayment == D(".000538")
    assert sum(fee_for_fill(D(price), 1, fee) for price in (".38", ".39", ".37")) == D(".06")


def test_six_decimal_rounding_occurs_per_fill_before_balance_accumulator():
    fee = replace(FEE, balance_quantum=D(".000001"))
    accumulator = FeeAccumulator(fee)
    assert accumulator.charge(D(".334"), 1) == D(".015572")
    assert accumulator.charge(D(".334"), 1) == D(".015572")
    assert fee_for_fill(D(".334"), 2, fee) == D(".031143")
    assert accumulator.charged == D(".031144")


def test_documented_non_direct_subcent_balance_rounding_and_rebate_cap():
    fee = replace(FEE, balance_quantum=D(".01"))
    accumulator = FeeAccumulator(fee)
    assert accumulator.charge(D(".055"), 1) == D(".005000")
    assert accumulator.model_fees == D(".003639")
    assert accumulator.rounding_overpayment == D(".001361")
    assert D(".055") + accumulator.charged == D(".060000")
    assert fee_for_fill(D(".055"), 1, fee, "sell") == D(".005000")
    # At zero model fee each .055 fill rounds a half-cent, but a cent rebate
    # would make this fill's net fee negative, so the credit must carry forward.
    zero_fee = FeeAccumulator(replace(fee, multiplier=D(0)))
    for _ in range(3):
        assert zero_fee.charge(D(".055"), 1) == D(".005")
    assert zero_fee.rounding_overpayment == D(".015")


@pytest.mark.parametrize("fee", [FeeConfig(), FeeConfig(multiplier=D(1)),
                                 replace(FEE, model="unknown"),
                                 replace(FEE, multiplier=D(-1)),
                                 replace(FEE, balance_quantum=D(0))])
def test_unknown_or_invalid_fees_are_collect_only(fee):
    assert not fee_available(fee)
    assert plan_entry(book(), "yes", D(5), fee) is None
    assert plan_exit(book(yes=((".96", 10),), no=((".02", 10),)), "yes", 1, fee) is None


def test_explicit_unverified_fee_scenario_is_usable_and_annotated():
    fee = FeeConfig(multiplier=D(1), scenario="M1-unverified-direct")
    decision = evaluate(market(fee=fee), history(), book(), NOW, None, D(5))
    assert decision.eligible
    assert not decision.metrics["fee_verified"]
    assert decision.metrics["fee_scenario"] == "M1-unverified-direct"


def test_complementary_book_symmetry_and_sorted_depth():
    original = book(yes=((".34", 10), (".35", 20)), no=((".61", 8), (".62", 12)))
    assert ask_levels(original, "yes") == (Level(D(".38"), 12), Level(D(".39"), 8))
    assert bid_levels(original, "yes")[0] == Level(D(".35"), 20)
    swapped = replace(original, yes_bids=original.no_bids, no_bids=original.yes_bids)
    assert plan_entry(original, "yes", D(5), FEE) == plan_entry(swapped, "no", D(5), FEE)


@pytest.mark.parametrize("example,reason", [
    (book(yes=((".40", 10),), no=((".61", 10),)), "crossed_book"),
    (book(yes=((".351", 10),)), "off_tick_book_price"),
    (book(yes=((".35", 0),)), "invalid_book_quantity"),
    (book(yes=((".35", 1), (".35", 2))), "duplicate_book_level"),
    (book(yes=(("NaN", 1),)), "invalid_book_price"),
])
def test_invalid_books_cannot_generate_fills(example, reason):
    assert reason in validate_book(example)
    assert plan_entry(example, "yes", D(5), FEE) is None


def test_actual_tapered_price_ranges_allow_subcent_tails_but_reject_middle():
    ranges = (PriceRange(D(0), D(".1"), D(".001")),
              PriceRange(D(".1"), D(".9"), D(".01")),
              PriceRange(D(".9"), D(1), D(".001")))
    tails = book(yes=((".093", 20),), no=((".905", 20),))
    assert not validate_book(tails, price_ranges=ranges)
    assert plan_entry(tails, "yes", D(5), FEE, price_ranges=ranges)
    assert "off_tick_book_price" in validate_book(book(yes=((".351", 20),)), price_ranges=ranges)
    for boundary in (".1", ".9"):
        assert not validate_book(book(yes=((boundary, 2),), no=()), price_ranges=ranges)


def test_conservative_depth_is_floored_per_level_and_max_quantity_is_respected():
    shallow = book(no=((".63", 5), (".62", 3)))
    assert plan_entry(shallow, "yes", D(5), FEE).quantity == 3
    assert plan_entry(shallow, "yes", D(5), FEE, depth_fraction=D(1)).quantity == 8
    assert plan_entry(shallow, "yes", D(5), FEE, max_quantity=2).quantity == 2
    assert plan_entry(book(no=((".62", 1),)), "yes", D(5), FEE) is None
    assert plan_entry(book(no=()), "yes", D(5), FEE) is None


def test_all_in_ceiling_rejects_40_cent_ask_and_unsafe_partial_prefix():
    assert plan_entry(book(no=((".60", 100),)), "yes", D(5), FEE) is None
    # A deliberately coarse scenario illustrates why aggregate quantity alone
    # cannot establish safe partial execution: first balance-rounded share
    # costs .42, while twelve average below .40.
    fee = replace(FEE, balance_quantum=D(".03"), verified=False, scenario="coarse-rounding-stress")
    assert (D(".38") * 12 + fee_for_fill(D(".38"), 12, fee)) / 12 < D(".40")
    assert plan_entry(book(), "yes", D(5), fee) is None


def test_more_than_one_hundred_contract_prefixes_are_checked_without_cap():
    cheap = book(yes=((".001", 1000),), no=((".99", 1000),))
    quote = plan_entry(cheap, "yes", D(5), FEE, tick_size=D(".001"))
    assert quote.quantity == 467
    assert quote.cost == D("4.993700")
    accumulator = FeeAccumulator(FEE)
    for prefix in range(1, quote.quantity + 1):
        cost = D(".01") * prefix + accumulator.preview(D(".01"), prefix)
        assert cost <= D(5)
        assert cost / prefix <= D(".40")
    assert D(".01") * 468 + accumulator.preview(D(".01"), 468) > D(5)


def test_all_prefixes_stay_under_budget_and_all_in_ceiling_across_price_levels():
    example = book(no=((".63", 10), (".62", 10), (".60", 100)))
    quote = plan_entry(example, "yes", D(5), FEE)
    assert quote is not None
    principal = D(0)
    quantity = 0
    accumulator = FeeAccumulator(FEE)
    for fill in quote.fills:
        for prefix in range(1, fill.quantity + 1):
            cost = principal + fill.price * prefix + accumulator.charged + accumulator.preview(fill.price, prefix)
            assert cost <= D(5)
            assert cost <= D(".40") * (quantity + prefix)
        principal += fill.price * fill.quantity
        quantity += fill.quantity
        accumulator.charge(fill.price, fill.quantity)


def test_exit_uses_bid_only_at_95_cents_and_permits_remaining_partial():
    example = book(yes=((".94", 100), (".95", 4), (".97", 2)), no=((".02", 10),))
    quote = plan_exit(example, "yes", 20, FEE)
    assert quote.quantity == 3
    assert [fill.price for fill in quote.fills] == [D(".97"), D(".95")]
    assert quote.proceeds < quote.principal
    assert plan_exit(book(yes=((".94", 100),), no=((".02", 100),)), "yes", 1, FEE) is None
    assert plan_exit(example, "yes", 20, FEE, min_price=D(0)).quantity == 20


@pytest.mark.parametrize("seconds,eligible", [(119.999, False), (120, True), (600, True), (600.001, False)])
def test_entry_window_exact_inclusive_boundaries(seconds, eligible):
    decision = evaluate(market(cutoff=NOW + seconds), history(), book(), NOW, None, D(5))
    assert decision.eligible is eligible
    assert ("outside_entry_window" in decision.reasons) is not eligible


def test_down_direction_and_yes_mapping_reversal_are_symmetric():
    prices = [D(100016) - D(i) / 5 for i in range(31)]
    down_book = book(yes=((".62", 100),), no=((".35", 100),))
    decision = evaluate(market(), history(prices), down_book, NOW, None, D(5))
    assert decision.eligible and decision.side == "no"
    assert decision.metrics["direction"] == "DOWN"
    assert decision.metrics["momentum"] == D(6)
    reversed_mapping = evaluate(market(yes_means_up=False), history(prices), book(), NOW, None, D(5))
    assert reversed_mapping.eligible and reversed_mapping.side == "yes"


def test_distance_and_momentum_threshold_and_reach_equalities():
    prices = [D(99795) + D(i) / 6 for i in range(31)]
    decision = evaluate(market(cutoff=NOW + 1200), history(prices), book(), NOW, None, D(5))
    assert decision.metrics["distance_fraction"] == D(".002")
    assert "strike_too_far" not in decision.reasons
    assert "momentum_below_threshold" not in decision.reasons
    assert "strike_not_reachable" not in decision.reasons
    assert "outside_entry_window" in decision.reasons
    # Gap 20 / (5/30) is exactly the 120-second cutoff boundary.
    prices = [D(99975) + D(i) / 6 for i in range(31)]
    decision = evaluate(market(), history(prices), book(), NOW, None, D(5))
    assert decision.eligible
    assert decision.metrics["reach_seconds"] == D(120)


def test_equal_strike_never_enters():
    decision = evaluate(market(strike=D(99990)), history(), book(), NOW, None, D(5))
    assert "at_strike" in decision.reasons
    assert decision.side is None and not decision.eligible


def test_latest_reference_is_causal_in_source_and_receipt_time():
    extra = [ReferenceTick("BTC", D(200000), NOW + 1, NOW),
             ReferenceTick("BTC", D(200000), NOW, NOW + 1),
             ReferenceTick("BTC", D(200000), NOW, NOW, "coinbase"),
             ReferenceTick("ETH", D(1), NOW, NOW)]
    decision = evaluate(market(), history() + extra, book(), NOW, None, D(5))
    assert decision.eligible
    assert decision.metrics["reference_price"] == D(99990)
    assert decision.metrics["ignored_future_ticks"] == 2
    assert decision.metrics["ignored_wrong_source_ticks"] == 1


def test_delayed_history_does_not_retroactively_fill_missing_sample():
    ticks = history()
    ticks[0] = replace(ticks[0], received_at=NOW - 29)
    decision = evaluate(market(), ticks, book(), NOW, None, D(5))
    assert "incomplete_reference_history" in decision.reasons
    assert decision.metrics["missing_sample_offsets"] == [-30]


def test_reference_age_two_seconds_inclusive_and_missing_history_pauses():
    ticks = history()
    # A quiet two-second gap can use the latest received reference tick.
    decision = evaluate(market(), ticks[:-2], book(), NOW, None, D(5))
    assert decision.metrics["sample_count"] == 31
    stale = evaluate(market(), ticks[:-3], book(), NOW, None, D(5))
    assert "reference_stale_or_missing" in stale.reasons
    gap = evaluate(market(), [tick for i, tick in enumerate(ticks) if i not in {10, 11, 12}],
                   book(), NOW, None, D(5))
    assert "incomplete_reference_history" in gap.reasons


def test_trend_diagnostics_do_not_silently_tighten_baseline():
    prices = [D(99980)] + [D(99990) - D(i) / 2 for i in range(1, 30)] + [D(99986)]
    decision = evaluate(market(), history(prices), book(), NOW, None, D(5))
    assert decision.eligible
    assert decision.metrics["direction_adjusted_ols_slope"] < 0
    assert decision.metrics["positive_blocks"] == 2
    assert decision.metrics["reversal_magnitude"] > D("2.5")


def test_missing_eth_sol_opening_threshold_blocks_only_extended_strategy():
    eth_history = [ReferenceTick("ETH", D(2499) + D(i) / 100, tick.source_at, tick.received_at)
                   for i, tick in enumerate(history())]
    eth_market = market(asset="ETH", strike=D(2500))
    assert "missing_momentum_threshold" in evaluate(eth_market, eth_history, book(), NOW, None, D(5)).reasons
    decision = evaluate(eth_market, eth_history, book(), NOW, D(".15625"), D(5))
    assert decision.eligible
    assert decision.metrics["momentum_threshold"] == D(".15625")
    assert evaluate(market(), history(), book(), NOW, D(100), D(5)).eligible


def test_every_available_skip_reason_is_retained():
    decision = evaluate(market(verified=False, fee=FeeConfig(), cutoff=NOW + 119),
                        history([D(99700) - D(i) for i in range(31)]),
                        book(yes=((".10", 100),), received_at=NOW - 6, healthy=False), NOW, None, D(0))
    assert set(decision.reasons) >= {"market_unverified", "fee_unavailable", "outside_entry_window",
                                     "no_budget", "strike_too_far", "not_approaching_strike",
                                     "momentum_below_threshold", "strike_not_reachable",
                                     "book_unhealthy", "book_stale_or_future", "spread_too_wide"}


@pytest.mark.parametrize("age,healthy", [(5, True), (5.001, False), (-.001, False)])
def test_book_receipt_freshness_boundary(age, healthy):
    decision = evaluate(market(), history(), book(received_at=NOW - age), NOW, None, D(5))
    assert decision.eligible is healthy
