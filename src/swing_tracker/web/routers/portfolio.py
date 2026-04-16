"""Portfolio router — positions, closed trades, equity summary."""

from __future__ import annotations

import asyncio
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from swing_tracker.web.dependencies import templates, get_repo, get_config
from swing_tracker.web.helpers import calc_capital_summary, build_cash_flows
from swing_tracker.web.price_cache import price_cache

router = APIRouter(prefix="/portfolio")


@router.get("", response_class=HTMLResponse)
async def portfolio(request: Request, cash_page: int = 1):
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

    # Kapanmis tradeler
    closed_trades = repo.get_trades_by_status("closed")

    # Sermaye ozeti + nakit akisi (sayfalanmis, TR saatinde)
    capital = calc_capital_summary(repo)
    cash_flows = build_cash_flows(
        repo, page=cash_page, per_page=50, tz=config.timezone
    )

    # Snapshotlar
    snapshots = repo.get_snapshots(limit=60)
    snapshots.reverse()

    now = datetime.now(config.timezone)

    return templates.TemplateResponse(
        request,
        "portfolio.html",
        context={
            "open_trades": open_trades,
            "closed_trades": closed_trades,
            "capital": capital,
            "cash_flows": cash_flows,
            "snapshots": snapshots,
            "now": now,
        },
    )


@router.get("/api/prices")
async def portfolio_live_prices():
    """JSON endpoint: live prices for portfolio page. Same data as dashboard."""
    repo = get_repo()
    open_trades = repo.get_open_trades()

    if not open_trades:
        return {"trades": [], "total_unrealized": 0, "total_market_value": 0, "live_portfolio": 0}

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
    }
