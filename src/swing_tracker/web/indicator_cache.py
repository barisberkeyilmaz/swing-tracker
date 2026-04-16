"""In-memory caches with TTL for symbol detail page.

- IndicatorCache: `_technical_summary` dict output (RSI/MACD/SMA/Bollinger)
- HistoryCache: borsapy OHLCV DataFrame — technical-chart ve chart-data
  fragment'leri bu cache'i paylaşır, ikinci call fetch yapmaz.

Thread-safe for asyncio.to_thread usage.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

TTL = 300  # 5 dk
MAX_SIZE = 200


class _BaseCache:
    """OrderedDict + TTL + LRU eviction. Thread-safe."""

    def __init__(self, max_size: int = MAX_SIZE, ttl: int = TTL):
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._ttl = ttl

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry and (time.monotonic() - entry[1]) < self._ttl:
                self._cache.move_to_end(key)
                return entry[0]
        return None

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._cache[key] = (value, time.monotonic())
            self._cache.move_to_end(key)
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)


class IndicatorCache(_BaseCache):
    """Sembol bazinda _technical_summary dict cache."""
    pass


class HistoryCache(_BaseCache):
    """Sembol bazinda borsapy OHLCV DataFrame cache."""
    pass


class InfoCache(_BaseCache):
    """Sembol bazinda borsapy ticker.info dict cache."""
    pass


# Module-level singletons
indicator_cache = IndicatorCache()
history_cache = HistoryCache(max_size=100)
info_cache = InfoCache(max_size=200, ttl=60)  # fiyat guncelligine uygun kisa TTL
