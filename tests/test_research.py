import json
from pathlib import Path
import sqlite3

import pytest

from kalshi_bot.models import D
from kalshi_bot.research import (
    analyze, compare_exits, compare_policies, day_block_bootstrap,
    export_parquet, fit_training_scaler, freeze_chronological_split, future_liquidation_labels, trajectory_summary,
    unusual_flow_diagnostic,
)


def point(at, net, minimum="0.60", **kwargs):
    return {"received_at": at, "executable_net_liquidation": net,
            "executable_quantity": 10, "depth_available": net is not None,
            "liquidation_min_price": minimum, **kwargs}


def test_excursions_use_executable_depth_not_midpoint_or_settlement():
    points = [point(0, "3.5"), point(1, None, mid="0.99"), point(2, "4.50"),
              point(3, "2.00"), point(6, "7.00"), point(121, "10")]
    report = trajectory_summary(points, "4.00", 10, 120)
    assert D(report["mfe"]) == D("3")
    assert D(report["mae"]) == D("-2")
    assert D(report["mfe_cents_per_share"]) == D("30")
    assert report["missing_points"] == 1
    assert report["fixed_elapsed_horizons"]["5"]["observed"]
    assert report["fixed_elapsed_horizons"]["5"]["net_pnl"] == "-2.00"
    assert not report["fixed_elapsed_horizons"]["120"]["observed"]
    assert D(report["observable_seconds"]) == 2  # no gap/interpolation bridge


def test_full_liquidation_does_not_accept_insufficient_quantity():
    report = trajectory_summary([point(0, "9", executable_quantity=9)], "4", 10, 1)
    assert report["mfe"] is None
    assert report["status"] == "unavailable"


def test_fixed_exit_delays_and_honors_all_shares_target():
    points = [point(0, "6", "0.60"), point(1, "5", "0.50"),
              point(2, "6.2", "0.62"), point(3, "6.1", "0.61")]
    outcome = compare_policies(points, "4", 10, 120, settlement_proceeds="0")["policies"]["fixed_60"]
    assert outcome["filled_at"] == 3
    assert outcome["net_pnl"] == "2.1"
    assert outcome["unavailable_or_failed_arrivals"] == 1
    assert outcome["paired_difference_vs_95"] == "6.1"


def test_unknown_settlement_remains_pending_and_no_cutoff_execution():
    report = compare_policies([point(10, "9.9", "0.99")], "4", 10, 10, delay_seconds=0)
    assert report["policies"]["fixed_95"]["status"] == "pending_or_censored"
    assert report["policies"]["fixed_95"]["net_pnl"] is None


def test_best_bid_is_not_full_position_executable_target():
    report = compare_policies([{**point(0, "9.6"), "liquidation_min_price": None,
                                "best_bid": "0.99"}], "4", 10, 10, delay_seconds=0)
    assert report["policies"]["fixed_95"]["net_pnl"] is None


def test_reversal_requires_consecutive_valid_decisions_and_delay():
    points = [point(at, "5", direction_adjusted_return_5s="-1") for at in (0, 1, 5, 6, 7, 8)]
    out = compare_policies(points, "4", 10, 120)["policies"]["reversal_3"]
    assert out["triggered_at"] == 7
    assert out["filled_at"] == 8


def test_unavailable_experimental_features_fall_back_to_baseline():
    points = [point(0, "9.5", "0.95"), point(1, "9.5", "0.95")]
    out = compare_policies(points, "4", 10, 120)["policies"]["unusual_flow"]
    assert out["reason"] == "baseline_95_fallback"
    assert out["feature_fallback_decisions"] == 1


def test_trailing_policy_arms_on_net_profit():
    points = [point(0, "4.9"), point(1, "5.2"), point(2, "4.6"), point(3, "4.5")]
    out = compare_policies(points, "4", 10, 120)["policies"]["trailing_10_5"]
    assert out["triggered_at"] == 2
    assert out["net_pnl"] == "0.5"


def test_split_groups_assets_purges_crossing_labels_and_is_immutable(tmp_path):
    records = [
        {"entry_id": "btc", "received_at": 900, "label_end": 1000},
        {"entry_id": "eth", "received_at": 901, "label_end": 1001},
        {"entry_id": "edge", "received_at": 85500, "label_end": 86401},
        {"entry_id": "embargo", "received_at": 86401, "label_end": 86500},
        {"entry_id": "pending", "received_at": 172800 + 900, "label_end": None},
    ]
    target = tmp_path / "split.json"
    report = freeze_chronological_split(records, target, collection_start=0,
                                        instrumentation_days=0, train_days=1,
                                        validation_days=1, test_days=1)
    rows = {row["entry_id"]: row for row in report["assignments"]}
    assert rows["btc"]["fold"] == rows["eth"]["fold"] == "train"
    assert rows["btc"]["quarter_hour"] == rows["eth"]["quarter_hour"]
    assert rows["edge"]["reason"] == "label_crosses_boundary"
    assert rows["embargo"]["reason"] == "15_minute_embargo"
    assert rows["pending"]["reason"] == "pending_label"
    assert freeze_chronological_split(records, target, collection_start=0,
                                       instrumentation_days=0, train_days=1,
                                       validation_days=1, test_days=1) == report
    with pytest.raises(ValueError, match="immutable"):
        freeze_chronological_split(records[:-1], target, collection_start=0,
                                   instrumentation_days=0, train_days=1,
                                   validation_days=1, test_days=1)


def test_bootstrap_never_infers_performance_from_demo_or_sparse_data():
    records = [{"day": str(index).zfill(2), "net_pnl": "1"} for index in range(20)]
    assert day_block_bootstrap(records, dataset="demo")["confidence_interval"] is None
    assert day_block_bootstrap(records[:4], dataset="real")["confidence_interval"] is None
    real = day_block_bootstrap(records, dataset="real", samples=100, seed=17)
    assert real == day_block_bootstrap(records, dataset="real", samples=100, seed=17)
    assert [D(value) for value in real["confidence_interval"]] == [D(1), D(1)]


def test_unusual_flow_requires_prior_history_and_does_not_use_future():
    assert not unusual_flow_diagnostic("100", [(0, "1")] * 999, 1)["available"]
    prior = [(0, "1")] * 1000 + [(2, "10000")] * 1000
    report = unusual_flow_diagnostic("5", prior, 1)
    assert report["prior_trades"] == 1000
    assert report["unusual"]


def research_database(tmp_path):
    target = tmp_path / "research.sqlite3"
    connection = sqlite3.connect(target)
    connection.executescript("""
        CREATE TABLE run_config(run_id TEXT,config_json TEXT);
        CREATE TABLE positions(id TEXT,market_ticker TEXT,side TEXT,quantity INTEGER,entry_cost TEXT,
                               realized_pnl TEXT,opened_at REAL,closed_at REAL,market_json TEXT);
        CREATE TABLE trajectories(id INTEGER,position_id TEXT,received_at REAL,payload TEXT);
        CREATE TABLE orders(id TEXT,status TEXT);
        CREATE TABLE fills(id TEXT,position_id TEXT,order_id TEXT,kind TEXT,quantity INTEGER,
                           price TEXT,fee TEXT,created_at REAL);
        CREATE TABLE ledger(position_id TEXT,realized_pnl TEXT,created_at REAL);
        CREATE TABLE events(id INTEGER,type TEXT,payload TEXT);
    """)
    connection.execute("INSERT INTO run_config VALUES (?,?)", ("run", json.dumps({"dataset": "demo", "api_key": "never-export"})))
    connection.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?)",
                       ("p", "market", "yes", 10, "3.99999999999999", "-3.99999999999999", 0, 121,
                        json.dumps({"cutoff": 120})))
    connection.execute("INSERT INTO orders VALUES ('o','filled')")
    connection.execute("INSERT INTO orders VALUES ('o2','cancelled')")
    connection.execute("INSERT INTO fills VALUES ('f','p','o','entry',10,'0.38','0.19999999999999',0)")
    connection.execute("INSERT INTO fills VALUES ('s','p','o','settlement',10,'0','0',121)")
    connection.execute("INSERT INTO ledger VALUES ('p','-3.99999999999999',121)")
    for i in range(121):
        payload = point(i, "5.99999999999999", "0.60")
        connection.execute("INSERT INTO trajectories VALUES (?,?,?,?)", (i, "p", i, json.dumps(payload)))
    connection.commit()
    connection.close()
    return target


def test_sql_reports_are_read_only_exact_and_clearly_demo(tmp_path):
    database = research_database(tmp_path)
    original = database.read_bytes()
    report = analyze(database)
    assert report["dataset"]["label"] == "demo"
    assert report["account"]["realized_pnl"] == "-3.99999999999999"
    assert report["account"]["fees"] == "0.19999999999999"
    assert report["account"]["fill_rate"] == "0.5"
    assert report["account"]["maximum_realized_drawdown"] == "3.99999999999999"
    assert report["trajectories"]["with_observations"] == 1
    assert report["day_bootstrap"]["confidence_interval"] is None
    exits = compare_exits(database)
    assert exits["entries"][0]["policies"]["fixed_95"]["status"] == "settled"
    assert exits["entries"][0]["policies"]["fixed_60"]["net_pnl"] == "2.00000000000000"
    assert database.read_bytes() == original


def test_parquet_preserves_money_strings_and_redacts_config(tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    database = research_database(tmp_path)
    output = tmp_path / "export"
    counts = export_parquet(database, output)
    assert counts["positions"] == 1
    assert pq.read_table(output / "positions.parquet").to_pylist()[0]["entry_cost"] == "3.99999999999999"
    config = pq.read_table(output / "run_config.parquet").to_pylist()[0]["config_json"]
    assert "never-export" not in config
    assert json.loads((output / "manifest.json").read_text())["dataset"]["label"] == "demo"


def book_point(at, qty, *, bid="0.60", depth_fraction="1"):
    return {**point(at, None), "side": "yes", "book": {
        "ticker": "m", "yes_bids": [[bid, qty]], "no_bids": [["0.30", 100]],
        "received_at": at, "healthy": True},
        "fee": {"multiplier": "1", "balance_quantum": "0.0001", "scenario": "test"},
        "depth_fraction": depth_fraction, "tick_size": "0.01", "price_ranges": []}


def test_partial_delayed_exits_have_exact_fees_and_settle_residual_only():
    points = [book_point(0, 6), book_point(1, 4)]
    out = compare_policies(points, "4", 10, 120, settlement_proceeds="10")["policies"]["fixed_60"]
    assert out["status"] == "partially_exited_then_settled"
    assert out["executions"][0]["quantity"] == 4  # arrival depth, not trigger depth
    assert D(out["exit_fees"]) == D("0.0672")
    assert D(out["observed_exit_proceeds"]) == D("2.3328")
    assert D(out["net_proceeds"]) == D("8.3328")  # sell 4 + official payout on remaining 6
    assert D(out["net_pnl"]) == D("4.3328")
    assert out["unexited_quantity"] == 6


def test_partial_exit_unknown_settlement_does_not_invent_residual_value():
    points = [book_point(0, 6), book_point(1, 4)]
    out = compare_policies(points, "4", 10, 120)["policies"]["fixed_60"]
    assert out["status"] == "partially_exited_pending_or_censored"
    assert out["net_proceeds"] is None
    assert out["net_pnl"] is None
    assert D(out["observed_exit_proceeds"]) == D("2.3328")


def test_labels_distinguish_negative_missing_and_postcutoff():
    points = [point(at, "4") for at in range(31)]
    report = future_liquidation_labels(points, decision_at=0, quantity=10, cutoff=100)
    assert report["30"]["improves_5c_per_share"] is False
    assert report["60"]["improves_5c_per_share"] is None
    assert report["120"]["reason"] == "horizon_after_cutoff"
    points[20]["executable_net_liquidation"] = "4.50"
    assert future_liquidation_labels(points, decision_at=0, quantity=10, cutoff=100)["30"]["improves_5c_per_share"] is True
    points.pop(5)
    assert future_liquidation_labels(points, decision_at=0, quantity=10, cutoff=100)["30"]["adverse_10c_first"] is None


def test_scaling_fits_only_training_and_rejects_future_features():
    manifest = {"sha256": "frozen", "assignments": [
        {"entry_id": "a", "fold": "train"}, {"entry_id": "b", "fold": "test"}]}
    records = [{"entry_id": "a", "received_at": 2, "feature_received_at": 1, "features": {"return": "2"}},
               {"entry_id": "b", "received_at": 2, "feature_received_at": 1, "features": {"return": "1000"}}]
    scaler = fit_training_scaler(records, ["return"], manifest)
    assert scaler["features"]["return"]["mean"] == "2"
    records[0]["feature_received_at"] = 3
    with pytest.raises(ValueError, match="receipt time"):
        fit_training_scaler(records, ["return"], manifest)
