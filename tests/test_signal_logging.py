"""_log_scored_signal donus degeri: yeni sinyal True donmeli.

Kok neden (2026-07-07): fonksiyon log_signal()'dan sonra `return True`
icermiyordu → None (falsy) dondu → sinyaller DB'ye yazilip candidates
listesine hic girmedi → Telegram bildirimi hic gitmedi ve loglar hep
"0 yeni sinyal" dedi. Cooldown da ayni sembolu 24h bastirdiginda
sinyaller sessizce yutuldu.
"""

from __future__ import annotations

import sqlite3

import pytest

from swing_tracker.config import CacheConfig, Config, LiquidityConfig, ScannerConfig
from swing_tracker.core.scanner import ScoredCandidate, Scanner
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


@pytest.fixture
def repo() -> Repository:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    create_all_tables(conn)
    return Repository(conn)


@pytest.fixture
def scanner(repo) -> Scanner:
    c = Config()
    c.scanner = ScannerConfig(universe="XTUMY", market_regime_index="XU100")
    c.cache = CacheConfig(enabled=True)
    c.liquidity = LiquidityConfig(enabled=False)
    return Scanner(repo, c, universe_builder=None)


def _candidate(symbol: str = "THYAO") -> ScoredCandidate:
    return ScoredCandidate(
        symbol=symbol,
        price=100.0,
        entry_score=5,
        reasons=["RSI=35"],
        analysis=None,  # _log_scored_signal analysis'e dokunmaz
    )


class TestLogScoredSignal:
    def test_new_signal_returns_true(self, scanner):
        """Yeni sinyal loglanmali VE True donmeli (bildirim zinciri buna bagli)."""
        assert scanner._log_scored_signal(_candidate()) is True

    def test_new_signal_is_persisted(self, scanner, repo):
        scanner._log_scored_signal(_candidate())
        assert repo.has_recent_signal("THYAO", "buy") is True

    def test_repeat_within_cooldown_returns_false(self, scanner):
        scanner._log_scored_signal(_candidate())
        assert scanner._log_scored_signal(_candidate()) is False
