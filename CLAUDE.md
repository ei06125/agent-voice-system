# DONNA

Ambient voice for Claude Code: a plugin whose monitor (`ears listen`) transcribes
the microphone locally and prints what it hears, which Claude Code delivers to
the model as notifications. `ears say` speaks replies through a local Kokoro
voice. `README.md` explains the design and how to use it.

When the plugin is enabled, a `SessionStart` hook has already told you what the
`[heard ...]` notifications are and how to respond to them
(`plugin/lib/donna_ears/protocol.md`). This file is about working on the code.

## Rules

- **Never open the microphone to test.** Feed the pipeline a recording:
  `plugin/bin/ears listen --input-file x.wav --exit-at-eof`, or
  `EARS_INPUT_FILE=x.wav` for a whole session. `say -o x.aiff "..."` makes test
  speech. The one exception is `ears doctor --mic-test`, and only when the user
  asks for a microphone test.
- **The listener's stdout is the event stream.** Every line printed there wakes
  the model. Diagnostics go to the log (`~/.donna/ears/ears.log`), never to stdout.
- **Transcripts are private.** Do not print `ears tail`, `ears since` or the
  files under `~/.donna/ears/sessions/` unless the user asks to see what was
  said. Nothing that was said may be written to the log.
- **The microphone must not outlive its session.** That is the invariant most
  of `listen.py` and `bin/ears` exists for. After touching either, run the
  lifecycle tests in `tests/test_pipeline.py` and `tests/test_listener.py`.
  In particular the signal handler must not take a lock, log, or call
  `Event.set()`: three SIGTERMs arrive together at session end, Python nests the
  handler, and a lock taken there deadlocks with the microphone open.
- **Do not make the machine speak to test.** `voice_server.VoiceServer` takes
  an engine and a player; the tests pass stand-ins. Run `ears say` for real
  only when the user is there to hear it and asked for speech.
- **What is spoken is private too.** Reply text never goes in a log. An
  exception from the speech engine may quote the text, so only its type is
  logged.
- **The voice server must not outlive the sessions using it.** Each request
  names its Claude Code process; the server exits when none is alive, and when
  it has been idle for `voice_idle_s`. Its signal handlers follow the same
  rule as the listener's.
- **The voice server keeps its own environment** (`lib/speak.py`: Kokoro,
  PyTorch). Nothing the listener or the light commands import may pull it in.
- **Ask before editing user-scoped Claude configuration** (`~/.claude.json`,
  `~/.claude/settings.json`).
- Use a private `DONNA_HOME` for experiments so they do not touch the real
  transcripts or a running listener. The tests do this automatically.

## Commands

```sh
uv run --with pytest pytest -q tests                                      # all, about a minute
uv run --with pytest pytest -q tests --deselect tests/test_pipeline.py    # unit tests, seconds
uvx ruff check plugin tests && uvx ruff format --check plugin tests
claude plugin validate plugin
```

The end-to-end tests need `whisper-server`, `ffmpeg`, `say` and the speech model
in `~/.donna/models`; they skip themselves otherwise. The pre-commit hook
(`.githooks/pre-commit`) runs gitleaks and the whole suite.

To try a change in a real session without touching the one you are in, start
another with `claude --plugin-dir plugin`. Monitors are armed at session start,
so edits to the listener need a new session to take effect.

## Facts about the harness the code depends on

Read from Claude Code 2.1.322; re-check after an upgrade.

- Monitor stdout: lines within 200 ms merge into one notification; a line is cut
  at 500 characters, a notification at 3000.
- Notifications are metered: a burst of 10, then one per 2 s. Excess is dropped,
  not queued. `events.EventWriter` paces writes to stay inside that.
- A plugin monitor is armed once per session and is not restarted if it exits,
  which is why `bin/ears` supervises the listener.
- On a normal exit the monitor's process group gets SIGTERM. If Claude Code is
  killed outright the monitor is orphaned and gets nothing; the listener's
  watchdog on `CLAUDE_PID` covers that.
- The monitor's environment carries `CLAUDE_PID`, `CLAUDE_CODE_SESSION_ID` and
  `CLAUDE_CODE_SESSION_ATTENDED` (`0` under `claude -p`, where monitors do not run).
- `SessionEnd` hooks get about 1.5 s, and fire on `/clear` too, where the
  process and its monitor live on. The stop hook therefore matches only real exits.
- The Monitor tool (as opposed to a plugin monitor) expires after 30 minutes at
  most. Arming `ears listen` with it is fine for a trial; it is not the product.

Measured on an M-series Mac, to re-check when Kokoro or PyTorch changes: the
voice server loads in about 5 s once its environment exists; a reply's first
sound comes 0.9 s after `ears say`, which is the time to synthesise the first
sentence (the output device is opened in parallel).

## History

Earlier attempts (wake-phrase daemon, Stop-hook buffer, SQLite delivery queue,
SDK-owned session, a Kokoro MCP server) are on the `DEPRECATED` branch. Do not
port pieces back without being asked.
