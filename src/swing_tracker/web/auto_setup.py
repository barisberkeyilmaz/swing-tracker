"""ATR tabanli SL/TP hesap yardimcisi.

Manuel Alis ve Sinyal alim modallerinde otomatik TP/SL doldurmak icin.
ATR hesabi borsapy'den 3 aylik veri cekip 14 periyotluk ATR uretir,
10 dakikalik cache ile tekrarlanan cagirilar hizli.
"""

from __future__ import annotations

import logging
import threading
import time

import borsapy as bp

from swing_tracker.core.strategy import get_strategy, get_strategy_params
from swing_tracker.web.dependencies import get_config
from swing_tracker.web.price_cache import price_cache

logger = logging.getLogger(__name__)

TTL = 600  # 10 dakika

_lock = threading.Lock()
_cache: dict[str, tuple[float, float]] = {}  # symbol -> (atr, monotonic ts)


def _get_atr(symbol: str) -> float | None:
    """ATR-14 degerini dondurur, 10 dk cache'li."""
    now = time.monotonic()
    with _lock:
        entry = _cache.get(symbol)
        if entry and (now - entry[1]) < TTL:
            return entry[0]

    try:
        df = bp.Ticker(symbol).history(period="3mo", interval="1d")
    except Exception:
        logger.warning("ATR icin veri cekilemedi: %s", symbol, exc_info=True)
        return None

    if df is None or len(df) < 14:
        return None

    try:
        df = bp.add_indicators(df, indicators=["atr"])
    except Exception:
        logger.warning("ATR indikator hatasi: %s", symbol, exc_info=True)
        return None

    last = df.iloc[-1]
    atr: float | None = None
    for key in ("ATR", "ATR_14", "atr", "atr_14"):
        if key in df.columns:
            try:
                val = float(last[key])
                if val > 0:
                    atr = val
                    break
            except (ValueError, TypeError):
                continue

    if atr is None:
        return None

    with _lock:
        _cache[symbol] = (atr, now)
    return atr


def compute_setup(symbol: str, price: float | None = None) -> dict | None:
    """ATR tabanli SL/TP seviyeleri hesapla.

    price verilmezse son Close kullanilir (price_cache uzerinden).
    Dondurur: {entry, sl, tp1, tp2, tp3, atr, rr} veya None.
    """
    symbol = symbol.strip().upper()
    if not symbol:
        return None

    atr = _get_atr(symbol)
    if atr is None:
        return None

    entry_price = price
    if entry_price is None or entry_price <= 0:
        entry_price = price_cache.fetch_one(symbol)
        if entry_price is None:
            return None

    strategy = get_strategy(get_config())
    params = get_strategy_params(strategy)
    sl_mult = float(params.get("sl_atr_mult", 1.5))
    tp1_mult = float(params.get("tp1_atr_mult", 1.5))
    tp2_mult = float(params.get("tp2_atr_mult", 3.0))
    tp3_mult = float(params.get("tp3_atr_mult", 4.5))

    sl = round(entry_price - atr * sl_mult, 2)
    tp1 = round(entry_price + atr * tp1_mult, 2)
    tp2 = round(entry_price + atr * tp2_mult, 2)
    tp3 = round(entry_price + atr * tp3_mult, 2)

    risk = entry_price - sl
    reward = tp1 - entry_price
    rr = round(reward / risk, 2) if risk > 0 else 0.0

    return {
        "entry": round(entry_price, 2),
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "atr": round(atr, 3),
        "rr": rr,
    }
