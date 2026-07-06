"""Telegram mesaj parcalama: 4096 karakter siniri asilmamali.

Kok neden (2026-07-06): /yakin cevabi cok aday oldugunda 4096'yi asip
telegram.error.BadRequest "Message is too long" firlatiyordu.
"""

from __future__ import annotations

from swing_tracker.bot.telegram import TELEGRAM_MAX_LEN, chunk_message


class TestChunkMessage:
    def test_short_message_single_chunk(self):
        assert chunk_message("merhaba") == ["merhaba"]

    def test_empty_message(self):
        assert chunk_message("") == [""]

    def test_exactly_at_limit_single_chunk(self):
        text = "a" * TELEGRAM_MAX_LEN
        assert chunk_message(text) == [text]

    def test_long_message_split_within_limit(self):
        lines = [f"  SYMBOL{i:03d} {100 + i:>8.2f} TL | rsi_dip macd_cross" for i in range(200)]
        text = "\n".join(lines)
        assert len(text) > TELEGRAM_MAX_LEN

        chunks = chunk_message(text)

        assert len(chunks) > 1
        assert all(len(c) <= TELEGRAM_MAX_LEN for c in chunks)

    def test_split_preserves_content(self):
        lines = [f"satir {i}" for i in range(2000)]
        text = "\n".join(lines)

        chunks = chunk_message(text)

        assert "\n".join(chunks) == text

    def test_split_respects_line_boundaries(self):
        """Hicbir satir ortadan bolunmemeli (HTML tag'leri satir ici)."""
        lines = [f"<b>satir {i}</b> icerik" * 5 for i in range(500)]
        text = "\n".join(lines)

        chunks = chunk_message(text)

        original_lines = set(text.split("\n"))
        for chunk in chunks:
            for line in chunk.split("\n"):
                assert line in original_lines

    def test_single_line_over_limit_hard_split(self):
        text = "x" * (TELEGRAM_MAX_LEN * 2 + 100)

        chunks = chunk_message(text)

        assert all(len(c) <= TELEGRAM_MAX_LEN for c in chunks)
        assert "".join(chunks) == text
