"""Configuration loading from TOML and environment variables."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
import os

PROJECT_ROOT = Path(__file__).parent.parent.parent


@dataclass
class TelegramConfig:
    enabled: bool = True
    token: str = ""
    chat_id: str = ""
    notify_signals: bool = True
    notify_tp_sl: bool = True
    notify_daily_report: bool = True


@dataclass
class PortfolioConfig:
    benchmark: str = "XU100"
    initial_cash: float = 60_000
    monthly_deposit: float = 30_000
    max_swing_positions: int = 5
    risk_per_trade_pct: float = 2.0


@dataclass
class ScannerConfig:
    universe: str = "XU100"
    quick_scan_interval_minutes: int = 30
    deep_scan_time: str = "18:30"
    prefilters: list[str] = field(default_factory=lambda: [
        "rsi < 35 and close > sma_50",
    ])


@dataclass
class MonitorConfig:
    check_interval_minutes: int = 5
    trailing_stop_enabled: bool = True
    trailing_stop_atr_mult: float = 1.5


@dataclass
class CacheConfig:
    enabled: bool = True
    daily_ttl_minutes: int = 60
    hourly_ttl_minutes: int = 15
    regime_ttl_minutes: int = 30
    scanner_max_workers: int = 5


@dataclass
class StrategyConfig:
    name: str = "default"
    min_score: int = 30
    sl_atr_mult: float = 2.0
    tp1_atr_mult: float = 1.5
    tp2_atr_mult: float = 3.0
    tp3_atr_mult: float = 4.5
    tp1_exit_pct: float = 0.50
    tp2_exit_pct: float = 0.30
    trailing_stop: bool = True
    use_sr_levels: bool = True


@dataclass
class Config:
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "swing_tracker.db")
    log_level: str = "INFO"
    log_file: Path = field(default_factory=lambda: PROJECT_ROOT / "logs" / "swing_tracker.log")
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Istanbul"))

    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    strategies: dict[str, StrategyConfig] = field(default_factory=dict)

    def get_strategy(self, name: str = "default") -> StrategyConfig:
        return self.strategies.get(name, StrategyConfig(name=name))


def load_config(config_path: Path | None = None) -> Config:
    """Load configuration from TOML file and environment variables."""
    load_dotenv(PROJECT_ROOT / ".env")

    if config_path is None:
        config_path = PROJECT_ROOT / "config.toml"

    raw: dict = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    general = raw.get("general", {})
    db_path_str = general.get("db_path", "data/swing_tracker.db")
    log_file_str = general.get("log_file", "logs/swing_tracker.log")

    config = Config(
        db_path=PROJECT_ROOT / db_path_str,
        log_level=general.get("log_level", "INFO"),
        log_file=PROJECT_ROOT / log_file_str,
        timezone=ZoneInfo(general.get("timezone", "Europe/Istanbul")),
    )

    # Portfolio
    pf = raw.get("portfolio", {})
    config.portfolio = PortfolioConfig(
        benchmark=pf.get("benchmark", "XU100"),
        initial_cash=pf.get("initial_cash", 60_000),
        monthly_deposit=pf.get("monthly_deposit", 30_000),
        max_swing_positions=pf.get("max_swing_positions", 5),
        risk_per_trade_pct=pf.get("risk_per_trade_pct", 2.0),
    )

    # Telegram
    tg = raw.get("telegram", {})
    config.telegram = TelegramConfig(
        enabled=tg.get("enabled", True),
        token=os.getenv("TELEGRAM_TOKEN", ""),
        chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        notify_signals=tg.get("notify_signals", True),
        notify_tp_sl=tg.get("notify_tp_sl", True),
        notify_daily_report=tg.get("notify_daily_report", True),
    )

    # Scanner
    sc = raw.get("scanner", {})
    config.scanner = ScannerConfig(
        universe=sc.get("universe", "XU100"),
        quick_scan_interval_minutes=sc.get("quick_scan_interval_minutes", 30),
        deep_scan_time=sc.get("deep_scan_time", "18:30"),
        prefilters=sc.get("prefilters", ["rsi < 35 and close > sma_50"]),
    )

    # Monitor
    mon = raw.get("monitor", {})
    config.monitor = MonitorConfig(
        check_interval_minutes=mon.get("check_interval_minutes", 5),
        trailing_stop_enabled=mon.get("trailing_stop_enabled", True),
        trailing_stop_atr_mult=mon.get("trailing_stop_atr_mult", 1.5),
    )

    # Cache
    ca = raw.get("cache", {})
    config.cache = CacheConfig(
        enabled=ca.get("enabled", True),
        daily_ttl_minutes=ca.get("daily_ttl_minutes", 60),
        hourly_ttl_minutes=ca.get("hourly_ttl_minutes", 15),
        regime_ttl_minutes=ca.get("regime_ttl_minutes", 30),
        scanner_max_workers=ca.get("scanner_max_workers", 5),
    )

    # Strategies
    for key, val in raw.get("strategy", {}).items():
        config.strategies[key] = StrategyConfig(
            name=key,
            min_score=val.get("min_score", 30),
            sl_atr_mult=val.get("sl_atr_mult", 2.0),
            tp1_atr_mult=val.get("tp1_atr_mult", 1.5),
            tp2_atr_mult=val.get("tp2_atr_mult", 3.0),
            tp3_atr_mult=val.get("tp3_atr_mult", 4.5),
            tp1_exit_pct=val.get("tp1_exit_pct", 0.50),
            tp2_exit_pct=val.get("tp2_exit_pct", 0.30),
            trailing_stop=val.get("trailing_stop", True),
            use_sr_levels=val.get("use_sr_levels", True),
        )

    return config
