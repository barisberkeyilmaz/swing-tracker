"""Shared dependencies for FastAPI routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from swing_tracker.config import Config
from swing_tracker.db.repository import Repository
from swing_tracker.web.auth import auth_enabled

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

# Auth flag — used in base.html for logout button visibility
templates.env.globals["auth_enabled"] = auth_enabled()

# Turkish status label mapping — used in all templates via {{ STATUS_TR.get(key, key) }}
STATUS_TR = {
    "open": "ACIK",
    "partial_exit": "KISMI CIKIS",
    "closed": "KAPALI",
    "strong": "GUCLU",
    "medium": "ORTA",
    "long": "UZUN",
    "short": "KISA",
    "tp1": "TP1",
    "tp2": "TP2",
    "tp3": "TP3",
    "sl": "STOP LOSS",
    "trailing": "TAKIP STOP",
    "manual": "MANUEL",
    "deposit": "YATIRMA",
    "withdrawal": "CEKME",
    "buy": "ALIM",
    "sell": "SATIM",
}
templates.env.globals["STATUS_TR"] = STATUS_TR


class AppState:
    """Application-wide shared state, initialized at startup."""

    def __init__(self, repo: Repository, config: Config):
        self.repo = repo
        self.config = config


_state: AppState | None = None


def init_state(repo: Repository, config: Config) -> None:
    global _state
    _state = AppState(repo, config)


def get_state() -> AppState:
    assert _state is not None, "AppState not initialized"
    return _state


def get_repo() -> Repository:
    return get_state().repo


def get_config() -> Config:
    return get_state().config
