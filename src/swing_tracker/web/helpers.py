"""Shared calculation helpers for web routes — no network calls, pure DB reads."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from swing_tracker.db.repository import Repository

_DEFAULT_TZ = ZoneInfo("Europe/Istanbul")


def _utc_to_local(ts: str, tz: ZoneInfo) -> str:
    """DB'deki UTC timestamp'i (datetime('now') ciktisi) yerel zamana cevir.

    Bos/gecersiz string gelirse oldugu gibi dondurur.
    """
    if not ts:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(ts[:19], fmt).replace(tzinfo=timezone.utc)
            return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return ts[:16]


@dataclass
class CapitalSummary:
    net_deposits: float        # Manuel net nakit (yatirma - cekme)
    total_bought: float        # Toplam alim maliyeti
    total_sold: float          # Toplam satis hasilati
    available_cash: float      # Eldeki nakit = net_deposits - alim + satim
    open_cost: float           # Acik pozisyonlarin maliyeti (kalan lot x giris)
    realized_pnl: float        # Realize K/Z (satislardan)
    total_portfolio: float     # Toplam portfoy = nakit + pozisyon maliyeti
    winning_exits: int
    total_exits: int
    win_rate: float


@dataclass
class CashFlow:
    date: str
    flow_type: str    # deposit, withdrawal, buy, sell
    symbol: str
    amount: float     # + gelen, - giden
    detail: str
    balance: float    # kumulatif bakiye


@dataclass
class CashFlowPage:
    items: list[CashFlow]
    page: int
    per_page: int
    total: int
    total_pages: int


def build_cash_flows(
    repo: Repository,
    page: int = 1,
    per_page: int = 50,
    tz: ZoneInfo | None = None,
) -> CashFlowPage:
    """Build chronological cash flow log from all sources."""
    tz = tz or _DEFAULT_TZ
    entries: list[tuple[str, str, str, float, str]] = []
    manual_cash_types = ("deposit", "withdrawal")

    # 1. Manuel nakit islemleri (UTC -> local)
    for tx in repo.get_cash_transactions(
        limit=10_000, transaction_types=manual_cash_types
    ):
        entries.append((
            _utc_to_local(tx.get("created_at", ""), tz),
            tx["transaction_type"],
            "",
            tx["amount"],
            tx.get("description") or tx["transaction_type"],
        ))

    # 2. Alimlar (swing_trades) + exit'ler tek batch'te (N+1 kacinir)
    all_trades = repo.get_open_trades() + repo.get_trades_by_status("closed")
    exits_by_trade = repo.get_all_trade_exits()
    for trade in all_trades:
        entry_price = trade.get("entry_price", 0) or 0
        shares = trade.get("shares", 0) or 0
        cost = entry_price * shares
        entries.append((
            (trade.get("entry_date") or "")[:16],
            "buy",
            trade["symbol"],
            -cost,
            f"{shares:.0f} lot @ {entry_price:.2f}",
        ))

        # 3. Satislar (trade_exits) — exit_date DB'de UTC
        for ex in exits_by_trade.get(trade["id"], []):
            ex_price = ex.get("price", 0) or 0
            ex_shares = ex.get("shares", 0) or 0
            proceeds = ex_price * ex_shares
            pnl = ex.get("pnl", 0) or 0
            entries.append((
                _utc_to_local(ex.get("exit_date", ""), tz),
                "sell",
                trade["symbol"],
                proceeds,
                f"{ex_shares:.0f} lot @ {ex_price:.2f} (K/Z: {pnl:+,.0f})",
            ))

    # Tarihe gore sirala (eski -> yeni) ve kumulatif bakiye hesapla
    entries.sort(key=lambda e: e[0])

    flows: list[CashFlow] = []
    balance = 0.0
    for date, flow_type, symbol, amount, detail in entries:
        balance += amount
        flows.append(CashFlow(
            date=date,
            flow_type=flow_type,
            symbol=symbol,
            amount=round(amount, 2),
            detail=detail,
            balance=round(balance, 2),
        ))

    # En yeni ustte olacak sekilde ters cevir, sonra sayfalama uygula
    flows.reverse()
    total = len(flows)
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    items = flows[start : start + per_page]

    return CashFlowPage(
        items=items,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
    )


def calc_capital_summary(repo: Repository) -> CapitalSummary:
    """Calculate capital summary from trade + cash data — no borsapy calls."""
    # Manuel net nakit hareketleri (yatirma - cekme)
    net_deposits = repo.get_cash_balance(("deposit", "withdrawal"))

    all_trades = repo.get_open_trades() + repo.get_trades_by_status("closed")
    exits_by_trade = repo.get_all_trade_exits()

    total_bought = 0.0
    total_sold = 0.0
    realized_pnl = 0.0
    open_cost = 0.0
    winning_exits = 0
    total_exits = 0

    for trade in all_trades:
        entry_price = trade.get("entry_price", 0) or 0
        shares = trade.get("shares", 0) or 0
        total_bought += entry_price * shares

        exits = exits_by_trade.get(trade["id"], [])
        for ex in exits:
            total_sold += (ex.get("price", 0) or 0) * (ex.get("shares", 0) or 0)
            pnl = ex.get("pnl", 0) or 0
            realized_pnl += pnl
            total_exits += 1
            if pnl > 0:
                winning_exits += 1

        if trade.get("status") in ("open", "partial_exit"):
            exited_shares = sum(e.get("shares", 0) or 0 for e in exits)
            remaining = shares - exited_shares
            if remaining > 0:
                open_cost += remaining * entry_price

    available_cash = net_deposits - total_bought + total_sold
    total_portfolio = available_cash + open_cost
    win_rate = (winning_exits / total_exits * 100) if total_exits else 0

    return CapitalSummary(
        net_deposits=round(net_deposits, 2),
        total_bought=round(total_bought, 2),
        total_sold=round(total_sold, 2),
        available_cash=round(available_cash, 2),
        open_cost=round(open_cost, 2),
        realized_pnl=round(realized_pnl, 2),
        total_portfolio=round(total_portfolio, 2),
        winning_exits=winning_exits,
        total_exits=total_exits,
        win_rate=round(win_rate, 1),
    )
