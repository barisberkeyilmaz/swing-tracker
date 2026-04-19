"""Trade detail router — single trade with exit history."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from swing_tracker.web.dependencies import templates, get_repo, get_config
from swing_tracker.web.price_cache import price_cache

router = APIRouter(prefix="/trades")


@router.get("/{trade_id}", response_class=HTMLResponse)
async def trade_detail(request: Request, trade_id: int):
    repo = get_repo()
    config = get_config()

    trade = repo.get_trade(trade_id)
    if not trade:
        return HTMLResponse("<h1>Trade bulunamadi</h1>", status_code=404)

    exits = repo.get_trade_exits(trade_id)
    exited_shares = sum(e["shares"] for e in exits)
    remaining_shares = trade.get("shares", 0) - exited_shares
    realized_pnl = sum(e.get("pnl", 0) or 0 for e in exits)

    # Hedefler karti: giris'e gore SL/TP uzakligi (statik)
    entry = trade.get("entry_price") or 0
    sl = trade.get("stop_loss")
    tp1 = trade.get("take_profit_1")
    tp2 = trade.get("take_profit_2")
    tp3 = trade.get("take_profit_3")
    target_pcts = {
        "sl": ((sl - entry) / entry * 100) if (entry and sl) else None,
        "tp1": ((tp1 - entry) / entry * 100) if (entry and tp1) else None,
        "tp2": ((tp2 - entry) / entry * 100) if (entry and tp2) else None,
        "tp3": ((tp3 - entry) / entry * 100) if (entry and tp3) else None,
    }

    # TP lot dagilimi: 50/30/20 kurali (telegram /al + monitor.py ile ayni)
    shares = int(trade.get("shares") or 0)
    tp_lots = {
        "tp1": int(shares * 0.50),
        "tp2": int(shares * 0.30),
        "tp3": int(shares * 0.20),
    }

    # Hold suresi
    days_held: int | None = None
    entry_date_raw = trade.get("entry_date")
    if entry_date_raw:
        try:
            entry_dt = datetime.strptime(entry_date_raw[:10], "%Y-%m-%d")
            if trade.get("status") == "closed" and trade.get("exit_date"):
                end_dt = datetime.strptime(trade["exit_date"][:10], "%Y-%m-%d")
            else:
                end_dt = datetime.now(config.timezone).replace(tzinfo=None)
            days_held = max(0, (end_dt - entry_dt).days)
        except (ValueError, TypeError):
            pass

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
            "target_pcts": target_pcts,
            "tp_lots": tp_lots,
            "days_held": days_held,
            "now": now,
        },
    )


@router.get("/{trade_id}/live")
async def trade_live(trade_id: int):
    """JSON endpoint: canli fiyat + unrealized P&L tek trade icin."""
    repo = get_repo()
    trade = repo.get_trade(trade_id)
    if not trade:
        return {"error": "not found"}

    exits = repo.get_trade_exits(trade_id)
    exited_shares = sum(e["shares"] for e in exits)
    remaining = trade.get("shares", 0) - exited_shares
    entry_price = trade.get("entry_price") or 0
    symbol = trade["symbol"]

    price = await asyncio.to_thread(price_cache.fetch_one, symbol)
    if price is None:
        return {
            "current_price": None,
            "unrealized": None,
            "unrealized_pct": None,
            "market_value": None,
        }

    if remaining > 0 and entry_price:
        unrealized = round((price - entry_price) * remaining, 0)
        unrealized_pct = round((price - entry_price) / entry_price * 100, 1)
        market_value = round(price * remaining, 0)
    else:
        unrealized = None
        unrealized_pct = None
        market_value = None

    return {
        "current_price": round(price, 2),
        "unrealized": unrealized,
        "unrealized_pct": unrealized_pct,
        "market_value": market_value,
    }


@router.post("/{trade_id}/exit")
async def exit_trade(
    trade_id: int,
    exit_price: float = Form(...),
    shares: int = Form(...),
    exit_type: str = Form("manual"),
):
    """Kismi veya tam cikis yap."""
    repo = get_repo()
    trade = repo.get_trade(trade_id)
    if not trade:
        return RedirectResponse(url="/", status_code=303)

    exits = repo.get_trade_exits(trade_id)
    exited_shares = sum(e["shares"] for e in exits)
    remaining = trade.get("shares", 0) - exited_shares

    if shares <= 0 or shares > remaining:
        return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)

    entry_price = trade.get("entry_price", 0)
    pnl = (exit_price - entry_price) * shares
    pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0

    repo.record_exit(trade_id, exit_type, shares, exit_price, pnl, pnl_pct)

    # Nakite ekle
    revenue = exit_price * shares
    repo.add_cash_transaction(
        revenue, "sell",
        related_trade_id=trade_id,
        description=f"{trade['symbol']} {shares} lot @ {exit_price}",
    )

    # Trade status guncelle
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    if shares == remaining:
        # Tam cikis — tum exit'lerin agirlikli ortalamasi
        all_exits = exits + [{"price": exit_price, "shares": shares}]
        total_exit_shares = sum(e["shares"] for e in all_exits)
        exit_avg = sum(e["price"] * e["shares"] for e in all_exits) / total_exit_shares
        total_pnl = sum(e.get("pnl", 0) or 0 for e in exits) + pnl
        total_pnl_pct = ((exit_avg - entry_price) / entry_price * 100) if entry_price else 0
        repo.update_trade_status(
            trade_id, "closed",
            exit_price_avg=round(exit_avg, 2),
            exit_date=now,
            realized_pnl=round(total_pnl, 2),
            realized_pnl_pct=round(total_pnl_pct, 2),
        )
    else:
        repo.update_trade_status(trade_id, "partial_exit")

    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.post("/{trade_id}/exits/{exit_id}/delete")
async def delete_exit(trade_id: int, exit_id: int):
    """Tek bir exit kaydini sil ve trade durumunu/nakiti geri al."""
    repo = get_repo()

    exit_record = repo.get_exit(exit_id)
    if not exit_record or exit_record["trade_id"] != trade_id:
        return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)

    trade = repo.get_trade(trade_id)
    if not trade:
        return RedirectResponse(url="/", status_code=303)

    # 1. Ilgili sell cash transaction'i sil (varsa)
    revenue = exit_record["price"] * exit_record["shares"]
    repo.delete_sell_transaction(trade_id, revenue)

    # 2. Exit kaydini sil
    repo.delete_exit(exit_id)

    # 3. Trade status'u yeniden hesapla
    remaining_exits = repo.get_trade_exits(trade_id)
    total_exited = sum(e["shares"] for e in remaining_exits)

    if total_exited == 0:
        repo.update_trade_status(
            trade_id, "open",
            exit_price_avg=None, exit_date=None,
            realized_pnl=None, realized_pnl_pct=None,
        )
    else:
        repo.update_trade_status(
            trade_id, "partial_exit",
            exit_price_avg=None, exit_date=None,
            realized_pnl=None, realized_pnl_pct=None,
        )

    return RedirectResponse(url=f"/trades/{trade_id}", status_code=303)


@router.post("/{trade_id}/delete")
async def delete_trade(trade_id: int):
    """Trade sil — exit'ler ve ilgili nakit islemleri de silinir."""
    repo = get_repo()
    repo.delete_trade(trade_id)
    return RedirectResponse(url="/", status_code=303)
