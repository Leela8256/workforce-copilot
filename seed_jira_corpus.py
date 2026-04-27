"""
seed_jira_corpus.py — generate a synthetic past-tickets corpus and ingest it
into the qdrant `jira_history` collection via the RocketRide ingest pipeline.

Run AFTER `python3 app.py` is up (pipelines started). This script reuses the
running ingest_jira_history pipeline rather than spinning up a second engine
slot.

Usage: .venv/bin/python seed_jira_corpus.py
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from rocketride import RocketRideClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed")

CORPUS = [
    ("KAN-101", "Safari login button unresponsive after refresh — frontend",
     "Reproduced on Safari 16.5. Root cause: button onClick handler racing with state hydration. "
     "Fixed by deferring the binding to useEffect and adding a key prop. Owner: ketki. Closed."),
    ("KAN-102", "Dashboard slow to load on Mondays — backend",
     "Spike on Monday morning traffic. Cause: N+1 query in ProjectListService. Fixed by batching "
     "the lookup with `prefetch_related`. Latency dropped from 4.2s p95 to 380ms. Owner: leela."),
    ("KAN-103", "OAuth callback fails for new Google clients — auth",
     "Refresh tokens missing for new OAuth clients. Fixed by passing access_type=offline + "
     "prompt=consent in the auth URL. Documented in /docs/google_oauth.md. Owner: aishanee."),
    ("KAN-104", "Webhook delivery drops under burst — infra",
     "20% of Slack webhooks dropped under > 50 req/s burst. Fixed by adding a Redis-backed retry "
     "queue with exponential backoff. Owner: bob."),
    ("KAN-105", "Calendar event timezone mis-stored — backend",
     "Events booked at 2pm PDT showed as 9pm UTC in queries. Cause: naive datetimes saved without "
     "tz info. Fixed with explicit zoneinfo on save. Owner: leela."),
    ("KAN-106", "PII leak in logging — security/compliance",
     "Email addresses appearing in stdout logs. Fixed by adding a logging filter that anonymizes "
     "via the same regex set used by the anonymize_text component. Owner: ketki."),
    ("KAN-107", "Duplicate Jira tickets created on retry — agent bug",
     "Slack interactivity retries caused duplicate tickets. Fixed by checking the X-Slack-Retry-Num "
     "header in parse_input. Owner: aishanee."),
    ("KAN-108", "Meeting summarizer chunking drops decisions — ML pipeline",
     "Decisions made in chunk N-1 vanished from the merged summary. Fixed by carrying forward "
     "decisions across chunks, not just abstracts. Owner: bob."),
    ("KAN-109", "ChromaDB writes 30% slower after upgrade — RLHF",
     "0.4 -> 0.5 upgrade. Cause: collection metadata reload on every add(). Fixed by caching the "
     "collection handle. Owner: leela."),
    ("KAN-110", "Slack thread context lost across button clicks — UX",
     "Button payload exceeded 3KB Slack limit. Migrated to Redis session lookup keyed by UUID. "
     "Owner: ketki."),
    ("KAN-111", "Lambda cold start over 8s — infra",
     "Cold start regressed after pinning numpy<2.0. Cause: large wheel reload. Fixed by removing "
     "unused onnxruntime import path. Owner: bob."),
    ("KAN-112", "Jira description rejected as invalid — integration",
     "Atlassian API rejected ADF docs missing version field. Fixed by always wrapping description "
     "in {type:doc, version:1, content:...}. Owner: aishanee."),
    ("KAN-113", "vLLM LoRA hot-swap requires restart — ML",
     "Hot-swap was restarting the engine. Investigated `--enable-lora --max-loras 4`. Now swaps "
     "live without disconnect. Owner: leela."),
    ("KAN-114", "Misclassified emails as meeting requests — model",
     "Status update emails were being booked as meetings. Added explicit no_action examples to the "
     "extraction prompt. Precision up to 0.94. Owner: ketki."),
    ("KAN-115", "Calendar attendee invites not sent — Google API",
     "Events created but attendees got no email. Cause: missing sendUpdates=all query param. "
     "Owner: aishanee."),
    ("KAN-116", "DPO training collapses to single response — RLHF",
     "Cause: insufficient diversity in rejected samples. Fixed by stratified sampling across "
     "rejection reasons. Owner: bob."),
    ("KAN-117", "Slack bot replies mention raw bot ID — UX",
     "User saw <@U123> in replies. Fixed by stripping the mention via regex before extraction. "
     "Owner: ketki."),
    ("KAN-118", "Memory leak in RocketRide pipeline — engine",
     "Long-running pipelines leaked ~50MB/hr. Cause: not closing pipe handles. Mitigated by adding "
     "an explicit close() in the wrapper. Owner: leela."),
    ("KAN-119", "Embedding API rate-limit on bulk ingest — RAG",
     "OpenAI 429s during 10K-doc ingest. Fixed with concurrency=4 + exponential backoff. Owner: bob."),
    ("KAN-120", "Slot picker prefers afternoons even when email says morning — CoD",
     "Proposer agent default-bias toward 2pm. Fixed by re-ordering instruction priority: email "
     "preferences > mid-morning default. Owner: aishanee."),
]


def _doc_text(key: str, summary: str, body: str) -> str:
    return f"[{key}] {summary}\n\n{body}"


async def main() -> int:
    pipe_path = str(Path(__file__).parent / "pipelines" / "ingest_jira_history.pipe")
    client = RocketRideClient()
    await client.connect()
    try:
        try:
            result = await client.use(filepath=pipe_path)
        except RuntimeError as e:
            if "already running" in str(e).lower():
                result = await client.use(filepath=pipe_path, use_existing=True)
            else:
                raise
        token = result["token"]
        logger.info("ingest pipeline ready (token=%s)", token[:12])

        for key, summary, body in CORPUS:
            text = _doc_text(key, summary, body)
            try:
                await client.send(
                    token,
                    text,
                    objinfo={"name": f"{key}.txt", "key": key, "summary": summary},
                    mimetype="text/plain",
                )
                logger.info("ingested %s: %s", key, summary[:50])
            except Exception as e:
                logger.warning("ingest %s failed: %s", key, e)
        logger.info("seeded %d tickets into qdrant `jira_history`", len(CORPUS))
    finally:
        await client.disconnect()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
