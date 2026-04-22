"""Entry point: scheduler setup, job definitions, graceful shutdown."""

from __future__ import annotations

import logging
import platform
import signal
import socket
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from swing_tracker.config import Config, load_config
from swing_tracker.core.monitor import Monitor
from swing_tracker.core.portfolio import PortfolioManager
from swing_tracker.core.scanner import Scanner
from swing_tracker.core.universe import UniverseBuilder
from swing_tracker.db.connection import get_connection
from swing_tracker.db.repository import Repository
from swing_tracker.bot.telegram import TelegramNotifier

logger = logging.getLogger("swing_tracker")

# Global references for shutdown
_scheduler: BackgroundScheduler | None = None
_notifier: TelegramNotifier | None = None
_scanner: Scanner | None = None
_universe_builder: UniverseBuilder | None = None
_shutdown_called: bool = False


def setup_logging(config: Config) -> None:
    """Configure logging with file and console handlers."""
    config.log_file.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        str(config.log_file),
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger("swing_tracker")
    root_logger.setLevel(getattr(logging, config.log_level, logging.INFO))
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)


# ── Scheduled Jobs ──


def job_quick_scan(scanner: Scanner, portfolio: PortfolioManager, notifier: TelegramNotifier):
    """Quick scan job: runs every 30 minutes during market hours."""
    logger.info("Quick scan basliyor...")
    try:
        result = scanner.run_quick_scan()

        if not result.market_bullish:
            logger.info("Ayi piyasasi — sinyal gonderilmiyor")
            return

        for candidate in result.candidates:
            notifier.run_sync(notifier.notify_scored_signal(candidate))

        logger.info(f"Quick scan: {result.filtered_count} sinyal bulundu")
    except Exception:
        logger.exception("Quick scan hatasi")


def job_deep_scan(scanner: Scanner, portfolio: PortfolioManager, notifier: TelegramNotifier):
    """Deep scan job: runs daily after market close."""
    logger.info("Deep scan basliyor...")
    try:
        result = scanner.run_deep_scan()
        open_trades = scanner._repo.get_open_trades()

        notifier.run_sync(notifier.notify_daily_report(
            open_trades=open_trades,
            new_signals=result.candidates,
            market_bullish=result.market_bullish,
        ))

        logger.info(f"Deep scan + gunluk rapor gonderildi")
    except Exception:
        logger.exception("Deep scan hatasi")


def job_monitor(monitor: Monitor, notifier: TelegramNotifier):
    """Position monitor job: runs every 5 minutes during market hours."""
    try:
        alerts = monitor.check_positions()
        for alert in alerts:
            notifier.run_sync(notifier.notify_alert(alert))
            logger.info(f"Alert: {alert.alert_type} - {alert.symbol}")
    except Exception:
        logger.exception("Monitor hatasi")


def job_daily_snapshot(portfolio: PortfolioManager):
    """Daily snapshot job: records portfolio value."""
    try:
        portfolio.record_daily_snapshot()
    except Exception:
        logger.exception("Snapshot hatasi")


def job_build_universe(builder: UniverseBuilder):
    """Likit evren yeniden ins'a: deep_scan'den 15dk once."""
    try:
        total, kept = builder.build()
        logger.info(f"Universe build: {total} aday → {kept} likit sembol")
    except Exception:
        logger.exception("Universe build hatasi")


# ── Main ──


def shutdown(signum=None, frame=None):
    """Graceful shutdown handler (idempotent)."""
    global _shutdown_called
    if _shutdown_called:
        return
    _shutdown_called = True
    logger.info("Kapatiliyor...")
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
    if _scanner is not None:
        _scanner.close()
    if _universe_builder is not None:
        _universe_builder.close()
    sys.exit(0)


def main():
    global _scheduler, _notifier, _scanner, _universe_builder

    # Load config
    config = load_config()
    setup_logging(config)
    logger.info("Swing Tracker baslatiliyor...")

    # Network safety: borsapy/yfinance icin global socket timeout. Timeout yok
    # olursa TradingView socket'i sonsuza dek hang olabilir ve APScheduler
    # default max_instances=1 yuzunden takip eden tum scan'ler silently drop
    # edilir.
    socket.setdefaulttimeout(60)

    # Signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    if platform.system() == "Windows":
        signal.signal(signal.SIGBREAK, shutdown)

    # Initialize DB
    conn = get_connection(config.db_path)
    repo = Repository(conn)

    # Initialize components
    portfolio = PortfolioManager(repo, config)

    universe_builder = UniverseBuilder(repo, config)
    _universe_builder = universe_builder

    scanner = Scanner(repo, config, universe_builder=universe_builder)
    _scanner = scanner
    monitor = Monitor(repo, config)
    _notifier = TelegramNotifier(config.telegram)

    # Wire up components for interactive commands
    _notifier.scanner = scanner
    _notifier.portfolio = portfolio
    _notifier.repo = repo
    _notifier.monitor = monitor

    # Start Telegram command polling in separate thread
    try:
        _notifier.start_polling_in_thread()
    except Exception:
        logger.warning("Telegram polling baslatilamadi, sadece bildirim modu")

    # Setup scheduler
    tz = str(config.timezone)
    _scheduler = BackgroundScheduler(timezone=tz)

    # Quick scan: every X minutes, Mon-Fri 10:00-18:00
    _scheduler.add_job(
        job_quick_scan,
        CronTrigger(
            day_of_week="mon-fri",
            hour="10-17",
            minute=f"*/{config.scanner.quick_scan_interval_minutes}",
            timezone=tz,
        ),
        args=[scanner, portfolio, _notifier],
        id="quick_scan",
        name="Quick Scan",
    )

    # Deep scan: daily at configured time, Mon-Fri
    deep_hour, deep_minute = config.scanner.deep_scan_time.split(":")
    _scheduler.add_job(
        job_deep_scan,
        CronTrigger(
            day_of_week="mon-fri",
            hour=int(deep_hour),
            minute=int(deep_minute),
            timezone=tz,
        ),
        args=[scanner, portfolio, _notifier],
        id="deep_scan",
        name="Deep Scan",
    )

    # Monitor: every X minutes, Mon-Fri 10:00-18:15
    _scheduler.add_job(
        job_monitor,
        CronTrigger(
            day_of_week="mon-fri",
            hour="10-18",
            minute=f"*/{config.monitor.check_interval_minutes}",
            timezone=tz,
        ),
        args=[monitor, _notifier],
        id="monitor",
        name="Position Monitor",
    )

    # Daily snapshot: Mon-Fri at 18:45
    _scheduler.add_job(
        job_daily_snapshot,
        CronTrigger(
            day_of_week="mon-fri",
            hour=18,
            minute=45,
            timezone=tz,
        ),
        args=[portfolio],
        id="daily_snapshot",
        name="Daily Snapshot",
    )

    # Universe build: Mon-Fri, deep_scan'den 15dk once
    if config.liquidity.enabled:
        build_hour, build_minute = config.liquidity.build_time.split(":")
        _scheduler.add_job(
            job_build_universe,
            CronTrigger(
                day_of_week="mon-fri",
                hour=int(build_hour),
                minute=int(build_minute),
                timezone=tz,
            ),
            args=[universe_builder],
            id="build_universe",
            name="Universe Builder",
        )

    _scheduler.start()

    logger.info("Scheduler baslatildi. Zamanlanmis gorevler:")
    for job in _scheduler.get_jobs():
        logger.info(f"  - {job.name}: {job.trigger}")

    logger.info("Swing Tracker calisiyor. Durdurmak icin Ctrl+C")

    # Keep main thread alive
    try:
        while True:
            import time
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        shutdown()


if __name__ == "__main__":
    main()
