"""BIST scanner with score-based multi-timeframe entry strategy.

Uses the same entry logic proven in backtesting:
- Market filter: XU100 > SMA 50
- Score-based entry with RSI, MACD, BB, volume signals
- Multi-timeframe: daily indicators + hourly entry trigger
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import borsapy as bp
import pandas as pd

from swing_tracker.config import Config
from swing_tracker.core.ohlcv_cache import get_ohlcv
from swing_tracker.core.signals import (
    AnalysisResult,
    TradeSetup,
    _add_all_indicators,
    _get_indicators,
    build_trade_setup,
    detect_support_resistance,
)
from swing_tracker.core.strategy import get_strategy, get_strategy_params
from swing_tracker.core.universe import UniverseBuilder
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
    usd_price: float | None = None
    usd_trend_ok: bool | None = None  # price > SMA100 in USD terms


@dataclass
class ScanResult:
    candidates: list[ScoredCandidate]
    scanned_count: int
    filtered_count: int
    market_bullish: bool = True
    market_index_value: float = 0
    market_sma_value: float = 0


class Scanner:
    def __init__(
        self,
        repo: Repository,
        config: Config,
        universe_builder: UniverseBuilder | None = None,
    ):
        self._repo = repo
        self._config = config
        self._universe_builder = universe_builder
        self._market_bullish: bool | None = None
        self._usdtry_rate: float | None = None
        workers = max(1, int(getattr(config.cache, "scanner_max_workers", 10)))
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scanner")

    def close(self) -> None:
        """Graceful shutdown of the fetch worker pool."""
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _fetch_daily(self, symbol: str, period: str = "6mo") -> pd.DataFrame | None:
        # Scanner 6mo icin 50+ bar istiyor, cache yetersizse full fetch tetiklensin.
        min_bars = 100 if period in ("6mo", "1y", "2y") else 0
        return get_ohlcv(
            symbol,
            interval="1d",
            period=period,
            repo=self._repo,
            cache_cfg=self._config.cache,
            min_bars=min_bars,
        )

    def _fetch_hourly(self, symbol: str, period: str = "5d") -> pd.DataFrame | None:
        min_bars = 20 if period in ("5d", "10d") else 0
        return get_ohlcv(
            symbol,
            interval="1h",
            period=period,
            repo=self._repo,
            cache_cfg=self._config.cache,
            min_bars=min_bars,
        )

    def _get_usdtry(self) -> float | None:
        """Get current USDTRY rate."""
        if self._usdtry_rate is not None:
            return self._usdtry_rate
        try:
            import yfinance as yf
            fx = yf.Ticker("USDTRY=X").history(period="5d", interval="1d")
            if fx is not None and len(fx) > 0:
                fx.index = fx.index.tz_localize(None) if fx.index.tz is not None else fx.index
                self._usdtry_rate = float(fx.iloc[-1]["Close"])
                return self._usdtry_rate
        except Exception:
            logger.warning("USDTRY kuru alinamadi")
        return None

    def check_market_regime(self) -> bool:
        """Check if market regime index is above SMA 100 (bull market)."""
        regime_index = self._config.scanner.market_regime_index
        try:
            df = get_ohlcv(
                regime_index,
                interval="1d",
                period="6mo",
                repo=self._repo,
                cache_cfg=self._config.cache,
                ttl_override_minutes=self._config.cache.regime_ttl_minutes,
            )
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
                f"Piyasa rejimi: {regime_index} "
                f"{close:.0f} vs SMA100 {sma:.0f} → {status}"
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
            # Daily data (cached)
            df_daily = self._fetch_daily(symbol, period="6mo")
            if df_daily is None or len(df_daily) < 50:
                return None
            df_daily = _add_all_indicators(df_daily)
            df_daily["Vol_Avg_20"] = df_daily["Volume"].rolling(20).mean()

            # Hourly data (cached)
            df_hourly = self._fetch_hourly(symbol, period="5d")

            last_daily = df_daily.iloc[-1]
            prev_daily = df_daily.iloc[-2]
            price = float(last_daily["Close"])

            # MANDATORY: trend filter — price > SMA 100
            if "SMA_100" not in df_daily.columns:
                df_daily["SMA_100"] = bp.calculate_sma(df_daily, period=100)
                last_daily = df_daily.iloc[-1]
            sma_100 = float(last_daily["SMA_100"]) if pd.notna(last_daily.get("SMA_100")) else None
            if sma_100 is None or price <= sma_100:
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

            if score < 4:
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

            # USD trend check
            usd_price = None
            usd_trend_ok = None
            rate = self._get_usdtry()
            if rate and rate > 0:
                usd_price = round(price / rate, 4)
                # Calculate USD SMA 100
                usd_closes = df_daily["Close"] / rate
                usd_sma_100 = usd_closes.rolling(100).mean().iloc[-1]
                if pd.notna(usd_sma_100):
                    usd_trend_ok = usd_price > usd_sma_100

            return ScoredCandidate(
                symbol=symbol,
                price=price,
                entry_score=score,
                reasons=reasons,
                analysis=analysis,
                daily_rsi=daily_rsi,
                hourly_rsi=hourly_rsi,
                usd_price=usd_price,
                usd_trend_ok=usd_trend_ok,
            )

        except Exception:
            logger.exception(f"{symbol}: Analiz hatasi")
            return None

    def _score_symbol_all(self, symbol: str) -> dict | None:
        """Score a symbol returning all details including trend fail status."""
        try:
            df_daily = self._fetch_daily(symbol, period="6mo")
            if df_daily is None or len(df_daily) < 50:
                return None
            df_daily = _add_all_indicators(df_daily)
            df_daily["Vol_Avg_20"] = df_daily["Volume"].rolling(20).mean()

            last_daily = df_daily.iloc[-1]
            price = float(last_daily["Close"])

            # Trend check
            if "SMA_100" not in df_daily.columns:
                df_daily["SMA_100"] = bp.calculate_sma(df_daily, period=100)
                last_daily = df_daily.iloc[-1]
            sma_100 = float(last_daily["SMA_100"]) if pd.notna(last_daily.get("SMA_100")) else None
            trend_ok = sma_100 is not None and price > sma_100

            # Score signals
            score = 0
            reasons: list[str] = []

            rsi = float(last_daily.get("RSI_14", 50)) if pd.notna(last_daily.get("RSI_14")) else None
            if rsi is not None and rsi < 45:
                score += 2
                reasons.append(f"RSI={rsi:.0f}(+2)")

            macd = float(last_daily.get("MACD", 0)) if pd.notna(last_daily.get("MACD")) else None
            signal_val = float(last_daily.get("Signal", 0)) if pd.notna(last_daily.get("Signal")) else None
            if macd is not None and signal_val is not None and macd < signal_val:
                score += 1
                reasons.append("MACD<Sig(+1)")

            bb_lower = float(last_daily.get("BB_Lower", 0)) if pd.notna(last_daily.get("BB_Lower")) else None
            if bb_lower is not None and bb_lower > 0:
                dist = (price - bb_lower) / bb_lower * 100
                if dist < 3.0:
                    score += 2
                    reasons.append(f"BB={dist:.1f}%(+2)")

            # Hourly RSI
            df_hourly = self._fetch_hourly(symbol, period="5d")
            if df_hourly is not None and len(df_hourly) >= 3:
                df_hourly = _add_all_indicators(df_hourly)
                h_last = df_hourly.iloc[-1]
                h_prev = df_hourly.iloc[-2]
                h_rsi = float(h_last.get("RSI_14", 50)) if pd.notna(h_last.get("RSI_14")) else None
                h_prev_rsi = float(h_prev.get("RSI_14", 50)) if pd.notna(h_prev.get("RSI_14")) else None
                if h_rsi is not None and h_prev_rsi is not None and h_prev_rsi < 40 and h_rsi >= 40:
                    score += 2
                    reasons.append(f"H_RSI={h_prev_rsi:.0f}->{h_rsi:.0f}(+2)")

            volume = float(last_daily.get("Volume", 0))
            vol_avg = float(last_daily.get("Vol_Avg_20", 0)) if pd.notna(last_daily.get("Vol_Avg_20")) else None
            if vol_avg and vol_avg > 0 and volume > vol_avg:
                score += 1
                reasons.append(f"Hacim={volume / vol_avg:.1f}x(+1)")

            return {
                "symbol": symbol,
                "price": price,
                "score": score,
                "reasons": reasons,
                "trend_ok": trend_ok,
                "sma_100": sma_100,
            }
        except Exception:
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
                    if "symbol" in result.columns:
                        symbols = result["symbol"].tolist()
                    elif result.index.name == "symbol":
                        symbols = result.index.tolist()
                    else:
                        logger.warning(f"Pre-filter sonucunda 'symbol' kolonu yok: {result.columns.tolist()}")
                        continue
                    candidate_symbols.update(str(s) for s in symbols)
                    logger.info(f"Pre-filter '{prefilter}': {len(symbols)} sonuc")
            except Exception:
                logger.warning(f"Pre-filter hatasi: {prefilter}")

        # Likidite filtresi: sadece liquid_universe ile kesisim
        if self._universe_builder is not None and self._config.liquidity.enabled:
            liquid = set(self._universe_builder.get_liquid_symbols())
            if liquid:
                before = len(candidate_symbols)
                candidate_symbols &= liquid
                logger.info(
                    f"Likidite filtresi: {before} aday → {len(candidate_symbols)} likit"
                )

        logger.info(f"Toplam {len(candidate_symbols)} benzersiz aday bulundu")

        # Score each candidate in parallel
        candidates: list[ScoredCandidate] = []
        results = self._executor.map(
            lambda s: self._score_symbol(s, available_cash, params),
            list(candidate_symbols),
        )
        for scored in results:
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

        # Likit evren: UniverseBuilder'dan oku, yoksa fallback bp.Index(universe)
        if self._universe_builder is not None and self._config.liquidity.enabled:
            all_symbols = self._universe_builder.get_liquid_symbols()
            source_label = "liquid_universe"
        else:
            try:
                index = bp.Index(universe)
                components = index.components
                all_symbols = [
                    s["symbol"] if isinstance(s, dict) else str(s)
                    for s in (components or [])
                ]
            except Exception:
                logger.exception(f"Universe yuklenemedi: {universe}")
                return ScanResult(candidates=[], scanned_count=0, filtered_count=0)
            source_label = universe

        if not all_symbols:
            logger.error(f"Evren bos: {source_label}")
            return ScanResult(candidates=[], scanned_count=0, filtered_count=0)

        logger.info(f"Deep scan basliyor: {len(all_symbols)} sembol ({source_label})")

        # Parallel scoring
        candidates: list[ScoredCandidate] = []
        symbols_str = [str(s) for s in all_symbols]
        results = self._executor.map(
            lambda s: self._score_symbol(s, available_cash, params),
            symbols_str,
        )
        done = 0
        for scored in results:
            done += 1
            if done % 50 == 0:
                logger.info(f"Ilerleme: {done}/{len(symbols_str)}")
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

    def _log_scored_signal(self, scored: ScoredCandidate) -> bool:
        """Log scored signal to database if not already logged in last 24h."""
        if self._repo.has_recent_signal(scored.symbol, "buy"):
            logger.debug(f"{scored.symbol}: Son 24 saatte sinyal var, atlanıyor")
            return False
        self._repo.log_signal(
            symbol=scored.symbol,
            signal_type="buy",
            indicator="multi_tf_score",
            strength="strong" if scored.entry_score >= 6 else "medium",
            price_at_signal=scored.price,
            indicator_values={"entry_score": scored.entry_score, "reasons": ", ".join(scored.reasons)},
            score=scored.entry_score * 10,
        )
