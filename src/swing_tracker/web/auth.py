"""Simple cookie-based authentication for the web dashboard.

If WEB_PASSWORD is not set in .env, authentication is completely disabled.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env before reading env vars — auth.py is imported before lifespan runs
load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

logger = logging.getLogger(__name__)

WEB_PASSWORD: str | None = os.getenv("WEB_PASSWORD")
SECRET_KEY: str = os.getenv("WEB_SECRET_KEY", "")

if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    if WEB_PASSWORD:
        logger.warning(
            "WEB_SECRET_KEY ayarlanmamis, otomatik uretildi. "
            "Restart'ta oturumlar gecersiz olacak."
        )

SESSION_COOKIE = "st_session"
SESSION_TTL = 86400  # 24 hours


def auth_enabled() -> bool:
    """Return True if authentication is configured."""
    return bool(WEB_PASSWORD)


def check_password(password: str) -> bool:
    """Verify the given password against WEB_PASSWORD (constant-time)."""
    if not WEB_PASSWORD:
        return False
    return hmac.compare_digest(password.encode(), WEB_PASSWORD.encode())


def create_session_token() -> str:
    """Create an HMAC-signed session token with current timestamp."""
    timestamp = str(int(time.time()))
    signature = hmac.new(
        SECRET_KEY.encode(), timestamp.encode(), hashlib.sha256
    ).hexdigest()
    return f"{timestamp}.{signature}"


def verify_session_token(token: str) -> bool:
    """Verify a session token's signature and TTL."""
    if not token or "." not in token:
        return False
    try:
        timestamp_str, signature = token.split(".", 1)
        expected = hmac.new(
            SECRET_KEY.encode(), timestamp_str.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        created_at = int(timestamp_str)
        return (time.time() - created_at) < SESSION_TTL
    except (ValueError, TypeError):
        return False
