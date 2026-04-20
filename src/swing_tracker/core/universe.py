"""Likidite tabanli evren kurucu.

XTUMY (tum BIST ~467 sembol) icinden, sembol basina:
- KAP'tan pazar segmenti cekilir (7 gun TTL cache)
- Gunluk OHLCV cache'den son 20 barin TL hacmi hesaplanir (medyan)
- Filtreden gecenler liquid_universe tablosuna yazilir

Gunluk bir kez (config.liquidity.build_time) calisir. Quick/deep scan bu
tablodan evren cekmek icin get_liquid_symbols() cagirir; tablo bossa
config.liquidity.fallback_universe'a duser.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import borsapy as bp

from swing_tracker.config import Config
from swing_tracker.core.ohlcv_cache import get_ohlcv
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)

UNKNOWN_MARKET = "UNKNOWN"


def _fetch_kap_market(symbol: str) -> tuple[str | None, str | None]:
    """Pazar + sektor bilgisi. KAP provider cagrisi."""
    try:
        from borsapy._providers.kap import get_kap_provider
        kap = get_kap_provider()
        details = kap.get_company_details(symbol)
        if not isinstance(details, dict):
            return (None, None)
        return (details.get("market"), details.get("sector"))
    except Exception:
        logger.debug(f"{symbol}: KAP fetch hatasi", exc_info=True)
        return (None, None)


def _fetch_market_cap(symbol: str) -> float | None:
    try:
        info = bp.Ticker(symbol).info
        cap = info.get("marketCap") if info else None
        return float(cap) if cap is not None else None
    except Exception:
        return None


class UniverseBuilder:
    def __init__(self, repo: Repository, config: Config):
        self._repo = repo
        self._config = config
        workers = max(1, int(config.liquidity.builder_max_workers))
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="universe"
        )

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ── Market cache (KAP, 7 gun TTL) ──

    def _get_market_info(self, symbol: str, now: datetime) -> tuple[str | None, str | None]:
        ttl_days = self._config.liquidity.market_cache_ttl_days
        cached = self._repo.get_symbol_market(symbol)
        if cached is not None:
            try:
                fetched = datetime.fromisoformat(cached["fetched_at"])
            except (ValueError, TypeError, KeyError):
                fetched = datetime.min
            if (now - fetched) < timedelta(days=ttl_days):
                return (cached.get("market"), cached.get("sector"))

        market, sector = _fetch_kap_market(symbol)
        self._repo.upsert_symbol_market(
            symbol=symbol,
            market=market,
            sector=sector,
            fetched_at=now.isoformat(timespec="seconds"),
        )
        return (market, sector)

    # ── Likidite hesabi ──

    def _compute_liquidity(self, symbol: str) -> dict | None:
        """Sembol icin medyan TL hacim + son kapanis + gun sayisi."""
        df = get_ohlcv(
            symbol,
            interval="1d",
            period="1mo",
            repo=self._repo,
            cache_cfg=self._config.cache,
        )
        if df is None or df.empty:
            return None
        tail = df.tail(20)
        # TL hacim = Volume (lot) * Close (fiyat)
        volume_tl = (tail["Volume"] * tail["Close"]).dropna()
        if len(volume_tl) == 0:
            return None
        return {
            "median_volume_tl": float(volume_tl.median()),
            "volume_days": int(len(volume_tl)),
            "last_close": float(tail["Close"].iloc[-1]) if not tail["Close"].empty else None,
        }

    def _evaluate_symbol(self, symbol: str, now: datetime) -> dict | None:
        """Tek sembol icin tam degerlendirme. None = veri yetersiz."""
        liq = self._compute_liquidity(symbol)
        if liq is None:
            return None
        market, _sector = self._get_market_info(symbol, now)
        market_cap = _fetch_market_cap(symbol)
        return {
            "symbol": symbol,
            "market": market or UNKNOWN_MARKET,
            "median_volume_tl": liq["median_volume_tl"],
            "volume_days": liq["volume_days"],
            "last_close": liq["last_close"],
            "market_cap_tl": market_cap,
        }

    # ── Filtre ──

    def _passes_filter(self, row: dict) -> bool:
        cfg = self._config.liquidity
        if row["volume_days"] < cfg.min_volume_days:
            return False
        if row["median_volume_tl"] < cfg.min_daily_volume_tl:
            return False
        if row["market"] in cfg.excluded_markets:
            return False
        return True

    # ── Public API ──

    def build(self, universe: str | None = None) -> tuple[int, int]:
        """Tum evreni tara, filtre et, liquid_universe'e yaz.

        Donus: (aday_sayisi, filtreli_sayisi)
        """
        target_universe = universe or self._config.scanner.universe
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        try:
            idx = bp.Index(target_universe)
            components = idx.components
        except Exception:
            logger.exception(f"Universe yuklenemedi: {target_universe}")
            return (0, 0)

        if isinstance(components, list):
            symbols = [
                c["symbol"] if isinstance(c, dict) else str(c) for c in components
            ]
        else:
            logger.error(f"Universe bilesenleri beklenmeyen format: {type(components)}")
            return (0, 0)

        logger.info(f"Universe build basliyor: {target_universe} ({len(symbols)} aday)")

        rows = list(
            self._executor.map(lambda s: self._evaluate_symbol(s, now), symbols)
        )
        rows = [r for r in rows if r is not None]

        kept_rows = [r for r in rows if self._passes_filter(r)]
        for r in kept_rows:
            self._repo.upsert_liquid_symbol(
                symbol=r["symbol"],
                market=r["market"],
                median_volume_tl=r["median_volume_tl"],
                volume_days=r["volume_days"],
                last_close=r["last_close"],
                market_cap_tl=r["market_cap_tl"],
            )

        kept_symbols = [r["symbol"] for r in kept_rows]
        removed = self._repo.delete_liquid_symbols_not_in(kept_symbols)

        logger.info(
            f"Universe build tamamlandi: {len(symbols)} aday → "
            f"{len(kept_rows)} likit ({100*len(kept_rows)/max(1,len(symbols)):.0f}%), "
            f"{removed} eski sembol temizlendi"
        )
        return (len(symbols), len(kept_rows))

    def get_liquid_symbols(self) -> list[str]:
        """Filtre edilmis likit semboller. Bossa fallback evrene dus."""
        rows = self._repo.get_liquid_symbols()
        if rows:
            return [r["symbol"] for r in rows]

        fallback = self._config.liquidity.fallback_universe
        logger.warning(
            f"liquid_universe tablosu bos, fallback evrene dusuyorum: {fallback}"
        )
        try:
            components = bp.Index(fallback).components
            if isinstance(components, list):
                return [
                    c["symbol"] if isinstance(c, dict) else str(c) for c in components
                ]
        except Exception:
            logger.exception(f"Fallback evren yuklenemedi: {fallback}")
        return []

    def get_symbol_info(self, symbol: str) -> dict | None:
        """Telegram/UI icin sembol metadata'si."""
        rows = self._repo.get_liquid_symbols()
        for r in rows:
            if r["symbol"] == symbol:
                return r
        return None
