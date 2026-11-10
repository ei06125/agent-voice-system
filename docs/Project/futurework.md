# Future work

Deferred items — not started, not scheduled. Distinct from `PROGRESS.md`'s
"Open items" (loose ends on work already in flight for *this* migration).
Plain list for now; may move to a proper Themes/Initiatives/Epics/Stories/
Tasks/Subtasks breakdown once the bootstrap tool below exists.

- **"Oh My God" series MCP tool — workspace doc bootstrap.** A tool,
  following the same naming convention as the voice control keywords
  ("omg mute" / "omg speak" / "omg go deaf"), that scaffolds the
  Jira-as-Code work-item tracking convention (Themes → Initiatives →
  Epics → Stories → Tasks → Subtasks, plus the Product/Project/Technical/
  Governance doc layout) into any workspace on demand — modeled on the
  structure already in place at `/Users/pedro/Desktop/Heartless/docs/`.
  Call it once per new project instead of hand-copying the folder
  structure each time.
- **~~Voice daemon: no periodic re-check during a long single turn.~~**
  Resolved — see `PROGRESS.md`. `voice-stop-hook.sh` now polls the buffer
  for up to an hour per Stop event instead of checking once and
  returning, with a PID-file lock so only one poller runs at a time.
  Confirmed live that delivery can interrupt an actively-generating turn,
  not just land at a clean boundary.
- **~~Buffer age-expiry destroyed real content during a long turn.~~**
  Resolved — see `PROGRESS.md`. Raised `MAX_BUFFER_AGE_SECONDS` from 300
  to 1260 (21 min) after it silently expired a real monologue while a
  poller hadn't spawned yet.
- **Revert the wake phrase from test mode.** `stt-local/listen_daemon.py`
  and `~/.claude/scripts/voice-listen.sh` (now dead code, see below) both
  match `"testing 1, 2, 3"` instead of the production phrase. Revert once
  confident the wake word itself is the only remaining unknown — the
  production regex is already commented directly above it in both files.
- **Bump `mcp` off the `1.28.1` pin.** Both `kokoro-local` and `stt-local`
  are pinned there to dodge `mcp` 2.0.0's `FastMCP` → `MCPServer` rename.
  `MCPServer` looks API-compatible (same `.tool()`/`.run()`) but untested —
  revisit bumping both together.
- **Delete `voice-listen.sh` once fully confident nothing depends on it.**
  Dead code since the Phase 7 always-on-daemon redesign — the Stop hook no
  longer spawns it. Left in place rather than deleted mid-migration.
- **Full `README.md` "How it works" pass.** Some of the mechanics
  description still reflects the pre-migration `dg listen`/`dg speak`
  design in spots, deferred pending confidence that the local pipeline is
  fully settled.
- **`docs/Technical/ARCHITECTURE/DeepResearchVAD.md` — remaining phases (2-5).** Research doc on local
  VAD/tooling. Phase 1 (real-time-safe capture) is done — see
  `PROGRESS.md`. Still open: Phase 2 (persistent whisper.cpp
  process/context instead of a cold-started `subprocess.run` per
  utterance — likely the biggest remaining latency win), Phase 3 (build a
  recorded/annotated test corpus, tune threshold/padding/endpoint policy
  against it rather than by feel), Phase 4 (semantic endpointing layer,
  e.g. Pipecat Smart Turn, only if fixed-silence endpointing still proves
  insufficient after tuning), Phase 5 (benchmark FireRedVAD/TEN VAD/WebRTC
  against Silero on the frozen corpus — only swap on a measured win).
  ~~VAD fundamentally can't solve the self-feedback problem~~ — Mode A
  (half-duplex gating, see `docs/Technical/ARCHITECTURE/DeepResearchEchoCancellation.md` below)
  resolved the practical impact of this; the underlying VAD limitation
  itself is still true, just no longer a live problem.
- **`docs/Technical/ARCHITECTURE/DeepResearchEchoCancellation.md` — remaining modes (B/C) for real
  barge-in.** Research doc on acoustic echo cancellation. Mode A
  (half-duplex: discard capture during TTS playback + hangover) is done —
  see `PROGRESS.md`. Still open, bigger scope: Mode B (Apple
  `AVAudioEngine` + `setVoiceProcessingEnabled(true)`, native full-duplex,
  the doc's top recommendation — needs a small Swift helper since
  `afplay`/`sounddevice` don't use `AVAudioEngine` today) or Mode C
  (WebRTC AEC3 via a Python/C++ bridge, more portable but bigger native
  dependency). Either would enable true barge-in (interrupt Claude mid-
  reply by speaking) instead of today's strict turn-taking. Also
  documented: no evidence Claude/Anthropic watermarks or fingerprints
  TTS/text output, so that's not a viable self-audio-detection shortcut
  even if it seemed appealing.
