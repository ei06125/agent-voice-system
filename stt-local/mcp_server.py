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

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("stt-local")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DAEMON_SCRIPT = os.path.join(SCRIPT_DIR, "listen_daemon.py")
SCRIPTS_DIR = os.path.expanduser("~/.claude/scripts")
PID_FILE = os.path.join(SCRIPTS_DIR, ".voice-daemon.pid")
LOG_FILE = os.path.join(SCRIPTS_DIR, ".voice-daemon.log")


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
            [sys.executable, DAEMON_SCRIPT],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    return "started"


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
