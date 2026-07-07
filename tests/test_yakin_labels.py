"""/yakin skor etiketleri canli tarayici esigiyle (MIN_ENTRY_SCORE=4) tutarli olmali.

Kok neden (2026-07-07): _cmd_yakin'de esik 5 hardcoded'di; canli tarayici
score < 4'u eliyor (4 = sinyal). /yakin skor 4'e "1 puan kaldi" diyordu.
"""

from __future__ import annotations

from swing_tracker.bot.telegram import yakin_score_label
from swing_tracker.core.scanner import MIN_ENTRY_SCORE


class TestYakinScoreLabel:
    def test_threshold_is_four(self):
        assert MIN_ENTRY_SCORE == 4

    def test_score_at_threshold_is_signal(self):
        emoji, label = yakin_score_label(4)
        assert emoji == "🟢"
        assert label == "SiNYAL"

    def test_score_above_threshold_is_signal(self):
        _, label = yakin_score_label(6)
        assert label == "SiNYAL"

    def test_one_below_threshold(self):
        emoji, label = yakin_score_label(3)
        assert emoji == "🟡"
        assert label == "1 puan kaldi"

    def test_two_below_threshold(self):
        emoji, label = yakin_score_label(2)
        assert emoji == "🔵"
        assert label == "2 puan kaldi"

    def test_far_below_threshold(self):
        emoji, label = yakin_score_label(1)
        assert emoji == "⚪"
        assert label == "3 puan kaldi"
