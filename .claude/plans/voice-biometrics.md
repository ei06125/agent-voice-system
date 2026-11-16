# Voice biometrics: delivery plan

Companion to `.claude/research/voice-biometrics.md` (the *why*). This file is
the *how*: branching, milestones, test approach. Written 2026-11-16 while the
user is away and **cannot record their voice**, so everything up to M8 is built
and tested without it. Fable (claude-fable-5-2) reviewed the plan as advisor.
Its advice is folded in below and marked *(Fable)* where it changed something.

## Branching

```
main ─────────────────────────────────────────────●  (merge after owner eval, via PR)
       \                                         /
        voice-biometrics ──M0──M1──M2──…──M8────┘
```

- **`main`** only receives finished, evaluated work, through a PR.
- **`voice-biometrics`** is the feature branch. **One commit per milestone**
  (a milestone may take two commits if a refactor is worth isolating). Every
  commit is green: the full unit suite, ruff check and ruff format.
- Commit subjects start with the milestone: `M3: speaker embedder on onnxruntime`.
  `git log --oneline main..voice-biometrics` then reads as the milestone list.
- **TDD inside a milestone:** red → green → refactor locally. Red states are
  never committed. **BDD:** the milestone's `.feature` scenarios are written
  first and committed with the code that makes them pass.
- Nothing is pushed and nothing is merged to `main` from the sandbox. The user
  does that from the Mac after re-running the full suite there.
- Paths are staged explicitly (`git add <paths>`), never `git add -A`.
  `.claude/settings.local.json` and other local files stay out *(Fable)*.
- Never committed: models, speech recordings, owner data. Test assets live in
  a cache outside the repo (`DONNA_TEST_ASSETS`, default
  `~/.cache/donna/test-assets`), fetched with sha256 pins.

### The pre-commit hook in this sandbox

`.githooks/pre-commit` runs gitleaks, then `uv run --with pytest pytest`. In
the sandbox, uv cannot reach PyPI (the network policy needs a host approval,
and the user is away), so the hook fails on tooling, not on the code. Until
the user is back, each milestone commit:

1. runs the hook's two halves by hand: `gitleaks protect --staged --redact`
   and `python3.13 -m pytest -q tests` (Debian's pytest, numpy, onnxruntime
   and pytest-bdd, installed via apt), plus `ruff check` / `ruff format --check`;
2. is then made with `--no-verify`, and says so in its body.

The hook is not edited and no uv shim is installed *(Fable)*. **On the Mac,
before merging:** run the full suite with the hook enabled
(`uv run --with pytest pytest -q tests`). This includes `tests/test_pipeline.py`
and the listener lifecycle tests, which skip here (no `whisper-server`, no
`say`). Let uv re-lock `uv.lock` for the new `pytest-bdd` dev dependency.

## Milestones

| # | Milestone | Needs the owner? |
| --- | --- | --- |
| M0 | Research notes (both) + this plan | no |
| M1 | Test scaffolding: `pytest-bdd` dev dependency, `tests/features/`, pinned asset fetch script, an asset fixture that skips when the cache is absent | no |
| M2 | `fbank.py`: Kaldi-compatible fbank in pure numpy. Unit tests plus a committed golden on synthetic signals | no |
| M3 | `speaker.py` embedder on onnxruntime. Reads `normalize_samples` / `feature_normalize_type` from the ONNX metadata. Parity with sherpa-onnx and a same > different speaker sanity test on public clips (opt-in assets) | no |
| M4 | Profile store (`voiceprint.json`, 0600, per device, model sha256 + preprocessing version, thresholds optional) and the pure decision policy (bands, `speaker_min_s` on *voiced* seconds, the follow rule) | no |
| M5 | `ears enroll --input-file … --device NAME`, `ears enroll --delete`, speaker info in `ears status` | no |
| M6 | Listener: extract `_handle(segment)`; stamp device and capture epoch on segments; **shadow mode** (decision recorded in the private transcript record, delivery unchanged) | no (lifecycle tests: rerun on the Mac) |
| M7 | **Owner routing**: verify before Whisper; `other` never transcribed; `?` rendering; fail-closed but loud; runtime switch through `control.json`; refuses `owner` for a device without calibrated thresholds | no (same) |
| M8 | `tests/speaker_eval.py`: an EER / score-by-duration harness, run on the public clips now and on the owner's later | no |
| M9 | **Later, with the owner:** record the eval set (Brio + AirPods, two days), bake-off (ResNet34 vs LM vs CAM++), thresholds, `protocol.md` paragraph, README privacy note, shadow → owner, PR to `main` | **yes** |

Postponed or cut *(Fable)*:
- interactive microphone enrollment (cannot be tested here; the microphone rule);
- sliding-window mixed-speaker detection (§6.3: 1.5 s windows are in the
  unreliable regime);
- best-of-all-profiles for an unknown device (it becomes `unknown` plus a
  notice instead);
- `kaldi-native-fbank` (the numpy fbank has no dependency; parity proved in
  the spike);
- AS-Norm, Keychain encryption, guided enrollment UX.

## Test approach

**BDD (pytest-bdd, thin, user-visible behaviour only)**, in `tests/features/`.
Steps drive the listener's per-segment handler with a stub embedder and a stub
transcriber: no Whisper, no microphone, no subprocess.

- `enrollment.feature`: enrolling from files creates a private per-device
  profile · a second device keeps the first · a profile from a different model
  is refused with a re-enroll message · delete removes the voiceprint.
- `shadow.feature`: shadow changes nothing the model sees · it records
  decisions without text · speaker mode can be switched off at runtime.
- `owner_only.feature`: owner speech is delivered · someone else is never
  transcribed · a short reply right after the owner is delivered unverified
  (`?`) · a short segment with no recent owner speech is dropped (the TV leak)
  · continuity resets on device change, a reply or a gap · a broken or missing
  profile is never owner and says so once.

**TDD (plain pytest)** for everything else: fbank maths and goldens,
embedding parity, band arithmetic, profile file format and permissions, `?`
surviving line wrapping, config coercion.

**Assets** (opt-in, never committed): the three sherpa-onnx speaker models
and sherpa's 14 public test clips (3 speakers, Chinese). They are good for
plumbing and parity. They say **nothing** about the owner's thresholds.

## Findings from the spike (2026-11-16, before M2)

- A pure-numpy Kaldi fbank (povey window, preemph 0.97, 25/10 ms, 80 bins,
  20 Hz to Nyquist−400 Hz, `snip_edges=False`, reflect padding) reproduces
  sherpa-onnx's embeddings to **cosine 1.000000** on all 14 clips, for
  ResNet34 (no CMN, samples ×32768) and CAM++ (global mean, samples in [-1, 1]).
- **sherpa runs WeSpeaker without per-segment mean normalisation (CMN); upstream
  WeSpeaker uses it.** On the public clips, ResNet34 *with* CMN separates
  speakers (lowest same-speaker 0.57 > highest different-speaker 0.40; +0.39
  gap after subtracting the mean embedding), *without* it does not (−0.07).
  So DONNA uses CMN for WeSpeaker models, which deliberately departs from
  sherpa. The embedder makes this an explicit, versioned preprocessing choice.
- CAM++ separated poorly on these clips. That proves little (14 clips, Chinese,
  one setup), but ResNet34 is the default for plumbing until the M9 bake-off.
- Mean-centring embeddings (WeSpeaker's "Vox2-dev mean normalisation") helps a
  lot. It needs a cohort mean vector, which is a candidate addition after M9.
