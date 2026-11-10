"""Agent SDK boundary for voice-command delivery."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol


class AgentAdapter(Protocol):
    agent_id: str
    session_id: str

    async def connect(self) -> None: ...
    async def accepted_ids(self) -> set[str]: ...
    async def submit(self, message_id: str, text: str) -> None: ...
    async def wait_completed(self, message_id: str) -> None: ...
    async def reply_text(self, message_id: str) -> str | None: ...
    async def close(self) -> None: ...


AgentAdapterFactory = Callable[[str, Path], AgentAdapter]


def _claude_factory(session_id: str, cwd: Path) -> AgentAdapter:
    from donna.orchestration.claude_bridge import ClaudeSessionBridge

    return ClaudeSessionBridge(session_id, cwd)


_FACTORIES: dict[str, AgentAdapterFactory] = {"claude": _claude_factory}


def available_agent_adapters() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def create_agent_adapter(agent_id: str, session_id: str, cwd: Path) -> AgentAdapter:
    try:
        factory = _FACTORIES[agent_id]
    except KeyError as error:
        available = ", ".join(available_agent_adapters())
        raise ValueError(f"unknown agent adapter {agent_id!r}; available: {available}") from error
    return factory(session_id, cwd)
