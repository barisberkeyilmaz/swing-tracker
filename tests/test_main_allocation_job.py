import sqlite3

from swing_tracker.config import AllocationConfig, AllocationTarget, Config
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables
from swing_tracker.main import job_allocation_check


class FakeNotifier:
    def __init__(self):
        self.calls = 0

    def notify_allocation_drift(self, view):
        self.calls += 1

    def notify_allocation_review(self, next_date):
        self.calls += 1


def test_job_allocation_check_runs_without_error(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    repo = Repository(conn)
    repo.upsert_allocation_holding("VOO", "AMEX", 1.0)
    config = Config()
    config.allocation = AllocationConfig(
        targets={"VOO": AllocationTarget("VOO", 40, "AMEX", "core", ""),
                 "QTUM": AllocationTarget("QTUM", 60, "NASDAQ", "satellite", "")}
    )

    from swing_tracker.core import allocation_service

    class FakeCache:
        def __init__(self):
            self.calls = 0

        def fetch_many(self, se, max_workers=5):
            self.calls += 1
            return {s: 100.0 for s in se}

        def fetch_usdtry(self):
            return 47.0

    fake_cache = FakeCache()
    monkeypatch.setattr(allocation_service.etf_prices, "etf_price_cache", fake_cache)
    # exception yutulmali, patlamamali
    job_allocation_check(repo, config, FakeNotifier())
    # fake'in gercekten kullanildigini dogrula — network cagrisi olmasin
    assert fake_cache.calls > 0, "FakeCache was not used; job made real network call"
