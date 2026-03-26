"""Signal detection and trade setup generation.

Uses borsapy only for data fetching (OHLCV, indicators).
All signal logic, scoring, and trade setup is implemented here from scratch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

import borsapy as bp
import pandas as pd

logger = logging.getLogger(__name__)


# ── Data Classes ──


@dataclass
class PriceLevel:
    price: float
    level_type: Literal["support", "resistance"]
    source: str  # 'swing', 'bollinger', 'sma50', 'sma200'
    strength: int = 1  # 1-5


@dataclass
class Signal:
    symbol: str
    signal_type: Literal["buy", "sell"]
    indicator: str
    strength: Literal["strong", "medium", "weak"]
    price: float
    reason: str
    indicator_values: dict[str, float] = field(default_factory=dict)
    score: int = 0


@dataclass
class TradeSetup:
    direction: Literal["long", "short", "neutral"]
    entry_price: float
    stop_loss: float | None = None
    stop_loss_pct: float | None = None
    take_profit_1: float | None = None
    take_profit_2: float | None = None
    take_profit_3: float | None = None
    risk_reward: float | None = None
    reasons: list[str] = field(default_factory=list)
    score: int = 0
    position_size: int = 0
    position_cost: float = 0.0
    risk_amount: float = 0.0


@dataclass
class AnalysisResult:
    symbol: str
    price: float
    signals: list[Signal]
    setup: TradeSetup | None
    levels: list[PriceLevel]
    indicators: dict[str, float]
    score: int


# ── Indicator Helpers ──


def _get_indicators(df: pd.DataFrame) -> dict[str, float]:
    """Extract latest indicator values from an OHLCV DataFrame with indicators added."""
    if df.empty:
        return {}

    last = df.iloc[-1]
    indicators = {}

    for col in df.columns:
        key = col.lower().replace(" ", "_")
        try:
            val = float(last[col])
            if pd.notna(val):
                indicators[key] = round(val, 4)
        except (ValueError, TypeError):
            continue

    return indicators


def _add_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all technical indicators to OHLCV DataFrame using borsapy."""
    df = bp.add_indicators(
        df,
        indicators=["rsi", "macd", "sma", "ema", "bollinger", "atr", "stochastic"],
        sma_period=20,
        ema_period=12,
    )
    # Additional SMAs for trend analysis
    df["SMA_50"] = bp.calculate_sma(df, period=50)
    df["SMA_100"] = bp.calculate_sma(df, period=100)
    df["SMA_200"] = bp.calculate_sma(df, period=200)
    df["EMA_26"] = bp.calculate_ema(df, period=26)
    return df


# ── Support / Resistance Detection ──


def detect_support_resistance(df: pd.DataFrame, lookback: int = 50) -> list[PriceLevel]:
    """Detect support and resistance levels from price action and indicators."""
    if len(df) < lookback:
        lookback = len(df)

    levels: list[PriceLevel] = []
    recent = df.tail(lookback)
    highs = recent["High"].values
    lows = recent["Low"].values

    # Swing lows (supports) - 5-bar pattern
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i - 1] and lows[i] < lows[i - 2] and \
           lows[i] < lows[i + 1] and lows[i] < lows[i + 2]:
            strength = 1
            for j in range(len(lows)):
                if j != i and abs(lows[j] - lows[i]) / lows[i] < 0.02:
                    strength += 1
            levels.append(PriceLevel(
                price=round(float(lows[i]), 2),
                level_type="support",
                source="swing",
                strength=min(strength, 5),
            ))

    # Swing highs (resistances) - 5-bar pattern
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i - 1] and highs[i] > highs[i - 2] and \
           highs[i] > highs[i + 1] and highs[i] > highs[i + 2]:
            strength = 1
            for j in range(len(highs)):
                if j != i and abs(highs[j] - highs[i]) / highs[i] < 0.02:
                    strength += 1
            levels.append(PriceLevel(
                price=round(float(highs[i]), 2),
                level_type="resistance",
                source="swing",
                strength=min(strength, 5),
            ))

    # Bollinger Bands as dynamic levels
    last = df.iloc[-1]
    if "BB_Lower" in df.columns and pd.notna(last.get("BB_Lower")):
        levels.append(PriceLevel(
            price=round(float(last["BB_Lower"]), 2),
            level_type="support",
            source="bollinger",
            strength=2,
        ))
    if "BB_Upper" in df.columns and pd.notna(last.get("BB_Upper")):
        levels.append(PriceLevel(
            price=round(float(last["BB_Upper"]), 2),
            level_type="resistance",
            source="bollinger",
            strength=2,
        ))

    # SMA 50 and 200 as dynamic levels
    current_price = float(last["Close"])
    for col, source, str_val in [("SMA_50", "sma50", 3), ("SMA_200", "sma200", 4)]:
        if col in df.columns and pd.notna(last.get(col)):
            sma_val = float(last[col])
            lt = "support" if sma_val < current_price else "resistance"
            levels.append(PriceLevel(
                price=round(sma_val, 2),
                level_type=lt,
                source=source,
                strength=str_val,
            ))

    # Deduplicate levels within 1% of each other, keep strongest
    levels = _deduplicate_levels(levels)

    # Sort: supports descending, resistances ascending
    supports = sorted([l for l in levels if l.level_type == "support"],
                      key=lambda x: x.price, reverse=True)[:3]
    resistances = sorted([l for l in levels if l.level_type == "resistance"],
                         key=lambda x: x.price)[:3]

    return supports + resistances


def _deduplicate_levels(levels: list[PriceLevel]) -> list[PriceLevel]:
    """Remove levels within 1% of each other, keeping the strongest."""
    if not levels:
        return []

    sorted_levels = sorted(levels, key=lambda x: x.price)
    result: list[PriceLevel] = [sorted_levels[0]]

    for level in sorted_levels[1:]:
        if abs(level.price - result[-1].price) / result[-1].price < 0.01:
            if level.strength > result[-1].strength:
                result[-1] = level
        else:
            result.append(level)

    return result


# ── Signal Detection ──


def detect_buy_signals(df: pd.DataFrame, symbol: str = "") -> list[Signal]:
    """Detect buy signals from OHLCV data with indicators."""
    if len(df) < 3:
        return []

    signals: list[Signal] = []
    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(last["Close"])
    ind = _get_indicators(df)

    # RSI crosses above 30 (oversold recovery)
    rsi = ind.get("rsi_14") or ind.get("rsi")
    prev_rsi = float(prev.get("RSI_14", prev.get("RSI", 50))) if "RSI_14" in df.columns or "RSI" in df.columns else None

    if rsi is not None and prev_rsi is not None:
        if prev_rsi < 30 and rsi >= 30:
            strength = "strong" if prev_rsi < 25 else "medium"
            signals.append(Signal(
                symbol=symbol,
                signal_type="buy",
                indicator="rsi",
                strength=strength,
                price=price,
                reason=f"RSI asiri satim bolgesinden cikti ({prev_rsi:.0f} -> {rsi:.0f})",
                indicator_values=ind,
            ))
        elif rsi < 30:
            signals.append(Signal(
                symbol=symbol,
                signal_type="buy",
                indicator="rsi",
                strength="weak",
                price=price,
                reason=f"RSI asiri satim bolgesinde ({rsi:.0f})",
                indicator_values=ind,
            ))

    # MACD crosses above signal
    macd_val = ind.get("macd")
    signal_val = ind.get("signal")
    if macd_val is not None and signal_val is not None:
        prev_macd = float(prev.get("MACD", 0)) if "MACD" in df.columns else None
        prev_signal = float(prev.get("Signal", 0)) if "Signal" in df.columns else None
        if prev_macd is not None and prev_signal is not None:
            if prev_macd <= prev_signal and macd_val > signal_val:
                signals.append(Signal(
                    symbol=symbol,
                    signal_type="buy",
                    indicator="macd",
                    strength="medium",
                    price=price,
                    reason="MACD sinyal cizgisini yukari kesti",
                    indicator_values=ind,
                ))

    # Price touches lower Bollinger Band with RSI confirmation
    bb_lower = ind.get("bb_lower")
    if bb_lower is not None and rsi is not None:
        if price <= bb_lower * 1.01 and rsi < 35:
            signals.append(Signal(
                symbol=symbol,
                signal_type="buy",
                indicator="bollinger",
                strength="strong",
                price=price,
                reason=f"Fiyat BB alt bandinda ve RSI dusuk ({rsi:.0f})",
                indicator_values=ind,
            ))

    # Stochastic crosses above 20
    stoch_k = ind.get("stoch_k")
    if stoch_k is not None and "Stoch_K" in df.columns:
        prev_stoch = float(prev.get("Stoch_K", 50))
        if prev_stoch < 20 and stoch_k >= 20:
            signals.append(Signal(
                symbol=symbol,
                signal_type="buy",
                indicator="stochastic",
                strength="medium",
                price=price,
                reason=f"Stochastic asiri satim bolgesinden cikti ({prev_stoch:.0f} -> {stoch_k:.0f})",
                indicator_values=ind,
            ))

    return signals


def detect_sell_signals(df: pd.DataFrame, symbol: str = "") -> list[Signal]:
    """Detect sell signals from OHLCV data with indicators."""
    if len(df) < 3:
        return []

    signals: list[Signal] = []
    last = df.iloc[-1]
    prev = df.iloc[-2]
    price = float(last["Close"])
    ind = _get_indicators(df)

    # RSI crosses below 70 (overbought reversal)
    rsi = ind.get("rsi_14") or ind.get("rsi")
    prev_rsi = float(prev.get("RSI_14", prev.get("RSI", 50))) if "RSI_14" in df.columns or "RSI" in df.columns else None

    if rsi is not None and prev_rsi is not None:
        if prev_rsi > 70 and rsi <= 70:
            strength = "strong" if prev_rsi > 75 else "medium"
            signals.append(Signal(
                symbol=symbol,
                signal_type="sell",
                indicator="rsi",
                strength=strength,
                price=price,
                reason=f"RSI asiri alim bolgesinden dondü ({prev_rsi:.0f} -> {rsi:.0f})",
                indicator_values=ind,
            ))

    # MACD crosses below signal
    macd_val = ind.get("macd")
    signal_val = ind.get("signal")
    if macd_val is not None and signal_val is not None:
        prev_macd = float(prev.get("MACD", 0)) if "MACD" in df.columns else None
        prev_signal = float(prev.get("Signal", 0)) if "Signal" in df.columns else None
        if prev_macd is not None and prev_signal is not None:
            if prev_macd >= prev_signal and macd_val < signal_val:
                signals.append(Signal(
                    symbol=symbol,
                    signal_type="sell",
                    indicator="macd",
                    strength="medium",
                    price=price,
                    reason="MACD sinyal cizgisini asagi kesti",
                    indicator_values=ind,
                ))

    # Price touches upper Bollinger Band with RSI confirmation
    bb_upper = ind.get("bb_upper")
    if bb_upper is not None and rsi is not None:
        if price >= bb_upper * 0.99 and rsi > 65:
            signals.append(Signal(
                symbol=symbol,
                signal_type="sell",
                indicator="bollinger",
                strength="strong",
                price=price,
                reason=f"Fiyat BB ust bandinda ve RSI yuksek ({rsi:.0f})",
                indicator_values=ind,
            ))

    # Stochastic crosses below 80
    stoch_k = ind.get("stoch_k")
    if stoch_k is not None and "Stoch_K" in df.columns:
        prev_stoch = float(prev.get("Stoch_K", 50))
        if prev_stoch > 80 and stoch_k <= 80:
            signals.append(Signal(
                symbol=symbol,
                signal_type="sell",
                indicator="stochastic",
                strength="medium",
                price=price,
                reason=f"Stochastic asiri alim bolgesinden dondü ({prev_stoch:.0f} -> {stoch_k:.0f})",
                indicator_values=ind,
            ))

    return signals


# ── Composite Scoring ──


def calculate_score(indicators: dict[str, float]) -> int:
    """Calculate composite sentiment score from -100 to +100."""
    score = 0

    # RSI (weight: ±25)
    rsi = indicators.get("rsi_14") or indicators.get("rsi")
    if rsi is not None:
        if rsi < 30:
            score += 25
        elif rsi < 35:
            score += 15
        elif rsi < 45:
            score += 5
        elif rsi > 70:
            score -= 25
        elif rsi > 65:
            score -= 15
        elif rsi > 55:
            score -= 5

    # MACD vs Signal (weight: ±20)
    macd = indicators.get("macd")
    signal = indicators.get("signal")
    if macd is not None and signal is not None:
        if macd > signal:
            score += 20
        else:
            score -= 20

    # Stochastic (weight: ±15)
    stoch_k = indicators.get("stoch_k")
    if stoch_k is not None:
        if stoch_k < 20:
            score += 15
        elif stoch_k > 80:
            score -= 15

    # Price vs SMA 20 (weight: ±10)
    close = indicators.get("close")
    sma_20 = indicators.get("sma_20") or indicators.get("sma")
    if close is not None and sma_20 is not None:
        if close > sma_20:
            score += 10
        else:
            score -= 10

    # Price vs SMA 50 (weight: ±10)
    sma_50 = indicators.get("sma_50")
    if close is not None and sma_50 is not None:
        if close > sma_50:
            score += 10
        else:
            score -= 10

    # Price vs SMA 200 (weight: ±15)
    sma_200 = indicators.get("sma_200")
    if close is not None and sma_200 is not None:
        if close > sma_200:
            score += 15
        else:
            score -= 15

    # Golden / Death Cross (weight: ±10)
    if sma_50 is not None and sma_200 is not None:
        if sma_50 > sma_200:
            score += 10
        else:
            score -= 10

    # Bollinger Band position (weight: ±10)
    bb_upper = indicators.get("bb_upper")
    bb_lower = indicators.get("bb_lower")
    if close is not None and bb_upper is not None and bb_lower is not None:
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            bb_position = (close - bb_lower) / bb_range
            if bb_position < 0.2:
                score += 10
            elif bb_position > 0.8:
                score -= 10

    return max(-100, min(100, score))


# ── Trade Setup ──


def build_trade_setup(
    df: pd.DataFrame,
    levels: list[PriceLevel],
    indicators: dict[str, float],
    score: int,
    available_cash: float,
    risk_per_trade_pct: float = 2.0,
    sl_atr_mult: float = 2.0,
    tp1_atr_mult: float = 1.5,
    tp2_atr_mult: float = 3.0,
    tp3_atr_mult: float = 4.5,
    use_sr_levels: bool = True,
) -> TradeSetup:
    """Build a complete trade setup with entry, TP, SL, and position sizing."""
    price = float(df.iloc[-1]["Close"])
    atr = indicators.get("atr") or indicators.get("atr_14")

    # Determine direction from score
    if score >= 30:
        direction = "long"
    elif score <= -30:
        direction = "short"
    else:
        direction = "neutral"

    if direction == "neutral" or atr is None:
        return TradeSetup(direction=direction, entry_price=price, score=score)

    # ATR-based levels
    if direction == "long":
        sl = price - (atr * sl_atr_mult)
        tp1 = price + (atr * tp1_atr_mult)
        tp2 = price + (atr * tp2_atr_mult)
        tp3 = price + (atr * tp3_atr_mult)
    else:
        sl = price + (atr * sl_atr_mult)
        tp1 = price - (atr * tp1_atr_mult)
        tp2 = price - (atr * tp2_atr_mult)
        tp3 = price - (atr * tp3_atr_mult)

    # Override with S/R levels if available
    if use_sr_levels and levels:
        supports = sorted([l for l in levels if l.level_type == "support"],
                          key=lambda x: x.price, reverse=True)
        resistances = sorted([l for l in levels if l.level_type == "resistance"],
                             key=lambda x: x.price)

        if direction == "long":
            # SL at nearest support below price
            below_supports = [s for s in supports if s.price < price]
            if below_supports:
                sl = below_supports[0].price * 0.98

            # TPs at resistance levels
            above_resistances = [r for r in resistances if r.price > price]
            if len(above_resistances) >= 1:
                tp1 = above_resistances[0].price
            if len(above_resistances) >= 2:
                tp2 = above_resistances[1].price
            if len(above_resistances) >= 3:
                tp3 = above_resistances[2].price

    # Calculate percentages
    sl_pct = abs(price - sl) / price * 100

    # Risk/Reward ratio
    risk = abs(price - sl)
    reward = abs(tp1 - price)
    rr = round(reward / risk, 2) if risk > 0 else 0

    # Position sizing based on risk
    total_portfolio = available_cash  # Simplified: use cash as base
    max_risk = total_portfolio * (risk_per_trade_pct / 100)
    risk_per_share = abs(price - sl)

    if risk_per_share > 0:
        shares = int(max_risk / risk_per_share)
        position_cost = shares * price
        # Don't exceed available cash
        if position_cost > available_cash * 0.95:
            shares = int((available_cash * 0.95) / price)
            position_cost = shares * price
    else:
        shares = 0
        position_cost = 0

    # Build reasons
    reasons = _build_reasons(indicators, score, direction)

    return TradeSetup(
        direction=direction,
        entry_price=round(price, 2),
        stop_loss=round(sl, 2),
        stop_loss_pct=round(sl_pct, 2),
        take_profit_1=round(tp1, 2),
        take_profit_2=round(tp2, 2),
        take_profit_3=round(tp3, 2),
        risk_reward=rr,
        reasons=reasons,
        score=score,
        position_size=shares,
        position_cost=round(position_cost, 2),
        risk_amount=round(shares * risk_per_share, 2) if shares > 0 else 0,
    )


def _build_reasons(indicators: dict[str, float], score: int, direction: str) -> list[str]:
    """Build a list of reasons supporting the trade direction."""
    reasons = []
    rsi = indicators.get("rsi_14") or indicators.get("rsi")
    macd = indicators.get("macd")
    signal = indicators.get("signal")
    stoch_k = indicators.get("stoch_k")
    close = indicators.get("close")
    sma_50 = indicators.get("sma_50")
    sma_200 = indicators.get("sma_200")

    if direction == "long":
        if rsi is not None and rsi < 35:
            reasons.append(f"RSI dusuk ({rsi:.0f})")
        if macd is not None and signal is not None and macd > signal:
            reasons.append("MACD pozitif")
        if stoch_k is not None and stoch_k < 25:
            reasons.append(f"Stochastic dusuk ({stoch_k:.0f})")
        if close is not None and sma_50 is not None and close > sma_50:
            reasons.append("Fiyat SMA50 uzerinde")
        if sma_50 is not None and sma_200 is not None and sma_50 > sma_200:
            reasons.append("Golden Cross aktif")
    else:
        if rsi is not None and rsi > 65:
            reasons.append(f"RSI yuksek ({rsi:.0f})")
        if macd is not None and signal is not None and macd < signal:
            reasons.append("MACD negatif")
        if stoch_k is not None and stoch_k > 75:
            reasons.append(f"Stochastic yuksek ({stoch_k:.0f})")
        if close is not None and sma_50 is not None and close < sma_50:
            reasons.append("Fiyat SMA50 altinda")

    return reasons[:6]


# ── Full Analysis ──


def analyze_symbol(
    symbol: str,
    period: str = "6mo",
    interval: str = "1d",
    available_cash: float = 0,
    strategy_params: dict | None = None,
) -> AnalysisResult | None:
    """Run full technical analysis on a symbol.

    Returns AnalysisResult with signals, score, trade setup, and S/R levels.
    Returns None if data is insufficient.
    """
    try:
        ticker = bp.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)

        if df is None or len(df) < 50:
            logger.warning(f"{symbol}: Yetersiz veri ({len(df) if df is not None else 0} bar)")
            return None

        # Add indicators
        df = _add_all_indicators(df)

        # Get latest indicator values
        indicators = _get_indicators(df)
        price = float(df.iloc[-1]["Close"])

        # Detect S/R levels
        levels = detect_support_resistance(df)

        # Detect signals
        buy_signals = detect_buy_signals(df, symbol)
        sell_signals = detect_sell_signals(df, symbol)
        all_signals = buy_signals + sell_signals

        # Calculate composite score
        score = calculate_score(indicators)

        # Build trade setup
        params = strategy_params or {}
        setup = build_trade_setup(
            df=df,
            levels=levels,
            indicators=indicators,
            score=score,
            available_cash=available_cash,
            risk_per_trade_pct=params.get("risk_per_trade_pct", 2.0),
            sl_atr_mult=params.get("sl_atr_mult", 2.0),
            tp1_atr_mult=params.get("tp1_atr_mult", 1.5),
            tp2_atr_mult=params.get("tp2_atr_mult", 3.0),
            tp3_atr_mult=params.get("tp3_atr_mult", 4.5),
            use_sr_levels=params.get("use_sr_levels", True),
        )

        return AnalysisResult(
            symbol=symbol,
            price=price,
            signals=all_signals,
            setup=setup,
            levels=levels,
            indicators=indicators,
            score=score,
        )

    except Exception:
        logger.exception(f"{symbol}: Analiz hatasi")
        return None
