"""Data models for backtest engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class BacktestConfig:
    symbols: list[str] = field(default_factory=lambda: ["THYAO", "ASELS", "KCHOL", "SAHOL", "BIMAS"])
    start_date: str = "2024-01-01"
    end_date: str = "2025-12-31"
    initial_cash: float = 100_000
    max_positions: int = 3
    risk_per_trade_pct: float = 2.0
    commission_pct: float = 0.1

    # Market: "bist" or "us"
    market: str = "bist"

    # Commission: percentage for BIST, fixed amount for US
    # For US: commission_pct=0 and commission_fixed used instead
    commission_fixed: float = 0.0  # fixed $ per trade (US)

    # Market regime filter
    market_filter_enabled: bool = True
    market_index: str = "XU100"  # endeks sembolü
    market_sma_period: int = 100  # endeks bu SMA üstündeyse "boğa"

    # Timeframe mode: "multi" (hourly+daily) or "daily" (daily only, for long backtests)
    timeframe_mode: str = "multi"

    # Strategy params — score-based entry
    trend_sma_period: int = 100
    min_entry_score: int = 4  # minimum skor for entry
    # Individual signal scores
    rsi_pullback_threshold: float = 45.0
    rsi_pullback_score: int = 2
    macd_negative_score: int = 1
    bb_lower_score: int = 2
    hourly_rsi_reversal_threshold: float = 40.0
    hourly_rsi_reversal_score: int = 2
    volume_above_avg_score: int = 1
    volume_avg_period: int = 20

    # Exit params
    sl_atr_mult: float = 2.0
    tp1_atr_mult: float = 1.5
    tp1_exit_pct: float = 0.50
    tp2_atr_mult: float = 3.0
    tp2_exit_pct: float = 0.30
    trailing_stop_pct: float = 0.20


@dataclass
class TradeExit:
    date: str
    price: float
    shares: int
    exit_type: Literal["tp1", "tp2", "trailing", "sl"]
    pnl: float
    pnl_pct: float


@dataclass
class BacktestTrade:
    symbol: str
    direction: Literal["long", "short"]
    entry_price: float
    entry_date: str
    shares: int
    stop_loss: float
    tp1: float
    tp2: float
    exits: list[TradeExit] = field(default_factory=list)
    status: Literal["open", "closed"] = "open"
    highest_price: float = 0.0
    tp1_hit: bool = False
    remaining_shares: int = 0

    def __post_init__(self):
        if self.remaining_shares == 0:
            self.remaining_shares = self.shares
        if self.highest_price == 0.0:
            self.highest_price = self.entry_price

    @property
    def total_pnl(self) -> float:
        return sum(e.pnl for e in self.exits)

    @property
    def total_pnl_pct(self) -> float:
        cost = self.entry_price * self.shares
        return (self.total_pnl / cost * 100) if cost > 0 else 0.0


@dataclass
class BacktestMetrics:
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    avg_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    total_return: float = 0.0
    total_return_pct: float = 0.0
    avg_holding_days: float = 0.0


@dataclass
class BacktestResult:
    trades: list[BacktestTrade]
    metrics: BacktestMetrics
    equity_curve: list[tuple[str, float]]
    params: dict = field(default_factory=dict)
