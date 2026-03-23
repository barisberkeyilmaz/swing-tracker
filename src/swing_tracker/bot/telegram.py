"""Telegram bot for notifications and commands.

Phase 1: Notification-only (send alerts)
Phase 2: Interactive commands (/portfoy, /swing, /roi, etc.)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Bot
from telegram.constants import ParseMode

from swing_tracker.config import TelegramConfig

if TYPE_CHECKING:
    from swing_tracker.core.monitor import Alert
    from swing_tracker.core.scanner import ScoredCandidate
    from swing_tracker.core.signals import AnalysisResult

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, config: TelegramConfig):
        self._config = config
        self._bot: Bot | None = None

        if config.enabled and config.token and config.chat_id:
            self._bot = Bot(token=config.token)
            logger.info("Telegram bot baslatildi")
        else:
            logger.warning("Telegram devre disi veya yapilandirilmamis")

    async def send_message(self, text: str) -> None:
        """Send a text message to the configured chat."""
        if not self._bot or not self._config.chat_id:
            logger.debug(f"Telegram devre disi, mesaj: {text[:50]}...")
            return

        try:
            await self._bot.send_message(
                chat_id=self._config.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Telegram mesaj gonderme hatasi")

    async def notify_scored_signal(self, candidate: ScoredCandidate) -> None:
        """Send a score-based buy signal notification."""
        if not self._config.notify_signals:
            return

        setup = candidate.analysis.setup
        if not setup or setup.direction == "neutral":
            return

        # Score bar visualization
        score = candidate.entry_score
        score_bar = "█" * score + "░" * (8 - score)

        # Reasons
        reasons_text = " | ".join(candidate.reasons)

        text = (
            f"🟢 <b>AL SiNYALi: {candidate.symbol}</b>\n"
            f"\n"
            f"Fiyat: <b>{candidate.price:.2f} TL</b>\n"
            f"Skor: [{score_bar}] {score}/8\n"
            f"Sinyaller: {reasons_text}\n"
        )

        if candidate.daily_rsi is not None:
            text += f"Gunluk RSI: {candidate.daily_rsi:.0f}"
            if candidate.hourly_rsi is not None:
                text += f" | Saatlik RSI: {candidate.hourly_rsi:.0f}"
            text += "\n"

        if setup.stop_loss and setup.take_profit_1:
            sl_pct = setup.stop_loss_pct or 0
            tp1_pct = abs(setup.take_profit_1 - setup.entry_price) / setup.entry_price * 100

            text += (
                f"\n"
                f"📐 <b>Trade Setup:</b>\n"
                f"  Giris: {setup.entry_price:.2f} TL\n"
                f"  SL: {setup.stop_loss:.2f} (-{sl_pct:.1f}%)\n"
                f"  TP1: {setup.take_profit_1:.2f} (+{tp1_pct:.1f}%)\n"
            )
            if setup.take_profit_2:
                tp2_pct = abs(setup.take_profit_2 - setup.entry_price) / setup.entry_price * 100
                text += f"  TP2: {setup.take_profit_2:.2f} (+{tp2_pct:.1f}%)\n"
            if setup.risk_reward:
                text += f"  R/R: {setup.risk_reward}x\n"

        if setup.position_size > 0:
            text += (
                f"\n"
                f"💰 Pozisyon: {setup.position_size} lot "
                f"({setup.position_cost:,.0f} TL)\n"
                f"Risk: {setup.risk_amount:,.0f} TL\n"
            )

        await self.send_message(text)

    async def notify_signal(self, result: AnalysisResult) -> None:
        """Send a buy signal notification (legacy format)."""
        if not self._config.notify_signals:
            return

        setup = result.setup
        if not setup or setup.direction == "neutral":
            return

        ind = result.indicators
        rsi = ind.get("rsi_14") or ind.get("rsi", 0)
        macd = ind.get("macd", 0)
        signal = ind.get("signal", 0)

        ind_lines = []
        if rsi:
            ind_lines.append(f"RSI: {rsi:.0f}")
        if macd and signal:
            direction = "Yukari" if macd > signal else "Asagi"
            ind_lines.append(f"MACD: {direction}")

        ind_text = " | ".join(ind_lines)

        text = (
            f"{'🟢' if setup.direction == 'long' else '🔴'} "
            f"{'AL' if setup.direction == 'long' else 'SAT'} SiNYALi: <b>{result.symbol}</b>\n"
            f"\n"
            f"Fiyat: {result.price:.2f} TL | Skor: {result.score:+d}/100\n"
            f"{ind_text}\n"
        )

        if setup.stop_loss and setup.take_profit_1:
            sl_pct = setup.stop_loss_pct or 0
            tp1_pct = abs(setup.take_profit_1 - setup.entry_price) / setup.entry_price * 100
            text += (
                f"\n📐 Trade Setup:\n"
                f"  Giris: {setup.entry_price:.2f} TL\n"
                f"  SL: {setup.stop_loss:.2f} (-{sl_pct:.1f}%)\n"
                f"  TP1: {setup.take_profit_1:.2f} (+{tp1_pct:.1f}%)\n"
            )

        await self.send_message(text)

    async def notify_alert(self, alert: Alert) -> None:
        """Send a TP/SL alert notification."""
        if not self._config.notify_tp_sl:
            return

        emoji = {
            "tp1": "🎯",
            "tp2": "🎯🎯",
            "tp3": "🏆",
            "sl": "🔴",
            "trailing_stop": "📉",
            "warning": "⚠️",
        }.get(alert.alert_type, "📢")

        text = f"{emoji} {alert.message}"
        await self.send_message(text)

    async def notify_daily_report(
        self,
        portfolio_value: float,
        cash_balance: float,
        swing_pnl: float,
        open_trades: list[dict],
        new_signals: list,
        market_bullish: bool = True,
    ) -> None:
        """Send daily summary report."""
        if not self._config.notify_daily_report:
            return

        market_emoji = "🟢" if market_bullish else "🔴"
        market_text = "Boga" if market_bullish else "Ayi"

        text = (
            f"📊 <b>Gunluk Rapor</b>\n"
            f"\n"
            f"Piyasa: {market_emoji} {market_text}\n"
            f"💼 Portfoy: {portfolio_value:,.0f} TL\n"
            f"💰 Nakit: {cash_balance:,.0f} TL\n"
            f"📈 Swing PnL: {swing_pnl:+,.0f} TL\n"
        )

        if open_trades:
            text += f"\n<b>Acik Pozisyonlar:</b>\n"
            for trade in open_trades[:5]:
                symbol = trade.get("symbol", "?")
                entry = trade.get("entry_price", 0)
                try:
                    import borsapy as bp
                    ticker = bp.Ticker(symbol)
                    current = float(ticker.fast_info.get("last", entry))
                    pnl_pct = (current - entry) / entry * 100 if entry else 0
                    emoji = "📈" if pnl_pct >= 0 else "📉"
                    text += f"  {emoji} {symbol}: {pnl_pct:+.1f}%\n"
                except Exception:
                    text += f"  ❓ {symbol}: fiyat alinamadi\n"

        if new_signals:
            text += f"\n<b>Yeni Sinyaller ({len(new_signals)}):</b>\n"
            for sig in new_signals[:5]:
                if hasattr(sig, "entry_score"):
                    text += f"  🟢 {sig.symbol}: Skor {sig.entry_score}/8 ({', '.join(sig.reasons)})\n"
                else:
                    text += f"  🟢 {sig.symbol}: Skor {sig.score:+d}\n"
        elif market_bullish:
            text += "\nYeni sinyal yok.\n"

        if not market_bullish:
            text += "\n⚠️ Ayi piyasasi — yeni pozisyon acilmiyor.\n"

        await self.send_message(text)
