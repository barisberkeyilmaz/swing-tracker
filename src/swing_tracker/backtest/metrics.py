"""Performance metrics calculation for backtest results."""

from __future__ import annotations

from datetime import datetime

from swing_tracker.backtest.models import BacktestMetrics, BacktestTrade


def calculate_metrics(
    trades: list[BacktestTrade],
    equity_curve: list[tuple[str, float]],
    initial_cash: float,
) -> BacktestMetrics:
    """Calculate performance metrics from completed trades."""
    if not trades:
        return BacktestMetrics()

    closed = [t for t in trades if t.status == "closed"]
    if not closed:
        return BacktestMetrics()

    pnls = [t.total_pnl for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_trades = len(closed)
    winning_trades = len(wins)
    losing_trades = len(losses)
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0

    gross_wins = sum(wins)
    gross_losses = abs(sum(losses))
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else float("inf")

    # Max drawdown from equity curve
    max_dd, max_dd_pct = _calculate_drawdown(equity_curve)

    # Total return
    final_equity = equity_curve[-1][1] if equity_curve else initial_cash
    total_return = final_equity - initial_cash
    total_return_pct = (total_return / initial_cash * 100) if initial_cash > 0 else 0

    # Average holding days
    holding_days = []
    for t in closed:
        if t.exits:
            last_exit_date = max(e.date for e in t.exits)
            try:
                entry = datetime.strptime(t.entry_date, "%Y-%m-%d")
                exit_d = datetime.strptime(last_exit_date, "%Y-%m-%d")
                holding_days.append((exit_d - entry).days)
            except ValueError:
                pass
    avg_holding = sum(holding_days) / len(holding_days) if holding_days else 0

    return BacktestMetrics(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=round(win_rate, 1),
        avg_win=round(avg_win, 2),
        avg_loss=round(avg_loss, 2),
        avg_pnl=round(avg_pnl, 2),
        max_drawdown=round(max_dd, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        profit_factor=round(profit_factor, 2),
        total_return=round(total_return, 2),
        total_return_pct=round(total_return_pct, 2),
        avg_holding_days=round(avg_holding, 1),
    )


def _calculate_drawdown(equity_curve: list[tuple[str, float]]) -> tuple[float, float]:
    """Calculate maximum drawdown from equity curve."""
    if not equity_curve:
        return 0.0, 0.0

    peak = equity_curve[0][1]
    max_dd = 0.0
    max_dd_pct = 0.0

    for _, equity in equity_curve:
        if equity > peak:
            peak = equity
        dd = peak - equity
        dd_pct = (dd / peak * 100) if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct

    return max_dd, max_dd_pct


def format_report(metrics: BacktestMetrics, params: dict | None = None) -> str:
    """Format metrics as a readable Turkish report."""
    lines = [
        "=" * 50,
        "  BACKTEST SONUCLARI",
        "=" * 50,
        "",
    ]

    if params:
        lines.append("Parametreler:")
        for k, v in params.items():
            lines.append(f"  {k}: {v}")
        lines.append("")

    lines.extend([
        f"Toplam Trade:      {metrics.total_trades}",
        f"Kazanan:           {metrics.winning_trades} ({metrics.win_rate:.1f}%)",
        f"Kaybeden:          {metrics.losing_trades}",
        "",
        f"Ort. Kazanc:       {metrics.avg_win:+,.0f} TL",
        f"Ort. Kayip:        {metrics.avg_loss:+,.0f} TL",
        f"Ort. PnL:          {metrics.avg_pnl:+,.0f} TL",
        "",
        f"Profit Factor:     {metrics.profit_factor:.2f}",
        f"Max Drawdown:      {metrics.max_drawdown:,.0f} TL ({metrics.max_drawdown_pct:.1f}%)",
        "",
        f"Toplam Getiri:     {metrics.total_return:+,.0f} TL ({metrics.total_return_pct:+.1f}%)",
        f"Ort. Pozisyon:     {metrics.avg_holding_days:.0f} gun",
        "",
        "=" * 50,
    ])

    return "\n".join(lines)


def compare_results(results: list[tuple[str, BacktestMetrics]]) -> str:
    """Compare multiple backtest results side by side."""
    if not results:
        return "Karsilastirilacak sonuc yok."

    header = f"{'Parametre':<25} {'Trade':>6} {'Win%':>6} {'PF':>6} {'Getiri%':>8} {'MaxDD%':>7}"
    lines = [header, "-" * 65]

    for label, m in results:
        lines.append(
            f"{label:<25} {m.total_trades:>6} {m.win_rate:>5.1f}% "
            f"{m.profit_factor:>5.2f} {m.total_return_pct:>+7.1f}% {m.max_drawdown_pct:>6.1f}%"
        )

    return "\n".join(lines)
