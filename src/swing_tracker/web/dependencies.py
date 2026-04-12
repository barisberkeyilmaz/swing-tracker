"""Shared dependencies for FastAPI routes."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from swing_tracker.config import Config
from swing_tracker.db.repository import Repository

WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


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
