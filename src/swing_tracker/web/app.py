"""FastAPI web application for Swing Tracker dashboard."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from swing_tracker.config import load_config
from swing_tracker.db.connection import get_connection
from swing_tracker.db.repository import Repository
from swing_tracker.web.auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    auth_enabled,
    check_password,
    create_session_token,
    verify_session_token,
)
from swing_tracker.web.dependencies import init_state
from swing_tracker.web.routers import dashboard, portfolio, signals, symbol, trades

logger = logging.getLogger("swing_tracker.web")

WEB_DIR = Path(__file__).parent
STATIC_DIR = WEB_DIR / "static"
_login_templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# Paths that skip authentication
_PUBLIC_PATHS = {"/login", "/favicon.ico"}
_PUBLIC_PREFIXES = ("/static/",)


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


# --- Authentication middleware ---

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Check session cookie on every request (if auth is enabled)."""
    if not auth_enabled():
        return await call_next(request)

    path = request.url.path
    if path in _PUBLIC_PATHS or any(path.startswith(p) for p in _PUBLIC_PREFIXES):
        return await call_next(request)

    token = request.cookies.get(SESSION_COOKIE)
    if token and verify_session_token(token):
        return await call_next(request)

    return RedirectResponse("/login", status_code=303)


# --- Login / Logout routes ---

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if not auth_enabled():
        return RedirectResponse("/", status_code=303)
    # Already logged in?
    token = request.cookies.get(SESSION_COOKIE)
    if token and verify_session_token(token):
        return RedirectResponse("/", status_code=303)
    return _login_templates.TemplateResponse(
        request, "login.html", context={"error": error}
    )


@app.post("/login")
async def login_submit(request: Request, password: str = Form(...)):
    if check_password(password):
        token = create_session_token()
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="lax",
        )
        return response
    return _login_templates.TemplateResponse(
        request, "login.html", context={"error": "Yanlis sifre"}, status_code=401
    )


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(dashboard.router)
app.include_router(portfolio.router)
app.include_router(signals.router)
app.include_router(trades.router)
app.include_router(symbol.router)


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
