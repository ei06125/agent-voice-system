# Voice biometrics for DONNA: research notes

Researched 2026-11-16 on the `voice-biometrics` branch. The question was how to
make `ears listen` tell **the user's** voice apart from everyone and everything
else near the microphone: other people, a video, a podcast, and DONNA's own
Kokoro voice.

Nothing here has been run yet. Accuracy figures come from papers and model cards
measured on VoxCeleb, not on this machine or this user. Anything marked
**verify** has to be measured before it is relied on.

Updated the same day: the user's decisions (§0) and a cross-reference with
Codex's independent research in `.codex/research/` (§11). Where the two
disagreed, §11 says which way this plan goes and why. The sections below have
been changed to match.

---

## 0. Decisions so far (2026-11-16) and the plan that follows

| Question | Answer | Consequence |
| --- | --- | --- |
| Tag other speakers, or drop them? | **"Start slow with just me being tracked."** | **Owner-only.** Only the user's speech reaches the transcript and the model. Other voices are not tracked: no labels, no profiles, no transcript. Meeting attribution (a `tag` mode) is out of scope for now. |
| Microphones | **Logitech Brio 4K** (webcam mics) most of the time; **AirPods** when echo is a problem | Two enrollment profiles, one per device (§5.3). Both are far from studio audio. |
| ~30 MB model download | Fine | The model downloads to `~/.donna/models` on the first `ears enroll`. |

Interpretation, to confirm with the user: "just me being tracked" means owner-only
routing **and** a single enrolled profile, with no attempt to identify or
label anyone else. It also means starting in **shadow mode** first (Codex's
phase 2). Shadow mode scores every segment and records only a private,
text-free decision log. Routing changes only after the evaluation shows the
thresholds work on both mics.

**Plan, smallest first:**

1. **Bake-off (offline, from files):** sherpa-onnx with WeSpeaker ResNet34
   **and** ResNet34-LM, plus CAM++. Run them on the user's Brio and AirPods
   recordings and the impostor set (§8). Output: a model and two
   per-device thresholds.
2. **Enrollment + shadow:** `ears enroll` (separate Brio and AirPods sessions),
   `speaker.py` scoring in the listener, and decisions written to a private
   metrics file (`segment 2.4s device=brio decision=owner`, never text). Live
   behaviour does not change.
3. **Owner-only routing:** `speaker: owner` drops `other` **before**
   `TranscriptLog.append` and `batcher.add`. `unknown` is handled as in §6.2.
   `off` stays available as a one-command fallback.
4. Later, and only if asked: tag/meeting mode, speaker-aware barge-in, AS-Norm,
   turn segmentation.

---

## 1. TL;DR

- **This is speaker *verification*, not authentication.** It gives one score per
  utterance: how much this sounds like the enrolled user. It is a good filter
  against TV and other people in the room. It is **not** a security boundary:
  replayed recordings and voice clones can get through (§7). The rule in
  `protocol.md` that a spoken "yes" does not authorise destructive actions
  must stay as it is.
- **Recommended stack:** a WeSpeaker **ResNet34-LM** (or CAM++) embedding model
  as ONNX, about 27–30 MB, run with **`onnxruntime`** (already a listener
  dependency) and **`kaldi-native-fbank`** for the features. No PyTorch, so the
  rule that the listener never pulls in the voice server's environment still holds.
- **Where it goes:** score each `Segment` from `segmenter.py` in the STT worker.
  In owner-only mode the score comes *before* the Whisper call, so other
  people's speech is never transcribed (§11). The embedding is much cheaper
  than Whisper, so it should add little latency (**verify**).
- **What it does with the score** (per the user's decision, §0): **owner-only**.
  Speech that doesn't match the user is dropped before it is written to the
  transcript or delivered. I first suggested a `tag` mode as the default; it
  is now deferred.
- **Hard parts:** segments under about 1 s (unreliable), segments where two
  people talk, different microphones (Brio far-field vs AirPods Bluetooth), and setting a
  threshold without any data. Plan a small evaluation set of recordings before
  choosing any number (§8).

---

## 2. Where it fits in the current pipeline

```
mic ─▶ Silero VAD ─▶ Segmenter ─▶ _stt_queue ─▶ _stt_worker ─▶ speaker.score(samples) ─┬─ owner / ? ─▶ whisper-server ─▶ textfilter ─▶ TranscriptLog + batcher ─▶ stdout
                    (0.25–28 s                                (new: fbank + ONNX, CPU)  │
                     segments)                                                          └─ other ─▶ dropped (count + text-free log line only)
```

Facts from the code that shape the design:

| Fact | Where | Consequence |
| --- | --- | --- |
| Segments are 16 kHz mono float frames of 512 samples (32 ms). Each holds 250 ms to 28 s of audio. | `segmenter.py` | Segments are the right unit for an embedding. Long ones may contain more than one speaker (§6.3). |
| Transcription runs in one worker thread off a bounded queue. | `listen.py:_stt_worker` | Compute the embedding in that worker, before or alongside `self._transcribe`. A second thread or a thread pool would let it overlap with Whisper. |
| The listener runs under `uv` with `numpy`, `onnxruntime`, `silero-vad-notorch`, `sounddevice`. | `lib/ears.py` header | `onnxruntime` is already present. The only new package is `kaldi-native-fbank`, which is small and has no other dependencies. |
| PyTorch is reserved for the voice server's environment. | `CLAUDE.md` | SpeechBrain, pyannote and NeMo can't run in the listener (they need torch). Use them only offline, to export ONNX. |
| Stdout is the event stream; the log must hold no transcript text. | `CLAUDE.md` | Log scores and decisions with durations only, never next to text. |
| Barge-in fires after `BARGE_IN_S` of voiced frames while a reply plays. Through loudspeakers, the microphone is gated off. | `listen.py:350–470` | A speaker check could later allow barge-in through loudspeakers (§6.5). Keep it out of the first version. |
| `Utterance` carries `text`, timestamps and `language`. `events.lines_for` renders the `[heard HH:MM:SS xx]` head. | `batcher.py`, `events.py` | Add a `speaker` field and render it the same way as the language tag. |

---

## 3. Approaches considered

| Approach | What it is | Fits DONNA? |
| --- | --- | --- |
| **Speaker embedding + cosine score per segment** | A pretrained network maps a stretch of speech to a 192–256-dim vector. Compare it with the enrolled average. | **Yes. Recommended.** Mature, small, ONNX-ready, text-independent, and simple to test with WAV files. |
| Personal VAD / target-speaker VAD | The VAD itself takes the enrollment embedding and fires only for the target speaker, frame by frame. | Not now. An open reimplementation exists ([wq2012/personal_vad](https://github.com/wq2012/personal_vad), Apache-2.0, [PyPI](https://pypi.org/project/personal-vad/0.2.1/)), trained on MLS. It would replace Silero, the most tested part of the listener. Revisit only if per-frame selectivity is needed for barge-in. Papers: [PVAD](https://arxiv.org/pdf/1908.04284), [PVAD 2.0](https://arxiv.org/pdf/2204.03793), [short-enrollment PVAD (2026)](https://arxiv.org/pdf/2601.12769). |
| Foreground VAD (enrollment-free) | Marks only the *dominant* speaker. Needs no enrollment. | Interesting but immature. A September 2026 preprint ([arXiv:2609.19856](https://arxiv.org/abs/2609.19856)) reports 1–2 ms/frame on CPU. No independent replication, and I found no released weights. It answers "the person closest to the mic", not "the user". Watch it. |
| Target speaker extraction (VoiceFilter-style) | Separates the user's voice out of a mixture before ASR. | No. Too heavy, and it changes the audio Whisper sees. Overkill for a desk setup. |
| Commercial on-device SDK: [Picovoice Eagle](https://picovoice.ai/docs/quick-start/eagle-python/) | `pveagle`: an enroll-until-100% profiler, then streaming per-frame scores. | Workable, and the API is the nicest of the options. But it is proprietary, needs a Picovoice Console AccessKey, and needs internet access for licence validation ([README](https://github.com/Picovoice/eagle/blob/main/README.md), via Codex). That is at odds with DONNA's all-local, no-account design. Good as a reference for enrollment UX ([GitHub](https://github.com/Picovoice/eagle)). |
| Diarization (pyannote, sherpa-onnx diarization) | Clusters "who spoke when" with no enrollment. | No for live use. It labels speakers A/B/C, not "the user", and its offline clustering needs the whole recording. May be useful offline for passive enrollment (§5.2). |

---

## 4. Choosing the embedding model

All of these are trained on VoxCeleb (English celebrities, interview audio) or
CN-Celeb, and work on 16 kHz input. EER is the equal error rate on the
VoxCeleb1-O trial list, as reported by the source. Lower is better.

| Model | ONNX file (sherpa-onnx release) | Size | Params | Reported EER Vox1-O | Notes |
| --- | --- | --- | --- | --- | --- |
| **WeSpeaker ResNet34-LM** | `wespeaker_en_voxceleb_resnet34_LM.onnx` | 26.5 MB | — | 0.723 % with AS-Norm; base ResNet34 0.867 % plain cosine | The best-documented choice. 256-dim. Expects an 80-bin Kaldi fbank. [Paper](https://arxiv.org/pdf/2210.17016), [recipe table](https://github.com/wenet-e2e/wespeaker/blob/master/examples/voxceleb/v2/README.md). Expect the plain-cosine number, not the AS-Norm one. |
| **CAM++** (3D-Speaker / WeSpeaker) | `3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx`, `wespeaker_en_voxceleb_CAM++_LM.onnx` | 29–30 MB | 7.18 M, 1.72 GFLOPs | 0.73 % | 192-dim, fast. [CAM++ paper](https://arxiv.org/pdf/2303.00332). A good second candidate. Benchmark it against ResNet34. |
| ERes2Net (3D-Speaker, VoxCeleb) | `3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx` | 26.5 MB | — | — | The sherpa-onnx release has no English ERes2Net**V2**, only the Chinese one. |
| ERes2NetV2 | (zh only in sherpa-onnx) | 71 MB | 17.8 M | 0.61 % full, **0.98 % at 3 s, 1.48 % at 2 s** | Designed for short utterances, which is DONNA's regime ([paper](https://arxiv.org/pdf/2406.02167)). An English ONNX would need an export from [3D-Speaker](https://github.com/modelscope/3D-Speaker). |
| NeMo TitaNet-S / L | `nemo_en_titanet_small.onnx` (40 MB), `_large` (101 MB) | — | — | TitaNet-L 0.68 % ([paper](https://arxiv.org/pdf/2110.04410v1)); TitaNet-S figure unverified | The preprocessing was not part of NeMo's ONNX export ([discussion](https://github.com/NVIDIA/NeMo/discussions/8132)). |
| SpeechBrain ECAPA-TDNN | Community ONNX: [kenrunio/transcriber-speaker-onnx](https://huggingface.co/kenrunio/transcriber-speaker-onnx) (84 MB, fbank inside), [vedk00/…](https://huggingface.co/vedk00/ecapa-voxceleb-speaker-embedding-onnx/blob/main/README.md) | 84 MB | — | ~0.8 % ([model card](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb)) | The upstream model needs torch. The community exports are third-party; audit them before use. |

The full list of model assets (fetched from the GitHub API) is in the
[sherpa-onnx `speaker-recongition-models` release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models)
(the misspelling is in the real tag). The release notes say *"each model has its
own license"*.

**Recommendation:** benchmark **ResNet34**, **ResNet34-LM** and **CAM++** on
DONNA's own evaluation set (§8). Choose on short-segment error rates and on CPU
time per second of audio. All are under 30 MB and would download to
`~/.donna/models` the same way the Whisper model does.

Why the plain ResNet34 is back in the bake-off: Codex found that WeSpeaker's
model catalog notes the large-margin (**LM**) variants may favour longer
audio, for example over 3 s
([pretrained.md](https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md)).
Most of DONNA's segments are shorter than that, so the LM model's better
VoxCeleb number may not carry over.

### Licensing, to check before shipping

- WeSpeaker's docs place its VoxCeleb weights under **CC BY 4.0**, but some
  model cards say Apache-2.0. An open issue asks the maintainers to clarify
  ([wespeaker#471](https://github.com/wenet-e2e/wespeaker/issues/471)). CC BY
  needs attribution in the README.
- The VoxCeleb *corpus* has its own terms, which matter only if we fine-tune
  ([VoicePrivacy 2024 plan](https://arxiv.org/pdf/2404.02677)).
- 3D-Speaker and NeMo models: check each model card.

### Runtime: plain onnxruntime vs sherpa-onnx

| | `onnxruntime` + `kaldi-native-fbank` | `sherpa-onnx` |
| --- | --- | --- |
| New dependencies | `kaldi-native-fbank` (small C++ wheel, no dependencies) | `sherpa-onnx` + `sherpa-onnx-bin`/`-core` (~2 + ~10–12 MB arm64 wheels, [install docs](https://k2-fsa.github.io/sherpa/onnx/python/install.html)). It bundles its **own** ONNX Runtime, next to the one Silero already uses. |
| Features | We write fbank + CMN (~30 lines) and must match the model's training front end exactly | Done for us: `SpeakerEmbeddingExtractor` reads the metadata in the ONNX file |
| API | Our own | `create_stream / accept_waveform / compute`, and `SpeakerEmbeddingManager.add/search(threshold)` ([example](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/speaker-identification.py), default threshold 0.6) |
| Risk | Getting the front end subtly wrong: the scores look plausible but are worse | Two ONNX runtimes in one process; a larger dependency to pin |

**Recommendation:** use **sherpa-onnx for the spike**: it gives a correct front
end for free and a reference to compare against. Ship
**`onnxruntime` + `kaldi-native-fbank`** once our own features match sherpa's
embeddings to within ~1e-4. Known WeSpeaker front-end settings, which must be
verified against the reference: 80 mel bins, 25 ms window / 10 ms shift,
Hamming window, dither 0, samples scaled by 32768, mean normalisation over the
segment ([kaldi-native-fbank](https://github.com/csukuangfj/kaldi-native-fbank/blob/master/README.md),
[a CoreML port documenting the match](https://huggingface.co/nikioth/wespeaker-resnet34-lm-coreml),
[wespeakerruntime `cmn`](https://pypi.org/project/wespeakerruntime/)).

---

## 5. Enrollment

### 5.1 Explicit: `ears enroll`

- Record **30–60 s** of natural speech, in several clips of 5–10 s (reading a
  short text, then talking freely). Store the **mean of the L2-normalised
  embeddings**, plus the individual embeddings so a max-over-enrollment score
  is possible. sherpa-onnx averages per speaker, and the
  [joeblau/voice gate](https://github.com/joeblau/voice/issues/47) takes the
  max over enrollment embeddings.
- **One profile per input device** (§5.3 covers the user's two). Channel
  mismatch is the largest practical source of false rejects. The listener
  already knows the device name (`audio.py`, `follows_default`). Key the
  centroid by device. An unknown device has no profile and scores against
  every profile, taking the best score, with thresholds calibrated for that
  rule. Codex warns that max-over-templates gives impostors more chances to
  match, so the rule must be calibrated as used.
- Reject poor enrollment clips (silence, clipping, overlap, a second voice).
  Inspect any clip whose embedding is far from the rest before averaging.
  Collect over **two separate sittings** (Codex), so the profile does not learn
  one day's room and mood.
- The profile file records the **model checksum and preprocessing version**
  (Codex). If either changes, the listener refuses the profile and asks for
  re-enrollment, instead of comparing incompatible embeddings.
- A guided flow that shows progress, like Eagle's "enroll until 100 %"
  ([Picovoice docs](https://picovoice.ai/docs/api/eagle-python/)), is good UX.
- `ears enroll --input-file a.wav b.wav` must work for tests. The rule in
  `CLAUDE.md` still applies: the microphone opens for enrollment only when the
  user runs it themselves.

### 5.2 Passive and adaptive: deferred

**Decision: no ambient self-enrollment for now** (agreeing with Codex).
In owner-only mode a false accept would be folded into the profile, making the
next false accept likelier. Re-enrollment is explicit and reversible
(`ears enroll` again, `ears enroll --delete`). The notes below are kept in case
this is revisited:

- Add an embedding to the profile only when its score is far above the accept
  threshold, the segment is longer than about 3 s, and the device is known.
  Cap how far the centroid can move from the explicit enrollment (a "drift
  cap"). The literature warns about drift and slow capture by impostors.
  Updates should be conservative, quality-weighted and gradual ([Shum et al.](https://people.csail.mit.edu/sshum/papers/Odyssey10paper.pdf),
  [quality-aware template update](https://uu.diva-portal.org/smash/get/diva2:2080552/FULLTEXT01.pdf),
  [EvoTSE](https://arxiv.org/html/2604.06810)).
- Never adapt on a segment recorded while a reply was playing.

### 5.3 The user's two microphones

| | Logitech Brio 4K | AirPods |
| --- | --- | --- |
| Capture | Two omnidirectional mics on the webcam, with built-in noise cancellation. Rated pickup is about 1 m (Logitech pages give 3 ft to 1.2 m). [Logitech](https://www.logitech.com/en-in/products/webcams/brio-4k-hdr-webcam.html), [Logitech EU](https://www.logitech.com/en-eu/shop/p/brio-4k) | A Bluetooth headset path. Opening the AirPods mic switches macOS to the hands-free profile, reportedly 16 kHz wideband (and the playback quality drops with it). The codec (mSBC vs AAC-ELD) is disputed in the sources. [QK4 PR](https://github.com/mikeg-dal/QK4/pull/102), [HN](https://news.ycombinator.com/item?id=41695756), [swissmacuser](https://swissmacuser.ch/airpods-suddenly-change-to-low-quality-audio-in-macos/) |
| What it means for verification | Far-field, at monitor distance: more room reverb, more of anyone else in the room, and of the loudspeakers (that is why the user switches to AirPods for echo). The device's noise suppression changes the spectrum the embedding sees. Expect lower owner scores than close-talk speech, and more impostor audio at usable levels. | Close-talk and good SNR, but narrowband-ish and heavily coded. VoxCeleb-trained models see little Bluetooth-codec audio. Expect the owner/impostor score distributions to sit somewhere else entirely than for the Brio. This is why the profiles and thresholds are per device. |
| Speaking back | Loudspeakers: the existing gate keeps the mic closed during replies, so Kokoro never reaches the verifier. | Headphones: the mic stays open; barge-in stays VAD-based (§6.5). |

Both feed the listener's 16 kHz pipeline; the models expect 16 kHz, so no
change there. **verify** in the bake-off: enroll on one device, test on the
other. That shows how bad the mismatch is and whether two profiles are really
needed. They are cheap, so plan for two.

---

## 6. Scoring and policy

### 6.1 Score

Use the cosine similarity between the L2-normalised segment embedding and the
device centroid. Raw cosine is not a probability. The threshold has to be
calibrated per model ([VoxRigor note](https://github.com/AB772K/VoxRigor/pull/3)).
AS-Norm (normalising against a cohort of ~300–1000 impostor embeddings) gives
the best published numbers ([TAS-norm](https://arxiv.org/abs/2504.04512),
[Cumani & Sarni 2023](https://www.isca-archive.org/interspeech_2023/cumani23_interspeech.html)).
A cohort of about 1k × 256 floats is around 1 MB, so it could be bundled later.
Start with plain cosine.

### 6.2 Three bands, not one threshold

From the joeblau/voice design, which is the closest real-world analogue to
DONNA ([issue #47](https://github.com/joeblau/voice/issues/47), [PR #150](https://github.com/joeblau/voice/pull/150)):

| Band | Condition | Owner-only behaviour (the chosen mode) |
| --- | --- | --- |
| **owner** | score ≥ `T_hi` | Transcribed, stored, delivered: `[heard 22:41:07] …` (unchanged) |
| **unknown** | `T_lo` ≤ score < `T_hi`; too short; mixed (§6.3); no profile for this device; model or profile error | See below |
| **other** | score < `T_lo` | Dropped before `TranscriptLog.append` and before the batcher. Ideally before Whisper too, so the text never exists |

Codex's rule, adopted: **failure means unknown, never owner.** A missing or
corrupt profile, a model error or a full queue must not quietly fall back to
"everyone is the user". `ears status` and an `[ears] …` notice must say so
instead.

**Unknown in owner-only mode** is the one real policy choice left. The options:

- (a) **Drop it.** The strictest option. But the user's short "yes" and "okay"
  will be lost whenever they fall into this band.
- (b) **Deliver it marked unverified** (`[heard 22:41:07 ?] …`), with
  `protocol.md` saying such lines are context, never a request on their own.
  Codex: "other/unknown is never a command".
- **Proposed:** (b) only for a *short* segment within ~5 s of an owner segment,
  on the same device, with no reply playing since; (a) otherwise. Shadow mode
  (§0) will show how often each case happens before this is fixed. Codex warns
  that continuity is exactly how the next person's brief reply gets mistaken
  for the user's, so the window is short. Continuity resets on a device change,
  a long gap, a capture reset or a reply.

Short segments (< ~1 s voiced) never become *owner* by themselves. Accuracy collapses at 1 s:
reported EERs range from about 3 % to about 14 % depending on the setup, against
under 1 % on full utterances ([survey](https://www.semanticscholar.org/paper/Speaker-verification-with-short-utterances:-a-of-Poddar-Sahidullah/27cb5c74c8a1647bfb31c7fd863e0cae238d4b8a),
[segment aggregation](https://arxiv.org/pdf/2005.03329)). "Yes", "stop" and
"okay" are exactly the utterances DONNA receives that are this short. Note the
leak a reviewer found in joeblau/voice: short TV segments with no recent
decision slipped through as *unsure*. The follow rule needs a test for
exactly that.

### 6.3 Long and mixed segments

A segment can be up to 28 s, and two people may speak in it. Score it in
sliding windows (for example 1.5 s windows with a 0.75 s hop) as well as as a
whole. If the windows disagree, the segment is **unknown**, never owner.
Codex's point: a whole-segment average can match the owner while one sentence
in it belongs to someone else, and the text would then be passed on with the
user's authority. Splitting the segment at speaker changes before Whisper
(Codex's turn layer, or online diarization such as
[diart](https://github.com/juanmc2005/diart/blob/main/README.md): 500 ms
updates, 0.5–5 s latency) is the proper fix. It is deferred, because for one
person at a desk mixed segments should be rare; shadow mode will show whether
they are.

### 6.4 Config (proposed, in `config.DEFAULTS`)

```python
"speaker": "off",            # off | shadow | owner   (tag: deferred)
"speaker_model": "",         # chosen by the bake-off
"speaker_min_s": 1.0,        # shorter segments are never "owner" on their own
"speaker_follow_s": 5.0,     # window for delivering a short "unknown" as "?" (§6.2)
```

Thresholds do **not** go in `config.json`. They are stored per device in the
profile, next to the model checksum they were calibrated for, because they
change when the model or the enrollment changes.

`off` is the default until a profile exists. `ears status` should say whether a
profile is loaded, which devices it covers, and the current mode.

### 6.5 Interaction with speaking back (later)

- **Kokoro's own voice** will almost certainly score as *other*: it is a
  different voice from the user's. That gives a second line of defence against
  self-transcription when headphones are misdetected. **verify** by scoring
  Kokoro output (all configured voices) against the profile. Note that TTS
  *can* fool verifiers when it resembles the target
  ([Spoofing SV with multi-speaker TTS](https://arxiv.org/pdf/1910.13054)).
  That is unlikely with a stock voice, but measure it.
- **Barge-in through loudspeakers:** today the microphone is gated while a
  reply plays through speakers. Speaker verification could re-open it and let
  the user (and only the user) interrupt. But verification needs about 1–1.5 s
  of speech, and the user's voice would be mixed with the reply. joeblau/voice
  measured barge-in waiting about 1.2 s for its 1.5 s score. Keep the current
  VAD-only barge-in on headphones. Treat loudspeaker barge-in as an experiment
  for phase 3.

---

## 7. Limits to state plainly

- **Not authentication.** Replay is the classic attack. Even the
  anti-spoofing detectors fail under it: W2V2-AASIST's EER rose from 4.7 % to
  18.2 % under replay, and to 11 % even after retraining with augmentation
  ([Replay attacks against deepfake detection, 2025](https://arxiv.org/pdf/2505.14862);
  [AASIST](https://github.com/clovaai/aasist)). DONNA should not add an
  anti-spoofing model. It should keep "a voice is not an identity" and the
  typed go-ahead for destructive actions in `protocol.md`, whatever the decision
  says.
- **False rejects will happen** (a cold, whispering, shouting, a new mic). In
  owner-only mode a false reject loses the user's words, which is the cost of
  the chosen mode. Mitigations: shadow mode first; `ears status` counts drops;
  `speaker: off` as a one-command fallback; typing always works.
- **Owner match ≠ addressed to Claude** (Codex). The existing `protocol.md`
  judgement (addressed to you vs talk near the mic) still applies to the
  user's own speech.
- **Statistics** (Codex): with zero false accepts in 1,000 independent impostor
  trials, the 95 % upper bound is still about 0.3 % (the rule of three).
  Overlapping windows of the same clip are not independent trials.
- **Language:** the embeddings are largely language-independent, but the
  models are trained mostly on English (VoxCeleb). The user speaks Portuguese
  too (`pt` tags exist). **verify** that cross-language scores (enrolled in
  English, speaking Portuguese) stay above `T_hi`.

---

## 8. Evaluation plan (before choosing thresholds)

All of it runs from files, in keeping with the "never open the microphone to
test" rule.

1. **Target set:** the user's own recordings on the **Brio** and on the
   **AirPods**, about 20 clips of 1–15 s each, in English and Portuguese,
   including short replies ("yes", "okay", "stop", "sim"). Record on **two or
   more days**, and split enrollment / threshold-tuning / test **by day**, not
   by clip, so there is no leakage (Codex). They are recorded once, by the
   user, deliberately, and kept outside the repo under `~/.donna/eval/`, never
   committed.
2. **Impostor set:** macOS `say` voices (`say -o x.aiff`), Kokoro outputs for
   every voice in `config`, a few LibriSpeech/VoxCeleb test clips, a TV or a
   podcast played through the speakers **and picked up by the Brio**, and, if
   anyone consents, a few real people in the room. TTS only exercises the
   plumbing; real voices are what calibrate false accepts (Codex).
3. **Attribution cases** (Codex): the user then someone else inside one
   segment, someone else then the user, overlap. The pass criterion is that
   none of them comes out as owner.
4. **Harness:** `tests/speaker_eval.py` (opt-in, skipped when no eval data is
   present) cuts clips through the real `Segmenter`, scores them, and prints
   EER, FRR at the chosen FAR, and an FRR/FAR-by-duration table (≤1 s, 1–2 s,
   2–5 s, >5 s), separately for each model.
5. **Unit tests** (always run): use a stub embedder, as `voice_server` takes a
   stand-in engine. Cover the band logic; the short-segment follow rule and its
   resets; mixed windows becoming unknown; `other` never reaching
   `TranscriptLog` or the batcher; a missing, corrupt or wrong-model profile
   failing to unknown with a visible notice; `?` surviving line wrapping in
   `events.py`; the profile file permissions; and, after touching
   `listen.py`, the lifecycle tests (a full speaker queue or a failed worker
   must not hold the microphone open).
6. **Latency:** measure the embedding time per second of audio on an M-series
   CPU, and confirm it hides behind Whisper's time. **verify**: no published
   Apple Silicon figure was found for these ONNX files.

---

## 9. Privacy and storage

- A voiceprint is **biometric data**. Under GDPR it becomes special-category
  data (Art. 9) when used to uniquely identify a person, which needs explicit
  consent. Illinois BIPA names voiceprints and requires written informed
  consent ([GDPR & speech data](https://waywithwords.net/resource/how-does-gdpr-apply-to-speech-datasets/),
  [BIPA template](https://www.document.com/documents/biometric-voiceprint-consent),
  [least-privilege for audio models](https://arxiv.org/pdf/2503.16833)). For a
  user enrolling themselves on their own machine this is low risk. Two cases
  matter more: (a) passive enrollment must never build a profile of anyone but
  the user, and (b) these would be design obligations if DONNA ever enrolled
  other people.
- Store the profile at `~/.donna/voiceprint.json`, mode `0600`, like the
  session transcripts (`ensure_dirs` already uses `0o700`). Never log or print
  it, and never send it to the model. Provide `ears enroll --delete`. Keep
  enrollment audio only if the user asks. Codex adds an option for later:
  encrypt the profile with a key kept in the macOS Keychain. File permissions
  don't protect against a compromised user account.
- Logs may say `segment 3.2s device=brio decision=other`, never next to text.
  That is consistent with the existing rule.
- Owner-only mode is also the privacy-friendly option for bystanders: their
  speech never reaches the transcript or the model, and no profile of them is
  ever built. `README.md` should say so.

---

## 10. Phases

Superseded by the plan in §0: bake-off → enrollment + shadow → owner-only →
later extras. The implementation pieces are the same as before:
`donna_ears/speaker.py` (sherpa-onnx in the bake-off; onnxruntime +
kaldi-native-fbank once the embeddings match), `ears enroll [--input-file …]
[--delete]`, the profile with per-device thresholds, a decision on `Utterance`,
the `?` rendering in `events.py`, a paragraph in `protocol.md`, and
`ears status`/`doctor` reporting.

## 11. Cross-reference with Codex's research (`.codex/research/`)

Codex worked from upstream GitHub docs only. Its search engine and most
non-GitHub hosts were blocked, so it has no paper-level numbers. It sticks
closely to failure modes. The two sets of notes agree on the core.

**Agreed by both independently:**

- Local, text-independent verification against one explicitly enrolled
  profile, with three outcomes (owner / other / unknown).
- WeSpeaker ONNX through sherpa-onnx first. Plain onnxruntime later, but only
  after an embedding-parity test, because a `.onnx` file does not tell you its
  input features.
- No PyTorch in the listener. SpeechBrain and pyannote only offline or in a
  separate environment.
- Cosine scores are not probabilities. Calibrate per model, preprocessing,
  aggregation rule and microphone. sherpa's 0.6 and SpeechBrain's 0.25
  defaults are not recommendations.
- Not liveness and not authentication. Keep the typed go-ahead for consequential
  actions. Anti-spoofing is a separate, unproven add-on.
- Store embeddings only, private files, deletable.
- 30–60 s of enrollment speech; cover both languages and the real microphones.

**Adopted from Codex** (now in the sections above):

| Point | Where |
| --- | --- |
| LM variants may favour audio over 3 s, so the plain ResNet34 joins the bake-off | §4 |
| Mixed segments become unknown, never owner; continuity resets on speaker/device change, gaps, capture resets | §6.2, §6.3 |
| Failures (missing or corrupt profile, model error) mean unknown, shown in status | §6.2 |
| Drop `other` before transcript persistence, not just before delivery | §0, §6.2 |
| No ambient self-enrollment, which reverses my earlier phase-2 adaptation idea | §5.2 |
| Model checksum and preprocessing version in the profile; re-enroll on mismatch | §5.1 |
| Max-over-templates needs its own calibration | §5.1 |
| Split eval data by day/session; real non-owner voices for false-accept rates; rule of three | §7, §8 |
| A shadow phase before routing changes | §0 |
| An owner match still needs the "addressed to Claude?" judgement | §7 |
| Keychain encryption as an option | §9 |
| Picovoice Eagle also needs internet access for licence validation, which strengthens the case against it | §3 |
| Candidates I had not listed: [diart](https://github.com/juanmc2005/diart/blob/main/README.md) (online diarization), [pyannote Community-1](https://github.com/pyannote/pyannote-audio/blob/develop/README.md) (offline reference; disable its telemetry with `PYANNOTE_METRICS_ENABLED=0`), [Resemblyzer](https://github.com/resemble-ai/Resemblyzer/blob/master/README.md) (simple PyTorch baseline, English-leaning) | §3, §6.3 |
| [ASVspoof 5 baselines](https://github.com/asvspoof-challenge/asvspoof5/blob/main/README.md) keep spoof detection and spoof-aware verification as separate tasks | §7 |
| sherpa-onnx's extractor takes a `provider` (cpu/cuda/coreml). Measure CPU first and keep it as the baseline before trying CoreML | §8 |

**Where the notes differ, and the call made here:**

| Topic | Codex | These notes | Resolution |
| --- | --- | --- | --- |
| Verify before or alongside Whisper | Verify first, and transcribe only owner speech (saves compute and keeps others' text from ever existing); or run both in parallel but emit nothing until identity resolves | Run alongside Whisper to hide latency | **Verify first in owner-only mode.** The embedding is far cheaper than Whisper, so the added latency is small (**verify**), and dropped speech is never transcribed, which suits "just me". Run in parallel only if shadow mode shows the latency matters. |
| Turn segmentation / diarization | Needed for "the intended experience"; phase 4 | Deferred; sliding-window disagreement → unknown | Deferred. One user at a desk with a Brio or AirPods. Revisit if shadow mode shows mixed segments are common. |
| Short replies after an owner turn | Don't let recent context turn the next person's reply into owner speech | Inherit the last decision within 5 s | **Narrowed:** a short segment never becomes owner. It can only be delivered as `?` (unverified, context only) under the follow rule in §6.2. |
| Default when someone else speaks | Owner-only for commands; a meeting mode keeps others with anonymous labels | Tag by default | **Owner-only**, by the user's decision. Meeting mode is out of scope. |
| Sources | 15 upstream GitHub sources; most other hosts blocked | Web search + papers, including figures for short utterances, replay and ERes2NetV2 | Complementary. Codex's figure, as reported: SpeechBrain ECAPA 0.80 % EER with score normalisation, 0.90 % without ([recipe](https://github.com/speechbrain/speechbrain/blob/develop/recipes/VoxCeleb/SpeakerRec/README.md)). |

## Open questions for the user

- Is "just me being tracked" read correctly as owner-only plus a single
  profile, starting in shadow mode? (§0)
- Is a short **unknown** reply ("yes", "sim") best delivered as an unverified
  `?` line under the follow rule, or dropped? (§6.2)
- Willing to record the eval set: about 20 clips per device (Brio, AirPods)
  over two days, plus a podcast played near the Brio? (§8)

---

## Sources

Models and toolkits
- [WeSpeaker paper](https://arxiv.org/pdf/2210.17016) · [VoxCeleb v2 recipe results](https://github.com/wenet-e2e/wespeaker/blob/master/examples/voxceleb/v2/README.md) · [WeSpeaker VoxSRC2023 baselines](https://arxiv.org/pdf/2306.15161) · [pyannote/wespeaker-voxceleb-resnet34-LM](https://huggingface.co/pyannote/wespeaker-voxceleb-resnet34-LM) · [ResNet34-LM model card](https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/main/README.md?download=true) · [Sentis ResNet34-LM port](https://huggingface.co/Sky-Kim/com.sky.sentis.resnet34lm) · [license issue #471](https://github.com/wenet-e2e/wespeaker/issues/471)
- [3D-Speaker repo](https://github.com/modelscope/3D-Speaker) · [3D-Speaker-Toolkit paper](https://arxiv.org/html/2403.19971v3) · [CAM++](https://arxiv.org/pdf/2303.00332) · [ERes2NetV2](https://arxiv.org/pdf/2406.02167) · [CAM++ ONNX community export](https://huggingface.co/welcomyou/campplus-3dspeaker-200k-onnx)
- [TitaNet](https://arxiv.org/pdf/2110.04410v1) · [TitaNet-S Sentis port](https://huggingface.co/Sky-Kim/com.sky.sentis.titanet-small) · [NeMo export discussion](https://github.com/NVIDIA/NeMo/discussions/8132)
- [SpeechBrain ECAPA](https://huggingface.co/speechbrain/spkrec-ecapa-voxceleb) · [kenrunio ECAPA ONNX](https://huggingface.co/kenrunio/transcriber-speaker-onnx) · [vedk00 ECAPA ONNX](https://huggingface.co/vedk00/ecapa-voxceleb-speaker-embedding-onnx/blob/main/README.md) · [speaker-detector](https://pypi.org/project/speaker-detector/)
- [sherpa-onnx speaker models release](https://github.com/k2-fsa/sherpa-onnx/releases/tag/speaker-recongition-models) · [speaker-identification.py](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/speaker-identification.py) · [sherpa-onnx Python install](https://k2-fsa.github.io/sherpa/onnx/python/install.html) · [sherpa-onnx diarization](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/index.html)
- [kaldi-native-fbank](https://github.com/csukuangfj/kaldi-native-fbank/blob/master/README.md) · [wespeakerruntime](https://pypi.org/project/wespeakerruntime/) · [WeSpeaker ResNet34-LM CoreML (front-end notes)](https://huggingface.co/nikioth/wespeaker-resnet34-lm-coreml)
- [Picovoice Eagle](https://github.com/Picovoice/eagle) · [Eagle Python quick start](https://picovoice.ai/docs/quick-start/eagle-python/) · [Eagle API](https://picovoice.ai/docs/api/eagle-python/) · [Picovoice voice biometrics guide](https://picovoice.ai/blog/voice-biometrics/)

Gating, VAD and system design
- [joeblau/voice verification gate issue #47](https://github.com/joeblau/voice/issues/47) · [PR #150](https://github.com/joeblau/voice/pull/150)
- [Personal VAD](https://arxiv.org/pdf/1908.04284) · [Personal VAD 2.0](https://arxiv.org/pdf/2204.03793) · [wq2012/personal_vad](https://github.com/wq2012/personal_vad) · [Short-enrollment PVAD](https://arxiv.org/pdf/2601.12769)
- [Foreground VAD (2026 preprint)](https://arxiv.org/abs/2609.19856) · [Target Speaker Identification streaming pipeline](https://arxiv.org/pdf/2608.17972) · [End-to-end speaker-dependent VAD](https://arxiv.org/pdf/2009.09906) · [Gated cross-attention speaker extraction](https://doi.org/10.3390/electronics13112046)

Scoring, short utterances, adaptation
- [Speaker verification with short utterances: review](https://www.semanticscholar.org/paper/Speaker-verification-with-short-utterances:-a-of-Poddar-Sahidullah/27cb5c74c8a1647bfb31c7fd863e0cae238d4b8a) · [Segment aggregation for short utterances](https://arxiv.org/pdf/2005.03329) · [UCLA short-utterance SV](http://www.seas.ucla.edu/spapl/paper/jinxi_is16.pdf)
- [Trainable AS-Norm](https://arxiv.org/abs/2504.04512) · [Adaptive data normalization (Interspeech 2023)](https://www.isca-archive.org/interspeech_2023/cumani23_interspeech.html) · [VoxRigor ECAPA/AASIST adapters](https://github.com/AB772K/VoxRigor/pull/3)
- [Unsupervised speaker adaptation (Shum et al.)](https://people.csail.mit.edu/sshum/papers/Odyssey10paper.pdf) · [Quality-aware template update](https://uu.diva-portal.org/smash/get/diva2:2080552/FULLTEXT01.pdf) · [EvoTSE](https://arxiv.org/html/2604.06810)

Microphones
- [Logitech Brio 4K (IN)](https://www.logitech.com/en-in/products/webcams/brio-4k-hdr-webcam.html) · [Logitech Brio 4K (EU)](https://www.logitech.com/en-eu/shop/p/brio-4k) · [BRIO 4K Pro specs](https://support.logi.com/hc/en-us/articles/360023462913-BRIO-4K-Pro-Webcam-Technical-Specifications)
- [AirPods/Bluetooth mic on macOS (QK4 PR)](https://github.com/mikeg-dal/QK4/pull/102) · [HN: AirPods quality degradation](https://news.ycombinator.com/item?id=41695756) · [swissmacuser: AirPods low-quality switch](https://swissmacuser.ch/airpods-suddenly-change-to-low-quality-audio-in-macos/)

From Codex's research (see `.codex/research/sources.md` for its notes and digests)
- [WeSpeaker README](https://github.com/wenet-e2e/wespeaker/blob/master/README.md) · [WeSpeaker pretrained catalog](https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md) · [SpeechBrain VoxCeleb recipe](https://github.com/speechbrain/speechbrain/blob/develop/recipes/VoxCeleb/SpeakerRec/README.md) · [SpeechBrain speaker.py](https://github.com/speechbrain/speechbrain/blob/develop/speechbrain/inference/speaker.py) · [pyannote.audio](https://github.com/pyannote/pyannote-audio/blob/develop/README.md) · [diart](https://github.com/juanmc2005/diart/blob/main/README.md) · [Eagle README](https://github.com/Picovoice/eagle/blob/main/README.md) · [Resemblyzer](https://github.com/resemble-ai/Resemblyzer/blob/master/README.md) · [ASVspoof 2021](https://github.com/asvspoof-challenge/2021/blob/main/README.md) · [ASVspoof 5](https://github.com/asvspoof-challenge/asvspoof5/blob/main/README.md) · [sherpa-onnx license](https://github.com/k2-fsa/sherpa-onnx/blob/master/LICENSE)

Security and privacy
- [AASIST](https://github.com/clovaai/aasist) · [Replay attacks against audio deepfake detection](https://arxiv.org/pdf/2505.14862) · [Spoofing SV with multi-speaker TTS](https://arxiv.org/pdf/1910.13054) · [Survey of threats against voice authentication](https://arxiv.org/pdf/2508.16843)
- [GDPR and speech datasets](https://waywithwords.net/resource/how-does-gdpr-apply-to-speech-datasets/) · [BIPA voiceprint consent](https://www.document.com/documents/biometric-voiceprint-consent) · [Least privilege for audio LMs](https://arxiv.org/pdf/2503.16833)
