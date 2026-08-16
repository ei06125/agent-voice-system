# Progress against PLAN.md

Status as of 2026-08-16, end of second working session. See `PLAN.md` for
the original plan and `README.md` for the pipeline diagram and mechanics.

## Done

**Phase 1 — whisper.cpp**
- Installed via `brew install whisper-cpp` (`whisper-cli` at
  `/opt/homebrew/bin/whisper-cli`, Metal backend on the M2 Max).
- `ggml-base.en.bin` downloaded to `models/` (gitignored — 141MB, not
  committed).
- Verified against synthesized speech (`say` + `afconvert`) before ever
  touching the live mic: exact transcript match, ~0.2-0.3s per call.

**Phase 2/3 — Silero VAD chunker (`stt-local/vad_listen.py`)**
- New `uv` project, Python 3.12, deps: `onnxruntime`, `sounddevice`,
  `soundfile`, `numpy`, `silero-vad-notorch` (the torch-free variant —
  plain `silero-vad` pulls in `torch`/`torchaudio`, ~106MB, avoided).
- Continuous mic capture (512-sample frames @ 16kHz) → `VADIterator` →
  flush WAV clip on trailing silence → `whisper-cli` subprocess → one
  transcript line to stdout per utterance.
- **Bug found and fixed:** `VADIterator`'s `start` event reports a sample
  index that already backs up by `speech_pad_ms`, assuming pre-roll audio
  is available — it isn't, unless you keep it. Added a rolling
  `deque` pre-buffer seeded into the clip on every `start` event. Without
  this, the first word of an utterance was getting clipped.
- **Device selection:** mirrors the active default *output* device — if
  AirPods are on (macOS auto-switches output to them), mic input follows
  them too, since talking into a desk mic while audio plays in your ears
  makes no sense. Falls through `External → Logitech/BRIO → AirPods`
  otherwise (AirPods aren't always connected). Env-overridable via
  `STT_DEVICE_PRIORITY`.
- Live-verified with real speech on both BRIO and AirPods Pro.

**Phase 4 — wired into `voice-listen.sh`**
- Producer swapped from `dg listen --mic --non-interactive` to
  `uv run --project stt-local python vad_listen.py`.
- Dropped the project-local copy of `voice-listen.sh` — it had already
  drifted out of sync with the canonical `~/.claude/scripts/voice-listen.sh`
  once (wake phrase changed in canonical, not here). Canonical copy is
  now the only one; no more "keep both in sync" footgun.
- **Bug found and fixed:** a natural pause right after the wake phrase
  makes Silero flush it as its own clip. The gate was discarding that
  line outright (`[[ -z "$command_text" ]] && continue`) instead of
  opening the debounce window, silently dropping whatever command
  followed on the next line. Fixed: a wake-word match always opens
  debounce now, even with nothing left after stripping.
- Live-verified end to end: wake phrase + a debounced command across a
  pause ("what time is it? bye") came through correctly stitched.

**Version control**
- `DONNA` and `kokoro-local` (`/Users/pedro/.mcp/kokoro-local/`) are now
  both git repos with checkpoint commits, per instruction — MCP tools /
  local projects we own should be under version control.

**Docs**
- Signal-path diagram (mermaid) added to `README.md` and published as a
  standalone artifact. Two stale facts in `README.md` fixed along the way
  (single `voice-listen.sh` location, actual exit phrase).

**Phase 5 — swap TTS to kokoro-local**
- Confirmed standalone (`uv run`), then registered `kokoro-local` as a
  persistent user-scoped MCP server (`claude mcp add -s user kokoro-local
  -- uv run --project /Users/pedro/.mcp/kokoro-local python
  /Users/pedro/.mcp/kokoro-local/mcp_server.py`) so the Kokoro-82M model
  loads once per Claude Code session instead of once per call.
- **Bug found and fixed:** kokoro's own device auto-detect
  (`kokoro/pipeline.py`) only checks `torch.cuda.is_available()`, never
  MPS — silently ran on CPU on this Apple Silicon machine. `mcp_server.py`
  now explicitly requests `device="mps"` with a CPU fallback. Committed
  in `kokoro-local` (`2f157b7`).
- Latency measured directly: `dg speak` 6.83s end-to-end (network +
  generation); `kokoro-local` warm-call generation 0.16s on MPS (was
  0.33s on CPU), after a one-time ~8-9s warm-up (pipeline load + first
  Metal kernel compile) on the first `speak` call of a session.
- New convention recorded as project memory (`tts_convention_kokoro_local`)
  and in `README.md`'s "How it works" step 5 — `mcp__kokoro-local__speak`
  replaces `dg speak "..." -o reply.mp3 --non-interactive && afplay
  reply.mp3`.
- Live-verified: called the MCP tool directly in-session, audio played
  correctly both cold and warm.

**Phase 6 — full live loop via the actual Stop hook**
- **Bug found and fixed before testing:** `voice-stop-hook.sh` only did
  `kill "$LISTENER_PID"` (the `voice-listen.sh` shell) plus a dead
  `pkill -f "dg listen --mic"` pattern left over from the old producer.
  Since `vad_listen.py` runs as the first stage of a pipeline *inside*
  `voice-listen.sh`, killing just the parent shell doesn't reliably kill
  it — it can orphan and keep holding the mic open. Matches the stray
  processes found during Phase 5 testing. Fixed: both the idempotency
  section and post-capture cleanup now `pkill -f "vad_listen.py"` by name.
- Registered `mcp__kokoro-local__speak` in the global permissions allow
  list (`~/.claude/settings.json`) so it doesn't prompt per call.
- Live-verified through the real hook path twice in a row, no manual
  process cleanup needed in between (the test gate): "what time is it?"
  → answered via kokoro-local; "what's the weather in Tokyo?" → answered
  via kokoro-local + a live web search. Both cycles: Tink → mic → VAD →
  whisper → wake gate → debounce → Pop → hook exit 2 → Claude reply →
  kokoro-local speak, cleanly.

**Phase 7 — redesigned scope: always-on listener, decoupled from the hook**
- Original scope (shrink the 3s debounce) got superseded mid-session. Model
  benchmark done first: tried `ggml-tiny.en.bin` against `base.en` on
  synthesized speech — tiny.en saved ~50-160ms/call but dropped a leading
  word once out of 3 tests; kept `base.en` (already fast enough, ~0.2-0.25s,
  not worth the accuracy risk given we already fixed one clipped-first-word
  bug in this pipeline). `models/ggml-tiny.en.bin` removed after the call.
- Live debounce/VAD tuning (`DEBOUNCE=1.2`, `STT_MIN_SILENCE_MS=350`) hit
  repeated silent/garbled tests — turned out to be a bigger, real problem:
  the per-turn-spawned listener only has a narrow window to hear you (after
  Claude's turn ends, before the next begins), and coordinating "say it
  now" over text chat was the root cause of most of this session's failed
  live tests, not the tuning itself.
- **Redesigned instead of tuned further:** always-on daemon
  (`stt-local/listen_daemon.py`), started/stopped only via two new MCP
  tools (`mcp__stt-local__start_listener` / `stop_listener`, registered
  user-scoped like `kokoro-local`) — never auto-starts at boot. Runs
  independent of any Claude turn or hook. Wake phrase now matched
  **anywhere** in an utterance (not anchored to line-start); once heard,
  everything is appended to `~/.claude/scripts/.voice-buffer.txt` until
  drained. `voice-stop-hook.sh` rewritten to just drain that file (exit 2
  if non-empty, else exit 0 immediately) — no longer spawns/kills a
  listener itself. Full mechanism + the three independent controls
  (`.voice-hook-disabled` manual / `.voice-mute` voice-toggleable-output-
  only / "deaf" voice-triggered-one-way-stop) recorded in project memory
  (`donna-always-on-listener`).
- **Bug found and fixed:** unpinned `uv add mcp` in `stt-local` grabbed
  `2.0.0`, which renamed `FastMCP` to `MCPServer` and moved its import
  path — `mcp_server.py` failed to connect with `ModuleNotFoundError`.
  Pinned to `mcp==1.28.1` (matches `kokoro-local`) to fix; see "Open items"
  for the follow-up.
- Built and unit-tested with plain-string inputs first (wake-anywhere
  matching, buffering + drain-detection state machine, mute/unmute/deaf
  dispatch, hook drain/join logic) — all passed, no audio involved
  (deliberately deferred live testing while the user was watching TV and
  didn't want ambient audio risked being captured).
- **Live-verified end to end once the TV was off:** started the daemon via
  `start_listener`, confirmed it buffered real speech across multiple
  Claude turns while unrelated work was happening (proving the core design
  goal — capture is no longer tied to turn timing), confirmed a wake
  phrase mid-buffer doesn't get specially stripped (just appended verbatim
  as continuation text, expected), and confirmed the real Stop hook
  drained the buffer correctly and rewoke Claude with the joined content.
  One bug caught live: a stale `.voice-hook-disabled` flag (left over from
  earlier debounce testing, never cleared) silently suppressed the first
  two test utterances — daemon behaved correctly (flag respected), but a
  reminder that flag state carries across the whole session.
- `voice-listen.sh` is now dead code in this flow (nothing calls it
  anymore) but left in place, not deleted, until fully confident nothing
  still depends on it.

**Phase 7 follow-ups: OMG-prefixed control keywords, ring buffers, blocking-poll hook**
- Live-tested "unmute" and found whisper genuinely couldn't transcribe it
  reliably ("and mute", "Amute", plain "mute" — never cleanly, across
  repeated attempts), each miss re-triggering mute instead of clearing it.
  Replaced all three control keywords with the OMG-prefixed series ("omg
  mute" / "omg speak" / "omg go deaf", reusing the same `(oh my god|omg|o
  m g)` alternation as the production wake phrase) — live-verified all
  three working correctly afterward, including mute correctly suppressing
  `kokoro-local speak` (text-only reply) and a self-feedback case (my own
  spoken reply re-triggering unmute harmlessly, since it's idempotent).
- **Bug found and fixed:** buffer file wasn't cleared on daemon
  start/restart — stale undrained content from before a restart could
  resurface on the next hook drain. `listen_daemon.py` now removes the
  buffer file at the top of `main()`.
- **Added, per explicit concern about unbounded memory growth:** both ring
  buffers now bounded. `BufferWriter` keeps only the most recent
  `STT_MAX_BUFFER_LINES` (default 200) utterances, atomically rewriting
  the file on each append; `clip_queue` (raw audio awaiting transcription)
  capped at `STT_MAX_QUEUED_CLIPS` (default 10), dropping the newest clip
  with a warning rather than blocking the real-time audio callback thread.
  A background watchdog thread also expires an entire undrained capture
  after `STT_MAX_BUFFER_AGE_SECONDS` (default 300) regardless of line
  count, checked every `WATCHDOG_INTERVAL_SECONDS` (10) — protects against
  a capture nobody ever drains, not just one that grows too large.
- **Hit live: Claude mid-turn for an extended stretch (many chained tool
  calls, no natural Stop event) meant captured speech just sat in the
  buffer with nothing surfacing it**, even though the daemon buffered it
  correctly — confirmed directly, then fixed. Considered `/loop` (periodic
  re-invocation) first and rejected it: pull-based, spends a full Claude
  turn checking even when the buffer is empty. Rewrote `voice-stop-hook.sh`
  instead to poll the buffer file for up to an hour per Stop event (was:
  check once, return immediately) — push-based and free while idle, since
  the watching happens in a detached bash process, not a Claude turn.
  Added a PID-file lock so frequent Stop events during normal chat don't
  spawn overlapping pollers racing on the same buffer file. Verified in
  isolation (dedup, delivery, cleanup all confirmed with a self-contained
  test harness) — **confirmed live: a notification landed attached to an
  in-progress tool-call turn**, not only at a clean turn boundary, so the
  poller can interrupt actively-generating work, not just idle time.
- **Bug found and fixed: the 5-minute buffer age-expiry (added earlier
  this same phase) silently destroyed a real, undelivered monologue.**
  Root cause: the wake phrase triggered buffering while Claude was still
  deep in one long uninterrupted turn (building the poller itself, plus
  ring buffers, plus committing) — no Stop event fired for well over 5
  minutes, so by the time a poller finally spawned and started watching,
  the watchdog had already expired and cleared the whole capture. The fix
  for unbounded memory growth ended up eating real content, in exactly
  the long-turn scenario that motivated building the poller in the first
  place. Diagnosed live: verified the poller itself was healthy (a
  manually-injected test line delivered correctly within seconds) and the
  buffer was already empty *before* that injection — pointing straight at
  the watchdog. Raised `MAX_BUFFER_AGE_SECONDS` default from 300 to 1260
  (21 minutes, user's call) — the line-count/queue caps already bound
  worst-case memory, so age-expiry only needs to catch genuinely abandoned
  captures now, not routine long turns. Confirmed nothing was actually
  lost from a record-keeping standpoint — `listen_daemon.py` logs every
  transcribed utterance regardless of buffering/delivery state, so the
  full raw content was recoverable by reading `.voice-daemon.log` directly
  even though the automatic notification never fired.

**Phase 7 follow-up: lint/format pass + real-time capture safety (DeepResearchVAD.md Phase 1)**
- Ran `black` + `ruff` (via `uvx`, no dev deps added) on all three
  `stt-local` Python files. Auto-fixed 5 findings, manually fixed the rest:
  string defaults for `os.environ.get()` calls (`PLW1508`), explicit
  `check=False` on `subprocess.run` (`PLW1510`), noqa'd the intentional
  broad `except Exception` in `find_device()` (device-query failures are
  backend-specific; deliberate fallback to the priority list).
- User supplied `DeepResearchVAD.md`, a research doc on local VAD/tooling
  scoped to this exact pipeline. Implemented its Phase 1 (make capture
  real-time safe):
  - **Bug the doc caught that the earlier ring-buffer pass missed:** VAD
    inference (`vad(chunk)`) ran directly inside the PortAudio audio
    callback in both `listen_daemon.py` and `vad_listen.py` — violates
    PortAudio's own callback contract (must avoid allocation, blocking,
    unpredictable-duration work), real risk of audio dropouts under load.
    Fixed in both files: extracted a module-level `run_vad_worker()`
    (VAD inference + segmentation, unit-testable with a mocked VAD
    callable) that runs on its own thread, fed by a new bounded
    `frame_queue` (`STT_MAX_QUEUED_FRAMES`, default 50) that the callback
    only does a non-blocking copy-and-enqueue into — no inference, no
    concatenation, no I/O on the audio thread anymore.
  - **Also fixed: `speech_buffer` (raw audio during one continuous
    in-speech run) had no size cap.** If VAD ever misclassifies sustained
    non-speech (music, noise, feedback) as speech, it never fires an "end"
    event and the buffer grows unbounded (~230MB/hour at 16kHz float32
    mono). Added `MAX_UTTERANCE_SECONDS` (default 30) — force-flushes
    whatever's accumulated and calls `vad.reset_states()` to keep VAD's
    internal state consistent with the forced cut.
  - Verified with synthetic mocked-VAD unit tests (both files): normal
    VAD-end flush still produces the correct clip with no spurious
    `reset_states()` call; force-flush at the duration cap produces the
    correct clip and does call `reset_states()`. Then live-verified:
    daemon restarted cleanly against the real mic/model, existing
    keyword-detection tests still pass unchanged, and it's still
    transcribing correctly.
- Remaining `DeepResearchVAD.md` phases (persistent whisper.cpp process,
  building a real tuning corpus, semantic endpointing, model bakeoff)
  moved to `futurework.md` — bigger scoped efforts, not done today.

## Open items

- **Wake phrase is in test mode.** `~/.claude/scripts/voice-listen.sh` and
  `stt-local/listen_daemon.py` both currently match `"testing 1, 2, 3"`
  instead of the production phrase, swapped for reliable cued testing over
  text chat (hard to sync speaking a specific phrase with someone reading
  a chat message telling them to). The production regex is commented
  directly above it in both files — **revert before relying on this for
  real use.**
- **`mcp` package pinned to `1.28.1` in both `kokoro-local` and
  `stt-local`.** `mcp` 2.0.0 renamed/restructured `FastMCP`
  (`mcp.server.fastmcp.FastMCP`) to `MCPServer`
  (`mcp.server.mcpserver.server.MCPServer`, imported as `mcp.server.MCPServer`).
  `MCPServer` looks API-compatible with `FastMCP` (same `.tool()`
  decorator, `.run()` method) but untested — revisit bumping both projects
  off the 1.x pin together at some point rather than drifting further
  behind.
- **Mute/deaf keywords not yet live-tested.** Unit-tested only so far (see
  Phase 7 above). Still need real-speech verification that "mute"/"unmute"
  correctly gate `mcp__kokoro-local__speak` (the check itself is a
  behavioral convention in memory, not enforced by code) and that "deaf"
  cleanly stops the daemon with `start_listener` able to bring it back.
- Biggest lesson from this session's live-testing: most "silent" test
  failures were either coordination lag (the old per-turn-spawned listener
  model, now superseded by Phase 7's redesign) or stale flag-file state
  carried over from earlier tests — not pipeline bugs. Worth checking flag
  state first before assuming a silent test means something's broken.

## Not started

Nothing currently — Phase 7 was the last item from `PLAN.md`'s original
7-phase breakdown, redesigned but complete. Open items above are the
remaining loose ends, not new phases.
