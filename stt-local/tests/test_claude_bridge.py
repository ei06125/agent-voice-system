import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_bridge import INTERRUPTED_SENTINEL, ClaudeSessionBridge


class ResultMessage:
    """Matches by class *name*, same as _drain()'s own dispatch - not the
    real SDK type, deliberately, so this test doesn't need the SDK's
    optional dependency installed to run."""

    def __init__(self, session_id: str, result: str) -> None:
        self.session_id = session_id
        self.result = result


class AssistantMessage:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


class FakeClient:
    def __init__(self, events: list) -> None:
        self._events = events

    async def receive_response(self):
        for event in self._events:
            yield event


def test_drain_captures_reply_text_on_normal_completion():
    async def scenario():
        bridge = ClaudeSessionBridge("session-x")
        bridge.client = FakeClient(
            [AssistantMessage("session-x"), ResultMessage("session-x", "the answer")]
        )
        await bridge._drain("msg-1")
        assert await bridge.reply_text("msg-1") == "the answer"
        # consumed once - a second read finds nothing left to pop
        assert await bridge.reply_text("msg-1") is None

    asyncio.run(scenario())


def test_drain_marks_interrupted_sentinel_when_no_result_message():
    """Regression: a turn cut off by the next submission before it ever
    produces a ResultMessage must not vanish silently. Before this fix,
    reply_text() returned None here - indistinguishable from "still
    working" - and delivery_worker.py's `if reply:` check meant no
    reply-ready file was ever written: the message just disappeared (hit
    live: a multi-tool-call investigation got interrupted mid-turn by the
    next voice command, and its answer never surfaced anywhere)."""

    async def scenario():
        bridge = ClaudeSessionBridge("session-x")
        bridge.client = FakeClient([AssistantMessage("session-x")])  # no ResultMessage
        await bridge._drain("msg-1")
        assert await bridge.reply_text("msg-1") == INTERRUPTED_SENTINEL

    asyncio.run(scenario())


def test_wait_completed_unblocks_even_when_interrupted():
    async def scenario():
        bridge = ClaudeSessionBridge("session-x")
        bridge._completed["msg-1"] = asyncio.Event()
        bridge.client = FakeClient([])
        await bridge._drain("msg-1")
        # Must not hang - submit()'s `await self._drain_task` on the next
        # message depends on this resolving even when interrupted.
        await asyncio.wait_for(bridge.wait_completed("msg-1"), timeout=1)

    asyncio.run(scenario())


if __name__ == "__main__":
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
            print(f"PASS {name}")
