"""What-if simulasyonu: uretilen buy sinyalleri alinsaydi performans ne olurdu.

Pure function'lar — veri erisimi (OHLCV, guncel fiyat) parametreyle enjekte edilir.
Giris fiyati sinyalden sonraki ilk 1h bar'in kapanisi (15 dk veri gecikmesi modeli).
Cikis kurallari backtest/exits.py ile ortak.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

from swing_tracker.backtest.exits import check_exits
from swing_tracker.backtest.models import BacktestConfig, BacktestTrade

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


def _simulate_strategy(
    trade: WhatIfTrade,
    df_1d: pd.DataFrame,
    current_price: float | None,
    bt_config: BacktestConfig,
) -> None:
    """Gunluk bar'lari check_exits'e vererek strateji sonucunu WhatIfTrade'e yazar."""
    bt = BacktestTrade(
        symbol=trade.symbol,
        direction="long",
        entry_price=trade.entry_price,
        entry_date=trade.signal_time,
        shares=VIRTUAL_SHARES,
        stop_loss=trade.stop_loss,
        tp1=trade.tp1,
        tp2=trade.tp2,
    )
    # Lookahead onlemi: giris gununun KENDI bar'i exit tetiklemez,
    # ertesi gunden itibaren bakilir.
    entry_day = pd.Timestamp(trade.signal_time).normalize()
    later = df_1d[df_1d.index.normalize() > entry_day]

    for ts, row in later.iterrows():
        exits = check_exits(
            bt, ts.date().isoformat(),
            float(row["High"]), float(row["Low"]), float(row["Close"]),
            bt_config,
        )
        # check_exits might return early without adding to trade.exits, so do it manually
        bt.exits.extend(exits)
        if bt.status == "closed":
            break

    cost = trade.entry_price * VIRTUAL_SHARES
    if bt.status == "closed":
        trade.status = "closed"
        trade.strategy_pnl_pct = round(bt.total_pnl / cost * 100, 2)
        last_exit = bt.exits[-1]
        trade.exit_type = last_exit.exit_type
        trade.exit_date = last_exit.date
        trade.holding_days = float(
            (pd.Timestamp(last_exit.date) - entry_day).days
        )
    else:
        trade.status = "open"
        unrealized = 0.0
        if current_price is not None:
            unrealized = (current_price - trade.entry_price) * bt.remaining_shares
        trade.strategy_pnl_pct = round((bt.total_pnl + unrealized) / cost * 100, 2)


def simulate_whatif(
    signals: list[dict],
    ohlcv_1h: dict[str, pd.DataFrame | None],
    ohlcv_1d: dict[str, pd.DataFrame | None],
    current_prices: dict[str, float],
    bt_config: BacktestConfig,
) -> tuple[list[WhatIfTrade], int]:
    """Sinyalleri kronolojik isler; (islemler, dedup ile atlanan sayisi) doner.

    Dedup: sembolde acik sanal pozisyon varken (veya kapanis sinyalden sonraysa)
    yeni buy sinyali atlanir.
    """
    trades: list[WhatIfTrade] = []
    skipped = 0
    # symbol -> son islemin kapanis Timestamp'i (None = hala acik/no_data)
    position_until: dict[str, pd.Timestamp | None] = {}

    for sig in signals:
        symbol = sig["symbol"]
        signal_ts = sig["created_at"]

        if symbol in position_until:
            closed_at = position_until[symbol]
            if closed_at is None or pd.Timestamp(signal_ts) <= closed_at:
                skipped += 1
                continue

        entry = find_entry(ohlcv_1h.get(symbol), signal_ts, sig.get("price_at_signal"))
        if entry is None:
            continue  # fiyat yok: islem uretilemez, dedup'a da girmez
        entry_price, source = entry

        price_at_signal = sig.get("price_at_signal")
        delay_cost = None
        if source == "bar_1h" and price_at_signal:
            delay_cost = round((entry_price - price_at_signal) / price_at_signal * 100, 2)

        current = current_prices.get(symbol)
        df_1d = ohlcv_1d.get(symbol)
        atr = atr_from_daily(df_1d, signal_ts) if df_1d is not None else None

        trade = WhatIfTrade(
            signal_id=sig["id"],
            symbol=symbol,
            signal_time=signal_ts,
            score=sig.get("score") or 0,
            price_at_signal=price_at_signal,
            entry_price=entry_price,
            entry_source=source,
            stop_loss=round(entry_price - (atr or 0) * bt_config.sl_atr_mult, 2),
            tp1=round(entry_price + (atr or 0) * bt_config.tp1_atr_mult, 2),
            tp2=round(entry_price + (atr or 0) * bt_config.tp2_atr_mult, 2),
            status="no_data",
            current_price=current,
            delay_cost_pct=delay_cost,
        )

        if current is not None:
            trade.buyhold_pnl_pct = round((current - entry_price) / entry_price * 100, 2)

        if df_1d is not None and atr is not None:
            _simulate_strategy(trade, df_1d, current, bt_config)

        trades.append(trade)
        if trade.status == "closed":
            position_until[symbol] = pd.Timestamp(trade.exit_date)
        else:
            position_until[symbol] = None  # acik veya no_data: sembol blokeli

    return trades, skipped
