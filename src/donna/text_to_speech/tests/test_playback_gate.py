"""Regression test for the cross-process playback-flag clobber bug.

Hit live: kokoro-local is a user-scoped MCP server, so multiple server
processes (one per open Claude Code session) can be alive and calling
speak() concurrently. When PLAYBACK_FLAG was a single shared path
(.voice-playback), one process's `finally: os.remove(PLAYBACK_FLAG)`
after its own playback finished deleted the flag while a *second*
process was still mid-playback - reopening listen_daemon.py's mic gate
early. The daemon then transcribed the second process's own TTS output
and delivered it back to Claude as if it were a real spoken command.

Fix: each process writes a PID-suffixed flag file
(.voice-playback-flag-<pid>); the daemon's gate (is_playback_active)
globs for *any* match instead of checking one fixed path. This test
simulates exactly the concurrent scenario above and asserts the gate
stays active until every process's flag is gone, not just one.
"""

import os
import sys
import tempfile

from donna.speech_to_text.listen_daemon import is_playback_active  # noqa: E402


def _touch(path: str) -> None:
    open(path, "a").close()


def test_gate_inactive_with_no_flags():
    with tempfile.TemporaryDirectory() as scripts_dir:
        assert is_playback_active(scripts_dir) is False


def test_gate_active_with_single_process_flag():
    with tempfile.TemporaryDirectory() as scripts_dir:
        _touch(os.path.join(scripts_dir, ".voice-playback-flag-111"))
        assert is_playback_active(scripts_dir) is True


def test_second_process_cleanup_does_not_clear_first_process_gate():
    """The actual regression: two concurrent speak() calls from two
    different server processes (e.g. two open Claude Code sessions).
    Process B finishing and removing *its own* flag must not affect
    process A, which is still speaking."""
    with tempfile.TemporaryDirectory() as scripts_dir:
        flag_a = os.path.join(scripts_dir, ".voice-playback-flag-111")
        flag_b = os.path.join(scripts_dir, ".voice-playback-flag-222")

        _touch(flag_a)  # process A starts speaking
        _touch(flag_b)  # process B starts speaking concurrently
        assert is_playback_active(scripts_dir) is True

        os.remove(flag_b)  # process B finishes, cleans up only its own flag
        assert is_playback_active(scripts_dir) is True, (
            "gate dropped while process A's flag is still present - "
            "this is the exact bug: a shared flag path lets one "
            "process's cleanup silently unmute the mic during another "
            "process's playback"
        )

        os.remove(flag_a)  # process A finally finishes too
        assert is_playback_active(scripts_dir) is False


def test_stale_flag_from_crashed_process_still_gates():
    """A process that dies before its `finally` runs (e.g. a cancelled
    MCP tool call) leaves an orphaned flag - this is a known, accepted
    failure mode (fails open toward "gate stays on", not toward silent
    self-feedback) rather than something this test tries to auto-clear."""
    with tempfile.TemporaryDirectory() as scripts_dir:
        _touch(os.path.join(scripts_dir, ".voice-playback-flag-999"))
        assert is_playback_active(scripts_dir) is True


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
