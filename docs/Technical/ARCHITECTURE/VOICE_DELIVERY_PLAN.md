# Donna Voice Delivery Plan

Status: Phases 1-3 implemented on 2026-08-16. Cutover is opt-in because it
changes session ownership from stock Claude Code to the Agent SDK wrapper.

## Objective

Deliver each finalized, wake-phrase-authorized transcript to Claude within about one second of VAD end-of-speech, including while Claude is in a long-running turn. Preserve commands across crashes, avoid duplicate delivery, and keep Kokoro playback interruptible.

The delivery target is prompt acceptance by the Claude session, not completion of Claude's response.

## Finding: the Stop hook is the bottleneck

The microphone, Silero VAD, and Whisper path already runs independently of Claude:

- `stt-local/listen_daemon.py` continuously captures 16 kHz microphone audio.
- Silero identifies speech start and end.
- `whisper-cli` transcribes each completed utterance.
- Wake-phrase-authorized text is written to `~/.claude/scripts/.voice-buffer.txt`.

The blocking dependency is downstream. Claude only starts `voice-stop-hook.sh` on a `Stop` lifecycle event. Claude Code documents `Stop` as running when Claude finishes responding, once per turn. A long response therefore creates a period in which no new Stop hook can be started. An already-running detached poller can sometimes help, but creating or replacing that poller is still lifecycle-bound. This cannot provide a reliable one-second delivery guarantee. [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)

The current path has additional delivery risks:

- The Stop hook polls every two seconds, already slower than the target.
- It truncates the buffer before Claude acknowledges receipt. A process failure in between can drop the command.
- The listener treats the file as a mutable snapshot/ring buffer rather than a durable queue.
- Listener startup removes the buffer, which can erase undelivered speech.
- A check-then-write PID file is not a transactional single-consumer lock.
- Identical text has no stable message identity, so retries cannot be distinguished from intentional repetition.

[`PROGRESS.md`](../../../PROGRESS.md) records an observed case where `asyncRewake` appeared to attach speech during active tool work. Keep that as useful evidence, but do not treat it as a supported general injection contract. The documented guarantee is that an `asyncRewake` command hook exiting with code 2 can wake Claude immediately when it is idle; asynchronous hook output otherwise arrives on a later turn, and separate hook executions are not deduplicated. [Claude Code hooks reference](https://code.claude.com/docs/en/hooks)

Cron must remain only an optional health check or emergency recovery mechanism. It must not be the primary delivery path.

## Recommended architecture

Use two local primitives:

1. SQLite as the durable source-of-truth queue.
2. A Unix-domain socket as a low-latency notification channel.

Use an always-running delivery bridge that owns the Claude conversation through the Claude Agent SDK in streaming mode. The bridge consumes queued messages and submits them to the same persistent SDK session. For an urgent voice command, it interrupts the current SDK turn, drains the interrupted response as required by the SDK, then submits the command.

```text
microphone -> Silero VAD -> Whisper -> command assembler
                                          |
                                          | one SQLite transaction
                                          v
                                  durable message queue
                                          |
                         Unix-socket wake  |  recovery scan
                                          v
                                  delivery bridge
                                  /             \
                         cancel Kokoro      Agent SDK session
                                                |
                                           Claude response
```

SQLite carries data and delivery state. The socket carries no authoritative data; it only tells the worker to scan immediately. If a socket notification is missed, the row remains in SQLite and is found by the worker's short timed wait or startup scan.

This design does not wait for a Claude Code Stop event.

## Why the bridge must own the Claude session

The stock interactive Claude Code process does not expose a documented local IPC endpoint for injecting an arbitrary new user prompt into an already-running turn.

Feasible approaches and their limits:

| Approach | During an active turn | Reliability | Decision |
|---|---:|---:|---|
| Agent SDK streaming session | Yes. Supports persistent exchanges and interruption. | Supported programmatic API | Primary recommendation |
| Stop hook with `asyncRewake` | Only after a hook instance has been launched; behavior during active work is not a general prompt-injection contract. | Lifecycle-bound | Compatibility fallback only |
| Normal async hook output | No immediate prompt guarantee; output is applied on a later conversation turn. | Supported, but wrong semantics | Do not use as primary path |
| `UserPromptSubmit` hook | No. It augments a prompt that has already been submitted. | Supported | Keep for typed-prompt TTS cancellation |
| `FileChanged` hook | No supported arbitrary user-message injection. | Notification/context behavior only | Not a delivery transport |
| MCP server | Claude must choose to call it; the server cannot push a user turn into Claude. | Pull-based | Not a delivery transport |
| Second `claude --resume` process | Starts another process against the transcript; it is not injection into the running process. Concurrent terminals can interleave the same session history. | Unsafe for one live owner | Reject |
| PTY/tmux/AppleScript keystrokes | Can imitate typing, but focus, permission prompts, terminal modes, and partial input make acknowledgement ambiguous. | Brittle and unsupported | Emergency prototype only |
| Minute cron | Cannot meet latency target. | Slow | Health check only |

Claude documents that resumed sessions used concurrently can interleave messages in one transcript. That is not a safe single-session transport. [Claude Code session management](https://code.claude.com/docs/en/sessions) [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)

The Agent SDK provides the required semantics: a persistent session, streaming input, multiple exchanges, and interruption. In Python, `ClaudeSDKClient` supports `query()` and `interrupt()` in streaming mode. After interruption, the interrupted response must be drained before another query is processed. [Agent SDK streaming input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode) [Python Agent SDK reference](https://code.claude.com/docs/en/agent-sdk/python) [Agent loop](https://code.claude.com/docs/en/agent-sdk/agent-loop)

Consequence: reliable mid-turn voice delivery requires launching Donna's Claude session through a small local SDK-backed wrapper/bridge. It cannot be guaranteed for an arbitrary pre-existing stock `claude` terminal process. If preserving the exact stock terminal UI is mandatory, the project must accept Stop-bound delivery as a limitation or use unsupported PTY automation.

## Message definition and command assembly

Do not enqueue partial Whisper hypotheses. Enqueue only a finalized command.

Recommended command boundaries:

- Wake phrase and command in one utterance: remove the wake phrase and enqueue the remainder at that utterance's VAD end.
- Wake phrase alone: enter an `armed` state and enqueue the next finalized utterance.
- Empty remainder: do not enqueue.
- A later command requires another wake phrase unless a deliberate multi-command conversation mode is enabled.
- If short-pause coalescing is desired, bound it to 300 ms so it cannot violate the one-second target.

Every source event receives a producer-generated immutable ID such as UUIDv7 plus an utterance sequence. Retrying the same source event reuses the same ID. Speaking identical words twice produces two different IDs and must deliver twice.

## Durable queue design

Suggested local location: a configured Donna state directory under the user's Claude configuration directory. The implementation must resolve this from configuration rather than hardcode a user-specific path.

Suggested SQLite properties:

- WAL journal mode.
- `synchronous=FULL` initially; benchmark `NORMAL` only after crash tests.
- Busy timeout.
- Parent directory mode `0700`; database and socket accessible only to the user.
- Schema version recorded with migrations.

Minimum logical fields:

| Field | Purpose |
|---|---|
| `message_id` | Producer-stable unique ID; primary key |
| `capture_id` / `utterance_seq` | Prevent duplicate producer insertion |
| `created_at` / `vad_ended_at` / `finalized_at` | Ordering and latency measurement |
| `text` | Immutable finalized command |
| `state` | `pending`, `leased`, `accepted`, `completed`, or `dead_letter` |
| `lease_owner` / `lease_until` | Crash-safe consumer ownership |
| `attempt_count` / `last_error` | Retry and diagnosis |
| `target_session` | Prevent delivery to the wrong conversation |
| `accepted_at` / `completed_at` | Delivery and response lifecycle metrics |

Do not use a text hash as the only uniqueness key. Two intentional commands can have identical text.

### Atomic producer write

For each finalized command:

1. Begin an immediate SQLite transaction.
2. Insert one immutable row as `pending`, keyed by `message_id` and the producer event identity.
3. Commit.
4. Only after commit, send a one-byte/datagram wake notification to the Unix socket.

If the listener crashes before commit, no message exists and no partial message is visible. If it crashes after commit but before notification, the worker's timed scan finds the durable row. A repeated insert with the same event identity becomes a no-op through a unique constraint.

The listener must never clear the durable queue on startup.

### Atomic consumer lease

Only one bridge should own a target Claude session. Still make queue consumption safe under accidental multiple workers:

1. In one immediate transaction, reclaim expired leases.
2. Select the oldest eligible `pending` message for the target session.
3. Atomically update it to `leased` with a random worker ID and short lease deadline, using `UPDATE ... RETURNING` or an equivalent guarded update.
4. Commit before contacting Claude.
5. Renew the lease while interruption or submission is in progress.

A competing worker cannot lease the same row. On worker death, the lease expires and the message becomes eligible for retry.

### Delivery acknowledgement and exactly-once semantics

Strict exactly-once behavior across SQLite and an external Claude process is not mathematically possible without a transactional acknowledgement or idempotency contract from the receiver. A crash can occur after Claude accepts a prompt but before SQLite records the acknowledgement.

Implement effectively-once delivery:

- Prefix or attach a machine-readable `voice-command-id` to every submitted SDK user message.
- Record `accepted` only after the SDK stream confirms the corresponding user message was accepted/echoed by the owned session.
- Maintain a durable accepted-ID ledger.
- Before retrying an ambiguous lease, check the ledger and, where available, the owned session event history for that ID.
- If the ID was accepted, mark it `accepted` without resubmission.
- If it was not accepted, resubmit with the same ID.
- Mark `completed` separately when the corresponding SDK result finishes. Delivery success must not depend on completion.
- After a bounded number of failures, move the row to `dead_letter` and surface a visible local alert. Never silently delete it.

This gives at-least-once durable transport plus receiver-side deduplication. It prevents ordinary duplicates while favoring no dropped commands in the irreducible crash window.

## Delivery policy during a running Claude turn

Default wake-phrase commands should be preemptive:

1. Commit the command to SQLite.
2. Cancel active TTS playback.
3. Notify the delivery bridge.
4. If Claude is generating, call the SDK interruption operation.
5. Drain the interrupted response to its terminal result as required by the SDK.
6. Submit the voice command with its message ID.
7. Mark it accepted when confirmed by the stream.

Provide a configuration option for non-preemptive mode. In that mode the row remains ordered and is delivered immediately after the current result, but it cannot promise one-second prompt acceptance during a long turn.

The bridge must serialize prompts for a session. Multiple queued commands retain creation order unless the user explicitly chooses a priority policy.

## TTS interruption and barge-in

Keep TTS cancellation independent from transcript delivery.

Current behavior has two separate mechanisms:

- `voice-interrupt-hook.sh` terminates the recorded playback PID on `UserPromptSubmit`; retain this for typed prompts and stock-CLI fallback.
- `listen_daemon.py` discards microphone frames while `.voice-playback` exists, plus a hangover. This avoids self-transcription but also prevents true spoken barge-in during Kokoro playback.

For phase one, cancel Kokoro as soon as a finalized voice command is committed and before SDK submission. The TTS process should have one owned cancellation endpoint or PID record with atomic lifecycle handling.

For the desired "as soon as the user begins a new prompt" behavior:

- Typed prompt: continue cancelling on `UserPromptSubmit`.
- Voice prompt when playback is not active: send a `speech_started` event from the VAD path to the TTS controller and cancel immediately.
- Voice prompt during speaker playback: true reliable detection requires full-duplex echo cancellation or an equivalent echo-aware capture path. The current playback gate deliberately prevents it. Do not claim full spoken barge-in until AEC is implemented and tested.

The delivery queue solves command transport. It does not by itself solve acoustic echo or full-duplex VAD.

## Latency budget and observability

Record monotonic timestamps at:

- VAD end-of-speech.
- Whisper final transcript.
- queue commit.
- bridge lease.
- TTS cancellation request and process exit.
- SDK interrupt request and terminal interrupted result.
- SDK user-message acceptance.
- Claude result completion.

Initial service objectives:

- VAD end to queue commit: p95 at or below 500 ms on the target machine.
- Queue commit to bridge lease: p95 at or below 100 ms.
- VAD end to SDK prompt acceptance: p95 at or below 1,000 ms.
- TTS cancellation after detected speech start: p95 at or below 150 ms when an echo-aware speech-start signal is available.

SDK interruption/drain or network delay can exceed the target. Report those separately instead of hiding them inside an average. Log message IDs and state transitions, never microphone audio or command text by default.

## Phased implementation checklist

### Phase 0: baseline and contracts

- [ ] Confirm the exact supported Python version and Claude Agent SDK version.
- [ ] Capture current VAD-end-to-Whisper-final latency over at least 100 utterances.
- [ ] Document the command-boundary rules and interruption policy.
- [ ] Add a feature flag for the new queue path; keep current behavior available during validation.
- [ ] Define the state directory, permissions, retention, and dead-letter policy.
- [ ] Decide whether the SDK wrapper replaces the stock CLI for primary use. This is required for guaranteed mid-turn injection.

Exit criterion: measured baseline and an approved session-ownership decision.

### Phase 1: durable queue in shadow mode

- [x] Add a small queue module with schema initialization and migrations.
- [x] Generate a stable event ID when a command is finalized.
- [x] Insert finalized commands in one SQLite transaction.
- [x] Add producer uniqueness constraints for retry safety.
- [x] Add the Unix-socket notification after commit.
- [x] Add a queue inspector/status command.
- [x] Do not clear pending rows on listener startup.
- [x] In shadow mode, continue the existing delivery path while verifying that queue rows match commands. Do not let both paths submit to Claude.
- [x] Unit-test atomic insert, repeat insert, ordering, permissions, and startup recovery.

Shadow is now the safe default. `deliver` is an explicit cutover that also
requires `DONNA_CLAUDE_SESSION_ID`; it creates new deliverable rows and never
converts old shadow rows. Inspect the queue with `python stt-local/voice_queue.py
status` or `list`. Command text is omitted unless `--include-text` is passed.

Exit criterion: every authorized finalized command appears exactly once in the durable queue under normal operation and injected producer failures.

### Phase 2: delivery worker with a test sink

- [x] Implement atomic leasing, lease renewal, expired-lease recovery, acknowledgement, and dead letters.
- [x] Wake from the Unix socket and also scan on startup and a 50-250 ms timed wait.
- [x] Enforce one ordered consumer per target session.
- [x] Add accepted-ID deduplication.
- [x] Add structured latency and state-transition metrics.
- [x] Use a deterministic fake sink to run crash-window tests before connecting Claude.

Exit criterion: no lost rows, no double sink calls after acknowledged delivery, and correct recovery for every injected crash point.

### Phase 3: Agent SDK session bridge

- [x] Build a minimal Python launcher that owns one persistent `ClaudeSDKClient` streaming session.
- [ ] Preserve project working directory, permissions, tools, settings, and session metadata expected by Donna.
- [x] Submit queued prompts with `voice-command-id` metadata.
- [x] Implement preemptive interruption, required response draining, and serialized resubmission.
- [x] Record accepted and completed states from SDK stream events.
- [x] Refuse delivery if `target_session` does not match the bridge's session.
- [x] Keep rows pending while the bridge or network is unavailable.
- [ ] Expose clear local status: session ID, queue depth, active message, last error, and latency.

Exit criterion: a command spoken during a deliberately long Claude turn is accepted by the same owned session without a Stop event.

### Phase 4: TTS integration and cutover

- [ ] Give Kokoro playback one explicit cancellation controller.
- [ ] Cancel playback on typed `UserPromptSubmit` and before voice-command SDK submission.
- [ ] Cancel on VAD speech-start where playback gating does not suppress the signal.
- [ ] Make the SQLite/SDK bridge the primary path.
- [ ] Disable the Stop hook as a delivery dependency.
- [ ] Retain a manual emergency drain command and optional health check.
- [ ] Remove the legacy mutable buffer only after rollback testing.
- [ ] Update `../../../README.md`, `../../../PROGRESS.md`, operational runbooks, and service definitions.

Exit criterion: the legacy Stop path can be disabled without changing delivery latency or reliability.

### Phase 5: optional full-duplex barge-in

- [ ] Select and integrate acoustic echo cancellation before VAD/Whisper.
- [ ] Replace the `.voice-playback` microphone discard gate with echo-aware capture.
- [ ] Validate speaker playback, headphones, multiple microphones, room echo, and false interruption rates.
- [ ] Cancel TTS on genuine near-end speech start, before transcription completes.

Exit criterion: spoken interruption during Kokoro playback works without TTS self-transcription or frequent false cancellation.

## Test plan

Run all tests with message IDs and monotonic timing enabled. Assert database state as well as visible Claude behavior.

### Short pause

Scenario: speak the wake phrase, pause briefly, then speak one command.

Assertions:

- Wake-only utterance arms the command assembler but creates no empty row.
- The next finalized utterance creates one row and one SDK user message.
- A 100-300 ms within-command pause follows the configured coalescing rule.
- VAD end to SDK acceptance is at or below one second at p95.
- Repeating identical words as a new utterance creates a different ID and delivers again.

### Long Claude turn

Scenario: start a Claude task that runs or streams for 30-120 seconds, then speak a wake-authorized command without waiting for Stop.

Assertions:

- Queue commit does not depend on a hook event.
- Preemptive mode interrupts and drains the active SDK response.
- The new prompt is accepted by the same session near the one-second target.
- The row reaches `accepted`; the interrupted result and new result remain attributable to the correct IDs.
- Non-preemptive mode preserves order and explicitly reports that it does not meet the active-turn latency objective.

### Overlapping speech and TTS

Scenario A: start long Kokoro playback, then inject a finalized queue message.

- Playback terminates before SDK prompt submission.
- The TTS PID/controller state is cleared atomically.
- A stale PID cannot terminate an unrelated reused process.

Scenario B: begin a typed prompt during playback.

- `UserPromptSubmit` still cancels playback.

Scenario C: speak during playback.

- Before AEC, verify and document that the playback gate blocks true barge-in; test the delivery side with a synthetic finalized message.
- After AEC, near-end speech start cancels playback within the target and does not transcribe Kokoro output as user speech.

### Crash recovery

Inject termination at each boundary:

- Before queue transaction commit: no partial row.
- After commit and before socket notification: worker timed/startup scan delivers the row.
- After lease and before SDK submission: lease expires and row retries.
- During SDK interruption/drain: lease renews or safely retries.
- After SDK acceptance and before SQLite acknowledgement: accepted-ID reconciliation prevents a second user message.
- After acknowledgement and before completion: row remains `accepted` and completion can be reconciled.
- During listener, worker, or bridge restart: pending order is preserved and startup never clears messages.

### Duplicate prevention

- Insert the same producer event ID twice: one row.
- Race two worker processes: one lease and one sink submission.
- Retry an expired lease before acceptance: one eventual accepted message.
- Retry after recorded acceptance: no second SDK submission.
- Simulate the ambiguous acceptance/ack crash window: reconcile by ID.
- Submit identical text under two event IDs: two intentional deliveries.

### Additional operational tests

- Queue database locked briefly: listener waits within its busy timeout or reports a visible failure without losing the in-memory finalized event.
- SDK unavailable: rows remain pending with backoff; recovery preserves order.
- Wrong target session: worker refuses delivery.
- Poison message: bounded retries, then `dead_letter` with inspection and replay tools.
- Socket missing or recreated: database scan still delivers.
- Machine sleep/wake: leases use timestamps safely and rows recover.
- Queue retention: only completed rows older than the configured retention period are pruned; pending and dead-letter rows are never silently removed.

## Acceptance criteria

- No primary-path dependency on `Stop`, cron, terminal focus, or Claude choosing to call an MCP tool.
- Durable command exists before any delivery attempt.
- No truncate-before-ack behavior.
- One stable message ID across retries.
- Effectively-once delivery with explicit handling of the unavoidable external-ack crash window.
- Commands spoken during a long Claude turn can enter the same owned session.
- VAD end to SDK prompt acceptance meets the one-second p95 target on the target machine, or outliers are attributed to measured Whisper, interruption/drain, or network delay.
- TTS cancellation remains independent and testable.
- Full spoken barge-in is not declared complete until echo-aware capture replaces playback-time microphone suppression.

## Implementation decision

Implement Phase 1 first: a SQLite durable queue and notification socket in shadow mode, with no change to Claude delivery behavior. Then validate producer correctness and crash safety before adding the Agent SDK bridge.

The critical product decision before Phase 3 is whether Donna may launch and own Claude through an Agent SDK wrapper. If yes, the one-second mid-turn goal is technically feasible. If no, stock Claude Code's lack of a supported prompt-injection IPC remains the limiting constraint.
