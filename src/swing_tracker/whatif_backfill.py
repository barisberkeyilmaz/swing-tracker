"""Tek seferlik backfill: signals_log'daki eski buy sinyallerini whatif_trades'e tasi.

Ayri bir retrospektif simulasyon yolu yoktur: sinyaller 'pending' eklenir,
ardindan gunluk job pipeline'i (run_whatif_update) bir kez kosturulur —
pending doldurma tarihi girisleri uretir, open guncelleme bugune kadar replay
yapar, expiry eski aciklari kapatir. INSERT OR IGNORE sayesinde idempotent.

Kullanim: python -m swing_tracker.whatif_backfill
"""

from __future__ import annotations

import logging

from swing_tracker.config import load_config
from swing_tracker.core.scanner import MIN_ENTRY_SCORE
from swing_tracker.core.whatif import normalize_signal_score
from swing_tracker.db.connection import get_connection
from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)


def backfill_signals(repo: Repository) -> dict:
    """Esik ustu buy sinyallerini pending satir olarak ekle (idempotent)."""
    counts = {"inserted": 0, "skipped_existing": 0}
    for sig in repo.get_buy_signals_asc(min_score=MIN_ENTRY_SCORE * 10):
        rowid = repo.insert_whatif_trade({
            "signal_id": sig["id"],
            "symbol": sig["symbol"],
            "signal_time": sig["created_at"],
            "score": normalize_signal_score(sig),
            "price_at_signal": sig["price_at_signal"],
        })
        counts["inserted" if rowid is not None else "skipped_existing"] += 1
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = load_config()
    conn = get_connection(config.db_path)
    repo = Repository(conn)
    try:
        counts = backfill_signals(repo)
        logger.info("Backfill: %s", counts)

        from swing_tracker.core.whatif_store import run_whatif_update
        summary = run_whatif_update(repo, config)
        logger.info("Ilk guncelleme: %s", summary)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
