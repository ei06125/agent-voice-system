import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from donna.orchestration.voice_queue import VoiceQueue
from donna.speech_to_text import listen_daemon


class RecordingBuffer:
    def __init__(self) -> None:
        self.lines = []
        self.drained = False

    def append(self, text: str) -> None:
        self.lines.append(text)

    def was_drained(self) -> bool:
        return self.drained

    def reset(self) -> None:
        self.lines.clear()


def _state() -> dict:
    return {
        "buffering": False,
        "stop": False,
        "buffer_started_at": None,
        "capture_id": None,
        "utterance_seq": 0,
        "pending_trigger_timer": None,
    }


def test_listener_mirrors_authorized_utterances_to_shadow_queue():
    with tempfile.TemporaryDirectory() as state_dir:
        queue = VoiceQueue(Path(state_dir) / "queue.sqlite3")
        buffer = RecordingBuffer()
        state = _state()
        original_afplay = listen_daemon.afplay_async
        original_disable_flag = listen_daemon.DISABLE_FLAG
        listen_daemon.afplay_async = lambda _path: None
        listen_daemon.DISABLE_FLAG = str(Path(state_dir) / "disabled")
        try:
            listen_daemon.handle_utterance(
                "Oh my god Donna first command",
                state,
                buffer,
                queue,
                vad_ended_at=100.0,
            )
            listen_daemon.handle_utterance(
                "second command",
                state,
                buffer,
                queue,
                vad_ended_at=101.0,
            )
        finally:
            listen_daemon.afplay_async = original_afplay
            listen_daemon.DISABLE_FLAG = original_disable_flag

        messages = queue.list_messages(include_text=True)
        assert buffer.lines == ["first command", "second command"]
        assert [message["text"] for message in messages] == buffer.lines
        assert [message["utterance_seq"] for message in messages] == [1, 2]
        assert [message["vad_ended_at"] for message in messages] == [100.0, 101.0]
        assert len({message["capture_id"] for message in messages}) == 1


def test_shadow_queue_failure_preserves_legacy_buffer_delivery():
    class FailingQueue:
        def enqueue_and_notify(self, **_kwargs):
            raise sqlite3.OperationalError("injected failure")

    buffer = RecordingBuffer()
    state = _state()
    state["buffering"] = True
    state["capture_id"] = "capture-a"

    listen_daemon.handle_utterance(
        "legacy still receives this",
        state,
        buffer,
        FailingQueue(),
        vad_ended_at=100.0,
    )

    assert buffer.lines == ["legacy still receives this"]


def test_wake_only_arms_exactly_one_following_queue_message():
    with tempfile.TemporaryDirectory() as state_dir:
        queue = VoiceQueue(Path(state_dir) / "queue.sqlite3")
        buffer = RecordingBuffer()
        state = _state()
        original_afplay = listen_daemon.afplay_async
        original_disable_flag = listen_daemon.DISABLE_FLAG
        listen_daemon.afplay_async = lambda _path: None
        listen_daemon.DISABLE_FLAG = str(Path(state_dir) / "disabled")
        try:
            listen_daemon.handle_utterance(
                "Oh my god Donna",
                state,
                buffer,
                queue,
                vad_ended_at=100.0,
            )
            assert queue.list_messages() == []
            listen_daemon.handle_utterance(
                "command after a short pause",
                state,
                buffer,
                queue,
                vad_ended_at=101.0,
            )
        finally:
            listen_daemon.afplay_async = original_afplay
            listen_daemon.DISABLE_FLAG = original_disable_flag

        messages = queue.list_messages(include_text=True)
        assert buffer.lines == ["command after a short pause"]
        assert len(messages) == 1
        assert messages[0]["text"] == "command after a short pause"
        assert messages[0]["utterance_seq"] == 1


def _patch_deliver_mode(state_dir):
    """Shared setup for the deliver-mode continuous-log tests: isolates
    every path listen_daemon would otherwise touch on the real
    filesystem. Returns a restore callable for a finally block."""
    originals = {
        "afplay_async": listen_daemon.afplay_async,
        "DISABLE_FLAG": listen_daemon.DISABLE_FLAG,
        "VOICE_QUEUE_MODE": listen_daemon.VOICE_QUEUE_MODE,
        "UTTERANCE_LOG_PATH": listen_daemon.UTTERANCE_LOG_PATH,
        "NAME_MENTIONED_PATH": listen_daemon.NAME_MENTIONED_PATH,
        "FILLERS_DIR": listen_daemon.FILLERS_DIR,
        "TRIGGER_DEBOUNCE_MS": listen_daemon.TRIGGER_DEBOUNCE_MS,
        "TRIGGER_MODE": listen_daemon.TRIGGER_MODE,
    }
    listen_daemon.afplay_async = lambda _path: None
    listen_daemon.DISABLE_FLAG = str(Path(state_dir) / "disabled")
    listen_daemon.VOICE_QUEUE_MODE = "deliver"
    listen_daemon.UTTERANCE_LOG_PATH = Path(state_dir) / "utterance-log.jsonl"
    listen_daemon.NAME_MENTIONED_PATH = Path(state_dir) / "donna-mentioned.json"
    # Real debounce delay would make every trigger-related test slow (and,
    # for the ones that don't care about debounce timing at all, flaky) -
    # tests that specifically exercise the debounce window override this
    # back to something deliberately larger than the default.
    listen_daemon.TRIGGER_DEBOUNCE_MS = 20
    listen_daemon.TRIGGER_MODE = "name"
    # _play_filler() shells out to afplay directly (not via afplay_async),
    # so it isn't covered by that mock above - an empty, real-fillers-free
    # dir makes it a no-op (its own `if not clips: return`) instead of
    # actually spawning afplay on a real clip during a test run (hit live:
    # the deliver-mode "Donna"-mention tests did exactly that the moment
    # FILLERS_DIR gained real files, before this fix).
    listen_daemon.FILLERS_DIR = Path(state_dir) / "fillers-empty"

    def restore():
        for name, value in originals.items():
            setattr(listen_daemon, name, value)

    return restore


class FakePopen:
    def __init__(self, *_args, **_kwargs) -> None:
        self.pid = 999999


def test_play_filler_is_a_noop_with_no_clips():
    """No clips available (fresh install, or - as this test itself relies
    on - an isolated test dir) must not error and must not touch
    subprocess at all."""
    with tempfile.TemporaryDirectory() as state_dir:
        original_dir = listen_daemon.FILLERS_DIR
        listen_daemon.FILLERS_DIR = Path(state_dir) / "empty"
        calls = []
        original_popen = listen_daemon.subprocess.Popen
        listen_daemon.subprocess.Popen = lambda *a, **k: calls.append(1) or FakePopen()
        try:
            listen_daemon._play_filler()
        finally:
            listen_daemon.FILLERS_DIR = original_dir
            listen_daemon.subprocess.Popen = original_popen

        assert calls == []


def test_play_filler_picks_a_clip_and_registers_playback():
    """Regression for the immediate-local-filler feature: must actually
    play *something* (not silently no-op) when clips exist, and must
    register with playback_control so a later barge-in can still kill
    it - same contract every other playback source uses. Mocks
    subprocess.Popen entirely so this never spawns a real afplay process
    or plays real audio during a test run (hit live: before FILLERS_DIR
    was added to _patch_deliver_mode's isolation, these deliver-mode
    "Donna"-mention tests actually played real filler clips)."""
    with tempfile.TemporaryDirectory() as state_dir:
        fillers_dir = Path(state_dir) / "fillers"
        fillers_dir.mkdir()
        (fillers_dir / "filler-1.wav").write_bytes(b"fake")

        original_dir = listen_daemon.FILLERS_DIR
        listen_daemon.FILLERS_DIR = fillers_dir
        popen_calls = []
        original_popen = listen_daemon.subprocess.Popen
        listen_daemon.subprocess.Popen = lambda *a, **k: (
            popen_calls.append(a)
        ) or FakePopen()
        registered = []
        original_register = listen_daemon.register_playback
        listen_daemon.register_playback = lambda pid: registered.append(pid)
        try:
            listen_daemon._play_filler()
        finally:
            listen_daemon.FILLERS_DIR = original_dir
            listen_daemon.subprocess.Popen = original_popen
            listen_daemon.register_playback = original_register

        assert len(popen_calls) == 1
        assert "afplay" in popen_calls[0][0]
        assert registered == [999999]


def test_deliver_mode_logs_every_utterance_no_wake_phrase_needed():
    """The new design (replacing wake-per-command): every finalized
    utterance is logged unconditionally, with no wake phrase and no
    per-command capture state - a mid-thought pause must not lose or cut
    off anything, unlike the old design this replaced."""
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        log_path = listen_daemon.UTTERANCE_LOG_PATH
        try:
            listen_daemon.handle_utterance(
                "first sentence with no wake phrase at all",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
            )
            listen_daemon.handle_utterance(
                "second sentence after a pause",
                state,
                buffer,
                None,
                vad_ended_at=105.0,
            )
        finally:
            restore()

        lines = log_path.read_text().splitlines()
        records = [json.loads(line) for line in lines]
        assert [r["text"] for r in records] == [
            "first sentence with no wake phrase at all",
            "second sentence after a pause",
        ]
        assert records[0]["ts"] == 100.0
        assert records[1]["ts"] == 105.0
        assert records[0]["playback_overlap"] is False
        # No legacy buffer writes and no capture state in this mode.
        assert buffer.lines == []
        assert state["buffering"] is False


def test_deliver_mode_marks_playback_overlap_in_log():
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        log_path = listen_daemon.UTTERANCE_LOG_PATH
        try:
            listen_daemon.handle_utterance(
                "speaker echo",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
                playback_overlap=True,
            )
        finally:
            restore()

        record = json.loads(log_path.read_text())
        assert record["playback_overlap"] is True


def test_deliver_mode_trigger_only_on_name_mention():
    """The trigger file (what actually pages a watching Claude session)
    must only appear when the bare name "Donna" is heard - everything
    else still gets logged (previous test), but shouldn't page anyone,
    or ambient noise/video/other people talking would flood the watcher
    exactly like the old wake-phrase-hallucination bug did. Debounced
    (see TRIGGER_DEBOUNCE_MS), so this waits briefly for the pending
    timer to actually fire rather than checking synchronously."""
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        try:
            listen_daemon.handle_utterance(
                "just some ambient chatter",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
            )
            assert not listen_daemon.NAME_MENTIONED_PATH.exists()

            listen_daemon.handle_utterance(
                "hey Donna what time is it",
                state,
                buffer,
                None,
                vad_ended_at=101.0,
            )
            assert not listen_daemon.NAME_MENTIONED_PATH.exists(), (
                "must not fire before the debounce window elapses"
            )
            state["pending_trigger_timer"].join(timeout=1)

            assert listen_daemon.NAME_MENTIONED_PATH.exists()
            data = json.loads(listen_daemon.NAME_MENTIONED_PATH.read_text())
            assert data["trigger_text"] == "hey Donna what time is it"

            # Both utterances are still in the log regardless of the
            # trigger - the trigger is a ping, not a content filter.
            lines = listen_daemon.UTTERANCE_LOG_PATH.read_text().splitlines()
            assert len(lines) == 2
        finally:
            restore()


def test_always_trigger_mode_fires_after_each_speech_interval():
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        listen_daemon.TRIGGER_MODE = "always"
        try:
            listen_daemon.handle_utterance(
                "what is two plus two",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
            )
            state["pending_trigger_timer"].join(timeout=1)

            assert listen_daemon.NAME_MENTIONED_PATH.exists()
            data = json.loads(listen_daemon.NAME_MENTIONED_PATH.read_text())
            assert data["trigger_text"] == "what is two plus two"
        finally:
            restore()


def test_always_trigger_mode_does_not_trigger_on_playback_echo():
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        listen_daemon.TRIGGER_MODE = "always"
        try:
            listen_daemon.handle_utterance(
                "assistant output from speaker",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
                playback_overlap=True,
            )

            assert state["pending_trigger_timer"] is None
            assert not listen_daemon.NAME_MENTIONED_PATH.exists()
        finally:
            restore()


def test_continued_speech_without_name_extends_debounce_window():
    """The actual fix requested live: saying "Donna" and then continuing
    a thought (no repeated name needed) must push the trigger back, not
    fire mid-sentence - reproduces "Alright Donna, so for the" / "Myth."
    being one interrupted thought, not two, and confirms the *final*
    utterance's text is what ends up in the trigger."""
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        try:
            listen_daemon.handle_utterance(
                "Donna, so for the",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
            )
            first_timer = state["pending_trigger_timer"]
            assert not listen_daemon.NAME_MENTIONED_PATH.exists()

            # Continues the thought - no "Donna" this time - before the
            # short test debounce window elapses.
            listen_daemon.handle_utterance(
                "filler word idea specifically",
                state,
                buffer,
                None,
                vad_ended_at=100.3,
            )
            second_timer = state["pending_trigger_timer"]

            assert first_timer is not second_timer, (
                "must cancel and reschedule, not let the first timer fire mid-thought"
            )
            assert not listen_daemon.NAME_MENTIONED_PATH.exists()

            second_timer.join(timeout=1)
            assert listen_daemon.NAME_MENTIONED_PATH.exists()
            data = json.loads(listen_daemon.NAME_MENTIONED_PATH.read_text())
            assert data["trigger_text"] == "filler word idea specifically"
        finally:
            restore()


def test_unrelated_speech_after_a_trigger_has_fired_does_not_retrigger():
    """Regression: state["pending_trigger_timer"] must be cleared back to
    None once a timer actually fires - a completed Timer object is still
    not None, so without clearing it, handle_utterance's
    `name_mentioned or state.get("pending_trigger_timer") is not None`
    check treats every utterance from then on, forever, as "still within
    a pending window" and re-triggers on content with no "Donna" mention
    at all (hit live: "This is it, Donna." correctly fired, then "This is
    the final test." - no name in it - incorrectly fired again)."""
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        try:
            listen_daemon.handle_utterance(
                "hey Donna", state, buffer, None, vad_ended_at=100.0
            )
            state["pending_trigger_timer"].join(timeout=1)
            assert listen_daemon.NAME_MENTIONED_PATH.exists()
            assert state["pending_trigger_timer"] is None

            listen_daemon.NAME_MENTIONED_PATH.unlink()
            listen_daemon.handle_utterance(
                "completely unrelated later sentence",
                state,
                buffer,
                None,
                vad_ended_at=105.0,
            )
            # No timer scheduled at all this time, so nothing to wait on.
            assert state["pending_trigger_timer"] is None
            assert not listen_daemon.NAME_MENTIONED_PATH.exists()
        finally:
            restore()


def test_name_mention_cancels_current_playback():
    """Regression for barge-in: hearing "Donna" again while a reply is
    playing must kill it immediately, not wait for it to finish."""
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        calls = []
        original_cancel = listen_daemon.cancel_playback
        listen_daemon.cancel_playback = lambda: calls.append(1)
        try:
            listen_daemon.handle_utterance(
                "hey Donna stop talking",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
            )
        finally:
            listen_daemon.cancel_playback = original_cancel
            restore()

        assert calls == [1]


def test_mute_matches_bare_donna_prefix_not_just_omg():
    """Regression: hit live as "Okay, Donna, mute" failing silently -
    MUTE_RE only accepted the old "OMG mute" phrasing, not the bare-name
    convention deliver mode's content trigger already uses. Must accept
    both forms; must still reject the bare word with no address at all
    (the original reason for requiring a prefix instead of bare "mute")."""
    assert listen_daemon.MUTE_RE.search("Okay, Donna, mute.")
    assert listen_daemon.MUTE_RE.search("Donna mute")
    assert listen_daemon.MUTE_RE.search("OMG Donna mute")
    assert not listen_daemon.MUTE_RE.search("just some ambient mute talk")


def test_mute_cancels_current_playback():
    """Regression: "mute" previously only prevented *future* speak()
    calls - a reply already mid-playback kept talking right over you,
    since nothing told afplay to stop. Must kill in-progress playback
    immediately, same as hearing "Donna" again mid-reply."""
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        original_mute_flag = listen_daemon.MUTE_FLAG
        listen_daemon.MUTE_FLAG = str(Path(state_dir) / "mute")
        calls = []
        original_cancel = listen_daemon.cancel_playback
        listen_daemon.cancel_playback = lambda: calls.append(1)
        try:
            listen_daemon.handle_utterance(
                "OMG mute",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
            )
        finally:
            listen_daemon.MUTE_FLAG = original_mute_flag
            listen_daemon.cancel_playback = original_cancel

        assert calls == [1]


def test_no_name_mention_does_not_touch_playback():
    """The flip side: ambient content with no "Donna" mention must not
    call cancel_playback() at all - only a real trigger should ever
    touch an in-progress reply."""
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        calls = []
        original_cancel = listen_daemon.cancel_playback
        listen_daemon.cancel_playback = lambda: calls.append(1)
        try:
            listen_daemon.handle_utterance(
                "just ambient chatter",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
            )
        finally:
            listen_daemon.cancel_playback = original_cancel
            restore()

        assert calls == []


def test_deliver_mode_skips_playback_gate_shadow_mode_keeps_it():
    """Regression for the barge-in redesign: deliver mode must keep
    listening during TTS playback (so hearing "Donna" again can interrupt
    it); shadow/off keep the original hard gate (self-feedback prevention
    - a real, previously hit-live bug for those modes' single-command
    semantics)."""
    original_is_active = listen_daemon.is_playback_active
    listen_daemon.is_playback_active = lambda: True
    try:
        assert listen_daemon._should_gate_for_playback("deliver") is False
        assert listen_daemon._should_gate_for_playback("shadow") is True
        assert listen_daemon._should_gate_for_playback("off") is True
    finally:
        listen_daemon.is_playback_active = original_is_active

    # And when nothing is actually playing, no mode gates.
    listen_daemon.is_playback_active = lambda: False
    try:
        assert listen_daemon._should_gate_for_playback("deliver") is False
        assert listen_daemon._should_gate_for_playback("shadow") is False
    finally:
        listen_daemon.is_playback_active = original_is_active


def test_deliver_mode_mute_deaf_are_controls_not_logged():
    """Control keywords must still short-circuit before logging - they
    are commands to the daemon, not conversational content."""
    with tempfile.TemporaryDirectory() as state_dir:
        buffer = RecordingBuffer()
        state = _state()
        restore = _patch_deliver_mode(state_dir)
        mute_flag = Path(state_dir) / "mute"
        original_mute_flag = listen_daemon.MUTE_FLAG
        listen_daemon.MUTE_FLAG = str(mute_flag)
        log_path = listen_daemon.UTTERANCE_LOG_PATH
        try:
            listen_daemon.handle_utterance(
                "OMG mute",
                state,
                buffer,
                None,
                vad_ended_at=100.0,
            )
            assert mute_flag.exists()
        finally:
            listen_daemon.MUTE_FLAG = original_mute_flag
            restore()

        assert not log_path.exists()


if __name__ == "__main__":
    for name, function in list(globals().items()):
        if name.startswith("test_") and callable(function):
            function()
            print(f"PASS {name}")
