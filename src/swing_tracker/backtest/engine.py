"""Backtest simulation engine: multi-timeframe event-driven loop."""

from __future__ import annotations

import logging

import pandas as pd

from swing_tracker.backtest.data import align_timeframes, fetch_index_data, preload_universe
from swing_tracker.backtest.exits import check_exits, close_trade_at_market
from swing_tracker.backtest.metrics import calculate_metrics
from swing_tracker.backtest.models import (
    BacktestConfig,
    BacktestResult,
    BacktestTrade,
)

logger = logging.getLogger(__name__)


def run_backtest(config: BacktestConfig) -> BacktestResult:
    """Run a full backtest with the given configuration.

    Flow per symbol:
    1. Load daily + hourly data
    2. Align timeframes (daily indicators on hourly bars)
    3. Walk through hourly bars:
       a. Check exits on open positions
       b. Check entry conditions
       c. Open new position if conditions met
    4. Force close any remaining positions at end
    """
    # Load data
    market = config.market
    daily_only = config.timeframe_mode == "daily"
    universe_data = preload_universe(
        config.symbols, config.start_date, config.end_date,
        market=market, daily_only=daily_only,
    )

    if not universe_data:
        logger.error("Hic veri yuklenemedi, backtest iptal")
        return BacktestResult(trades=[], metrics=calculate_metrics([], [], config.initial_cash),
                              equity_curve=[], params=_config_to_params(config))

    all_trades: list[BacktestTrade] = []
    cash = config.initial_cash
    open_positions: list[BacktestTrade] = []
    equity_curve: list[tuple[str, float]] = []

    # Build dataframes per symbol
    aligned: dict[str, pd.DataFrame] = {}
    daily_frames: dict[str, pd.DataFrame] = {}
    for symbol, data in universe_data.items():
        if daily_only:
            # Daily-only mode: use daily data directly with d_ prefix columns
            df = _prepare_daily_only(data["daily"])
            if len(df) > 50:
                aligned[symbol] = df
                daily_frames[symbol] = data["daily"]
        else:
            df = align_timeframes(data["daily"], data["hourly"])
            df = df.dropna(subset=[c for c in df.columns if c.startswith("d_")])
            if len(df) > 0:
                aligned[symbol] = df
                daily_frames[symbol] = data["daily"]

    if not aligned:
        logger.error("Hizalanmis veri yok, backtest iptal")
        return BacktestResult(trades=[], metrics=calculate_metrics([], [], config.initial_cash),
                              equity_curve=[], params=_config_to_params(config))

    # Load market index data for regime filter
    market_daily: pd.DataFrame | None = None
    market_regime: dict[str, bool] = {}  # date -> bull market?
    if config.market_filter_enabled:
        market_daily = fetch_index_data(
            config.market_index, config.start_date, config.end_date, market=market
        )
        if market_daily is not None and "SMA_50" in market_daily.columns:
            for idx_date, idx_row in market_daily.iterrows():
                date_str = str(idx_date.date()) if hasattr(idx_date, 'date') else str(idx_date)[:10]
                sma = idx_row.get("SMA_50")
                close = idx_row.get("Close")
                if pd.notna(sma) and pd.notna(close):
                    market_regime[date_str] = float(close) > float(sma)
            bull_days = sum(1 for v in market_regime.values() if v)
            total_days = len(market_regime)
            logger.info(
                f"Piyasa filtresi: {config.market_index} SMA{config.market_sma_period}, "
                f"{bull_days}/{total_days} gun boga piyasasi"
            )
        else:
            logger.warning("Endeks verisi yuklenemedi, piyasa filtresi devre disi")
            config.market_filter_enabled = False

    # Collect all hourly timestamps across symbols and sort
    all_timestamps: set[pd.Timestamp] = set()
    for df in aligned.values():
        all_timestamps.update(df.index)
    sorted_timestamps = sorted(all_timestamps)

    logger.info(f"Backtest basliyor: {len(aligned)} sembol, {len(sorted_timestamps)} bar")

    last_date = ""
    # Track last entry date per symbol to prevent same-day re-entry
    last_entry_date: dict[str, str] = {}

    for ts in sorted_timestamps:
        current_date = str(ts.date()) if hasattr(ts, 'date') else str(ts)[:10]

        # Record equity once per day
        if current_date != last_date and last_date != "":
            equity = _calculate_equity(cash, open_positions, aligned, ts)
            equity_curve.append((last_date, equity))
        last_date = current_date

        # Check exits for open positions
        for trade in open_positions[:]:
            if trade.status == "closed":
                continue
            symbol = trade.symbol
            if symbol not in aligned:
                continue
            df = aligned[symbol]
            if ts not in df.index:
                continue

            row = df.loc[ts]
            exits = check_exits(
                trade=trade,
                date=current_date,
                high=float(row["High"]),
                low=float(row["Low"]),
                close=float(row["Close"]),
                config=config,
            )
            for ex in exits:
                # Return sale proceeds to cash (price * shares minus commission)
                commission = config.commission_fixed if config.commission_fixed > 0 else ex.price * ex.shares * (config.commission_pct / 100)
                cash += ex.price * ex.shares - commission

        # Remove closed positions
        closed = [t for t in open_positions if t.status == "closed"]
        all_trades.extend(closed)
        open_positions = [t for t in open_positions if t.status == "open"]

        # Check entry conditions for each symbol
        if len(open_positions) >= config.max_positions:
            continue

        # Market regime filter: skip entries in bear market
        if config.market_filter_enabled and market_regime:
            is_bull = market_regime.get(current_date)
            if is_bull is not None and not is_bull:
                continue  # bear market, no new entries

        for symbol, df in aligned.items():
            if len(open_positions) >= config.max_positions:
                break

            # Skip if already have position in this symbol
            if any(t.symbol == symbol for t in open_positions):
                continue

            # Skip if already entered this symbol today (prevent same-day re-entry)
            if last_entry_date.get(symbol) == current_date:
                continue

            if ts not in df.index:
                continue

            row = df.loc[ts]
            idx = df.index.get_loc(ts)
            if idx < 1:
                continue
            prev_row = df.iloc[idx - 1]

            score, reasons = _check_entry(row, prev_row, config)
            if score >= config.min_entry_score:
                trade = _open_position(symbol, row, daily_frames.get(symbol), config, cash)
                if trade is not None and trade.shares >= 3:
                    cost = trade.entry_price * trade.shares
                    commission = config.commission_fixed if config.commission_fixed > 0 else cost * (config.commission_pct / 100)
                    if cost + commission <= cash:
                        cash -= (cost + commission)
                        open_positions.append(trade)
                        last_entry_date[symbol] = current_date
                        logger.debug(
                            f"GIRIS: {symbol} @ {trade.entry_price:.2f} x{trade.shares} "
                            f"skor={score} ({', '.join(reasons)})"
                        )

    # Force close remaining positions at last known price
    for trade in open_positions:
        symbol = trade.symbol
        if symbol in aligned and len(aligned[symbol]) > 0:
            last_row = aligned[symbol].iloc[-1]
            last_price = float(last_row["Close"])
            remaining = trade.remaining_shares
            close_trade_at_market(trade, last_date, last_price, config)
            # Return the sale proceeds to cash
            cash += last_price * remaining
    all_trades.extend(open_positions)

    # Final equity point
    equity_curve.append((last_date, cash))

    metrics = calculate_metrics(all_trades, equity_curve, config.initial_cash)

    logger.info(
        f"Backtest tamamlandi: {metrics.total_trades} trade, "
        f"Win rate: {metrics.win_rate:.1f}%, Getiri: {metrics.total_return_pct:+.1f}%"
    )

    return BacktestResult(
        trades=all_trades,
        metrics=metrics,
        equity_curve=equity_curve,
        params=_config_to_params(config),
    )


def _prepare_daily_only(daily: pd.DataFrame) -> pd.DataFrame:
    """Prepare daily data for daily-only mode by adding d_ prefix columns."""
    df = daily.copy()
    # Add d_ prefix columns (shifted by 1 to avoid look-ahead)
    for col in ["Close", "RSI_14", "RSI", "SMA_50", "SMA_200", "ATR", "ATR_14",
                "MACD", "Signal", "Vol_Avg_20", "BB_Upper", "BB_Lower"]:
        if col in df.columns:
            df[f"d_{col.lower()}"] = df[col].shift(1)
    return df.dropna(subset=[c for c in df.columns if c.startswith("d_")])


def _check_entry(row: pd.Series, prev_row: pd.Series, config: BacktestConfig) -> tuple[int, list[str]]:
    """Score-based multi-timeframe entry check.

    Mandatory: price > SMA 50 (trend filter)
    Score signals (any combination):
      - Daily RSI < threshold        → rsi_pullback_score pts
      - Daily MACD < signal line     → macd_negative_score pts
      - Price near BB lower band     → bb_lower_score pts
      - Hourly RSI reversal          → hourly_rsi_reversal_score pts
      - Volume > average             → volume_above_avg_score pts

    Returns (score, reasons). Entry if score >= config.min_entry_score.
    """
    score = 0
    reasons: list[str] = []

    # Daily indicators (from aligned data, prefixed with d_)
    d_close = _safe_float(row, "d_close")

    # MANDATORY: Trend filter — price must be above SMA (configurable period)
    sma_col = f"d_sma_{config.trend_sma_period}"
    d_sma = _safe_float(row, sma_col)
    # Fallback to sma_50 if specific period not available
    if d_sma is None:
        d_sma = _safe_float(row, "d_sma_50") or _safe_float(row, "d_sma_100") or _safe_float(row, "d_sma_200")
    if d_close is None or d_sma is None:
        return 0, []
    if d_close <= d_sma:
        return 0, []

    # Signal 1: Daily RSI pullback
    d_rsi = _safe_float(row, "d_rsi_14") or _safe_float(row, "d_rsi")
    if d_rsi is not None and d_rsi < config.rsi_pullback_threshold:
        score += config.rsi_pullback_score
        reasons.append(f"RSI={d_rsi:.0f}")

    # Signal 2: Daily MACD below signal (momentum cooling)
    d_macd = _safe_float(row, "d_macd")
    d_signal = _safe_float(row, "d_signal")
    if d_macd is not None and d_signal is not None and d_macd < d_signal:
        score += config.macd_negative_score
        reasons.append("MACD<Signal")

    # Signal 3: Price near lower Bollinger Band
    d_bb_lower = _safe_float(row, "d_bb_lower")
    if d_close is not None and d_bb_lower is not None and d_bb_lower > 0:
        distance_pct = (d_close - d_bb_lower) / d_bb_lower * 100
        if distance_pct < 3.0:  # within 3% of lower band
            score += config.bb_lower_score
            reasons.append(f"BB_alt={distance_pct:.1f}%")

    # Signal 4: RSI reversal (hourly in multi-tf mode, daily in daily-only mode)
    h_rsi = _safe_float(row, "RSI_14") or _safe_float(row, "RSI")
    prev_h_rsi = _safe_float(prev_row, "RSI_14") or _safe_float(prev_row, "RSI")
    if h_rsi is not None and prev_h_rsi is not None:
        threshold = config.hourly_rsi_reversal_threshold
        if prev_h_rsi < threshold and h_rsi >= threshold:
            score += config.hourly_rsi_reversal_score
            reasons.append(f"RSI_rev={prev_h_rsi:.0f}->{h_rsi:.0f}")

    # Signal 5: Volume above average
    volume = _safe_float(row, "Volume")
    vol_avg = _safe_float(row, "Vol_Avg_20")
    if volume is not None and vol_avg is not None and vol_avg > 0:
        if volume > vol_avg:
            score += config.volume_above_avg_score
            reasons.append(f"Hacim={volume / vol_avg:.1f}x")

    return score, reasons


def _open_position(
    symbol: str,
    row: pd.Series,
    daily_df: pd.DataFrame | None,
    config: BacktestConfig,
    cash: float,
) -> BacktestTrade | None:
    """Create a new trade with ATR-based TP/SL levels."""
    price = float(row["Close"])

    # Get ATR from daily data (more stable)
    atr = _safe_float(row, "d_atr") or _safe_float(row, "d_atr_14")
    if atr is None or atr <= 0:
        # Fallback to hourly ATR
        atr = _safe_float(row, "ATR") or _safe_float(row, "ATR_14")
        if atr is None or atr <= 0:
            return None

    sl = price - (atr * config.sl_atr_mult)
    tp1 = price + (atr * config.tp1_atr_mult)
    tp2 = price + (atr * config.tp2_atr_mult)

    # Position sizing
    risk_per_share = price - sl
    if risk_per_share <= 0:
        return None

    max_risk = cash * (config.risk_per_trade_pct / 100)
    shares = int(max_risk / risk_per_share)

    # Don't exceed 95% of cash
    if shares * price > cash * 0.95:
        shares = int((cash * 0.95) / price)

    if shares <= 0:
        return None

    date = str(row.name.date()) if hasattr(row.name, 'date') else str(row.name)[:10]

    return BacktestTrade(
        symbol=symbol,
        direction="long",
        entry_price=round(price, 2),
        entry_date=date,
        shares=shares,
        stop_loss=round(sl, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
    )


def _calculate_equity(
    cash: float,
    positions: list[BacktestTrade],
    aligned: dict[str, pd.DataFrame],
    current_ts: pd.Timestamp,
) -> float:
    """Calculate total equity (cash + open position values)."""
    equity = cash
    for trade in positions:
        if trade.symbol in aligned:
            df = aligned[trade.symbol]
            # Find the closest timestamp <= current
            mask = df.index <= current_ts
            if mask.any():
                last_price = float(df.loc[mask].iloc[-1]["Close"])
                equity += last_price * trade.remaining_shares
    return round(equity, 2)


def _safe_float(row: pd.Series, col: str) -> float | None:
    """Safely extract a float from a row."""
    try:
        val = row.get(col) if hasattr(row, 'get') else row[col] if col in row.index else None
        if val is not None and pd.notna(val):
            return float(val)
    except (KeyError, TypeError, ValueError):
        pass
    return None


def _config_to_params(config: BacktestConfig) -> dict:
    """Convert config to a dict for reporting."""
    return {
        "symbols": config.symbols,
        "period": f"{config.start_date} ~ {config.end_date}",
        "initial_cash": config.initial_cash,
        "market_filter": f"{config.market_index} > SMA{config.market_sma_period}" if config.market_filter_enabled else "Kapali",
        "min_entry_score": config.min_entry_score,
        "rsi_pullback": f"<{config.rsi_pullback_threshold} (+{config.rsi_pullback_score})",
        "macd_negative": f"+{config.macd_negative_score}",
        "bb_lower": f"+{config.bb_lower_score}",
        "h_rsi_reversal": f"<{config.hourly_rsi_reversal_threshold} (+{config.hourly_rsi_reversal_score})",
        "volume": f"+{config.volume_above_avg_score}",
        "sl_atr_mult": config.sl_atr_mult,
        "tp1_atr_mult": config.tp1_atr_mult,
        "trailing_stop_pct": config.trailing_stop_pct,
    }
