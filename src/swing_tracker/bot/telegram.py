"""Telegram bot for notifications and interactive commands.

Phase 1: Notifications (send alerts)
Phase 2: Interactive commands (/durum, /portfoy, /scan, /nakit, /pozisyon)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from swing_tracker.config import TelegramConfig

if TYPE_CHECKING:
    from swing_tracker.core.monitor import Alert
    from swing_tracker.core.portfolio import PortfolioManager
    from swing_tracker.core.scanner import Scanner, ScoredCandidate
    from swing_tracker.core.signals import AnalysisResult
    from swing_tracker.db.repository import Repository

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, config: TelegramConfig):
        self._config = config
        self._bot: Bot | None = None
        self._app: Application | None = None

        # These will be set by main.py after initialization
        self.scanner: Scanner | None = None
        self.portfolio: PortfolioManager | None = None
        self.repo: Repository | None = None

        if config.enabled and config.token and config.chat_id:
            self._bot = Bot(token=config.token)
            logger.info("Telegram bot baslatildi")
        else:
            logger.warning("Telegram devre disi veya yapilandirilmamis")

    def start_polling_in_thread(self) -> None:
        """Start the bot polling in a separate thread.

        Uses manual init/start instead of run_polling() to avoid
        signal handler issues in non-main threads.
        """
        if not self._config.token:
            return

        import threading

        def _run_polling():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _start():
                app = Application.builder().token(self._config.token).build()
                self._app = app

                app.add_handler(CommandHandler("durum", self._cmd_durum))
                app.add_handler(CommandHandler("portfoy", self._cmd_portfoy))
                app.add_handler(CommandHandler("pozisyon", self._cmd_pozisyon))
                app.add_handler(CommandHandler("sinyal", self._cmd_sinyal))
                app.add_handler(CommandHandler("scan", self._cmd_scan))
                app.add_handler(CommandHandler("nakit", self._cmd_nakit))
                app.add_handler(CommandHandler("yardim", self._cmd_yardim))
                app.add_handler(CommandHandler("start", self._cmd_yardim))

                await app.initialize()
                await app.start()
                await app.updater.start_polling(drop_pending_updates=True)
                logger.info("Telegram komut dinleme baslatildi")

                # Keep running until thread is killed
                while True:
                    await asyncio.sleep(1)

            try:
                loop.run_until_complete(_start())
            except Exception:
                logger.debug("Telegram polling thread kapandi")

        thread = threading.Thread(target=_run_polling, daemon=True)
        thread.start()

    # ── Command Handlers ──

    async def _cmd_yardim(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show available commands."""
        text = (
            "🤖 <b>Swing Tracker Komutlari</b>\n"
            "\n"
            "/durum — Sistem durumu ve piyasa rejimi\n"
            "/portfoy — Portfoy ozeti (nakit + yatirim)\n"
            "/pozisyon — Acik pozisyonlar\n"
            "/sinyal — Son sinyaller\n"
            "/scan — Manuel tarama baslat\n"
            "/nakit — Nakit bakiye\n"
            "/nakit ekle 50000 — Nakit yatir\n"
            "/nakit cek 10000 — Nakit cek\n"
            "/yardim — Bu mesaj\n"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_durum(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show system status and market regime."""
        lines = ["📊 <b>Sistem Durumu</b>\n"]

        # Market regime
        if self.scanner:
            is_bull = self.scanner.check_market_regime()
            emoji = "🟢" if is_bull else "🔴"
            status = "Boga" if is_bull else "Ayi"
            lines.append(f"Piyasa: {emoji} {status}")

        # Portfolio
        if self.portfolio:
            try:
                summary = self.portfolio.get_summary()
                lines.append(f"Portfoy: {summary.total_value:,.0f} TL")
                lines.append(f"Nakit: {summary.cash_balance:,.0f} TL")
            except Exception:
                lines.append("Portfoy: hesaplanamadi")

        # Open trades
        if self.repo:
            open_trades = self.repo.get_open_trades()
            lines.append(f"Acik pozisyon: {len(open_trades)}")

            # Recent signals
            signals = self.repo.get_recent_signals(limit=5)
            if signals:
                last = signals[0]
                lines.append(f"Son sinyal: {last['symbol']} ({last['created_at'][:16]})")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_portfoy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show portfolio summary."""
        if not self.portfolio:
            await update.message.reply_text("Portfoy modulu hazir degil.")
            return

        try:
            summary = self.portfolio.get_summary()
            swing = self.portfolio.get_swing_summary()

            text = (
                f"💼 <b>Portfoy Ozeti</b>\n"
                f"\n"
                f"Toplam Deger: <b>{summary.total_value:,.0f} TL</b>\n"
                f"Nakit: {summary.cash_balance:,.0f} TL\n"
                f"Yatirim: {summary.invested_value:,.0f} TL\n"
                f"PnL: {summary.total_pnl:+,.0f} TL ({summary.total_pnl_pct:+.1f}%)\n"
                f"\n"
                f"📈 <b>Swing Trading</b>\n"
                f"Acik pozisyon: {swing.open_trades}\n"
                f"Yatirilan: {swing.total_invested:,.0f} TL\n"
                f"Gerceklesmemis PnL: {swing.unrealized_pnl:+,.0f} TL\n"
                f"Gerceklesmis PnL: {swing.realized_pnl:+,.0f} TL\n"
            )
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Portfoy komutu hatasi")
            await update.message.reply_text("Portfoy bilgisi alinamadi.")

    async def _cmd_pozisyon(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show open positions."""
        if not self.repo:
            await update.message.reply_text("DB hazir degil.")
            return

        open_trades = self.repo.get_open_trades()
        if not open_trades:
            await update.message.reply_text("Acik pozisyon yok.")
            return

        lines = [f"📈 <b>Acik Pozisyonlar ({len(open_trades)})</b>\n"]

        for trade in open_trades:
            symbol = trade["symbol"]
            entry = trade.get("entry_price", 0)
            shares = trade.get("shares", 0)
            sl = trade.get("stop_loss")
            tp1 = trade.get("take_profit_1")

            # Try to get current price
            try:
                import borsapy as bp
                current = float(bp.Ticker(symbol).fast_info.get("last", entry))
                pnl_pct = (current - entry) / entry * 100 if entry else 0
                emoji = "📈" if pnl_pct >= 0 else "📉"
                price_text = f"Simdi: {current:.2f} ({pnl_pct:+.1f}%)"
            except Exception:
                price_text = "Fiyat alinamadi"
                emoji = "❓"

            line = (
                f"\n{emoji} <b>{symbol}</b>\n"
                f"  Giris: {entry:.2f} x{shares} lot\n"
                f"  {price_text}\n"
            )
            if sl:
                line += f"  SL: {sl:.2f}"
            if tp1:
                line += f" | TP1: {tp1:.2f}"
            line += "\n"
            lines.append(line)

        await update.message.reply_text("".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_sinyal(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show recent signals."""
        if not self.repo:
            await update.message.reply_text("DB hazir degil.")
            return

        signals = self.repo.get_recent_signals(limit=10)
        if not signals:
            await update.message.reply_text("Henuz sinyal yok.")
            return

        lines = [f"🔔 <b>Son Sinyaller</b>\n"]
        for sig in signals:
            symbol = sig["symbol"]
            score = sig.get("score", 0)
            price = sig.get("price_at_signal", 0)
            date = sig["created_at"][:16]
            lines.append(f"  🟢 {symbol} | Skor: {score} | {price:.2f} TL | {date}")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_scan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Trigger a manual quick scan."""
        if not self.scanner or not self.portfolio:
            await update.message.reply_text("Scanner hazir degil.")
            return

        await update.message.reply_text("🔍 Tarama basliyor...")

        try:
            cash = self.portfolio.available_cash()
            result = self.scanner.run_quick_scan(available_cash=cash)

            if not result.market_bullish:
                await update.message.reply_text(
                    "🔴 Ayi piyasasi — sinyal aramiyor.\n"
                    "XU100 SMA100'un altinda."
                )
                return

            if not result.candidates:
                await update.message.reply_text(
                    f"✅ Tarama tamamlandi.\n"
                    f"{result.scanned_count} sembol tarandi, sinyal bulunamadi."
                )
                return

            lines = [
                f"✅ Tarama tamamlandi: {result.scanned_count} tarandi, "
                f"{result.filtered_count} sinyal\n"
            ]
            for c in result.candidates[:5]:
                setup = c.analysis.setup
                sl_text = f"SL:{setup.stop_loss:.2f}" if setup and setup.stop_loss else ""
                tp_text = f"TP1:{setup.take_profit_1:.2f}" if setup and setup.take_profit_1 else ""
                lines.append(
                    f"\n🟢 <b>{c.symbol}</b> @ {c.price:.2f} TL\n"
                    f"  Skor: {c.entry_score}/8 ({', '.join(c.reasons)})\n"
                    f"  {sl_text} | {tp_text}\n"
                )

            await update.message.reply_text("".join(lines), parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("Manuel scan hatasi")
            await update.message.reply_text("Tarama sirasinda hata olustu.")

    async def _cmd_nakit(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show cash balance or deposit/withdraw cash."""
        if not self.repo or not self.portfolio:
            await update.message.reply_text("DB hazir degil.")
            return

        args = context.args or []

        # /nakit ekle 50000
        if len(args) >= 2 and args[0] in ("ekle", "yatir"):
            try:
                amount = float(args[1])
                desc = " ".join(args[2:]) if len(args) > 2 else "Nakit yatirma"
                self.portfolio.deposit_cash(amount, desc)
                balance = self.repo.get_cash_balance()
                await update.message.reply_text(
                    f"✅ {amount:,.0f} TL yatirildi.\n"
                    f"Yeni bakiye: <b>{balance:,.0f} TL</b>",
                    parse_mode=ParseMode.HTML,
                )
            except ValueError:
                await update.message.reply_text("Gecersiz miktar. Ornek: /nakit ekle 50000")
            return

        # /nakit cek 10000
        if len(args) >= 2 and args[0] in ("cek", "cikar"):
            try:
                amount = float(args[1])
                desc = " ".join(args[2:]) if len(args) > 2 else "Nakit cekme"
                self.repo.add_cash_transaction(-amount, "withdrawal", description=desc)
                balance = self.repo.get_cash_balance()
                await update.message.reply_text(
                    f"✅ {amount:,.0f} TL cekildi.\n"
                    f"Yeni bakiye: <b>{balance:,.0f} TL</b>",
                    parse_mode=ParseMode.HTML,
                )
            except ValueError:
                await update.message.reply_text("Gecersiz miktar. Ornek: /nakit cek 10000")
            return

        # /nakit — show balance and recent transactions
        balance = self.repo.get_cash_balance()
        transactions = self.repo.get_cash_transactions(limit=5)

        lines = [
            f"💰 <b>Nakit Bakiye: {balance:,.0f} TL</b>\n",
        ]

        if transactions:
            lines.append("Son islemler:")
            for tx in transactions:
                emoji = "➕" if tx["amount"] > 0 else "➖"
                lines.append(
                    f"  {emoji} {abs(tx['amount']):,.0f} TL — "
                    f"{tx.get('description', tx['transaction_type'])} "
                    f"({tx['created_at'][:10]})"
                )

        lines.append("\n/nakit ekle 50000 — Yatir\n/nakit cek 10000 — Cek")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ── Notification Methods ──

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

        score = candidate.entry_score
        score_bar = "█" * score + "░" * (8 - score)
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
                f"\n📐 <b>Trade Setup:</b>\n"
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
                f"\n💰 Pozisyon: {setup.position_size} lot "
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

        text = (
            f"{'🟢' if setup.direction == 'long' else '🔴'} "
            f"{'AL' if setup.direction == 'long' else 'SAT'} SiNYALi: <b>{result.symbol}</b>\n"
            f"\nFiyat: {result.price:.2f} TL | Skor: {result.score:+d}/100\n"
        )
        await self.send_message(text)

    async def notify_alert(self, alert: Alert) -> None:
        """Send a TP/SL alert notification."""
        if not self._config.notify_tp_sl:
            return

        emoji = {
            "tp1": "🎯", "tp2": "🎯🎯", "tp3": "🏆",
            "sl": "🔴", "trailing_stop": "📉", "warning": "⚠️",
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
            f"\nPiyasa: {market_emoji} {market_text}\n"
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
                    current = float(bp.Ticker(symbol).fast_info.get("last", entry))
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

        if not market_bullish:
            text += "\n⚠️ Ayi piyasasi — yeni pozisyon acilmiyor.\n"

        await self.send_message(text)
