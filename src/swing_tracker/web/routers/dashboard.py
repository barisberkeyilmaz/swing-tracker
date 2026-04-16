"""Dashboard router — main page with market overview, positions, and signals."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from swing_tracker.web.dependencies import templates, get_repo, get_config
from swing_tracker.web.helpers import calc_capital_summary
from swing_tracker.web.price_cache import price_cache
from swing_tracker.web.regime_cache import get_market_regime

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    repo = get_repo()
    config = get_config()

    # Acik pozisyonlar
    open_trades = repo.get_open_trades()
    for trade in open_trades:
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


@router.get("/api/prices")
async def live_prices():
    """JSON endpoint: live prices + unrealized P&L for open trades + piyasa rejim."""
    repo = get_repo()
    open_trades = repo.get_open_trades()

    market_bullish = await asyncio.to_thread(get_market_regime)

    if not open_trades:
        return {
            "trades": [],
            "total_unrealized": 0,
            "total_market_value": 0,
            "live_portfolio": 0,
            "market_bullish": market_bullish,
        }

    for trade in open_trades:
        exits = repo.get_trade_exits(trade["id"])
        exited_shares = sum(e["shares"] for e in exits)
        trade["remaining_shares"] = trade.get("shares", 0) - exited_shares

    symbols = list({t["symbol"] for t in open_trades})
    prices = await asyncio.to_thread(price_cache.fetch_many, symbols)

    trade_data = []
    total_unrealized = 0.0
    total_market_value = 0.0
    capital = calc_capital_summary(repo)

    for trade in open_trades:
        symbol = trade["symbol"]
        current_price = prices.get(symbol)
        entry_price = trade.get("entry_price", 0) or 0
        remaining = trade.get("remaining_shares", 0)

        if current_price and entry_price and remaining > 0:
            unrealized = round((current_price - entry_price) * remaining, 0)
            unrealized_pct = round((current_price - entry_price) / entry_price * 100, 1)
            market_val = round(current_price * remaining, 0)
            total_unrealized += unrealized
            total_market_value += market_val
        else:
            unrealized = None
            unrealized_pct = None
            market_val = None
            current_price = None

        trade_data.append({
            "id": trade["id"],
            "current_price": round(current_price, 2) if current_price else None,
            "unrealized": unrealized,
            "unrealized_pct": unrealized_pct,
        })

    live_portfolio = round(capital.available_cash + total_market_value, 0)

    return {
        "trades": trade_data,
        "total_unrealized": round(total_unrealized, 0),
        "total_market_value": round(total_market_value, 0),
        "live_portfolio": live_portfolio,
        "market_bullish": market_bullish,
    }


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
