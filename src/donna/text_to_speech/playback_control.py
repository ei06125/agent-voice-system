"""Safe ownership-based cancellation for Kokoro playback processes."""

from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path

DEFAULT_CONTROL_DIR = Path(
    os.environ.get("DONNA_PLAYBACK_CONTROL_DIR", "~/.claude/donna/playback")
).expanduser()


def process_start_token(pid: int) -> str | None:
    """Return an OS token that changes when a PID is reused (Linux/macOS)."""
    try:
        return str(os.stat(f"/proc/{pid}").st_ctime_ns)
    except OSError:
        try:
            import subprocess

            return (
                subprocess.run(
                    ["ps", "-o", "lstart=", "-p", str(pid)],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                or None
            )
        except OSError:
            return None


def register_playback(pid: int, control_dir: Path = DEFAULT_CONTROL_DIR) -> Path:
    """Called by the playback owner immediately after spawning its child."""
    control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = process_start_token(pid)
    if token is None:
        raise ProcessLookupError(pid)
    path = control_dir / f"{pid}.json"
    temporary = control_dir / f".{pid}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({"pid": pid, "start_token": token}))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return path


def unregister_playback(pid: int, control_dir: Path = DEFAULT_CONTROL_DIR) -> None:
    path = control_dir / f"{pid}.json"
    try:
        record = json.loads(path.read_text())
        if record.get("pid") == pid:
            path.unlink()
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def cancel_playback(
    control_dir: Path = DEFAULT_CONTROL_DIR, timeout: float = 0.15
) -> list[int]:
    """Terminate only children whose PID identity still matches their record."""
    cancelled: list[int] = []
    if not control_dir.exists():
        return cancelled
    for path in control_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text())
            pid = int(record["pid"])
            if process_start_token(pid) != record["start_token"]:
                path.unlink(missing_ok=True)
                continue
            os.kill(pid, signal.SIGTERM)
            cancelled.append(pid)
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
    deadline = time.monotonic() + timeout
    for pid in cancelled:
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.005)
    return cancelled


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cancel owned Kokoro playback")
    parser.add_argument("cancel", nargs="?")
    parser.add_argument("--control-dir", type=Path, default=DEFAULT_CONTROL_DIR)
    args = parser.parse_args()
    print(json.dumps({"cancelled_pids": cancel_playback(args.control_dir)}))
