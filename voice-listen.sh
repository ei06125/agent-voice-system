#!/bin/zsh
# Wake-word gated mic listener with debounce, for the hands-free voice loop.
# Streams dg listen --mic. A line only starts a command if it begins with
# a wake word ("Claude" / "Cloud" - common STT mishearing for it). Once a
# command starts, we wait up to DEBOUNCE seconds for more lines (a natural
# pause can split one sentence into several "final" transcripts) before
# treating the utterance as complete and echoing it to stdout as one event.
set -uo pipefail

DEBOUNCE=3.0
WAKE_RE='^[[:space:]]*(okay[,.]?[[:space:]]*)?(claude|cloud)[,.:]?[[:space:]]*'

flush() {
  [[ -z "$buffer" ]] && return
  osascript "$HOME/.claude/scripts/notify.applescript" "Wake word heard: $buffer" >/dev/null 2>&1
  echo "$buffer"
  buffer=""
}

buffer=""
in_command=0

dg listen --mic --non-interactive 2>/dev/null | while true; do
  if [[ $in_command -eq 1 ]]; then
    if IFS= read -r -t $DEBOUNCE line; then
      case "$line" in
        "✓ Listening"*|"") continue ;;
      esac
      buffer="$buffer $line"
    else
      flush
      in_command=0
    fi
  else
    if IFS= read -r line; then
      case "$line" in
        "✓ Listening"*|"") continue ;;
      esac
      lower=$(echo "$line" | tr '[:upper:]' '[:lower:]')
      if [[ "$lower" =~ $WAKE_RE ]]; then
        command_text=$(echo "$line" | sed -E 's/^[[:space:]]*([Oo]kay[,.]?[[:space:]]*)?([Cc]laude|[Cc]loud)[,.:]?[[:space:]]*//')
        [[ -z "$command_text" ]] && continue
        buffer="$command_text"
        in_command=1
      fi
    else
      flush
      break
    fi
  fi
done
