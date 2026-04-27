"""
rocketride_client.py — thin async wrapper around the RocketRide SDK.

Owns one connection to the local engine and a token per pre-started pipeline.
Every "thinking" step in the app goes through this wrapper so the rest of the
codebase never has to know about the SDK's surface.

Usage:
    rr = RocketRideHelper()
    await rr.start({
        "slack_to_jira": "pipelines/slack_to_jira_agent.pipe",
        "team_qa":       "pipelines/team_qa_rag.pipe",
    })
    answer = await rr.ask("slack_to_jira", question="...", context={...})
    await rr.close()
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

from rocketride import RocketRideClient
from rocketride.schema import Question

logger = logging.getLogger(__name__)


class RocketRideHelper:
    def __init__(self) -> None:
        self._client: RocketRideClient | None = None
        self._tokens: dict[str, str] = {}
        self._paths:  dict[str, str] = {}  # remember filepath per alias for restart

    async def _start_pipeline(self, alias: str, full_path: str) -> str:
        """Start (or reuse) a pipeline and return its token. ttl=0 disables idle timeout."""
        try:
            result = await self._client.use(filepath=full_path, ttl=0)
        except RuntimeError as e:
            if "already running" in str(e).lower():
                logger.info("rocketride: %s already running, reusing", alias)
                result = await self._client.use(filepath=full_path, use_existing=True, ttl=0)
            else:
                raise
        return result["token"]

    async def start(self, pipelines: dict[str, str]) -> None:
        """Connect once and start every pipeline by alias.

        Falls back to use_existing=True if the engine reports the pipeline is
        already running. Pipelines are started with ttl=0 (no idle timeout)
        so they survive long quiet periods between user actions.
        """
        self._client = RocketRideClient()
        await self._client.connect()
        for alias, path in pipelines.items():
            full = str(Path(path).resolve())
            self._paths[alias] = full
            self._tokens[alias] = await self._start_pipeline(alias, full)
            logger.info("rocketride: started %s (token=%s)", alias, self._tokens[alias])

    async def close(self) -> None:
        if self._client:
            await self._client.disconnect()
            self._client = None
            self._tokens.clear()

    async def ask(
        self,
        alias: str,
        question: str,
        *,
        expect_json: bool = True,
        instructions: Iterable[tuple[str, str]] | None = None,
        examples: Iterable[tuple[str, Any]] | None = None,
        context: Any | None = None,
        goal: str | None = None,
    ) -> Any:
        """Send a question to a pre-started pipeline and return the first answer."""
        if not self._client or alias not in self._tokens:
            raise RuntimeError(f"Pipeline '{alias}' not started. Call start() first.")

        q = Question(expectJson=expect_json)
        q.addQuestion(question)
        if goal:
            q.addGoal(goal)
        for subtitle, body in (instructions or []):
            q.addInstruction(subtitle, body)
        for given, result in (examples or []):
            q.addExample(given, result)
        if context is not None:
            if not isinstance(context, str):
                context = json.dumps(context, default=str)
            q.addContext(context)

        try:
            resp = await self._client.chat(token=self._tokens[alias], question=q)
        except RuntimeError as e:
            if "not running" in str(e).lower() and alias in self._paths:
                logger.warning("rocketride: %s pipeline gone (TTL?), restarting and retrying once", alias)
                self._tokens[alias] = await self._start_pipeline(alias, self._paths[alias])
                resp = await self._client.chat(token=self._tokens[alias], question=q)
            else:
                raise
        answers = resp.get("answers", []) if isinstance(resp, dict) else []
        if not answers:
            logger.warning("rocketride: empty answers for %s — full resp=%s", alias, resp)
            return None
        return answers[0]
