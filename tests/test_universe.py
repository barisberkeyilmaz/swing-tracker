from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from swing_tracker.config import CacheConfig, Config, LiquidityConfig, ScannerConfig
from swing_tracker.core.universe import UNKNOWN_MARKET, UniverseBuilder
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


def _daily_df(days: int, price: float, volume_lot: float) -> pd.DataFrame:
    idx = pd.date_range(end="2026-04-18", periods=days, freq="D")
    return pd.DataFrame(
        {
            "Open": [price] * days,
            "High": [price * 1.02] * days,
            "Low": [price * 0.98] * days,
            "Close": [price] * days,
            "Volume": [volume_lot] * days,
        },
        index=idx,
    )


def _make_builder(repo, config, market_overrides=None, history_overrides=None):
    """Build UniverseBuilder with mocked KAP + ohlcv fetches."""
    market_overrides = market_overrides or {}
    history_overrides = history_overrides or {}

    def fake_kap(symbol):
        return market_overrides.get(symbol, ("YILDIZ PAZAR", "TEKNOLOJI"))

    def fake_ohlcv(symbol, *, interval, period, repo, cache_cfg, **kw):
        return history_overrides.get(symbol)

    builder = UniverseBuilder(repo, config)
    patches = [
        patch("swing_tracker.core.universe._fetch_kap_market", side_effect=fake_kap),
        patch("swing_tracker.core.universe.get_ohlcv", side_effect=fake_ohlcv),
        patch("swing_tracker.core.universe._fetch_market_cap", return_value=None),
    ]
    for p in patches:
        p.start()
    return builder, patches


def _stop(patches):
    for p in patches:
        p.stop()


def test_builder_filters_by_volume(repo, config):
    # LIKID: 25M TL hacim, likit. KUCUK: 500K TL, filtre disi.
    histories = {
        "LIKID": _daily_df(days=20, price=100, volume_lot=250_000),   # 25M TL
        "KUCUK": _daily_df(days=20, price=10, volume_lot=50_000),     # 500K TL
    }
    builder, patches = _make_builder(repo, config, history_overrides=histories)
    try:
        with patch("swing_tracker.core.universe.bp.Index") as mock_idx:
            mock_idx.return_value.components = [
                {"symbol": "LIKID"}, {"symbol": "KUCUK"},
            ]
            total, kept = builder.build()
    finally:
        _stop(patches)
        builder.close()

    assert total == 2
    assert kept == 1
    liquids = [r["symbol"] for r in repo.get_liquid_symbols()]
    assert liquids == ["LIKID"]


def test_builder_excludes_market(repo, config):
    histories = {
        "GOZA": _daily_df(days=20, price=100, volume_lot=500_000),
        "OK":   _daily_df(days=20, price=100, volume_lot=500_000),
    }
    markets = {
        "GOZA": ("GOZALTI PAZARI", None),
        "OK":   ("ANA PAZAR", None),
    }
    builder, patches = _make_builder(repo, config, market_overrides=markets,
                                      history_overrides=histories)
    try:
        with patch("swing_tracker.core.universe.bp.Index") as mock_idx:
            mock_idx.return_value.components = [{"symbol": "GOZA"}, {"symbol": "OK"}]
            builder.build()
    finally:
        _stop(patches)
        builder.close()

    assert [r["symbol"] for r in repo.get_liquid_symbols()] == ["OK"]


def test_yip_kept_with_agresif_profile(repo, config):
    # YIP dislanmiyor (agresif profil), hacim yeterliyse kalir.
    histories = {
        "YIP1": _daily_df(days=20, price=100, volume_lot=500_000),   # 50M TL
    }
    markets = {"YIP1": ("YAKIN IZLEME PAZARI", None)}
    builder, patches = _make_builder(repo, config, market_overrides=markets,
                                      history_overrides=histories)
    try:
        with patch("swing_tracker.core.universe.bp.Index") as mock_idx:
            mock_idx.return_value.components = [{"symbol": "YIP1"}]
            builder.build()
    finally:
        _stop(patches)
        builder.close()

    liquids = repo.get_liquid_symbols()
    assert len(liquids) == 1
    assert liquids[0]["market"] == "YAKIN IZLEME PAZARI"


def test_min_volume_days_enforced(repo, config):
    # 10 gun veri → min 15 olarak ayarli, reddedilir.
    histories = {"AZVERI": _daily_df(days=10, price=100, volume_lot=500_000)}
    builder, patches = _make_builder(repo, config, history_overrides=histories)
    try:
        with patch("swing_tracker.core.universe.bp.Index") as mock_idx:
            mock_idx.return_value.components = [{"symbol": "AZVERI"}]
            builder.build()
    finally:
        _stop(patches)
        builder.close()

    assert repo.get_liquid_symbols() == []


def test_market_cache_ttl_respected(repo, config):
    """7 gun icinde KAP tekrar cagrilmaz, disinda cagrilir."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # 3 gun once fetch edilmis bir kayit
    repo.upsert_symbol_market(
        "CACHED", market="ANA PAZAR", sector=None,
        fetched_at=(now - timedelta(days=3)).isoformat(),
    )
    call_count = {"n": 0}

    def counting_kap(symbol):
        call_count["n"] += 1
        return ("YENI_KAP", None)

    builder = UniverseBuilder(repo, config)
    try:
        with patch("swing_tracker.core.universe._fetch_kap_market", side_effect=counting_kap):
            m1, _ = builder._get_market_info("CACHED", now)
            # 7 gun icinde, KAP cagrilmadi
            assert call_count["n"] == 0
            assert m1 == "ANA PAZAR"

            # Simdi 8 gun once icin TTL asildi
            old_now = now + timedelta(days=5)  # fetched_at 3 gun once → simdi 8 gun
            builder._get_market_info("CACHED", old_now)
            assert call_count["n"] == 1
    finally:
        builder.close()


def test_fallback_when_empty_table(repo, config):
    builder = UniverseBuilder(repo, config)
    try:
        with patch("swing_tracker.core.universe.bp.Index") as mock_idx:
            mock_idx.return_value.components = [
                {"symbol": "AKBNK"}, {"symbol": "THYAO"}, {"symbol": "GARAN"},
            ]
            symbols = builder.get_liquid_symbols()
    finally:
        builder.close()

    # Tablo bos → fallback XU100'e dusmeli
    assert symbols == ["AKBNK", "THYAO", "GARAN"]


def test_build_cleans_stale_symbols(repo, config):
    # Ilk build: A ve B likit.
    # Ikinci build: sadece A ve C geliyor. B silinmeli.
    first = {
        "A": _daily_df(days=20, price=100, volume_lot=500_000),
        "B": _daily_df(days=20, price=100, volume_lot=500_000),
    }
    builder, patches = _make_builder(repo, config, history_overrides=first)
    try:
        with patch("swing_tracker.core.universe.bp.Index") as mock_idx:
            mock_idx.return_value.components = [{"symbol": "A"}, {"symbol": "B"}]
            builder.build()
    finally:
        _stop(patches)

    assert {r["symbol"] for r in repo.get_liquid_symbols()} == {"A", "B"}

    second = {
        "A": _daily_df(days=20, price=100, volume_lot=500_000),
        "C": _daily_df(days=20, price=100, volume_lot=500_000),
    }
    builder2, patches2 = _make_builder(repo, config, history_overrides=second)
    try:
        with patch("swing_tracker.core.universe.bp.Index") as mock_idx:
            mock_idx.return_value.components = [{"symbol": "A"}, {"symbol": "C"}]
            builder2.build()
    finally:
        _stop(patches2)
        builder2.close()
        builder.close()

    final = {r["symbol"] for r in repo.get_liquid_symbols()}
    assert final == {"A", "C"}


def test_unknown_market_is_included(repo, config):
    """KAP fetch fail olan sembol UNKNOWN ile dahil edilir (excluded degil)."""
    histories = {"XBOGUS": _daily_df(days=20, price=100, volume_lot=500_000)}
    markets = {"XBOGUS": (None, None)}  # KAP'tan bosluk geldi
    builder, patches = _make_builder(repo, config, market_overrides=markets,
                                      history_overrides=histories)
    try:
        with patch("swing_tracker.core.universe.bp.Index") as mock_idx:
            mock_idx.return_value.components = [{"symbol": "XBOGUS"}]
            builder.build()
    finally:
        _stop(patches)
        builder.close()

    liquids = repo.get_liquid_symbols()
    assert len(liquids) == 1
    assert liquids[0]["market"] == UNKNOWN_MARKET
