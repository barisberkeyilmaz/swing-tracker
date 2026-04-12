"""FastAPI web application for Swing Tracker dashboard."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from swing_tracker.config import load_config
from swing_tracker.db.connection import get_connection
from swing_tracker.db.repository import Repository
from swing_tracker.web.dependencies import init_state
from swing_tracker.web.routers import dashboard, portfolio, signals, trades

logger = logging.getLogger("swing_tracker.web")

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize DB connection and shared state on startup."""
    config = load_config()
    conn = get_connection(config.db_path)
    repo = Repository(conn)
    init_state(repo, config)
    logger.info("Web app baslatildi — DB: %s", config.db_path)
    yield
    conn.close()
    logger.info("Web app kapatildi")


app = FastAPI(
    title="Swing Tracker",
    description="BIST Swing Trading Sinyal Sistemi",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(dashboard.router)
app.include_router(portfolio.router)
app.include_router(signals.router)
app.include_router(trades.router)


def main():
    """Entry point for swing-tracker-web command."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    uvicorn.run(
        "swing_tracker.web.app:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )


if __name__ == "__main__":
    main()
