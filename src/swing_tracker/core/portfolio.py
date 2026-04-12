"""Portfolio management: wraps borsapy Portfolio, adds cash tracking and snapshots."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import borsapy as bp

from swing_tracker.config import Config
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class PortfolioSummary:
    total_value: float
    invested_value: float
    total_pnl: float
    total_pnl_pct: float


@dataclass
class SwingSummary:
    open_trades: int
    total_invested: float
    unrealized_pnl: float
    realized_pnl: float


class PortfolioManager:
    def __init__(self, repo: Repository, config: Config):
        self._repo = repo
        self._config = config

    def get_summary(self) -> PortfolioSummary:
        """Get portfolio summary based on swing trades (current market value)."""
        open_trades = self._repo.get_open_trades()
        total_cost = 0.0
        current_value = 0.0

        for trade in open_trades:
            # Kalan lot hesabi (partial exit'ler cikarilir)
            exits = self._repo.get_trade_exits(trade["id"])
            exited_shares = sum(e["shares"] for e in exits)
            remaining = trade.get("shares", 0) - exited_shares
            if remaining <= 0:
                continue

            entry_price = trade.get("entry_price", 0) or 0
            total_cost += remaining * entry_price

            # Guncel fiyat
            try:
                ticker = bp.Ticker(trade["symbol"])
                df = ticker.history(period="5d", interval="1d")
                if df is not None and len(df) > 0:
                    current_price = float(df.iloc[-1]["Close"])
                    current_value += remaining * current_price
                else:
                    current_value += remaining * entry_price
            except Exception:
                logger.warning(f"Fiyat alinamadi: {trade['symbol']}")
                current_value += remaining * entry_price

        # Kapanmis tradelerden realize PnL
        closed = self._repo.get_trades_by_status("closed")
        realized_pnl = sum(t.get("realized_pnl", 0) or 0 for t in closed)

        # Acik pozisyonlardaki kismil exit PnL'leri
        for trade in open_trades:
            exits = self._repo.get_trade_exits(trade["id"])
            realized_pnl += sum(e.get("pnl", 0) or 0 for e in exits)

        unrealized_pnl = current_value - total_cost
        total_pnl = unrealized_pnl + realized_pnl
        total_value = current_value
        total_pnl_pct = (unrealized_pnl / total_cost * 100) if total_cost > 0 else 0

        return PortfolioSummary(
            total_value=round(total_value, 2),
            invested_value=round(total_cost, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl_pct, 2),
        )

    def get_swing_summary(self) -> SwingSummary:
        """Get summary of active swing trades."""
        open_trades = self._repo.get_open_trades()
        total_invested = sum(t.get("total_cost", 0) or 0 for t in open_trades)

        # Calculate unrealized P&L
        unrealized_pnl = 0.0
        for trade in open_trades:
            try:
                ticker = bp.Ticker(trade["symbol"])
                df = ticker.history(period="5d", interval="1d")
                if df is not None and len(df) > 0:
                    current = float(df.iloc[-1]["Close"])
                    if current and trade.get("entry_price"):
                        shares = trade.get("shares", 0)
                        unrealized_pnl += (current - trade["entry_price"]) * shares
            except Exception:
                logger.warning(f"Fiyat alinamadi: {trade['symbol']}")

        # Realized P&L from closed trades
        closed = self._repo.get_trades_by_status("closed")
        realized_pnl = sum(t.get("realized_pnl", 0) or 0 for t in closed)

        return SwingSummary(
            open_trades=len(open_trades),
            total_invested=round(total_invested, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            realized_pnl=round(realized_pnl, 2),
        )

    def record_daily_snapshot(self) -> None:
        """Record today's portfolio snapshot using cash flow model."""
        try:
            from swing_tracker.web.helpers import calc_capital_summary
            capital = calc_capital_summary(self._repo)
            swing = self.get_swing_summary()
            today = datetime.now().strftime("%Y-%m-%d")

            self._repo.save_snapshot(
                date=today,
                total_value=capital.total_portfolio,
                cash_balance=capital.available_cash,
                invested_value=capital.open_cost,
                total_pnl=capital.realized_pnl,
                total_pnl_pct=0,
                swing_pnl=swing.unrealized_pnl + swing.realized_pnl,
            )
            logger.info(
                f"Gunluk snapshot: {today} - portfoy {capital.total_portfolio:.0f} TL "
                f"(nakit {capital.available_cash:.0f} + poz {capital.open_cost:.0f})"
            )
        except Exception:
            logger.exception("Snapshot kaydi hatasi")

