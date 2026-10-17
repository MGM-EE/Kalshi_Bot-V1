"""Shared immutable data types; money is always represented by Decimal."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from decimal import Decimal
from typing import Any


def D(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


ZERO = D(0)


def jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


@dataclass(frozen=True)
class FeeConfig:
    multiplier: Decimal | None = None
    balance_quantum: Decimal = D("0.0001")
    verified: bool = False
    scenario: str | None = None
    model: str = "quadratic"


@dataclass(frozen=True)
class PriceRange:
    start: Decimal
    end: Decimal
    step: Decimal


@dataclass(frozen=True)
class Market:
    ticker: str
    asset: str
    strike: Decimal
    open_time: float
    cutoff: float
    observation_end: float
    expiration: float
    fee: FeeConfig = field(default_factory=FeeConfig)
    verified: bool = False
    yes_means_up: bool = True
    rules: str = ""
    invalid_reasons: tuple[str, ...] = ()
    determination: float | None = None
    settlement: float | None = None
    tick_size: Decimal = D("0.01")
    price_ranges: tuple[PriceRange, ...] = ()


@dataclass(frozen=True)
class ReferenceTick:
    asset: str
    price: Decimal
    source_at: float
    received_at: float
    source: str = "cfbenchmarks_value"


@dataclass(frozen=True)
class Level:
    price: Decimal
    quantity: int


@dataclass(frozen=True)
class OrderBook:
    ticker: str
    yes_bids: tuple[Level, ...]
    no_bids: tuple[Level, ...]
    received_at: float
    healthy: bool = True
    sequence: int | None = None


@dataclass(frozen=True)
class Fill:
    price: Decimal
    quantity: int
    fee: Decimal


@dataclass(frozen=True)
class Quote:
    fills: tuple[Fill, ...]

    @property
    def quantity(self) -> int:
        return sum(fill.quantity for fill in self.fills)

    @property
    def principal(self) -> Decimal:
        return sum((fill.price * fill.quantity for fill in self.fills), ZERO)

    @property
    def fees(self) -> Decimal:
        return sum((fill.fee for fill in self.fills), ZERO)

    @property
    def cost(self) -> Decimal:
        return self.principal + self.fees

    @property
    def proceeds(self) -> Decimal:
        return self.principal - self.fees


@dataclass(frozen=True)
class Decision:
    ticker: str
    now: float
    side: str | None
    reasons: tuple[str, ...]
    metrics: dict[str, Any]
    quote: Quote | None = None

    @property
    def eligible(self) -> bool:
        return not self.reasons and self.quote is not None


def market_from_dict(value: dict) -> Market:
    data = dict(value)
    fee = dict(data.pop("fee", {}))
    if fee.get("multiplier") is not None:
        fee["multiplier"] = D(fee["multiplier"])
    if "balance_quantum" in fee:
        fee["balance_quantum"] = D(fee["balance_quantum"])
    data["strike"] = D(data["strike"])
    if "tick_size" in data:
        data["tick_size"] = D(data["tick_size"])
    data["invalid_reasons"] = tuple(data.get("invalid_reasons", ()))
    data["price_ranges"] = tuple(PriceRange(D(r["start"]), D(r["end"]), D(r["step"]))
                                for r in data.get("price_ranges", ()))
    return Market(**data, fee=FeeConfig(**fee))


def book_from_dict(value: dict) -> OrderBook:
    def levels(items):
        return tuple(Level(D(item["price"]), int(item["quantity"])) if isinstance(item, dict)
                     else Level(D(item[0]), int(D(item[1]))) for item in items)
    return OrderBook(value["ticker"], levels(value.get("yes_bids", [])),
                     levels(value.get("no_bids", [])), float(value["received_at"]),
                     bool(value.get("healthy", True)), value.get("sequence"))
