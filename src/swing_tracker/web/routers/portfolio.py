"""Portfolio router — positions, closed trades, equity summary."""

from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from swing_tracker.web.dependencies import templates, get_repo, get_config
from swing_tracker.web.helpers import calc_capital_summary, build_cash_flows

router = APIRouter(prefix="/portfolio")


@router.get("", response_class=HTMLResponse)
async def portfolio(request: Request):
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

    # Kapanmis tradeler
    closed_trades = repo.get_trades_by_status("closed")
    for trade in closed_trades:
        if trade.get("entry_reasons"):
            try:
                trade["entry_reasons"] = json.loads(trade["entry_reasons"])
            except (json.JSONDecodeError, TypeError):
                pass

    # Sermaye ozeti + nakit akisi
    capital = calc_capital_summary(repo)
    cash_flows = build_cash_flows(repo)

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
