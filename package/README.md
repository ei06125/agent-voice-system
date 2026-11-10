# Running DONNA on a new machine

Everything needed to bring the voice pipeline up on a fresh macOS machine,
beyond what's already in the main repo. Read `../CLAUDE.md` first for how
the system behaves once it's running; this is just the install path.

## Prerequisites

- macOS (the whole pipeline is macOS-specific: `afplay`, `sounddevice`
  CoreAudio backend, AirPods echo-cancellation behavior).
- [Homebrew](https://brew.sh).
- [`uv`](https://docs.astral.sh/uv/) for Python dependency management.
- `whisper-cli`: `brew install whisper-cpp` (must resolve to
  `/opt/homebrew/bin/whisper-cli` - `src/donna/speech_to_text/listen_daemon.py`'s
  `WHISPER_BIN` constant is hardcoded to that path; edit it if your
  Homebrew prefix differs, e.g. Intel Macs at `/usr/local`).
- `gitleaks`: `brew install gitleaks` (required by `.githooks/pre-commit`,
  which fails closed - aborts every commit - if it's missing).

## 1. Clone and install the main project

```sh
git clone git@github.com:ei06125/agent-voice-system.git DONNA
cd DONNA
git config core.hooksPath .githooks   # activates the test + gitleaks pre-commit hook
uv sync
```

## 2. Download the Whisper model

`models/` is gitignored (large binaries) - the daemon expects
`models/ggml-base.en.bin` (whisper.cpp base English model, ~148MB) at the
repository root:

```sh
mkdir -p models
curl -L -o models/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
```

## 3. Install the unified local stack

Kokoro TTS, STT, orchestration, and MCP tools are all in the root `src/donna/`
package. The `uv sync` command above installs the complete local stack.

## 4. Register both MCP servers

Merge `package/claude-config/mcp-servers.snippet.json`'s `mcpServers`
entries into your `~/.claude.json` (top-level key, sibling to any
existing `mcpServers` entries) - replace `/absolute/path/to/...` with
your actual paths. Preserve any other MCP servers already registered
there; don't overwrite the whole file.

## 5. Install the playback-interrupt hook

```sh
cp package/claude-config/voice-interrupt-hook.sh ~/.claude/scripts/
chmod +x ~/.claude/scripts/voice-interrupt-hook.sh
```

Edit the copied script: it hardcodes `~/_/AGENTS/DONNA/stt-local` as the
project path for `playback_control.py cancel` - change both occurrences
to wherever you cloned this repo.

Merge `package/claude-config/hooks.snippet.json`'s `hooks.UserPromptSubmit`
entry into `~/.claude/settings.json`, again preserving anything already
there rather than overwriting the file.

## 6. Restart Claude Code

MCP server and hook registration only take effect on a new session (or
reconnecting the specific MCP servers) - restart Claude Code, or use
`/mcp` to reconnect `stt-local` and `kokoro-local` specifically.

## 7. Verify

From the repo root:

```sh
uv run --with pytest pytest -q
```

Then in Claude Code, call `mcp__stt-local__start_listener` and say "Donna"
followed by anything - a filler clip should play almost immediately
(pre-rendered in `~/.claude/donna/fillers/`, regenerated via
`mcp__kokoro-local__speak(text=..., play=False, output_dir="~/.claude/donna/fillers")`
if that directory doesn't exist yet - see `listen_daemon.py`'s
`FILLERS_DIR` comment), and the session should pick up and respond to
what you said shortly after.

Voice controls once running: say "Donna" anywhere to get attention
(no wake phrase needed), "mute"/"unmute" (or the old "OMG mute" phrasing,
both work) to toggle spoken replies, "go deaf" to stop the listener
entirely (only restartable by typing in Claude Code, since a stopped
listener can't hear a command to restart itself).

## What's NOT packaged here

- `models/ggml-base.en.bin` - too large for git, see step 2.
- The Agent SDK bridge path under `src/donna/orchestration/` - that's a working but
  currently-unused alternate delivery mechanism (a second, SDK-owned
  Claude session as the replier). See `STATUS.md` and `CLAUDE.md` if you
  want to stand that up instead of/alongside the direct-file design this
  package sets up. It needs `claude-agent-sdk` (already in
  `pyproject.toml`) and a dedicated Claude session ID that no
  stock Claude Code terminal is concurrently using.
