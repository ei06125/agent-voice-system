#!/bin/zsh
# UserPromptSubmit hook: a new prompt just landed, so any kokoro-local TTS
# still playing from the previous turn is now stale - kill it immediately
# instead of letting it keep talking over the new turn.
#
# Uses playback_control.py's register/cancel ownership contract (PID +
# start-token, guards against PID reuse) - the same one mcp_server.py
# registers its afplay child under, and the same one listen_daemon.py's
# barge-in (hearing "Donna" again mid-reply) calls independently. One
# mechanism, three trigger points, instead of a divergent PID-file scheme
# per caller.
#
# Never blocks prompt submission: always exits 0.
set -uo pipefail

uv run --project "$HOME/_/AGENTS/DONNA" \
  python -m donna.text_to_speech.playback_control cancel \
  >/dev/null 2>&1

exit 0
