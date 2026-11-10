"""Persistent Agent SDK session owned by Donna's delivery worker."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Any

VOICE_ID_RE = re.compile(r"<voice-command-id>([^<]+)</voice-command-id>")

# _drain() sentinel for a message interrupted by the next submission before
# it ever produced a ResultMessage - without this, such a message reaches
# "completed" in the queue (submit() unblocks on the same _completed event
# either way) with no reply_text entry at all, and delivery_worker.py's
# `if reply:` check silently skips writing a reply-ready file. The message
# just vanishes: no file, no notification, no way to tell "no answer" apart
# from "still working" (hit live: a multi-tool-call investigation got cut
# off mid-turn by the next voice command and its answer never surfaced).
INTERRUPTED_SENTINEL = "[interrupted by a later voice command before finishing]"


class ClaudeSessionBridge:
    """Serialize, interrupt, drain, and submit exchanges to one SDK session.

    Donna intentionally owns this session. Stock Claude Code has no supported
    IPC for injecting a user prompt mid-turn, so it is not a compatible owner.
    """

    agent_id = "claude"

    def __init__(self, session_id: str, cwd: Path | None = None) -> None:
        if not session_id.strip():
            raise ValueError("an explicit target session ID is required")
        self.session_id = session_id
        self.cwd = cwd or Path.cwd()
        self.client: Any = None
        self._drain_task: asyncio.Task[None] | None = None
        self._accepted: set[str] = set()
        self._completed: dict[str, asyncio.Event] = {}
        self._reply_text: dict[str, str] = {}
        self._active_message: str | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
        except ImportError as error:
            raise RuntimeError(
                "install the optional Agent SDK: uv add claude-agent-sdk"
            ) from error
        options = ClaudeAgentOptions(
            cwd=str(self.cwd),
            resume=self.session_id,
            permission_mode=os.environ.get("DONNA_CLAUDE_PERMISSION_MODE", "default"),
        )
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()

    async def accepted_ids(self) -> set[str]:
        return set(self._accepted)

    async def _drain(self, expected_id: str) -> None:
        got_result = False
        async for event in self.client.receive_response():
            event_session = getattr(event, "session_id", None)
            if event_session is not None and event_session != self.session_id:
                raise RuntimeError(
                    f"SDK session mismatch: expected {self.session_id}, got {event_session}"
                )
            # query() returning means the input was accepted. Stream draining
            # remains mandatory before another query after interruption.
            if event.__class__.__name__ == "ResultMessage":
                got_result = True
                # ResultMessage.result is the SDK's own final-answer text -
                # no need to accumulate AssistantMessage text blocks by hand.
                result_text = getattr(event, "result", None)
                if result_text:
                    self._reply_text[expected_id] = result_text
                self._completed.setdefault(expected_id, asyncio.Event()).set()
        if not got_result:
            self._reply_text[expected_id] = INTERRUPTED_SENTINEL
        self._completed.setdefault(expected_id, asyncio.Event()).set()

    async def reply_text(self, message_id: str) -> str | None:
        """Final-answer text for a completed message, consumed once."""
        return self._reply_text.pop(message_id, None)

    async def submit(self, message_id: str, text: str) -> None:
        if self.client is None:
            raise RuntimeError("bridge is not connected")
        async with self._lock:
            if self._drain_task and not self._drain_task.done():
                await self.client.interrupt()
                await self._drain_task
            prompt = f"<voice-command-id>{message_id}</voice-command-id>\n{text}"
            await self.client.query(prompt, session_id=self.session_id)
            self._accepted.add(message_id)
            self._active_message = message_id
            self._completed[message_id] = asyncio.Event()
            self._drain_task = asyncio.create_task(self._drain(message_id))

    async def wait_completed(self, message_id: str) -> None:
        await self._completed[message_id].wait()

    async def close(self) -> None:
        if self._drain_task:
            with suppress(Exception):
                await self._drain_task
        if self.client is not None:
            await self.client.disconnect()
