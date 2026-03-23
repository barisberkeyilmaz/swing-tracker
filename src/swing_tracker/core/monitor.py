"""Position monitor: checks open trades for TP/SL hits.

Runs every 5 minutes during market hours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import borsapy as bp

from swing_tracker.config import Config
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class Alert:
    trade_id: int
    symbol: str
    alert_type: Literal["tp1", "tp2", "tp3", "sl", "trailing_stop", "warning"]
    current_price: float
    target_price: float
    entry_price: float
    pnl_pct: float
    message: str


class Monitor:
    def __init__(self, repo: Repository, config: Config):
        self._repo = repo
        self._config = config
        self._highest_prices: dict[int, float] = {}  # trade_id -> highest price seen

    def check_positions(self) -> list[Alert]:
        """Check all open positions for TP/SL hits.

        Returns a list of alerts that should be sent as notifications.
        """
        open_trades = self._repo.get_open_trades()
        if not open_trades:
            return []

        alerts: list[Alert] = []

        for trade in open_trades:
            trade_id = trade["id"]
            symbol = trade["symbol"]

            try:
                ticker = bp.Ticker(symbol)
                info = ticker.fast_info
                current_price = float(info.get("last", 0))

                if current_price <= 0:
                    logger.warning(f"Gecersiz fiyat: {symbol}")
                    continue

            except Exception:
                logger.warning(f"Fiyat alinamadi: {symbol}")
                continue

            entry_price = trade.get("entry_price", 0)
            if not entry_price:
                continue

            pnl_pct = (current_price - entry_price) / entry_price * 100
            direction = trade.get("direction", "long")

            # Track highest price for trailing stop
            prev_highest = self._highest_prices.get(trade_id, entry_price)
            if current_price > prev_highest:
                self._highest_prices[trade_id] = current_price

            # Check Stop Loss
            sl = trade.get("stop_loss")
            if sl:
                sl_hit = (
                    (direction == "long" and current_price <= sl) or
                    (direction == "short" and current_price >= sl)
                )
                if sl_hit:
                    alerts.append(Alert(
                        trade_id=trade_id,
                        symbol=symbol,
                        alert_type="sl",
                        current_price=current_price,
                        target_price=sl,
                        entry_price=entry_price,
                        pnl_pct=round(pnl_pct, 2),
                        message=(
                            f"STOP LOSS: {symbol}\n"
                            f"Giris: {entry_price:.2f} -> Simdi: {current_price:.2f}\n"
                            f"Zarar: {pnl_pct:+.1f}%"
                        ),
                    ))
                    continue  # SL hit, no need to check TPs

            # Check Take Profits (TP1 -> TP2 -> TP3)
            for tp_key, tp_name in [
                ("take_profit_1", "tp1"),
                ("take_profit_2", "tp2"),
                ("take_profit_3", "tp3"),
            ]:
                tp = trade.get(tp_key)
                if not tp:
                    continue

                tp_hit = (
                    (direction == "long" and current_price >= tp) or
                    (direction == "short" and current_price <= tp)
                )
                if tp_hit:
                    # Check if already exited at this level
                    exits = self._repo.get_trade_exits(trade_id)
                    already_exited = any(e["exit_type"] == tp_name for e in exits)

                    if not already_exited:
                        alerts.append(Alert(
                            trade_id=trade_id,
                            symbol=symbol,
                            alert_type=tp_name,
                            current_price=current_price,
                            target_price=tp,
                            entry_price=entry_price,
                            pnl_pct=round(pnl_pct, 2),
                            message=(
                                f"{tp_name.upper()} ULASILDI: {symbol}\n"
                                f"Giris: {entry_price:.2f} -> Simdi: {current_price:.2f}\n"
                                f"Kar: {pnl_pct:+.1f}%"
                            ),
                        ))

            # Check trailing stop (after TP1)
            if self._config.monitor.trailing_stop_enabled:
                exits = self._repo.get_trade_exits(trade_id)
                tp1_exited = any(e["exit_type"] == "tp1" for e in exits)

                if tp1_exited and direction == "long":
                    highest = self._highest_prices.get(trade_id, current_price)
                    # Simple trailing: if price drops X% from highest
                    trailing_pct = self._config.monitor.trailing_stop_atr_mult * 2
                    trail_level = highest * (1 - trailing_pct / 100)

                    if current_price <= trail_level:
                        alerts.append(Alert(
                            trade_id=trade_id,
                            symbol=symbol,
                            alert_type="trailing_stop",
                            current_price=current_price,
                            target_price=trail_level,
                            entry_price=entry_price,
                            pnl_pct=round(pnl_pct, 2),
                            message=(
                                f"TRAILING STOP: {symbol}\n"
                                f"En yuksek: {highest:.2f} -> Simdi: {current_price:.2f}\n"
                                f"Kar: {pnl_pct:+.1f}%"
                            ),
                        ))

        return alerts

    def cleanup_closed_trades(self) -> None:
        """Remove tracking data for closed trades."""
        open_ids = {t["id"] for t in self._repo.get_open_trades()}
        self._highest_prices = {
            tid: price for tid, price in self._highest_prices.items()
            if tid in open_ids
        }
