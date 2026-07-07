"""Web helper: sinyal listesindeki UTC created_at'leri yerel saate cevirme.

Kok neden (2026-07-07): signals_log.created_at SQLite datetime('now') ile
UTC yazilir; dashboard/signals sayfalari bunu cig basiyordu (3 saat geri).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

from swing_tracker.web.helpers import localize_signal_timestamps

IST = ZoneInfo("Europe/Istanbul")


class TestLocalizeSignalTimestamps:
    def test_converts_utc_to_istanbul(self):
        signals = [{"symbol": "THYAO", "created_at": "2026-07-07 07:31:12"}]

        localize_signal_timestamps(signals, IST)

        assert signals[0]["created_at"] == "2026-07-07 10:31"

    def test_handles_missing_created_at(self):
        signals = [{"symbol": "THYAO"}]

        localize_signal_timestamps(signals, IST)

        assert signals[0].get("created_at", "") == ""

    def test_returns_same_list(self):
        signals = [{"created_at": "2026-07-07 12:00:00"}]
        assert localize_signal_timestamps(signals, IST) is signals
