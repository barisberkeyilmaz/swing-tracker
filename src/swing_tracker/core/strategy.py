"""Strategy definitions loaded from config."""

from __future__ import annotations

from dataclasses import asdict

from swing_tracker.config import Config, StrategyConfig


def get_strategy(config: Config, name: str = "default") -> StrategyConfig:
    """Get a strategy by name from config, with defaults if not found."""
    return config.get_strategy(name)


def get_strategy_params(strategy: StrategyConfig) -> dict:
    """Convert a StrategyConfig to a dict for passing to signals module."""
    d = asdict(strategy)
    d.pop("name", None)
    return d


def list_strategies(config: Config) -> list[str]:
    """List all available strategy names."""
    return list(config.strategies.keys()) or ["default"]
