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
        self._alerted: set[tuple[int, str]] = set()  # (trade_id, alert_type) already sent

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

            # Check remaining shares
            exits = self._repo.get_trade_exits(trade_id)
            total_shares = trade.get("shares", 0)
            exited_shares = sum(e.get("shares", 0) for e in exits)
            remaining = total_shares - exited_shares

            if remaining <= 0:
                continue

            try:
                ticker = bp.Ticker(symbol)
                df = ticker.history(period="5d", interval="1d")
                if df is None or len(df) == 0:
                    logger.warning(f"Fiyat alinamadi: {symbol}")
                    continue
                current_price = float(df.iloc[-1]["Close"])

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
                if sl_hit and (trade_id, "sl") not in self._alerted:
                    self._alerted.add((trade_id, "sl"))
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
                            f"Kalan: {remaining:.0f} lot\n"
                            f"Zarar: {pnl_pct:+.1f}%\n"
                            f"/sat {trade_id} {remaining:.0f} {current_price:.2f}"
                        ),
                    ))
                    continue

            # Check if TP levels already handled (by manual exit or tp exit)
            any_exit_done = len(exits) > 0

            # Check Take Profits
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

                if tp_hit and (trade_id, tp_name) not in self._alerted:
                    # Skip if already exited at this level (manual or auto)
                    already_exited = any(
                        e["exit_type"] in (tp_name, "manual") and e.get("price", 0) >= tp * 0.98
                        for e in exits
                    )
                    if already_exited:
                        self._alerted.add((trade_id, tp_name))
                        continue

                    self._alerted.add((trade_id, tp_name))
                    tp_pct = 0.50 if tp_name == "tp1" else 0.30 if tp_name == "tp2" else 0.20
                    suggested_lots = min(int(total_shares * tp_pct), remaining)

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
                            f"Kar: {pnl_pct:+.1f}%\n"
                            f"Kalan: {remaining:.0f} lot\n"
                            f"/sat {trade_id} {suggested_lots} {current_price:.2f}"
                        ),
                    ))

            # Check trailing stop (after any exit done)
            if self._config.monitor.trailing_stop_enabled and any_exit_done:
                if direction == "long" and (trade_id, "trailing_stop") not in self._alerted:
                    highest = self._highest_prices.get(trade_id, current_price)
                    trailing_pct = self._config.monitor.trailing_stop_atr_mult * 2
                    trail_level = highest * (1 - trailing_pct / 100)

                    if current_price <= trail_level:
                        self._alerted.add((trade_id, "trailing_stop"))
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
                                f"Kar: {pnl_pct:+.1f}%\n"
                                f"Kalan: {remaining:.0f} lot\n"
                                f"/sat {trade_id} {remaining:.0f} {current_price:.2f}"
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
        self._alerted = {
            (tid, atype) for tid, atype in self._alerted
            if tid in open_ids
        }
