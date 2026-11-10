# DONNA Voice System

## Current status

Phases 1-3 of `docs/Technical/ARCHITECTURE/VOICE_DELIVERY_PLAN.md` are implemented. Production cutover
requires launching the SDK-owned session and setting its session ID.

- The microphone daemon, Silero VAD, Whisper transcription, wake gate, legacy text buffer, and Stop-hook delivery remain operational.
- Shadow is the safe default; existing shadow rows are permanently excluded.
- Deliver mode writes non-shadow rows for one explicit SDK session.
- `delivery_worker.py` owns socket wakeup, recovery, leasing and the SDK bridge.
- Stock Claude Code remains unsupported for mid-turn injection.

## Safety rules

- Do not start microphone capture without an explicit user request.
- Start one listener only. Use the MCP control tools instead of launching duplicate daemons.
- Do not delete, truncate, or rewrite the SQLite queue.
- Do not display stored transcript text unless the user asks for it. Queue inspection omits text by default.
- Do not disable the legacy Stop-hook path while the queue is in shadow mode.
- Preserve unrelated working-tree changes.

## Components

| Component | Path or interface | Role |
|---|---|---|
| Listener daemon | `src/donna/speech_to_text/listen_daemon.py` | Microphone, VAD, Whisper, wake/control phrases, legacy buffer, shadow queue producer |
| Listener MCP server | `src/donna/tools/stt_mcp_server.py` | Exposes `start_listener`, `stop_listener`, and `check_readiness` |
| Durable queue | `src/donna/orchestration/voice_queue.py` | SQLite schema, atomic enqueue, notification, inspection |
| Legacy buffer | `$HOME/.claude/scripts/.voice-buffer.txt` | Current Stop-hook delivery source |
| Listener log | `$HOME/.claude/scripts/.voice-daemon.log` | Runtime diagnostics |
| Queue database | `$HOME/.claude/donna/voice-delivery.sqlite3` | Durable shadow messages |
| Notification socket | `$HOME/.claude/donna/delivery.sock` | Best-effort wake signal; SQLite remains authoritative |

## Enable shadow mode

`DONNA_VOICE_QUEUE_MODE` defaults to `shadow`. Set it explicitly in MCP config:

```text
DONNA_VOICE_QUEUE_MODE=shadow
```

For the user-scoped `stt-local` MCP server, set this value in that server's environment configuration. The effective entry in `$HOME/.claude.json` should contain:

```json
{
  "mcpServers": {
    "stt-local": {
      "env": {
        "DONNA_VOICE_QUEUE_MODE": "shadow"
      }
    }
  }
}
```

Preserve the entry's existing command, arguments, and other environment values. Ask before editing user-scoped Claude configuration.

Environment changes do not affect an already-running MCP server or detached listener. Use this sequence:

1. Call `mcp__stt-local__stop_listener` if the daemon is running.
2. Configure `DONNA_VOICE_QUEUE_MODE=shadow` for the `stt-local` MCP server.
3. Restart or reconnect the `stt-local` MCP server so it receives the new environment.
4. Call `mcp__stt-local__start_listener`.
5. Confirm the daemon log contains `shadow-queued voice command` after a test command.
6. Confirm the queue status reports a shadow pending row.

If changing MCP configuration is not desired, run a foreground diagnostic listener from the repository root:

```sh
DONNA_VOICE_QUEUE_MODE=shadow uv run python -m donna.speech_to_text.listen_daemon
```

Stop the foreground diagnostic with `Ctrl-C`. Do not run it while the MCP-managed daemon is active.

## Normal operation

When the user explicitly requests voice mode:

1. Call `mcp__stt-local__check_readiness` before telling the user it is safe to
   speak. It starts the listener if needed and reports `ready: true/false`
   plus a `problems` list covering the listener process, queue mode, the
   hook-disabled flag, and queue reachability. `mcp__stt-local__start_listener`
   remains available directly if a readiness check isn't wanted.
2. If it reports `already running`, do not start another process.
3. Leave the listener running across Claude turns. Do not stop and restart it after every response.
4. In shadow mode only, the old Stop hook remains the compatibility path.
5. Call `mcp__stt-local__stop_listener` when the user asks to stop microphone capture.

`check_readiness` only verifies the shadow-mode Stop-hook path used to reach
*this* interactive session. It does not start or check `delivery_worker.py` -
that process drives a separate, Agent-SDK-owned session and is unrelated to
whether this terminal can hear you.

The production wake phrase is:

```text
Oh my god, Donna
```

It may occur anywhere in a finalized utterance.

- Wake phrase plus text: the remainder is buffered and shadow-queued immediately.
- Wake phrase alone: arms capture; the next finalized utterance becomes the first command.
- While capture remains open, later finalized utterances are also buffered and shadow-queued.
- The legacy Stop-hook drain closes the current capture. A new wake phrase is then required.

Current voice controls:

- `OMG mute`: suppress spoken replies while listening continues.
- `OMG speak`: allow spoken replies.
- `OMG go deaf`: stop the listener. It cannot hear a command to restart itself.

The file `$HOME/.claude/scripts/.voice-hook-disabled` blocks wake-authorized commands. Control phrases are checked before that flag. If speech appears ignored, check whether the flag exists before diagnosing VAD or Whisper.

## Inspect the queue

Run from the repository root:

```sh
uv run python -m donna.orchestration.voice_queue status
```

List message metadata without transcript text:

```sh
uv run python -m donna.orchestration.voice_queue list --limit 20
```

Only when the user asks to inspect transcript content:

```sh
uv run python -m donna.orchestration.voice_queue list --limit 20 --include-text
```

Expected Phase 1 rows:

- `state` is `pending`.
- `is_shadow` is `1`.
- `capture_id` groups utterances authorized by one wake event.
- `utterance_seq` increases within that capture.
- `message_id` uniquely identifies the durable event.
- Repeating the same words intentionally creates a new message ID.

Do not replay these rows. Phase 2 must explicitly ignore `is_shadow=1` when its worker is implemented.

## Logs and diagnostics

Read recent listener output:

```sh
tail -n 100 "$HOME/.claude/scripts/.voice-daemon.log"
```

Useful messages:

- `shadow-queued voice command <id>`: SQLite commit succeeded. Socket notification may or may not have a receiver; the row is still durable.
- `shadow queue unavailable`: queue initialization failed; legacy buffering continues.
- `shadow queue write failed`: the specific utterance was not mirrored; legacy buffering continues.
- `buffer full`: the legacy ring buffer dropped its oldest line. The durable shadow queue is independent.
- `transcription backlog full`: an audio clip was dropped before transcription.
- `VAD backlog full`: a microphone frame was dropped before VAD processing.

Diagnostic order:

1. Verify `mcp__stt-local__start_listener` reports running or started.
2. Verify `DONNA_VOICE_QUEUE_MODE=shadow` reached the MCP server and listener.
3. Check `.voice-hook-disabled`.
4. Check the listener log for microphone selection, transcript output, and queue messages.
5. Run queue `status`.
6. Inspect text only with user approval.
7. Treat missing socket notification as non-fatal if the SQLite row exists.

## TTS limitations

Kokoro TTS remains separate from queue delivery.

- Typed `UserPromptSubmit` cancels active playback through the existing hook.
- The listener discards microphone frames while Kokoro playback flags exist, plus a short hangover.
- Spoken barge-in during TTS is therefore not implemented.
- Do not remove the playback gate until an echo-aware capture path is implemented and tested.

## Verification

Run from `stt-local`:

```sh
uv run python tests/test_voice_queue.py
uv run python tests/test_voice_queue_listener.py
uv run python tests/test_playback_gate.py
uv run python -m compileall -q listen_daemon.py voice_queue.py tests
```

The Unix-socket notification test must be allowed to bind a temporary local socket.

## Next phase boundary

Do not implement Phase 2 without explicit approval. Phase 2 adds atomic leasing, lease expiry, acknowledgement, dead letters, accepted-ID deduplication, and a deterministic test sink. It still must not submit shadow rows to Claude.
