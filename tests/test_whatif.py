"""Tests for what-if simulation: repository, entry price, simulation, stats."""

from __future__ import annotations

import sqlite3

import pytest

from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def _log(repo, symbol, signal_type="buy", score=5, price=100.0, created_at=None):
    sid = repo.log_signal(
        symbol=symbol,
        signal_type=signal_type,
        indicator="score",
        strength="medium",
        price_at_signal=price,
        score=score,
    )
    if created_at:
        repo._conn.execute(
            "UPDATE signals_log SET created_at = ? WHERE id = ?", (created_at, sid)
        )
        repo._conn.commit()
    return sid


class TestGetBuySignalsAsc:
    def test_filters_type_and_score_orders_asc(self, repo):
        _log(repo, "THYAO", score=5, created_at="2026-07-02 08:00:00")
        _log(repo, "ASELS", score=3, created_at="2026-07-01 08:00:00")  # skor dusuk
        _log(repo, "GARAN", signal_type="sell", score=8, created_at="2026-07-01 09:00:00")
        _log(repo, "KCHOL", score=4, created_at="2026-07-01 07:00:00")

        rows = repo.get_buy_signals_asc(min_score=4)

        assert [r["symbol"] for r in rows] == ["KCHOL", "THYAO"]
        assert all(isinstance(r, dict) for r in rows)

    def test_empty(self, repo):
        assert repo.get_buy_signals_asc(min_score=4) == []
