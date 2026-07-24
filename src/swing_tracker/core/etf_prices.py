"""US ETF fiyat katmani — TradingView (exchange destekli) + USDTRY.

Mevcut web/price_cache.py deseni (TTL + LRU + paralel fetch) ile ayni,
fark: BIST'e sabit bp.Ticker yerine exchange parametreli get_quote kullanir.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import borsapy as bp
from borsapy._providers.tradingview import get_tradingview_provider

logger = logging.getLogger(__name__)

TTL = 300  # saniye — ETF fiyatlari icin 5 dk
USDTRY_TTL = 300
MAX_SIZE = 200
MAX_WORKERS = 5


class EtfPriceCache:
    def __init__(self, max_size: int = MAX_SIZE):
        self._cache: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size
        self._usdtry: tuple[float, float] | None = None

    def _get(self, symbol: str) -> float | None:
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

    def fetch_one(self, symbol: str, exchange: str) -> float | None:
        cached = self._get(symbol)
        if cached is not None:
            return cached
        try:
            quote = get_tradingview_provider().get_quote(symbol, exchange=exchange)
            price = float(quote.get("last") or 0)
            if price <= 0:
                logger.warning("ETF fiyati alinamadi: %s:%s", exchange, symbol)
                return None
            self._set(symbol, price)
            return price
        except Exception:
            logger.warning("ETF fiyat cekme hatasi: %s:%s", exchange, symbol, exc_info=True)
            return None

    def fetch_many(
        self, symbol_exchange: dict[str, str], max_workers: int = MAX_WORKERS
    ) -> dict[str, float]:
        if not symbol_exchange:
            return {}
        items = list(symbol_exchange.items())
        workers = min(max_workers, len(items))
        result: dict[str, float] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            prices = pool.map(lambda p: self.fetch_one(p[0], p[1]), items)
            for (symbol, _exchange), price in zip(items, prices):
                if price is not None:
                    result[symbol] = price
        return result

    def fetch_usdtry(self) -> float | None:
        if self._usdtry and (time.monotonic() - self._usdtry[1]) < USDTRY_TTL:
            return self._usdtry[0]
        try:
            fx = bp.FX("USD")
            info = getattr(fx, "info", None) or {}
            rate = float(info.get("last") or 0)
            if rate <= 0:
                return None
            self._usdtry = (rate, time.monotonic())
            return rate
        except Exception:
            logger.warning("USDTRY cekme hatasi", exc_info=True)
            return None


etf_price_cache = EtfPriceCache()
