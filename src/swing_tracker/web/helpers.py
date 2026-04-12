"""Shared calculation helpers for web routes — no network calls, pure DB reads."""

from __future__ import annotations

from dataclasses import dataclass, field

from swing_tracker.db.repository import Repository


@dataclass
class CapitalSummary:
    deposits: float            # Manuel yatirilan nakit toplami
    total_bought: float        # Toplam alim maliyeti
    total_sold: float          # Toplam satis hasilati
    available_cash: float      # Eldeki nakit = yatirilan - alim + satim
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


def build_cash_flows(repo: Repository) -> list[CashFlow]:
    """Build chronological cash flow log from all sources."""
    entries: list[tuple[str, str, str, float, str]] = []

    # 1. Manuel nakit islemleri
    for tx in repo.get_cash_transactions(limit=200):
        entries.append((
            tx.get("created_at", "")[:16],
            tx["transaction_type"],
            "",
            tx["amount"],
            tx.get("description") or tx["transaction_type"],
        ))

    # 2. Alimlar (swing_trades)
    all_trades = repo.get_open_trades() + repo.get_trades_by_status("closed")
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

        # 3. Satislar (trade_exits)
        for ex in repo.get_trade_exits(trade["id"]):
            ex_price = ex.get("price", 0) or 0
            ex_shares = ex.get("shares", 0) or 0
            proceeds = ex_price * ex_shares
            pnl = ex.get("pnl", 0) or 0
            entries.append((
                (ex.get("exit_date") or "")[:16],
                "sell",
                trade["symbol"],
                proceeds,
                f"{ex_shares:.0f} lot @ {ex_price:.2f} (K/Z: {pnl:+,.0f})",
            ))

    # Tarihe gore sirala
    entries.sort(key=lambda e: e[0])

    # Kumulatif bakiye hesapla
    flows = []
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

    return flows


def calc_capital_summary(repo: Repository) -> CapitalSummary:
    """Calculate capital summary from trade + cash data — no borsapy calls."""
    # Manuel nakit yatirimlari (web'den eklenen)
    deposits = repo.get_cash_balance()

    all_trades = repo.get_open_trades() + repo.get_trades_by_status("closed")

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

        exits = repo.get_trade_exits(trade["id"])
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

    available_cash = deposits - total_bought + total_sold
    total_portfolio = available_cash + open_cost
    win_rate = (winning_exits / total_exits * 100) if total_exits else 0

    return CapitalSummary(
        deposits=round(deposits, 2),
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
