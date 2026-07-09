"""What-if simulasyonu: uretilen buy sinyalleri alinsaydi performans ne olurdu.

Pure function'lar — veri erisimi (OHLCV, guncel fiyat) parametreyle enjekte edilir.
Giris fiyati sinyalden sonraki ilk 1h bar'in kapanisi (15 dk veri gecikmesi modeli).
Cikis kurallari backtest/exits.py ile ortak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

ATR_PERIOD = 14
VIRTUAL_SHARES = 100  # yuzde getiri olculuyor; sabit sanal lot


@dataclass
class WhatIfTrade:
    signal_id: int
    symbol: str
    signal_time: str            # UTC "YYYY-MM-DD HH:MM:SS"
    score: int
    price_at_signal: float | None
    entry_price: float
    entry_source: Literal["bar_1h", "fallback"]
    stop_loss: float
    tp1: float
    tp2: float
    status: Literal["open", "closed", "no_data"]
    strategy_pnl_pct: float | None = None
    exit_type: str | None = None      # kapali islemde son cikisin tipi
    exit_date: str | None = None      # kapali islemde son cikisin tarihi (ISO)
    holding_days: float | None = None  # sadece kapali islemler
    buyhold_pnl_pct: float | None = None
    current_price: float | None = None
    delay_cost_pct: float | None = None  # (entry - price_at_signal) / price_at_signal * 100


def find_entry(
    df_1h: pd.DataFrame | None,
    signal_ts: str,
    price_at_signal: float | None,
) -> tuple[float, str] | None:
    """Sinyalden sonraki ilk 1h bar'in kapanisini giris fiyati olarak sec.

    1h bar yoksa veya sinyal son bar'dan sonraysa price_at_signal'a duser.
    Hicbir fiyat yoksa None.
    """
    ts = pd.Timestamp(signal_ts)
    if df_1h is not None and not df_1h.empty:
        later = df_1h[df_1h.index >= ts]
        if not later.empty:
            close = later.iloc[0]["Close"]
            if pd.notna(close) and float(close) > 0:
                return float(close), "bar_1h"
    if price_at_signal is not None and price_at_signal > 0:
        return float(price_at_signal), "fallback"
    return None


def atr_from_daily(
    df_1d: pd.DataFrame, upto_ts: str, period: int = ATR_PERIOD
) -> float | None:
    """Sinyal gununden ONCEKI gunluk bar'lardan ATR (basit rolling mean TR).

    Sinyal gununun kendi bar'i dahil edilmez: gunun tam araligi sinyal aninda
    henuz bilinemez (lookahead onlemi).
    """
    ts = pd.Timestamp(upto_ts).normalize()
    df = df_1d[df_1d.index < ts]
    if len(df) < period + 1:
        return None
    prev_close = df["Close"].shift(1)
    tr = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    if pd.isna(atr) or atr <= 0:
        return None
    return float(atr)
