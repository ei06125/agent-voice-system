import sys
from pathlib import Path

import pytest

from donna.orchestration.agent_adapter import available_agent_adapters, create_agent_adapter
from donna.orchestration.claude_bridge import ClaudeSessionBridge


def test_claude_is_the_default_available_adapter():
    assert available_agent_adapters() == ("claude",)


def test_factory_builds_claude_adapter_with_session_and_cwd(tmp_path: Path):
    adapter = create_agent_adapter("claude", "session-x", tmp_path)
    assert isinstance(adapter, ClaudeSessionBridge)
    assert adapter.agent_id == "claude"
    assert adapter.session_id == "session-x"
    assert adapter.cwd == tmp_path


def test_factory_rejects_unknown_adapter(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown agent adapter 'unknown'"):
        create_agent_adapter("unknown", "session-x", tmp_path)
