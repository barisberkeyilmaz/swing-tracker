"""Database CRUD operations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime


class Repository:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    # ── Portfolio Holdings ──

    def add_holding(
        self,
        symbol: str,
        asset_type: str,
        shares: float,
        cost_per_share: float | None = None,
        category: str | None = None,
        auto_buy_monthly: float = 0,
        notes: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO portfolio_holdings
               (symbol, asset_type, shares, cost_per_share, category, auto_buy_monthly, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                 shares = excluded.shares,
                 cost_per_share = excluded.cost_per_share,
                 category = excluded.category,
                 auto_buy_monthly = excluded.auto_buy_monthly,
                 notes = excluded.notes,
                 updated_at = datetime('now')""",
            (symbol, asset_type, shares, cost_per_share, category, auto_buy_monthly, notes),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_holding(self, symbol: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM portfolio_holdings WHERE symbol = ?", (symbol,)
        ).fetchone()
        return dict(row) if row else None

    def get_all_holdings(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM portfolio_holdings").fetchall()
        return [dict(r) for r in rows]

    def get_holdings_by_category(self, category: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM portfolio_holdings WHERE category = ?", (category,)
        ).fetchall()
        return [dict(r) for r in rows]

    def remove_holding(self, symbol: str) -> None:
        self._conn.execute("DELETE FROM portfolio_holdings WHERE symbol = ?", (symbol,))
        self._conn.commit()

    # ── Swing Trades ──

    def create_trade(
        self,
        symbol: str,
        direction: str,
        status: str = "watching",
        entry_price: float | None = None,
        entry_date: str | None = None,
        shares: float = 0,
        stop_loss: float | None = None,
        take_profit_1: float | None = None,
        take_profit_2: float | None = None,
        take_profit_3: float | None = None,
        entry_reasons: list[str] | None = None,
        signal_score: int | None = None,
        strategy: str = "default",
        notes: str | None = None,
    ) -> int:
        total_cost = (entry_price or 0) * shares
        cur = self._conn.execute(
            """INSERT INTO swing_trades
               (symbol, direction, status, entry_price, entry_date, shares, total_cost,
                stop_loss, take_profit_1, take_profit_2, take_profit_3,
                entry_reasons, signal_score, strategy, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol, direction, status, entry_price, entry_date, shares, total_cost,
                stop_loss, take_profit_1, take_profit_2, take_profit_3,
                json.dumps(entry_reasons or [], ensure_ascii=False),
                signal_score, strategy, notes,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_trade(self, trade_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM swing_trades WHERE id = ?", (trade_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_open_trades(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM swing_trades WHERE status IN ('open', 'partial_exit')"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_trades_by_status(self, status: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM swing_trades WHERE status = ?", (status,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_trade_status(self, trade_id: int, status: str, **kwargs) -> None:
        sets = ["status = ?", "updated_at = datetime('now')"]
        params: list = [status]
        for col, val in kwargs.items():
            sets.append(f"{col} = ?")
            params.append(val)
        params.append(trade_id)
        self._conn.execute(
            f"UPDATE swing_trades SET {', '.join(sets)} WHERE id = ?", params
        )
        self._conn.commit()

    def delete_trade(self, trade_id: int) -> None:
        """Delete a trade, its exits, and related cash transactions."""
        self._conn.execute("DELETE FROM trade_exits WHERE trade_id = ?", (trade_id,))
        self._conn.execute("DELETE FROM cash_transactions WHERE related_trade_id = ?", (trade_id,))
        self._conn.execute("DELETE FROM swing_trades WHERE id = ?", (trade_id,))
        self._conn.commit()

    # ── Trade Exits ──

    def record_exit(
        self,
        trade_id: int,
        exit_type: str,
        shares: float,
        price: float,
        pnl: float | None = None,
        pnl_pct: float | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO trade_exits (trade_id, exit_type, shares, price, pnl, pnl_pct)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (trade_id, exit_type, shares, price, pnl, pnl_pct),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_exit(self, exit_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM trade_exits WHERE id = ?", (exit_id,)
        ).fetchone()
        return dict(row) if row else None

    def delete_exit(self, exit_id: int) -> None:
        self._conn.execute("DELETE FROM trade_exits WHERE id = ?", (exit_id,))
        self._conn.commit()

    def get_all_trade_exits(self) -> dict[int, list[dict]]:
        """Tum exit'leri tek query'de cek, trade_id'ye gore grupla. N+1 kaciniri."""
        rows = self._conn.execute(
            "SELECT * FROM trade_exits ORDER BY trade_id, id"
        ).fetchall()
        grouped: dict[int, list[dict]] = {}
        for r in rows:
            d = dict(r)
            grouped.setdefault(d["trade_id"], []).append(d)
        return grouped

    def get_trade_exits(self, trade_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trade_exits WHERE trade_id = ?", (trade_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_sell_transaction(self, trade_id: int, amount: float) -> bool:
        """En son eslesen sell cash transaction'i sil. Bulunamazsa False doner."""
        cur = self._conn.execute(
            """DELETE FROM cash_transactions WHERE id = (
                SELECT id FROM cash_transactions
                WHERE related_trade_id = ? AND transaction_type = 'sell'
                AND ABS(amount - ?) < 0.01
                ORDER BY created_at DESC LIMIT 1
            )""",
            (trade_id, amount),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_last_exit(self) -> dict | None:
        """Son yapilan exit kaydini dondurur (tum trade'ler arasinda)."""
        row = self._conn.execute(
            "SELECT * FROM trade_exits ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def delete_exit(self, exit_id: int) -> None:
        """Bir exit kaydini siler."""
        self._conn.execute("DELETE FROM trade_exits WHERE id = ?", (exit_id,))
        self._conn.commit()

    # ── Signals Log ──

    def log_signal(
        self,
        symbol: str,
        signal_type: str,
        indicator: str,
        strength: str,
        price_at_signal: float,
        indicator_values: dict | None = None,
        score: int | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO signals_log
               (symbol, signal_type, indicator, strength, price_at_signal, indicator_values, score)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol, signal_type, indicator, strength, price_at_signal,
                json.dumps(indicator_values or {}, ensure_ascii=False), score,
            ),
        )
        self._conn.commit()
        return cur.lastrowid

    def has_recent_signal(self, symbol: str, signal_type: str, hours: int = 24) -> bool:
        """Check if a signal was already logged for this symbol within the last N hours."""
        row = self._conn.execute(
            """SELECT 1 FROM signals_log
               WHERE symbol = ? AND signal_type = ?
               AND created_at > datetime('now', ?)
               LIMIT 1""",
            (symbol, signal_type, f"-{hours} hours"),
        ).fetchone()
        return row is not None

    def get_recent_signals(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM signals_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unacted_signals(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM signals_log WHERE acted_on = 0 ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_signal_acted(self, signal_id: int) -> None:
        self._conn.execute(
            "UPDATE signals_log SET acted_on = 1 WHERE id = ?", (signal_id,)
        )
        self._conn.commit()

    # ── Portfolio Snapshots ──

    def save_snapshot(
        self,
        date: str,
        total_value: float,
        cash_balance: float,
        invested_value: float,
        total_pnl: float | None = None,
        total_pnl_pct: float | None = None,
        swing_pnl: float | None = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO portfolio_snapshots
               (date, total_value, cash_balance, invested_value, total_pnl, total_pnl_pct, swing_pnl)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 total_value = excluded.total_value,
                 cash_balance = excluded.cash_balance,
                 invested_value = excluded.invested_value,
                 total_pnl = excluded.total_pnl,
                 total_pnl_pct = excluded.total_pnl_pct,
                 swing_pnl = excluded.swing_pnl""",
            (date, total_value, cash_balance, invested_value, total_pnl, total_pnl_pct, swing_pnl),
        )
        self._conn.commit()

    def get_snapshots(self, limit: int = 30) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM portfolio_snapshots ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── Cash Transactions ──

    def add_cash_transaction(
        self,
        amount: float,
        transaction_type: str,
        related_trade_id: int | None = None,
        description: str | None = None,
    ) -> int:
        cur = self._conn.execute(
            """INSERT INTO cash_transactions
               (amount, transaction_type, related_trade_id, description)
               VALUES (?, ?, ?, ?)""",
            (amount, transaction_type, related_trade_id, description),
        )
        self._conn.commit()
        return cur.lastrowid

    def get_cash_balance(self, transaction_types: tuple[str, ...] | None = None) -> float:
        if transaction_types:
            placeholders = ",".join("?" for _ in transaction_types)
            row = self._conn.execute(
                f"""SELECT COALESCE(SUM(amount), 0) as balance
                    FROM cash_transactions
                    WHERE transaction_type IN ({placeholders})""",
                transaction_types,
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(amount), 0) as balance FROM cash_transactions"
            ).fetchone()
        return row["balance"]

    def get_cash_transactions(
        self,
        limit: int = 50,
        transaction_types: tuple[str, ...] | None = None,
    ) -> list[dict]:
        if transaction_types:
            placeholders = ",".join("?" for _ in transaction_types)
            rows = self._conn.execute(
                f"""SELECT * FROM cash_transactions
                    WHERE transaction_type IN ({placeholders})
                    ORDER BY created_at DESC LIMIT ?""",
                (*transaction_types, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM cash_transactions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
