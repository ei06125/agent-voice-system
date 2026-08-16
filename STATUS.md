# Donna delivery handoff status

Date: 2026-08-16

## Executive status

The durable Phase 2 queue and delivery worker are implemented and covered by
focused automated tests. The Phase 3 Claude Agent SDK bridge is implemented but
has not been run against a real Claude-owned session. TTS cancellation has a
safe controller, but the external Kokoro MCP server has not yet been patched to
register its playback child. Operational cutover has not occurred.

Do not describe the one-second active-turn goal as validated yet.

## Implemented

- SQLite schema v2 migration preserving existing schema-v1 rows.
- Historical and new `is_shadow=1` rows are excluded inside the leasing
  transaction and can never be submitted or dead-letter-replayed.
- Atomic ordered leasing for one explicit `target_session`.
- Lease expiry recovery and periodic lease renewal.
- Retry with bounded exponential backoff.
- Dead-letter transition, inspection, and explicit replay.
- Durable accepted-ID ledger for ordinary acceptance/acknowledgement crash
  recovery.
- Separate `accepted` and `completed` lifecycle states.
- Acceptance latency and state-transition event records.
- Completed-row retention command; pending and dead-letter rows are never
  silently pruned.
- Unix datagram socket receiver plus 100 ms timed/startup scans.
- Deterministic JSONL sink for delivery tests.
- Persistent `ClaudeSDKClient` wrapper with:
  - explicit session ID;
  - project working directory;
  - serialized submission;
  - active-response interruption;
  - response draining before the next query;
  - stream session-ID validation;
  - accepted/completed tracking;
  - `<voice-command-id>` markers.
- Listener modes `off`, `shadow`, and `deliver`.
- `shadow` is now the listener default.
- `deliver` refuses startup without `DONNA_CLAUDE_SESSION_ID`, creates only
  deliverable rows, and does not write the legacy Stop-hook buffer.
- Production wake phrase: “Oh my god, Donna.”
- PID-start-token-safe playback cancellation controller.
- Worker heartbeat/status file and queue health command.
- Example launchd service at `ops/com.donna.voice-delivery.plist.example`.
- Agent SDK dependency recorded in `stt-local/pyproject.toml` and `uv.lock`.

## Automated verification completed

From `stt-local`:

```sh
uv run --with pytest pytest -q \
  tests/test_voice_queue.py tests/test_delivery_worker.py
```

Result: `15 passed`.

The tests cover queue creation/migration behavior, producer deduplication,
shadow exclusion, atomic leasing, lease expiry, backoff/dead letters, replay,
two-worker leasing races, ordered delivery, accepted-ID sink deduplication, the
external-acceptance/database-acknowledgement crash window, and socket wakeup.

The changed delivery files also passed Ruff, `compileall`, and
`git diff --check`.

The complete listener suite could not be collected in the implementation
environment because its Linux image had no PortAudio shared library. Run those
tests on the target Mac:

```sh
cd /Users/pedro/_/AGENTS/DONNA/stt-local
uv run --with pytest pytest -q
```

## Not completed or not validated

- No real Agent SDK session was launched.
- No synthetic prompt was submitted to Claude through the bridge.
- Interruption of a genuinely long Claude response was not measured.
- Session unavailable/reconnect behavior was not exercised against Claude.
- The launchd service template was not installed.
- User-scoped Claude MCP environment was not edited or restarted.
- The legacy Stop hook was not removed or disabled.
- The external Kokoro MCP server was not patched to call
  `register_playback()`/`unregister_playback()` around its exact playback
  child. Therefore queue-triggered cancellation is implemented at the caller
  but cannot find Kokoro playback until that registration is added.
- The typed-prompt cancellation hook was not changed to call
  `playback_control.py cancel`.
- No real TTS-overlap test was run.
- No acoustic echo cancellation was implemented. The listener still discards
  microphone frames during TTS, so true spoken barge-in remains unavailable.
- No microphone test was started; project safety rules require explicit user
  authorization before microphone capture.

## Required architecture decision

Donna may own Claude through the Agent SDK wrapper. This is the selected
architecture for guaranteed mid-turn delivery.

Do not run the bridge concurrently against a stock Claude Code process that is
also writing the same resumed session. Stock Claude Code has no supported
arbitrary prompt-injection IPC, and concurrent resume owners can interleave
session history. The bridge must be the sole live owner for the session being
tested.

## Recommended Claude test sequence

### 1. Re-run automated tests on the target Mac

```sh
cd /Users/pedro/_/AGENTS/DONNA/stt-local
uv sync
uv run --with pytest pytest -q
```

Investigate any failures before starting the bridge.

### 2. Inspect existing queue state without transcript text

```sh
uv run python voice_queue.py status
uv run python voice_queue.py list --limit 20
```

Do not convert or submit existing shadow rows. Do not use `--include-text`
unless the user explicitly asks to inspect stored transcripts.

### 3. Establish a solely SDK-owned Claude session

Choose or create the session that the wrapper will own. Confirm that no stock
Claude Code process is concurrently using it. Record its exact session ID:

```sh
export DONNA_CLAUDE_SESSION_ID='<SDK_OWNED_SESSION_ID>'
```

### 4. Start the worker in the foreground

```sh
cd /Users/pedro/_/AGENTS/DONNA
uv run --project stt-local python stt-local/delivery_worker.py \
  --session "$DONNA_CLAUDE_SESSION_ID"
```

In another terminal:

```sh
uv run --project stt-local python stt-local/voice_queue.py health
```

Expected: socket present, worker state `ready`, matching session ID, recent
heartbeat, and `healthy: true`.

### 5. Run a synthetic prompt test before enabling the microphone

```sh
cd /Users/pedro/_/AGENTS/DONNA/stt-local
uv run python - <<'PY'
import os
import uuid

from voice_queue import VoiceQueue

session_id = os.environ["DONNA_CLAUDE_SESSION_ID"]
test_id = str(uuid.uuid4())
result = VoiceQueue().enqueue_and_notify(
    message_id=test_id,
    capture_id=f"synthetic-{test_id}",
    utterance_seq=1,
    text="Reply with exactly: Donna bridge accepted.",
    is_shadow=False,
    target_session=session_id,
)
print(result.message_id)
PY
```

Confirm the correct Claude session receives exactly one prompt, then inspect
metadata:

```sh
uv run python voice_queue.py list --limit 20
uv run python voice_queue.py health
```

Expected row transition: `pending` -> `leased` -> `accepted` -> `completed`.
The prompt must contain the same machine-readable voice command ID and must not
appear twice.

### 6. Test active-turn interruption

Start a deliberately long response through the same SDK-owned client. During
that response, enqueue another uniquely identified synthetic message. Verify:

- the first response is interrupted;
- its stream is drained before the next query;
- the second prompt is accepted by the same session;
- the second queue row becomes `accepted` without a Stop event;
- no prompt is duplicated;
- queue commit-to-acceptance latency is recorded.

Test reconnect, worker termination after lease, and termination after SDK
acceptance but before SQLite acknowledgement before production cutover.

### 7. Finish Kokoro integration

In `/Users/pedro/.mcp/kokoro-local/mcp_server.py`, wrap the exact playback
child lifecycle:

```python
from playback_control import register_playback, unregister_playback

player = subprocess.Popen(...)
register_playback(player.pid)
try:
    player.wait()
finally:
    unregister_playback(player.pid)
```

Ensure the Donna `stt-local` module is importable without copying divergent
controller implementations. Update the existing typed `UserPromptSubmit` hook
to invoke:

```sh
uv run --project /Users/pedro/_/AGENTS/DONNA/stt-local \
  python /Users/pedro/_/AGENTS/DONNA/stt-local/playback_control.py cancel
```

Test stale PID records and real playback overlap before relying on this path.

### 8. Cut over only after the synthetic tests pass

Configure the listener MCP process with both values:

```text
DONNA_VOICE_QUEUE_MODE=deliver
DONNA_CLAUDE_SESSION_ID=<same SDK-owned session ID>
```

Restart/reconnect the MCP server so it inherits the environment. Do not start
the listener or microphone without explicit user approval.

Once deliver mode and the bridge are proven healthy, remove the Stop hook as
the primary delivery mechanism. Do not replay or convert historical shadow
rows. Keep a documented rollback to `DONNA_VOICE_QUEUE_MODE=shadow` until live
interruption and crash recovery have passed.

## Operational commands

```sh
# Queue and worker health
uv run --project stt-local python stt-local/voice_queue.py health

# Queue counts and metadata only
uv run --project stt-local python stt-local/voice_queue.py status
uv run --project stt-local python stt-local/voice_queue.py list --limit 20

# Explicit dead-letter replay; shadow rows cannot be replayed
uv run --project stt-local python stt-local/voice_queue.py replay MESSAGE_ID

# Remove only completed rows older than the retention period
uv run --project stt-local python stt-local/voice_queue.py prune \
  --completed-older-than-days 30
```

## Principal files

- `stt-local/voice_queue.py`: durable queue, lifecycle, inspection, retention.
- `stt-local/delivery_worker.py`: socket receiver, leasing loop, sink handling.
- `stt-local/claude_bridge.py`: persistent Agent SDK session bridge.
- `stt-local/playback_control.py`: playback ownership and safe cancellation.
- `stt-local/listen_daemon.py`: listener modes and production wake phrase.
- `stt-local/tests/test_delivery_worker.py`: Phase 2 delivery/crash tests.
- `ops/com.donna.voice-delivery.plist.example`: startup template.

## Safety and rollback

- Never submit `is_shadow=1` rows.
- Never edit transcript text in queued messages.
- Never prune pending, leased, accepted, or dead-letter rows.
- Never run two owners against the same Claude session.
- Do not remove the playback microphone gate until AEC is implemented and
  validated.
- Do not start microphone capture without explicit user authorization.
- Rollback delivery by stopping the worker and setting the listener back to
  `DONNA_VOICE_QUEUE_MODE=shadow`; restart the MCP server afterward.
