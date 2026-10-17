"""Receipt-time research reports. Money is returned as exact decimal strings.

These are descriptive paper-trading tools, never profitability claims. Comparisons
reuse recorded baseline quantities and do not release cash for additional trades.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import random
import sqlite3
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from .models import D, ZERO, FeeConfig, PriceRange, book_from_dict, jsonable

HORIZONS = (5, 15, 30, 60, 120)
TARGETS = (45, 50, 60, 70, 80, 90, 95)
RESEARCH_POLICIES = ("fixed_50", "fixed_60", "fixed_70", "fixed_80", "fixed_90",
                     "fixed_95", "trailing_10_5", "time_60", "reversal_3",
                     "unusual_flow", "volatility_liquidity")
NY = ZoneInfo("America/New_York")


def _time(value: Any) -> float:
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    try:
        return float(value)
    except (ValueError, TypeError):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    number = D(value)
    return number if number.is_finite() else None


def _quantile(values: list[Decimal], probability: Decimal | float) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    at = D(probability) * (len(ordered) - 1)
    lo = int(at)
    if lo == len(ordered) - 1:
        return ordered[lo]
    return ordered[lo] + (ordered[lo + 1] - ordered[lo]) * (at - lo)


def _distribution(values: list[Decimal]) -> dict:
    return jsonable({"count": len(values), "mean": sum(values, ZERO) / len(values) if values else None,
                     "median": _quantile(values, D(".5")),
                     "p10": _quantile(values, D(".1")), "p25": _quantile(values, D(".25")),
                     "p75": _quantile(values, D(".75")), "p90": _quantile(values, D(".9"))})


def _connect(database: str | Path) -> sqlite3.Connection:
    path = Path(database).expanduser().resolve()
    connection = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("BEGIN")  # A consistent snapshot across the complete report.
    return connection


def _tables(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _rows(connection: sqlite3.Connection, name: str) -> list[dict]:
    if name not in _tables(connection):
        return []
    return [dict(row) for row in connection.execute('SELECT * FROM "' + name.replace('"', '""') + '"')]


def _payload(row: dict) -> dict:
    data = dict(row)
    raw = data.pop("payload", None)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if isinstance(raw, dict):
        # Identity/time columns are authoritative; payload cannot overwrite them.
        data = {**raw, **data}
    return data


def _safe_json(value: Any) -> Any:
    """Avoid exporting accidentally supplied secrets embedded in configuration."""
    if isinstance(value, dict):
        return {key: "[REDACTED]" if any(term in key.lower() for term in
                ("private_key", "api_key", "password", "secret", "access_token", "auth_token"))
                else _safe_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    return value


def _dataset(configs: list[dict]) -> dict:
    labels = set()
    for row in configs:
        config = row.get("config_json", row.get("config", {}))
        if isinstance(config, str):
            try:
                config = json.loads(config)
            except json.JSONDecodeError:
                config = {}
        if not isinstance(config, dict):
            config = {}
        label = config.get("dataset", config.get("data_mode", config.get("mode")))
        if label in ("demo", "synthetic", "fixture") or config.get("demo") is True:
            labels.add("demo")
        elif label == "real":
            labels.add("real")
        else:
            labels.add("unknown")
    label = next(iter(labels)) if len(labels) == 1 else "mixed" if labels else "unknown"
    return {"label": label, "run_labels": sorted(labels), "paper_only": True,
            "performance_inference": False,
            "notice": "Synthetic/demo data verify software only." if "demo" in labels else
                      "Descriptive paper results; execution, fees, coverage and held-out evidence require validation."}


def export_parquet(database: str | Path, destination: str | Path) -> dict[str, int]:
    """Export one transactionally consistent snapshot, preserving monetary TEXT.

    No price/cost/fee column is converted to binary floating point. JSON payloads
    remain JSON strings so Arrow cannot silently infer floats for decimal values.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with _connect(database) as connection:
        for table in _tables(connection):
            rows = _rows(connection, table)
            for row in rows:
                for key, value in row.items():
                    if isinstance(value, str) and key in ("payload", "config_json", "metadata"):
                        try:
                            row[key] = json.dumps(_safe_json(json.loads(value)), separators=(",", ":"))
                        except json.JSONDecodeError:
                            pass
            if rows:
                arrow = pa.Table.from_pylist(rows)
            else:
                columns = [row[1] for row in connection.execute(
                    'PRAGMA table_info("' + table.replace('"', '""') + '")')]
                arrow = pa.table({column: pa.array([], type=pa.string()) for column in columns})
            pq.write_table(arrow, destination / f"{table}.parquet", compression="zstd")
            counts[table] = len(rows)
        manifest = {"format_version": 1, "tables": counts, "dataset": _dataset(_rows(connection, "run_config")),
                    "monetary_representation": "exact strings; original SQLite TEXT preserved",
                    "paper_only": True}
        (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return counts


def _points(points: Iterable[dict], cutoff: float) -> list[dict]:
    result = []
    for raw in points:
        point = _payload(raw)
        try:
            at = _time(point.get("received_at", point.get("time")))
        except (ValueError, TypeError):
            continue
        if at <= cutoff:
            point["received_at"] = at
            result.append(point)
    return sorted(result, key=lambda item: (item["received_at"], str(item.get("id", ""))))


def _full(point: dict, quantity: int) -> Decimal | None:
    if point.get("depth_available") is False or point.get("observed") is False:
        return None
    if int(point.get("executable_quantity", point.get("liquidation_quantity", quantity)) or 0) < quantity:
        return None
    return _decimal(point.get("executable_net_liquidation", point.get("liquidation_value")))


def _min_bid(point: dict) -> Decimal | None:
    # best_bid alone cannot establish that *all* baseline shares clear a target.
    return _decimal(point.get("liquidation_min_price", point.get("full_liquidation_min_price")))


def trajectory_summary(points: Iterable[dict], entry_cost: Any, quantity: int,
                       cutoff: Any, *, opened_at: Any | None = None,
                       actual_exit_at: Any | None = None) -> dict:
    """Full-position executable excursions; never substitutes midpoint or settlement."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    cutoff = _time(cutoff)
    ordered = _points(points, cutoff)
    if opened_at is None:
        opened_at = ordered[0].get("opened_at", ordered[0]["received_at"]) if ordered else cutoff
    opened = _time(opened_at)
    ordered = [point for point in ordered if point["received_at"] >= opened]
    cost = D(entry_cost)
    observed = [(point, _full(point, quantity)) for point in ordered]
    valid = [(point, value - cost) for point, value in observed if value is not None]
    values = [value for _, value in valid]
    holding_end = _time(actual_exit_at) if actual_exit_at is not None else cutoff
    holding_values = [value for point, value in valid if point["received_at"] <= holding_end]
    horizons = {}
    for horizon in HORIZONS:
        target = opened + horizon
        eligible = target <= cutoff
        # A receipt at target or up to 2 s before it; no future interpolation.
        candidates = [(point, value) for point, value in valid
                      if target - 2 <= point["received_at"] <= target] if eligible else []
        value = candidates[-1][1] if candidates else None
        horizons[str(horizon)] = {"eligible": eligible, "observed": value is not None,
                                  "net_pnl": str(value) if value is not None else None,
                                  "censored_reason": None if value is not None else
                                  "horizon_after_cutoff" if not eligible else "missing_executable_depth"}
    cutoff_horizons = {}
    for horizon in HORIZONS:
        target = cutoff - horizon
        eligible = target >= opened
        candidates = [(point, value) for point, value in valid
                      if target - 2 <= point["received_at"] <= target] if eligible else []
        value = candidates[-1][1] if candidates else None
        cutoff_horizons[str(horizon)] = {"eligible": eligible, "observed": value is not None,
                                         "net_pnl": str(value) if value is not None else None,
                                         "censored_reason": None if value is not None else
                                         "before_entry" if not eligible else "missing_executable_depth"}
    touches = {}
    for cents in TARGETS:
        first = next((point for point, _ in valid if _min_bid(point) is not None
                      and _min_bid(point) >= D(cents) / 100), None)
        touches[str(cents)] = {"received_at": first["received_at"] if first else None,
                               "elapsed_seconds": first["received_at"] - opened if first else None,
                               "status": "observed_touch" if first else "not_observed"}
    # Integrate only observed intervals <= 2 seconds. Never bridge feed gaps.
    total_time = ZERO
    weighted = ZERO
    above = below = ZERO
    for (point, value), (next_point, _) in zip(observed, observed[1:]):
        dt = next_point["received_at"] - point["received_at"]
        if value is not None and 0 < dt <= 2:
            duration = D(str(dt))
            profit = value - cost
            weighted += profit * duration
            total_time += duration
            above += duration if profit > ZERO else ZERO
            below += duration if profit < ZERO else ZERO
    expected_seconds = max(0, int(cutoff - opened) + 1)
    unique_seconds = len({int(point["received_at"] - opened) for point, _ in valid})
    return jsonable({"quantity": quantity, "entry_cost": cost, "opened_at": opened, "cutoff": cutoff,
                     "observed_points": len(valid), "missing_points": len(ordered) - len(valid),
                     "eligible_seconds": expected_seconds, "observed_seconds": unique_seconds,
                     "coverage": D(unique_seconds) / expected_seconds if expected_seconds else None,
                     "status": "observed" if valid else "unavailable",
                     "mfe": max(values) if values else None, "mae": min(values) if values else None,
                     "mfe_cents_per_share": 100 * max(values) / quantity if values else None,
                     "mae_cents_per_share": 100 * min(values) / quantity if values else None,
                     "holding_mfe": max(holding_values) if holding_values else None,
                     "holding_mae": min(holding_values) if holding_values else None,
                     "observable_seconds": total_time,
                     "time_weighted_mean_pnl": weighted / total_time if total_time else None,
                     "fraction_observable_time_above_break_even": above / total_time if total_time else None,
                     "fraction_observable_time_below_break_even": below / total_time if total_time else None,
                     "fixed_elapsed_horizons": horizons, "fixed_seconds_to_cutoff": cutoff_horizons,
                     "first_full_executable_touches": touches,
                     "limitations": ["Coverage is receipt-time observations, not proof of continuous liquidity.",
                                     "Settlement payouts are excluded from pre-cutoff excursions."]})



def _arrival_quote(point: dict, quantity: int, limit: Decimal | None):
    """Re-walk this already-received state with a new per-order fee accumulator."""
    from .strategy import plan_exit

    raw_book = point.get("book")
    if not isinstance(raw_book, dict):
        return None
    raw_fee = point.get("fee")
    if not isinstance(raw_fee, dict):
        return None
    try:
        fee = FeeConfig(multiplier=_decimal(raw_fee.get("multiplier")),
                        balance_quantum=D(raw_fee.get("balance_quantum", ".0001")),
                        verified=bool(raw_fee.get("verified", False)),
                        scenario=raw_fee.get("scenario"), model=raw_fee.get("model", "quadratic"))
        book = book_from_dict(raw_book)
        now = _time(point["received_at"])
        if not 0 <= now - book.received_at <= 5:
            return None
        ranges = tuple(PriceRange(D(band["start"]), D(band["end"]), D(band["step"]))
                       for band in point.get("price_ranges", ()))
        return plan_exit(book, point.get("side"), quantity, fee,
                         D(point.get("depth_fraction", ".5")), limit if limit is not None else ZERO,
                         D(point.get("tick_size", ".01")), ranges)
    except (ValueError, TypeError, KeyError, ArithmeticError):
        return None


def compare_policies(points: Iterable[dict], entry_cost: Any, quantity: int, cutoff: Any, *,
                     opened_at: Any | None = None, settlement_proceeds: Any | None = None,
                     delay_seconds: float = 0.5, policies: Iterable[str] = RESEARCH_POLICIES) -> dict:
    """Paired, delayed IOC alternatives using the original baseline quantity.

    Recorded ladders support partial fills with fresh per-order fee rounding.
    Older trajectories without ladders support only recorded full liquidations;
    partial execution in those records remains explicitly unavailable.
    """
    if quantity <= 0 or delay_seconds < 0:
        raise ValueError("positive quantity and nonnegative delay required")
    cutoff = _time(cutoff)
    ordered = _points(points, cutoff)
    opened = _time(opened_at) if opened_at is not None else (
        _time(ordered[0].get("opened_at", ordered[0]["received_at"])) if ordered else cutoff)
    ordered = [point for point in ordered if opened <= point["received_at"] < cutoff]
    cost = D(entry_cost)
    outcomes = {}
    for policy in policies:
        if policy not in RESEARCH_POLICIES and policy != "fixed_45":
            raise ValueError(f"Unknown pre-registered policy: {policy}")
        peak = None
        armed = False
        negative_streak = risk_streak = 0
        previous_decision = None
        pending = None
        outcome = None
        fallback_count = unavailable_arrivals = 0
        remaining = quantity
        realized_proceeds = ZERO
        fees = ZERO
        executions = []

        def execute(point, intent):
            nonlocal remaining, realized_proceeds, fees
            quote = _arrival_quote(point, remaining, intent["limit"])
            if quote is not None:
                filled, proceeds, charged = quote.quantity, quote.proceeds, quote.fees
            elif "book" not in point and remaining == quantity:
                # Compatibility path cannot invent fees/depth for residual shares.
                net, minimum = _full(point, quantity), _min_bid(point)
                if net is None or (intent["limit"] is not None and
                                   (minimum is None or minimum < intent["limit"])):
                    return False
                filled, proceeds = quantity, net
                charged = _decimal(point.get("exit_fee"))
            else:
                return False
            remaining -= filled
            realized_proceeds += proceeds
            if charged is not None:
                fees += charged
            executions.append({"triggered_at": intent["triggered_at"], "filled_at": point["received_at"],
                               "quantity": filled, "net_proceeds": str(proceeds),
                               "assumed_arrival_at": intent["arrival"],
                               "effective_delay_seconds": point["received_at"] - intent["triggered_at"],
                               "fee": str(charged) if charged is not None else None, "reason": intent["reason"]})
            return True

        for point in ordered:
            now = point["received_at"]
            net = _full(point, quantity)
            minimum = _min_bid(point)
            profit = (net - cost) / quantity if net is not None else None
            if pending is not None and now >= pending["arrival"]:
                if now - pending["arrival"] > 2 or not execute(point, pending):
                    unavailable_arrivals += 1
                pending = None
                if not remaining:
                    break
            # Consecutive means consecutive valid one-second decisions, not any
            # three scattered negative returns across a data gap.
            if previous_decision is not None and not 0 < now - previous_decision <= 1.5:
                negative_streak = risk_streak = 0
            previous_decision = now
            ret = _decimal(point.get("direction_adjusted_return_5s", point.get("signed_reference_change")))
            negative_streak = negative_streak + 1 if ret is not None and ret < 0 else 0
            trigger = False
            limit = None
            reason = policy
            if policy.startswith("fixed_"):
                limit = D(policy.split("_")[1]) / 100
                trigger = bool(_arrival_quote(point, remaining, limit)) if "book" in point else (
                    net is not None and minimum is not None and minimum >= limit)
            elif policy == "trailing_10_5":
                if profit is not None:
                    peak = profit if peak is None else max(peak, profit)
                    armed |= profit >= D(".10")
                    trigger = armed and peak - profit >= D(".05")
            elif policy == "time_60":
                trigger = now >= cutoff - 60
            elif policy == "reversal_3":
                if ret is None:
                    fallback_count += 1
                trigger = negative_streak >= 3
            elif policy == "unusual_flow":
                available = point.get("unusual_flow_available") is True and ret is not None
                if not available:
                    fallback_count += 1
                trigger = available and point.get("adverse_unusual_flow") is True and ret < 0
            elif policy == "volatility_liquidity":
                available = point.get("volatility_liquidity_available") is True
                if not available:
                    fallback_count += 1
                risk_streak = risk_streak + 1 if available and point.get("volatility_liquidity_risk") is True else 0
                trigger = risk_streak >= 3
            baseline = (bool(_arrival_quote(point, remaining, D(".95"))) if "book" in point else
                        net is not None and minimum is not None and minimum >= D(".95"))
            if not policy.startswith("fixed_") and baseline:
                trigger, limit, reason = True, D(".95"), "baseline_95_fallback"
            if trigger and pending is None:
                pending = {"triggered_at": now, "arrival": now + delay_seconds, "limit": limit, "reason": reason}
                if delay_seconds == 0:
                    if not execute(point, pending):
                        unavailable_arrivals += 1
                    pending = None
                    if not remaining:
                        break
        settlement = _decimal(settlement_proceeds)
        complete = remaining == 0 or settlement is not None
        if not remaining:
            status = "exited"
        elif settlement is not None:
            realized_proceeds += settlement * remaining / quantity
            status = "partially_exited_then_settled" if executions else "settled"
        else:
            status = "partially_exited_pending_or_censored" if executions else "pending_or_censored"
        outcome = {"status": status, "quantity": quantity, "unexited_quantity": remaining,
                   "net_proceeds": str(realized_proceeds) if complete else None,
                   "observed_exit_proceeds": str(sum((D(fill["net_proceeds"]) for fill in executions), ZERO)),
                   "net_pnl": str(realized_proceeds - cost) if complete else None,
                   "triggered_at": executions[0]["triggered_at"] if executions else None,
                   "filled_at": executions[-1]["filled_at"] if executions else None,
                   "reason": executions[-1]["reason"] if executions else
                             "official_settlement" if settlement is not None else "no_executable_exit_or_unknown_settlement",
                   "executions": executions, "exit_fees": str(fees),
                   "delay_assumption_seconds": delay_seconds,
                   "feature_fallback_decisions": fallback_count,
                   "unavailable_or_failed_arrivals": unavailable_arrivals}
        outcomes[policy] = outcome
    baseline = outcomes.get("fixed_95", {}).get("net_pnl")
    for outcome in outcomes.values():
        pnl = outcome.get("net_pnl")
        outcome["paired_difference_vs_95"] = str(D(pnl) - D(baseline)) if pnl is not None and baseline is not None else None
    return {"policies": outcomes, "quantity": quantity, "reinvestment": False,
            "execution_model": "delayed_first_observed_trajectory_snapshot",
            "limitations": ["Visible ladders are replayed without market impact; this is a paper-capacity assumption.",
                            "Older records without ladders support full-position fills only.",
                            "Arrival uses the first recorded one-second trajectory at/after assumed delay, within two seconds; effective delay is reported.",
                            "Counterfactual policy timing differs from the engine baseline, which processes arrivals against already-received event state at the exact timer.",
                            "Missing experimental features fall back to 95-cent/official-settlement baseline."]}


def future_liquidation_labels(points: Iterable[dict], *, decision_at: Any, quantity: int,
                              cutoff: Any, horizons: Iterable[int] = (30, 60, 120)) -> dict:
    """Research-only future labels, separate from causal decision-time features.

    A positive target can be established by an observed executable improvement.
    Absence of a target is negative only when the entire horizon was observed.
    """
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    now, cutoff = _time(decision_at), _time(cutoff)
    ordered = _points(points, cutoff)
    initial = [(point, _full(point, quantity)) for point in ordered
               if now - 2 <= point["received_at"] <= now and _full(point, quantity) is not None]
    base = initial[-1][1] if initial else None
    report = {}
    for horizon in horizons:
        if horizon <= 0:
            raise ValueError("horizons must be positive")
        end = now + horizon
        if end > cutoff or base is None:
            report[str(horizon)] = {"improves_5c_per_share": None, "adverse_10c_first": None,
                                    "status": "censored", "reason": "horizon_after_cutoff" if end > cutoff
                                    else "initial_full_liquidation_unavailable"}
            continue
        observed = [(point["received_at"], _full(point, quantity)) for point in ordered
                    if now < point["received_at"] <= end and _full(point, quantity) is not None]
        # Each expected one-second boundary requires an executable observation;
        # arbitrary higher-frequency points cannot hide a missing second.
        bins = {int(at - now) for at, value in observed}
        complete = all(second in bins for second in range(1, horizon + 1))
        improvement = next((at for at, value in observed if value - base >= D(".05") * quantity), None)
        adverse = next((at for at, value in observed if base - value >= D(".10") * quantity), None)
        report[str(horizon)] = {"improves_5c_per_share": True if improvement is not None else False if complete else None,
                                "adverse_10c_first": (adverse is not None and (improvement is None or adverse < improvement))
                                                        if complete else None,
                                "first_improvement_at": improvement, "first_adverse_at": adverse,
                                "fully_observed": complete, "observed_second_bins": len(bins),
                                "status": "observed" if complete else "positive_observed_with_gaps" if improvement is not None else "censored",
                                "label_window_end": end}
    return report


def fit_training_scaler(records: Iterable[dict], feature_names: Iterable[str], split_manifest: dict) -> dict:
    """Fit descriptive scaling on immutable training membership only.

    Records supply entry_id, received_at, feature_received_at and features. A
    missing availability watermark is an error; validation/test values never
    contribute to fitted parameters.
    """
    training = {str(row["entry_id"]) for row in split_manifest["assignments"] if row["fold"] == "train"}
    names = tuple(feature_names)
    values = {name: [] for name in names}
    for row in records:
        if str(row["entry_id"]) not in training:
            continue
        if row.get("feature_received_at") is None or _time(row["feature_received_at"]) > _time(row["received_at"]):
            raise ValueError("Features must be available by decision receipt time")
        for name in names:
            number = _decimal(row.get("features", {}).get(name))
            if number is not None:
                values[name].append(number)
    parameters = {}
    for name, numbers in values.items():
        mean = sum(numbers, ZERO) / len(numbers) if numbers else None
        variance = sum(((number - mean) ** 2 for number in numbers), ZERO) / len(numbers) if numbers else None
        parameters[name] = {"count": len(numbers), "mean": str(mean) if mean is not None else None,
                            "standard_deviation": str(variance.sqrt()) if variance is not None else None}
    return {"split_sha256": split_manifest.get("sha256"), "fit_fold": "train", "features": parameters}

def unusual_flow_diagnostic(notional: Any, prior_trades: Iterable[tuple[Any, Any]], now: Any) -> dict:
    now = _time(now)
    prior = [D(value) for at, value in prior_trades if now - 3600 <= _time(at) < now]
    if len(prior) < 1000:
        return {"available": False, "prior_trades": len(prior), "unusual": None,
                "reason": "requires_1000_prior_trades_in_trailing_hour"}
    median = _quantile(prior, D(".5"))
    percentile = _quantile(prior, D(".995"))
    return jsonable({"available": True, "prior_trades": len(prior), "median": median,
                     "p995": percentile, "unusual": D(notional) > percentile and D(notional) >= 5 * median,
                     "aggressive_side": "unavailable_without_venue_semantics"})


def freeze_chronological_split(records: Iterable[dict], destination: str | Path, *,
                               collection_start: Any, instrumentation_days: int = 7,
                               train_days: int = 21, validation_days: int = 7,
                               test_days: int = 14, embargo_seconds: int = 900) -> dict:
    """Freeze quarter-hour groups and purge crossing labels before every fold.

    Records require entry_id, received_at and label_end. Missing label_end means
    pending labels, so the complete correlated quarter-hour group is purged.
    Existing manifests may be reused identically, never silently overwritten.
    """
    start = _time(collection_start)
    if min(instrumentation_days, train_days, validation_days, test_days, embargo_seconds) < 0:
        raise ValueError("split durations cannot be negative")
    training = start + instrumentation_days * 86400
    validation = training + train_days * 86400
    test = validation + validation_days * 86400
    end = test + test_days * 86400
    groups = defaultdict(list)
    for record in records:
        groups[int(_time(record["received_at"]) // 900) * 900].append(dict(record))
    assignments = []
    for group, entries in sorted(groups.items()):
        if group < training:
            fold, boundary = "instrumentation", training
        elif group < validation:
            fold, boundary = "train", validation
        elif group < test:
            fold, boundary = "validation", test
        elif group < end:
            fold, boundary = "test", end
        else:
            fold, boundary = "outside_plan", float("inf")
        reason = None
        if fold in ("train", "validation", "test"):
            lower = {"train": training, "validation": validation, "test": test}[fold]
            if group < lower + embargo_seconds:
                reason = "15_minute_embargo" if embargo_seconds == 900 else "boundary_embargo"
            elif any(entry.get("label_end") is None for entry in entries):
                reason = "pending_label"
            elif any(_time(entry["label_end"]) >= boundary for entry in entries):
                reason = "label_crosses_boundary"
        for entry in sorted(entries, key=lambda item: str(item["entry_id"])):
            assignments.append({"entry_id": str(entry["entry_id"]), "quarter_hour": group,
                                "fold": "purged" if reason else fold, "intended_fold": fold,
                                "reason": reason})
    manifest = {"version": 1, "collection_start": start,
                "durations_days": {"instrumentation": instrumentation_days, "train": train_days,
                                   "validation": validation_days, "test": test_days},
                "embargo_seconds": embargo_seconds, "assignments": assignments}
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    destination = Path(destination)
    if destination.exists():
        saved = json.loads(destination.read_text())
        if saved != manifest:
            raise ValueError("Existing split is immutable; changed records/parameters need a new versioned manifest")
        return saved
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x") as handle:
        handle.write(json.dumps(manifest, indent=2) + "\n")
    return manifest


def day_block_bootstrap(records: Iterable[dict], *, value_key: str = "net_pnl", day_key: str = "day",
                        dataset: str = "unknown", seed: int = 17, samples: int = 2000,
                        minimum_days: int = 14, block_days: int = 1, comparisons: int = 1) -> dict:
    """Seeded day-clustered mean dollars/trade; suppress inference on toy data."""
    daily: dict[str, list[Decimal]] = defaultdict(list)
    for row in records:
        if row.get(value_key) is not None:
            daily[str(row[day_key])].append(D(row[value_key]))
    values = [value for entries in daily.values() for value in entries]
    report = {"days": len(daily), "trades": len(values), "seed": seed, "samples": samples,
              "block_days": block_days, "comparisons": comparisons,
              "estimate": str(sum(values, ZERO) / len(values)) if values else None,
              "confidence_interval": None, "meaningful_performance_inference": False}
    if dataset != "real" or len(daily) < minimum_days:
        report["reason"] = "requires_real_data_and_at_least_%s_observed_days" % minimum_days
        return report
    if samples < 100 or block_days < 1 or comparisons < 1:
        raise ValueError("samples >=100, positive block_days/comparisons required")
    ordered = [daily[day] for day in sorted(daily)]
    generator = random.Random(seed)
    means = []
    for _ in range(samples):
        sampled = []
        selected_days = 0
        while selected_days < len(ordered):
            index = generator.randrange(len(ordered))
            for offset in range(min(block_days, len(ordered) - selected_days)):
                sampled.extend(ordered[(index + offset) % len(ordered)])
            selected_days += min(block_days, len(ordered) - selected_days)
        means.append(sum(sampled, ZERO) / len(sampled))
    tail = D(".025") / comparisons  # Bonferroni familywise bounds for disclosed secondary trials.
    lo, hi = _quantile(means, tail), _quantile(means, 1 - tail)
    report.update({"confidence_interval": [str(lo), str(hi)], "meaningful_performance_inference": True,
                   "method": "circular chronological day-block bootstrap; Bonferroni familywise interval",
                   "conclusion": "inconclusive" if lo <= 0 <= hi else "descriptive_interval_excludes_zero",
                   "limitation": "Observed-day blocks do not repair missing days, execution bias or parameter tuning."})
    return report


def _position_groups(connection: sqlite3.Connection) -> list[tuple[dict, list[dict]]]:
    points = defaultdict(list)
    for row in _rows(connection, "trajectories"):
        point = _payload(row)
        points[str(point.get("position_id", point.get("entry_id")))].append(point)
    positions = [_payload(row) for row in _rows(connection, "positions")]
    known = {str(row.get("position_id", row.get("id"))) for row in positions}
    # Retain trajectories even if a minimal fixture has no positions table.
    for identifier, entries in points.items():
        if identifier not in known:
            positions.append({**entries[0], "position_id": identifier})
    return [(position, points.get(str(position.get("position_id", position.get("id"))), []))
            for position in positions]


def _position_terms(position: dict, points: list[dict]) -> tuple[int, Decimal | None, Any, Any]:
    merged = {**(points[0] if points else {}), **position}
    quantity = int(merged.get("entry_quantity", merged.get("initial_quantity", merged.get("quantity", 0))) or 0)
    cost = _decimal(merged.get("entry_cost", merged.get("original_cost", merged.get("cost"))))
    market = merged.get("market_json", {})
    if isinstance(market, str):
        try:
            market = json.loads(market)
        except json.JSONDecodeError:
            market = {}
    cutoff = merged.get("market_cutoff", merged.get("cutoff", market.get("cutoff")))
    opened = merged.get("opened_at", merged.get("entry_at"))
    return quantity, cost, cutoff, opened



def _settlement_outcomes(connection: sqlite3.Connection) -> dict[str, Decimal]:
    """Observed official outcomes, including contracts sold before resolution."""
    outcomes = {}
    positions = [_payload(row) for row in _rows(connection, "positions")]
    for fill in _rows(connection, "fills"):
        if fill.get("kind") == "settlement" and D(fill["price"]) in (ZERO, D(1)):
            outcomes[str(fill["position_id"])] = D(fill["price"])
    official = {}
    for raw in _rows(connection, "events"):
        event = _payload(raw)
        if event.get("type") not in ("settlement", "official_settlement"):
            continue
        if event.get("official") is not True and event.get("dataset") != "demo":
            continue
        ticker = event.get("market_ticker", event.get("ticker"))
        if ticker and event.get("result") in ("yes", "no"):
            official[ticker] = event["result"]
    for position in positions:
        ticker = position.get("market_ticker", position.get("ticker"))
        if ticker in official:
            outcomes[str(position.get("position_id", position.get("id")))] = D(int(position.get("side") == official[ticker]))
    return outcomes

def compare_exits(database: str | Path) -> dict:
    rows = []
    with _connect(database) as connection:
        dataset = _dataset(_rows(connection, "run_config"))
        settlements = _settlement_outcomes(connection)
        for position, points in _position_groups(connection):
            quantity, cost, cutoff, opened = _position_terms(position, points)
            identifier = position.get("position_id", position.get("id"))
            if quantity <= 0 or cost is None or cutoff is None:
                rows.append({"position_id": identifier, "status": "unavailable",
                             "reason": "missing_baseline_quantity_cost_or_cutoff"})
                continue
            # An actual 95c sale is not a counterfactual settlement payout.
            settlement = position.get("official_settlement_proceeds")
            outcome = settlements.get(str(identifier))
            if outcome is not None:
                settlement = quantity * outcome
            if settlement is None and position.get("settlement_outcome") in (0, 1, "0", "1"):
                settlement = quantity * D(position["settlement_outcome"])
            for point in points:
                point.setdefault("side", position.get("side"))
            comparison = compare_policies(points, cost, quantity, cutoff, opened_at=opened,
                                          settlement_proceeds=settlement)
            rows.append({"position_id": identifier, "market_ticker": position.get("market_ticker", position.get("ticker")),
                         **comparison})
    aggregates = {}
    for policy in RESEARCH_POLICIES:
        outcomes = [row["policies"][policy] for row in rows if "policies" in row]
        pnls = [D(row["net_pnl"]) for row in outcomes if row["net_pnl"] is not None]
        paired = [D(row["paired_difference_vs_95"]) for row in outcomes if row["paired_difference_vs_95"] is not None]
        aggregates[policy] = {"eligible_entries": len(rows), "observed_outcomes": len(pnls),
                              "pending_or_censored": len(rows) - len(pnls), "net_pnl": _distribution(pnls),
                              "paired_difference_vs_95": _distribution(paired)}
    return {"dataset": dataset, "comparison_type": "paired_baseline_entries_no_reinvestment",
            "policies": aggregates, "entries": rows, "performance_claim": False,
            "execution_model": "delayed_first_observed_trajectory_snapshot",
            "limitations": ["Descriptive recorded-quote replay; full-portfolio capital reuse is not simulated.",
                            "Policy fills use the first recorded one-second snapshot after delay; engine baseline fills use exact receipt-time timers.",
                            "Older trajectory records without ladders cannot establish partial-depth fills."]}


def analyze(database: str | Path) -> dict:
    with _connect(database) as connection:
        configs = _rows(connection, "run_config")
        dataset = _dataset(configs)
        tables = {table: connection.execute('SELECT count(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                  for table in _tables(connection)}
        positions = [_payload(row) for row in _rows(connection, "positions")]
        fills = [_payload(row) for row in _rows(connection, "fills") or _rows(connection, "paper_fills")]
        orders = [_payload(row) for row in _rows(connection, "orders") or _rows(connection, "paper_orders")]
        ledger = [_payload(row) for row in _rows(connection, "ledger")]
        entries = []
        for position, points in _position_groups(connection):
            quantity, cost, cutoff, opened = _position_terms(position, points)
            if quantity <= 0 or cost is None or cutoff is None:
                entries.append({"position_id": position.get("position_id", position.get("id")), "status": "unavailable",
                                "reason": "missing_baseline_quantity_cost_or_cutoff"})
                continue
            entries.append({"position_id": position.get("position_id", position.get("id")),
                            "asset": position.get("asset"), "direction": position.get("side"),
                            **trajectory_summary(points, cost, quantity, cutoff, opened_at=opened,
                                                 actual_exit_at=position.get("closed_at"))})
        # Prefer incremental realized ledger movements; position P&L includes only
        # final completed results and would miss partial-exit daily stop movements.
        movements = []
        position_tickers = {str(row.get("id", row.get("position_id"))): str(row.get("market_ticker", row.get("ticker", ""))) for row in positions}
        for row in ledger:
            pnl = _decimal(row.get("realized_pnl", row.get("pnl")))
            at = row.get("received_at", row.get("created_at", row.get("timestamp", row.get("at"))))
            if pnl is not None and at is not None:
                movements.append((_time(at), str(row.get("market_ticker", row.get("ticker", position_tickers.get(str(row.get("position_id")), "")))), pnl))
        if not movements:
            for position in positions:
                pnl = _decimal(position.get("realized_pnl", position.get("net_pnl")))
                at = position.get("closed_at")
                if pnl is not None and at is not None:
                    movements.append((_time(at), str(position.get("market_ticker", position.get("ticker", ""))), pnl))
        daily = defaultdict(lambda: ZERO)
        cumulative = peak = drawdown = gross_losses = gross_gains = ZERO
        loss_count = 0
        for at, ticker, pnl in sorted(movements, key=lambda item: (item[0], item[1])):
            daily[datetime.fromtimestamp(at, NY).date().isoformat()] += pnl
            cumulative += pnl
            peak = max(peak, cumulative)
            drawdown = max(drawdown, peak - cumulative)
            gross_losses += -pnl if pnl < ZERO else ZERO
            gross_gains += pnl if pnl > ZERO else ZERO
            loss_count += pnl < ZERO
        fees = sum((D(row.get("fee", row.get("fees", "0"))) for row in fills), ZERO)
        completed = [position for position in positions if position.get("closed_at") is not None]
        completed.sort(key=lambda row: (_time(row["closed_at"]), str(row.get("market_ticker", row.get("ticker", "")))))
        streak = maximum_streak = 0
        streak_day = None
        for position in completed:
            day = datetime.fromtimestamp(_time(position["closed_at"]), NY).date()
            if day != streak_day:
                streak = 0
                streak_day = day
            pnl = _decimal(position.get("realized_pnl", position.get("net_pnl")))
            if pnl is None:
                continue
            if pnl < ZERO:
                streak += 1
                maximum_streak = max(maximum_streak, streak)
            elif pnl > ZERO:
                streak = 0
        order_ids = {str(row.get("order_id")) for row in fills if row.get("order_id") is not None}
        entry_orders = [row for row in orders if row.get("kind", row.get("type", row.get("action", "entry"))) in ("entry", "buy")]
        filled_orders = sum(str(row.get("order_id", row.get("id"))) in order_ids or
                            int(row.get("filled_quantity", 0) or 0) > 0 for row in entry_orders)
    horizon_aggregates = {}
    for horizon in HORIZONS:
        observations = [entry["fixed_elapsed_horizons"][str(horizon)] for entry in entries if "fixed_elapsed_horizons" in entry]
        eligible = [row for row in observations if row["eligible"]]
        values = [D(row["net_pnl"]) for row in eligible if row["observed"]]
        horizon_aggregates[str(horizon)] = {"eligible_trades": len(eligible), "fully_observed_trades": len(values),
                                           "missing_or_censored": len(eligible) - len(values),
                                           "horizon_after_cutoff": len(observations) - len(eligible),
                                           "net_pnl": _distribution(values)}
    bootstrap_records = [{"day": day, "net_pnl": str(pnl)} for day, pnl in sorted(daily.items())]
    return jsonable({"dataset": dataset, "tables": tables, "paper_only": True,
                     "account": {"realized_pnl": cumulative, "realized_pnl_by_new_york_day": dict(sorted(daily.items())),
                                 "fees": fees, "gross_realized_losses": gross_losses, "loss_movements": loss_count,
                                 "maximum_realized_drawdown": drawdown, "maximum_completed_loss_streak": maximum_streak,
                                 "profit_factor": gross_gains / gross_losses if gross_losses else None,
                                 "profit_factor_status": "defined" if gross_losses else "undefined_without_losses",
                                 "completed_positions": len(completed), "entry_order_attempts": len(entry_orders),
                                 "filled_entry_orders": filled_orders,
                                 "fill_rate": D(filled_orders) / len(entry_orders) if entry_orders else None,
                                 "drawdown_basis": "realized cash P&L only; unpriced open positions excluded"},
                     "trajectories": {"positions": len(entries), "with_observations": sum(entry.get("observed_points", 0) > 0 for entry in entries),
                                      "unavailable": sum(entry.get("status") == "unavailable" for entry in entries),
                                      "mfe": _distribution([D(entry["mfe"]) for entry in entries if entry.get("mfe") is not None]),
                                      "mae": _distribution([D(entry["mae"]) for entry in entries if entry.get("mae") is not None]),
                                      "fixed_elapsed_horizons": horizon_aggregates, "entries": entries},
                     "day_bootstrap": day_block_bootstrap(bootstrap_records, dataset=dataset["label"]),
                     "limitations": ["No calibrated probability or profitability claim is produced.",
                                     "Daily bootstrap above summarizes daily aggregates, not trade expectancy.",
                                     "Unknown fees, missing depth and pending official settlements require independent validation.",
                                     "Deployment and data costs are not included in trading P&L."]})
