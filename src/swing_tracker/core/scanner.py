"""BIST scanner with score-based multi-timeframe entry strategy.

Uses the same entry logic proven in backtesting:
- Market filter: XU100 > SMA 50
- Score-based entry with RSI, MACD, BB, volume signals
- Multi-timeframe: daily indicators + hourly entry trigger
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import borsapy as bp
import pandas as pd

from swing_tracker.config import Config
from swing_tracker.core.signals import (
    AnalysisResult,
    TradeSetup,
    _add_all_indicators,
    _get_indicators,
    build_trade_setup,
    detect_support_resistance,
)
from swing_tracker.core.strategy import get_strategy, get_strategy_params
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class ScoredCandidate:
    symbol: str
    price: float
    entry_score: int
    reasons: list[str]
    analysis: AnalysisResult
    daily_rsi: float | None = None
    hourly_rsi: float | None = None


@dataclass
class ScanResult:
    candidates: list[ScoredCandidate]
    scanned_count: int
    filtered_count: int
    market_bullish: bool = True
    market_index_value: float = 0
    market_sma_value: float = 0


class Scanner:
    def __init__(self, repo: Repository, config: Config):
        self._repo = repo
        self._config = config
        self._market_bullish: bool | None = None

    def check_market_regime(self) -> bool:
        """Check if market index is above SMA 50 (bull market)."""
        try:
            ticker = bp.Ticker(self._config.scanner.universe)
            df = ticker.history(period="6mo", interval="1d")
            if df is None or len(df) < 50:
                logger.warning("Endeks verisi yetersiz, piyasa filtresi devre disi")
                return True

            sma_period = 100  # config'den alinabilir, varsayilan 100
            df[f"SMA_{sma_period}"] = bp.calculate_sma(df, period=sma_period)
            last = df.iloc[-1]
            close = float(last["Close"])
            sma = float(last[f"SMA_{sma_period}"]) if pd.notna(last.get(f"SMA_{sma_period}")) else None

            if sma is None:
                return True

            self._market_bullish = close > sma
            status = "BOGA" if self._market_bullish else "AYI"
            logger.info(
                f"Piyasa rejimi: {self._config.scanner.universe} "
                f"{close:.0f} vs SMA50 {sma:.0f} → {status}"
            )
            return self._market_bullish
        except Exception:
            logger.exception("Piyasa rejimi kontrol hatasi")
            return True

    def _score_symbol(
        self, symbol: str, available_cash: float, strategy_params: dict
    ) -> ScoredCandidate | None:
        """Score a single symbol using multi-timeframe analysis."""
        try:
            ticker = bp.Ticker(symbol)

            # Daily data
            df_daily = ticker.history(period="6mo", interval="1d")
            if df_daily is None or len(df_daily) < 50:
                return None
            df_daily = _add_all_indicators(df_daily)
            df_daily["Vol_Avg_20"] = df_daily["Volume"].rolling(20).mean()

            # Hourly data (last 5 days for entry timing)
            df_hourly = ticker.history(period="5d", interval="1h")

            last_daily = df_daily.iloc[-1]
            prev_daily = df_daily.iloc[-2]
            price = float(last_daily["Close"])

            # MANDATORY: trend filter — price > SMA 50
            sma_50 = float(last_daily["SMA_50"]) if pd.notna(last_daily.get("SMA_50")) else None
            if sma_50 is None or price <= sma_50:
                return None

            score = 0
            reasons: list[str] = []
            daily_rsi = None
            hourly_rsi = None

            # Signal 1: Daily RSI pullback
            rsi = float(last_daily.get("RSI_14", 50)) if pd.notna(last_daily.get("RSI_14")) else None
            if rsi is not None and rsi < 45:
                score += 2
                reasons.append(f"RSI={rsi:.0f}")
                daily_rsi = rsi

            # Signal 2: MACD below signal
            macd = float(last_daily.get("MACD", 0)) if pd.notna(last_daily.get("MACD")) else None
            signal_val = float(last_daily.get("Signal", 0)) if pd.notna(last_daily.get("Signal")) else None
            if macd is not None and signal_val is not None and macd < signal_val:
                score += 1
                reasons.append("MACD<Signal")

            # Signal 3: Price near lower Bollinger Band
            bb_lower = float(last_daily.get("BB_Lower", 0)) if pd.notna(last_daily.get("BB_Lower")) else None
            if bb_lower is not None and bb_lower > 0:
                distance_pct = (price - bb_lower) / bb_lower * 100
                if distance_pct < 3.0:
                    score += 2
                    reasons.append(f"BB_alt={distance_pct:.1f}%")

            # Signal 4: Hourly RSI reversal
            if df_hourly is not None and len(df_hourly) >= 3:
                df_hourly = _add_all_indicators(df_hourly)
                h_last = df_hourly.iloc[-1]
                h_prev = df_hourly.iloc[-2]

                h_rsi = float(h_last.get("RSI_14", 50)) if pd.notna(h_last.get("RSI_14")) else None
                h_prev_rsi = float(h_prev.get("RSI_14", 50)) if pd.notna(h_prev.get("RSI_14")) else None

                if h_rsi is not None and h_prev_rsi is not None:
                    hourly_rsi = h_rsi
                    if h_prev_rsi < 40 and h_rsi >= 40:
                        score += 2
                        reasons.append(f"H_RSI={h_prev_rsi:.0f}->{h_rsi:.0f}")

            # Signal 5: Volume above average
            volume = float(last_daily.get("Volume", 0))
            vol_avg = float(last_daily.get("Vol_Avg_20", 0)) if pd.notna(last_daily.get("Vol_Avg_20")) else None
            if vol_avg and vol_avg > 0 and volume > vol_avg:
                score += 1
                reasons.append(f"Hacim={volume / vol_avg:.1f}x")

            if score < 5:
                return None

            # Build full analysis for TP/SL levels
            indicators = _get_indicators(df_daily)
            levels = detect_support_resistance(df_daily)
            setup = build_trade_setup(
                df=df_daily,
                levels=levels,
                indicators=indicators,
                score=score * 10,  # scale to signals.py scoring
                available_cash=available_cash,
                **{k: v for k, v in strategy_params.items()
                   if k in ("risk_per_trade_pct", "sl_atr_mult", "tp1_atr_mult",
                            "tp2_atr_mult", "tp3_atr_mult", "use_sr_levels")},
            )

            analysis = AnalysisResult(
                symbol=symbol,
                price=price,
                signals=[],
                setup=setup,
                levels=levels,
                indicators=indicators,
                score=score * 10,
            )

            return ScoredCandidate(
                symbol=symbol,
                price=price,
                entry_score=score,
                reasons=reasons,
                analysis=analysis,
                daily_rsi=daily_rsi,
                hourly_rsi=hourly_rsi,
            )

        except Exception:
            logger.warning(f"{symbol}: Analiz hatasi")
            return None

    def run_quick_scan(self, available_cash: float = 0) -> ScanResult:
        """Quick scan with score-based multi-timeframe strategy.

        1. Check market regime (XU100 > SMA50)
        2. Pre-filter with bp.scan()
        3. Score each candidate with multi-TF analysis
        4. Return candidates with score >= 5
        """
        strategy = get_strategy(self._config)
        params = get_strategy_params(strategy)
        universe = self._config.scanner.universe

        # Market regime check
        is_bull = self.check_market_regime()
        if not is_bull:
            logger.info("Ayi piyasasi — quick scan atlanıyor")
            return ScanResult(
                candidates=[], scanned_count=0, filtered_count=0,
                market_bullish=False,
            )

        # Pre-filter using borsapy scanner
        candidate_symbols: set[str] = set()
        for prefilter in self._config.scanner.prefilters:
            try:
                result = bp.scan(universe, prefilter, interval="1d")
                if result is not None and not result.empty:
                    symbols = result.index.tolist() if result.index.name else result.iloc[:, 0].tolist()
                    candidate_symbols.update(str(s) for s in symbols)
                    logger.info(f"Pre-filter '{prefilter}': {len(symbols)} sonuc")
            except Exception:
                logger.warning(f"Pre-filter hatasi: {prefilter}")

        logger.info(f"Toplam {len(candidate_symbols)} benzersiz aday bulundu")

        # Score each candidate
        candidates: list[ScoredCandidate] = []
        for symbol in candidate_symbols:
            scored = self._score_symbol(symbol, available_cash, params)
            if scored is not None:
                candidates.append(scored)
                self._log_scored_signal(scored)

        candidates.sort(key=lambda x: x.entry_score, reverse=True)

        logger.info(
            f"Quick scan tamamlandi: {len(candidate_symbols)} tarandi, "
            f"{len(candidates)} sinyal (skor >= 5)"
        )

        return ScanResult(
            candidates=candidates,
            scanned_count=len(candidate_symbols),
            filtered_count=len(candidates),
            market_bullish=True,
        )

    def run_deep_scan(self, available_cash: float = 0) -> ScanResult:
        """Deep scan: score-based analysis on entire universe."""
        strategy = get_strategy(self._config)
        params = get_strategy_params(strategy)
        universe = self._config.scanner.universe

        # Market regime check
        is_bull = self.check_market_regime()

        # Get all symbols in universe
        try:
            index = bp.Index(universe)
            all_symbols = index.components
            if isinstance(all_symbols, list) and all_symbols:
                all_symbols = [s["symbol"] if isinstance(s, dict) else str(s) for s in all_symbols]
            else:
                logger.error(f"Universe bilesenleri alinamadi: {universe}")
                return ScanResult(candidates=[], scanned_count=0, filtered_count=0)
        except Exception:
            logger.exception(f"Universe yuklenemedi: {universe}")
            return ScanResult(candidates=[], scanned_count=0, filtered_count=0)

        logger.info(f"Deep scan basliyor: {len(all_symbols)} sembol ({universe})")

        candidates: list[ScoredCandidate] = []
        for i, symbol in enumerate(all_symbols):
            if (i + 1) % 20 == 0:
                logger.info(f"Ilerleme: {i + 1}/{len(all_symbols)}")

            # In bear market, still scan but mark as such
            scored = self._score_symbol(str(symbol), available_cash, params)
            if scored is not None:
                candidates.append(scored)
                self._log_scored_signal(scored)

        candidates.sort(key=lambda x: x.entry_score, reverse=True)

        logger.info(
            f"Deep scan tamamlandi: {len(all_symbols)} tarandi, "
            f"{len(candidates)} sinyal"
        )

        return ScanResult(
            candidates=candidates[:10],
            scanned_count=len(all_symbols),
            filtered_count=len(candidates),
            market_bullish=is_bull,
        )

    def _log_scored_signal(self, scored: ScoredCandidate) -> None:
        """Log scored signal to database."""
        self._repo.log_signal(
            symbol=scored.symbol,
            signal_type="buy",
            indicator="multi_tf_score",
            strength="strong" if scored.entry_score >= 6 else "medium",
            price_at_signal=scored.price,
            indicator_values={"entry_score": scored.entry_score, "reasons": ", ".join(scored.reasons)},
            score=scored.entry_score * 10,
        )
