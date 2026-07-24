import sqlite3
from datetime import datetime

import pytest

from swing_tracker.config import AllocationConfig, AllocationTarget
from swing_tracker.core.allocation_service import build_report
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


class FakePriceCache:
    def __init__(self, prices, usdtry=47.0):
        self._prices = prices
        self._usdtry = usdtry

    def fetch_many(self, symbol_exchange, max_workers=5):
        return {s: self._prices[s] for s in symbol_exchange if s in self._prices}

    def fetch_usdtry(self):
        return self._usdtry


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def _config():
    return AllocationConfig(
        enabled=True,
        monthly_contribution_usd=100.0,
        drift_threshold_pct=5.0,
        review_interval_days=91,
        fractional=True,
        targets={
            "VOO": AllocationTarget("VOO", 40, "AMEX", "core", ""),
            "QTUM": AllocationTarget("QTUM", 60, "NASDAQ", "satellite", ""),
        },
    )


def test_build_report_uses_config_contribution(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 10.0)
    repo.upsert_allocation_holding("QTUM", "NASDAQ", 30.0)
    cache = FakePriceCache({"VOO": 10.0, "QTUM": 10.0})
    view = build_report(repo, _config(), now=datetime(2026, 7, 24), price_cache=cache)
    assert view.contribution_usd == 100.0
    assert view.report.total_value_usd == 400.0
    assert view.report.usdtry == 47.0
    assert view.dca is not None and view.rebalance is not None and view.eta is not None


def test_build_report_prefers_saved_contribution(repo):
    repo.set_allocation_setting("last_contribution_usd", "250")
    cache = FakePriceCache({"VOO": 10.0})
    view = build_report(repo, _config(), now=datetime(2026, 7, 24), price_cache=cache)
    assert view.contribution_usd == 250.0


def test_build_report_override_wins(repo):
    repo.set_allocation_setting("last_contribution_usd", "250")
    cache = FakePriceCache({"VOO": 10.0})
    view = build_report(
        repo,
        _config(),
        now=datetime(2026, 7, 24),
        contribution_override=999.0,
        price_cache=cache,
    )
    assert view.contribution_usd == 999.0
