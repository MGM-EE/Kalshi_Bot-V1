import pytest

from kalshi_bot.config import BotConfig


@pytest.mark.parametrize("values", [
    {"clock_uncertainty_ms": float("nan")},
    {"clock_uncertainty_ms": float("inf")},
    {"depth_fraction": "NaN"}, {"depth_fraction": "Infinity"},
    {"refresh_seconds": float("nan")}, {"execution_delay_ms": True},
    {"assets": ["BTC", "BTC"]}, {"api_url": "http://external-api.kalshi.com"},
])
def test_invalid_health_and_execution_settings_are_rejected(values):
    with pytest.raises(ValueError):
        BotConfig(**values)


def test_example_defaults_are_collect_only_and_paths_resolve(tmp_path):
    config = BotConfig.load("config.example.toml")
    assert config.clock_uncertainty_ms is None
    assert config.fee_scenario is None
    file = tmp_path / "custom.toml"
    file.write_text('market_overrides_file = "rules.json"\n')
    assert BotConfig.load(str(file)).market_overrides_file == str(tmp_path / "rules.json")


def test_unknown_options_are_reported(tmp_path):
    file = tmp_path / "custom.toml"
    file.write_text("live_orders = true\n")
    with pytest.raises(ValueError, match="live_orders"):
        BotConfig.load(str(file))


def test_changed_override_contents_require_a_new_frozen_run(tmp_path):
    from kalshi_bot.engine import Engine
    file = tmp_path / "rules.json"
    file.write_text('{"markets":{}}')
    settings = BotConfig(market_overrides_file=str(file))
    engine = Engine(tmp_path / "run", settings, mode="demo")
    assert engine.frozen_overrides.read_text() == file.read_text()
    engine.close()
    file.write_text('{"markets":{},"review_version":2}')
    with pytest.raises(ValueError, match="frozen configuration"):
        Engine(tmp_path / "run", settings, mode="demo")
