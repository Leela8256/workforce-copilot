"""
app.py — Workforce Copilot Slack-native backend.

Three demo paths, all driven by Slack + Gmail (no web UI):

  1. Slack -> Jira     (@-mention with a task description -> ticket created)
  2. Slack -> Q&A      (@-mention starting with "ask:" -> RAG over past tickets)
  3. Email -> Calendar (Gmail poller -> Slack card with 3 slot buttons -> click -> event)

Pipelines pre-start at boot so every Slack/email event has a hot path.

Run: .venv/bin/python app.py
"""
import asyncio
import json
import logging
import os
import re
import ssl
import threading
from pathlib import Path

import certifi
# Python.org Python 3.13 on macOS doesn't trust system roots — point urllib + ssl
# at certifi's CA bundle so slack-sdk's HTTPS calls don't fail with
# CERTIFICATE_VERIFY_FAILED.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
ssl._create_default_https_context = lambda *a, **kw: ssl.create_default_context(cafile=certifi.where())

from dotenv import load_dotenv
# Load .env BEFORE importing modules that read env vars at module-import time
# (gmail_poller reads SLACK_BOT_TOKEN, SELF_EMAIL, etc. at import).
load_dotenv(Path(__file__).parent / ".env")

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from rocketride_client import RocketRideHelper
from google_auth import get_access_token
import gmail_poller
import pending_meetings

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("workforce_copilot")

SLACK_BOT_TOKEN      = os.environ["ROCKETRIDE_SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN      = os.environ["ROCKETRIDE_SLACK_APP_TOKEN"]
SLACK_SIGNING_SECRET = os.environ.get("ROCKETRIDE_SLACK_SIGNING_SECRET", "")

# ── async event loop owned by the app (one shared loop for RR calls) ─────────
_loop = asyncio.new_event_loop()
threading.Thread(target=_loop.run_forever, daemon=True).start()


def _run(coro):
    """Run an async coroutine on the shared loop and block for the result."""
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()


# ── RocketRide pipelines (started once at boot) ──────────────────────────────
PIPELINES = {
    "slack_to_jira":   "pipelines/slack_to_jira_agent.pipe",      # agent + tool_http_request -> Jira
    "email_extract":   "pipelines/email_extract_intent.pipe",     # structured-extract LLM
    "calendar_create": "pipelines/calendar_create_agent.pipe",    # agent + tool_http_request -> Calendar
    "cod_meeting":     "pipelines/cod_meeting_fanout.pipe",       # 3 parallel agents (Pattern 8)
    "team_qa":         "pipelines/team_qa_rag.pipe",              # chat -> emb -> qdrant -> prompt -> llm
}

rr = RocketRideHelper()


def _bootstrap_rocketride():
    logger.info("starting RocketRide pipelines: %s", list(PIPELINES.keys()))
    _run(rr.start(PIPELINES))
    logger.info("rocketride pipelines ready")


def _sync_ask(alias: str, **kwargs):
    """Sync shim around the async wrapper, for use by the Gmail poller thread."""
    return _run(rr.ask(alias, **kwargs))


# ── Helpers ──────────────────────────────────────────────────────────────────

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*", re.IGNORECASE)
_ASK_PREFIX_RE = re.compile(r"^(ask|q|question)\s*[:\-]\s*", re.IGNORECASE)


def _strip_mention(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


def _parse_answer(answer):
    """Pipeline answers come back as either a JSON string, a Python-repr dict
    string, or a dict — normalise to dict."""
    if isinstance(answer, str):
        try:
            return json.loads(answer)
        except json.JSONDecodeError:
            pass
        # Some agents return Python-style dict repr (single quotes) — try ast
        try:
            import ast
            v = ast.literal_eval(answer)
            if isinstance(v, dict):
                return v
        except Exception:
            pass
        return {"action": "raw", "text": answer}
    return answer or {}


def _format_jira_reply(parsed: dict) -> str:
    action = (parsed or {}).get("action")
    if action == "created":
        return (
            f"✅ Created <{parsed.get('url')}|{parsed.get('key')}> — "
            f"*{parsed.get('summary')}*\n"
            f"_assignee hint: {parsed.get('assignee_hint','-')}_"
        )
    if action == "none":
        return f"ℹ️ Nothing to file: {parsed.get('reason','no actionable task')}"
    if action == "failed":
        return (
            f"⚠️ Couldn't create ticket: {parsed.get('error','unknown')} "
            f"(status {parsed.get('http_status','?')})"
        )
    return f"```{json.dumps(parsed, indent=2, default=str)}```"


# ── Slack bolt (Socket Mode) ─────────────────────────────────────────────────
slack_app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)


@slack_app.event("app_mention")
def handle_mention(event, say, client, logger):  # noqa: ARG001 — bolt provides these
    user_text = _strip_mention(event.get("text", ""))
    thread_ts = event.get("thread_ts") or event.get("ts")
    logger.info("app_mention from user=%s text=%r", event.get("user"), user_text)

    if not user_text:
        say(text=("Mention me with a task or a question:\n"
                  "• `@Workforce Copilot fix the safari login bug` (creates a Jira ticket)\n"
                  "• `@Workforce Copilot ask: have we hit a Safari issue before?` (RAG over past tickets)"),
            thread_ts=thread_ts)
        return

    # Dispatch:
    #   - "ask:/q:/question:" prefix      -> RAG
    #   - text ending in "?"              -> RAG (natural questions)
    #   - else                             -> ticket creation
    ask_match = _ASK_PREFIX_RE.match(user_text)
    is_question = bool(ask_match) or user_text.rstrip().endswith("?")
    if is_question:
        question = user_text[ask_match.end():].strip() if ask_match else user_text.strip()
        if not question:
            say(text="Ask me a question, e.g. `have we hit a Safari issue before?`",
                thread_ts=thread_ts)
            return
        logger.info("routing to team_qa: %r", question)
        say(text=":mag: searching past tickets...", thread_ts=thread_ts)
        try:
            answer = _run(rr.ask("team_qa", question=question, expect_json=False))
            logger.info("team_qa answered (%d chars)", len(answer) if isinstance(answer, str) else -1)
            text = answer if isinstance(answer, str) else json.dumps(answer, indent=2, default=str)
        except Exception as e:  # pragma: no cover
            logger.exception("team_qa pipeline call failed")
            text = f"⚠️ RAG error: `{e}`"
        say(text=text, thread_ts=thread_ts)
        logger.info("team_qa Slack reply sent")
        return

    # Default: create-ticket flow
    say(text=":hourglass_flowing_sand: working...", thread_ts=thread_ts)
    try:
        answer = _run(rr.ask("slack_to_jira", question=user_text))
        parsed = _parse_answer(answer)
        reply = _format_jira_reply(parsed)
    except Exception as e:  # pragma: no cover
        logger.exception("slack_to_jira pipeline call failed")
        reply = f"⚠️ Pipeline error: `{e}`"

    say(text=reply, thread_ts=thread_ts)


# ── Email→Calendar HITL: handlers for the 3 slot buttons + cancel ─────────────

def _on_slot_pick(slot_index: int, body: dict, say, client, logger):
    raw_value = body["actions"][0].get("value", "") or ""
    # button value format: "<sid>|<index>"
    sid = raw_value.split("|", 1)[0]
    meeting = pending_meetings.load(sid)
    channel = body["channel"]["id"]
    ts = body["container"]["message_ts"]
    if not meeting:
        client.chat_update(channel=channel, ts=ts,
                           text="⚠️ This meeting proposal expired. Send the email again.",
                           blocks=None)
        return
    if slot_index >= len(meeting.top_slots):
        say(":warning: invalid slot index")
        return

    chosen = meeting.top_slots[slot_index]
    logger.info("user picked slot %d for sid=%s start=%s", slot_index, sid, chosen.get("start"))
    client.chat_update(
        channel=channel, ts=ts,
        text=f":hourglass_flowing_sand: creating Calendar event for slot {slot_index+1}...",
        blocks=None,
    )

    cal_payload = json.dumps({
        "bearer_token": get_access_token(),
        "title":        meeting.intent.get("title") or meeting.subject or "Meeting",
        "start":        chosen["start"],
        "end":          chosen["end"],
        "attendees":    meeting.attendees,
        "description":  f"Auto-scheduled by Workforce Copilot from email. Original sender: {meeting.sender}.",
    })

    try:
        cal_raw = _run(rr.ask("calendar_create", question=cal_payload))
        logger.info("calendar_create raw response: %r", cal_raw)
        cal = _parse_answer(cal_raw)
        logger.info("calendar_create parsed: %r", cal)
    except Exception as e:
        logger.exception("calendar_create failed")
        cal = {"action": "failed", "error": str(e)}

    if cal.get("action") == "created":
        link = cal.get("html_link", "(no link)")
        title = cal.get("title", meeting.subject or "Meeting")
        msg = (
            f"✅ *Calendar event created*\n"
            f"_From:_ {meeting.sender}\n"
            f"_Subject:_ {meeting.subject}\n"
            f"_Slot:_ {chosen['start']}\n"
            f"<{link}|{title}>"
        )
    else:
        msg = (
            f"⚠️ *Calendar create failed*\n"
            f"Reason: `{cal.get('error','unknown')}` (status {cal.get('http_status','?')})"
        )

    client.chat_update(channel=channel, ts=ts, text=msg, blocks=None)
    pending_meetings.remove(sid)


@slack_app.action("select_slot_0")
def handle_slot_0(ack, body, say, client, logger):
    ack()
    _on_slot_pick(0, body, say, client, logger)


@slack_app.action("select_slot_1")
def handle_slot_1(ack, body, say, client, logger):
    ack()
    _on_slot_pick(1, body, say, client, logger)


@slack_app.action("select_slot_2")
def handle_slot_2(ack, body, say, client, logger):
    ack()
    _on_slot_pick(2, body, say, client, logger)


@slack_app.action("cancel_meeting")
def handle_cancel_meeting(ack, body, say, client, logger):  # noqa: ARG001
    ack()
    sid = body["actions"][0].get("value", "")
    pending_meetings.remove(sid)
    channel = body["channel"]["id"]
    ts = body["container"]["message_ts"]
    client.chat_update(channel=channel, ts=ts,
                       text="❌ Meeting proposal cancelled.",
                       blocks=None)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    _bootstrap_rocketride()

    # Background thread: poll Gmail and trigger the email->calendar HITL flow
    gmail_poller.start(_sync_ask)
    logger.info("Gmail poller started (interval=%ds)", gmail_poller.POLL_INTERVAL)

    logger.info("connecting Slack Socket Mode...")
    SocketModeHandler(slack_app, SLACK_APP_TOKEN).start()


if __name__ == "__main__":
    main()
