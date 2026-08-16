import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from subprocess import Popen

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import soundfile as sf
import torch
from kokoro import KPipeline
from mcp.server.fastmcp import FastMCP

# playback_control.py lives in the separate stt-local project (its own
# venv), not this one - DONNA's project root is fixed on this machine, same
# convention as the other hardcoded paths here (WHISPER_BIN etc. in
# listen_daemon.py). Gives this server the same register/cancel ownership
# contract listen_daemon.py's barge-in and the typed UserPromptSubmit hook
# both use, instead of a third divergent PID-tracking implementation.
sys.path.insert(0, os.path.expanduser("~/_/AGENTS/DONNA/stt-local"))
from playback_control import register_playback, unregister_playback

mcp = FastMCP("kokoro-local")

# Signals the DONNA voice daemon (stt-local/listen_daemon.py) to discard
# capture while this plays, plus a short hangover after - VAD can't tell
# TTS output from real speech, so this is the only way to stop the daemon
# hearing its own playback and misreading it as a command (self-feedback,
# hit live in the DONNA session). See DeepResearchEchoCancellation.md
# "Mode A" for the full design; this is the writer side of that signal.
#
# kokoro-local is a user-scoped MCP server, so every open Claude Code
# session spawns its own copy - multiple can legitimately be alive and
# speaking at once. A single shared flag path let one process's `finally`
# cleanup delete another's still-in-progress flag, reopening the mic gate
# mid-playback (hit live: a speak() call describing this exact mechanism
# had its own wake phrase transcribed back as a "voice command" because a
# second server process's cleanup clobbered the first's flag). PID-suffix
# both files so each process only ever touches its own; the daemon side
# globs for *any* live one instead of checking a single fixed path.
SCRIPTS_DIR = os.path.expanduser("~/.claude/scripts")
PLAYBACK_FLAG = os.path.join(SCRIPTS_DIR, f".voice-playback-flag-{os.getpid()}")

# kokoro's own device auto-detect only checks torch.cuda.is_available(),
# never MPS - so it silently falls back to CPU on Apple Silicon. ~2x faster
# per call once warm (0.16s vs 0.33s), measured directly on this machine.
DEVICE = "mps" if torch.backends.mps.is_available() else "cpu"

PIPELINES: dict[str, KPipeline] = {}


def get_pipeline(lang_code: str) -> KPipeline:
    if lang_code not in PIPELINES:
        PIPELINES[lang_code] = KPipeline(
            lang_code=lang_code,
            repo_id="hexgrad/Kokoro-82M",
            device=DEVICE,
        )
    return PIPELINES[lang_code]


@mcp.tool()
def speak(
    text: str,
    voice: str = "af_heart",
    lang_code: str = "a",
    speed: float = 1,
    play: bool = True,
    output_dir: str = "outputs",
) -> list[str]:
    """Convert text to speech locally with Kokoro and optionally play it."""
    path = Path(output_dir)
    path.mkdir(exist_ok=True)

    pipeline = get_pipeline(lang_code)
    generator = pipeline(text, voice=voice, speed=speed, split_pattern=r"\n+")
    output_paths = []
    run_id = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")

    if play:
        open(PLAYBACK_FLAG, "a").close()
    try:
        for index, (_graphemes, _phonemes, audio) in enumerate(generator):
            output_path = path / f"mcp-{run_id}-{index}.wav"
            sf.write(output_path, audio, 24000)
            output_paths.append(str(output_path))

            if play:
                proc = Popen(["afplay", str(output_path)])
                register_playback(proc.pid)
                try:
                    proc.wait()
                finally:
                    unregister_playback(proc.pid)
                if proc.returncode is not None and proc.returncode < 0:
                    # Killed (e.g. by voice-interrupt-hook.sh on the next
                    # prompt submission, or listen_daemon.py's barge-in on
                    # hearing "Donna" again mid-reply) - don't keep playing
                    # later segments of this same reply over the new turn.
                    break
    finally:
        if play:
            try:
                os.remove(PLAYBACK_FLAG)
            except FileNotFoundError:
                pass

    return output_paths


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
