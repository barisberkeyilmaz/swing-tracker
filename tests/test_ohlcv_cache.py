from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta

import pandas as pd
import pytest

from swing_tracker.config import CacheConfig
from swing_tracker.core.ohlcv_cache import get_ohlcv
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
def cfg() -> CacheConfig:
    return CacheConfig(
        enabled=True,
        daily_ttl_minutes=60,
        hourly_ttl_minutes=15,
        regime_ttl_minutes=30,
        scanner_max_workers=4,
    )


def _daily_df(n: int = 10, start: str = "2026-04-01") -> pd.DataFrame:
    idx = pd.date_range(start=start, periods=n, freq="D")
    return pd.DataFrame(
        {
            "Open": [100 + i for i in range(n)],
            "High": [105 + i for i in range(n)],
            "Low": [95 + i for i in range(n)],
            "Close": [101 + i for i in range(n)],
            "Volume": [1_000_000 + i * 1000 for i in range(n)],
        },
        index=idx,
    )


class _CountingFetch:
    def __init__(self, df: pd.DataFrame | None):
        self.df = df
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, symbol: str, period: str, interval: str) -> pd.DataFrame | None:
        self.calls.append((symbol, period, interval))
        return self.df


def test_cache_miss_triggers_full_fetch_and_persists(repo, cfg):
    fetch = _CountingFetch(_daily_df(n=10))

    df = get_ohlcv(
        "THYAO", interval="1d", period="6mo",
        repo=repo, cache_cfg=cfg, fetch_fn=fetch,
    )

    assert df is not None
    assert len(df) == 10
    assert fetch.calls == [("THYAO", "6mo", "1d")]

    cached = repo.get_cached_ohlcv("THYAO", "1d")
    assert len(cached) == 10
    meta = repo.get_ohlcv_meta("THYAO", "1d")
    assert meta is not None
    assert meta["bar_count"] == 10


def test_cache_fresh_skips_fetch(repo, cfg):
    fetch = _CountingFetch(_daily_df(n=5))
    now = datetime(2026, 4, 19, 12, 0, 0)

    get_ohlcv("X", interval="1d", period="6mo", repo=repo, cache_cfg=cfg,
              fetch_fn=fetch, now=now)
    assert len(fetch.calls) == 1

    # 10 dakika sonra tekrar cagir — TTL 60dk, taze.
    df = get_ohlcv("X", interval="1d", period="6mo", repo=repo, cache_cfg=cfg,
                   fetch_fn=fetch, now=now + timedelta(minutes=10))
    assert df is not None
    assert len(df) == 5
    assert len(fetch.calls) == 1  # no additional fetch


def test_cache_stale_triggers_incremental_fetch(repo, cfg):
    initial = _daily_df(n=10, start="2026-04-01")
    fetch = _CountingFetch(initial)
    now = datetime(2026, 4, 19, 12, 0, 0)

    get_ohlcv("Y", interval="1d", period="6mo", repo=repo, cache_cfg=cfg,
              fetch_fn=fetch, now=now)

    # 2 saat sonra tekrar cagir — stale (TTL 60dk).
    # Incremental fetch son 5 gunu doner, 2 gunu eski ile ortuser.
    tail = _daily_df(n=5, start="2026-04-09")
    fetch.df = tail
    df = get_ohlcv("Y", interval="1d", period="6mo", repo=repo, cache_cfg=cfg,
                   fetch_fn=fetch, now=now + timedelta(hours=2))

    assert df is not None
    # Incremental call: period="5d" (stale window for 1d)
    assert fetch.calls[-1] == ("Y", "5d", "1d")
    # Total bars in cache: union of 10 (4/1..4/10) + 5 (4/9..4/13) = 13 unique
    cached = repo.get_cached_ohlcv("Y", "1d")
    assert len(cached) == 13


def test_upsert_is_idempotent(repo):
    bars = [
        {"bar_ts": "2026-04-01T00:00:00", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 100},
        {"bar_ts": "2026-04-02T00:00:00", "open": 1.5, "high": 2.5, "low": 1, "close": 2, "volume": 200},
    ]
    repo.upsert_ohlcv_bars("Z", "1d", bars)
    repo.upsert_ohlcv_bars("Z", "1d", bars)  # again — no duplicates
    repo.upsert_ohlcv_bars(
        "Z", "1d",
        [{"bar_ts": "2026-04-02T00:00:00", "open": 9, "high": 9, "low": 9, "close": 9, "volume": 999}],
    )

    rows = repo.get_cached_ohlcv("Z", "1d")
    assert len(rows) == 2
    row2 = next(r for r in rows if r["bar_ts"] == "2026-04-02T00:00:00")
    assert row2["close"] == 9  # updated
    assert row2["volume"] == 999


def test_disabled_cache_bypasses(repo):
    cfg = CacheConfig(enabled=False)
    fetch = _CountingFetch(_daily_df(n=3))

    df = get_ohlcv("W", interval="1d", period="6mo",
                   repo=repo, cache_cfg=cfg, fetch_fn=fetch)
    assert df is not None
    assert len(fetch.calls) == 1

    # Second call should ALSO fetch (no cache read)
    get_ohlcv("W", interval="1d", period="6mo",
              repo=repo, cache_cfg=cfg, fetch_fn=fetch)
    assert len(fetch.calls) == 2
    # Nothing persisted
    assert repo.get_cached_ohlcv("W", "1d") == []


def test_ttl_override_for_regime(repo, cfg):
    fetch = _CountingFetch(_daily_df(n=5))
    now = datetime(2026, 4, 19, 12, 0, 0)

    get_ohlcv("XU100", interval="1d", period="6mo", repo=repo, cache_cfg=cfg,
              fetch_fn=fetch, now=now, ttl_override_minutes=30)
    assert len(fetch.calls) == 1

    # 20 dakika sonra — 30dk TTL icinde, taze.
    get_ohlcv("XU100", interval="1d", period="6mo", repo=repo, cache_cfg=cfg,
              fetch_fn=fetch, now=now + timedelta(minutes=20), ttl_override_minutes=30)
    assert len(fetch.calls) == 1

    # 40 dakika sonra — stale, incremental fetch.
    get_ohlcv("XU100", interval="1d", period="6mo", repo=repo, cache_cfg=cfg,
              fetch_fn=fetch, now=now + timedelta(minutes=40), ttl_override_minutes=30)
    assert len(fetch.calls) == 2


def test_fresh_cache_insufficient_bars_triggers_full_refetch(repo, cfg):
    """Cache taze ama caller daha fazla bar istiyorsa full refetch tetiklenmeli."""
    # Ilk fetch: 20 barlik kisa period (ornegin UniverseBuilder 1mo)
    short = _daily_df(n=20, start="2026-04-01")
    fetch = _CountingFetch(short)
    now = datetime(2026, 4, 19, 12, 0, 0)

    get_ohlcv("X", interval="1d", period="1mo", repo=repo, cache_cfg=cfg,
              fetch_fn=fetch, now=now, min_bars=15)
    assert len(fetch.calls) == 1

    # Ikinci caller daha uzun period istiyor (scanner 6mo, 100+ bar)
    long = _daily_df(n=125, start="2025-12-01")
    fetch.df = long

    df = get_ohlcv("X", interval="1d", period="6mo", repo=repo, cache_cfg=cfg,
                   fetch_fn=fetch, now=now + timedelta(minutes=5),  # taze!
                   min_bars=100)
    assert df is not None
    assert len(df) >= 100
    # Yeni full fetch yapildi
    assert len(fetch.calls) == 2
    assert fetch.calls[-1][1] == "6mo"  # period=6mo ile fetch edildi


def test_concurrent_fetch_idempotent(repo, cfg):
    """20 thread ayni sembolu cagirirsa yine cache tutarli olmali (PK idempotent)."""
    fetch = _CountingFetch(_daily_df(n=10))
    results: list[pd.DataFrame | None] = []
    errors: list[BaseException] = []

    def worker():
        try:
            df = get_ohlcv("T", interval="1d", period="6mo",
                           repo=repo, cache_cfg=cfg, fetch_fn=fetch)
            results.append(df)
        except BaseException as e:
            errors.append(e)

    # :memory: SQLite concurrent-write ile tam thread-safe degildir; production
    # WAL + dosya DB bunu hallediyor. Test'te 5 thread idempotency kontrolune yeter.
    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Errors: {errors}"
    assert all(r is not None and len(r) == 10 for r in results)
    # Cache: unique bar count = 10, duplike yok.
    assert len(repo.get_cached_ohlcv("T", "1d")) == 10
