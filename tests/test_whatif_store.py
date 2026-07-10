"""Tests for whatif persistent store: config, schema/CRUD, job steps."""

from __future__ import annotations

from swing_tracker.config import WhatIfConfig, load_config


class TestWhatIfConfig:
    def test_defaults(self):
        cfg = WhatIfConfig()
        assert cfg.enabled is True
        assert cfg.max_holding_days == 60

    def test_load_from_toml(self):
        config = load_config()
        assert isinstance(config.whatif, WhatIfConfig)
        assert config.whatif.max_holding_days == 60
