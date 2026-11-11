from donna_ears.segmenter import FRAME_SECONDS, Segmenter

SPEECH, QUIET, UNSURE = 0.9, 0.01, 0.42


def feed(segmenter, probabilities, start=100.0):
    """Push one numbered frame per probability; collect finished segments."""
    finished = []
    now = start
    for index, probability in enumerate(probabilities):
        now = start + (index + 1) * FRAME_SECONDS
        segment = segmenter.push(index, probability, now)
        if segment is not None:
            finished.append(segment)
    return finished, now


def make(**overrides):
    settings = dict(
        threshold=0.5, min_silence_ms=640, pre_roll_ms=320, min_speech_ms=256, max_utterance_s=28
    )
    settings.update(overrides)
    return Segmenter(**settings)


def test_an_utterance_ends_after_enough_silence():
    segmenter = make()  # 20 frames of silence end it, 10 frames of pre-roll
    finished, _ = feed(segmenter, [QUIET] * 15 + [SPEECH] * 30 + [QUIET] * 20)
    assert len(finished) == 1
    segment = finished[0]
    # 10 frames of pre-roll + 30 voiced + a 200 ms (6 frame) tail of the silence
    assert segment.frames == list(range(5, 15)) + list(range(15, 45)) + list(range(45, 51))
    assert abs(segment.voiced_seconds - 30 * FRAME_SECONDS) < 1e-9
    assert not segmenter.in_speech


def test_start_time_reaches_back_over_the_pre_roll():
    segmenter = make()
    finished, _ = feed(segmenter, [QUIET] * 15 + [SPEECH] * 30 + [QUIET] * 20, start=100.0)
    # speech was detected at frame 15 (time 100 + 16 frames); 10 frames of pre-roll precede it
    expected = 100.0 + 16 * FRAME_SECONDS - 11 * FRAME_SECONDS
    assert abs(finished[0].started_at - expected) < 1e-6
    assert finished[0].ended_at > finished[0].started_at


def test_a_blip_is_discarded():
    segmenter = make()  # needs 8 voiced frames
    finished, _ = feed(segmenter, [QUIET] * 5 + [SPEECH] * 3 + [QUIET] * 25)
    assert finished == []
    assert not segmenter.in_speech


def test_uncertain_frames_neither_extend_nor_end_speech():
    segmenter = make()
    # A pause full of in-between probabilities must not split the sentence.
    finished, _ = feed(segmenter, [SPEECH] * 12 + [UNSURE] * 40 + [SPEECH] * 12 + [QUIET] * 20)
    assert len(finished) == 1
    assert abs(finished[0].voiced_seconds - 24 * FRAME_SECONDS) < 1e-9


def test_a_short_pause_does_not_split_an_utterance():
    segmenter = make()
    finished, _ = feed(segmenter, [SPEECH] * 12 + [QUIET] * 10 + [SPEECH] * 12 + [QUIET] * 20)
    assert len(finished) == 1


def test_endless_speech_is_cut_at_the_maximum_length():
    segmenter = make(max_utterance_s=1.6)  # 50 frames
    finished, _ = feed(segmenter, [SPEECH] * 120)
    assert [len(segment.frames) for segment in finished] == [50, 50]
    assert segmenter.in_speech  # the remaining 20 frames are a third utterance in progress


def test_flush_hands_over_the_utterance_in_progress():
    segmenter = make()
    finished, now = feed(segmenter, [SPEECH] * 20)
    assert finished == []
    segment = segmenter.flush(now)
    assert segment is not None and len(segment.frames) == 20
    assert segmenter.flush(now) is None


def test_reset_forgets_everything():
    segmenter = make()
    feed(segmenter, [SPEECH] * 20)
    segmenter.reset()
    assert not segmenter.in_speech
    finished, _ = feed(segmenter, [QUIET] * 30)
    assert finished == []
