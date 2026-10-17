"""Frozen baseline signal, Decimal fees and executable paper quotes.

This module has no network or order-submission code. Quotes are conditional on
the supplied book; callers must evaluate again at simulated order arrival.
"""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .models import D, Decision, FeeConfig, Fill, Level, Market, OrderBook, PriceRange, Quote, ReferenceTick


MODEL_QUANTUM = D("0.000001")
ENTRY_CEILING = D("0.40")


def _finite(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def fee_available(fee: FeeConfig) -> bool:
    """Unknown fees require an explicitly named, fully specified scenario."""
    return bool(
        (fee.verified or (fee.scenario and fee.scenario.strip()))
        and fee.model == "quadratic"
        and fee.multiplier is not None
        and _finite(fee.multiplier)
        and fee.multiplier >= 0
        and _finite(fee.balance_quantum)
        and fee.balance_quantum > 0
    )


class FeeAccumulator:
    """One order's model fees and balance rounding, including overpayment credit.

    Each fill's model fee is rounded upward to six decimal dollars. The account
    signed revenue minus model fees is floored to the balance quantum. Later
    fills use the previous rounding overpayment,
    rather than rounding each debit independently. ``charge`` returns only the
    additional fee debit for that fill. Create a new accumulator per order.
    """

    def __init__(self, fee: FeeConfig, action: str = "buy"):
        if not fee_available(fee):
            raise ValueError("A verified fee configuration or explicit scenario is required")
        if action not in {"buy", "sell"}:
            raise ValueError("Action must be 'buy' or 'sell'")
        self.fee = fee
        self.action = action
        self.model_fees = D(0)
        self.charged = D(0)
        self._rounding_overpayment = D(0)

    @property
    def rounding_overpayment(self) -> Decimal:
        return self._rounding_overpayment

    def _components(self, price: Decimal, quantity: int) -> tuple[Decimal, Decimal, Decimal]:
        if not _finite(price) or not 0 < price < 1:
            raise ValueError("Price must be a finite number between zero and one")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("Quantity must be a positive integer")
        trade_fee = (D("0.07") * self.fee.multiplier * quantity * price * (1 - price)).quantize(
            MODEL_QUANTUM, rounding=ROUND_CEILING
        )
        revenue = price * quantity * (-1 if self.action == "buy" else 1)
        change = revenue - trade_fee
        aligned_change = (change / self.fee.balance_quantum).to_integral_value(
            rounding=ROUND_FLOOR
        ) * self.fee.balance_quantum
        rounding_fee = change - aligned_change
        accumulated = self.rounding_overpayment + rounding_fee
        rebate_units = min(
            (accumulated / self.fee.balance_quantum).to_integral_value(rounding=ROUND_FLOOR),
            ((trade_fee + rounding_fee) / self.fee.balance_quantum).to_integral_value(rounding=ROUND_FLOOR),
        )
        rebate = rebate_units * self.fee.balance_quantum
        return trade_fee, rounding_fee, rebate

    def preview(self, price: Decimal, quantity: int) -> Decimal:
        """Return a fill's net fee without consuming its order accumulator."""
        trade_fee, rounding_fee, rebate = self._components(price, quantity)
        return trade_fee + rounding_fee - rebate

    def charge(self, price: Decimal, quantity: int) -> Decimal:
        trade_fee, rounding_fee, rebate = self._components(price, quantity)
        charged = trade_fee + rounding_fee - rebate
        self.model_fees += trade_fee
        self.charged += charged
        self._rounding_overpayment += rounding_fee - rebate
        return charged


def fee_for_fill(price: Decimal, quantity: int, fee: FeeConfig, action: str = "buy") -> Decimal:
    """Fee debit for a single fill constituting a new order."""
    return FeeAccumulator(fee, action).charge(price, quantity)


def bid_levels(book: OrderBook, side: str) -> tuple[Level, ...]:
    """Return selected-outcome bids from best to worst."""
    if side not in {"yes", "no"}:
        raise ValueError("Side must be 'yes' or 'no'")
    return tuple(sorted(book.yes_bids if side == "yes" else book.no_bids,
                        key=lambda level: level.price, reverse=True))


def ask_levels(book: OrderBook, side: str) -> tuple[Level, ...]:
    """YES asks are 1 minus NO bids, and NO asks are 1 minus YES bids."""
    opposite = "no" if side == "yes" else "yes" if side == "no" else None
    if opposite is None:
        raise ValueError("Side must be 'yes' or 'no'")
    return tuple(Level(1 - level.price, level.quantity) for level in bid_levels(book, opposite))


def validate_book(book: OrderBook, tick_size: Decimal = D("0.01"),
                  price_ranges: tuple[PriceRange, ...] = ()) -> tuple[str, ...]:
    """Validate price increments, integral depth and complementary uncrossed bids."""
    reasons: list[str] = []
    if not _finite(tick_size) or not 0 < tick_size < 1:
        return ("invalid_tick_size",)
    if any(not all(_finite(value) for value in (band.start, band.end, band.step))
           or not 0 <= band.start < band.end <= 1 or not 0 < band.step < 1
           for band in price_ranges):
        return ("invalid_price_ranges",)

    def on_tick(price: Decimal) -> bool:
        if price_ranges:
            return any(band.start <= price <= band.end and (price - band.start) % band.step == 0
                       for band in price_ranges)
        return price % tick_size == 0

    for side, levels in (("yes", book.yes_bids), ("no", book.no_bids)):
        seen: set[Decimal] = set()
        for level in levels:
            if not _finite(level.price) or not 0 < level.price < 1:
                reasons.append("invalid_book_price")
                continue
            if not on_tick(level.price) or not on_tick(1 - level.price):
                reasons.append("off_tick_book_price")
            if level.price in seen:
                reasons.append("duplicate_book_level")
            seen.add(level.price)
            if (not isinstance(level.quantity, int) or isinstance(level.quantity, bool)
                    or level.quantity <= 0):
                reasons.append("invalid_book_quantity")
    if not reasons and book.yes_bids and book.no_bids:
        if max(level.price for level in book.yes_bids) + max(level.price for level in book.no_bids) > 1:
            reasons.append("crossed_book")
    return tuple(dict.fromkeys(reasons))


def _usable_depth(quantity: int, fraction: Decimal) -> int:
    return int((quantity * fraction).to_integral_value(rounding=ROUND_FLOOR))


def _quote_ready(book: OrderBook, side: str, fee: FeeConfig,
                 depth_fraction: Decimal, tick_size: Decimal,
                 price_ranges: tuple[PriceRange, ...] = ()) -> bool:
    return (side in {"yes", "no"} and book.healthy and fee_available(fee)
            and _finite(depth_fraction) and 0 < depth_fraction <= 1
            and not validate_book(book, tick_size, price_ranges))


def plan_entry(book: OrderBook, side: str, budget: Decimal, fee: FeeConfig,
               depth_fraction: Decimal = D("0.5"), max_quantity: int | None = None,
               tick_size: Decimal = D("0.01"),
               price_ranges: tuple[PriceRange, ...] = ()) -> Quote | None:
    """Largest safe integer IOC quote, checking every possible partial prefix.

    Available depth is floored independently at each price level. An order that
    would have an unsafe one-contract prefix cannot be rescued by averaging its
    costs over a larger quantity. Fees accumulate once per executable level.
    """
    if (not _quote_ready(book, side, fee, depth_fraction, tick_size, price_ranges)
            or not _finite(budget) or budget <= 0):
        return None
    if max_quantity is not None and (not isinstance(max_quantity, int)
                                    or isinstance(max_quantity, bool) or max_quantity <= 0):
        return None
    accumulator = FeeAccumulator(fee)
    fills: list[Fill] = []
    principal = D(0)
    total_quantity = 0
    for level in ask_levels(book, side):
        available = _usable_depth(level.quantity, depth_fraction)
        if max_quantity is not None:
            available = min(available, max_quantity - total_quantity)
        # Fees are nonnegative, so principal alone provides a cheap upper bound.
        available = min(available, int(((budget - principal - accumulator.charged)
                                       / level.price).to_integral_value(rounding=ROUND_FLOOR)))
        accepted = 0
        for quantity in range(1, available + 1):
            fees = accumulator.charged + accumulator.preview(level.price, quantity)
            cost = principal + level.price * quantity + fees
            if cost > budget or cost > ENTRY_CEILING * (total_quantity + quantity):
                break
            accepted = quantity
        if accepted:
            debit = accumulator.charge(level.price, accepted)
            fills.append(Fill(level.price, accepted, debit))
            principal += level.price * accepted
            total_quantity += accepted
        if accepted < available or (max_quantity is not None and total_quantity == max_quantity):
            break
    return Quote(tuple(fills)) if fills else None


def plan_exit(book: OrderBook, side: str, quantity: int, fee: FeeConfig,
              depth_fraction: Decimal = D("0.5"), min_price: Decimal = D("0.95"),
              tick_size: Decimal = D("0.01"),
              price_ranges: tuple[PriceRange, ...] = ()) -> Quote | None:
    """Walk executable bids at or above the sell limit, allowing partial exits."""
    if (not _quote_ready(book, side, fee, depth_fraction, tick_size, price_ranges)
            or not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0
            or not _finite(min_price) or not 0 <= min_price <= 1):
        return None
    accumulator = FeeAccumulator(fee, action="sell")
    fills: list[Fill] = []
    remaining = quantity
    for level in bid_levels(book, side):
        if level.price < min_price:
            break
        count = min(remaining, _usable_depth(level.quantity, depth_fraction))
        if count:
            fills.append(Fill(level.price, count, accumulator.charge(level.price, count)))
            remaining -= count
        if not remaining:
            break
    return Quote(tuple(fills)) if fills else None


def _sample_history(history: list[ReferenceTick], asset: str, now: float
                    ) -> tuple[list[Decimal | None], dict]:
    eligible: list[ReferenceTick] = []
    ignored_future = ignored_source = invalid = 0
    for tick in history:
        if tick.asset.upper() != asset.upper():
            continue
        if tick.source != "cfbenchmarks_value":
            ignored_source += 1
            continue
        if (not math.isfinite(tick.source_at) or not math.isfinite(tick.received_at)
                or not _finite(tick.price) or tick.price <= 0):
            invalid += 1
            continue
        if tick.source_at > now or tick.received_at > now:
            ignored_future += 1
            continue
        eligible.append(tick)
    eligible.sort(key=lambda tick: (tick.source_at, tick.received_at))
    samples: list[Decimal | None] = []
    for offset in range(30, -1, -1):
        boundary = now - offset
        candidates = (tick for tick in eligible
                      if tick.source_at <= boundary and tick.received_at <= boundary
                      and boundary - tick.source_at <= 2)
        selected = max(candidates, key=lambda tick: (tick.source_at, tick.received_at), default=None)
        samples.append(selected.price if selected else None)
    return samples, {
        "sample_count": sum(sample is not None for sample in samples),
        "missing_sample_offsets": [i - 30 for i, sample in enumerate(samples) if sample is None],
        "ignored_future_ticks": ignored_future,
        "ignored_wrong_source_ticks": ignored_source,
        "ignored_invalid_ticks": invalid,
    }


def evaluate(market: Market, history: list[ReferenceTick], book: OrderBook | None,
             now: float, threshold: Decimal | None, budget: Decimal,
             depth_fraction: Decimal = D("0.5")) -> Decision:
    """Evaluate every available baseline gate without adding strict-trend gates."""
    reasons: list[str] = list(market.invalid_reasons)
    time_left = market.cutoff - now
    metrics: dict = {
        "strategy_version": "baseline-cf-1s-v1",
        "time_left": time_left,
        "budget": budget,
        "depth_fraction": depth_fraction,
        "fee_verified": market.fee.verified,
        "fee_scenario": market.fee.scenario,
    }
    if not market.verified:
        reasons.append("market_unverified")
    if not math.isfinite(now) or not math.isfinite(time_left) or not 120 <= time_left <= 600:
        reasons.append("outside_entry_window")
    valid_strike = _finite(market.strike) and market.strike > 0
    if not valid_strike:
        reasons.append("invalid_strike")
    if not fee_available(market.fee):
        reasons.append("fee_unavailable")
    if not _finite(budget) or budget <= 0:
        reasons.append("no_budget")
    if not _finite(depth_fraction) or not 0 < depth_fraction <= 1:
        reasons.append("invalid_depth_fraction")

    if market.asset.upper() == "BTC":
        threshold = D(5)
    elif market.asset.upper() not in {"ETH", "SOL"}:
        reasons.append("unsupported_asset")
    if threshold is None or not _finite(threshold) or threshold <= 0:
        reasons.append("missing_momentum_threshold")
        threshold = None
    metrics["momentum_threshold"] = threshold

    samples, sample_metrics = _sample_history(history, market.asset, now)
    metrics.update(sample_metrics)
    complete = all(sample is not None for sample in samples)
    if not complete:
        reasons.append("incomplete_reference_history")
    current = samples[-1]
    if current is None:
        reasons.append("reference_stale_or_missing")
    metrics["reference_price"] = current
    side: str | None = None
    direction: int | None = None
    if current is not None and valid_strike:
        if current == market.strike:
            reasons.append("at_strike")
        else:
            direction = 1 if current < market.strike else -1
            side = "yes" if (direction == 1) == market.yes_means_up else "no"
            metrics["direction"] = "UP" if direction == 1 else "DOWN"
            gap = direction * (market.strike - current)
            distance = abs(market.strike - current) / market.strike
            metrics.update(gap=gap, distance_fraction=distance)
            if distance > D("0.002"):
                reasons.append("strike_too_far")
    if complete and direction is not None:
        prices = [sample for sample in samples if sample is not None]
        momentum = direction * (prices[-1] - prices[0])
        speed = momentum / 30
        reach = metrics["gap"] / speed if speed > 0 else None
        mean = sum(prices, D(0)) / len(prices)
        beta = sum((D(i) - 15) * (price - mean) for i, price in enumerate(prices)) / D(2480)
        positive_blocks = sum(direction * (prices[i + 5] - prices[i]) > 0 for i in range(0, 30, 5))
        reversal = max(direction * price for price in prices) - direction * prices[-1]
        metrics.update(momentum=momentum, speed=speed, reach_seconds=reach,
                       direction_adjusted_ols_slope=direction * beta,
                       positive_blocks=positive_blocks, persistence=D(positive_blocks) / 6,
                       reversal_magnitude=reversal)
        if momentum <= 0:
            reasons.append("not_approaching_strike")
        if threshold is not None and momentum < threshold:
            reasons.append("momentum_below_threshold")
        if reach is None or reach > D(time_left):
            reasons.append("strike_not_reachable")

    book_valid = False
    if book is None:
        reasons.append("book_missing")
    else:
        book_age = now - book.received_at
        metrics["book_age_seconds"] = book_age
        if book.ticker != market.ticker:
            reasons.append("book_ticker_mismatch")
        if not book.healthy:
            reasons.append("book_unhealthy")
        if not math.isfinite(book_age) or not 0 <= book_age <= 5:
            reasons.append("book_stale_or_future")
        book_reasons = validate_book(book, market.tick_size, market.price_ranges)
        reasons.extend(book_reasons)
        book_valid = not book_reasons
        if book_valid and side is not None:
            bids, asks = bid_levels(book, side), ask_levels(book, side)
            if not bids or not asks:
                reasons.append("book_depth_missing")
            else:
                spread = asks[0].price - bids[0].price
                metrics.update(best_bid=bids[0].price, best_ask=asks[0].price, spread=spread)
                if spread > D("0.06"):
                    reasons.append("spread_too_wide")
    quote = None
    if book is not None and book_valid and side is not None:
        quote = plan_entry(book, side, budget, market.fee, depth_fraction,
                           tick_size=market.tick_size, price_ranges=market.price_ranges)
        if quote is None:
            reasons.append("no_feasible_quantity")
        else:
            metrics.update(candidate_quantity=quote.quantity, candidate_cost=quote.cost,
                           candidate_fees=quote.fees, all_in_per_contract=quote.cost / quote.quantity)
    return Decision(market.ticker, now, side, tuple(dict.fromkeys(reasons)), metrics, quote)
