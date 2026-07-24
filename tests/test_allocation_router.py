import sqlite3

import pytest
from fastapi.testclient import TestClient

from swing_tracker.config import AllocationConfig, AllocationTarget, Config
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables
from swing_tracker.web import dependencies
from swing_tracker.core import allocation_service


class FakePriceCache:
    def fetch_many(self, symbol_exchange, max_workers=5):
        return {s: 100.0 for s in symbol_exchange}

    def fetch_usdtry(self):
        return 47.0


@pytest.fixture
def client(monkeypatch):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    repo = Repository(conn)
    config = Config()
    config.allocation = AllocationConfig(
        targets={
            "VOO": AllocationTarget("VOO", 40, "AMEX", "core", ""),
            "QTUM": AllocationTarget("QTUM", 60, "NASDAQ", "satellite", ""),
        }
    )
    dependencies.init_state(repo, config)
    monkeypatch.setattr(allocation_service.etf_prices, "etf_price_cache", FakePriceCache())
    from swing_tracker.web.app import app
    return TestClient(app), repo


def test_allocation_page_renders(client):
    tc, repo = client
    repo.upsert_allocation_holding("VOO", "AMEX", 4.0)
    resp = tc.get("/allocation")
    assert resp.status_code == 200
    assert "VOO" in resp.text


def test_add_holding_redirects(client):
    tc, repo = client
    resp = tc.post("/allocation/holding",
                   data={"symbol": "QTUM", "exchange": "NASDAQ", "shares": "10"},
                   follow_redirects=False)
    assert resp.status_code in (302, 303)
    assert repo.get_allocation_holding("QTUM")["shares"] == 10.0


def test_review_marks_done(client):
    tc, repo = client
    tc.post("/allocation/review", follow_redirects=False)
    assert repo.get_last_allocation_review() is not None


def test_dca_persists_contribution(client):
    tc, repo = client
    tc.post("/allocation/dca", data={"contribution": "750"}, follow_redirects=False)
    assert repo.get_allocation_setting("last_contribution_usd") == "750.0"
