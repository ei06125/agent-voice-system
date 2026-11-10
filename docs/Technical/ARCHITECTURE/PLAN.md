# DONNA next phase — fully local voice pipeline

Plan only. Nothing in this document has been executed. Written 2026-08-15
after the Deepgram-based build (see `../../../README.md`) proved unreliable in live
testing (unpredictable wake-word capture, mic-device confusion, latency)
and cost real money per call. Goal: replace both the STT and TTS legs with
local, free, offline components, while keeping everything that already
works and was already proven live today.

## What stays exactly as-is

These parts of today's build are STT/TTS-engine-agnostic and don't need to
change:

- **Wake-word gate + debounce buffer** in `voice-listen.sh` — operates on
  plain text lines from stdin, regardless of what produced them. "Oh my
  god, Donna" pattern-matching and the 3s debounce-and-stitch logic carry
  over unchanged.
- **Stop-hook architecture** in `voice-stop-hook.sh` (`~/.claude/settings.json`,
  global `Stop` hook, `asyncRewake`) — waits on a FIFO for one line from the
  listener, wakes Claude with it via exit code 2. Also engine-agnostic;
  only the producer feeding the FIFO changes.
- **Audio cues** — Tink on hook start, Pop on capture, both already wired
  into `voice-stop-hook.sh` directly (not gated on STT connection timing).
- **Kill switch** — `~/.claude/scripts/.voice-hook-disabled`.

The only thing that needs replacing is the single line in `voice-listen.sh`
that currently does `dg listen --mic --non-interactive | while ...`. That
producer needs to become a local whisper.cpp + Silero VAD pipeline instead
of the Deepgram CLI. Separately, the TTS leg (currently `dg speak` + `afplay`
in the agent's own tool calls each turn) should move to the already-built
local kokoro-local server.

## STT: whisper.cpp + Silero VAD

### Why both, not whisper.cpp alone

whisper.cpp's own `stream` example has a basic energy-threshold VAD built
in (`--vad-thold`), which is what makes it look tempting to use alone. In
practice that kind of simple RMS-energy gate is worse than Deepgram's
server-side endpointing at telling "pause mid-sentence" from "done
talking" — exactly the problem the 3s debounce hack in `voice-listen.sh`
was working around today. Silero VAD is a small (~1MB), fast, ONNX-based
model purpose-built for this distinction and runs easily in real time on
this hardware. Using it as a proper front-end, rather than relying on
whisper.cpp's built-in energy gate, should let the debounce hack shrink or
disappear rather than just move to a different engine.

### Components

- **whisper.cpp**: `brew install whisper-cpp` (confirmed available via
  Homebrew, not yet installed — this is as far as today's session got
  before being told to stop and write this plan instead). Gives Metal
  acceleration on this Apple Silicon Mac Studio for free.
- **Model**: start with `tiny.en` or `base.en` for lowest latency; only
  move to `small.en` if accuracy turns out to be the bottleneck, not
  speed. Download via whisper.cpp's `models/download-ggml-model.sh`.
- **Silero VAD**: available as an ONNX model + a small Python or C++
  runner (`onnxruntime`). Simplest integration path: a small Python
  script using `onnxruntime` + `sounddevice` (or `pyaudio`) that reads
  the mic in ~30ms frames, runs each through Silero, and only writes
  audio to a buffer/pipe while speech is detected, flushing on a
  configurable trailing-silence threshold (this replaces the DEBOUNCE
  hack with an actual purpose-built mechanism).

### Pipeline shape

```
mic (BRIO — confirmed the only audio input device on this Mac Studio,
     no built-in mic exists)
  -> Silero VAD (Python/onnxruntime, ~30ms frames)
       -> while speech: buffer audio
       -> on trailing silence: flush buffered clip
  -> whisper.cpp (whisper-cli, one-shot transcription of the flushed clip)
  -> plain text line to stdout
  -> (unchanged) wake-word gate + debounce in voice-listen.sh
  -> (unchanged) Stop hook FIFO handoff to Claude
```

This is a bigger implementation lift than swapping a CLI flag — it means
writing and maintaining the VAD-chunking glue script ourselves, versus
today's approach of leaning on `dg listen --mic`'s already-built mic
capture + cloud endpointing. Budget real implementation time for this,
not a quick swap.

## TTS: kokoro-local (already built, not yet wired in)

`/Users/pedro/.mcp/kokoro-local/mcp_server.py` already exists and works:
a local Kokoro-82M model via the `kokoro` Python package, exposing a
`speak(text, voice="af_heart", lang_code="a", speed=1, play=True,
output_dir="outputs")` function that generates a WAV and optionally plays
it directly via `afplay` — same playback mechanism already used with `dg
speak` today, just a different generator. It's already configured as an
MCP server for **Codex** (`~/.codex/config.toml`), not for Claude Code.

To use it from Claude Code without going through MCP overhead per call,
it can be invoked directly:

```
uv run --project /Users/pedro/.mcp/kokoro-local python -c \
  "from mcp_server import speak; speak('text to say')"
```

replacing `dg speak "..." -o reply.mp3 --non-interactive && afplay
reply.mp3` in the agent's per-turn voice-reply step. This removes the
Deepgram TTS cost and network round-trip entirely. Latency needs a real
side-by-side comparison against `dg speak` once wired in — Kokoro-82M is
small enough it should be competitive, but this hasn't been measured.

## Implementation checklist (in order)

1. `brew install whisper-cpp`
2. Download `tiny.en` (or `base.en`) ggml model
3. Confirm `whisper-cli` transcribes a test WAV correctly (sanity check
   before touching the live pipeline)
4. Write the Silero VAD chunking script (Python, `onnxruntime` +
   `sounddevice`) — reads BRIO mic, emits one flushed WAV clip per
   detected utterance to a temp path
5. Wire that script's output into a `whisper-cli` call per clip, echoing
   the transcript to stdout — this becomes the new producer inside
   `voice-listen.sh` in place of `dg listen --mic`
6. Test the wake-word gate + debounce against this new producer with the
   same "Oh my god, Donna" phrase used today
7. Swap the TTS call in the agent's per-turn voice-reply step from `dg
   speak` to the kokoro-local direct-invoke shown above
8. Re-run the full loop end-to-end: Stop hook fires -> Tink -> Silero
   detects speech -> whisper.cpp transcribes -> wake word matches -> Pop
   -> Claude responds -> kokoro-local speaks it -> turn ends -> hook fires
   again
9. Only after that works, revisit whether the debounce hack in
   `voice-listen.sh` can be simplified now that Silero is doing real
   endpoint detection, instead of stacked on top of it unnecessarily

## Open questions / things to verify before committing further time

- Real end-to-end latency of whisper.cpp `tiny.en` vs today's Deepgram
  round-trip, on this hardware, for a typical sentence-length utterance —
  unverified, could go either way once VAD-chunking overhead is included
- Whether `sounddevice`/`pyaudio` can reliably grab the BRIO as a specific
  device by index rather than relying on macOS's "current default input
  device," to avoid a repeat of today's device-switching confusion
- CPU/thermal load of running Silero VAD continuously in the background
  for long stretches, on top of whisper.cpp inference on each utterance

## What went wrong today (don't repeat)

- Left multiple background listener processes running concurrently more
  than once (forgot to stop an old Monitor/hook task before starting a
  new one) — caused mic contention and confusing symptoms. Always
  explicitly `pkill -f "dg listen"` / equivalent before starting a new
  producer, not just before the *first* one.
- Assumed the Mac Studio had a built-in mic; it doesn't — BRIO webcam is
  the only audio input device on this machine. Don't re-litigate this,
  it's confirmed via `ffmpeg -f avfoundation -list_devices true -i ""`.
- Debugged by guessing and restarting rather than getting an objective
  signal level reading — `ffmpeg -i ... -af volumedetect` (mean/max dB) was
  what actually resolved the "is this a distance problem or a real bug"
  question, twice. Reach for that instead of blind retries next time
  something claims "not working."
