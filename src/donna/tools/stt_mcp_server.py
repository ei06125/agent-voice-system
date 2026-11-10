"""MCP server exposing start/stop control for the always-on voice listener.

listen_daemon.py runs independently of any Claude Code turn or Stop hook -
these two tools are the only supported way to start or stop it, so its
lifecycle is explicit and agent-controlled rather than auto-starting at
boot (no silent background mic transcription).
"""

import os
import signal
import subprocess
import sys
import time

from mcp.server.fastmcp import FastMCP

from donna.orchestration.voice_queue import VoiceQueue

mcp = FastMCP("stt-local")

DAEMON_MODULE = "donna.speech_to_text.listen_daemon"
SCRIPTS_DIR = os.path.expanduser("~/.claude/scripts")
PID_FILE = os.path.join(SCRIPTS_DIR, ".voice-daemon.pid")
LOG_FILE = os.path.join(SCRIPTS_DIR, ".voice-daemon.log")
DISABLE_FLAG = os.path.join(SCRIPTS_DIR, ".voice-hook-disabled")


def _running_pid() -> int | None:
    try:
        with open(PID_FILE) as f:
            pid = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


@mcp.tool()
def start_listener() -> str:
    """Start the always-on voice listener daemon, if not already running."""
    pid = _running_pid()
    if pid is not None:
        return f"already running (pid {pid})"
    with open(LOG_FILE, "a") as log:
        subprocess.Popen(
            [sys.executable, "-m", DAEMON_MODULE],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return "started"


@mcp.tool()
def check_readiness() -> dict:
    """Verify (starting it if needed) everything the shadow-mode Stop-hook
    path needs to carry spoken commands into this Claude Code session:
    the listener process, DONNA_VOICE_QUEUE_MODE, the hook-disabled flag,
    and that the durable queue is reachable. Call this before telling the
    user it is safe to start speaking.
    """
    listener_pid = _running_pid()
    started = False
    if listener_pid is None:
        with open(LOG_FILE, "a") as log:
            subprocess.Popen(
                [sys.executable, "-m", DAEMON_MODULE],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        started = True
        for _ in range(20):
            time.sleep(0.1)
            listener_pid = _running_pid()
            if listener_pid is not None:
                break

    queue_mode = os.environ.get("DONNA_VOICE_QUEUE_MODE", "shadow").lower()
    hook_disabled = os.path.exists(DISABLE_FLAG)

    queue_reachable = True
    queue_error = None
    try:
        VoiceQueue().status()
    except Exception as error:  # noqa: BLE001 - surfaced to the caller, not raised
        queue_reachable = False
        queue_error = f"{type(error).__name__}: {error}"

    problems = []
    if listener_pid is None:
        problems.append("listener process did not come up")
    if queue_mode != "shadow":
        problems.append(
            f"DONNA_VOICE_QUEUE_MODE is '{queue_mode}', not 'shadow' - the "
            "Stop-hook compatibility path this session relies on requires shadow mode"
        )
    if hook_disabled:
        problems.append(".voice-hook-disabled is set - wake-authorized commands are blocked")
    if not queue_reachable:
        problems.append(f"voice queue unreachable: {queue_error}")

    return {
        "ready": not problems,
        "listener_pid": listener_pid,
        "listener_started_now": started,
        "queue_mode": queue_mode,
        "hook_disabled": hook_disabled,
        "queue_reachable": queue_reachable,
        "problems": problems,
    }


@mcp.tool()
def stop_listener() -> str:
    """Stop the always-on voice listener daemon, if running."""
    pid = _running_pid()
    if pid is None:
        return "not running"
    os.kill(pid, signal.SIGTERM)
    try:
        os.remove(PID_FILE)
    except FileNotFoundError:
        pass
    return f"stopped (pid {pid})"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
