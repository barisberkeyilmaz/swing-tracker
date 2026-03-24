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
                app.add_handler(CommandHandler("al", self._cmd_al))
                app.add_handler(CommandHandler("sat", self._cmd_sat))
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
            "<b>Bilgi:</b>\n"
            "/durum — Sistem durumu ve piyasa rejimi\n"
            "/portfoy — Portfoy ozeti (nakit + yatirim)\n"
            "/pozisyon — Acik pozisyonlar ve canli PnL\n"
            "/sinyal — Son sinyaller\n"
            "/scan — Manuel tarama baslat\n"
            "\n"
            "<b>Islem:</b>\n"
            "/al THYAO 315.50 100 — Alis kaydet (sembol fiyat lot)\n"
            "/sat 1 — Pozisyonu kapat (trade ID)\n"
            "/sat 1 50 328.00 — Kismi satis (ID lot fiyat)\n"
            "\n"
            "<b>Nakit:</b>\n"
            "/nakit — Bakiye\n"
            "/nakit ekle 50000 — Yatir\n"
            "/nakit cek 10000 — Cek\n"
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

    def _get_current_price(self, symbol: str) -> float | None:
        """Get current price for a symbol."""
        try:
            import borsapy as bp
            df = bp.Ticker(symbol).history(period="5d", interval="1d")
            if df is not None and len(df) > 0:
                return float(df.iloc[-1]["Close"])
        except Exception:
            pass
        return None

    async def _cmd_pozisyon(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Show open positions, grouped by symbol."""
        if not self.repo:
            await update.message.reply_text("DB hazir degil.")
            return

        open_trades = self.repo.get_open_trades()
        if not open_trades:
            await update.message.reply_text("Acik pozisyon yok.")
            return

        # Group trades by symbol
        grouped: dict[str, list[dict]] = {}
        for trade in open_trades:
            symbol = trade["symbol"]
            grouped.setdefault(symbol, []).append(trade)

        lines = [f"📈 <b>Acik Pozisyonlar</b>\n"]

        for symbol, trades in grouped.items():
            current = self._get_current_price(symbol)

            # Calculate combined position
            total_shares = sum(t.get("shares", 0) for t in trades)
            total_cost = sum(t.get("entry_price", 0) * t.get("shares", 0) for t in trades)
            avg_cost = total_cost / total_shares if total_shares > 0 else 0

            if current:
                pnl = (current - avg_cost) * total_shares
                pnl_pct = (current - avg_cost) / avg_cost * 100 if avg_cost else 0
                emoji = "📈" if pnl >= 0 else "📉"
                lines.append(
                    f"\n{emoji} <b>{symbol}</b> — {current:.2f} TL\n"
                    f"  Toplam: {total_shares:.0f} lot | Ort. maliyet: {avg_cost:.2f}\n"
                    f"  PnL: {pnl:+,.0f} TL ({pnl_pct:+.1f}%)\n"
                )
            else:
                lines.append(
                    f"\n❓ <b>{symbol}</b>\n"
                    f"  Toplam: {total_shares:.0f} lot | Ort. maliyet: {avg_cost:.2f}\n"
                )

            # Individual trades
            for t in trades:
                tid = t["id"]
                entry = t.get("entry_price", 0)
                shares = t.get("shares", 0)
                sl = t.get("stop_loss")
                tp1 = t.get("take_profit_1")
                tp2 = t.get("take_profit_2")
                detail = f"  <i>#{tid} {entry:.2f} x{shares:.0f}"
                if sl:
                    detail += f" | SL:{sl:.2f}"
                if tp1:
                    detail += f" | TP1:{tp1:.2f}"
                if tp2:
                    detail += f" | TP2:{tp2:.2f}"
                detail += "</i>\n"
                lines.append(detail)

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

    async def _cmd_al(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Record a buy and auto-calculate TP/SL levels.

        Usage: /al THYAO 315.50 100
        """
        if not self.repo:
            await update.message.reply_text("DB hazir degil.")
            return

        args = context.args or []
        if len(args) < 3:
            await update.message.reply_text(
                "Kullanim: /al SEMBOL FIYAT LOT\n"
                "Ornek: /al THYAO 315.50 100"
            )
            return

        try:
            symbol = args[0].upper()
            entry_price = float(args[1])
            shares = int(args[2])
        except (ValueError, IndexError):
            await update.message.reply_text("Gecersiz format. Ornek: /al THYAO 315.50 100")
            return

        # Calculate TP/SL using ATR from live data
        try:
            import borsapy as bp
            from swing_tracker.core.signals import _add_all_indicators

            ticker = bp.Ticker(symbol)
            df = ticker.history(period="3mo", interval="1d")
            if df is not None and len(df) > 14:
                df = _add_all_indicators(df)
                last = df.iloc[-1]
                atr = float(last.get("ATR", 0)) or float(last.get("ATR_14", 0))
            else:
                atr = entry_price * 0.03  # fallback: %3
        except Exception:
            atr = entry_price * 0.03

        strategy = self.repo._conn.execute("SELECT 1").fetchone()  # DB check
        sl_mult = 1.5
        tp1_mult = 1.5
        tp2_mult = 3.0

        sl = round(entry_price - atr * sl_mult, 2)
        tp1 = round(entry_price + atr * tp1_mult, 2)
        tp2 = round(entry_price + atr * tp2_mult, 2)
        total_cost = entry_price * shares

        # Save to DB
        from datetime import datetime
        trade_id = self.repo.create_trade(
            symbol=symbol,
            direction="long",
            status="open",
            entry_price=entry_price,
            entry_date=datetime.now().strftime("%Y-%m-%d"),
            shares=shares,
            stop_loss=sl,
            take_profit_1=tp1,
            take_profit_2=tp2,
            entry_reasons=["manuel giris"],
            signal_score=0,
        )

        # Deduct cash
        self.repo.add_cash_transaction(
            -total_cost, "buy", related_trade_id=trade_id,
            description=f"{symbol} alim {shares} lot @ {entry_price}",
        )

        # Add to holdings
        self.repo.add_holding(
            symbol=symbol, asset_type="stock",
            shares=shares, cost_per_share=entry_price,
        )

        sl_pct = (entry_price - sl) / entry_price * 100
        tp1_pct = (tp1 - entry_price) / entry_price * 100
        tp2_pct = (tp2 - entry_price) / entry_price * 100
        rr = round(tp1_pct / sl_pct, 1) if sl_pct > 0 else 0

        balance = self.repo.get_cash_balance()

        text = (
            f"✅ <b>ALIS KAYDEDILDI</b>\n"
            f"\n"
            f"#{trade_id} {symbol} @ {entry_price:.2f} TL x{shares} lot\n"
            f"Toplam: {total_cost:,.0f} TL\n"
            f"\n"
            f"📐 <b>Otomatik TP/SL (ATR bazli):</b>\n"
            f"  🔴 SL: {sl:.2f} (-{sl_pct:.1f}%)\n"
            f"  🎯 TP1: {tp1:.2f} (+{tp1_pct:.1f}%) — %50 sat\n"
            f"  🎯 TP2: {tp2:.2f} (+{tp2_pct:.1f}%) — %30 sat\n"
            f"  R/R: {rr}x\n"
            f"\n"
            f"💰 Kalan nakit: {balance:,.0f} TL\n"
            f"\n"
            f"Pozisyon her 5 dk kontrol edilecek.\n"
            f"TP/SL tetiklenince bildirim gelecek."
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _cmd_sat(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Close or partially close a position.

        Usage:
          /sat 1           → Close trade #1 at market
          /sat 1 50 328.00 → Sell 50 shares of trade #1 at 328.00
        """
        if not self.repo:
            await update.message.reply_text("DB hazir degil.")
            return

        args = context.args or []
        if not args:
            # Show open trades for reference
            open_trades = self.repo.get_open_trades()
            if not open_trades:
                await update.message.reply_text("Acik pozisyon yok.")
                return
            lines = ["Kullanim: /sat ID [lot fiyat]\n\nAcik pozisyonlar:"]
            for t in open_trades:
                lines.append(f"  #{t['id']} {t['symbol']} @ {t.get('entry_price', 0):.2f} x{t.get('shares', 0)} lot")
            await update.message.reply_text("\n".join(lines))
            return

        try:
            trade_id = int(args[0])
        except ValueError:
            await update.message.reply_text("Gecersiz trade ID. Ornek: /sat 1")
            return

        trade = self.repo.get_trade(trade_id)
        if not trade:
            await update.message.reply_text(f"Trade #{trade_id} bulunamadi.")
            return
        if trade["status"] not in ("open", "partial_exit"):
            await update.message.reply_text(f"Trade #{trade_id} zaten kapali.")
            return

        symbol = trade["symbol"]
        entry_price = trade.get("entry_price", 0)

        # Determine sell price and shares
        if len(args) >= 3:
            sell_shares = int(args[1])
            sell_price = float(args[2])
        elif len(args) == 2:
            sell_shares = int(args[1])
            sell_price = self._get_current_price(symbol)
            if not sell_price:
                await update.message.reply_text("Fiyat alinamadi. Fiyati belirt: /sat 1 50 328.00")
                return
        else:
            # Full close at market
            sell_shares = trade.get("shares", 0)
            sell_price = self._get_current_price(symbol)
            if not sell_price:
                await update.message.reply_text("Fiyat alinamadi. Fiyati belirt: /sat 1 100 328.00")
                return

        pnl = (sell_price - entry_price) * sell_shares
        pnl_pct = (sell_price - entry_price) / entry_price * 100 if entry_price else 0

        # Record exit
        self.repo.record_exit(
            trade_id=trade_id,
            exit_type="manual",
            shares=sell_shares,
            price=sell_price,
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 2),
        )

        # Return cash
        self.repo.add_cash_transaction(
            sell_price * sell_shares, "sell", related_trade_id=trade_id,
            description=f"{symbol} satis {sell_shares} lot @ {sell_price:.2f}",
        )

        # Update trade status
        remaining = trade.get("shares", 0) - sell_shares
        exits = self.repo.get_trade_exits(trade_id)
        total_exited = sum(e.get("shares", 0) for e in exits)

        if total_exited >= trade.get("shares", 0):
            self.repo.update_trade_status(trade_id, "closed", realized_pnl=sum(e.get("pnl", 0) for e in exits))
            status_text = "KAPANDI"
            # Remove from holdings
            self.repo.remove_holding(symbol)
        else:
            self.repo.update_trade_status(trade_id, "partial_exit")
            status_text = f"KISMI SATIS ({total_exited}/{trade.get('shares', 0)} lot)"
            # Update holding shares
            self.repo.add_holding(
                symbol=symbol, asset_type="stock",
                shares=trade.get("shares", 0) - total_exited,
                cost_per_share=entry_price,
            )

        balance = self.repo.get_cash_balance()
        emoji = "📈" if pnl >= 0 else "📉"

        text = (
            f"{emoji} <b>SATIS: {symbol}</b> — {status_text}\n"
            f"\n"
            f"#{trade_id} {sell_shares} lot @ {sell_price:.2f} TL\n"
            f"PnL: {pnl:+,.0f} TL ({pnl_pct:+.1f}%)\n"
            f"💰 Nakit: {balance:,.0f} TL"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

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
                current = self._get_current_price(symbol)
                if current and entry:
                    pnl_pct = (current - entry) / entry * 100
                    emoji = "📈" if pnl_pct >= 0 else "📉"
                    text += f"  {emoji} {symbol}: {pnl_pct:+.1f}%\n"
                else:
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
