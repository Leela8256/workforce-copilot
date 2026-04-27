"""
gmail_poller.py — background poller that processes new Gmail INBOX messages.

Replaces the original Pub/Sub watcher. Every POLL_INTERVAL seconds it asks
Gmail "what's new since the last historyId I saw?" via users.history.list,
fetches each new INBOX message, runs it through the Email→Calendar pipeline
(email_extract → slot_finder → cod_meeting_fanout → calendar_create_agent),
and posts the result to Slack.

State lives in .gmail_state.json (just the last historyId). Processed messages
are added to a Gmail label so we don't double-process across restarts.

Designed to run inside app.py as a daemon thread.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

import requests
from slack_sdk import WebClient

from google_auth import get_access_token
from slot_finder import candidate_slots_for_meeting
import pending_meetings

logger = logging.getLogger("gmail_poller")

POLL_INTERVAL = 30                                            # seconds
STATE_PATH    = Path(__file__).parent / ".gmail_state.json"
SELF_EMAIL    = (os.environ.get("ROCKETRIDE_GOOGLE_USER_EMAIL") or "").lower()
SLACK_CHANNEL = os.environ.get("ROCKETRIDE_SLACK_NOTIFY_CHANNEL") or "#all-rocketridedemo"
SLACK_BOT_TOKEN = os.environ.get("ROCKETRIDE_SLACK_BOT_TOKEN", "")


# ── State persistence ────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ── Gmail helpers ────────────────────────────────────────────────────────────

def _gmail_get(path: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"https://gmail.googleapis.com/gmail/v1{path}",
        headers={"Authorization": f"Bearer {get_access_token()}"},
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _gmail_post(path: str, body: dict) -> dict:
    resp = requests.post(
        f"https://gmail.googleapis.com/gmail/v1{path}",
        headers={
            "Authorization": f"Bearer {get_access_token()}",
            "Content-Type":  "application/json",
        },
        json=body,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def _current_history_id() -> str:
    return str(_gmail_get("/users/me/profile")["historyId"])


def _list_new_messages(start_history_id: str) -> tuple[list[str], str]:
    """Return (new_message_ids, latest_history_id)."""
    try:
        out = _gmail_get(
            "/users/me/history",
            params={
                "startHistoryId": start_history_id,
                "historyTypes":   "messageAdded",
                "labelId":        "INBOX",
            },
        )
    except requests.HTTPError as e:
        # 404 means startHistoryId is too old — reset to current
        if getattr(e.response, "status_code", None) == 404:
            logger.warning("history 404 — resetting to current historyId")
            return [], _current_history_id()
        raise

    msg_ids: list[str] = []
    for record in out.get("history", []):
        for ma in record.get("messagesAdded", []):
            mid = ma.get("message", {}).get("id")
            label_ids = ma.get("message", {}).get("labelIds") or []
            if mid and "INBOX" in label_ids:
                msg_ids.append(mid)
    latest = str(out.get("historyId") or start_history_id)
    return msg_ids, latest


def _decode_part(part: dict) -> str:
    if part.get("mimeType") == "text/plain":
        data = part.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for sub in part.get("parts", []) or []:
        text = _decode_part(sub)
        if text:
            return text
    return ""


def _fetch_message(msg_id: str) -> dict:
    raw = _gmail_get(f"/users/me/messages/{msg_id}", params={"format": "full"})
    headers = {h["name"].lower(): h["value"] for h in raw["payload"]["headers"]}

    def _addr(s: str) -> str:
        if "<" in s and ">" in s:
            return s.split("<", 1)[1].split(">", 1)[0].strip().lower()
        return s.strip().lower()

    def _addrs(s: str) -> list[str]:
        return [_addr(x) for x in s.split(",") if x.strip()]

    return {
        "id":         msg_id,
        "thread_id":  raw["threadId"],
        "subject":    headers.get("subject", ""),
        "from_email": _addr(headers.get("from", "")),
        "to_emails":  _addrs(headers.get("to", "")),
        "cc_emails":  _addrs(headers.get("cc", "")),
        "date":       headers.get("date", ""),
        "body":       _decode_part(raw["payload"])[:4000],
        "snippet":    raw.get("snippet", ""),
        "label_ids":  raw.get("labelIds", []),
    }


def _mark_read(msg_id: str) -> None:
    try:
        _gmail_post(
            f"/users/me/messages/{msg_id}/modify",
            {"removeLabelIds": ["UNREAD"]},
        )
    except Exception as e:
        logger.warning("mark_read failed for %s: %s", msg_id, e)


# ── Slack helper ─────────────────────────────────────────────────────────────

_slack: WebClient | None = None


def _slack_client() -> WebClient | None:
    global _slack
    if _slack is None and SLACK_BOT_TOKEN:
        _slack = WebClient(token=SLACK_BOT_TOKEN)
    return _slack


def _slack_post(text: str, blocks: list | None = None) -> dict | None:
    client = _slack_client()
    if not client:
        logger.warning("Slack token missing — skipping post")
        return None
    try:
        resp = client.chat_postMessage(channel=SLACK_CHANNEL, text=text, blocks=blocks)
        return resp.data
    except Exception as e:
        logger.warning("slack post failed: %s", e)
        return None


def _format_slot_label(start_iso: str) -> str:
    from dateutil.parser import parse as dp
    try:
        dt = dp(start_iso)
        return dt.strftime("%a %b %-d, %-I:%M %p")
    except Exception:
        return start_iso


def _build_slot_card(meeting: pending_meetings.PendingMeeting, sid: str) -> tuple[str, list[dict]]:
    """Block Kit card matching the team's standard format:
       📧 header / Title / Location / Attendees / 'CoD proposed the following slots — pick one:'
       / 3 numbered slots with reasons + Slot buttons / Cancel.
    """
    title    = meeting.intent.get("title") or meeting.subject or "Meeting"
    location = meeting.intent.get("location") or "N/A"
    attendees = ", ".join(meeting.attendees) if meeting.attendees else meeting.sender

    header_text = (
        f"📧 *Meeting detected in incoming email*\n"
        f"*Title:*       {title}\n"
        f"*Location:*  {location}\n"
        f"*Attendees:* {attendees}\n\n"
        f"*CoD proposed the following slots — pick one:*"
    )

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
    ]

    # Numbered slot rows with reasons + button accessories
    for i, slot in enumerate(meeting.top_slots[:3]):
        label  = _format_slot_label(slot.get("start", ""))
        reason = slot.get("reason", "")
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"*#{i+1}* — {label}  _{reason}_"},
            "accessory": {
                "type":      "button",
                "text":      {"type": "plain_text", "text": f"✅ Slot {i+1}"},
                "style":     "primary",
                "value":     f"{sid}|{i}",
                "action_id": f"select_slot_{i}",
            },
        })

    # Cancel button
    blocks.append({
        "type": "actions",
        "elements": [{
            "type":      "button",
            "text":      {"type": "plain_text", "text": "❌ Cancel"},
            "style":     "danger",
            "value":     sid,
            "action_id": "cancel_meeting",
        }],
    })

    fallback = f"Meeting detected from {meeting.sender}: pick a slot"
    return fallback, blocks


# ── Pipeline pull-through ────────────────────────────────────────────────────

def _process_message(msg: dict, ask: Callable[..., dict]) -> None:
    """Run one Gmail message through the email→calendar pipeline chain."""
    sender = msg["from_email"]
    if sender == SELF_EMAIL:
        logger.info("skip self-sent message %s", msg["id"])
        return

    text_for_extract = (
        f"From: {msg['from_email']}\n"
        f"Subject: {msg['subject']}\n"
        f"To: {', '.join(msg['to_emails'])}\n\n"
        f"{msg['body']}"
    )
    logger.info("processing email id=%s subject=%r", msg["id"], msg["subject"][:60])

    # 1. email_extract_intent
    intent = ask(
        "email_extract",
        question=text_for_extract,
        instructions=[
            ("Task", "Extract meeting intent from this email and return strict JSON."),
            ("Output", "Return {\"is_meeting\":bool,\"title\":str|null,\"attendees\":[email],"
                       "\"start_window\":\"YYYY-MM-DDTHH:MM:SS-07:00\"|null,"
                       "\"end_window\":\"YYYY-MM-DDTHH:MM:SS-07:00\"|null,"
                       "\"time_preference\":str|null} only — no prose."),
        ],
    )
    if not isinstance(intent, dict):
        try:
            intent = json.loads(intent) if isinstance(intent, str) else {}
        except Exception:
            intent = {}

    if not intent.get("is_meeting"):
        logger.info("not a meeting (id=%s) — skipping", msg["id"])
        return

    # 2. Multi-participant slot finding (sender + receiver + email attendees)
    #    Matches src/calendar_cod.py — checks every participant we have a token for.
    slots, slot_debug = candidate_slots_for_meeting(
        intent,
        sender=sender,
        receiver=SELF_EMAIL,
    )
    logger.info(
        "slot_finder debug: participants_resolved=%s with_tokens=%s window=%s free=%d partial=%d",
        slot_debug["participants_resolved"],
        slot_debug["participants_with_tokens"],
        slot_debug["search_label"],
        slot_debug["free_count"],
        slot_debug["partial_count"],
    )
    if not slots:
        _slack_post(
            f"📧 Got meeting email from {sender} (subject: _{msg['subject']}_) — "
            f"no available slots in window `{slot_debug['search_label']}` "
            f"(participants checked: {', '.join(slot_debug['participants_with_tokens']) or 'none'})."
        )
        return

    # 3. cod_meeting_fanout — 3 agents in parallel (Proposer + Challenger + Judge)
    cod_payload = json.dumps({
        "email_context":   {**intent, "sender": sender, "body": msg["body"][:600]},
        "candidate_slots": slots[:12],
    }, default=str)
    cod_raw = ask("cod_meeting", question=cod_payload, expect_json=False)

    answers = cod_raw if isinstance(cod_raw, list) else [cod_raw]
    parsed_verdicts: list[dict] = []
    judge_top_slots: list[dict] = []
    for a in answers:
        try:
            p = json.loads(a) if isinstance(a, str) else a
        except Exception:
            continue
        if isinstance(p, dict):
            parsed_verdicts.append(p)
            if p.get("role") == "judge" and p.get("top_slots"):
                judge_top_slots = p["top_slots"][:3]

    # Fallback if Judge didn't return: combine Proposer's pick + Challenger's counter + first 2 free slots
    if not judge_top_slots:
        seen_starts: set[str] = set()
        for p in parsed_verdicts:
            cand = p.get("proposed_slot") or p.get("counter_slot")
            if cand and cand.get("start") not in seen_starts:
                judge_top_slots.append({"start": cand["start"], "end": cand["end"], "reason": p.get("argument", "")})
                seen_starts.add(cand["start"])
        for s in slots[:3]:
            if s["start"] not in seen_starts and len(judge_top_slots) < 3:
                judge_top_slots.append({"start": s["start"], "end": s["end"], "reason": "free slot"})
                seen_starts.add(s["start"])

    if not judge_top_slots:
        logger.warning("CoD returned no usable slots — skipping (id=%s)", msg["id"])
        return

    # 4. Save pending meeting + post Slack card with 3 slot buttons (HITL)
    #    Display all resolved participants (sender + receiver + email attendees) on the card.
    display_attendees = slot_debug.get("participants_resolved") or (intent.get("attendees") or [])
    meeting = pending_meetings.PendingMeeting(
        sender=sender,
        subject=msg["subject"],
        attendees=display_attendees,
        top_slots=judge_top_slots,
        cod_verdicts=parsed_verdicts,
        intent=intent,
    )
    sid = pending_meetings.save(meeting)
    fallback, blocks = _build_slot_card(meeting, sid)
    resp = _slack_post(fallback, blocks=blocks)
    if resp:
        # Stash the message timestamp + channel so the action handler can update the card
        pending_meetings.update(
            sid,
            channel_id=resp.get("channel"),
            message_ts=resp.get("ts"),
        )
        logger.info("posted slot card for sid=%s (Slack ts=%s)", sid, resp.get("ts"))


# ── Poll loop ────────────────────────────────────────────────────────────────

def _poll_loop(ask: Callable[..., dict], stop_event: threading.Event) -> None:
    state = _load_state()
    last  = state.get("history_id") or _current_history_id()
    logger.info("gmail poller starting from historyId=%s (interval=%ds)", last, POLL_INTERVAL)

    while not stop_event.is_set():
        try:
            new_ids, latest = _list_new_messages(last)
            if new_ids:
                logger.info("found %d new INBOX message(s)", len(new_ids))
                for mid in new_ids:
                    try:
                        msg = _fetch_message(mid)
                        _process_message(msg, ask)
                        _mark_read(mid)
                    except Exception as e:
                        logger.exception("processing %s failed: %s", mid, e)
            last = latest
            _save_state({"history_id": last, "updated_at": time.time()})
        except Exception as e:
            logger.exception("poll error: %s", e)

        stop_event.wait(POLL_INTERVAL)


def start(ask: Callable[..., dict]) -> threading.Event:
    """Start the poller as a daemon thread. Returns the stop_event so app.py can shut it down."""
    stop_event = threading.Event()
    threading.Thread(
        target=_poll_loop,
        args=(ask, stop_event),
        daemon=True,
        name="gmail-poller",
    ).start()
    return stop_event
