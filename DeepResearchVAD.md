# Deep Research: Voice Activity Detection and Local Tooling

Research date: 2026-08-16

Scope: local/offline VAD for the DONNA hands-free voice pipeline, with emphasis on real-time utterance segmentation on Apple Silicon. Sources are primarily project repositories, official documentation, and papers. Product and project benchmark claims are identified as such.

## Executive conclusion

Keep Silero VAD as the production baseline. It is already installed, permissively licensed, accurate, and vastly faster than real time on this machine. Replacing it is unlikely to fix the pipeline's biggest problems.

The highest-value work is:

1. Move ONNX inference, NumPy allocation, concatenation, and logging out of the PortAudio callback.
2. Add a hard maximum utterance duration and bounded audio storage.
3. Stop launching a fresh `whisper-cli` process and loading the 141 MB Whisper model for every utterance; use a persistent recognizer process or in-process context.
4. Build a recorded, annotated test corpus from the actual microphones and rooms, then tune threshold, trailing silence, padding, and endpoint policy against application-level metrics.
5. If fixed silence still cuts off mid-thought, add a local end-of-turn model after VAD. VAD answers “is there speech?”; it cannot reliably answer “has the user finished the thought?”

The best model challengers are FireRedVAD and TEN VAD. WebRTC VAD is the tiny, deterministic baseline. Pipecat Smart Turn is the most practical fully local semantic endpointing layer. None should replace the current stack solely on vendor-reported benchmark numbers.

## 1. What VAD is—and is not

Voice activity detection, also called speech activity detection, classifies audio frames as speech or non-speech. A complete streaming system has several distinct layers:

| Layer | Question answered | Typical output |
|---|---|---|
| Audio front end | Is the signal usable? | Resampled/denoised/echo-cancelled PCM |
| Frame VAD | Does this short frame contain speech? | Boolean or probability |
| Segmentation | Where does a speech region start and end? | Timestamp pairs or events |
| Endpointing | When should this utterance be submitted? | End-of-utterance event |
| Turn detection | Has the speaker finished the conversational turn? | Finished/wait/unfinished |
| Wake-word detection | Was a particular phrase spoken? | Keyword event |
| ASR | What was said? | Text and timestamps |

This distinction matters. A 500 ms pause is acoustically non-speech whether it is the end of a command or a hesitation inside one. No conventional VAD can infer the difference from silence alone. LiveKit's turn-taking documentation consequently treats VAD-only, STT endpointing, semantic turn detection, and manual push-to-talk as separate modes. Its recommended turn detector combines meaning and acoustic evidence on top of VAD ([LiveKit turn-taking overview](https://docs.livekit.io/agents/logic/turns/)). Pipecat makes the same distinction: its Smart Turn model runs only after a lightweight VAD detects silence, then judges whether the current turn is complete ([Smart Turn repository](https://github.com/pipecat-ai/smart-turn)).

Whisper's `no_speech_threshold` is also not a streaming VAD. It is a decoder-side rule used to skip a Whisper window when the model predicts no speech and the token log probability is low ([OpenAI Whisper transcription source](https://github.com/openai/whisper/blob/main/whisper/transcribe.py)). It cannot decide when to stop microphone capture before the window is submitted.

### 1.1 The practical streaming state machine

A robust local endpoint normally uses:

- Small fixed input frames.
- A model probability or binary decision per frame.
- Separate start and stop thresholds, or equivalent hysteresis.
- A pre-roll ring buffer to retain initial consonants.
- A minimum speech duration to reject clicks and short noise bursts.
- A trailing-silence/hangover duration to tolerate short pauses.
- Speech padding at both boundaries.
- A maximum utterance duration to bound memory and latency.
- A reset/recovery policy for overflow, device changes, and long noise events.

Silero's reference post-processing uses a speech threshold and, by default, an exit threshold 0.15 lower. This hysteresis prevents rapid speech/silence toggling around a single threshold. Its offline timestamp function also supports minimum speech, maximum speech, minimum silence, padding, and a policy for splitting long speech at the best available silence ([Silero VAD utility source](https://github.com/snakers4/silero-vad/blob/master/src/silero_vad/utils_vad.py)). SpeechBrain documents the same general pattern: frame posteriors, activation/deactivation thresholds, candidate segments, merging close regions, and removing short segments ([SpeechBrain VAD tutorial](https://speechbrain.readthedocs.io/en/latest/tutorials/tasks/voice-activity-detection.html)).

### 1.2 Main algorithm families

**Energy gates.** RMS level, peak level, spectral energy, or zero-crossing thresholds are cheap and understandable. They detect audible activity, not specifically human speech. Keyboard noise, music, fans, and playback can trigger them, while quiet or distant speech can be missed. FFmpeg `silencedetect` and `librosa.effects.split` are useful batch baselines, not robust conversational VADs ([FFmpeg filter documentation](https://ffmpeg.org/ffmpeg-filters.html#silencedetect), [librosa split documentation](https://librosa.org/doc/main/generated/librosa.effects.split.html)).

**Statistical/classical VAD.** The WebRTC VAD uses frequency-band features and Gaussian mixture models, compares speech and noise hypotheses with likelihood-ratio tests, and updates its models over time ([WebRTC VAD source](https://chromium.googlesource.com/external/webrtc/+/HEAD/common_audio/vad/vad_core.c)). It is exceptionally small and fast, but returns a binary decision rather than a calibrated probability.

**Neural VAD.** Silero, TEN VAD, FireRedVAD, FunASR FSMN-VAD, and NVIDIA MarbleNet learn speech/no-speech patterns across larger datasets and difficult noise conditions. They normally return probabilities and retain temporal state. They cost more than WebRTC but remain cheap compared with ASR.

**End-to-turn models.** Audio-native models can use prosody and pace; text models can use grammar and semantics; joint models can use both. These are endpointing layers, not replacements for a continuously running lightweight VAD.

## 2. Local tools already present in this repository

### 2.1 Installed runtime

Observed locally on 2026-08-16:

| Component | Local state |
|---|---|
| Platform | `arm64`, macOS 26.5.2 |
| `whisper-cli` | Installed at `/opt/homebrew/bin/whisper-cli`, whisper.cpp 1.9.2 |
| Whisper model | `models/ggml-base.en.bin`, approximately 141 MB |
| FFmpeg | 8.1.1; `silencedetect` and `silenceremove` filters enabled |
| SoX | Not installed |
| Silero wrapper | `silero-vad-notorch` 6.2.1.1 |
| Silero model | Bundled ONNX model, approximately 2.2 MB |
| ONNX Runtime | 1.28.0 |
| Audio Python packages | `sounddevice` 0.5.5, `soundfile` 0.14.0, NumPy 2.5.2 |
| Native whisper.cpp VAD model | No separate whisper.cpp Silero VAD model found under `models/` |

The dependency versions come from [`stt-local/uv.lock`](stt-local/uv.lock). `silero-vad-notorch` is an unofficial MIT-licensed packaging of Silero with Torch and TorchAudio removed; it forces the ONNX CPU path and bundles several model formats ([package repository](https://github.com/caffeinism/silero-vad-notorch), [PyPI release](https://pypi.org/project/silero-vad-notorch/)).

### 2.2 Current DONNA VAD configuration

Both [`stt-local/vad_listen.py`](stt-local/vad_listen.py) and [`stt-local/listen_daemon.py`](stt-local/listen_daemon.py) currently use:

| Setting | Value | Consequence |
|---|---:|---|
| Sample rate | 16 kHz mono float32 | Native Silero/Whisper rate; good |
| Frame size | 512 samples | 32 ms; required by current Silero at 16 kHz |
| Start threshold | 0.50 | Standard baseline |
| Effective stop threshold | 0.35 | Wrapper implements `threshold - 0.15` hysteresis |
| Minimum trailing silence | 500 ms | At least roughly half a second before an end event |
| Speech padding/pre-roll | 200 ms | Protects the start of speech from clipping |
| Clip queue | Unbounded in `vad_listen.py`; 10 in `listen_daemon.py` | Daemon is safer under ASR backlog |
| Whisper execution | New `whisper-cli` subprocess per utterance | Repeated process/model startup cost |

The frame size is correct. Silero v5 and later operate on fixed 512-sample frames at 16 kHz and 256-sample frames at 8 kHz ([Silero release notes](https://github.com/snakers4/silero-vad/releases)). The current wrapper's `VADIterator` returns start and end timestamps, but the application only uses the presence of the events. It prepends its own 200 ms ring buffer and sends all buffered frames through the moment the end event arrives. Therefore the WAV sent to Whisper includes the accumulated trailing silence rather than being trimmed to Silero's padded end timestamp.

### 2.3 Local microbenchmark

A warm, single-process test of the installed ONNX model processed 1,000 all-zero 512-sample frames in 104.58 ms:

- 0.1046 ms per 32 ms frame.
- Approximately 306× real time.
- CPU inference only.

This is a narrow performance measurement, not an accuracy benchmark. It proves that VAD inference is not the throughput bottleneck on this host. Audio capture, endpoint delay, process startup, Whisper model loading/inference, and downstream orchestration dominate perceived latency.

### 2.4 Current implementation risks

**The audio callback does too much.** It copies arrays, runs ONNX inference, appends Python objects, may concatenate an utterance, enqueues it, and prints status. PortAudio states that its callback runs at high or real-time priority and must avoid allocation, I/O, blocking, and functions with unpredictable duration ([PortAudio callback contract](https://portaudio.com/docs/v19-doxydocs/portaudio_8h.html)). A short inference often works, but the design can produce dropouts under scheduler pressure or simultaneous Whisper work. The callback should copy into a preallocated ring buffer or bounded queue; a normal worker thread should run VAD and segmentation.

**A continuous speech-positive event can grow memory indefinitely.** `speech_buffer` has no maximum duration. At 16 kHz float32 mono, raw samples accumulate at about 64 KB/s, or about 230 MB/hour, before Python/list overhead. Music, feedback, or persistent noise misclassified as speech can prevent an end event. The text-buffer watchdog does not bound this audio buffer.

**Whisper is cold-started for every clip.** `subprocess.run()` launches `whisper-cli` for each utterance and reloads the 141 MB model. A persistent whisper.cpp server/context or in-process binding should remove repeated model initialization.

**Only one important VAD parameter is externally tunable.** `STT_MIN_SILENCE_MS` is configurable, while threshold and padding are constants. There is no minimum utterance duration, maximum utterance duration, probability logging, or threshold sweep mechanism.

**VAD cannot solve playback feedback.** If the always-on daemon hears TTS through speakers, an ordinary VAD sees valid human-like speech. Full-duplex barge-in needs acoustic echo cancellation, a playback reference/muting policy, or a personalized/speaker-conditioned detector—not merely a higher threshold.

**The current 500 ms rule is an endpoint policy.** It is reasonable for commands but will split deliberate speakers at pauses longer than half a second. Raising it reduces false cuts but directly adds dead air after genuine completion. A second-stage turn detector is the principled way out of this tradeoff.

## 3. Local VAD tool survey

### 3.1 Decision matrix

| Tool | Method/output | Streaming | Runtime/footprint | License/constraint | Best use here |
|---|---|---:|---|---|---|
| Silero VAD v6 | Stateful neural probability | Yes | ONNX or Torch; ~2 MB class | MIT | Production baseline |
| `silero-vad-notorch` | Silero wrapper, ONNX CPU | Yes | Current installed Python path | MIT; unofficial wrapper | Minimal-dependency DONNA path |
| whisper.cpp native VAD | Silero model before Whisper | File/CLI; separate basic stream gate | Native C/C++; VAD model ~864 KB in docs | MIT | Batch/file trimming, not mic endpoint trigger |
| FireRedVAD | DFSMN neural probability; VAD and AED | Yes and batch | ~568k parameters/~2.2 MB weights; PyTorch reference | Apache-2.0 | Strong experimental challenger |
| TEN VAD | Neural probability | Yes | 16 kHz, 10/16 ms hops; native/ONNX/WASM | Apache-2.0 plus additional conditions | Native cross-platform challenger; review license |
| WebRTC VAD | GMM/LRT binary decision | Yes | Tiny native C | BSD-style core; wrapper licenses vary | CPU-minimal baseline/telephony |
| Picovoice Cobra | Proprietary neural probability | Yes | Native SDK, broad platforms | Account and AccessKey; commercial terms | Supported proprietary option |
| FunASR FSMN-VAD | FSMN neural segmentation | Yes/integrated ASR | 0.4M params; Python/native stack | Check model/runtime terms | Chinese/English FunASR deployments |
| NVIDIA MarbleNet | Neural frame VAD | Primarily toolkit pipeline | NeMo/PyTorch stack | Apache-2.0 code; model terms apply | Training/fine-tuning and diarization research |
| SpeechBrain CRDNN VAD | Neural frame VAD plus rich post-process | Mainly batch/offline | PyTorch research stack | Apache-2.0 code; model terms apply | Transparent experimentation |
| pyannote.audio | Segmentation/diarization toolkit | Mainly offline | Heavy PyTorch stack; gated models may require token | MIT code; model terms vary | Meetings/diarization, not low-latency mic gate |
| `@ricky0123/vad-web` | Silero through ONNX Runtime Web | Yes | Browser AudioWorklet/WASM | ISC package; Silero MIT | Browser/client-side capture |
| `wavekat-vad` | Unified Rust interface: WebRTC/Silero/TEN/FireRed | Yes | Native Rust plus optional ONNX | Check crate and model licenses | Reproducible cross-model lab |
| FFmpeg/librosa | Energy/silence threshold | Batch | Already available/standard Python | Open-source | Diagnostics and a cheap baseline |

### 3.2 Silero VAD

Silero is the safest default for this project:

- Supports 8 kHz and 16 kHz.
- Uses fixed 256/512-sample frames in current versions.
- Runs each 30+ ms chunk in under 1 ms on one CPU thread according to the project; the local measurement is about 0.105 ms.
- Published under MIT with no key, registration, telemetry, or network dependency.
- Available through TorchScript, ONNX, native/community bindings, browser runtimes, and whisper.cpp.
- Trained across more than 6,000 languages according to the project ([Silero repository](https://github.com/snakers4/silero-vad)).

Silero's March 2026 quality table reports v6 ROC-AUC 0.97 and frame accuracy 0.92 on its 17-hour multi-domain validation set, versus 0.73/0.74 for WebRTC and 0.93/0.87 for TEN VAD. It also shows substantial domain variation, which reinforces the need for local validation rather than a global winner ([Silero quality metrics](https://github.com/snakers4/silero-vad/wiki/Quality-Metrics)). These are Silero's own measurements and include private subsets; treat them as directional rather than neutral certification.

Known failure categories include voice-like instruments, very high-pitched/artificial voices, cartoons, and small children. A reported 2025 telephony regression between v5 and v6 also demonstrates that newer aggregate benchmark results do not guarantee improvement in every channel ([LiveKit Silero v5/v6 issue](https://github.com/livekit/agents/issues/3780)). Pin the exact model and regression-test upgrades.

For DONNA, the no-Torch wrapper is sensible. The official `silero-vad` Python package pulls in Torch/TorchAudio by default; the installed wrapper uses ONNX Runtime only. The tradeoff is that its streaming iterator exposes fewer policies than the current official offline timestamp function—most importantly no maximum speech duration.

### 3.3 whisper.cpp's built-in VAD

whisper.cpp 1.9.2 supports a separate Silero VAD model through `whisper-cli --vad --vad-model ...`. It detects speech segments first and only sends those segments to Whisper, reducing ASR work on long files ([whisper.cpp VAD documentation](https://github.com/ggml-org/whisper.cpp#voice-activity-detection-vad)). The documented model `silero-v6.2.0` is about 864 KB in GGML form.

The installed CLI exposes:

- Threshold: 0.50.
- Minimum speech: 250 ms.
- Minimum silence: 100 ms.
- Maximum speech: unlimited by default.
- Speech padding: 30 ms.
- Inter-segment overlap: 100 ms.

This is useful for recorded files and long batch transcription. It does not replace the external VAD that decides when a live microphone utterance is complete, because `whisper-cli` needs a completed input file before it can segment it. Running native VAD again on the already-segmented short WAV would duplicate work.

`whisper-stream` is different: its sliding-window mode uses what its own documentation calls a “very basic” VAD, controlled by an energy threshold ([whisper.cpp stream example](https://github.com/ggml-org/whisper.cpp/tree/master/examples/stream)). It is not equivalent to the CLI's model-backed Silero pass and is a weaker endpointing choice than the current implementation.

### 3.4 FireRedVAD

FireRedVAD was released as a standalone project in March 2026. It offers separate streaming and non-streaming models plus audio-event detection for speech, singing, and music in 100+ languages. The streaming model uses 10 ms frames, exposes threshold, start padding, minimum/maximum speech, and minimum silence, and has about 568k parameters ([FireRedVAD repository](https://github.com/FireRedTeam/FireRedVAD), [streaming implementation](https://github.com/FireRedTeam/FireRedVAD/blob/main/fireredvad/stream_vad.py)).

The project reports 97.57% F1 and 99.60% ROC-AUC on FLEURS-VAD-102, ahead of Silero, TEN, FunASR, and WebRTC. Caveats:

- The reported test set was described as “coming soon” in the repository, limiting independent reproduction at research time.
- The benchmark emphasizes multilingual read speech with manually added binary labels; DONNA is a far-field conversational endpointing problem.
- The reference implementation uses a broader PyTorch feature/inference stack than the current ONNX-only Silero wrapper.
- The project is new, so deployment maturity and model-version stability are less proven.

It is the first challenger worth benchmarking, especially because it includes a maximum-speech policy and 10 ms frame output. Do not migrate before an actual BRIO/AirPods corpus test.

### 3.5 TEN VAD

TEN VAD is a low-latency neural detector with 16 kHz input and optimized 160/256-sample hops (10/16 ms). The project ships native libraries for macOS, Linux, Windows, Android, iOS, and WebAssembly, with bindings across C, Python, Java, Go, and JavaScript. Its published macOS M1 real-time factor is 0.016 and its macOS library size is 731 KB ([TEN VAD repository](https://github.com/TEN-framework/ten-vad)).

TEN publishes a precision-recall comparison claiming better precision than Silero and WebRTC. Silero's own newer multi-domain table ranks Silero v6 above TEN. The apparent conflict is unsurprising: datasets, model versions, labels, thresholds, and post-processing differ. Neither result replaces a same-corpus test.

Licensing needs deliberate review. The repository describes the license as Apache 2.0 **with additional conditions**, not unmodified Apache 2.0. That may matter for redistribution or commercial integration.

### 3.6 WebRTC VAD, libfvad, and Python wheels

WebRTC VAD is still the best tiny baseline:

- Accepts 16-bit mono PCM at 8, 16, 32, or 48 kHz.
- Accepts only 10, 20, or 30 ms frames.
- Exposes aggressiveness modes 0–3.
- Returns a Boolean rather than a continuous score ([py-webrtcvad documentation](https://github.com/wiseman/py-webrtcvad)).

`libfvad` extracts the WebRTC VAD as a standalone BSD-3-Clause C library ([libfvad repository](https://github.com/dpirch/libfvad)). For Python, `webrtcvad-wheels` 2.0.14 is more convenient than the old source-only wrapper and publishes arm64 macOS wheels ([PyPI package](https://pypi.org/project/webrtcvad-wheels/)).

Its advantages are minuscule footprint, deterministic native performance, no ML runtime, and 48 kHz input support. Its disadvantages are binary output, rigid formats, weaker noise/speech discrimination in published Silero and FireRed evaluations, and less flexible threshold tuning. On this Mac, where Silero already consumes only about 0.1 ms per frame, WebRTC's extra speed has no practical value unless dependency size or embedded portability becomes the primary constraint.

### 3.7 Picovoice Cobra

Cobra runs audio inference locally and returns a voice probability. It supports macOS arm64/x86, Linux, Windows, iOS, Android, Raspberry Pi, browsers, and some microcontrollers. SDKs exist for C, Python, Node, Web, .NET, Android, and iOS ([Cobra documentation](https://picovoice.ai/docs/cobra/)).

The engine is proprietary. It requires a Picovoice account and AccessKey, even though audio processing is on-device. Picovoice claims markedly fewer errors and lower CPU use than Silero and WebRTC on its benchmark ([Cobra product benchmark](https://picovoice.ai/products/voice/voice-activity-detection/)). Those are vendor claims about a closed model. Cobra is appropriate when paid support, broad SDK coverage, and commercial accountability are worth key management and licensing constraints; it is a poor fit for a dependency-free, fully open, air-gapped pipeline.

### 3.8 FunASR FSMN-VAD

FunASR's `fsmn-vad` is a roughly 0.4M-parameter, 16 kHz model trained on 5,000 hours of Mandarin and English data. It is tightly integrated with FunASR's local ASR, punctuation, and diarization pipeline ([FunASR repository](https://github.com/modelscope/FunASR)). Native/runtime deployment is available, including an offline service and newer GGUF edge path ([FunASR runtime documentation](https://github.com/modelscope/FunASR/blob/main/runtime/docs/SDK_tutorial_en.md)).

It is compelling when adopting FunASR for Chinese/English recognition or wanting one integrated stack. It is not an obvious standalone improvement for an English whisper.cpp pipeline. FireRed's reported comparison also shows FSMN-VAD trading a very low miss rate for a high false-alarm rate, a reminder that default operating points differ.

### 3.9 Research-oriented VAD stacks

**NVIDIA NeMo MarbleNet** is NVIDIA's recommended multilingual frame-level VAD model and fits NeMo diarization/training workflows ([NeMo model guide](https://docs.nvidia.com/nemo/speech/nightly/starthere/choosing_a_model.html)). It offers fine-tuning and rich pipeline integration but brings a much heavier environment than this task warrants.

**SpeechBrain** provides a transparent CRDNN VAD pipeline with separate activation/deactivation thresholds and modular post-processing. It is excellent for learning, batch segmentation, and domain-specific training, but not the lightest always-on runtime.

**pyannote.audio** is primarily a segmentation and speaker-diarization toolkit. It is valuable for meetings, overlapping speech, and offline analysis, but model access can be gated and the standard pipeline is heavier and less streaming-oriented than Silero ([pyannote.audio repository](https://github.com/pyannote/pyannote-audio)).

### 3.10 Cross-platform wrappers and utilities

**sherpa-onnx** provides offline ONNX-based ASR, VAD, diarization, TTS, and other speech components across embedded, mobile, desktop, and many language bindings. It supports Silero and, in recent releases, TEN VAD ([sherpa-onnx repository](https://github.com/k2-fsa/sherpa-onnx), [change log](https://github.com/k2-fsa/sherpa-onnx/blob/master/CHANGELOG.md)). It is attractive for a future native unified runtime, but migrating only VAD to it adds integration work without an expected quality gain.

**`@ricky0123/vad-web`** provides browser microphone capture, AudioWorklet integration, and Silero inference through ONNX Runtime Web. Server-side Node support is being discontinued, so it is specifically a browser solution ([repository](https://github.com/ricky0123/vad)).

**`wavekat-vad`** exposes WebRTC, Silero, TEN, and FireRed behind one Rust trait and includes a local comparison lab. It documents each backend's exact frame/rate contract and can use local model paths for offline builds ([repository](https://github.com/wavekat/wavekat-vad)). It is promising as a reproducible benchmark harness; adopting it in DONNA would require a Python/native boundary or a Rust rewrite.

**FFmpeg and librosa** are useful sanity checks. If a neural VAD performs no better than a tuned energy baseline on the actual room corpus, the audio front end or labels likely need attention. They should not be the production conversational gate in a noisy environment.

## 4. Local endpoint and turn-detection tools

### 4.1 Pipecat Smart Turn v3.2

Smart Turn is the strongest practical local addition for DONNA's mid-sentence pause problem:

- Audio-native: consumes 16 kHz mono PCM and uses prosody as well as speech content.
- Designed to run after a lightweight VAD detects silence.
- Supports up to the latest eight seconds of the current turn.
- Supports 23 languages, including English and Japanese.
- Approximately 8 MB quantized CPU and 32 MB FP32 GPU variants.
- Project-reported inference is roughly 10–100 ms depending on CPU; Pipecat Cloud reports about 65 ms on a standard instance.
- BSD-2-Clause code, model, training code, and datasets are published ([Smart Turn repository](https://github.com/pipecat-ai/smart-turn)).

A practical policy is: Silero detects 250–400 ms of silence; Smart Turn judges the accumulated turn; if incomplete, continue listening; if complete, flush immediately; enforce a maximum wait regardless of the model. This can reduce both false cutoffs and fixed post-speech delay.

The current DONNA worker only transcribes after the VAD flush, but Smart Turn is audio-native, so it can be inserted before ASR without requiring partial text. The cost is another model and a more complex state machine.

### 4.2 TEN Turn Detection

TEN Turn Detection classifies transcript text as `finished`, `unfinished`, or `wait`. It uses a Qwen2.5-7B transformer and supports English and Chinese. The project reports high accuracy on its published bilingual test set ([TEN Turn Detection repository](https://github.com/TEN-framework/ten-turn-detection)).

It is much heavier than Smart Turn, depends on an ASR result before the endpoint decision, and its license is Apache 2.0 with additional restrictions. It is excessive for this local command loop unless a capable local LLM is already loaded and its semantic result is reused.

### 4.3 LiveKit turn detector and benchmark

LiveKit's detector combines acoustic and semantic evidence and currently leads its open end-of-turn benchmark. At a 5% false-cutoff budget, the published English result shows 543 ms mean endpoint delay, versus 1,600 ms for the silence-only VAD baseline and 1,051 ms for Smart Turn v3.2 ([LiveKit EOT benchmark](https://github.com/livekit/eot-bench)). This is useful evidence that VAD-only endpointing has a hard quality/latency ceiling.

The benchmark is more important to this project than the LiveKit model itself. LiveKit's model license has restricted standalone use to the LiveKit Agents framework, and the local-plugin path has been in transition. Do not embed it in DONNA without checking the exact current model license and runtime path.

## 5. Why public benchmarks disagree

VAD leaderboards are not interchangeable. Results change with:

- Dataset domain: read speech, meetings, calls, movies, far-field rooms, or pure noise.
- Sample rate, codec, microphone, and signal-to-noise ratio.
- Frame length and label boundary tolerance.
- Whether music with vocals, singing, breath, laughter, or backchannels count as speech.
- Threshold selection and whether it was tuned on the test domain.
- Smoothing, hysteresis, minimum duration, padding, and merge rules.
- Metric choice and class balance.
- Model version; “Silero” can mean v3, v4, v5, or v6.
- Streaming versus offline models and access to future context.

Examples of conflicting but plausible claims:

- Silero's 2026 multi-domain benchmark ranks Silero v6 ahead of TEN.
- TEN's repository reports a superior precision-recall curve over Silero and WebRTC.
- FireRed reports higher F1 than both on FLEURS-VAD-102.
- Cobra reports much lower error rates than Silero and WebRTC on its proprietary engine benchmark.

All may be true at their chosen operating points. None establishes the best detector for a specific room, speaker, microphone, and endpoint policy.

Frame accuracy alone is particularly weak because non-speech often dominates. A detector can achieve high accuracy while still clipping the first consonant of many commands or firing repeatedly on one television. ROC-AUC measures ranking across thresholds, not the quality of the chosen production threshold. End-of-turn metrics also differ from frame VAD metrics.

## 6. How to evaluate VAD for DONNA

### 6.1 Build the right corpus

Record the actual capture path, not downloaded clean speech. Include both primary devices and the conditions that cause failures:

- BRIO near-field and far-field speech.
- AirPods microphone speech.
- Quiet room, HVAC/fan, keyboard, chair movement, doors, dishes, and footsteps.
- TTS playback through speakers at several volumes.
- Television, podcasts, music with and without vocals.
- Short commands and short wake phrases.
- Quiet speech, loud speech, whispering, laughter, throat clearing, and breathing.
- Hesitations of 200, 400, 600, 1,000, and 1,500 ms inside an utterance.
- Fast back-to-back utterances separated by short gaps.
- Long monologues and continuous non-speech events.

Annotate speech start/end, intended utterance boundaries, and whether the agent should act. VAD labels and turn labels must be separate.

### 6.2 Measure application outcomes

At minimum:

| Metric | Why it matters |
|---|---|
| Missed utterance rate | Commands never reach ASR |
| False utterances/hour | Noise creates ASR work or accidental actions |
| Front-end clipping rate/ms | Lost initial consonants damage wake-word recognition |
| Tail clipping rate/ms | Lost final words damage intent |
| False cutoff rate | Mid-thought pauses become separate commands |
| Endpoint delay | Silence after true completion before ASR starts |
| Downstream wake-word recall | Direct product outcome |
| Downstream WER/command success | Boundary quality affects ASR |
| VAD CPU time and process RSS | Always-on cost |
| Input overflow/drop count | Real-time implementation health |
| Clip queue depth/drop count | ASR throughput health |
| Maximum live audio-buffer bytes | Runaway-event safety |

For frame classification, also report precision-recall curves, false-positive rate, false-negative rate, and AUC. For conversational endpointing, use the approach in LiveKit's EOT benchmark: compare mean latency at a fixed false-cutoff budget and false cutoffs at fixed latency budgets.

### 6.3 Run a controlled bakeoff

Use identical resampled PCM and identical post-processing wherever possible:

1. Current Silero v6 baseline.
2. FireRed streaming VAD.
3. TEN VAD.
4. WebRTC mode 0–3 baseline.
5. Energy threshold baseline.

Sweep thresholds and trailing-silence settings. Select a Pareto frontier, not one aggregate score. Then test two endpoint policies on the best frame VAD:

- Fixed silence only.
- VAD candidate silence plus Smart Turn, with a fixed maximum timeout.

Pin model hashes and save raw frame probabilities/events so future upgrades can be compared without re-recording audio.

## 7. Tuning guide

| Parameter | Lower value | Higher value |
|---|---|---|
| Speech/start threshold | Better recall; more noise triggers | Fewer false triggers; more quiet/distant speech misses |
| Exit threshold | Ends sooner but may chatter | Holds speech state longer |
| Minimum speech | Preserves short words/noises | Rejects clicks; may drop short wake words |
| Minimum silence | Faster endpoint; more mid-thought splits | Fewer splits; more response delay |
| Pre-roll/start pad | Less memory; more initial clipping | Safer first phoneme; more leading noise |
| End pad | Tighter clip | Safer final phoneme; more trailing audio |
| Maximum speech | More forced splits | More memory/latency exposure |
| Probability smoothing | More responsive | More stable but adds latency |

Recommended starting experiment ranges for the current 16 kHz command use case—not universal defaults:

- Start threshold: 0.35–0.65 in 0.05 steps.
- Minimum silence: 250–800 ms.
- Pre-roll: 150–300 ms.
- Minimum speech: 100–250 ms, with explicit short wake-word tests.
- Maximum speech: 20–45 seconds for commands; split at the best recent silence.
- Endpoint maximum wait with Smart Turn: roughly 1.2–2.0 seconds.

Thresholds should be calibrated per input path if BRIO and AirPods probability distributions differ materially. Avoid silently changing the threshold based on instantaneous energy; normalization and noise suppression should be tested separately because they also change the detector's score distribution.

## 8. Recommended implementation sequence

### Phase 0: preserve the current baseline

- Pin `silero-vad-notorch==6.2.1.1`, ONNX Runtime, and the exact bundled model hash.
- Make threshold, exit/hysteresis if supported, minimum silence, speech padding, and maximum speech configuration explicit.
- Add optional structured event/probability logging for recorded evaluation, disabled in normal operation.

### Phase 1: make capture real-time safe

- Restrict the PortAudio callback to copying fixed-size frames into a preallocated/bounded queue or ring buffer.
- Run Silero and segmentation on a normal worker thread.
- Add hard limits for queued frames, live utterance duration, and live utterance bytes.
- On overflow, record a metric, reset VAD state, and recover to listening rather than retaining a partial unbounded clip.
- Preserve timestamps so padding and trailing silence can be applied precisely rather than retaining every frame through the end event.

### Phase 2: remove ASR cold starts

- Keep one whisper.cpp model/context alive.
- Submit complete utterance buffers to it without a process/model reload.
- Measure VAD-end-to-final-transcript time separately from capture and endpoint delay.

This likely improves perceived latency more than any VAD swap.

### Phase 3: tune on real audio

- Build the corpus and harness described above.
- Tune current Silero before comparing models.
- Establish acceptance limits for false cutoffs, false activations/hour, clipping, queue drops, and endpoint latency.

### Phase 4: add semantic endpointing only if needed

- Keep Silero for frame detection and barge-in onset.
- Invoke Smart Turn only at candidate silence.
- Resume accumulation on `incomplete`; flush on `complete`; enforce a maximum timeout.
- Keep a manual/push-to-talk fallback for environments where acoustic endpointing remains unreliable.

### Phase 5: challenge Silero

- Benchmark FireRed, TEN, and WebRTC on the frozen corpus.
- Change production VAD only for a measured application-level win.
- Review model and runtime licensing before redistribution, especially TEN and proprietary Cobra.

## 9. Bottom line for DONNA

The current model choice is sound. Silero's local cost is negligible, the input format is correct, and the 200 ms pre-roll addresses the most common start-clipping failure. The immediate engineering weaknesses are around Silero:

- Real-time-unsafe callback work.
- No maximum speech duration.
- Imprecise use of returned timestamps.
- Limited parameter observability/tuning.
- Repeated Whisper process and model startup.
- Fixed-silence endpointing being asked to solve conversational turn completion.

Fix those first. Then evaluate Smart Turn for pause handling. Treat FireRedVAD as the most interesting new model experiment, TEN as the most portable native alternative, WebRTC as the tiny baseline, and Cobra as the supported proprietary option. Keep the winner tied to reproducible tests from the actual microphones and room rather than public headline metrics.

## Source index

### Core VAD and implementations

- [Silero VAD repository](https://github.com/snakers4/silero-vad)
- [Silero quality metrics](https://github.com/snakers4/silero-vad/wiki/Quality-Metrics)
- [Silero VAD utility/post-processing source](https://github.com/snakers4/silero-vad/blob/master/src/silero_vad/utils_vad.py)
- [silero-vad-notorch on PyPI](https://pypi.org/project/silero-vad-notorch/)
- [whisper.cpp VAD documentation](https://github.com/ggml-org/whisper.cpp#voice-activity-detection-vad)
- [whisper.cpp streaming example](https://github.com/ggml-org/whisper.cpp/tree/master/examples/stream)
- [FireRedVAD repository](https://github.com/FireRedTeam/FireRedVAD)
- [FireRedASR2S paper](https://arxiv.org/abs/2603.10420)
- [TEN VAD repository](https://github.com/TEN-framework/ten-vad)
- [WebRTC VAD core](https://chromium.googlesource.com/external/webrtc/+/HEAD/common_audio/vad/vad_core.c)
- [py-webrtcvad repository](https://github.com/wiseman/py-webrtcvad)
- [libfvad repository](https://github.com/dpirch/libfvad)
- [webrtcvad-wheels on PyPI](https://pypi.org/project/webrtcvad-wheels/)
- [Picovoice Cobra documentation](https://picovoice.ai/docs/cobra/)
- [FunASR repository](https://github.com/modelscope/FunASR)
- [NVIDIA NeMo speech model guide](https://docs.nvidia.com/nemo/speech/nightly/starthere/choosing_a_model.html)
- [SpeechBrain VAD tutorial](https://speechbrain.readthedocs.io/en/latest/tutorials/tasks/voice-activity-detection.html)
- [pyannote.audio repository](https://github.com/pyannote/pyannote-audio)
- [sherpa-onnx repository](https://github.com/k2-fsa/sherpa-onnx)
- [Browser VAD using Silero](https://github.com/ricky0123/vad)
- [wavekat-vad multi-backend Rust library](https://github.com/wavekat/wavekat-vad)

### Endpointing, turn detection, and evaluation

- [Pipecat Smart Turn](https://github.com/pipecat-ai/smart-turn)
- [TEN Turn Detection](https://github.com/TEN-framework/ten-turn-detection)
- [LiveKit turn-taking overview](https://docs.livekit.io/agents/logic/turns/)
- [LiveKit end-of-turn benchmark](https://github.com/livekit/eot-bench)
- [Semantic VAD paper](https://arxiv.org/abs/2305.12450)
- [AVA-Speech dataset paper](https://arxiv.org/abs/1808.00606)
- [Window Size Versus Accuracy Experiments in VADs](https://arxiv.org/abs/2601.17270)

### Audio and baseline tooling

- [PortAudio callback contract](https://portaudio.com/docs/v19-doxydocs/portaudio_8h.html)
- [FFmpeg audio filters](https://ffmpeg.org/ffmpeg-filters.html)
- [librosa non-silent interval splitting](https://librosa.org/doc/main/generated/librosa.effects.split.html)
- [RNNoise local noise suppression](https://github.com/xiph/rnnoise)
