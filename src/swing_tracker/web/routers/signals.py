"""Signals router — signal history list + buy from signal."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from swing_tracker.web.dependencies import templates, get_repo, get_config

router = APIRouter(prefix="/signals")


@router.get("", response_class=HTMLResponse)
async def signals_list(request: Request):
    repo = get_repo()
    config = get_config()

    recent_signals = repo.get_recent_signals(limit=50)
    for sig in recent_signals:
        if sig.get("indicator_values"):
            try:
                sig["indicator_values"] = json.loads(sig["indicator_values"])
            except (json.JSONDecodeError, TypeError):
                pass

    now = datetime.now(config.timezone)

    return templates.TemplateResponse(
        request,
        "signals.html",
        context={
            "signals": recent_signals,
            "now": now,
        },
    )


@router.post("/buy")
async def buy_from_signal(
    symbol: str = Form(...),
    entry_price: float = Form(...),
    shares: int = Form(...),
    signal_score: int = Form(0),
    reasons: str = Form(""),
):
    """Sinyalden pozisyon ac ve nakitten dus."""
    repo = get_repo()
    today = datetime.now().strftime("%Y-%m-%d %H:%M")

    reason_list = [r.strip() for r in reasons.split(",") if r.strip()]

    trade_id = repo.create_trade(
        symbol=symbol,
        direction="long",
        status="open",
        entry_price=entry_price,
        entry_date=today,
        shares=shares,
        signal_score=signal_score,
        entry_reasons=reason_list,
    )

    # Nakitten dus
    total_cost = entry_price * shares
    repo.add_cash_transaction(
        -total_cost, "buy",
        related_trade_id=trade_id,
        description=f"{symbol} {shares} lot @ {entry_price}",
    )

    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)
