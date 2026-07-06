"""Scanner likidite filtresi: liquid_universe bos oldugunda kesisim atlanmali.

Kok neden (2026-07-06 homelab deploy): DB sifirlandiginda tablo bos kalir,
fallback evren (XU100) ile XTUMY adaylarinin kesisimi 0 cikar ve sistem
build calisana kadar hic aday uretmez ("fallback korlugu").
"""

from __future__ import annotations

import sqlite3

import pytest

from swing_tracker.config import CacheConfig, Config, LiquidityConfig, ScannerConfig
from swing_tracker.core.scanner import Scanner
from swing_tracker.core.universe import UniverseBuilder
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


@pytest.fixture
def repo() -> Repository:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    create_all_tables(conn)
    return Repository(conn)


@pytest.fixture
def config() -> Config:
    c = Config()
    c.scanner = ScannerConfig(universe="XTUMY", market_regime_index="XU100")
    c.cache = CacheConfig(enabled=True)
    c.liquidity = LiquidityConfig(
        enabled=True,
        min_daily_volume_tl=10_000_000,
        min_volume_days=15,
        excluded_markets=["GOZALTI PAZARI"],
        fallback_universe="XU100",
        market_cache_ttl_days=7,
        builder_max_workers=2,
    )
    return c


def _liquid_row(symbol: str) -> dict:
    return {
        "symbol": symbol,
        "market": "YILDIZ PAZARI",
        "median_volume_tl": 50_000_000.0,
        "volume_days": 20,
        "last_close": 10.0,
        "market_cap_tl": 1_000_000_000.0,
    }


class TestHasBuiltUniverse:
    def test_false_when_table_empty(self, repo, config):
        builder = UniverseBuilder(repo, config)
        assert builder.has_built_universe() is False

    def test_true_when_table_populated(self, repo, config):
        repo.upsert_liquid_symbol(**_liquid_row("AEFES"))
        builder = UniverseBuilder(repo, config)
        assert builder.has_built_universe() is True


class TestApplyLiquidityFilter:
    def test_skips_intersection_when_table_empty(self, repo, config):
        """Tablo bosken adaylar OLDUGU GIBI kalmali (fallback kesisimi yok)."""
        builder = UniverseBuilder(repo, config)
        scanner = Scanner(repo, config, universe_builder=builder)

        candidates = {"ADEL", "AGROT", "AKENR"}
        result = scanner._apply_liquidity_filter(candidates)

        assert result == candidates

    def test_intersects_when_table_populated(self, repo, config):
        repo.upsert_liquid_symbol(**_liquid_row("ADEL"))
        repo.upsert_liquid_symbol(**_liquid_row("AEFES"))
        builder = UniverseBuilder(repo, config)
        scanner = Scanner(repo, config, universe_builder=builder)

        result = scanner._apply_liquidity_filter({"ADEL", "AGROT", "AKENR"})

        assert result == {"ADEL"}

    def test_returns_candidates_when_liquidity_disabled(self, repo, config):
        config.liquidity.enabled = False
        builder = UniverseBuilder(repo, config)
        scanner = Scanner(repo, config, universe_builder=builder)

        candidates = {"ADEL", "AGROT"}
        assert scanner._apply_liquidity_filter(candidates) == candidates

    def test_returns_candidates_when_no_builder(self, repo, config):
        scanner = Scanner(repo, config, universe_builder=None)

        candidates = {"ADEL", "AGROT"}
        assert scanner._apply_liquidity_filter(candidates) == candidates
