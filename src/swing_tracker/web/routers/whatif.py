"""What-if router — kalici whatif_trades tablosundan okur; simulasyon yapmaz.

Hesaplama sinyal aninda (scanner hook) ve gunluk 18:40 job'unda yasar
(core/whatif_store.py). Burada yalnizca: DB okumasi, acik pozisyonlara canli
fiyat, dedup gorunum filtresi ve istatistik toplama.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from swing_tracker.core.whatif import (
    VIRTUAL_SHARES,
    WhatIfStats,
    WhatIfTrade,
    compute_stats,
    dedup_filter,
)
from swing_tracker.web.dependencies import get_config, get_repo, templates
from swing_tracker.web.helpers import localize_signal_timestamps
from swing_tracker.web.price_cache import price_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatif")


def row_to_trade(row: dict) -> WhatIfTrade:
    """whatif_trades satirini template/istatistik modeli WhatIfTrade'e cevir."""
    return WhatIfTrade(
        signal_id=row["signal_id"],
        symbol=row["symbol"],
        signal_time=row["signal_time"],
        score=row["score"],
        price_at_signal=row["price_at_signal"],
        entry_price=row["entry_price"] or 0.0,
        entry_source=row["entry_source"] or "fallback",
        stop_loss=row["stop_loss"] or 0.0,
        tp1=row["tp1"] or 0.0,
        tp2=row["tp2"] or 0.0,
        status=row["status"],
        strategy_pnl_pct=row["strategy_pnl_pct"],
        exit_type=row["exit_type"],
        exit_date=row["exit_date"],
        holding_days=row["holding_days"],
        buyhold_pnl_pct=row["buyhold_pnl_pct"],
        current_price=row["last_close"],
        delay_cost_pct=row["delay_cost_pct"],
    )


def build_whatif_data(repo, mode: str = "takip") -> tuple[list[WhatIfTrade], WhatIfStats]:
    """DB'den oku; yalnizca acik pozisyonlara canli fiyat uygula. Simulasyon yok."""
    rows = repo.get_whatif_trades()

    open_symbols = sorted({r["symbol"] for r in rows if r["status"] == "open"})
    live = price_cache.fetch_many(open_symbols) if open_symbols else {}

    trades: list[WhatIfTrade] = []
    for row in rows:
        trade = row_to_trade(row)
        price = live.get(row["symbol"])
        if row["status"] == "open" and price is not None and row["entry_price"]:
            cost = row["entry_price"] * VIRTUAL_SHARES
            unrealized = (price - row["entry_price"]) * (row["remaining_shares"] or 0)
            trade.strategy_pnl_pct = round(
                ((row["realized_pnl"] or 0.0) + unrealized) / cost * 100, 2
            )
            trade.buyhold_pnl_pct = round(
                (price - row["entry_price"]) / row["entry_price"] * 100, 2
            )
            trade.current_price = price
        trades.append(trade)

    if mode == "tum":
        stats = compute_stats(trades, skipped_dedup=0)
        return trades, stats

    kept, skipped = dedup_filter(trades)
    stats = compute_stats(kept, skipped_dedup=skipped)
    return kept, stats


@router.get("", response_class=HTMLResponse)
async def whatif_page(request: Request, mode: str = Query("takip")):
    """Skeleton sayfa — fragment htmx ile yuklenir."""
    if mode not in ("takip", "tum"):
        mode = "takip"
    return templates.TemplateResponse(request, "whatif.html", context={"mode": mode})


@router.get("/results", response_class=HTMLResponse)
async def whatif_results(request: Request, mode: str = Query("takip")):
    repo = get_repo()
    config = get_config()
    if mode not in ("takip", "tum"):
        mode = "takip"

    try:
        trades, stats = await asyncio.to_thread(build_whatif_data, repo, mode)
    except Exception:
        logger.exception("whatif: sonuc olusturulamadi")
        return HTMLResponse(
            '<div class="bg-surface-raised border border-border rounded-xl p-8 '
            'text-center text-txt-muted">Sonuclar yuklenemedi. '
            'Sayfayi yenileyip tekrar deneyin.</div>'
        )

    display = []
    for t in trades:
        d = t.__dict__.copy()
        d["signal_time_local"] = localize_signal_timestamps(
            [{"created_at": t.signal_time}], config.timezone
        )[0]["created_at"]
        display.append(d)
    display.sort(key=lambda d: d["signal_time"], reverse=True)

    return templates.TemplateResponse(
        request,
        "fragments/whatif_results.html",
        context={"trades": display, "stats": stats, "mode": mode},
    )
