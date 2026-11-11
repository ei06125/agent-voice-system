# DONNA

Ambient voice for Claude Code. While a session runs, the microphone is
transcribed on this machine and what it hears is pushed into the conversation.
Claude writes down what was said and what was meant, and acts on what was
addressed to it. Nothing is typed, nothing polls, and the microphone closes
when the session does. Claude can answer out loud too, in a local voice that
stops when you talk over it.

```
you, out loud:   "Hey Claude, can you run the test suite again?"

⏺ Monitor event: [heard 22:41:07] Hey Claude, can you run the test suite again? · Microphone transcript (donna ears)
⏺ Heard: you want the test suite run again. Running it now.
⏺ Bash(uv run pytest -q) ...
⏺ Bash(ears say "All hundred and eighty tests pass.")      <- in your headphones
```

## How it works

```
microphone ──▶ Silero VAD ──▶ whisper.cpp server ──▶ filter ──▶ batcher ──▶ stdout
 sounddevice    cuts audio     large-v3-turbo on      drops       waits for    │
 16 kHz mono    into           Metal, loaded once     Whisper's   a pause      │
                utterances     for the session        phantoms                 ▼
                                             Claude Code plugin monitor: every burst of
                                             lines becomes one notification that wakes
                                             the model, idle or mid-task
```

It is one process, `ears listen`, declared as a **plugin monitor**. Claude Code
starts it with the session, turns its stdout into notifications, and stops it
when the session ends. There is no queue, no socket, no hook polling a buffer
and no second Claude session: the harness does the delivery.

A `SessionStart` hook tells the model what the `[heard ...]` lines are and how
to treat them. A `SessionEnd` hook stops the listener, as a second line of
defence behind the harness's own SIGTERM.

### Speaking back

`ears say "..."` is the other direction. The first call starts a voice server
that keeps a [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M) model warm on
Metal: about five seconds to load, under a second from the call to the first
sound after that. Replies are played in the order they were sent. It is a
separate process with its own, heavier environment, so a session that never
speaks never loads PyTorch.

Speaking and listening at once comes down to one question: can the microphone
hear the reply?

| Output | While a reply plays |
| --- | --- |
| Headphones (AirPods, a headset, the headphone jack) | The microphone stays open. Talk over the reply and it stops; what you said is delivered as usual. |
| Loudspeakers, or a device that is not recognised | The listener ignores the microphone until the reply has ended, because voice detection cannot tell the reply from you. `ears voice stop` cuts it short. |

While it plays, the voice server keeps a small flag file (`run/speaking.json`:
who is speaking, and whether through loudspeakers). The listener checks it for
every audio frame, and signals the server when you start to talk.

### Why a plugin monitor

The ways Claude Code 2.1.322 lets something outside the conversation start a
turn. "Wakes idle" means a turn starts with nothing typed; "mid-task" means the
message lands between two tool calls of a turn already running.

| Mechanism | Wakes idle | Mid-task | Starts by itself | The catch |
| --- | --- | --- | --- | --- |
| **Plugin monitor** | yes | yes | yes, with the session | none found: this is what DONNA uses |
| Monitor tool | yes | yes | no, the model must arm it | expires after 30 minutes at most, then needs re-arming |
| `asyncRewake` hook | yes | not tested | no, one long-poll per message | must be re-armed after every delivery; a hook timeout applies |
| Channel (MCP push) | yes | queued for the next turn | yes | research preview: a custom server needs `--dangerously-load-development-channels` on every launch |
| `/loop`, cron | only while idle | no | no | a model turn per tick even in silence, a minute of latency at best |
| MCP tool | no | no | no | a tool answers a call; it cannot start a turn |

The plugin monitor row was tested in real sessions: a line printed by the
monitor started the first turn of a session nobody had typed into, and a line
printed while the model was running a chain of commands arrived between the
third and fourth. The Monitor tool shares that delivery path. The other rows
are from the Claude Code documentation and were not exercised here.

So, to the question "can an MCP tool ping Claude every five seconds": a tool
cannot, a channel can but is not ready for daily use, and a monitor does the
same job with no flags.

## Install

Requirements: macOS on Apple silicon, [whisper.cpp](https://github.com/ggerganov/whisper.cpp)
and [uv](https://docs.astral.sh/uv/).

```sh
brew install whisper-cpp uv
```

Enable the plugin for this project only, loaded in place from this checkout:

```sh
claude plugin marketplace add /path/to/DONNA --scope local
claude plugin install donna@donna --scope local
```

Use `--scope user` instead to have it in every project. To try it for one
session without installing anything:

```sh
claude --plugin-dir /path/to/DONNA/plugin
```

Start a new session after installing: monitors are armed at session start.

The first session downloads the speech model (1.6 GB) into `~/.donna/models`.
macOS grants the microphone per terminal application, so the first run also
asks for that. `ears doctor` checks all of it.

Claude runs `ears` through its Bash tool. To keep spoken replies from waiting
on a permission prompt, allow it once in your settings:
`"permissions": {"allow": ["Bash(ears *)"]}`.

Spoken replies cost nothing until they are used. The first `ears say` builds
the voice environment (PyTorch and Kokoro, about 1 GB, cached by uv) and
fetches the Kokoro model (310 MB) from Hugging Face. Replies fall back to the
macOS built-in voice until that has finished, and whenever Kokoro cannot start.

## Use

Talk. A couple of seconds after you stop, what you said arrives in the
conversation and Claude responds:

- **Said to Claude** (a question, a request, a correction): handled like a typed message.
- **Said near the microphone** (a call, a colleague): a short note of who said what
  and what was decided. No interruption, no work started.
- **Unintelligible**: one line saying so.

Speech can direct Claude but cannot authorise it. Anything destructive, hard to
undo or visible to other people still needs a typed go-ahead, because anyone
near the microphone, or a video playing, can produce a transcript line.

Say "talk back to me" or "answer out loud" and Claude also speaks its replies:
the gist in a sentence or three, with code, paths and lists left on screen.
"Text only" turns that off again. Set `speak` to have every session start
with it on.

| Command | Effect |
| --- | --- |
| `ears status` | what the listener is doing |
| `ears pause` / `ears resume` | close and reopen the microphone |
| `ears mode notes` | one digest a minute instead of a push at every pause (meetings, calls) |
| `ears mode live` | back to conversation |
| `ears voice on` / `ears voice off` | spoken replies for this session |
| `ears say "..."` | speak; this is what Claude runs to answer out loud |
| `ears voice stop` | cut off what is being said |
| `ears tail -n 20` | the last utterances heard |
| `ears since 15m` / `ears since 14:30` | everything heard since then |
| `ears doctor` | check the installation; `--mic-test` records three seconds |

`ears` is on Claude's PATH while the plugin is enabled, so asking "mute the mic"
or "what did we just say about the deploy?" works too. `/donna:ears pause` is
the same thing as a slash command.

### Settings

`~/.donna/config.json`, read when a session starts. Every key is optional, and
`EARS_<KEY>` in the environment overrides the file.

```json
{
  "ears": {
    "device": "BRIO, MacBook",
    "language": "auto",
    "pause_s": 2.0
  }
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `device` | *(empty)* | Follow the macOS default input, including when it changes. Otherwise name fragments in order of preference |
| `language` | `auto` | Whisper language code, or detect per utterance |
| `model` | `large-v3-turbo` | A whisper.cpp model name, or a path to a ggml file |
| `prompt` | *(empty)* | Vocabulary hint for Whisper (names, jargon) |
| `mode` | `live` | Mode a session starts in |
| `pause_s` | `2.0` | Live: silence that ends a batch |
| `min_interval_s` | `3.0` | Live: least time between two pushes |
| `max_wait_s` | `30.0` | Live: a monologue is delivered in chunks this long |
| `busy_events`, `busy_interval_s` | `5`, `15.0` | Live: after this many pushes in a minute, space them this far apart |
| `notes_interval_s` | `60.0` | Notes: length of one digest |
| `vad_threshold`, `min_silence_ms`, `min_speech_ms`, `pre_roll_ms`, `max_utterance_s` | `0.5`, `700`, `250`, `300`, `28` | Voice activity detection |
| `speak` | `false` | Start sessions with spoken replies on |
| `voice` | `af_heart` | Kokoro voice. Its first letter is its language: `a` American and `b` British English, `p` Brazilian Portuguese, `e` Spanish, `f` French, `i` Italian. `ears say --voice pf_dora` picks one for a single reply |
| `voice_speed` | `1.0` | Speaking rate |
| `voice_engine` | `kokoro` | `say` uses the macOS built-in voice: nothing to download, less natural |
| `say_voice` | *(empty)* | macOS voice for the `say` engine and for the fallback; empty is the system voice |
| `voice_gate` | `auto` | Ignore the microphone while speaking: `auto` does unless the output is headphones, or force it `on` or `off` |
| `voice_barge_in` | `true` | Talking over a reply stops it |
| `voice_idle_s` | `1800` | The voice server exits after this long without a reply |

Japanese and Chinese Kokoro voices need extra packages that are not installed.

## Privacy

- Audio is processed in memory and never written to disk or sent anywhere.
  Whisper runs locally; its server listens on loopback only.
- Transcript **text** is kept in `~/.donna/ears/sessions/<session>.jsonl`,
  readable by you alone, and it becomes part of the conversation, which goes to
  Anthropic like anything you type.
- The log (`~/.donna/ears/ears.log`) records timings and counts, never words.
- The microphone is open exactly as long as the listener process lives, and the
  listener cannot outlive its session: it stops on SIGTERM, when the Claude Code
  process disappears, when its output pipe closes, and five seconds after being
  asked to stop no matter what.
- One session holds the microphone at a time. A second session waits and takes
  over when the first ends. Headless runs (`claude -p`) never listen.
- Speech output is local as well. What is spoken is not logged. The voice
  server listens on loopback only and serves only callers holding the token in
  its state file, which you alone can read. It exits when the sessions that
  used it are gone, and after half an hour without a reply.

## Develop

```sh
uv run --with pytest pytest -q tests     # everything, about a minute
uv run --with pytest pytest -q tests --deselect tests/test_pipeline.py   # unit tests only, seconds
uvx ruff check plugin tests && uvx ruff format --check plugin tests
```

No test opens a microphone or makes a sound. The end-to-end tests synthesise
speech with `say`, play it from a file through the real listener process, and
check what comes out and how the process starts, shares the microphone,
restarts and dies. The voice server is tested with stand-ins for Kokoro and
for the loudspeaker. To do the same by hand:

```sh
say -o /tmp/hello.aiff "Hey Claude, what is the current git branch?"
plugin/bin/ears listen --input-file /tmp/hello.aiff --exit-at-eof
```

`EARS_INPUT_FILE=/path/to.wav claude --plugin-dir plugin` runs a whole session
against a recording instead of the microphone.

```
plugin/
  .claude-plugin/plugin.json
  monitors/monitors.json     the listener, as a monitor
  hooks/hooks.json           SessionStart: protocol for the model. SessionEnd: stop
  skills/ears/SKILL.md       /donna:ears
  bin/ears                   launcher; restarts a crashed listener
  lib/ears.py                entry point, with the dependency list uv installs
  lib/speak.py               entry point of the voice server (Kokoro, PyTorch)
  lib/donna_ears/
    listen.py                lifetime, capture loop, delivery
    voice.py                 `ears say`: client, speaking flag, macOS fallback
    voice_server.py          warm Kokoro model, playback, interruption
    segmenter.py             speech probability -> utterances
    stt.py                   whisper-server process and client
    textfilter.py            rejects Whisper's hallucinations
    batcher.py               when to push
    events.py                formatting and pacing within the harness's limits
    audio.py                 microphone and file sources
    cli.py, config.py        commands, settings, state files
    lock.py                  one microphone holder, one voice server
    protocol.md              what the model is told at session start
tests/
```

Monitor limits the code is built around, read from Claude Code 2.1.322 and worth
re-checking after an upgrade: lines written within 200 ms merge into one
notification; a line is cut at 500 characters and a notification at 3000;
notifications are metered at a burst of 10 and one per 2 s after that, and
anything over is dropped; a plugin monitor is armed once per session and not
restarted if it exits.

## History

Everything tried before this, a wake-phrase daemon with a Stop-hook buffer, a
durable SQLite queue with leasing, an Agent SDK-owned session, a Kokoro MCP
server, and the research notes that went with them, is on the `DEPRECATED`
branch.
