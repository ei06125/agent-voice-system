# Progress against PLAN.md

Status as of 2026-08-15, end of first working session. See `PLAN.md` for
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

## Open items

- **Wake phrase is in test mode.** `~/.claude/scripts/voice-listen.sh`
  currently matches `"testing 1, 2, 3"` instead of the production phrase,
  swapped for reliable cued testing over text chat (hard to sync speaking
  a specific phrase with someone reading a chat message telling them to).
  The production regex is commented directly above it in the script —
  **revert before relying on this for real use.**
- Biggest lesson from today's live-testing: most "silent" test failures
  were coordination lag (user speaks while dictating a chat message,
  which finishes *before* the next listening window opens), not pipeline
  bugs — confirmed by an objective raw signal-level check
  (`sounddevice` max/mean amplitude) that caught real speech cleanly once
  timing actually lined up. Two real bugs surfaced this way regardless
  (above); worth remembering the pipeline itself is solid before
  assuming a silent test means it's broken.

## Not started

**Phase 5 — swap TTS to kokoro-local**
- Confirm standalone: `uv run --project /Users/pedro/.mcp/kokoro-local
  python -c "from mcp_server import speak; speak('test')"`.
- Record the new convention as project memory (the `dg speak` convention
  was never in a config file — it was a per-turn behavioral pattern, not
  something a file edit replaces).
- Side-by-side latency check against `dg speak` (unmeasured, per
  `PLAN.md`'s open questions).

**Phase 6 — full live loop via the actual Stop hook**
- Not yet tested through the real hook path (`~/.claude/settings.json` →
  `voice-stop-hook.sh` → FIFO). All testing so far has been direct
  invocation of `voice-listen.sh`, bypassing the hook.

**Phase 7 (optional, deferred)**
- Revisit whether the 3s debounce can shrink now that Silero does real
  endpointing. Only after Phase 6 passes.
