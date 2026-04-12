"""Dashboard router — main page with market overview, positions, and signals."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from swing_tracker.web.dependencies import templates, get_repo, get_config
from swing_tracker.web.helpers import calc_capital_summary

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    repo = get_repo()
    config = get_config()

    # Acik pozisyonlar
    open_trades = repo.get_open_trades()
    for trade in open_trades:
        if trade.get("entry_reasons"):
            try:
                trade["entry_reasons"] = json.loads(trade["entry_reasons"])
            except (json.JSONDecodeError, TypeError):
                pass
        exits = repo.get_trade_exits(trade["id"])
        exited_shares = sum(e["shares"] for e in exits)
        trade["remaining_shares"] = trade.get("shares", 0) - exited_shares
        trade["exits"] = exits
        trade["realized_pnl"] = sum(e.get("pnl", 0) or 0 for e in exits)

    # Son sinyaller
    recent_signals = repo.get_recent_signals(limit=10)
    for sig in recent_signals:
        if sig.get("indicator_values"):
            try:
                sig["indicator_values"] = json.loads(sig["indicator_values"])
            except (json.JSONDecodeError, TypeError):
                pass

    # Sermaye ozeti (sadece DB okuma — hizli)
    capital = calc_capital_summary(repo)

    now = datetime.now(config.timezone)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "open_trades": open_trades,
            "recent_signals": recent_signals,
            "capital": capital,
            "now": now,
            "max_positions": config.portfolio.max_swing_positions,
        },
    )


@router.post("/cash/add")
async def add_cash(amount: float = Form(...), description: str = Form("")):
    """Manuel nakit ekleme."""
    repo = get_repo()
    desc = description.strip() or "Manuel nakit yatirma"
    repo.add_cash_transaction(amount, "deposit", description=desc)
    return RedirectResponse(url="/", status_code=303)


@router.post("/cash/withdraw")
async def withdraw_cash(amount: float = Form(...), description: str = Form("")):
    """Manuel nakit cekme."""
    repo = get_repo()
    desc = description.strip() or "Manuel nakit cekme"
    repo.add_cash_transaction(-amount, "withdrawal", description=desc)
    return RedirectResponse(url="/", status_code=303)
