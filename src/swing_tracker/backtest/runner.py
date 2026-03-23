"""CLI entry point for running backtests."""

from __future__ import annotations

import argparse
import logging
import sys
from itertools import product

from swing_tracker.backtest.engine import run_backtest
from swing_tracker.backtest.metrics import compare_results, format_report
from swing_tracker.backtest.models import BacktestConfig, BacktestResult
from swing_tracker.config import load_config


def _parse_config_from_toml() -> BacktestConfig:
    """Load backtest config from config.toml [backtest] section."""
    config = load_config()
    raw_bt = {}

    # Re-read TOML for backtest section
    import tomllib
    from swing_tracker.config import PROJECT_ROOT

    config_path = PROJECT_ROOT / "config.toml"
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        raw_bt = raw.get("backtest", {})

    defaults = BacktestConfig()
    strategy = config.get_strategy()

    return BacktestConfig(
        symbols=raw_bt.get("symbols", defaults.symbols),
        start_date=raw_bt.get("start_date", defaults.start_date),
        end_date=raw_bt.get("end_date", defaults.end_date),
        initial_cash=raw_bt.get("initial_cash", config.portfolio.initial_cash),
        max_positions=raw_bt.get("max_positions", config.portfolio.max_swing_positions),
        risk_per_trade_pct=raw_bt.get("risk_per_trade_pct", config.portfolio.risk_per_trade_pct),
        commission_pct=raw_bt.get("commission_pct", defaults.commission_pct),
        trend_sma_period=raw_bt.get("trend_sma_period", defaults.trend_sma_period),
        min_entry_score=raw_bt.get("min_entry_score", defaults.min_entry_score),
        rsi_pullback_threshold=raw_bt.get("rsi_pullback_threshold", defaults.rsi_pullback_threshold),
        rsi_pullback_score=raw_bt.get("rsi_pullback_score", defaults.rsi_pullback_score),
        macd_negative_score=raw_bt.get("macd_negative_score", defaults.macd_negative_score),
        bb_lower_score=raw_bt.get("bb_lower_score", defaults.bb_lower_score),
        hourly_rsi_reversal_threshold=raw_bt.get("hourly_rsi_reversal_threshold", defaults.hourly_rsi_reversal_threshold),
        hourly_rsi_reversal_score=raw_bt.get("hourly_rsi_reversal_score", defaults.hourly_rsi_reversal_score),
        volume_above_avg_score=raw_bt.get("volume_above_avg_score", defaults.volume_above_avg_score),
        volume_avg_period=raw_bt.get("volume_avg_period", defaults.volume_avg_period),
        sl_atr_mult=raw_bt.get("sl_atr_mult", strategy.sl_atr_mult),
        tp1_atr_mult=raw_bt.get("tp1_atr_mult", strategy.tp1_atr_mult),
        tp1_exit_pct=raw_bt.get("tp1_exit_pct", strategy.tp1_exit_pct),
        tp2_atr_mult=raw_bt.get("tp2_atr_mult", strategy.tp2_atr_mult),
        tp2_exit_pct=raw_bt.get("tp2_exit_pct", strategy.tp2_exit_pct),
        trailing_stop_pct=raw_bt.get("trailing_stop_pct", defaults.trailing_stop_pct),
    )


def run_single(overrides: dict | None = None) -> BacktestResult:
    """Run a single backtest with optional parameter overrides."""
    config = _parse_config_from_toml()

    if overrides:
        for key, value in overrides.items():
            if hasattr(config, key):
                setattr(config, key, value)

    return run_backtest(config)


def run_comparison(param_grid: dict[str, list]) -> list[tuple[str, BacktestResult]]:
    """Run backtests across a parameter grid and return all results.

    Example param_grid:
        {"pullback_rsi_threshold": [35, 40, 45], "sl_atr_mult": [1.5, 2.0, 2.5]}
    """
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(product(*values))

    results: list[tuple[str, BacktestResult]] = []

    for combo in combinations:
        overrides = dict(zip(keys, combo))
        label = ", ".join(f"{k}={v}" for k, v in overrides.items())

        print(f"\nCalistiriliyor: {label}")
        result = run_single(overrides)
        results.append((label, result))

    return results


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Swing Tracker Backtest")
    parser.add_argument("--symbols", nargs="+", help="Sembol listesi")
    parser.add_argument("--start", help="Baslangic tarihi (YYYY-MM-DD)")
    parser.add_argument("--end", help="Bitis tarihi (YYYY-MM-DD)")
    parser.add_argument(
        "--compare", action="store_true",
        help="Parametre karsilastirmasi yap"
    )
    parser.add_argument(
        "--param", action="append", default=[],
        help="Parametre override: KEY=VAL (tekrarlanabilir)"
    )
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Build overrides
    overrides: dict = {}
    for p in args.param:
        if "=" in p:
            key, val = p.split("=", 1)
            try:
                overrides[key] = float(val)
            except ValueError:
                overrides[key] = val

    if args.symbols:
        overrides["symbols"] = args.symbols
    if args.start:
        overrides["start_date"] = args.start
    if args.end:
        overrides["end_date"] = args.end

    if args.compare:
        # Default comparison grid
        grid = {
            "pullback_rsi_threshold": [35.0, 40.0, 45.0],
            "sl_atr_mult": [1.5, 2.0, 2.5],
        }
        results = run_comparison(grid)
        metrics_list = [(label, r.metrics) for label, r in results]
        print("\n" + compare_results(metrics_list))
    else:
        result = run_single(overrides)
        print("\n" + format_report(result.metrics, result.params))

        # Print trade details
        if result.trades:
            print(f"\nTrade Detaylari ({len(result.trades)} trade):")
            print("-" * 70)
            for t in result.trades:
                exit_info = ", ".join(
                    f"{e.exit_type}@{e.price:.2f}" for e in t.exits
                ) if t.exits else "acik"
                print(
                    f"  {t.symbol:<8} {t.entry_date} @ {t.entry_price:>8.2f} "
                    f"x{t.shares:<4} -> {exit_info}  PnL: {t.total_pnl:>+8.0f} TL"
                )


if __name__ == "__main__":
    main()
