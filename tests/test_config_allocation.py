from swing_tracker.config import load_config


def test_allocation_config_parsed(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        """
[allocation]
enabled = true
monthly_contribution_usd = 750
drift_threshold_pct = 5.0
review_interval_days = 91
fractional = true

[allocation.targets.VOO]
weight = 28
exchange = "AMEX"
group = "core"
note = "S&P 500"

[allocation.targets.QTUM]
weight = 20
exchange = "NASDAQ"
group = "satellite"
note = "Kuantum/AI"
""",
        encoding="utf-8",
    )
    config = load_config(cfg_file)
    assert config.allocation.enabled is True
    assert config.allocation.monthly_contribution_usd == 750
    assert config.allocation.fractional is True
    assert set(config.allocation.targets) == {"VOO", "QTUM"}
    voo = config.allocation.targets["VOO"]
    assert voo.weight == 28
    assert voo.exchange == "AMEX"
    assert voo.group == "core"


def test_allocation_config_defaults_when_missing(tmp_path):
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text("[general]\n", encoding="utf-8")
    config = load_config(cfg_file)
    assert config.allocation.enabled is True
    assert config.allocation.targets == {}
