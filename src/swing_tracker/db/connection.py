"""SQLite database connection management."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from swing_tracker.db.schema import create_all_tables


def get_connection(db_path: Path) -> sqlite3.Connection:
    """Create a SQLite connection with WAL mode and return it."""
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    create_all_tables(conn)
    return conn
