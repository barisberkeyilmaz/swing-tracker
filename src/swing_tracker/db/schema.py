"""SQLite database schema definitions."""

TABLES = [
    """
    CREATE TABLE IF NOT EXISTS portfolio_holdings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL UNIQUE,
        asset_type TEXT NOT NULL CHECK(asset_type IN ('stock','fx','crypto','fund')),
        shares REAL NOT NULL DEFAULT 0,
        cost_per_share REAL,
        category TEXT,
        auto_buy_monthly REAL DEFAULT 0,
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS swing_trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL CHECK(direction IN ('long','short')),
        status TEXT NOT NULL CHECK(status IN ('watching','open','partial_exit','closed','cancelled')),
        entry_price REAL,
        entry_date TEXT,
        shares REAL DEFAULT 0,
        total_cost REAL DEFAULT 0,
        stop_loss REAL,
        take_profit_1 REAL,
        take_profit_2 REAL,
        take_profit_3 REAL,
        exit_price_avg REAL,
        exit_date TEXT,
        realized_pnl REAL,
        realized_pnl_pct REAL,
        entry_reasons TEXT,
        signal_score INTEGER,
        strategy TEXT DEFAULT 'default',
        notes TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        updated_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_exits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id INTEGER NOT NULL REFERENCES swing_trades(id),
        exit_type TEXT NOT NULL CHECK(exit_type IN ('tp1','tp2','tp3','sl','manual','trailing')),
        shares REAL NOT NULL,
        price REAL NOT NULL,
        pnl REAL,
        pnl_pct REAL,
        exit_date TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        signal_type TEXT NOT NULL CHECK(signal_type IN ('buy','sell')),
        indicator TEXT NOT NULL,
        strength TEXT CHECK(strength IN ('strong','medium','weak')),
        price_at_signal REAL,
        indicator_values TEXT,
        score INTEGER,
        acted_on INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS portfolio_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL UNIQUE,
        total_value REAL,
        cash_balance REAL,
        invested_value REAL,
        total_pnl REAL,
        total_pnl_pct REAL,
        swing_pnl REAL,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cash_transactions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        amount REAL NOT NULL,
        transaction_type TEXT NOT NULL CHECK(
            transaction_type IN ('deposit','withdrawal','buy','sell','dividend','auto_buy')
        ),
        related_trade_id INTEGER REFERENCES swing_trades(id),
        description TEXT,
        created_at TEXT DEFAULT (datetime('now'))
    )
    """,
]


def create_all_tables(conn) -> None:
    """Create all tables if they don't exist."""
    cursor = conn.cursor()
    for ddl in TABLES:
        cursor.execute(ddl)
    conn.commit()
