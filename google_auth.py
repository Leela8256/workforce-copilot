"""
google_auth.py — refresh-token-based Google access token helper.

The Google Calendar / Gmail APIs need a Bearer access_token that expires every
~1 hour. We mint a fresh one on demand using the offline refresh_token stored
in .env. Callers cache for ~50 minutes.

No tokens are persisted to disk (other than refresh_token in .env) — kept in
process memory.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

# Cache keyed by refresh_token so we can hold tokens for multiple participants
_ACCESS_CACHE: dict[str, tuple[str, float]] = {}


@dataclass(frozen=True)
class GoogleCreds:
    access_token: str
    expires_at: float


def _refresh(refresh_token: str) -> GoogleCreds:
    resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id":     os.environ["ROCKETRIDE_GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["ROCKETRIDE_GOOGLE_CLIENT_SECRET"],
            "refresh_token": refresh_token,
            "grant_type":    "refresh_token",
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if "access_token" not in body:
        raise RuntimeError(f"Google refresh failed: {body}")
    return GoogleCreds(
        access_token=body["access_token"],
        expires_at=time.time() + int(body.get("expires_in", 3500)) - 60,
    )


def get_access_token(refresh_token: str | None = None) -> str:
    """
    Return a fresh Google access token.
    If refresh_token is provided, mint/refresh for that user.
    Otherwise fall back to ROCKETRIDE_GOOGLE_REFRESH_TOKEN (the demo user).
    """
    rt = refresh_token or os.environ["ROCKETRIDE_GOOGLE_REFRESH_TOKEN"]
    cached = _ACCESS_CACHE.get(rt)
    if cached is None or cached[1] <= time.time():
        creds = _refresh(rt)
        _ACCESS_CACHE[rt] = (creds.access_token, creds.expires_at)
    return _ACCESS_CACHE[rt][0]


def auth_header(refresh_token: str | None = None) -> str:
    """Return the value for the Authorization header (Bearer + access_token)."""
    return f"Bearer {get_access_token(refresh_token)}"


def participant_tokens_from_env() -> dict[str, str]:
    """
    Returns {email_lower: refresh_token} merged from:
      - ROCKETRIDE_CALENDAR_TOKENS_JSON (a JSON object env var, optional)
      - ROCKETRIDE_GOOGLE_USER_EMAIL + ROCKETRIDE_GOOGLE_REFRESH_TOKEN (the single-user fallback)

    This mirrors the CALENDAR_TOKENS_JSON pattern in the original src/state.py.
    """
    import json as _json
    out: dict[str, str] = {}

    raw = os.environ.get("ROCKETRIDE_CALENDAR_TOKENS_JSON", "")
    if raw:
        try:
            data = _json.loads(raw)
            if isinstance(data, dict):
                for email, tok in data.items():
                    if isinstance(email, str) and isinstance(tok, str) and "@" in email:
                        out[email.strip().lower()] = tok
        except Exception:
            pass

    user = (os.environ.get("ROCKETRIDE_GOOGLE_USER_EMAIL") or "").strip().lower()
    rt   = os.environ.get("ROCKETRIDE_GOOGLE_REFRESH_TOKEN")
    if user and rt and user not in out:
        out[user] = rt

    return out
