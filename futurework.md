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
  Still unverified: whether `exit 2` can interrupt an actively-generating
  turn, or only deliver once the current turn naturally ends — no way to
  test that boundary from inside a single conversation.
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
