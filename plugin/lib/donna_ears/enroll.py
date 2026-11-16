"""`ears enroll`: turn clips of the owner's speech into a voiceprint.

It reads audio files the owner recorded on purpose; it never opens the
microphone. One device at a time, because each microphone colours a voice
differently (see .claude/research/voice-biometrics.md §5.3).

Runs in the listener's environment (numpy, onnxruntime), through uv.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import audio, speaker, speaker_models, voiceprint

MIN_CLIP_S = 1.0
SILENT_RMS = 1e-3  # about -60 dBFS
CLIPPED_FRACTION = 0.01
ADVISED_TOTAL_S = 30.0


def make_embedder(path: Path):
    return speaker.Embedder(path)


@dataclass
class Report:
    device: str
    model: str
    used: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    seconds: float = 0.0
    consistency: list[tuple[str, float]] = field(default_factory=list)


class EnrollError(RuntimeError):
    pass


def _problem(samples) -> str | None:
    import numpy as np

    if len(samples) < MIN_CLIP_S * audio.SAMPLE_RATE:
        return f"shorter than {MIN_CLIP_S:g} s"
    if float(np.sqrt(np.mean(np.square(samples)))) < SILENT_RMS:
        return "silent"
    if float(np.mean(np.abs(samples) >= 0.999)) > CLIPPED_FRACTION:
        return "clipped (too loud)"
    return None


def enroll(
    files: list[str], device: str, model_name: str = speaker_models.DEFAULT, replace: bool = False
) -> Report:
    """Enroll `device` from `files` and save the voiceprint; EnrollError if not possible."""
    try:
        existing = voiceprint.load()
    except voiceprint.ProfileError as exc:
        if not replace:
            raise EnrollError(f"{exc} (or pass --replace to start a new voiceprint)") from None
        existing = None

    try:
        model_path = speaker_models.ensure(model_name)
    except speaker_models.ModelError as exc:
        raise EnrollError(str(exc)) from None
    embedder = make_embedder(model_path)
    model = voiceprint.ModelId(
        name=model_name,
        sha256=speaker_models.MODELS[model_name].sha256,
        preprocessing=embedder.preprocessing,
    )
    if existing is not None and not replace:
        try:
            existing.check(model)
        except voiceprint.ProfileError as exc:
            raise EnrollError(
                f"{exc} every device with --replace, or delete the old voiceprint"
            ) from None
    profile = existing if existing is not None and not replace else voiceprint.Profile.new(model)

    report = Report(device=device, model=model_name)
    vectors = []
    for name in files:
        label = Path(name).name
        try:
            samples = audio.decode_file(name)
        except audio.SourceError as exc:
            report.skipped.append((label, str(exc)))
            continue
        problem = _problem(samples)
        if problem is None:
            try:
                vectors.append(embedder.embed(samples))
            except speaker.SpeakerError as exc:
                problem = str(exc)
        if problem:
            report.skipped.append((label, problem))
            continue
        report.used.append(label)
        report.seconds += len(samples) / audio.SAMPLE_RATE
    if not vectors:
        raise EnrollError("no clip had usable speech; nothing was enrolled")

    centre = speaker.centroid(vectors)
    report.consistency = [
        (label, speaker.score(vector, centre))
        for label, vector in zip(report.used, vectors, strict=True)
    ]
    profile.enroll(device, vectors, seconds=report.seconds)
    voiceprint.save(profile)
    return report


def describe(report: Report) -> list[str]:
    """What happened, without the voiceprint itself."""
    lines = [
        f"enrolled {report.device!r}: {len(report.used)} clip(s), "
        f"{report.seconds:.0f} s of audio, model {report.model}"
    ]
    for label, why in report.skipped:
        lines.append(f"  skipped {label}: {why}")
    if len(report.consistency) > 1:
        scores = sorted(score for _, score in report.consistency)
        median = scores[len(scores) // 2]
        lines.append(f"  clip agreement: lowest {scores[0]:.2f}, median {median:.2f}")
        for label, score in report.consistency:
            if score < median - 0.2:
                lines.append(f"  {label} sounds unlike the others ({score:.2f}): check it")
    if report.seconds < ADVISED_TOTAL_S:
        lines.append(
            f"  note: {ADVISED_TOTAL_S:.0f} s or more of speech makes a steadier voiceprint"
        )
    lines.append("  the voiceprint is not used to filter speech until this device is calibrated")
    return lines
