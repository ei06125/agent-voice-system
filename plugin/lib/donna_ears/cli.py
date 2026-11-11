"""Command line for donna ears.

``listen`` and ``doctor`` need the audio dependencies and run under uv. Every
other command is standard library only, because the session hooks call them
and have to return in well under a second.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, config

# What the model is told at the start of every session. A notification from a
# monitor is, to the model, anonymous background output: this text is what
# makes it the user's voice. It is prose for a model, so it lives in its own
# file rather than in a Python string.
PROTOCOL_FILE = Path(__file__).with_name("protocol.md")

STANDBY_NOTE = (
    "\nRight now another Claude Code session holds the microphone, so this session hears "
    "nothing. It takes over automatically if that session ends. `ears status` shows who has it.\n"
)


def protocol() -> str:
    return PROTOCOL_FILE.read_text(encoding="utf-8")


# ----------------------------------------------------------------- helpers


def _ancestors(limit: int = 8) -> list[int]:
    """Process ids above this one, nearest first."""
    found: list[int] = []
    pid = os.getppid()
    for _ in range(limit):
        if pid <= 1:
            break
        found.append(pid)
        try:
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
            pid = int(out)
        except (OSError, ValueError, subprocess.SubprocessError):
            break
    return found


def _mine(listeners: list[dict]) -> dict | None:
    """The listener that belongs to the Claude Code session running this command."""
    claude_pid = os.environ.get("CLAUDE_PID", "")
    if claude_pid.isdigit():
        for listener in listeners:
            if int(listener.get("claude_pid") or 0) == int(claude_pid):
                return listener
    if listeners:
        above = set(_ancestors())
        for listener in listeners:
            if int(listener.get("claude_pid") or 0) in above:
                return listener
    session = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if session:
        for listener in listeners:
            if listener.get("session_id") == session:
                return listener
    return None


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _describe(listener: dict) -> str:
    now = time.time()
    status = listener.get("status", "unknown")
    detail = listener.get("detail", "")
    head = f"ears: {status}"
    if status == "listening":
        head += f' ({listener.get("mode", "live")}) on "{listener.get("device", "?")}"'
    elif status == "paused":
        head += " - microphone closed; `ears resume` reopens it"
    if detail:
        head += f" - {detail}"
    last_voice = float(listener.get("last_voice_at") or 0)
    lines = [
        head,
        "      {utterances} utterances heard, {pushed} events pushed, {pending} pending; "
        "last voice {voice}; up {up}".format(
            utterances=listener.get("utterances", 0),
            pushed=listener.get("pushed", 0),
            pending=listener.get("pending", 0),
            voice=f"{_ago(now - last_voice)} ago" if last_voice else "never",
            up=_ago(now - float(listener.get("started_at") or now)),
        ),
        "      model {model}, language {language}, pid {pid}, session {session}".format(
            model=listener.get("model", "?"),
            language=listener.get("language", "?"),
            pid=listener.get("pid", "?"),
            session=str(listener.get("session_id", "?"))[:8],
        ),
    ]
    return "\n".join(lines)


def _active(listeners: list[dict]) -> dict | None:
    """The listener that holds the microphone, if any."""
    for listener in listeners:
        if listener.get("status") in ("listening", "paused", "starting", "error"):
            return listener
    return None


# ---------------------------------------------------------------- commands


def cmd_status(args) -> int:
    listeners = config.listeners()
    if args.json:
        print(json.dumps({"listeners": listeners, "control": config.read_control()}))
        return 0
    mine = _mine(listeners)
    if mine:
        print(_describe(mine))
    else:
        print("ears: no listener is running for this session.")
        if not listeners:
            print(
                "      It starts with an interactive Claude Code session when the donna "
                "plugin is enabled."
            )
    print("      " + _voice_line(config.load()))
    for other in listeners:
        if other is not mine:
            print("other session -> " + _describe(other).replace("\n", "\n                 "))
    return 0


def _wait_for(status: str, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = _active(config.listeners())
        if active and active.get("status") == status:
            return True
        time.sleep(0.1)
    return False


def cmd_pause(args) -> int:  # noqa: ARG001
    if not _active(config.listeners()):
        print("ears: no active listener to pause.")
        return 1
    config.write_control(paused=True)
    confirmed = _wait_for("paused")
    print(
        "ears: paused - the microphone is closed"
        + ("." if confirmed else " (not confirmed yet; check `ears status`).")
        + " A spoken 'resume' cannot be heard: run `ears resume`."
    )
    return 0


def cmd_resume(args) -> int:  # noqa: ARG001
    if not _active(config.listeners()):
        print("ears: no active listener to resume.")
        return 1
    config.write_control(paused=False)
    confirmed = _wait_for("listening", timeout=4.0)
    print("ears: listening again." if confirmed else "ears: resume requested; check `ears status`.")
    return 0


def cmd_mode(args) -> int:
    active = _active(config.listeners())
    if not args.mode:
        current = (
            (active or {}).get("mode") or config.read_control()["mode"] or config.load()["mode"]
        )
        print(f"ears: mode is {current}")
        return 0
    config.write_control(mode=args.mode)
    cfg = config.load()
    if args.mode == "notes":
        print(
            f"ears: notes mode - one digest every {int(float(cfg['notes_interval_s']))}s "
            "while people are talking."
        )
    else:
        print("ears: live mode - speech is pushed at each natural pause.")
    if not active:
        print("      (no active listener right now; a new session starts in its configured mode)")
    return 0


def cmd_stop(args) -> int:
    listeners = config.listeners()
    mine = _mine(listeners)
    targets = listeners if args.all else ([mine] if mine else [])
    for listener in targets:
        try:
            os.kill(int(listener["pid"]), signal.SIGTERM)
        except (OSError, KeyError, ValueError):
            pass
    from . import voice

    if args.all:
        voice.stop_server()
    else:
        voice.release(voice.owner_pid())
    if not args.quiet:
        print(f"ears: stopped {len(targets)} listener(s)." if targets else "ears: nothing to stop.")
    return 0


def _speak_setting(cfg: dict) -> bool:
    """Whether spoken replies are on: the session's switch, else the setting."""
    chosen = config.read_control()["speak"]
    return bool(cfg["speak"]) if chosen is None else chosen


def _voice_line(cfg: dict) -> str:
    from . import voice

    line = "spoken replies " + ("on" if _speak_setting(cfg) else "off")
    if str(cfg["voice_engine"]).lower() == "say":
        return line + "; voice: macOS built-in"
    state = voice.server_state()
    loaded = {"ready": "ready", "loading": "loading"}.get((state or {}).get("status"), "not loaded")
    return line + f"; voice {cfg['voice']} (kokoro): {loaded}"


def cmd_say(args) -> int:
    from . import voice

    text = " ".join(args.text).strip()
    if not text and not sys.stdin.isatty():
        text = sys.stdin.read()
    try:
        result = voice.say(
            text,
            config.load(),
            voice_name=args.voice or "",
            speed=args.speed or 0.0,
            wait=args.wait,
        )
    except voice.VoiceError as exc:
        print(f"ears: could not speak: {exc}", file=sys.stderr)
        return 1
    print(voice.describe(result))
    return 0


def cmd_voice(args) -> int:
    from . import voice

    cfg = config.load()
    if args.action == "on":
        config.write_control(speak=True)
        if str(cfg["voice_engine"]).lower() != "say":
            try:
                voice.warm()  # so the first reply does not wait for the model
            except voice.VoiceError as exc:
                print(f"ears: {exc}; the macOS voice will be used instead.")
        print("ears: " + _voice_line(cfg) + '. `ears say "..."` speaks.')
    elif args.action == "off":
        config.write_control(speak=False)
        voice.hush()
        print("ears: spoken replies off.")
    elif args.action == "stop":
        print("ears: stopped speaking." if voice.hush() else "ears: nothing was being said.")
    else:
        print("ears: " + _voice_line(cfg))
    return 0


def _transcript_file(session: str | None) -> Path | None:
    directory = config.sessions_dir()
    candidates: list[str] = []
    if session:
        candidates.append(session)
    else:
        if os.environ.get("CLAUDE_CODE_SESSION_ID"):
            candidates.append(os.environ["CLAUDE_CODE_SESSION_ID"])
        mine = _mine(config.listeners())
        if mine and mine.get("session_id"):
            candidates.append(str(mine["session_id"]))
    for candidate in candidates:
        matches = sorted(directory.glob(f"{candidate}*.jsonl"))
        if matches:
            return matches[0]
    if session:
        return None
    try:
        files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    except OSError:
        return None
    return files[-1] if files else None


def _read_transcript(path: Path) -> list[dict]:
    records = []
    try:
        with open(path) as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict) and record.get("text"):
                    records.append(record)
    except OSError:
        pass
    return records


def _print_records(records: list[dict]) -> None:
    today = time.strftime("%Y-%m-%d")
    for record in records:
        when = time.localtime(float(record.get("t0") or 0))
        day = time.strftime("%Y-%m-%d", when)
        stamp = time.strftime("%H:%M:%S", when)
        if day != today:
            stamp = f"{day} {stamp}"
        language = record.get("lang") or ""
        tag = f" {language}" if language and language != "en" else ""
        print(f"[heard {stamp}{tag}] {record['text']}")


def cmd_tail(args) -> int:
    path = _transcript_file(args.session)
    if not path:
        print("ears: nothing has been heard yet.")
        return 0
    records = _read_transcript(path)
    _print_records(records[-args.n :])
    if not records:
        print("ears: nothing has been heard yet.")
    return 0


def _parse_since(spec: str) -> float | None:
    """'15m', '2h', '90s' or '14:30' (today) as a wall-clock timestamp."""
    spec = spec.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600}
    try:
        if spec and spec[-1] in units:
            return time.time() - float(spec[:-1]) * units[spec[-1]]
        if ":" in spec:
            parts = [int(p) for p in spec.split(":")]
            now = time.localtime()
            return time.mktime(
                (
                    now.tm_year,
                    now.tm_mon,
                    now.tm_mday,
                    parts[0],
                    parts[1],
                    parts[2] if len(parts) > 2 else 0,
                    0,
                    0,
                    -1,
                )  # fmt: skip
            )
        return time.time() - float(spec) * 60
    except (ValueError, IndexError):
        return None


def cmd_since(args) -> int:
    start = _parse_since(args.when)
    if start is None:
        print("ears: give a span like 15m, 2h or 90s, or a time like 14:30.", file=sys.stderr)
        return 2
    path = _transcript_file(args.session)
    records = (
        [r for r in _read_transcript(path) if float(r.get("t1") or 0) >= start] if path else []
    )
    if not records:
        print(f"ears: nothing heard since {time.strftime('%H:%M:%S', time.localtime(start))}.")
        return 0
    _print_records(records)
    return 0


def cmd_context(args) -> int:  # noqa: ARG001
    """SessionStart hook: tell the model what the transcript lines are."""
    hook: dict = {}
    if not sys.stdin.isatty():
        try:
            hook = json.load(sys.stdin)
        except ValueError:
            hook = {}
    # Monitors only run in interactive sessions. In a headless run (`claude -p`,
    # the Agent SDK) there is no listener, so saying "the microphone is on"
    # would be untrue. Claude Code marks those runs in the hook's environment.
    if os.environ.get("CLAUDE_CODE_ENTRYPOINT", "cli").startswith("sdk"):
        return 0
    if os.environ.get("CLAUDE_CODE_SESSION_ATTENDED", "1").lower() in ("0", "false"):
        return 0
    source = hook.get("source", "startup")
    listeners = config.listeners()
    mine = _mine(listeners)
    other = next(
        (
            item
            for item in listeners
            if item is not mine and item.get("status") in ("listening", "paused")
        ),
        None,
    )
    cfg = config.load()
    # At startup the listener has not yet reset the switches for this session.
    speak = bool(cfg["speak"]) if source == "startup" else _speak_setting(cfg)
    context = (
        protocol()
        + (STANDBY_NOTE if other else "")
        + f"\nSpoken replies are {'ON' if speak else 'OFF'} right now.\n"
    )
    out: dict = {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}
    }
    if source in ("startup", "resume"):
        if other:
            out["systemMessage"] = (
                "donna ears: standby - another Claude Code session holds the microphone"
            )
        else:
            out["systemMessage"] = (
                "donna ears: the microphone is live for this session "
                "(`ears pause` closes it, `ears status` shows what it is doing)"
            )
    print(json.dumps(out))
    return 0


def cmd_doctor(args) -> int:
    from . import audio, stt

    config.ensure_dirs()
    cfg = config.load()
    healthy = True

    def report(good: bool, label: str, detail: str) -> None:
        nonlocal healthy
        healthy = healthy and good
        print(f"{'ok  ' if good else 'FAIL'}  {label}: {detail}")

    binary = stt.find_server_binary()
    report(bool(binary), "whisper-server", binary or "not found - brew install whisper-cpp")
    model = stt.model_path(cfg)
    if not model.exists() and args.download and stt.model_is_named(cfg):
        print(f"      downloading {cfg['model']} ...")
        try:
            stt.download_model(cfg)
        except (stt.SttError, subprocess.SubprocessError, OSError) as exc:
            print(f"      download failed: {exc}")
    if model.exists():
        report(True, "speech model", f"{model} ({model.stat().st_size / 1e6:.0f} MB)")
    else:
        report(
            False,
            "speech model",
            f"{model} is missing - it downloads on first use, or run `ears doctor --download`",
        )
    try:
        inputs = audio.list_inputs()
    except Exception as exc:  # noqa: BLE001
        inputs = []
        report(False, "audio", f"cannot query devices: {exc}")
    report(bool(inputs), "input devices", f"{len(inputs)} found")
    for device in inputs:
        print(f"        {'*' if device['default'] else ' '} {device['name']}")
    hint = str(cfg["device"]).strip()
    print(f"      device setting: {hint or '(follow the macOS default input, marked *)'}")
    print(
        "      macOS grants the microphone per terminal app "
        f"({os.environ.get('TERM_PROGRAM') or 'unknown'}): "
        "System Settings > Privacy & Security > Microphone"
    )
    from . import voice

    engine = str(cfg["voice_engine"]).lower()
    if engine == "say":
        report(bool(voice.shutil.which("say")), "speech output", "the macOS built-in voice")
    else:
        uv = voice.find_uv()
        report(
            bool(uv),
            "speech output",
            f"Kokoro, voice {cfg['voice']}; loads on the first `ears say` (server: "
            f"{(voice.server_state() or {}).get('status', 'not running')})"
            if uv
            else "uv not found - the macOS voice will be used",
        )
    listeners = config.listeners()
    active = _active(listeners)
    print(
        "      listeners: "
        + (
            ", ".join(f"pid {item.get('pid')} {item.get('status')}" for item in listeners)
            if listeners
            else "none running"
        )
    )
    print(f"      state: {config.ears_dir()}   log: {config.log_path()}")

    if args.mic_test:
        if active and active.get("status") == "listening":
            print("      mic test skipped: a listener already has the microphone open")
        else:
            import numpy as np

            print("      mic test: recording 3 seconds ...")
            source = audio.MicSource(hint)
            try:
                source.open()
                frames = []
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    frame = source.read(timeout=0.5)
                    if frame is not None:
                        frames.append(frame)
            except audio.SourceError as exc:
                report(False, "mic test", str(exc))
                frames = []
            finally:
                source.close()
            if frames:
                peak = float(np.abs(np.concatenate(frames)).max())
                if peak == 0.0:
                    report(False, "mic test", f"{source.name}: pure silence - permission missing?")
                else:
                    level = 20 * np.log10(peak)
                    report(True, "mic test", f"{source.name}: peak {level:.0f} dBFS")
    return 0 if healthy else 1


def cmd_listen(args) -> int:
    from . import listen

    return listen.main(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ears",
        description="donna ears: live microphone transcripts pushed into a Claude Code session.",
    )
    parser.add_argument("--version", action="version", version=f"donna ears {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    p = sub.add_parser("status", help="what the listener is doing")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pause", help="close the microphone")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resume", help="open the microphone again")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("mode", help="live: push at each pause; notes: one digest per minute")
    p.add_argument("mode", nargs="?", choices=config.MODES)
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("tail", help="the last utterances heard in this session")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--session", help="session id (or its first characters)")
    p.set_defaults(func=cmd_tail)

    p = sub.add_parser("since", help="everything heard since 15m / 2h / 90s / 14:30")
    p.add_argument("when", nargs="?", default="10m")
    p.add_argument("--session", help="session id (or its first characters)")
    p.set_defaults(func=cmd_since)

    p = sub.add_parser("doctor", help="check the installation")
    p.add_argument("--mic-test", action="store_true", help="record 3 seconds and report the level")
    p.add_argument("--download", action="store_true", help="fetch the speech model if missing")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("stop", help="stop this session's listener")
    p.add_argument("--all", action="store_true", help="stop every listener")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("say", help="speak text aloud (your reply, in your own words)")
    p.add_argument("text", nargs="*", help="what to say; read from stdin when omitted")
    p.add_argument("--voice", help="Kokoro voice: af_heart, bm_george, pf_dora ...")
    p.add_argument("--speed", type=float, help="1.0 is normal")
    p.add_argument("--wait", action="store_true", help="return once it has been said")
    p.set_defaults(func=cmd_say)

    p = sub.add_parser("voice", help="spoken replies: on, off, stop (what is being said)")
    p.add_argument("action", nargs="?", choices=("on", "off", "stop", "status"), default="status")
    p.set_defaults(func=cmd_voice)

    p = sub.add_parser("listen", help="run the listener (Claude Code starts this as a monitor)")
    p.add_argument("--input-file", help="play an audio file instead of opening the microphone")
    p.add_argument("--exit-at-eof", action="store_true", help="with --input-file: stop at the end")
    p.add_argument(
        "--fast", action="store_true", help="with --input-file: do not pace in real time"
    )
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("context", help="SessionStart hook: print the protocol for the model")
    p.set_defaults(func=cmd_context)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    if os.environ.get("EARS_INPUT_FILE") and args.command == "listen" and not args.input_file:
        args.input_file = os.environ["EARS_INPUT_FILE"]
    config.ensure_dirs()
    return int(args.func(args) or 0)
