"""In-memory price cache with TTL for live price display.

Caches borsapy price lookups so repeated page loads within TTL
don't re-fetch from the API.  Thread-safe for asyncio.to_thread usage.
"""

from __future__ import annotations

import logging
import threading
import time

import borsapy as bp

logger = logging.getLogger(__name__)

TTL = 60  # seconds — matches the 60s auto-refresh in base.html


class PriceCache:
    def __init__(self):
        self._cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
        self._lock = threading.Lock()

    def get(self, symbol: str) -> float | None:
        """Return cached price if fresh, else None."""
        with self._lock:
            entry = self._cache.get(symbol)
            if entry and (time.monotonic() - entry[1]) < TTL:
                return entry[0]
        return None

    def _set(self, symbol: str, price: float) -> None:
        with self._lock:
            self._cache[symbol] = (price, time.monotonic())

    def fetch_one(self, symbol: str) -> float | None:
        """Fetch price for a single symbol, using cache if fresh."""
        cached = self.get(symbol)
        if cached is not None:
            return cached

        try:
            ticker = bp.Ticker(symbol)
            df = ticker.history(period="5d", interval="1d")
            if df is None or len(df) == 0:
                logger.warning("Fiyat alinamadi: %s", symbol)
                return None
            price = float(df.iloc[-1]["Close"])
            if price <= 0:
                return None
            self._set(symbol, price)
            return price
        except Exception:
            logger.warning("Fiyat cekme hatasi: %s", symbol, exc_info=True)
            return None

    def fetch_many(self, symbols: list[str]) -> dict[str, float]:
        """Fetch prices for multiple symbols, returns {symbol: price}."""
        result: dict[str, float] = {}
        for symbol in symbols:
            price = self.fetch_one(symbol)
            if price is not None:
                result[symbol] = price
        return result


# Module-level singleton
price_cache = PriceCache()
