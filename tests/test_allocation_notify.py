import sqlite3
from datetime import datetime

import pytest

from swing_tracker.bot.telegram import build_drift_message, build_review_message
from swing_tracker.config import AllocationConfig, AllocationTarget
from swing_tracker.core.allocation import AllocationLeg, AllocationReport, RebalanceAlert
from swing_tracker.core.allocation_service import AllocationView, run_allocation_check
from swing_tracker.db.repository import Repository
from swing_tracker.db.schema import create_all_tables


def _view_with_drift():
    leg = AllocationLeg("VOO", "AMEX", "core", 28, 1, 300.0, 300.0, 75.0, 47.0, False)
    report = AllocationReport([leg], 300.0, 75.0, 0.0, 47.0)
    alert = RebalanceAlert([leg], True, None, None)
    return AllocationView(report, alert, None, None, None, 100.0)


def test_build_drift_message_lists_legs():
    msg = build_drift_message(_view_with_drift())
    assert "VOO" in msg
    assert "+47" in msg or "47.0" in msg


def test_build_review_message():
    from datetime import date
    msg = build_review_message(date(2026, 10, 1))
    assert "2026" in msg


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send_message_sync(self, text):
        self.sent.append(text)

    def notify_allocation_drift(self, view):
        self.send_message_sync("drift")

    def notify_allocation_review(self, next_date):
        self.send_message_sync("review")


class FakePriceCache:
    def fetch_many(self, se, max_workers=5):
        return {s: 100.0 for s in se}

    def fetch_usdtry(self):
        return 47.0


@pytest.fixture
def repo():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    create_all_tables(conn)
    return Repository(conn)


def _config():
    return AllocationConfig(
        drift_threshold_pct=5.0,
        targets={
            "VOO": AllocationTarget("VOO", 40, "AMEX", "core", ""),
            "QTUM": AllocationTarget("QTUM", 60, "NASDAQ", "satellite", ""),
        },
    )


def test_run_check_sends_when_drifted(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 1.0)  # tek bacak -> %100, drift
    notifier = FakeNotifier()
    run_allocation_check(repo, _config(), notifier,
                         now=datetime(2026, 7, 24), price_cache=FakePriceCache())
    assert len(notifier.sent) >= 1


def test_run_check_silent_when_no_drift_and_reviewed(repo):
    repo.upsert_allocation_holding("VOO", "AMEX", 40.0)
    repo.upsert_allocation_holding("QTUM", "NASDAQ", 60.0)
    repo.log_allocation_review("init")
    notifier = FakeNotifier()
    run_allocation_check(repo, _config(), notifier,
                         now=datetime(2026, 7, 24), price_cache=FakePriceCache())
    assert notifier.sent == []
