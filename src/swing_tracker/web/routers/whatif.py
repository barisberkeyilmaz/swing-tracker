"""What-if router — sinyaller alinsaydi performans simulasyonu."""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from swing_tracker.backtest.runner import parse_config_from_toml
from swing_tracker.core.ohlcv_cache import get_ohlcv
from swing_tracker.core.scanner import MIN_ENTRY_SCORE
from swing_tracker.core.whatif import WhatIfStats, WhatIfTrade, compute_stats, simulate_whatif
from swing_tracker.web.dependencies import get_config, get_repo, templates
from swing_tracker.web.helpers import localize_signal_timestamps
from swing_tracker.web.price_cache import price_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatif")

# Simulasyon icin veri pencereleri
_DAILY_PERIOD = "1y"
_HOURLY_PERIOD = "3mo"


def build_whatif_data(repo, config) -> tuple[list[WhatIfTrade], WhatIfStats]:
    """Sinyalleri cek, OHLCV + guncel fiyatlari topla, simulasyonu kostur. Sync/blocking."""
    signals = repo.get_buy_signals_asc(min_score=MIN_ENTRY_SCORE)
    symbols = list(dict.fromkeys(s["symbol"] for s in signals))

    ohlcv_1h = {}
    ohlcv_1d = {}
    for sym in symbols:
        try:
            ohlcv_1h[sym] = get_ohlcv(
                sym, interval="1h", period=_HOURLY_PERIOD,
                repo=repo, cache_cfg=config.cache,
            )
        except Exception:
            logger.warning("whatif: 1h veri alinamadi: %s", sym, exc_info=True)
            ohlcv_1h[sym] = None
        try:
            ohlcv_1d[sym] = get_ohlcv(
                sym, interval="1d", period=_DAILY_PERIOD,
                repo=repo, cache_cfg=config.cache,
            )
        except Exception:
            logger.warning("whatif: 1d veri alinamadi: %s", sym, exc_info=True)
            ohlcv_1d[sym] = None

    current_prices = price_cache.fetch_many(symbols)
    # Komisyon sifirlanir: sanal islemlerde yuzde getiri olculur (spec karari)
    bt_config = dataclasses.replace(
        parse_config_from_toml(), commission_pct=0.0, commission_fixed=0.0
    )

    trades, skipped = simulate_whatif(signals, ohlcv_1h, ohlcv_1d, current_prices, bt_config)
    stats = compute_stats(trades, skipped)
    return trades, stats


@router.get("", response_class=HTMLResponse)
async def whatif_page(request: Request):
    """Skeleton sayfa — hesaplama yok, fragment htmx ile yuklenir."""
    return templates.TemplateResponse(request, "whatif.html", context={})


@router.get("/results", response_class=HTMLResponse)
async def whatif_results(request: Request):
    """Simulasyonu kosturup sonuc fragment'ini dondurur."""
    repo = get_repo()
    config = get_config()

    trades, stats = await asyncio.to_thread(build_whatif_data, repo, config)

    # Sinyal saatlerini goruntuleme icin Istanbul'a cevir
    display = []
    for t in trades:
        d = t.__dict__.copy()
        d["signal_time_local"] = localize_signal_timestamps(
            [{"created_at": t.signal_time}], config.timezone
        )[0]["created_at"]
        display.append(d)

    # En yeni sinyal ustte
    display.sort(key=lambda d: d["signal_time"], reverse=True)

    return templates.TemplateResponse(
        request,
        "fragments/whatif_results.html",
        context={"trades": display, "stats": stats},
    )
