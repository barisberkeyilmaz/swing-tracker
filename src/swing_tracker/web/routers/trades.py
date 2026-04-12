"""Trade detail router — single trade with exit history."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from swing_tracker.web.dependencies import templates, get_repo, get_config

router = APIRouter(prefix="/trades")


@router.get("/{trade_id}", response_class=HTMLResponse)
async def trade_detail(request: Request, trade_id: int):
    repo = get_repo()
    config = get_config()

    trade = repo.get_trade(trade_id)
    if not trade:
        return HTMLResponse("<h1>Trade bulunamadi</h1>", status_code=404)

    if trade.get("entry_reasons"):
        try:
            trade["entry_reasons"] = json.loads(trade["entry_reasons"])
        except (json.JSONDecodeError, TypeError):
            pass

    exits = repo.get_trade_exits(trade_id)
    exited_shares = sum(e["shares"] for e in exits)
    remaining_shares = trade.get("shares", 0) - exited_shares
    realized_pnl = sum(e.get("pnl", 0) or 0 for e in exits)

    # Sinyal gecmisi
    signals = repo.get_recent_signals(limit=50)
    trade_signals = [
        s for s in signals
        if s.get("symbol") == trade.get("symbol")
    ][:5]
    for sig in trade_signals:
        if sig.get("indicator_values"):
            try:
                sig["indicator_values"] = json.loads(sig["indicator_values"])
            except (json.JSONDecodeError, TypeError):
                pass

    now = datetime.now(config.timezone)

    return templates.TemplateResponse(
        request,
        "trade_detail.html",
        context={
            "trade": trade,
            "exits": exits,
            "remaining_shares": remaining_shares,
            "exited_shares": exited_shares,
            "realized_pnl": realized_pnl,
            "trade_signals": trade_signals,
            "now": now,
        },
    )


@router.post("/{trade_id}/delete")
async def delete_trade(trade_id: int):
    """Trade sil — exit'ler ve ilgili nakit islemleri de silinir."""
    repo = get_repo()
    repo.delete_trade(trade_id)
    return RedirectResponse(url="/", status_code=303)
