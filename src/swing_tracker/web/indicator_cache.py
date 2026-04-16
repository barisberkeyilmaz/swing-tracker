"""In-memory indicator cache with TTL for symbol detail page.

Caches `_technical_summary` output so repeat visits to the same symbol
within TTL skip the heavy indicator math (RSI/MACD/Stochastic/Bollinger/SMA).
Thread-safe for asyncio.to_thread usage.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

TTL = 300  # 5 dk — teknik gostergeler sembol detay sayfasinda yeterince tazelik saglar
MAX_SIZE = 200


class IndicatorCache:
    def __init__(self, max_size: int = MAX_SIZE, ttl: int = TTL):
        self._cache: OrderedDict[str, tuple[dict, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._ttl = ttl

    def get(self, symbol: str) -> dict | None:
        with self._lock:
            entry = self._cache.get(symbol)
            if entry and (time.monotonic() - entry[1]) < self._ttl:
                self._cache.move_to_end(symbol)
                return entry[0]
        return None

    def set(self, symbol: str, summary: dict) -> None:
        with self._lock:
            self._cache[symbol] = (summary, time.monotonic())
            self._cache.move_to_end(symbol)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)


# Module-level singleton
indicator_cache = IndicatorCache()
