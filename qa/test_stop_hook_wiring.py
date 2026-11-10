"""Regression tests for the Stop-hook delivery path being silently unwired.

Hit live on 2026-08-22: ~/.claude/settings.json had no "Stop" hook entry at
all - only "UserPromptSubmit" - so voice-stop-hook.sh never ran, and spoken
commands piled up in the shadow queue and the legacy buffer with nothing
ever draining them into a Claude turn. The packaged reference snippet
(package/claude-config/hooks.snippet.json) had the same gap, so re-installing
from the package would have reproduced the bug on a fresh machine too.

Separately, voice-stop-hook.sh wrote its drained line to stdout before
`exit 2`. Claude Code's Stop-hook blocking convention feeds stderr back to
Claude on exit 2 - stdout only reaches the transcript. Even with the hook
wired up, the drained command would not have reached Claude.

These tests catch both failure modes so they can't silently regress again.
"""

import json
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG_DIR = os.path.join(REPO_ROOT, "package", "claude-config")
SNIPPET_PATH = os.path.join(CONFIG_DIR, "hooks.snippet.json")
SETTINGS_SNAPSHOT_PATH = os.path.join(CONFIG_DIR, "settings.snapshot.json")
STOP_HOOK_SNAPSHOT_PATH = os.path.join(CONFIG_DIR, "voice-stop-hook.sh")
LIVE_SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
STOP_HOOK_SCRIPT = os.path.expanduser("~/.claude/scripts/voice-stop-hook.sh")


def _stop_hook_entries(hooks_block: dict) -> list[str]:
    commands = []
    for entry in hooks_block.get("Stop", []):
        for hook in entry.get("hooks", []):
            commands.append(hook.get("command", ""))
    return commands


def test_packaged_snippet_registers_stop_hook():
    with open(SNIPPET_PATH) as f:
        data = json.load(f)
    commands = _stop_hook_entries(data.get("hooks", {}))
    assert any("voice-stop-hook.sh" in c for c in commands), (
        "package/claude-config/hooks.snippet.json must register a Stop hook "
        "pointing at voice-stop-hook.sh, or a fresh install reproduces the "
        "2026-08-22 silent-delivery bug"
    )


def test_live_settings_registers_stop_hook():
    if not os.path.exists(LIVE_SETTINGS_PATH):
        import pytest

        pytest.skip("no live ~/.claude/settings.json on this machine")
    with open(LIVE_SETTINGS_PATH) as f:
        data = json.load(f)
    commands = _stop_hook_entries(data.get("hooks", {}))
    assert any("voice-stop-hook.sh" in c for c in commands), (
        "~/.claude/settings.json has no Stop hook wired to voice-stop-hook.sh "
        "- spoken commands will queue but never reach this session"
    )


def test_settings_snapshot_matches_live():
    if not (os.path.exists(LIVE_SETTINGS_PATH) and os.path.exists(SETTINGS_SNAPSHOT_PATH)):
        import pytest

        pytest.skip("live settings.json or its repo snapshot is missing")
    with open(LIVE_SETTINGS_PATH) as f:
        live = f.read()
    with open(SETTINGS_SNAPSHOT_PATH) as f:
        snapshot = f.read()
    assert live == snapshot, (
        "package/claude-config/settings.snapshot.json has drifted from "
        "~/.claude/settings.json - run package/claude-config/sync.sh"
    )


def test_stop_hook_script_snapshot_matches_live():
    if not (os.path.exists(STOP_HOOK_SCRIPT) and os.path.exists(STOP_HOOK_SNAPSHOT_PATH)):
        import pytest

        pytest.skip("live voice-stop-hook.sh or its repo snapshot is missing")
    with open(STOP_HOOK_SCRIPT) as f:
        live = f.read()
    with open(STOP_HOOK_SNAPSHOT_PATH) as f:
        snapshot = f.read()
    assert live == snapshot, (
        "package/claude-config/voice-stop-hook.sh has drifted from "
        "~/.claude/scripts/voice-stop-hook.sh - run package/claude-config/sync.sh"
    )


def test_stop_hook_drains_to_stderr_not_stdout():
    if not os.path.exists(STOP_HOOK_SCRIPT):
        import pytest

        pytest.skip("voice-stop-hook.sh not installed on this machine")

    with tempfile.TemporaryDirectory() as fake_home:
        scripts_dir = os.path.join(fake_home, ".claude", "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        marker = "REGRESSION_TEST_MARKER_LINE"
        with open(os.path.join(scripts_dir, ".voice-buffer.txt"), "w") as f:
            f.write(marker + "\n")

        env = dict(os.environ)
        env["HOME"] = fake_home

        result = subprocess.run(
            ["/bin/zsh", STOP_HOOK_SCRIPT],
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )

    assert result.returncode == 2, (
        f"expected exit 2 (blocking, deliver to Claude) with a non-empty "
        f"buffer, got {result.returncode}"
    )
    assert marker in result.stderr, (
        "drained command must be on stderr - Claude Code's Stop-hook exit-2 "
        "convention only feeds stderr back to Claude, not stdout"
    )
    assert marker not in result.stdout, (
        "drained command was written to stdout, where Claude never sees it "
        "(regression of the 2026-08-22 stdout/stderr bug)"
    )


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
            except Exception as e:  # noqa: BLE001
                if type(e).__name__ == "Skipped":
                    print(f"SKIP {name}")
                else:
                    failures += 1
                    print(f"ERROR {name}: {e}")
    sys.exit(1 if failures else 0)
