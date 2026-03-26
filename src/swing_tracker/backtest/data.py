"""Data fetching and multi-timeframe alignment for backtesting.

Supports two markets:
- BIST: via borsapy
- US: via yfinance
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import pandas as pd

from swing_tracker.core.signals import _add_all_indicators

logger = logging.getLogger(__name__)

CACHE_DIR = Path(__file__).parent.parent.parent.parent / "data" / "backtest_cache"

Market = Literal["bist", "us"]


def _detect_market(symbol: str) -> Market:
    """Detect market from symbol pattern."""
    # BIST symbols are all uppercase Turkish, typically 4-5 chars
    # US symbols can overlap but we check for known US patterns
    bist_chars = set(symbol.upper())
    # If symbol has dots or is a known index prefix, it's likely US
    if "." in symbol or symbol.startswith("^"):
        return "us"
    return "bist"


def _fetch_bist(symbol: str, start: str, end: str, interval: str) -> pd.DataFrame | None:
    """Fetch data via borsapy (BIST)."""
    import borsapy as bp
    ticker = bp.Ticker(symbol)
    df = ticker.history(start=start, end=end, interval=interval)
    return df


def _fetch_us(symbol: str, start: str, end: str, interval: str) -> pd.DataFrame | None:
    """Fetch data via yfinance (US markets).

    For hourly data, fetches in 59-day chunks due to yfinance limitations.
    """
    import yfinance as yf
    from datetime import datetime, timedelta

    ticker = yf.Ticker(symbol)

    if interval in ("1h", "60m"):
        # yfinance limits hourly data requests to ~60 days per request
        start_dt = datetime.strptime(start, "%Y-%m-%d")
        end_dt = datetime.strptime(end, "%Y-%m-%d")
        chunks: list[pd.DataFrame] = []
        current = start_dt

        while current < end_dt:
            chunk_end = min(current + timedelta(days=59), end_dt)
            try:
                chunk = ticker.history(
                    start=current.strftime("%Y-%m-%d"),
                    end=chunk_end.strftime("%Y-%m-%d"),
                    interval=interval,
                )
                if chunk is not None and len(chunk) > 0:
                    chunks.append(chunk)
            except Exception:
                pass
            current = chunk_end

        if not chunks:
            return None
        df = pd.concat(chunks)
        df = df[~df.index.duplicated(keep='first')]
    else:
        df = ticker.history(start=start, end=end, interval=interval)

    if df is not None and len(df) > 0:
        df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index
    return df


def _fetch(symbol: str, start: str, end: str, interval: str, market: Market) -> pd.DataFrame | None:
    """Fetch data from the appropriate source."""
    if market == "bist":
        return _fetch_bist(symbol, start, end, interval)
    else:
        return _fetch_us(symbol, start, end, interval)


def _add_indicators(df: pd.DataFrame, market: Market) -> pd.DataFrame:
    """Add technical indicators. Uses borsapy for BIST, manual calc for US."""
    if market == "bist":
        return _add_all_indicators(df)
    else:
        # Manual indicator calculation for US (no borsapy dependency)
        return _add_indicators_manual(df)


def _add_indicators_manual(df: pd.DataFrame) -> pd.DataFrame:
    """Calculate technical indicators without borsapy."""
    # RSI
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(14).mean()
    rs = gain / loss
    df["RSI_14"] = 100 - (100 / (1 + rs))

    # SMA
    df["SMA_20"] = df["Close"].rolling(20).mean()
    df["SMA_50"] = df["Close"].rolling(50).mean()
    df["SMA_100"] = df["Close"].rolling(100).mean()
    df["SMA_200"] = df["Close"].rolling(200).mean()

    # EMA
    df["EMA_12"] = df["Close"].ewm(span=12, adjust=False).mean()
    df["EMA_26"] = df["Close"].ewm(span=26, adjust=False).mean()

    # MACD
    df["MACD"] = df["EMA_12"] - df["EMA_26"]
    df["Signal"] = df["MACD"].ewm(span=9, adjust=False).mean()

    # Bollinger Bands
    df["BB_Middle"] = df["SMA_20"]
    bb_std = df["Close"].rolling(20).std()
    df["BB_Upper"] = df["BB_Middle"] + (bb_std * 2)
    df["BB_Lower"] = df["BB_Middle"] - (bb_std * 2)

    # ATR
    high_low = df["High"] - df["Low"]
    high_close = (df["High"] - df["Close"].shift()).abs()
    low_close = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df["ATR"] = tr.rolling(14).mean()
    df["ATR_14"] = df["ATR"]

    # Stochastic
    low_14 = df["Low"].rolling(14).min()
    high_14 = df["High"].rolling(14).max()
    df["Stoch_K"] = ((df["Close"] - low_14) / (high_14 - low_14)) * 100
    df["Stoch_D"] = df["Stoch_K"].rolling(3).mean()

    # Volume average
    df["Vol_Avg_20"] = df["Volume"].rolling(20).mean()

    return df


def fetch_backtest_data(
    symbol: str,
    start: str,
    end: str,
    market: Market = "bist",
    use_cache: bool = True,
    daily_only: bool = False,
) -> dict[str, pd.DataFrame] | None:
    """Fetch daily and hourly OHLCV data for a symbol.

    Returns {"daily": df_daily, "hourly": df_hourly} or None on failure.
    If daily_only=True, skips hourly data fetch (for long-period backtests).
    """
    cache_key = f"{symbol}_do" if daily_only else symbol
    if use_cache:
        cached = _load_cache(cache_key, start, end)
        if cached is not None:
            return cached

    try:
        # Daily data
        df_daily = _fetch(symbol, start, end, "1d", market)
        if df_daily is None or len(df_daily) < 50:
            logger.warning(f"{symbol}: Yetersiz gunluk veri ({len(df_daily) if df_daily is not None else 0})")
            return None

        df_daily = _add_indicators(df_daily, market)
        if "Vol_Avg_20" not in df_daily.columns:
            df_daily["Vol_Avg_20"] = df_daily["Volume"].rolling(20).mean()

        if daily_only:
            result = {"daily": df_daily}
            if use_cache:
                _save_cache(cache_key, start, end, result)
            logger.info(f"{symbol}: {len(df_daily)} gunluk bar yuklendi (daily-only)")
            return result

        # Hourly data
        df_hourly = _fetch(symbol, start, end, "1h", market)
        if df_hourly is None or len(df_hourly) < 100:
            logger.warning(f"{symbol}: Yetersiz saatlik veri ({len(df_hourly) if df_hourly is not None else 0})")
            return None

        df_hourly = _add_indicators(df_hourly, market)
        if "Vol_Avg_20" not in df_hourly.columns:
            df_hourly["Vol_Avg_20"] = df_hourly["Volume"].rolling(20).mean()

        result = {"daily": df_daily, "hourly": df_hourly}

        if use_cache:
            _save_cache(cache_key, start, end, result)

        logger.info(f"{symbol}: {len(df_daily)} gunluk, {len(df_hourly)} saatlik bar yuklendi")
        return result

    except Exception:
        logger.exception(f"{symbol}: Veri cekme hatasi")
        return None


def align_timeframes(daily: pd.DataFrame, hourly: pd.DataFrame) -> pd.DataFrame:
    """Align daily indicators with hourly bars.

    Each hourly bar gets the PREVIOUS day's daily indicators to avoid look-ahead bias.
    """
    # Extract daily columns we need
    daily_cols = ["Close", "RSI_14", "RSI", "SMA_50", "SMA_100", "SMA_200", "ATR", "ATR_14",
                  "MACD", "Signal", "Vol_Avg_20", "BB_Upper", "BB_Lower"]
    available_cols = [c for c in daily_cols if c in daily.columns]

    daily_subset = daily[available_cols].copy()
    daily_subset.columns = [f"d_{c.lower()}" for c in available_cols]

    # Get the date part of daily index
    daily_subset["date"] = daily_subset.index.date if hasattr(daily_subset.index, 'date') else pd.to_datetime(daily_subset.index).date

    # Shift daily data by 1 to avoid look-ahead: use previous day's close indicators
    for col in daily_subset.columns:
        if col != "date":
            daily_subset[col] = daily_subset[col].shift(1)

    daily_subset = daily_subset.dropna(subset=[c for c in daily_subset.columns if c != "date"])

    # Map hourly bars to their date
    hourly = hourly.copy()
    hourly["date"] = hourly.index.date if hasattr(hourly.index, 'date') else pd.to_datetime(hourly.index).date

    # Merge: each hourly bar gets previous day's daily data
    daily_by_date = daily_subset.set_index("date")
    hourly = hourly.join(daily_by_date, on="date", how="left")
    hourly = hourly.drop(columns=["date"])

    return hourly


def fetch_index_data(
    index_symbol: str,
    start: str,
    end: str,
    market: Market = "bist",
    use_cache: bool = True,
) -> pd.DataFrame | None:
    """Fetch daily index data with SMA for market regime filter."""
    if use_cache:
        cached = _load_cache(index_symbol, start, end)
        if cached is not None and "daily" in cached:
            return cached["daily"]

    try:
        df = _fetch(index_symbol, start, end, "1d", market)
        if df is None or len(df) < 50:
            logger.warning(f"{index_symbol}: Yetersiz endeks verisi")
            return None

        if market == "bist":
            import borsapy as bp
            df["SMA_50"] = bp.calculate_sma(df, period=50)
            df["SMA_200"] = bp.calculate_sma(df, period=200)
        else:
            df["SMA_50"] = df["Close"].rolling(50).mean()
            df["SMA_200"] = df["Close"].rolling(200).mean()

        if use_cache:
            _save_cache(index_symbol, start, end, {"daily": df})

        logger.info(f"{index_symbol}: {len(df)} gunluk bar yuklendi")
        return df
    except Exception:
        logger.exception(f"{index_symbol}: Endeks verisi cekme hatasi")
        return None


def preload_universe(
    symbols: list[str],
    start: str,
    end: str,
    market: Market = "bist",
    use_cache: bool = True,
    daily_only: bool = False,
) -> dict[str, dict[str, pd.DataFrame]]:
    """Fetch data for all symbols. Returns {symbol: {"daily": df, "hourly": df}}."""
    data: dict[str, dict[str, pd.DataFrame]] = {}

    for i, symbol in enumerate(symbols):
        logger.info(f"Veri yukleniyor: {symbol} ({i + 1}/{len(symbols)})")
        result = fetch_backtest_data(symbol, start, end, market=market, use_cache=use_cache, daily_only=daily_only)
        if result is not None:
            data[symbol] = result

    logger.info(f"{len(data)}/{len(symbols)} sembol yuklendi")
    return data


def _cache_path(symbol: str, start: str, end: str, tf: str) -> Path:
    return CACHE_DIR / f"{symbol}_{start}_{end}_{tf}.parquet"


def _save_cache(symbol: str, start: str, end: str, data: dict[str, pd.DataFrame]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        for tf, df in data.items():
            path = _cache_path(symbol, start, end, tf)
            df.to_parquet(path)
    except Exception:
        logger.warning(f"Cache kaydi hatasi: {symbol}")


def _load_cache(symbol: str, start: str, end: str) -> dict[str, pd.DataFrame] | None:
    try:
        daily_path = _cache_path(symbol, start, end, "daily")
        hourly_path = _cache_path(symbol, start, end, "hourly")
        if daily_path.exists() and hourly_path.exists():
            return {
                "daily": pd.read_parquet(daily_path),
                "hourly": pd.read_parquet(hourly_path),
            }
    except Exception:
        pass
    return None
