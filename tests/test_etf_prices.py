from swing_tracker.core import etf_prices


def test_fetch_etf_prices_maps_symbol_to_price(monkeypatch):
    calls = []

    class FakeProvider:
        def get_quote(self, symbol, exchange="BIST"):
            calls.append((symbol, exchange))
            return {"VOO": {"last": 682.24}, "VXUS": {"last": 83.9}}[symbol]

    monkeypatch.setattr(etf_prices, "get_tradingview_provider", lambda: FakeProvider())
    cache = etf_prices.EtfPriceCache()
    out = cache.fetch_many({"VOO": "AMEX", "VXUS": "NASDAQ"})
    assert out == {"VOO": 682.24, "VXUS": 83.9}
    assert ("VOO", "AMEX") in calls and ("VXUS", "NASDAQ") in calls


def test_fetch_skips_failing_symbol(monkeypatch):
    class FakeProvider:
        def get_quote(self, symbol, exchange="BIST"):
            if symbol == "BAD":
                raise RuntimeError("no data")
            return {"last": 100.0}

    monkeypatch.setattr(etf_prices, "get_tradingview_provider", lambda: FakeProvider())
    cache = etf_prices.EtfPriceCache()
    out = cache.fetch_many({"VOO": "AMEX", "BAD": "AMEX"})
    assert out == {"VOO": 100.0}


def test_cache_hit_within_ttl(monkeypatch):
    n = {"count": 0}

    class FakeProvider:
        def get_quote(self, symbol, exchange="BIST"):
            n["count"] += 1
            return {"last": 50.0}

    monkeypatch.setattr(etf_prices, "get_tradingview_provider", lambda: FakeProvider())
    cache = etf_prices.EtfPriceCache()
    cache.fetch_many({"XLE": "AMEX"})
    cache.fetch_many({"XLE": "AMEX"})
    assert n["count"] == 1  # ikinci cagri cache'ten
