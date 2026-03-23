"""Exit rules for backtest trades: TP1, TP2, trailing stop, stop loss."""

from __future__ import annotations

from swing_tracker.backtest.models import BacktestConfig, BacktestTrade, TradeExit


def _calc_commission(price: float, shares: int, config: BacktestConfig) -> float:
    """Calculate commission for a trade exit."""
    if config.commission_fixed > 0:
        return config.commission_fixed
    return price * shares * (config.commission_pct / 100)


def check_exits(
    trade: BacktestTrade,
    date: str,
    high: float,
    low: float,
    close: float,
    config: BacktestConfig,
) -> list[TradeExit]:
    """Check if any exit conditions are met for a trade.

    Order matters: SL first, then TPs, then trailing.
    Returns list of exits triggered on this bar.
    """
    exits: list[TradeExit] = []

    if trade.status == "closed" or trade.remaining_shares <= 0:
        return exits

    # Update highest price
    if high > trade.highest_price:
        trade.highest_price = high

    # 1. Stop Loss — closes all remaining shares
    if low <= trade.stop_loss:
        exit_price = trade.stop_loss
        pnl = (exit_price - trade.entry_price) * trade.remaining_shares
        pnl -= _calc_commission(exit_price, trade.remaining_shares, config)
        pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100

        exits.append(TradeExit(
            date=date,
            price=exit_price,
            shares=trade.remaining_shares,
            exit_type="sl",
            pnl=round(pnl, 2),
            pnl_pct=round(pnl_pct, 2),
        ))
        trade.remaining_shares = 0
        trade.status = "closed"
        return exits

    # 2. TP1 — exit tp1_exit_pct of original position
    if not trade.tp1_hit and high >= trade.tp1:
        trade.tp1_hit = True
        tp1_shares = int(trade.shares * config.tp1_exit_pct)
        tp1_shares = min(tp1_shares, trade.remaining_shares)

        if tp1_shares > 0:
            exit_price = trade.tp1
            pnl = (exit_price - trade.entry_price) * tp1_shares
            pnl -= _calc_commission(exit_price, tp1_shares, config)
            pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100

            exits.append(TradeExit(
                date=date,
                price=exit_price,
                shares=tp1_shares,
                exit_type="tp1",
                pnl=round(pnl, 2),
                pnl_pct=round(pnl_pct, 2),
            ))
            trade.remaining_shares -= tp1_shares

    # 3. TP2 — exit tp2_exit_pct of original position
    tp2_already = any(e.exit_type == "tp2" for e in trade.exits + exits)
    if not tp2_already and high >= trade.tp2 and trade.remaining_shares > 0:
        tp2_shares = int(trade.shares * config.tp2_exit_pct)
        tp2_shares = min(tp2_shares, trade.remaining_shares)

        if tp2_shares > 0:
            exit_price = trade.tp2
            pnl = (exit_price - trade.entry_price) * tp2_shares
            pnl -= _calc_commission(exit_price, tp2_shares, config)
            pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100

            exits.append(TradeExit(
                date=date,
                price=exit_price,
                shares=tp2_shares,
                exit_type="tp2",
                pnl=round(pnl, 2),
                pnl_pct=round(pnl_pct, 2),
            ))
            trade.remaining_shares -= tp2_shares

    # 4. Trailing stop — only active after TP1 hit
    if trade.tp1_hit and trade.remaining_shares > 0:
        trail_level = trade.highest_price * (1 - config.trailing_stop_pct)
        if low <= trail_level:
            exit_price = trail_level
            pnl = (exit_price - trade.entry_price) * trade.remaining_shares
            pnl -= _calc_commission(exit_price, trade.remaining_shares, config)
            pnl_pct = (exit_price - trade.entry_price) / trade.entry_price * 100

            exits.append(TradeExit(
                date=date,
                price=round(exit_price, 2),
                shares=trade.remaining_shares,
                exit_type="trailing",
                pnl=round(pnl, 2),
                pnl_pct=round(pnl_pct, 2),
            ))
            trade.remaining_shares = 0

    # Close trade if no shares remaining
    if trade.remaining_shares <= 0:
        trade.status = "closed"

    # Record exits
    trade.exits.extend(exits)

    return exits


def close_trade_at_market(trade: BacktestTrade, date: str, price: float, config: BacktestConfig) -> TradeExit:
    """Force close a trade at market price (end of backtest)."""
    pnl = (price - trade.entry_price) * trade.remaining_shares
    pnl -= _calc_commission(price, trade.remaining_shares, config)
    pnl_pct = (price - trade.entry_price) / trade.entry_price * 100

    exit = TradeExit(
        date=date,
        price=price,
        shares=trade.remaining_shares,
        exit_type="trailing",
        pnl=round(pnl, 2),
        pnl_pct=round(pnl_pct, 2),
    )
    trade.exits.append(exit)
    trade.remaining_shares = 0
    trade.status = "closed"
    return exit
