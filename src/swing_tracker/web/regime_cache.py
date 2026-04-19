"""TTL cache for market regime check (XU100 vs SMA).

borsapy ile endeks verisi cekmek pahalı (~1-2s), dashboard her acilista
cagirmayalim. 15 dakikalik TTL piyasa rejim degisimi icin yeterli.
"""

from __future__ import annotations

import logging
import threading
import time

from swing_tracker.core.scanner import Scanner
from swing_tracker.web.dependencies import get_config, get_repo

logger = logging.getLogger(__name__)

TTL = 900  # 15 dakika

_lock = threading.Lock()
_cache: dict = {"value": None, "ts": 0.0}


def get_market_regime() -> bool | None:
    """True=boga, False=ayi, None=bilinmiyor (hata/veri yok). 15 dk cache."""
    now = time.monotonic()
    with _lock:
        if _cache["value"] is not None and (now - _cache["ts"]) < TTL:
            return _cache["value"]

    try:
        scanner = Scanner(get_repo(), get_config())
        is_bull = scanner.check_market_regime()
    except Exception:
        logger.warning("Piyasa rejim kontrolu hatasi", exc_info=True)
        return _cache["value"]  # stale varsa onu don

    with _lock:
        _cache["value"] = is_bull
        _cache["ts"] = now
    return is_bull
