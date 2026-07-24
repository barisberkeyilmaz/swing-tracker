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


def test_upsert_and_get_holding(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 4.0, cost_per_share=650.0, notes="core")
    got = repo.get_allocation_holding("VOO")
    assert got["symbol"] == "VOO"
    assert got["exchange"] == "AMEX"
    assert got["shares"] == 4.0
    assert got["cost_per_share"] == 650.0


def test_upsert_updates_existing(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 4.0)
    repo.upsert_allocation_holding("VOO", "AMEX", 6.5)
    rows = repo.get_allocation_holdings()
    assert len(rows) == 1
    assert rows[0]["shares"] == 6.5


def test_delete_holding(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 4.0)
    repo.delete_allocation_holding("VOO")
    assert repo.get_allocation_holdings() == []


def test_review_log(repo):
    assert repo.get_last_allocation_review() is None
    repo.log_allocation_review("ceyreklik")
    last = repo.get_last_allocation_review()
    assert last["note"] == "ceyreklik"
    assert last["reviewed_at"] is not None


def test_settings_upsert(repo):
    assert repo.get_allocation_setting("last_contribution_usd") is None
    assert repo.get_allocation_setting("last_contribution_usd", "0") == "0"
    repo.set_allocation_setting("last_contribution_usd", "750")
    assert repo.get_allocation_setting("last_contribution_usd") == "750"
    repo.set_allocation_setting("last_contribution_usd", "800")
    assert repo.get_allocation_setting("last_contribution_usd") == "800"
