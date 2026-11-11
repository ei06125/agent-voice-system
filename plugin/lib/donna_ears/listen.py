"""The listener: microphone -> utterances -> text -> events on stdout.

It runs as a Claude Code plugin monitor. Claude Code starts it with the
session, turns every burst of stdout lines into a notification that wakes the
model, and stops it when the session ends. Everything here follows from that:

* stdout carries only what the model should read. Diagnostics go to the log.
* The microphone is open exactly as long as this process lives, so the process
  must not outlive its session. Claude Code sends SIGTERM on a normal exit; a
  watchdog covers the case where Claude Code itself is killed.
* Only one listener may hold the microphone. A second session waits its turn
  instead of hearing, and answering, the same words.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import signal
import subprocess
import sys
import threading
import time

from . import audio, config, events, stt, textfilter, voice
from .batcher import Batcher, Utterance
from .lock import FileLock
from .segmenter import FRAME_SAMPLES, SAMPLE_RATE, Segmenter

# Exit status for "do not restart me": the launcher's supervisor loop respawns
# the listener after a crash, but not after a problem only the user can fix.
EXIT_FATAL = 75

ERROR_REPEAT_S = 300.0  # the same complaint at most once per five minutes

# Once asked to stop, the process is gone within this many seconds no matter
# what: an open microphone must never outlive its session because a teardown
# call got stuck.
STOP_DEADLINE_S = 5.0

# Speaking and listening at once. While a reply is played through loudspeakers
# the microphone hears it, and voice detection cannot tell it from the user, so
# those frames are ignored, plus a moment for the room to go quiet. Through
# headphones nothing leaks: the microphone stays open, and the user talking
# over the reply stops it.
GATE_HANGOVER_S = 0.4
BARGE_IN_S = 0.3  # this much continuous speech counts as talking over the reply


def make_logger() -> logging.Logger:
    log = logging.getLogger("ears")
    log.setLevel(logging.INFO)
    log.propagate = False
    if not log.handlers:
        handler = logging.handlers.RotatingFileHandler(
            config.log_path(), maxBytes=1_000_000, backupCount=2
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
        log.addHandler(handler)
    return log


class MicLock(FileLock):
    """Advisory lock that says "this process has the microphone"."""

    def __init__(self) -> None:
        super().__init__(config.lock_path())


class TranscriptLog:
    """Append-only record of everything heard in one session.

    The events are the push; this is the memory. ``ears tail`` and
    ``ears since`` read it back.
    """

    def __init__(self, session_id: str) -> None:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in session_id)
        self.path = config.sessions_dir() / f"{safe or 'session'}.jsonl"

    def append(self, utterance: Utterance) -> None:
        record = {
            "t0": round(utterance.started_at, 2),
            "t1": round(utterance.ended_at, 2),
            "lang": utterance.language,
            "text": utterance.text,
        }
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


class Listener:
    def __init__(self, cfg: dict, args, out: events.EventWriter, log: logging.Logger) -> None:
        self.cfg = cfg
        self.args = args
        self.out = out
        self.log = log
        self.stop = threading.Event()
        self._signalled: int | None = None
        self._done = threading.Event()
        self.owner_pid = int(os.environ.get("CLAUDE_PID") or 0) or os.getppid()
        self.session_id = os.environ.get("CLAUDE_CODE_SESSION_ID") or f"manual-{os.getpid()}"
        self.started_at = time.time()
        self.batcher = Batcher(cfg)
        self.transcript = TranscriptLog(self.session_id)
        self.server: stt.WhisperServer | None = None
        self.vad = None
        self._stt_queue: queue.Queue = queue.Queue(maxsize=64)
        self._errors: dict[str, float] = {}
        self._state_lock = threading.Lock()
        self._state: dict = {}
        self._reopen = threading.Event()
        self._probing = threading.Event()
        self.utterances = 0
        self.rejected = 0
        self.pushed = 0
        self.dropped_segments = 0
        self.barge_ins = 0
        self.last_voice_wall = 0.0

    # ------------------------------------------------------------------ state

    def write_state(self, status: str | None = None, **extra) -> None:
        with self._state_lock:
            if status:
                self._state["status"] = status
                self._state.pop("detail", None)
            self._state.update(extra)
            self._state.update(
                pid=os.getpid(),
                claude_pid=self.owner_pid,
                session_id=self.session_id,
                started_at=self.started_at,
                updated_at=time.time(),
                mode=self.batcher.mode,
                model=str(self.cfg["model"]),
                language=str(self.cfg["language"]),
                utterances=self.utterances,
                rejected=self.rejected,
                pushed=self.pushed,
                pending=len(self.batcher),
                dropped_segments=self.dropped_segments,
                last_voice_at=self.last_voice_wall,
                whisper_pid=self.server.pid if self.server else 0,
            )
            try:
                config.write_json_atomic(config.state_path(os.getpid()), self._state)
            except OSError as exc:
                self.log.warning("could not write state: %s", exc)

    def error_once(self, key: str, message: str) -> None:
        """Tell the model about a problem, without repeating it every second."""
        if self._stopping():
            return  # things break while a session is torn down; that is not news
        now = time.monotonic()
        if now - self._errors.get(key, float("-inf")) < ERROR_REPEAT_S:
            return
        self._errors[key] = now
        self.log.error("%s: %s", key, message)
        self.out.error(message)

    # --------------------------------------------------------------- lifetime

    def _stopping(self) -> bool:
        return self._signalled is not None or self.stop.is_set()

    def _on_signal(self, signum, frame) -> None:  # noqa: ARG002
        # Runs in the main thread, and re-enters itself when signals arrive
        # together. At the end of a session they do: Claude Code signals the
        # process group, the launcher forwards that, and the SessionEnd hook
        # adds its own, all within a millisecond. So this must not take a lock.
        # Event.set() takes one, and a nested call would then wait forever for
        # the lock its own outer call is holding, with the microphone open.
        # Record the signal and nothing else; the wake-up pipe installed in
        # run() hands it to a thread that is free to do the rest.
        self._signalled = signum

    def _relay_signals(self, fd: int) -> None:
        while True:
            try:
                data = os.read(fd, 32)
            except OSError:
                return
            if not data:
                return
            self.log.info("signal %s: stopping", data[-1])
            self.stop.set()

    def _watchdog(self) -> None:
        """Stop when the session that owns us is gone, even if nobody told us,
        and make sure that stopping actually ends the process."""
        while not self.stop.wait(1.0):
            if self._signalled is not None:
                self.stop.set()
            elif not config.pid_alive(self.owner_pid):
                self.log.info("owner pid %s is gone: stopping", self.owner_pid)
                self.stop.set()
            elif self.out.broken:
                self.log.info("stdout closed: stopping")
                self.stop.set()
        if not self._done.wait(STOP_DEADLINE_S):
            self.log.error("shutdown exceeded %ss: exiting forcibly", STOP_DEADLINE_S)
            try:
                if self.server:
                    self.server.kill()
            finally:
                os._exit(0)

    def run(self) -> int:
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        signal.set_wakeup_fd(write_fd, warn_on_full_buffer=False)
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(signum, self._on_signal)
        threading.Thread(
            target=self._relay_signals, args=(read_fd,), name="signals", daemon=True
        ).start()
        threading.Thread(target=self._watchdog, name="watchdog", daemon=True).start()
        config.listeners()  # clears out whatever a killed listener left behind
        self.log.info(
            "listener start session=%s owner=%s source=%s",
            self.session_id,
            self.owner_pid,
            self.args.input_file or "microphone",
        )
        lock = MicLock()
        try:
            if os.environ.get("CLAUDE_CODE_SESSION_ATTENDED", "1").lower() in ("0", "false"):
                # Nobody is sitting at this session; it has no business listening.
                self.write_state("standby", detail="unattended session")
                self.stop.wait()
                return 0
            announced = False
            while not lock.try_acquire():
                if not announced:
                    announced = True
                    self.log.info("standby: another session holds the microphone")
                    self.write_state("standby", detail="another session holds the microphone")
                if self.stop.wait(2.0):
                    return 0
            return self._serve()
        except Exception:  # noqa: BLE001
            self.log.exception("listener crashed")
            return 1
        finally:
            if self.server:
                self.server.stop()
            lock.release()
            try:
                config.state_path(os.getpid()).unlink()
            except OSError:
                pass
            self.log.info(
                "listener stop utterances=%s pushed=%s rejected=%s",
                self.utterances,
                self.pushed,
                self.rejected,
            )
            self._done.set()

    def _fatal(self, message: str) -> int:
        if self._stopping():
            # The speech server and the download share our process group, so a
            # session ending mid-start takes them down first. Not an error.
            return 0
        self.log.error("fatal: %s", message)
        self.out.error(message)
        self.write_state("error", detail=message)
        return EXIT_FATAL

    def _serve(self) -> int:
        # Every session starts out listening, in its configured mode, whatever
        # the last one left behind. A listener that is restarted within a
        # session keeps what the user chose in it.
        if config.read_json(config.control_path()).get("session") != self.session_id:
            config.write_control(
                paused=False,
                mode=str(self.cfg["mode"]),
                speak=bool(self.cfg["speak"]),
                session=self.session_id,
            )
        self.write_state("starting")

        if not stt.model_path(self.cfg).exists():
            if not stt.model_is_named(self.cfg):
                return self._fatal(f"speech model not found: {stt.model_path(self.cfg)}")
            self.out.notice(
                f"downloading the speech model '{self.cfg['model']}' (first run only); "
                "listening starts when it finishes"
            )
            self.write_state("starting", detail="downloading speech model")
            try:
                stt.download_model(self.cfg)
            except (stt.SttError, subprocess.SubprocessError, OSError) as exc:
                return self._fatal(f"could not download the speech model: {exc}")

        self.server = stt.WhisperServer(self.cfg, self.log)
        try:
            self.server.start()
        except stt.SttError as exc:
            return self._fatal(str(exc))

        from silero_vad_notorch import load_silero_vad

        self.vad = load_silero_vad()
        threading.Thread(target=self._stt_worker, name="stt", daemon=True).start()

        if self.args.input_file:
            source = audio.FileSource(
                self.args.input_file,
                realtime=not self.args.fast,
                hold_open=not self.args.exit_at_eof,
            )
        else:
            source = audio.MicSource(str(self.cfg["device"]))
        try:
            return self._capture(source)
        finally:
            source.close()

    # ---------------------------------------------------------------- capture

    def _capture(self, source) -> int:
        cfg = self.cfg
        threshold = float(cfg["vad_threshold"])
        segmenter = Segmenter(
            threshold=threshold,
            min_silence_ms=float(cfg["min_silence_ms"]),
            pre_roll_ms=float(cfg["pre_roll_ms"]),
            min_speech_ms=float(cfg["min_speech_ms"]),
            max_utterance_s=float(cfg["max_utterance_s"]),
        )
        paused = False
        opened = False
        failures = 0
        ended = False
        now = time.monotonic()
        last_voice = now
        last_frame = now
        next_control = 0.0
        next_state = 0.0
        next_probe = now + 15.0
        silent_since: float | None = None
        warned_silent = False
        speaking = voice.SpeakingWatch()
        barge_in = bool(cfg["voice_barge_in"])
        barge_frames = max(1, round(BARGE_IN_S * SAMPLE_RATE / FRAME_SAMPLES))
        gate_until = 0.0
        gated = False
        voiced_run = 0
        barged = False

        while not self._stopping():
            now = time.monotonic()

            if now >= next_control:
                next_control = now + 0.3
                control = config.read_control()
                if control["mode"] and control["mode"] != self.batcher.mode:
                    self.log.info("mode -> %s", control["mode"])
                    self.batcher.mode = control["mode"]
                    self.write_state()
                if control["paused"] != paused:
                    paused = control["paused"]
                    self.log.info("paused -> %s", paused)
                    if paused:
                        self._submit(segmenter.flush(time.time()))
                        source.close()
                        opened = False
                        self.write_state("paused")

            if now >= next_state:
                next_state = now + 2.0
                self.write_state()

            if paused:
                # The microphone is closed. Still deliver what was heard before.
                self._tick(False, now - last_voice)
                self.stop.wait(0.2)
                continue

            if self._reopen.is_set():
                self._reopen.clear()
                self._submit(segmenter.flush(time.time()))
                source.close()
                opened = False

            if not opened:
                try:
                    source.open()
                except audio.SourceError as exc:
                    failures += 1
                    self.write_state("error", detail=str(exc))
                    self.log.warning("open failed (%s): %s", failures, exc)
                    if failures >= 3:
                        self.error_once("open", f"cannot open the microphone: {exc}")
                    self.stop.wait(min(30.0, 2.0**failures))
                    continue
                failures = 0
                opened = True
                segmenter.reset()
                self.vad.reset_states()
                last_frame = time.monotonic()
                self.log.info("listening on %s", source.name)
                self.write_state("listening", device=source.name)

            try:
                frame = source.read(timeout=0.25)
            except audio.EndOfSource:
                ended = True
                break
            now = time.monotonic()

            if frame is None:
                if now - last_frame > 3.0:
                    self.log.warning("no audio for 3 s: reopening %s", source.name)
                    source.close()
                    opened = False
                self._tick(segmenter.in_speech, now - last_voice)
                continue
            last_frame = now

            if source.is_microphone and not warned_silent:
                if float(abs(frame).max()) == 0.0:
                    silent_since = silent_since or now
                    if now - silent_since > 6.0:
                        warned_silent = True
                        app = os.environ.get("TERM_PROGRAM") or "your terminal app"
                        self.error_once(
                            "silent",
                            f"the microphone ({source.name}) is delivering pure digital silence. "
                            "On macOS that usually means the terminal has no microphone "
                            "permission: System Settings > Privacy & Security > Microphone, "
                            f"enable {app}, then restart the session.",
                        )
                else:
                    silent_since = None

            reply = speaking.get()
            if reply and reply.get("gate"):
                gate_until = now + GATE_HANGOVER_S
            if now < gate_until:
                if not gated:
                    gated = True
                    # Keep what the user had said so far; the reply has not
                    # reached the microphone yet.
                    self._submit(segmenter.flush(time.time()))
                    self.log.info("gate on: a reply is playing through loudspeakers")
                self._tick(False, now - last_voice)
                continue
            if gated:
                gated = False
                self.vad.reset_states()
                self.log.info("gate off")

            probability = float(self.vad(frame, SAMPLE_RATE).reshape(-1)[0])
            if probability >= threshold:
                last_voice = now
                self.last_voice_wall = time.time()
            if reply and barge_in:
                voiced_run = voiced_run + 1 if probability >= threshold else 0
                if voiced_run >= barge_frames and not barged:
                    barged = voice.interrupt()
                    if barged:
                        self.barge_ins += 1
                        self.log.info("barge-in: the user spoke over the reply")
            else:
                voiced_run = 0
                barged = False
            self._submit(segmenter.push(frame, probability, time.time()))
            self._tick(segmenter.in_speech, now - last_voice)

            if source.follows_default and now >= next_probe and not segmenter.in_speech:
                next_probe = now + 15.0
                if not self._probing.is_set():
                    self._probing.set()
                    threading.Thread(
                        target=self._probe_default, args=(source.name,), daemon=True
                    ).start()

        if ended:
            # A file source ran out: transcribe and deliver what is left, then go.
            self._submit(segmenter.flush(time.time()))
            self._stt_queue.join()
            self._flush()
        return 0

    def _probe_default(self, current: str) -> None:
        try:
            name = audio.probe_default_input()
            if name and name != current:
                self.log.info("default input changed: %r -> %r", current, name)
                self._reopen.set()
        finally:
            self._probing.clear()

    # ------------------------------------------------------------ transcription

    def _submit(self, segment) -> None:
        if segment is None:
            return
        try:
            self._stt_queue.put_nowait(segment)
        except queue.Full:
            self.dropped_segments += 1
            self.log.warning("transcription backlog full: dropped %.1fs", segment.duration)

    def _transcribe(self, samples) -> tuple[str, str]:
        assert self.server is not None
        try:
            return self.server.transcribe(samples)
        except stt.SttError as exc:
            if self._stopping():
                return "", ""
            self.log.warning("transcription failed, restarting whisper-server: %s", exc)
        try:
            self.server.stop()
            self.server.start()
            return self.server.transcribe(samples)
        except stt.SttError as exc:
            self.error_once("stt", f"speech recognition is failing: {exc}")
            return "", ""

    def _stt_worker(self) -> None:
        import numpy as np

        try:
            started = time.monotonic()
            self.server.warm_up()
            self.log.info("model warm in %.1fs", time.monotonic() - started)
        except stt.SttError as exc:
            self.log.warning("warm-up failed: %s", exc)
        while True:
            segment = self._stt_queue.get()
            try:
                started = time.monotonic()
                raw, language = self._transcribe(np.concatenate(segment.frames))
                text = textfilter.accept(raw) if raw else None
                self.log.info(
                    "utterance %.1fs (voiced %.1fs) transcribed in %.1fs: %s",
                    segment.duration,
                    segment.voiced_seconds,
                    time.monotonic() - started,
                    f"{len(text)} chars [{language}]" if text else "rejected",
                )
                if text:
                    utterance = Utterance(
                        text=text,
                        started_at=segment.started_at,
                        ended_at=segment.ended_at,
                        language=language,
                    )
                    self.transcript.append(utterance)
                    self.batcher.add(utterance)
                    self.utterances += 1
                else:
                    self.rejected += 1
            except Exception:  # noqa: BLE001 - one bad clip must not end the session
                self.log.exception("transcription worker error")
            finally:
                self._stt_queue.task_done()

    # ---------------------------------------------------------------- delivery

    def _tick(self, speech_active: bool, quiet_for: float) -> None:
        stt_busy = self._stt_queue.unfinished_tasks > 0
        if self.batcher.due(speech_active, stt_busy, quiet_for):
            self._flush()

    def _flush(self) -> None:
        items = self.batcher.take()
        if not items:
            return
        for block in events.render(items):
            if not self.out.emit(block):
                self.stop.set()
                return
        self.pushed += 1
        self.log.info("pushed %s utterance(s) as event %s", len(items), self.pushed)
        self.write_state()


def main(args) -> int:
    config.ensure_dirs()
    cfg = config.load()
    log = make_logger()
    out = events.EventWriter(sys.stdout)
    code = Listener(cfg, args, out, log).run()
    # Leave without running interpreter finalisers. Everything worth keeping is
    # already on disk, and an audio library's exit handler that hangs would
    # keep this process, and the microphone indicator, alive.
    logging.shutdown()
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    os._exit(code)
