"""Configuration is non-secret. Credentials are read only by the feed adapter."""
from __future__ import annotations

import hashlib
import json
import math
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class BotConfig:
    assets: list[str] = field(default_factory=lambda: ["BTC", "ETH", "SOL"])
    api_url: str = "https://external-api.kalshi.com/trade-api/v2"
    ws_url: str = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
    refresh_seconds: float = 30
    depth_fraction: str = "0.5"
    execution_delay_ms: int = 500
    clock_uncertainty_ms: float | None = None
    fee_scenario: dict | None = None
    market_overrides_file: str | None = None
    exchange_feeds: list[str] = field(default_factory=list)

    def __post_init__(self):
        from .models import D
        if not self.assets or any(a not in {"BTC", "ETH", "SOL"} for a in self.assets):
            raise ValueError("assets must select BTC, ETH, SOL")
        if len(set(self.assets)) != len(self.assets):
            raise ValueError("assets cannot contain duplicates")
        depth = D(self.depth_fraction)
        if not depth.is_finite() or not 0 < depth <= 1:
            raise ValueError("depth_fraction must be in (0, 1]")
        if (not isinstance(self.execution_delay_ms, int) or isinstance(self.execution_delay_ms, bool)
                or not 0 <= self.execution_delay_ms <= 60000):
            raise ValueError("execution_delay_ms must be between 0 and 60000")
        if self.clock_uncertainty_ms is not None and (
                not math.isfinite(self.clock_uncertainty_ms) or self.clock_uncertainty_ms < 0):
            raise ValueError("clock_uncertainty_ms must be nonnegative or omitted")
        if not math.isfinite(self.refresh_seconds) or self.refresh_seconds < 1:
            raise ValueError("refresh_seconds must be at least 1")
        if not self.api_url.startswith("https://") or not self.ws_url.startswith("wss://"):
            raise ValueError("TLS is required for live API and WebSocket connections")
        if any(name not in {"coinbase", "kraken"} for name in self.exchange_feeds):
            raise ValueError("exchange_feeds may contain coinbase and kraken")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def load(cls, filename: str | None) -> "BotConfig":
        if filename is None:
            return cls()
        path = Path(filename)
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        unknown = set(data) - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError("Unknown configuration keys: " + ", ".join(sorted(unknown)))
        result = cls(**data)
        if result.market_overrides_file:
            result.market_overrides_file = str((path.parent / result.market_overrides_file).resolve())
        return result


def config_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
