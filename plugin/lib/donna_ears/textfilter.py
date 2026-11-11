"""Turn raw Whisper output into text worth delivering, or reject it.

Whisper was trained on subtitled video. Given a clip with no real speech in it
(a cough, a keyboard, a door) it tends to answer with the things subtitles say
when nobody is talking: "Thank you.", "Thanks for watching!", a subtitle
credit, a bracketed sound description. Voice activity detection keeps most of
those clips away from the model; this module catches the ones that get through.

Standard library only.
"""

from __future__ import annotations

import re

# "[BLANK_AUDIO]", "(keyboard clicking)", "*sighs*", music notes.
_ANNOTATION = re.compile(
    r"\[[^\]]{0,60}\]|\([^)]{0,60}\)|（[^）]{0,60}）|【[^】]{0,60}】|\*[^*]{1,40}\*|[♪♫]+"
)
_SPACE = re.compile(r"\s+")
_NOT_WORD = re.compile(r"[^\w\s]", re.UNICODE)

# Whole-utterance hallucinations, compared after lowercasing and stripping
# punctuation. A person who really says one of these loses nothing by it being
# dropped: none of them carries a request or a fact.
_PHANTOMS = frozenset(
    {
        # English
        "thank you",
        "thank you very much",
        "thank you so much",
        "thanks",
        "thanks for watching",
        "thank you for watching",
        "thanks for listening",
        "thank you for listening",
        "please subscribe",
        "subscribe",
        "like and subscribe",
        "bye",
        "bye bye",
        "goodbye",
        "see you next time",
        "see you in the next video",
        "ill see you in the next video",
        "ill see you next time",
        "the end",
        "you",
        "so",
        "oh",
        "uh",
        "um",
        "hmm",
        "mm",
        "mmhmm",
        # Portuguese
        "obrigado",
        "obrigada",
        "muito obrigado",
        "muito obrigada",
        "obrigado por assistir",
        "obrigada por assistir",
        "tchau",
        "tchau tchau",
        "e aí",
        "até a próxima",
        "até à próxima",
        "inscrevase no canal",
        # Spanish, French, German
        "gracias",
        "muchas gracias",
        "gracias por ver el video",
        "suscríbete",
        "merci",
        "merci beaucoup",
        "vielen dank",
        # Japanese, Chinese, Russian
        "ご視聴ありがとうございました",
        "谢谢观看",
        "продолжение следует",
    }
)

# Subtitle credits, in any language: reject anything that contains one.
_CREDIT = re.compile(
    r"amara\s*\.?\s*org|subtitles? by|captions? by|transcri(?:bed|ption) by|"
    r"legendas? (?:pela|por|da) |legendado por|subt[ií]tulos? (?:realizados|por)|"
    r"sous-?titres? (?:r[ée]alis[ée]s|par)|untertitel (?:der|von|im auftrag)|"
    r"dimatorzok",
    re.IGNORECASE,
)


def normalise(text: str) -> str:
    """Lowercase, punctuation removed, single spaces: the form phantoms are stored in."""
    return _SPACE.sub(" ", _NOT_WORD.sub("", text.lower())).strip()


def strip_annotations(text: str) -> str:
    return _SPACE.sub(" ", _ANNOTATION.sub(" ", text)).strip()


def collapse_repeats(text: str, max_unit: int = 8, min_repeats: int = 4) -> str:
    """Collapse a phrase Whisper got stuck repeating down to one occurrence.

    "I'm sorry. I'm sorry. I'm sorry. I'm sorry." becomes "I'm sorry.".
    """
    words = text.split()
    keys = [w.lower().strip(",.!?;:") for w in words]
    out: list[str] = []
    i = 0
    while i < len(words):
        collapsed = False
        for size in range(1, max_unit + 1):
            if i + size > len(words):
                break
            unit = keys[i : i + size]
            repeats = 1
            while keys[i + repeats * size : i + (repeats + 1) * size] == unit:
                repeats += 1
            if repeats >= min_repeats:
                out.extend(words[i : i + size])
                i += repeats * size
                collapsed = True
                break
        if not collapsed:
            out.append(words[i])
            i += 1
    return " ".join(out)


def is_phantom(text: str) -> bool:
    if _CREDIT.search(text):
        return True
    return normalise(text) in _PHANTOMS


def accept(raw: str) -> str | None:
    """Clean ``raw``; return None when there is nothing in it worth delivering."""
    text = strip_annotations(raw.replace("\n", " "))
    if not text:
        return None
    text = collapse_repeats(text)
    if not any(ch.isalnum() for ch in text):
        return None
    if is_phantom(text):
        return None
    return text
