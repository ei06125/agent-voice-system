#!/bin/zsh
# One-way backup: live ~/.claude config -> versioned copies in this repo.
#
# The live files are the source of truth and are edited directly (Claude
# Code reads them from $HOME, not from this repo). This script's only job
# is to keep a diffable, git-tracked snapshot in sync with them, so drift
# is visible in `git diff` and `tests/test_stop_hook_wiring.py` can catch
# it in CI/regression runs instead of it going unnoticed like the missing
# Stop hook did on 2026-08-22.
#
# Run this after any manual edit to the live files.
set -euo pipefail

script_dir=${0:A:h}

cp "$HOME/.claude/settings.json" "$script_dir/settings.snapshot.json"
cp "$HOME/.claude/scripts/voice-stop-hook.sh" "$script_dir/voice-stop-hook.sh"
cp "$HOME/.claude/scripts/voice-interrupt-hook.sh" "$script_dir/voice-interrupt-hook.sh"

echo "synced settings.json, voice-stop-hook.sh, voice-interrupt-hook.sh into $script_dir"
