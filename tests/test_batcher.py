from donna_ears import config
from donna_ears.batcher import Batcher, Utterance


def make(clock, **overrides):
    cfg = dict(config.DEFAULTS)
    cfg.update(overrides)
    return Batcher(cfg, clock=clock)


def said(text="hello"):
    return Utterance(text=text, started_at=0.0, ended_at=1.0)


def idle(batcher, quiet_for):
    return batcher.due(speech_active=False, stt_busy=False, quiet_for=quiet_for)


# ------------------------------------------------------------------- live


def test_nothing_pending_is_never_due(clock):
    batcher = make(clock)
    assert not idle(batcher, quiet_for=999)


def test_live_waits_for_a_pause(clock):
    batcher = make(clock, pause_s=2.0)
    batcher.add(said())
    assert not idle(batcher, quiet_for=1.9)
    assert idle(batcher, quiet_for=2.0)


def test_live_holds_while_more_text_is_on_its_way(clock):
    batcher = make(clock)
    batcher.add(said())
    assert not batcher.due(speech_active=True, stt_busy=False, quiet_for=0.0)
    assert not batcher.due(speech_active=False, stt_busy=True, quiet_for=5.0)


def test_a_monologue_is_delivered_in_chunks(clock):
    batcher = make(clock, max_wait_s=30.0)
    batcher.add(said())
    clock.advance(29.0)
    assert not batcher.due(speech_active=True, stt_busy=False, quiet_for=0.0)
    clock.advance(1.0)
    assert batcher.due(speech_active=True, stt_busy=False, quiet_for=0.0)


def test_take_empties_the_batch_and_keeps_order(clock):
    batcher = make(clock)
    batcher.add(said("one"))
    batcher.add(said("two"))
    assert [u.text for u in batcher.take()] == ["one", "two"]
    assert len(batcher) == 0
    assert batcher.take() == []
    assert not idle(batcher, quiet_for=999)


def test_live_pushes_are_spaced_apart(clock):
    batcher = make(clock, min_interval_s=3.0)
    batcher.add(said())
    assert idle(batcher, quiet_for=5.0)
    batcher.take()
    batcher.add(said())
    clock.advance(2.9)
    assert not idle(batcher, quiet_for=5.0)
    clock.advance(0.2)
    assert idle(batcher, quiet_for=5.0)


def test_a_burst_of_pushes_widens_the_spacing_then_it_recovers(clock):
    batcher = make(clock, min_interval_s=3.0, busy_events=5, busy_interval_s=15.0)
    for _ in range(5):
        batcher.add(said())
        batcher.take()
        clock.advance(4.0)
    assert batcher.spacing() == 15.0
    batcher.add(said())
    assert not idle(batcher, quiet_for=5.0)  # only 4 s since the last push
    clock.advance(11.5)
    assert idle(batcher, quiet_for=5.0)
    clock.advance(60.0)  # a quiet minute later the burst is forgotten
    assert batcher.spacing() == 3.0


def test_an_empty_take_does_not_count_as_a_push(clock):
    batcher = make(clock, busy_events=2)
    for _ in range(10):
        batcher.take()
    assert batcher.spacing() == float(config.DEFAULTS["min_interval_s"])


# ------------------------------------------------------------------ notes


def test_notes_waits_a_full_interval_even_through_long_silences(clock):
    batcher = make(clock, mode="notes", notes_interval_s=60.0)
    batcher.add(said())
    clock.advance(59.0)
    assert not idle(batcher, quiet_for=59.0)
    clock.advance(1.0)
    assert idle(batcher, quiet_for=60.0)


def test_notes_prefers_to_cut_at_a_pause_but_does_not_hold_forever(clock):
    batcher = make(clock, mode="notes", notes_interval_s=60.0)
    batcher.add(said())
    clock.advance(61.0)
    assert not batcher.due(speech_active=True, stt_busy=False, quiet_for=0.0)
    clock.advance(29.0)
    assert batcher.due(speech_active=True, stt_busy=False, quiet_for=0.0)


def test_the_mode_can_change_while_utterances_are_pending(clock):
    batcher = make(clock, mode="notes", notes_interval_s=60.0)
    batcher.add(said())
    clock.advance(5.0)
    assert not idle(batcher, quiet_for=5.0)
    batcher.mode = "live"
    assert idle(batcher, quiet_for=5.0)
