"""
google_oauth_setup.py — get a fresh refresh_token for the demo Google account(s).

Two modes:

  Default:                          one-shot OAuth, writes to
                                    ROCKETRIDE_GOOGLE_REFRESH_TOKEN (and
                                    ROCKETRIDE_GOOGLE_USER_EMAIL).
                                    Use this for the primary account.

  --participant:                    OAuth a *second* Google account, fetch its
                                    email via /oauth2/v2/userinfo, and merge
                                    {email: refresh_token} into
                                    ROCKETRIDE_CALENDAR_TOKENS_JSON.
                                    Use this to add a teammate calendar so the
                                    multi-participant slot finder has more
                                    constraints to work with.

Sign in with the *target* Google account when the browser opens.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import requests
from google_auth_oauthlib.flow import InstalledAppFlow

ENV_PATH = Path(__file__).parent / ".env"

PRIMARY_SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]

PARTICIPANT_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


def _read_env() -> dict:
    out: dict = {}
    if not ENV_PATH.exists():
        return out
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env_var(key: str, value: str, *, quote: bool = False) -> None:
    text = ENV_PATH.read_text() if ENV_PATH.exists() else ""
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    rendered = f"{key}='{value}'" if quote else f"{key}={value}"
    if pattern.search(text):
        text = pattern.sub(rendered, text)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += rendered + "\n"
    ENV_PATH.write_text(text)


def _userinfo_email(access_token: str) -> str | None:
    try:
        r = requests.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json().get("email")
    except Exception:
        return None


def _client_config(env: dict) -> dict:
    cid = env.get("ROCKETRIDE_GOOGLE_CLIENT_ID")
    cs  = env.get("ROCKETRIDE_GOOGLE_CLIENT_SECRET")
    if not cid or not cs:
        raise SystemExit("ERROR: ROCKETRIDE_GOOGLE_CLIENT_ID / ROCKETRIDE_GOOGLE_CLIENT_SECRET missing in .env")
    return {
        "installed": {
            "client_id":     cid,
            "client_secret": cs,
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def run_primary() -> int:
    env = _read_env()
    flow = InstalledAppFlow.from_client_config(_client_config(env), PRIMARY_SCOPES)
    print("Browser opening — sign in as the PRIMARY demo account.")
    print("  (the receiver inbox: msadi.finalproject@gmail.com or whichever you use)")
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    if not creds.refresh_token:
        print("ERROR: no refresh_token returned. Revoke at https://myaccount.google.com/permissions and re-run.")
        return 1
    email = _userinfo_email(creds.token) or env.get("ROCKETRIDE_GOOGLE_USER_EMAIL", "")
    _write_env_var("ROCKETRIDE_GOOGLE_REFRESH_TOKEN", creds.refresh_token)
    if email:
        _write_env_var("ROCKETRIDE_GOOGLE_USER_EMAIL", email)
    print(f"\nOK. Wrote PRIMARY refresh_token (email={email or '?'}) to {ENV_PATH}")
    return 0


def run_participant() -> int:
    env = _read_env()
    flow = InstalledAppFlow.from_client_config(_client_config(env), PARTICIPANT_SCOPES)
    print("Browser opening — sign in as the SECOND (participant) account.")
    print("  This is a teammate whose calendar should be checked when scheduling meetings.")
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    if not creds.refresh_token:
        print("ERROR: no refresh_token returned. Revoke at https://myaccount.google.com/permissions and re-run.")
        return 1

    email = _userinfo_email(creds.token)
    if not email:
        print("ERROR: could not fetch userinfo email. Token granted but unusable for participant key.")
        return 1
    email = email.strip().lower()

    # Merge into ROCKETRIDE_CALENDAR_TOKENS_JSON
    raw = env.get("ROCKETRIDE_CALENDAR_TOKENS_JSON", "")
    cur: dict = {}
    if raw:
        try:
            cur = json.loads(raw)
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            cur = {}
    cur[email] = creds.refresh_token
    encoded = json.dumps(cur, separators=(",", ":"))
    _write_env_var("ROCKETRIDE_CALENDAR_TOKENS_JSON", encoded, quote=True)

    print(f"\nOK. Added participant {email} to ROCKETRIDE_CALENDAR_TOKENS_JSON ({len(cur)} total).")
    print(f"  (Wrote to {ENV_PATH}.) Restart app.py to pick up the change.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--participant", action="store_true",
                        help="Add a second Google account to ROCKETRIDE_CALENDAR_TOKENS_JSON")
    args = parser.parse_args()
    if args.participant:
        return run_participant()
    return run_primary()


if __name__ == "__main__":
    sys.exit(main())
