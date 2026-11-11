import pytest

from donna_ears import textfilter


@pytest.mark.parametrize(
    "raw, expected",
    [
        (" Hey Claude, can you run the tests?", "Hey Claude, can you run the tests?"),
        ("first line\nsecond line", "first line second line"),
        ("  lots   of   space  ", "lots of space"),
        ("[BLANK_AUDIO] the build is green", "the build is green"),
        ("(keyboard clicking) ship it", "ship it"),
        ("♪ deploy on Friday ♪", "deploy on Friday"),
    ],
)
def test_real_speech_is_cleaned_and_kept(raw, expected):
    assert textfilter.accept(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        ".",
        " . . . ",
        "?",
        "[BLANK_AUDIO]",
        "(silence)",
        "[Música]",
        "*sighs*",
        "Thank you.",
        "Thanks for watching!",
        "Thank you for watching.",
        "you",
        "Bye.",
        "Obrigado.",
        "Obrigada por assistir!",
        "Legendas pela comunidade Amara.org",
        "Subtitles by the Amara.org community",
        "Sous-titres réalisés para la communauté d'Amara.org",
        "ご視聴ありがとうございました",
    ],
)
def test_whisper_hallucinations_are_rejected(raw):
    assert textfilter.accept(raw) is None


@pytest.mark.parametrize("raw", ["Yes.", "No.", "Stop.", "Okay.", "Wait, not that file.", "Sim."])
def test_short_replies_that_mean_something_survive(raw):
    assert textfilter.accept(raw) == raw


def test_a_phantom_phrase_inside_a_real_sentence_is_kept():
    text = "Thank you for fixing that, now run the linter."
    assert textfilter.accept(text) == text


def test_a_stuck_repetition_collapses_to_one_occurrence():
    assert textfilter.collapse_repeats("I'm sorry. " * 9).strip() == "I'm sorry."
    assert textfilter.collapse_repeats("the the the the the build") == "the build"


def test_ordinary_repetition_is_left_alone():
    text = "no no, I said the other one, the other one"
    assert textfilter.collapse_repeats(text) == text


def test_repetition_that_collapses_into_a_phantom_is_rejected():
    assert textfilter.accept("Thank you. Thank you. Thank you. Thank you. Thank you.") is None
