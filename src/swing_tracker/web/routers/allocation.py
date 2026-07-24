"""Allocation router — hedef vs gercek agirlik, DCA + rebalance onerileri."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from swing_tracker.core import etf_prices
from swing_tracker.core.allocation_service import build_report
from swing_tracker.web.dependencies import get_config, get_repo, templates

router = APIRouter(prefix="/allocation")


@router.get("", response_class=HTMLResponse)
async def allocation_page(request: Request):
    repo = get_repo()
    config = get_config()
    view = await asyncio.to_thread(
        build_report, repo, config.allocation, price_cache=etf_prices.etf_price_cache
    )
    return templates.TemplateResponse(
        request,
        "allocation.html",
        context={"view": view, "config": config.allocation},
    )


@router.post("/holding")
async def add_holding(
    symbol: str = Form(...),
    exchange: str = Form(...),
    shares: float = Form(...),
    cost_per_share: float | None = Form(None),
    notes: str | None = Form(None),
):
    repo = get_repo()
    repo.upsert_allocation_holding(
        symbol.strip().upper(), exchange.strip().upper(), shares, cost_per_share, notes
    )
    return RedirectResponse("/allocation", status_code=303)


@router.post("/holding/delete")
async def delete_holding(symbol: str = Form(...)):
    get_repo().delete_allocation_holding(symbol.strip().upper())
    return RedirectResponse("/allocation", status_code=303)


@router.post("/dca")
async def set_contribution(contribution: float = Form(...)):
    get_repo().set_allocation_setting("last_contribution_usd", str(float(contribution)))
    return RedirectResponse("/allocation", status_code=303)


@router.post("/review")
async def mark_reviewed(note: str | None = Form(None)):
    get_repo().log_allocation_review(note)
    return RedirectResponse("/allocation", status_code=303)
