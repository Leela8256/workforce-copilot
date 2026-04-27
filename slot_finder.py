"""
slot_finder.py — multi-participant free/busy + candidate-slot computation.

Ports the slot logic from src/calendar_cod.py:
  • One refresh_token per participant (CALENDAR_TOKENS_JSON pattern)
  • Walk the search window in 60-min steps, classify each slot as
        free        — all participants free
        partial     — exactly one participant has a conflict
                      (annotated with displaceability of that conflict)
  • Tier selection: prefer free; else displaceable conflicts; else any 1-conflict
  • Slots are returned as plain dicts ready to feed into cod_meeting_fanout.pipe

This module is pure Python — no LLM. The CoD agents do the *judging*; this
module does the *math*.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import requests

from google_auth import get_access_token, participant_tokens_from_env

logger = logging.getLogger(__name__)

PDT_TZ = timezone(timedelta(hours=-7), name="PDT")

_NON_URGENT_KEYWORDS = {
    "lunch", "1:1", "1-1", "one-on-one", "weekly sync", "weekly standup",
    "daily standup", "standup", "stand-up", "coffee", "coffee chat", "catch up",
    "catch-up", "team social", "happy hour", "offsite", "check-in", "check in",
    "team lunch", "team dinner", "social", "retrospective", "retro",
}


def _is_displaceable(title: str, is_recurring: bool) -> bool:
    lower = (title or "").lower()
    if any(kw in lower for kw in _NON_URGENT_KEYWORDS):
        return True
    if is_recurring and any(kw in lower for kw in {"sync", "standup", "weekly", "daily", "review"}):
        return True
    return False


@dataclass(frozen=True)
class Slot:
    start: datetime
    end:   datetime
    conflicts: list[dict]

    @property
    def conflict_count(self) -> int:
        return len(self.conflicts)

    def to_dict(self) -> dict:
        return {
            "start":          self.start.isoformat(),
            "end":            self.end.isoformat(),
            "label":          f"{self.start.strftime('%a %b %-d %-I:%M %p')} {self.start.strftime('%Z')}",
            "conflict_count": self.conflict_count,
            "conflicts":      self.conflicts,
        }


# ── Google Calendar fetch ─────────────────────────────────────────────────────

def _fetch_events(access_token: str, time_min: str, time_max: str) -> list[dict]:
    """Pull events for one calendar (always 'primary') in the given window."""
    resp = requests.get(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
        headers={"Authorization": f"Bearer {access_token}"},
        params={
            "timeMin":      time_min,
            "timeMax":      time_max,
            "singleEvents": True,
            "orderBy":      "startTime",
        },
        timeout=15,
    )
    resp.raise_for_status()
    out = []
    for item in resp.json().get("items", []):
        s = item.get("start", {})
        e = item.get("end", {})
        s_str = s.get("dateTime", s.get("date", ""))
        e_str = e.get("dateTime", e.get("date", ""))
        if not s_str or not e_str:
            continue
        out.append({
            "title":        item.get("summary") or "(No title)",
            "start":        s_str,
            "end":          e_str,
            "is_recurring": bool(item.get("recurringEventId")),
        })
    return out


# ── Slot enumeration (per-day) ────────────────────────────────────────────────

def _filter_events_for_day(all_events: list[dict], day: date) -> list[dict]:
    day_start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=PDT_TZ)
    day_end   = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=PDT_TZ)
    out = []
    for ev in all_events:
        try:
            s = datetime.fromisoformat(ev["start"].replace("Z", "+00:00")).astimezone(PDT_TZ)
            e = datetime.fromisoformat(ev["end"].replace("Z", "+00:00")).astimezone(PDT_TZ)
            if s < day_end and e > day_start:
                out.append(ev)
        except Exception:
            pass
    return out


def _enumerate_slots(
    events_by_participant: dict[str, list[dict]],
    day: date,
    start_hour: int = 9,
    end_hour:   int = 17,
    slot_minutes: int = 60,
) -> tuple[list[Slot], list[Slot]]:
    """
    Walk the work window per participant and classify each slot.
    Returns (free_slots, partial_slots) — free = no conflicts; partial = exactly 1 conflict.
    """
    window_start = datetime(day.year, day.month, day.day, start_hour, tzinfo=PDT_TZ)
    window_end   = datetime(day.year, day.month, day.day, end_hour,   tzinfo=PDT_TZ)
    delta = timedelta(minutes=slot_minutes)

    intervals: dict[str, list[dict]] = {}
    for who, events in events_by_participant.items():
        intervals[who] = []
        for ev in events:
            try:
                s = datetime.fromisoformat(ev["start"].replace("Z", "+00:00")).astimezone(PDT_TZ)
                e = datetime.fromisoformat(ev["end"].replace("Z", "+00:00")).astimezone(PDT_TZ)
                intervals[who].append({
                    "start":           s,
                    "end":             e,
                    "title":           ev["title"],
                    "is_recurring":    ev["is_recurring"],
                    "is_displaceable": _is_displaceable(ev["title"], ev["is_recurring"]),
                })
            except Exception:
                continue

    free, partial = [], []
    cursor = window_start
    while cursor + delta <= window_end:
        slot_end = cursor + delta
        conflicts: list[dict] = []
        for who, ivs in intervals.items():
            for iv in ivs:
                if iv["start"] < slot_end and iv["end"] > cursor:
                    conflicts.append({
                        "participant":     who,
                        "event_title":     iv["title"],
                        "is_recurring":    iv["is_recurring"],
                        "is_displaceable": iv["is_displaceable"],
                    })
                    break  # max one conflict per participant per slot
        slot = Slot(start=cursor, end=slot_end, conflicts=conflicts)
        if slot.conflict_count == 0:
            free.append(slot)
        elif slot.conflict_count == 1:
            partial.append(slot)
        cursor += delta
    return free, partial


# ── Search window resolution ──────────────────────────────────────────────────

def _next_weekdays(n: int = 5) -> list[date]:
    today = date.today()
    days_to_monday = (7 - today.weekday()) % 7 or 7
    monday = today + timedelta(days=days_to_monday)
    return [monday + timedelta(days=i) for i in range(n)]


def _resolve_search_days(intent: dict) -> tuple[list[date], int, int, str]:
    """Map email-extracted (start_window, end_window) -> (days, start_hour, end_hour, human_label)."""
    start = intent.get("start_window")
    end   = intent.get("end_window")
    if start:
        try:
            start_d = date.fromisoformat(str(start)[:10])
            end_d   = date.fromisoformat(str(end)[:10]) if end else start_d
            try:    start_h = datetime.fromisoformat(str(start)).hour
            except Exception: start_h = 9
            try:    end_h = datetime.fromisoformat(str(end)).hour if end else 17
            except Exception: end_h = 17
            days, cur = [], start_d
            while cur <= end_d:
                if cur.weekday() < 5:  # Mon-Fri
                    days.append(cur)
                cur += timedelta(days=1)
            if days:
                label = (f"{days[0].strftime('%a %b %-d')} {start_h:02d}:00–{end_h:02d}:00"
                         if len(days) == 1
                         else f"{days[0].strftime('%b %-d')}–{days[-1].strftime('%b %-d')} {start_h:02d}:00–{end_h:02d}:00")
                return days, start_h, end_h, label
        except Exception:
            pass
    days = _next_weekdays()
    label = f"next week ({days[0].strftime('%b %-d')}–{days[-1].strftime('%b %-d')}) 09:00–17:00"
    return days, 9, 17, label


# ── Public surface ────────────────────────────────────────────────────────────

def candidate_slots_for_meeting(
    intent: dict,
    sender: str | None = None,
    receiver: str | None = None,
) -> tuple[list[dict], dict]:
    """
    Multi-participant slot finding (matches src/calendar_cod.py logic).

    Returns (slots, debug_info) where:
      slots — list of slot dicts with start/end/label/conflict_count/conflicts
      debug_info — {participants_resolved, participants_with_tokens, search_label, free_count, partial_count}
    """
    import os as _os

    receiver = (receiver or _os.environ.get("ROCKETRIDE_GOOGLE_USER_EMAIL", "")).strip().lower()

    # 1. Build the participant set: sender + receiver + attendees from email_classify
    participants: set[str] = set()
    if receiver:
        participants.add(receiver)
    if sender and "@" in sender:
        participants.add(sender.strip().lower())
    for a in (intent.get("attendees") or []):
        if isinstance(a, str) and "@" in a:
            participants.add(a.strip().lower())

    # 2. Match against the available token map (same as src CALENDAR_TOKENS_JSON pattern)
    token_map = participant_tokens_from_env()
    matched = {p: token_map[p] for p in participants if p in token_map}

    debug = {
        "participants_resolved":     sorted(participants),
        "participants_with_tokens":  sorted(matched.keys()),
        "search_label":              "",
        "free_count":                0,
        "partial_count":             0,
    }

    if not matched:
        logger.warning("slot_finder: no participant tokens available for %s", sorted(participants))
        return [], debug

    if len(matched) < len(participants):
        logger.info(
            "slot_finder: %d/%d participants have calendar tokens — others contribute no constraints",
            len(matched), len(participants),
        )

    # 3. Resolve search window from extracted intent
    days, start_h, end_h, label = _resolve_search_days(intent)
    debug["search_label"] = label
    fetch_min = datetime(days[0].year,  days[0].month,  days[0].day,  start_h, tzinfo=PDT_TZ).isoformat()
    fetch_max = datetime(days[-1].year, days[-1].month, days[-1].day, end_h,   tzinfo=PDT_TZ).isoformat()

    # 4. Fetch each participant's calendar events for the window
    events_by: dict[str, list[dict]] = {}
    for email, refresh_tok in matched.items():
        try:
            access = get_access_token(refresh_tok)
            events_by[email] = _fetch_events(access, fetch_min, fetch_max)
            logger.info("slot_finder: %s — %d event(s) in window", email, len(events_by[email]))
        except Exception as e:
            logger.warning("slot_finder: events fetch failed for %s: %s", email, e)

    if not events_by:
        return [], debug

    # 5. Walk slots day by day, classify free vs single-conflict
    free_all: list[Slot] = []
    partial_all: list[Slot] = []
    for d in days:
        per_day = {p: _filter_events_for_day(evs, d) for p, evs in events_by.items()}
        free_d, partial_d = _enumerate_slots(per_day, d, start_hour=start_h, end_hour=end_h)
        free_all.extend(free_d)
        partial_all.extend(partial_d)

    debug["free_count"]    = len(free_all)
    debug["partial_count"] = len(partial_all)
    logger.info("slot_finder: window=%s free=%d partial=%d", label, len(free_all), len(partial_all))

    # 6. Tier selection — exact same priority as src/calendar_cod.py slot_cod
    if free_all:
        return [s.to_dict() for s in free_all], debug
    displaceable = [s for s in partial_all if s.conflicts and s.conflicts[0]["is_displaceable"]]
    if displaceable:
        return [s.to_dict() for s in displaceable], debug
    return [s.to_dict() for s in partial_all], debug


# Back-compat shim for the old single-arg API used by app.py /dev/email
def candidate_slots_for_email(intent: dict, calendars: list[str] | None = None) -> list[dict]:  # noqa: ARG001
    """Legacy single-receiver wrapper. New callers should use candidate_slots_for_meeting()."""
    slots, _ = candidate_slots_for_meeting(intent)
    return slots
