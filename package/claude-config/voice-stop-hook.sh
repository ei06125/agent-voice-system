#!/bin/zsh
# Stop hook: watches the buffer file that the always-on listen_daemon.py
# fills when it hears the wake phrase - the daemon itself is started/
# stopped via the stt-local MCP server's start_listener/stop_listener
# tools, NOT by this hook, so it runs independent of any Claude turn.
#
# Runs detached (asyncRewake) and POLLS the buffer for up to MAX_WAIT
# seconds instead of checking once and returning - a Claude turn that
# runs long (many chained tool calls, no natural Stop event) previously
# meant nothing got surfaced until that turn happened to end AND the
# buffer happened to have content at that exact instant. Polling in this
# background bash process costs nothing while idle (no Claude turn, no
# tokens) and delivers the moment content actually appears - push, not
# poll-by-re-invoking-Claude.
#
# Only one poller runs at a time (PID-file lock): if turns end frequently
# during normal chat, each Stop event would otherwise spawn its own
# overlapping poller racing on the same buffer file (double-delivery
# risk). A Stop event that finds one already watching just no-ops.
#
# .voice-hook-disabled: manual (typed/touched) master switch - while set,
# the daemon skips wake/buffering entirely. Independent of voice "mute"
# (TTS-only, see .voice-mute) and voice "deaf" (stops the daemon itself -
# restart via the start_listener MCP tool, since a stopped daemon can't
# hear an "unmute"-style revert word by definition).
set -uo pipefail

BUFFER_FILE="$HOME/.claude/scripts/.voice-buffer.txt"
STOP_SOUND="/System/Library/Sounds/Pop.aiff"
POLLER_PID_FILE="$HOME/.claude/scripts/.voice-hook-poller.pid"
POLL_INTERVAL=2
MAX_WAIT=3600

if [[ -f "$POLLER_PID_FILE" ]]; then
  existing_pid=$(cat "$POLLER_PID_FILE" 2>/dev/null)
  if [[ -n "$existing_pid" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    exit 0
  fi
fi
echo $$ > "$POLLER_PID_FILE"
trap 'rm -f "$POLLER_PID_FILE"' EXIT

elapsed=0
while (( elapsed < MAX_WAIT )); do
  if [[ -s "$BUFFER_FILE" ]]; then
    raw=$(cat "$BUFFER_FILE")
    : > "$BUFFER_FILE"
    line=$(echo "$raw" | tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//')
    if [[ -n "$line" ]]; then
      afplay "$STOP_SOUND" >/dev/null 2>&1 &
      echo "$line" >&2
      exit 2
    fi
  fi
  sleep "$POLL_INTERVAL"
  elapsed=$((elapsed + POLL_INTERVAL))
done
exit 0
