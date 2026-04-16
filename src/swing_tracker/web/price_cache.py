"""In-memory price cache with TTL for live price display.

Caches borsapy price lookups so repeated page loads within TTL
don't re-fetch from the API.  Thread-safe for asyncio.to_thread usage.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import borsapy as bp

logger = logging.getLogger(__name__)

TTL = 60  # seconds — matches the 60s auto-refresh in base.html
MAX_SIZE = 500  # LRU capacity — prevents unbounded memory growth
MAX_WORKERS = 10  # borsapy paralel fetch worker sayisi


class PriceCache:
    def __init__(self, max_size: int = MAX_SIZE):
        self._cache: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size

    def get(self, symbol: str) -> float | None:
        """Return cached price if fresh, else None. Refreshes LRU order on hit."""
        with self._lock:
            entry = self._cache.get(symbol)
            if entry and (time.monotonic() - entry[1]) < TTL:
                self._cache.move_to_end(symbol)
                return entry[0]
        return None

    def _set(self, symbol: str, price: float) -> None:
        with self._lock:
            self._cache[symbol] = (price, time.monotonic())
            self._cache.move_to_end(symbol)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

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

    def fetch_many(
        self, symbols: list[str], max_workers: int = MAX_WORKERS
    ) -> dict[str, float]:
        """Fetch prices for multiple symbols in parallel, returns {symbol: price}."""
        if not symbols:
            return {}

        unique = list(dict.fromkeys(symbols))  # sirayi koru, duplicate'i ele
        workers = min(max_workers, len(unique))

        result: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for symbol, price in zip(unique, pool.map(self.fetch_one, unique)):
                if price is not None:
                    result[symbol] = price
        return result


# Module-level singleton
price_cache = PriceCache()
