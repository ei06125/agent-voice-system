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
    hashes = []
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
        hashes.append(speaker_models.sha256_of(Path(name)))
    if not vectors:
        raise EnrollError("no clip had usable speech; nothing was enrolled")

    centre = speaker.centroid(vectors)
    report.consistency = [
        (label, speaker.score(vector, centre))
        for label, vector in zip(report.used, vectors, strict=True)
    ]
    profile.enroll(device, vectors, seconds=report.seconds, clip_hashes=hashes)
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


# The lengths of speech the listener actually judges, plus each whole clip
# (capped at speaker_max_s, as the listener caps it). 0.5 s windows are
# scored for the report only: shorter than speaker_min_s, never "owner".
CALIBRATION_WINDOWS_S = (0.5, 1.0, 2.0, 4.0, 0.0)
SHORT_S = 0.5
LONG_S = (2.0, 4.0, 0.0)
MAX_RECORDING_S = 120.0  # read at most this much of each recording


@dataclass
class Calibration:
    device: str
    proposal: object  # calibrate.Proposal
    by_length: list[tuple[str, int, float, int, float]]  # label, n owner, median, n other, max
    saved: bool
    problems: list[str] = field(default_factory=list)
    refused: bool = False  # --write asked for, not done because of `problems`


def _trials(embedder, files: list[str], max_s: float) -> dict[float, list]:
    """Embeddings of every window of every recording, keyed by window length."""
    from . import calibrate

    vectors: dict[float, list] = {length: [] for length in CALIBRATION_WINDOWS_S}
    for name in files:
        try:
            samples = audio.decode_file(name)[: int(MAX_RECORDING_S * audio.SAMPLE_RATE)]
        except audio.SourceError as exc:
            raise EnrollError(str(exc)) from None
        for length in CALIBRATION_WINDOWS_S:
            pieces = calibrate.windows(samples, length)
            if length == 0.0:
                pieces = [samples[: int(max_s * audio.SAMPLE_RATE)]]
            for piece in pieces:
                if len(piece) < SHORT_S * audio.SAMPLE_RATE or _problem_window(piece):
                    continue
                try:
                    vectors[length].append(embedder.embed(piece))
                except speaker.SpeakerError:
                    continue
    return vectors


def _problem_window(samples) -> str | None:
    """Like _problem, without the minimum length: windows are cut to size."""
    import numpy as np

    if float(np.sqrt(np.mean(np.square(samples)))) < SILENT_RMS:
        return "silent"
    if float(np.mean(np.abs(samples) >= 0.999)) > CLIPPED_FRACTION:
        return "clipped"
    return None


def calibrate_device(
    device: str,
    owner_files: list[str],
    other_files: list[str],
    write: bool,
    max_s: float = 8.0,
    force: bool = False,
) -> Calibration:
    """Score held-out owner clips and other voices against `device`'s voiceprint."""
    from . import calibrate

    try:
        profile = voiceprint.load()
    except voiceprint.ProfileError as exc:
        raise EnrollError(str(exc)) from None
    entry = profile.device(device) if profile else None
    if entry is None:
        raise EnrollError(f"{device!r} has no voiceprint: enroll it first (ears enroll)")
    try:
        model_path = speaker_models.ensure(profile.model.name)
    except speaker_models.ModelError as exc:
        raise EnrollError(str(exc)) from None
    enrolled = set(entry.clip_hashes)
    reused = [Path(n).name for n in owner_files if speaker_models.sha256_of(Path(n)) in enrolled]
    if reused:
        raise EnrollError(
            f"{', '.join(reused)} were used for enrollment; calibrate with held-out "
            "recordings of your speech"
        )
    embedder = make_embedder(model_path)
    try:
        profile.check(
            voiceprint.ModelId(profile.model.name, profile.model.sha256, embedder.preprocessing)
        )
    except voiceprint.ProfileError as exc:
        raise EnrollError(str(exc)) from None

    mine = _trials(embedder, owner_files, max_s)
    theirs = _trials(embedder, other_files, max_s)
    owner_scores = {k: [speaker.score(v, entry.centroid) for v in vs] for k, vs in mine.items()}
    other_scores = {k: [speaker.score(v, entry.centroid) for v in vs] for k, vs in theirs.items()}

    def pooled(scores):
        return [s for length, values in scores.items() if length != SHORT_S for s in values]

    try:
        proposal = calibrate.propose(
            pooled(owner_scores),
            pooled(other_scores),
            other_recordings=len(other_files),
            others_short=other_scores[SHORT_S],
        )
    except ValueError as exc:
        raise EnrollError(str(exc)) from None
    owner_long = [s for length in LONG_S for s in owner_scores[length]]
    problems = calibrate.problems(proposal, owner_long)

    by_length = []
    for length in CALIBRATION_WINDOWS_S:
        ours, others = sorted(owner_scores[length]), other_scores[length]
        by_length.append(
            (
                f"{length:g} s" if length else "whole clip",
                len(ours),
                ours[len(ours) // 2] if ours else float("nan"),
                len(others),
                max(others) if others else float("nan"),
            )
        )
    save = write and (force or not problems)
    if save:
        profile.calibrate(device, accept=proposal.accept, reject=proposal.reject)
        voiceprint.save(profile)
    return Calibration(
        device, proposal, by_length, saved=save, problems=problems, refused=write and not save
    )


def describe_calibration(result: Calibration) -> list[str]:
    p = result.proposal
    lines = [
        f"calibration for {result.device!r}: {p.owner_trials} trials of your speech, "
        f"{p.other_trials} of other voices",
        f"  equal error rate {p.eer:.1%}",
        "  length       yours  median   others  highest",
    ]
    for label, n_ours, median, n_others, highest in result.by_length:
        lines.append(f"  {label:<11} {n_ours:6d}  {median:6.2f}   {n_others:6d}  {highest:7.2f}")
    lines += [
        f"  proposed: accept at {p.accept:.2f} or above, reject below {p.reject:.2f}",
        f"  with these: {p.owner_rejected:.0%} of your trials rejected, "
        f"{p.owner_unknown:.0%} unknown",
        f"  false accepts in this data: none; with {p.other_trials} windows from "
        "these recordings, the 95% bound per recording is "
        f"{p.false_accept_bound:.0%} (more, and more varied, recordings lower it)",
        f"  short other-voice windows (0.5 s) at or above reject: {p.short_leak:.0%} "
        "(could pass as an unverified reply right after you)",
    ]
    if p.overlap:
        lines.append(
            "  warning: some other voices scored as high as yours; more or cleaner "
            "enrollment speech may help"
        )
    for problem in result.problems:
        lines.append(f"  problem: {problem}")
    if result.saved:
        lines.append(f"  saved: owner-only listening now filters {result.device!r}")
    elif result.refused:
        lines.append("  not saved because of the problem above (--force saves anyway)")
    else:
        lines.append("  not saved (dry run): add --write to use these thresholds")
    return lines
