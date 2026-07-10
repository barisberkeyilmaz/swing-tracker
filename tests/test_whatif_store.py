"""Tests for whatif persistent store: config, schema/CRUD, job steps."""

from __future__ import annotations

import sqlite3

import pytest

from swing_tracker.config import WhatIfConfig, load_config
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


class TestWhatIfConfig:
    def test_defaults(self):
        cfg = WhatIfConfig()
        assert cfg.enabled is True
        assert cfg.max_holding_days == 60

    def test_load_from_toml(self):
        config = load_config()
        assert isinstance(config.whatif, WhatIfConfig)
        assert config.whatif.max_holding_days == 60


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def _insert_signal(repo, symbol="THYAO", created_at="2026-07-01 07:30:00",
                   score=50, price=100.0):
    sid = repo.log_signal(
        symbol=symbol, signal_type="buy", indicator="score", strength="medium",
        price_at_signal=price, score=score,
        indicator_values={"entry_score": score // 10, "reasons": "test"},
    )
    repo._conn.execute(
        "UPDATE signals_log SET created_at = ? WHERE id = ?", (created_at, sid)
    )
    repo._conn.commit()
    return sid


def _pending_fields(signal_id, symbol="THYAO", signal_time="2026-07-01 07:30:00",
                    score=5, price=100.0):
    return {
        "signal_id": signal_id, "symbol": symbol, "signal_time": signal_time,
        "score": score, "price_at_signal": price,
    }


class TestWhatIfTradeCrud:
    def test_insert_and_get(self, repo):
        sid = _insert_signal(repo)
        rowid = repo.insert_whatif_trade(_pending_fields(sid))
        assert rowid is not None

        rows = repo.get_whatif_trades()
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
        assert rows[0]["symbol"] == "THYAO"
        assert rows[0]["score"] == 5

    def test_insert_or_ignore_idempotent(self, repo):
        sid = _insert_signal(repo)
        assert repo.insert_whatif_trade(_pending_fields(sid)) is not None
        assert repo.insert_whatif_trade(_pending_fields(sid)) is None  # ayni signal_id
        assert len(repo.get_whatif_trades()) == 1

    def test_status_filter_and_order(self, repo):
        s1 = _insert_signal(repo, "AAA", "2026-07-02 08:00:00")
        s2 = _insert_signal(repo, "BBB", "2026-07-01 08:00:00")
        repo.insert_whatif_trade(_pending_fields(s1, "AAA", "2026-07-02 08:00:00"))
        rid2 = repo.insert_whatif_trade(_pending_fields(s2, "BBB", "2026-07-01 08:00:00"))
        repo.update_whatif_trade(rid2, {"status": "open", "entry_price": 101.0})

        assert [r["symbol"] for r in repo.get_whatif_trades()] == ["BBB", "AAA"]
        opens = repo.get_whatif_trades(status="open")
        assert len(opens) == 1 and opens[0]["entry_price"] == 101.0

    def test_update_whitelist(self, repo):
        sid = _insert_signal(repo)
        rid = repo.insert_whatif_trade(_pending_fields(sid))
        with pytest.raises(ValueError):
            repo.update_whatif_trade(rid, {"symbol; DROP TABLE": 1})
