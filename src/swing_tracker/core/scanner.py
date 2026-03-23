"""BIST scanner for entry opportunities.

Uses borsapy's bp.scan() for pre-filtering, then runs full analysis
on candidates via signals.analyze_symbol().
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import borsapy as bp

from swing_tracker.config import Config
from swing_tracker.core.signals import AnalysisResult, analyze_symbol
from swing_tracker.core.strategy import get_strategy, get_strategy_params
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    candidates: list[AnalysisResult]
    scanned_count: int
    filtered_count: int


class Scanner:
    def __init__(self, repo: Repository, config: Config):
        self._repo = repo
        self._config = config

    def run_quick_scan(self, available_cash: float = 0) -> ScanResult:
        """Quick scan: pre-filter with bp.scan(), then full analysis on candidates.

        Runs every 30 minutes during market hours.
        """
        strategy = get_strategy(self._config)
        params = get_strategy_params(strategy)
        universe = self._config.scanner.universe

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

        # Full analysis on each candidate
        candidates: list[AnalysisResult] = []
        for symbol in candidate_symbols:
            result = analyze_symbol(
                symbol=symbol,
                period="6mo",
                interval="1d",
                available_cash=available_cash,
                strategy_params=params,
            )

            if result is None:
                continue

            # Filter by minimum score
            if result.score >= strategy.min_score and result.setup and result.setup.direction == "long":
                candidates.append(result)
                self._log_signals(result)

        # Sort by score descending
        candidates.sort(key=lambda x: x.score, reverse=True)

        logger.info(
            f"Quick scan tamamlandi: {len(candidate_symbols)} tarandi, "
            f"{len(candidates)} sinyal"
        )

        return ScanResult(
            candidates=candidates,
            scanned_count=len(candidate_symbols),
            filtered_count=len(candidates),
        )

    def run_deep_scan(self, available_cash: float = 0) -> ScanResult:
        """Deep scan: full analysis on entire universe.

        Runs daily after market close (18:30).
        """
        strategy = get_strategy(self._config)
        params = get_strategy_params(strategy)
        universe = self._config.scanner.universe

        # Get all symbols in universe
        try:
            index = bp.Index(universe)
            components = index.components()
            if components is not None:
                all_symbols = components.index.tolist() if hasattr(components.index, 'tolist') else list(components.index)
            else:
                logger.error(f"Universe bileşenleri alinamadi: {universe}")
                return ScanResult(candidates=[], scanned_count=0, filtered_count=0)
        except Exception:
            logger.exception(f"Universe yuklenemedi: {universe}")
            return ScanResult(candidates=[], scanned_count=0, filtered_count=0)

        logger.info(f"Deep scan basliyor: {len(all_symbols)} sembol ({universe})")

        candidates: list[AnalysisResult] = []
        for i, symbol in enumerate(all_symbols):
            if (i + 1) % 20 == 0:
                logger.info(f"Ilerleme: {i + 1}/{len(all_symbols)}")

            result = analyze_symbol(
                symbol=str(symbol),
                period="6mo",
                interval="1d",
                available_cash=available_cash,
                strategy_params=params,
            )

            if result is None:
                continue

            if result.score >= strategy.min_score and result.setup and result.setup.direction == "long":
                candidates.append(result)
                self._log_signals(result)

        candidates.sort(key=lambda x: x.score, reverse=True)

        logger.info(
            f"Deep scan tamamlandi: {len(all_symbols)} tarandi, "
            f"{len(candidates)} sinyal"
        )

        return ScanResult(
            candidates=candidates[:10],  # Top 10 for daily report
            scanned_count=len(all_symbols),
            filtered_count=len(candidates),
        )

    def _log_signals(self, result: AnalysisResult) -> None:
        """Log signals to database."""
        for signal in result.signals:
            if signal.signal_type == "buy":
                self._repo.log_signal(
                    symbol=result.symbol,
                    signal_type=signal.signal_type,
                    indicator=signal.indicator,
                    strength=signal.strength,
                    price_at_signal=signal.price,
                    indicator_values=signal.indicator_values,
                    score=result.score,
                )
