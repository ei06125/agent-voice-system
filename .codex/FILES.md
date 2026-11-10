# DONNA voice system — file inventory

Generated 2026-08-22 for external (Codex) verification of a suspected delivery bug.

## Suspected root cause (please verify independently)

Voice capture → transcription → shadow queue is confirmed working (27+ utterances
successfully queued, legacy buffer file has pending content). Nothing reaches the
interactive Claude Code session anyway. Two candidate bugs, found by inspection,
not yet fixed:

1. **`~/.claude/settings.json` has no `Stop` hook registered at all.** Only
   `UserPromptSubmit` → `voice-interrupt-hook.sh` is present. The drain script
   `~/.claude/scripts/voice-stop-hook.sh` exists on disk and is executable, but
   nothing calls it — there is no `"Stop": [...]` entry in the `hooks` block.
2. **`voice-stop-hook.sh` line 51 writes the drained line to stdout**
   (`echo "$line"`) immediately before `exit 2`. Claude Code's Stop-hook
   blocking convention feeds **stderr** back to Claude on exit code 2 — stdout
   only reaches the transcript/user, not the model. Even if (1) were fixed,
   this line likely needs to be `echo "$line" >&2`.

Neither issue touches the daemon, VAD, Whisper, the SQLite queue, or
`delivery_worker.py` — all of that is intact.

## Repo: `src/donna/` (modular voice system)

- `src/donna/speech_to_text/` — microphone capture, Silero VAD, Whisper transcription
- `src/donna/text_to_speech/` — Kokoro, playback ownership, and speech CLI/worker
- `src/donna/orchestration/` — queue, Agent SDK bridge, delivery worker, and voice loop
- `src/donna/tools/` — MCP control tools
- `src/donna/*/tests/` — module unit tests
- `qa/` — end-to-end verification
- `pyproject.toml` — unified package/dependencies

## Repo: `package/` (deployable bundle for another machine)

- `package/README.md`
- `package/claude-config/hooks.snippet.json` — reference hooks block (currently only `UserPromptSubmit`; does **not** include a `Stop` entry — matches the gap found in `~/.claude/settings.json`)
- `package/claude-config/mcp-servers.snippet.json`
- `package/claude-config/voice-interrupt-hook.sh`

## Repo: docs / plans

- `CLAUDE.md` — authoritative operating rules for this project
- `VOICE_DELIVERY_PLAN.md` — phased delivery design (Phase 1–3)
- `PLAN.md`, `PROGRESS.md`, `STATUS.md`, `README.md`, `futurework.md`
- `DeepResearch*.md` — background research docs (afplay alternatives, echo cancellation, VAD, etc.)

## Home-scoped runtime state (`~/.claude/scripts/`)

- `.voice-buffer.txt` — legacy buffer; **currently has undrained content**
- `.voice-daemon.log` — listener runtime log
- `.voice-daemon.pid` — listener PID file
- `.voice-mute` — TTS mute flag (present = muted)
- `voice-capture.sh`
- `voice-interrupt-hook.sh` — registered as `UserPromptSubmit` hook (cancels TTS playback on typed input)
- `voice-listen.sh`
- `voice-stop-hook.sh` — **drain script, NOT registered as a `Stop` hook** (see root cause above)

## Home-scoped runtime state (`~/.claude/donna/`)

- `voice-delivery.sqlite3` (+ `-shm`/`-wal`) — durable queue database
- `delivery.sock` — best-effort notification socket
- `delivery-status.json`
- `claude-last-read.offset`
- `utterance-log.jsonl`
- `fillers/`, `playback/` — supporting state dirs

## Config (not files, but relevant)

- `~/.claude/settings.json` → `hooks` block: only `UserPromptSubmit`, **no `Stop` entry**
- `~/.claude.json` → `mcpServers.stt-local`: `uv run --project /Users/pedro/_/AGENTS/DONNA python -m donna.tools.stt_mcp_server`
- `~/.claude.json` → `mcpServers.kokoro-local`: `uv run --project /Users/pedro/_/AGENTS/DONNA python -m donna.text_to_speech.kokoro_mcp_server`
