from __future__ import annotations

import time

import pandas as pd
import pytest

from swing_tracker.web import price_cache as pc_module
from swing_tracker.web.indicator_cache import HistoryCache, IndicatorCache
from swing_tracker.web.price_cache import PriceCache


class _MockTicker:
    """borsapy.Ticker mock — configurable fetch latency and price."""

    delay_s: float = 0.0
    prices: dict[str, float] = {}

    def __init__(self, symbol: str):
        self.symbol = symbol

    def history(self, period: str, interval: str):
        if self.delay_s:
            time.sleep(self.delay_s)
        price = self.prices.get(self.symbol, 100.0)
        return pd.DataFrame([{"Close": price}])


@pytest.fixture
def mock_bp(monkeypatch):
    """bp.Ticker'i mock'a cevir, delay/price konfigure edilebilir."""
    _MockTicker.delay_s = 0.0
    _MockTicker.prices = {}
    monkeypatch.setattr(pc_module.bp, "Ticker", _MockTicker)
    return _MockTicker


def test_fetch_one_caches_result(mock_bp):
    mock_bp.prices = {"THYAO": 250.0}
    cache = PriceCache()

    assert cache.fetch_one("THYAO") == 250.0
    # Mock price degistirsek bile cache hit oldugundan eski deger dondu
    mock_bp.prices = {"THYAO": 999.0}
    assert cache.fetch_one("THYAO") == 250.0


def test_fetch_many_runs_in_parallel(mock_bp):
    """5 sembol × 100ms seri olsa 500ms; paralelde 10 worker ile <250ms olmali."""
    mock_bp.delay_s = 0.1
    mock_bp.prices = {s: 10.0 for s in ("AAA", "BBB", "CCC", "DDD", "EEE")}
    cache = PriceCache()

    start = time.monotonic()
    result = cache.fetch_many(list(mock_bp.prices))
    elapsed = time.monotonic() - start

    assert len(result) == 5
    assert elapsed < 0.25, f"fetch_many paralel degil, {elapsed:.2f}s surdu"


def test_lru_evicts_oldest_entries(mock_bp):
    # Not: price_cache 0 veya negatif fiyati kabul etmiyor (>0 zorunlu)
    mock_bp.prices = {f"S{i}": float(i + 1) for i in range(5)}
    cache = PriceCache(max_size=3)

    for sym in mock_bp.prices:
        cache.fetch_one(sym)

    # Cache sadece son 3 sembolu tutmali
    assert len(cache._cache) == 3
    assert cache.get("S0") is None
    assert cache.get("S1") is None
    assert cache.get("S4") == 5.0


def test_lru_refresh_on_hit(mock_bp):
    """Get ile hit olan sembol en yeni konuma tasinir; eviction'da atilmaz."""
    mock_bp.prices = {f"S{i}": float(i + 1) for i in range(4)}
    cache = PriceCache(max_size=3)

    for sym in ("S0", "S1", "S2"):
        cache.fetch_one(sym)
    # S0'a hit at — en yeni olmali
    cache.get("S0")
    # Yeni sembol ekle — eviction S1'i atmali (S0 degil)
    cache.fetch_one("S3")

    assert cache.get("S0") == 1.0
    assert cache.get("S1") is None
    assert cache.get("S2") == 3.0
    assert cache.get("S3") == 4.0


def test_ttl_expires_entry(mock_bp, monkeypatch):
    mock_bp.prices = {"THYAO": 250.0}
    cache = PriceCache()
    cache.fetch_one("THYAO")

    # time.monotonic'i ileri sar
    original = time.monotonic()
    monkeypatch.setattr(
        pc_module.time, "monotonic", lambda: original + pc_module.TTL + 1
    )

    assert cache.get("THYAO") is None


def test_fetch_many_ignores_duplicates(mock_bp):
    mock_bp.prices = {"THYAO": 250.0, "ASELS": 180.0}
    cache = PriceCache()

    result = cache.fetch_many(["THYAO", "ASELS", "THYAO", "ASELS"])

    assert result == {"THYAO": 250.0, "ASELS": 180.0}


# ─── IndicatorCache ───

def test_indicator_cache_stores_and_retrieves():
    cache = IndicatorCache(max_size=10, ttl=60)
    summary = {"rsi": 55.0, "macd": 0.1}

    cache.set("THYAO", summary)
    assert cache.get("THYAO") == summary


def test_indicator_cache_evicts_oldest():
    cache = IndicatorCache(max_size=2, ttl=60)
    cache.set("AAA", {"rsi": 1})
    cache.set("BBB", {"rsi": 2})
    cache.set("CCC", {"rsi": 3})

    assert cache.get("AAA") is None
    assert cache.get("BBB") == {"rsi": 2}
    assert cache.get("CCC") == {"rsi": 3}


def test_indicator_cache_ttl_expires(monkeypatch):
    from swing_tracker.web import indicator_cache as ic_module

    cache = IndicatorCache(max_size=10, ttl=60)
    cache.set("THYAO", {"rsi": 55.0})

    original = time.monotonic()
    monkeypatch.setattr(ic_module.time, "monotonic", lambda: original + 61)

    assert cache.get("THYAO") is None


# ─── HistoryCache ───

def test_history_cache_stores_dataframe():
    cache = HistoryCache(max_size=10, ttl=60)
    df = pd.DataFrame({"Close": [100.0, 101.0], "Volume": [1000, 2000]})

    cache.set("THYAO", df)
    result = cache.get("THYAO")

    assert result is not None
    assert len(result) == 2
    assert float(result.iloc[-1]["Close"]) == 101.0


def test_history_cache_evicts_oldest():
    cache = HistoryCache(max_size=2, ttl=60)
    for sym in ("AAA", "BBB", "CCC"):
        cache.set(sym, pd.DataFrame({"Close": [1.0]}))

    assert cache.get("AAA") is None
    assert cache.get("BBB") is not None
    assert cache.get("CCC") is not None
