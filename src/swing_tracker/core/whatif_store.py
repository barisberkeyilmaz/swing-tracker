"""Kalici what-if islemleri: whatif_trades satirlarini gunluk ilerleten katman.

Sayfa hicbir simulasyon yapmaz; sinyal dusunce scanner 'pending' satir ekler,
gunluk job (fill_pending -> update_open -> refresh_buyhold -> expire_stale)
hissenin yolunu DB'de yasatir. OHLCV parametreyle enjekte edilir (network yok).
"""

from __future__ import annotations

import logging

import pandas as pd

from swing_tracker.backtest.models import BacktestConfig, BacktestTrade
from swing_tracker.core.whatif import VIRTUAL_SHARES, atr_from_daily, find_entry
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)

OhlcvMap = dict[str, "pd.DataFrame | None"]


def row_to_bt(row: dict) -> BacktestTrade:
    """Open satirin durum alanlarindan BacktestTrade kur (incremental replay icin)."""
    return BacktestTrade(
        symbol=row["symbol"],
        direction="long",
        entry_price=row["entry_price"],
        entry_date=row["signal_time"],
        shares=VIRTUAL_SHARES,
        stop_loss=row["stop_loss"],
        tp1=row["tp1"],
        tp2=row["tp2"],
        status="open",
        highest_price=row["highest_price"] or row["entry_price"],
        tp1_hit=bool(row["tp1_hit"]),
        remaining_shares=row["remaining_shares"],
    )


def fill_pending(
    repo: Repository,
    ohlcv_1h: OhlcvMap,
    ohlcv_1d: OhlcvMap,
    bt_config: BacktestConfig,
) -> dict:
    """Pending satirlarin girisini doldur: pending -> open / no_data.

    Giris bulunamazsa satir pending kalir (ertesi gun yeniden denenir).
    """
    counts = {"opened": 0, "no_data": 0, "left_pending": 0}
    for row in repo.get_whatif_trades(status="pending"):
        symbol = row["symbol"]
        entry = find_entry(ohlcv_1h.get(symbol), row["signal_time"], row["price_at_signal"])
        if entry is None:
            counts["left_pending"] += 1
            continue
        entry_price, source = entry

        delay_cost = None
        if source == "bar_1h" and row["price_at_signal"]:
            delay_cost = round(
                (entry_price - row["price_at_signal"]) / row["price_at_signal"] * 100, 2
            )

        df_1d = ohlcv_1d.get(symbol)
        atr = atr_from_daily(df_1d, row["signal_time"]) if df_1d is not None else None
        if atr is None:
            repo.update_whatif_trade(row["id"], {
                "status": "no_data",
                "entry_price": entry_price,
                "entry_source": source,
                "delay_cost_pct": delay_cost,
            })
            counts["no_data"] += 1
            continue

        signal_day = pd.Timestamp(row["signal_time"]).date().isoformat()
        repo.update_whatif_trade(row["id"], {
            "status": "open",
            "entry_price": entry_price,
            "entry_source": source,
            "delay_cost_pct": delay_cost,
            "stop_loss": round(entry_price - atr * bt_config.sl_atr_mult, 2),
            "tp1": round(entry_price + atr * bt_config.tp1_atr_mult, 2),
            "tp2": round(entry_price + atr * bt_config.tp2_atr_mult, 2),
            "remaining_shares": VIRTUAL_SHARES,
            "realized_pnl": 0.0,
            "highest_price": entry_price,
            "tp1_hit": 0,
            "last_update": signal_day,  # exit kontrolu ertesi gunden (lookahead onlemi)
        })
        counts["opened"] += 1
    return counts
