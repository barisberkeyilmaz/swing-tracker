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
    cash_balance: float
    invested_value: float
    total_pnl: float
    total_pnl_pct: float
    holdings_count: int


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
        """Get full portfolio summary including all holdings and cash."""
        holdings = self._repo.get_all_holdings()
        cash = self._repo.get_cash_balance()

        # Build borsapy Portfolio for current valuations
        portfolio = bp.Portfolio(benchmark=self._config.portfolio.benchmark)
        total_cost = 0.0

        for h in holdings:
            try:
                portfolio.add(
                    symbol=h["symbol"],
                    shares=h["shares"],
                    cost=h["cost_per_share"],
                    asset_type=h["asset_type"],
                )
                if h["cost_per_share"]:
                    total_cost += h["shares"] * h["cost_per_share"]
            except Exception:
                logger.warning(f"Portfoy degeri alinamadi: {h['symbol']}")

        try:
            invested_value = float(portfolio.value)
        except Exception:
            invested_value = total_cost
            logger.warning("Portfoy degeri hesaplanamadi, maliyet degeri kullaniliyor")

        total_value = invested_value + cash
        total_pnl = total_value - (total_cost + cash)
        total_pnl_pct = (total_pnl / (total_cost + cash) * 100) if (total_cost + cash) > 0 else 0

        return PortfolioSummary(
            total_value=round(total_value, 2),
            cash_balance=round(cash, 2),
            invested_value=round(invested_value, 2),
            total_pnl=round(total_pnl, 2),
            total_pnl_pct=round(total_pnl_pct, 2),
            holdings_count=len(holdings),
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
                current = ticker.fast_info.get("last", 0)
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

    def available_cash(self) -> float:
        """Get cash available for swing trades."""
        return self._repo.get_cash_balance()

    def calculate_position_size(
        self,
        entry_price: float,
        stop_loss: float,
        risk_per_trade_pct: float | None = None,
    ) -> tuple[int, float, float]:
        """Calculate position size based on risk.

        Returns: (shares, position_cost, risk_amount)
        """
        cash = self.available_cash()
        risk_pct = risk_per_trade_pct or self._config.portfolio.risk_per_trade_pct
        max_risk = cash * (risk_pct / 100)
        risk_per_share = abs(entry_price - stop_loss)

        if risk_per_share <= 0:
            return 0, 0.0, 0.0

        shares = int(max_risk / risk_per_share)
        position_cost = shares * entry_price

        # Don't exceed 95% of available cash
        if position_cost > cash * 0.95:
            shares = int((cash * 0.95) / entry_price)
            position_cost = shares * entry_price

        risk_amount = shares * risk_per_share
        return shares, round(position_cost, 2), round(risk_amount, 2)

    def record_daily_snapshot(self) -> None:
        """Record today's portfolio snapshot."""
        try:
            summary = self.get_summary()
            swing = self.get_swing_summary()
            today = datetime.now().strftime("%Y-%m-%d")

            self._repo.save_snapshot(
                date=today,
                total_value=summary.total_value,
                cash_balance=summary.cash_balance,
                invested_value=summary.invested_value,
                total_pnl=summary.total_pnl,
                total_pnl_pct=summary.total_pnl_pct,
                swing_pnl=swing.unrealized_pnl + swing.realized_pnl,
            )
            logger.info(f"Gunluk snapshot kaydedildi: {today} - {summary.total_value:.0f} TL")
        except Exception:
            logger.exception("Snapshot kaydi hatasi")

    def deposit_cash(self, amount: float, description: str = "Nakit yatirma") -> None:
        """Record a cash deposit."""
        self._repo.add_cash_transaction(amount, "deposit", description=description)
        logger.info(f"Nakit yatirildi: {amount:.0f} TL")

    def initialize_cash(self) -> None:
        """Initialize cash balance if not already set."""
        current = self._repo.get_cash_balance()
        if current == 0:
            initial = self._config.portfolio.initial_cash
            self._repo.add_cash_transaction(
                initial, "deposit", description="Baslangic nakdi"
            )
            logger.info(f"Baslangic nakdi ayarlandi: {initial:.0f} TL")
