"""
pending_meetings.py — in-memory session store for the human-in-the-loop step
between "agent proposed slots" and "user picked one".

Replaces the Redis store the original Lambda used. We're a single long-running
process, so a thread-safe dict + uuid keys is enough.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

_LOCK = threading.Lock()
_STORE: dict[str, "PendingMeeting"] = {}
_TTL_SECONDS = 60 * 60  # one hour


@dataclass
class PendingMeeting:
    sender: str
    subject: str
    attendees: list[str]
    top_slots: list[dict]                    # [{start, end, reason}, ...]
    cod_verdicts: list[dict] = field(default_factory=list)  # full Proposer/Challenger/Judge JSON
    intent: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    channel_id: Optional[str] = None
    message_ts: Optional[str] = None         # set after the Slack card posts


def save(meeting: PendingMeeting) -> str:
    """Stash a pending meeting and return its session id."""
    sid = str(uuid4())
    with _LOCK:
        _purge_expired()
        _STORE[sid] = meeting
    return sid


def load(sid: str) -> Optional[PendingMeeting]:
    with _LOCK:
        _purge_expired()
        return _STORE.get(sid)


def update(sid: str, **fields) -> None:
    with _LOCK:
        m = _STORE.get(sid)
        if not m:
            return
        for k, v in fields.items():
            setattr(m, k, v)


def remove(sid: str) -> None:
    with _LOCK:
        _STORE.pop(sid, None)


def _purge_expired() -> None:
    now = time.time()
    expired = [sid for sid, m in _STORE.items() if now - m.created_at > _TTL_SECONDS]
    for sid in expired:
        _STORE.pop(sid, None)
