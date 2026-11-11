import io
import time

from donna_ears import events
from donna_ears.batcher import Utterance

# What Claude Code does to monitor output (see the module docstring).
HARNESS_LINE_LIMIT = 500
HARNESS_EVENT_LIMIT = 3000


def said(text, at=0.0, language="en"):
    return Utterance(text=text, started_at=at, ended_at=at + 1, language=language)


def test_one_utterance_is_one_line_with_its_local_time():
    at = time.mktime((2026, 11, 10, 22, 41, 7, 0, 0, -1))
    assert events.render([said("run the tests", at=at)]) == ["[heard 22:41:07] run the tests"]


def test_the_language_is_shown_only_when_it_is_not_english():
    at = time.mktime((2026, 11, 10, 9, 5, 0, 0, 0, -1))
    assert events.render([said("olá", at=at, language="pt")]) == ["[heard 09:05:00 pt] olá"]
    assert events.render([said("hello", at=at, language="")]) == ["[heard 09:05:00] hello"]


def test_utterances_in_one_batch_share_one_event_in_order():
    (block,) = events.render([said("first"), said("second"), said("third")])
    lines = block.split("\n")
    assert [line.split("] ", 1)[1] for line in lines] == ["first", "second", "third"]


def test_long_speech_is_wrapped_under_the_line_limit_without_losing_words():
    words = [f"word{i}" for i in range(200)]  # about 1500 characters: one event, several lines
    (block,) = events.render([said(" ".join(words))])
    lines = block.split("\n")
    assert len(lines) > 1
    assert all(len(line) <= HARNESS_LINE_LIMIT for line in lines)
    assert lines[0].startswith("[heard ") and lines[1].startswith("[heard cont.] ")
    recovered = " ".join(line.split("] ", 1)[1] for line in lines).split()
    assert recovered == words


def test_a_huge_batch_is_split_into_events_under_the_event_limit():
    batch = [said(" ".join(["talking"] * 60)) for _ in range(20)]  # ~9600 characters
    blocks = events.render(batch)
    assert len(blocks) > 1
    assert all(len(block) <= HARNESS_EVENT_LIMIT for block in blocks)
    assert sum(block.count("[heard ") for block in blocks) >= 20


def test_one_unbroken_token_is_hard_split():
    (block,) = events.render([said("x" * 1200)])
    lines = block.split("\n")
    assert all(len(line) <= HARNESS_LINE_LIMIT for line in lines)
    assert sum(line.count("x") for line in lines) == 1200


def test_nothing_renders_to_no_events():
    assert events.render([]) == []


# ------------------------------------------------------------------ writer


def test_an_event_is_written_as_one_flushed_burst(clock):
    stream = io.StringIO()
    writer = events.EventWriter(stream, clock=clock, sleep=clock.sleep)
    assert writer.emit("[heard 10:00:00] one\n\n[heard 10:00:02] two")
    assert stream.getvalue() == "[heard 10:00:00] one\n[heard 10:00:02] two\n"
    assert writer.count == 1


def test_events_are_kept_apart_so_the_harness_does_not_merge_them(clock):
    stream = io.StringIO()
    writer = events.EventWriter(stream, clock=clock, sleep=clock.sleep)
    writer.emit("a")
    writer.emit("b")
    assert clock.slept and clock.slept[0] >= 0.2  # the harness merges within 200 ms


class HarnessMeter:
    """Claude Code's token bucket for monitor events, as shipped in 2.1.322:
    ten tokens to start, one returned per whole 2 s elapsed, excess dropped."""

    def __init__(self, clock, capacity=10, refill_s=2.0):
        self.clock, self.capacity, self.refill_s = clock, capacity, refill_s
        self.tokens = capacity
        self.mark = clock()

    def try_consume(self):
        gained = int((self.clock() - self.mark) // self.refill_s)
        if gained > 0:
            self.tokens = min(self.capacity, self.tokens + gained)
            self.mark += gained * self.refill_s
        if self.tokens > 0:
            self.tokens -= 1
            return True
        return False


def test_the_writer_never_outruns_the_harness_meter(clock):
    meter = HarnessMeter(clock)
    accepted = []

    class Harness:
        def write(self, text):
            accepted.append(meter.try_consume())

        def flush(self):
            pass

    writer = events.EventWriter(Harness(), clock=clock, sleep=clock.sleep)
    for burst in range(4):  # four floods with idle gaps of different lengths between
        for index in range(40):
            assert writer.emit(f"event {burst}.{index}")
        clock.advance(7.0 * burst)
    assert len(accepted) == 160
    assert all(accepted), "the harness would have dropped an event"


def test_a_closed_pipe_marks_the_writer_broken():
    class Closed:
        def write(self, _):
            raise BrokenPipeError

        def flush(self):
            pass

    writer = events.EventWriter(Closed())
    assert writer.emit("anyone there?") is False
    assert writer.broken
    assert writer.emit("still no") is False


def test_listener_messages_are_marked_as_coming_from_the_listener(clock):
    stream = io.StringIO()
    writer = events.EventWriter(stream, clock=clock, sleep=clock.sleep)
    writer.error("cannot open the microphone")
    writer.notice("downloading the model")
    first, second = stream.getvalue().splitlines()
    assert first == "[ears error] cannot open the microphone"
    assert second == "[ears] downloading the model"
