# Deep Research: Acoustic Echo Cancellation, Audio Fingerprinting, and Local Tools

**Research date:** 2026-08-16  
**Target:** DONNA on Apple Silicon macOS, with local Kokoro TTS, `afplay` playback, `sounddevice` microphone capture, Silero VAD, and local Whisper transcription  
**Scope:** acoustic echo cancellation (AEC), echo suppression, double-talk/barge-in, local and platform-native tools, audio fingerprints, audio watermarks, and the claim that Claude fingerprints its outputs

## Executive conclusion

DONNA does not need to fingerprint or watermark its TTS to solve self-hearing. It already owns a much stronger signal: the exact PCM waveform it sends to the speaker. A real echo canceller consumes that render/playback signal as a synchronized reference, estimates how the loudspeaker, room, microphone, device latency, and clock transform it, and subtracts the estimated echo from the microphone signal. The cleaned microphone signal then goes to VAD and STT.

An audio fingerprint or watermark answers a different question: “Is this audio probably derived from a known recording or generator?” It does not reconstruct the user’s speech from a mixture of user speech and loudspeaker echo. Muting every frame in which a watermark is detected would also mute the user during double-talk, defeating barge-in.

The recommended order for DONNA is:

1. **Immediate reliable mode, without barge-in:** explicitly pause or discard microphone/VAD input from just before TTS playback until roughly 150–300 ms after playback drains. This is half-duplex turn-taking, not AEC.
2. **Preferred full-duplex implementation on this Mac:** move Kokoro playback and microphone capture into one `AVAudioEngine` graph and enable Apple voice processing with `setVoiceProcessingEnabled(true)`. Apple describes its main use as echo cancellation/VoIP and says audio emitted by the device is removed from incoming audio. Both I/O nodes participate, and the engine must be stopped when voice processing is enabled. [Apple WWDC19](https://developer.apple.com/videos/play/wwdc2019/510/)
3. **Portable fallback or explicit DSP control:** use WebRTC Audio Processing/AEC3 with 10 ms render and capture frames, a delay hint, and a shared full-duplex clock. WebRTC AEC3 exposes the exact required lifecycle: analyze/store render audio, analyze capture audio, and process capture to remove echo. [WebRTC AEC3 interface](https://webrtc.googlesource.com/src/+/master/modules/audio_processing/aec3/echo_canceller3.h)
4. **Simpler fallback:** SpeexDSP AEC plus its residual echo suppressor. It is lightweight and permissively licensed, but older and generally less robust than current WebRTC AEC3 in difficult double-talk, nonlinear, or moving-device conditions. [SpeexDSP AEC manual](https://www.speex.org/docs/manual/speex-manual/node7.html)
5. **Optional secondary safeguards:** playback/microphone correlation, transcript similarity, or an AudioSeal watermark can detect likely self-audio. Use them for telemetry, delay estimation, provenance, or last-resort command rejection—not as the primary echo-removal path.

The Claude claim does not hold up as stated. As of this research date, no official Anthropic source found says Claude embeds a fingerprint or watermark in generated text or images. Anthropic says Claude does not currently generate image or video outputs, describes its outputs as text-based (including TTS voice output), and says it continues to explore watermarking. Anthropic’s documented image hash matching applies to **images uploaded by users** and matches known CSAM; it is not a mark embedded in Claude output. [Anthropic Transparency Hub](https://www.anthropic.com/transparency/voluntary-commitments/security%26privacy), [Anthropic safeguards](https://www.anthropic.com/news/building-safeguards-for-claude)

## 1. What acoustic echo cancellation actually removes

Let:

- `x[n]` be the digital far-end/render signal sent to the loudspeaker—DONNA’s TTS;
- `h[n]` be the acoustic and electroacoustic path through output buffering, DAC, loudspeaker, room reflections, microphone, ADC, and input buffering;
- `D` be bulk delay;
- `s[n]` be the user’s near-end speech;
- `v[n]` be other noise;
- `y[n]` be the microphone signal.

A simplified model is:

```text
y[n] = s[n] + (h * x)[n - D] + v[n]
```

The AEC maintains an adaptive estimate `ĥ` and produces:

```text
e[n] = y[n] - (ĥ * x)[n - D̂]
```

`e[n]` should retain the user and ambient sound while removing as much of the loudspeaker echo as possible. In a complete system, a residual echo suppressor then attenuates echo that the linear filter cannot model.

This is why simple polarity reversal or “phase cancellation” is not sufficient. The microphone does not receive a delayed copy of the original waveform with one fixed gain. It receives hundreds or thousands of delayed reflections, frequency-dependent speaker and microphone responses, operating-system buffering, time-varying geometry, and often nonlinear distortion. The adaptive filter is continually identifying that path.

### Related but different problems

| Problem | Meaning | Correct tool |
|---|---|---|
| Acoustic echo | Speaker output re-enters a microphone | AEC with a render reference |
| Residual echo | Echo left after the linear filter | Residual echo suppressor / nonlinear processor |
| Reverberation | A speaker’s own voice persists through room reflections | Dereverberation |
| Acoustic feedback | A closed mic-to-speaker loop howls or grows unstable | Feedback suppressor, gain control, routing changes |
| Electrical sidetone | A device intentionally feeds mic into earpiece | Device/telephony routing controls |
| Noise | Fans, typing, traffic, HVAC | Noise suppression |
| Self-triggered ASR | Assistant output becomes a transcript or command | AEC first; command policy as defense in depth |

Noise suppression alone cannot solve echo because TTS is structured speech, not stationary noise. VAD cannot solve it because TTS is valid speech. Speaker recognition can label a source but does not unmix simultaneous sources.

## 2. Why full-duplex AEC is difficult

### 2.1 Render-reference timing

The canceller must receive the exact playback stream before the corresponding acoustic echo arrives in capture. Speex’s documentation is unusually direct: the signal in the microphone must already have been submitted as playback reference, while excessive reference delay wastes filter capacity. If the delay exceeds the filter tail, cancellation fails. [Speex AEC timing guidance](https://www.speex.org/docs/manual/speex-manual/node7.html)

WebRTC AEC3 accepts an optional audio-buffer delay and partly handles jitter between render and capture API calls, but it still needs ordered 10 ms render and capture frames. [WebRTC AEC3 interface](https://webrtc.googlesource.com/src/+/master/modules/audio_processing/aec3/echo_canceller3.h)

### 2.2 Clock drift

If playback and capture use different hardware clocks, their sample counts slowly diverge even if both advertise 48 kHz. The reference then slides relative to the echo. Resampling/drift compensation is possible, but avoiding the problem is better: use one full-duplex device or a clock-locked aggregate device. Speex explicitly warns that unrelated capture and playback sound cards generally will not work. [Speex troubleshooting](https://www.speex.org/docs/manual/speex-manual/node7.html)

### 2.3 Double-talk

Double-talk is the central barge-in case: DONNA is speaking and the user begins speaking. A naive adaptive filter may interpret the user as unexplained error and corrupt its echo-path estimate. Practical AEC systems detect double-talk or otherwise control adaptation so the near-end voice is preserved.

Echo suppression by gating is half-duplex: it can make the mic quiet while DONNA talks, but it cannot retain the user. A full-duplex AEC must balance two competing errors:

- too little suppression leaves DONNA’s voice and false-triggers VAD/STT;
- too much suppression damages the user, clips syllables, or produces watery/musical artifacts.

ITU testing guidance treats double-talk separately because echo controllers can introduce clipping, distortion, noise contrast, and level changes while both parties speak. [ITU-T G.168](https://www.itu.int/epublications/es/publication/itu-t-g-168-2015-cor-1-2022-12-reference-error-corrections), [ITU-T P.831](https://www.itu.int/rec/T-REC-P.831-199812-I/en)

### 2.4 Nonlinear hardware

Clipping, limiter behavior, loudspeaker distortion, automatic volume processing, and enclosure vibration generate components that a linear filter cannot predict. Speex recommends avoiding saturation because nonlinear distortion cannot be modeled by its linear adaptive filter. Modern systems therefore combine linear cancellation with nonlinear/residual suppression, sometimes neural. [Speex troubleshooting](https://www.speex.org/docs/manual/speex-manual/node7.html)

### 2.5 Path changes

Moving the laptop, changing volume, rotating a speaker, covering a microphone, or switching devices changes the impulse response. The canceller must converge again without a long burst of echo. WebRTC AEC3 contains delay controllers, render buffers, an echo remover, saturation detection, multichannel handling, and residual-echo estimation rather than being one static filter. [WebRTC AEC3 source](https://webrtc.googlesource.com/src/+/master/modules/audio_processing/aec3/echo_canceller3.h), [WebRTC block processor](https://webrtc.googlesource.com/src/+/main/modules/audio_processing/aec3/block_processor.h)

## 3. Correct processing order

For DONNA, the voice pipeline should be:

```text
Kokoro PCM ───────────────┬──> render-reference queue ──┐
                          └──> output device/speaker     │
                                                        ▼
microphone ──> HPF ──> linear AEC ──> residual echo suppression
                                           ──> noise suppression
                                           ──> optional AGC
                                           ──> VAD
                                           ──> Whisper/STT
                                           ──> wake/control policy
```

AEC must precede VAD and STT. Running Silero VAD on the raw microphone asks it to distinguish two valid speech sources without the reference signal. It was not designed to know that one source belongs to DONNA.

AGC normally follows echo control. Aggressive gain before AEC can amplify residuals or cause clipping. WebRTC’s current processing implementation combines AEC3 with high-pass filtering, noise suppression, and gain-control modules. [WebRTC Audio Processing implementation](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/audio_processing_impl.cc)

## 4. DONNA’s current architecture and the missing reference path

### 4.1 Repository findings

The local repository currently shows:

- `stt-local/listen_daemon.py` opens a mono `sounddevice.InputStream` and continuously services it.
- The same daemon starts the Tink cue through a separate asynchronous `/usr/bin/afplay` process.
- The broader architecture documents Kokoro WAV output played through `afplay`.
- No AEC, echo suppressor, render-reference ring, synchronized duplex stream, or voice-processing API is present.
- [`PROGRESS.md`](../../../PROGRESS.md) records a self-feedback case.
- [`README.md`](../../../README.md) says the listener is stopped during replies, but the current daemon code contains an always-running input stream and no playback-state control tied to external Kokoro/`afplay`. The documentation and current runtime path should therefore be reconciled during implementation.

The decisive problem is the process boundary:

```text
Kokoro WAV ──> afplay process ──> speaker

sounddevice InputStream ──> raw mic ──> Silero VAD ──> Whisper
```

The capture process has no sample-aligned copy of what `afplay` actually renders and no reliable playback timestamp. Having the WAV file is necessary but not sufficient for robust AEC: the implementation must feed its decoded PCM into the canceller in the order and timing used for playback.

### 4.2 Local machine/tool audit

Observed on 2026-08-16:

| Item | Local status | Relevance |
|---|---|---|
| Platform | Apple Silicon `arm64`, macOS 26.5.2 | Apple native voice-processing path is available to test |
| `/usr/bin/afplay` | Installed and currently used | Convenient player; not an AEC engine or reference API |
| `/opt/homebrew/bin/ffplay` | Installed | Can stream audio, but does not by itself cancel echo |
| Swift toolchain | `/usr/bin/swift` installed | Suitable for a small native `AVAudioEngine` helper |
| Python `sounddevice` | Project dependency | Provides PortAudio input/output streams, not built-in AEC |
| SpeexDSP | Not installed as a Homebrew formula | Available to add, but not currently local |
| WebRTC audio-processing library | Not installed | Must be built, bundled, or consumed through a binding |
| GStreamer | Not installed | Its WebRTC DSP pipeline is an optional alternative |
| PipeWire/PulseAudio | Not installed; Linux-oriented | Not a natural macOS solution |
| Chromaprint/`fpcalc` | Not installed | Fingerprinting is optional and does not replace AEC |

`python-sounddevice` is a PortAudio binding and supports a simultaneous input/output `Stream`, which is useful for building a synchronized callback. Its real-time callback must avoid blocking operations and allocations. [python-sounddevice documentation](https://python-sounddevice.readthedocs.io/en/latest/), [PortAudio API](https://portaudio.com/docs/v19-doxydocs/)

Neither `afplay`, `ffplay`, nor PortAudio automatically creates an AEC reference. A media player and an echo canceller are separate roles.

## 5. Local and platform AEC options

### 5.1 Apple `AVAudioEngine` voice processing — best first experiment

Apple exposes `AVAudioIONode.setVoiceProcessingEnabled(_:)`. `AVAudioIONode` represents macOS input and output nodes connected to the system default devices. [AVAudioIONode](https://developer.apple.com/documentation/avfaudio/avaudioionode), [setVoiceProcessingEnabled](https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled%28_%3A%29)

Apple’s WWDC explanation supplies the important operational details:

- the primary use is echo cancellation and VoIP;
- incoming audio gets extra processing and audio emitted by the device is removed;
- both input and output nodes must be in voice-processing mode, which the engine coordinates;
- voice processing is available only while rendering to an audio device, not manual rendering;
- the engine must be stopped before enabling or disabling it;
- `AVAudioSourceNode` and `AVAudioSinkNode` provide real-time render/capture blocks. [Apple WWDC19 transcript](https://developer.apple.com/videos/play/wwdc2019/510/)

#### Suggested DONNA shape

```text
Kokoro WAV/PCM -> AVAudioPlayerNode or AVAudioSourceNode -> engine output
engine input -> voice processing -> AVAudioSinkNode/tap -> Python VAD/STT pipe
```

This replaces `afplay` for assistant speech. A Swift helper can own the full-duplex engine and expose cleaned mono PCM to Python through a local pipe/socket while accepting Kokoro PCM plus stop/interrupt commands.

Advantages:

- native, local, and optimized for Apple hardware;
- render and capture are inside one audio graph;
- no large third-party WebRTC build;
- likely the shortest path to robust barge-in on this exact machine.

Risks and constraints:

- it changes the audio architecture rather than adding one Python function;
- the engine uses system-default macOS I/O unless lower-level Core Audio routing is added;
- route switches require restarting/rebuilding audio state;
- a current unanswered Apple Developer Forums report describes failures when input and output are mismatched devices and an aggregate-device channel count does not match. Treat this as a test warning, not a documented universal limitation. [Apple forum report](https://developer.apple.com/forums/thread/810129)

Prefer paired routes: Mac microphone + Mac speakers, or the input/output sides of the same headset/device. AirPods already provide large passive acoustic isolation, but route/profile changes still need tests.

### 5.2 WebRTC Audio Processing Module / AEC3 — best portable DSP

WebRTC AEC3 is the strongest open-source conventional default for a local cross-platform voice agent. Its public class receives 10 ms band-split frames, stores render audio through `AnalyzeRender`, detects capture saturation through `AnalyzeCapture`, removes echo in `ProcessCapture`, accepts an external buffer-delay estimate, and exposes metrics. It is BSD-style licensed with a separate patent grant. [AEC3 header](https://webrtc.googlesource.com/src/+/master/modules/audio_processing/aec3/echo_canceller3.h), [WebRTC license](https://webrtc.googlesource.com/src/+/refs/heads/main/LICENSE), [WebRTC patent grant](https://webrtc.googlesource.com/src/+/refs/heads/main/PATENTS)

For DONNA:

- run audio I/O at 48 kHz if that is the native full-duplex device rate;
- process exactly 480 samples/channel per 10 ms frame;
- submit the output frame as render/reference before processing its corresponding mic frame;
- give AEC3 the measured stream/buffer delay or let its estimator converge;
- resample **after AEC** to 16 kHz for the current VAD/Whisper path if required;
- reset or reconverge on device switches, discontinuities, or major volume changes;
- log raw mic, render reference, and cleaned output with shared timestamps.

Integration choices:

1. Build a small C++ bridge around upstream WebRTC APM.
2. Use Freedesktop’s `webrtc-audio-processing` extraction, which packages the APM used by GStreamer/PipeWire. [Freedesktop SDK component](https://freedesktop-sdk.gitlab.io/documentation/reference/api-reference/fdsdk/elements/components/webrtc-audio-processing.html)
3. Evaluate a third-party Python wrapper. `pywebrtc-audio` currently advertises AEC3, 16/32/48 kHz support, 10 ms internal framing, delay hints, and macOS arm64 wheels. It is a convenient prototype candidate, not an upstream Google/WebRTC package; pin and audit its bundled WebRTC revision and licenses before depending on it. [pywebrtc-audio repository](https://github.com/strands-labs/pywebrtc-audio)

The DSP library still does not solve I/O synchronization. DONNA must replace external `afplay` with a duplex playback path or otherwise provide an accurate render stream and delay model.

### 5.3 SpeexDSP — lightweight conventional fallback

SpeexDSP exposes a direct three-buffer API: microphone frame, played frame, and cleaned output. Its manual recommends roughly 20 ms frames, a filter tail related to room reverberation, tight render/capture timing, and optional association with its preprocessor for residual echo suppression. It also supports a playback/capture API for asynchronous threads, at the cost of added frame delay. [Speex AEC API](https://github.com/xiph/speexdsp/blob/master/include/speex/speex_echo.h), [Speex manual](https://www.speex.org/docs/manual/speex-manual/node7.html)

Advantages:

- small C library;
- simple API;
- permissive license;
- useful baseline and embedded option.

Limitations:

- older design;
- manual delay/tail tuning matters greatly;
- different soundcard clocks are a documented failure mode;
- residual suppression and difficult double-talk behavior are less sophisticated than a modern WebRTC stack.

### 5.4 GStreamer WebRTC DSP — useful pipeline wrapper

GStreamer provides `webrtcdsp` plus `webrtcechoprobe`. The probe must sit on the far-end stream before playback; DSP sits near capture. Their sample rates must match and both elements must share one top-level pipeline. This is a concrete demonstration that AEC needs the playback reference, not merely a “remove echo” flag. [GStreamer `webrtcdsp`](https://gstreamer.freedesktop.org/documentation/webrtcdsp/webrtcdsp.html), [GStreamer `webrtcechoprobe`](https://gstreamer.freedesktop.org/documentation/webrtcdsp/webrtcechoprobe.html)

This could work locally after installing GStreamer, but a Swift Apple path or direct WebRTC bridge fits the current macOS/Python architecture better.

### 5.5 PipeWire echo-cancel — strong Linux system solution, not macOS-native

PipeWire’s echo-cancel module creates virtual capture/playback nodes and uses `aec/libspa-aec-webrtc` by default. It is attractive for Linux deployments because applications can select an echo-cancelled virtual microphone without embedding AEC. [PipeWire echo-cancel module](https://pipewire.pages.freedesktop.org/pipewire/page_module_echo_cancel.html)

It is not installed here and is not a normal macOS audio-server choice. Keep it for a future Linux DONNA port.

### 5.6 Browser/WebRTC capture

If DONNA gains a browser frontend, `getUserMedia` can request `echoCancellation: true`. The W3C specification defines boolean echo cancellation and newer `all`/`remote-only` modes, while actual behavior depends on the browser and device. [W3C Media Capture and Streams](https://www.w3.org/TR/mediacapture-streams/), [MDN echo-cancellation constraint](https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackConstraints/echoCancellation)

This is easy for a web client, but does not clean the current Python `sounddevice` stream.

### 5.7 Neural and hybrid AEC

Neural AEC can improve nonlinear residual echo and difficult acoustic paths. It is not automatically a better first integration because models still require a synchronized far-end reference, add latency/compute, and can distort near-end speech outside their training distribution.

Notable local research options:

- **DTLN-AEC:** pretrained 1.8M, 3.9M, and 10.4M parameter TensorFlow Lite models, MIT-licensed, submitted to the 2021 AEC Challenge. [DTLN-AEC](https://github.com/breizhn/DTLN-aec)
- **LocalVQE:** current community project with GGML/PyTorch streaming AEC variants, all requiring a far-end loopback/reference. It reports 16 ms hops and publishes AECMOS/ERLE results, but its own benchmark claims need local reproduction. [LocalVQE](https://github.com/localai-org/LocalVQE)
- **Microsoft AEC Challenge:** open real and synthetic datasets covering single-talk and double-talk on more than 10,000 devices, plus AECMOS tooling and latency-constrained evaluations. [Microsoft AEC Challenge](https://github.com/microsoft/AEC-Challenge)
- **NVIDIA Maxine Audio Effects:** offers GPU AEC, but requires supported NVIDIA RTX hardware and is mismatched to this Apple Silicon target. [NVIDIA Maxine AFX SDK](https://github.com/NVIDIA-Maxine/Maxine-AFX-SDK)

For DONNA, first establish correct duplex I/O and a classical Apple/WebRTC baseline. A neural residual suppressor is an optimization after the three synchronized signals—render, microphone, output—are measurable.

## 6. “Claude fingerprints text and images” — claim check

### 6.1 What Anthropic officially says

The official evidence found points away from the claim:

- Anthropic’s Transparency Hub says Claude has multimodal input and text-based outputs, including TTS voice output. It says watermarking is most commonly applied to image output, which Anthropic does not currently provide, and that Anthropic continues to explore the field. [Anthropic Transparency Hub](https://www.anthropic.com/transparency/voluntary-commitments/security%26privacy)
- The same page says Claude does not generate image or video outputs.
- Anthropic’s documented hash matching compares **uploaded images** against databases of known CSAM. That is input safety scanning, not output watermarking or authorship fingerprinting. [Building safeguards for Claude](https://www.anthropic.com/news/building-safeguards-for-claude)
- Anthropic’s Help Center likewise says users may upload images for Claude to analyze, but Claude does not produce images. [Anthropic Help Center](https://support.anthropic.com/es/articles/9002504-puede-claude-producir-imagenes)

Careful conclusion: **no official public evidence was found that Claude embeds a detectable fingerprint/watermark in generated text, image, or voice output.** This cannot prove that no undisclosed system exists, but the public claim should not be treated as an engineering premise.

The rumor may conflate Claude with:

- Google DeepMind’s SynthID, which explicitly watermarks Google-generated text, images, video, and some audio;
- C2PA Content Credentials and signed provenance metadata;
- Anthropic’s uploaded-image hash matching;
- statistical AI-text detectors, which estimate origin without an embedded mark.

### 6.2 DONNA’s speech is local Kokoro output

Even if an Anthropic-hosted voice feature eventually watermarks its audio, DONNA’s current TTS path is local Kokoro. Anthropic does not synthesize the waveform. Any watermark would have to be inserted locally after Kokoro produces audio and before playback.

## 7. Fingerprints, watermarks, correlations, and voiceprints

These terms are often mixed together but provide different guarantees.

| Technique | Adds data to audio? | Typical result | Can remove echo? | Useful to DONNA? |
|---|---:|---|---:|---|
| Direct render reference | No | Exact known playback PCM | **Yes, with AEC** | Primary solution |
| Cross-correlation/GCC-PHAT | No | Similarity and likely delay | Helps align AEC; not cancellation alone | Strong diagnostic/helper |
| Acoustic fingerprint | No | Identity of a known recording | No | Slow fallback/self-audio evidence |
| Neural audio watermark | Yes | Probability/payload/localization | No | Provenance or secondary detector |
| Speaker/voice embedding | No | Likely voice identity | No | Weak policy signal |
| Transcript matching | No | Text similarity after STT | No | Late defense against exact replays |
| Ultrasonic beacon | Yes | Marker presence | No | Fragile detector; generally avoid |

### 7.1 Acoustic fingerprinting

Fingerprinting derives compact features from an existing recording. It does not alter the recording. Shazam-style systems hash stable time-frequency landmarks and can recognize a known item through noise, reverberation, compression, and foreground speech. [Avery Wang, “An Industrial-Strength Audio Search Algorithm”](https://www.ee.columbia.edu/~dpwe/papers/Wang03-shazam.pdf)

Chromaprint is the main open local tool, but its own README says it targets near-identical full-file identification, duplicate detection, and long-stream monitoring—not general-purpose fingerprinting. [Chromaprint](https://github.com/acoustid/chromaprint) Essentia’s wrapper defaults to long analysis windows and warns against analysis windows below 30 seconds, which is a poor fit for sub-second barge-in. [Essentia Chromaprinter](https://essentia.upf.edu/reference/streaming_Chromaprinter.html)

A specialized short-window landmark matcher could recognize chunks of just-played TTS, but its output is still “this mixture contains DONNA.” If the user is also present, the mixture cannot be discarded.

### 7.2 Direct correlation—the useful fingerprint-like mechanism

Because DONNA owns the exact render PCM, normalized cross-correlation or GCC-PHAT between recent playback and microphone buffers can:

- confirm acoustic leakage;
- estimate bulk delay;
- track delay changes;
- measure echo-path strength;
- reject an obviously pure self-echo command as a policy fallback;
- seed or validate WebRTC’s delay estimate.

This should operate on short rolling windows and timestamps, not a database of fingerprints. It is simpler, lower latency, and more specific than general audio identification. It still does not replace the adaptive echo-path filter because correlation alone does not model the room response or preserve double-talk.

### 7.3 Audio watermarks

Watermarking modifies audio to carry a detectable signal or payload.

#### AudioSeal

Meta’s AudioSeal is the most relevant openly available local option found. Its official implementation provides:

- a generator that produces an additive watermark waveform;
- a detector that returns sample-level watermark probabilities;
- an optional 16-bit message;
- streaming support;
- Python/PyTorch implementation and MIT licensing;
- claimed robustness to compression, resampling, and added noise. [AudioSeal repository](https://github.com/facebookresearch/audioseal)

Possible DONNA use:

```text
Kokoro PCM -> AudioSeal generator -> watermarked PCM -> playback
mic -> AudioSeal detector -> "DONNA audio likely present"
```

Why it is not AEC:

- detection says where a mark is present but does not estimate and subtract the audible Kokoro speech;
- over-the-air speaker-to-microphone detection is a harder channel than many digital-edit tests and must be measured on every route;
- low playback volume, laptop EQ, Bluetooth codecs, resampling, reverberation, and near-end speech may change error rates;
- muting watermark-positive frames mutes the user during barge-in;
- it adds a neural generator and detector to every TTS path despite already knowing the source PCM.

AudioSeal could make sense for **provenance**—marking exported DONNA speech so a later file can be identified—or as a secondary self-audio signal. It should not gate raw microphone audio by itself.

#### Google SynthID Audio

Google DeepMind says SynthID embeds imperceptible watermarks in audio from Lyria and NotebookLM and can detect Google AI watermarks in uploaded content. It is designed for provenance/identification and robustness to common media modifications. [Google DeepMind SynthID](https://deepmind.google/models/synthid/)

It demonstrates that robust AI-audio watermarking is feasible, but no general public local audio embedding SDK was found for arbitrary Kokoro output. More importantly, Google presents it as identifying AI-generated content, not separating echo from live double-talk.

#### C2PA Content Credentials

C2PA attaches signed provenance assertions and can use fingerprints or watermarks as “soft bindings” to recover a remote manifest if embedded metadata is lost. [C2PA specification](https://spec.c2pa.org/specifications/specifications/2.4/specs/C2PA_Specification.html), [C2PA UX guidance](https://spec.c2pa.org/specifications/specifications/2.2/ux/UX_Recommendations.html)

File metadata is useful when distributing an audio asset. It does not travel acoustically through a speaker into a microphone, so it cannot solve live echo.

### 7.4 Voiceprints and speaker embeddings

A speaker embedding could learn Kokoro’s voice and score captured speech as “DONNA-like.” This is weaker than an exact render reference:

- short commands provide little speaker evidence;
- room coloration and Bluetooth processing shift embeddings;
- a human may sound similar;
- an overlapping user and TTS produce a mixed embedding;
- successful classification still provides no cleaned waveform.

Use it only as a command-policy feature, never as the audio separator.

### 7.5 Transcript similarity

After Whisper produces text, compare it with the TTS text spoken in the recent playback window. High similarity plus temporal overlap is good evidence of self-transcription.

This is worth adding as defense in depth because it is cheap and can stop dangerous self-issued control commands. It is not a barge-in mechanism:

- it acts only after VAD and STT latency;
- acoustic echo may transcribe differently;
- the user may intentionally repeat or quote DONNA;
- it cannot recover user speech hidden by echo.

### 7.6 Ultrasonic or near-ultrasonic tags

An inaudible pilot tone or spread-spectrum tag can detect that playback is active and estimate delay. It is inferior to direct PCM correlation here. Laptop speakers/microphones, Bluetooth codecs, automatic filtering, and sample-rate conversion may remove it; high-frequency energy may be audible to some users or animals; and it still cannot cancel audible speech.

## 8. Recommended architectures for DONNA

### 8.1 Mode A: half-duplex now

Use this if reliable command capture matters more than interruption:

1. Signal `playback_start` before playing Tink or Kokoro audio.
2. Stop feeding capture frames into VAD, or discard them with timestamps.
3. Play the complete response.
4. Wait until the output device has drained, then add a configurable 150–300 ms acoustic hangover.
5. Reset VAD state and clear preroll containing playback.
6. Resume capture processing.

Do not merely ignore transcripts after the fact; that still wastes VAD/STT compute and may pollute state. Keep microphone hardware open if closing/reopening causes route latency, but gate frames before VAD.

This matches the intended strict turn-taking described in [`README.md`](../../../README.md) and should be implemented explicitly across the actual Kokoro playback boundary.

### 8.2 Mode B: Apple-native full duplex — recommended

```text
                 one AVAudioEngine / one device clock

Kokoro PCM -> player/source node -> mixer -> voice-processing output -> speaker
                                          │
mic -> voice-processing input -> sink/tap ┴-> cleaned PCM -> VAD -> Whisper
                                                        └-> barge-in controller
```

Implementation outline:

1. Add a small Swift executable that owns `AVAudioEngine`.
2. Stop the engine and enable voice processing on an I/O node; verify both I/O nodes report it enabled.
3. Route decoded Kokoro PCM and the Tink cue through an engine player/source node, replacing `afplay`.
4. Read voice-processed microphone PCM from a sink node or suitable input-chain tap.
5. Send timestamped cleaned mono frames to the existing Python listener over a bounded local IPC channel.
6. Run VAD on the cleaned stream. During TTS, a near-end VAD onset becomes the barge-in signal.
7. On barge-in, stop/fade the player, retain near-end preroll, and continue recording the user.
8. Rebuild the engine and clear DSP/VAD state on device-route changes.

Keep heavy work out of real-time callbacks. Apple and `sounddevice` both warn against allocations, file I/O, locks, or blocking calls in audio render callbacks. Queue fixed-size buffers into worker threads.

### 8.3 Mode C: Python/C++ full duplex with WebRTC AEC3

```text
Kokoro worker -> bounded PCM queue -> sounddevice.Stream output buffer
                                      │ exact rendered frame
                                      ▼
                                  AEC3 render input

sounddevice.Stream mic input -> AEC3 capture input -> cleaned queue -> VAD/STT
```

Requirements:

- use one `sounddevice.Stream`, not separate unrelated input and `afplay` processes;
- prefer a single paired input/output device and native 48 kHz clock;
- maintain monotonic timestamps/sample counters for render and capture;
- process 10 ms frames and report buffer delay;
- handle output underruns by submitting the actual silence/output, not intended-but-unplayed audio;
- avoid Python allocations in the callback; use preallocated ring buffers and native DSP;
- resample cleaned capture after AEC;
- expose AEC metrics and raw synchronized diagnostic dumps.

This gives more control and portability than Apple voice processing, at the cost of a larger native dependency and more synchronization responsibility.

### 8.4 Hybrid safeguards

Regardless of the main mode:

- track `playback_start`, output sample position, `playback_stop`, and device route;
- compare microphone/render energy and rolling correlation for diagnostics;
- compare final transcripts with recent TTS text before executing high-impact voice commands;
- require the established “OMG” control prefix and wake policy;
- rate-limit repeated identical self-like commands;
- never suppress a whole double-talk frame solely because a fingerprint/watermark is present.

## 9. Evaluation plan

### 9.1 Record the right signals

Every test should capture synchronized:

1. `render.wav` — exact samples supplied to the output stream;
2. `mic_raw.wav` — microphone before AEC;
3. `mic_clean.wav` — AEC/residual-suppression output;
4. timestamps for VAD onset, playback start/stop, STT completion, and interruption;
5. device IDs, sample rates, channel counts, volume, callback sizes, and under/overrun flags.

Without those three waveforms, most AEC failures are guesswork.

### 9.2 Required scenarios

| Scenario | Expected behavior |
|---|---|
| Silence, no playback | No false VAD; noise floor not pumped |
| DONNA-only playback | Strong echo removal; no wake/control transcript |
| User-only speech | Minimal speech distortion and baseline WER |
| Double-talk | DONNA suppressed, user preserved, barge-in detected |
| User repeats DONNA’s words | User retained; transcript-similarity safeguard must not blindly discard |
| Tink cue while listening | Cue must not trigger VAD/wake |
| Sudden volume change | Fast reconvergence without long echo burst |
| Move/rotate laptop | Echo path reconverges |
| Speaker clipping/high volume | Failure is visible; volume policy avoids saturation |
| Device route switch | Engine restarts cleanly; no stale reference |
| AirPods paired I/O | Stable route and low false trigger rate |
| Mismatched external mic/speaker | Either supported with drift handling or rejected/fallback mode |

### 9.3 Metrics

- **ERLE:** `10 log10(E[mic_echo²] / E[clean_echo²])` during far-end single-talk. Useful, but it can reward deleting everything.
- **Near-end WER:** transcribe clean user reference and AEC output; measure damage to user speech.
- **Double-talk VAD recall/precision:** the metric most directly tied to barge-in.
- **Residual self-transcription rate:** how often DONNA-only playback produces a transcript or recognized control phrase.
- **Interruption latency:** user acoustic onset to playback fade/stop.
- **Convergence/reconvergence time:** after playback starts or the path changes.
- **AECMOS:** perceptual scores for echo and other degradation. Microsoft created AECMOS because ERLE and PESQ correlate imperfectly with human judgments. [AECMOS paper/tool](https://www.microsoft.com/en-us/research/publication/aecmos-a-speech-quality-assessment-metric-for-echo-impairment/)
- **CPU, p50/p99 callback time, underflow/overflow count, and added buffering latency.**

The Microsoft ICASSP 2023 challenge constrained algorithmic plus buffering latency to 20 ms and evaluates far-end single-talk, near-end single-talk, and double-talk using real and synthetic data. It is a useful benchmark source, though DONNA’s end-to-end threshold should be selected from local user-experience tests. [ICASSP 2023 AEC Challenge paper](https://arxiv.org/abs/2309.12553)

Provisional product targets for the first full-duplex prototype:

- zero executed wake/control commands in a one-hour DONNA-only playback stress test;
- at least 20 dB median ERLE in far-end-only local tests, without using hard muting to inflate the score;
- no more than 2 absolute WER points worse than the no-playback user baseline;
- at least 95% double-talk onset recall across normal speaking levels;
- p95 acoustic-onset-to-playback-stop below 200 ms;
- no audio callback underruns/overruns in a 30-minute stress run.

These are engineering targets, not guarantees from any library.

## 10. Decision matrix

| Option | Barge-in | User preserved in double-talk | Integration effort | macOS fit | Recommendation |
|---|---:|---:|---:|---:|---|
| Pause/discard mic during TTS | No | No capture | Low | Excellent | Implement immediately |
| Apple AVAudioEngine voice processing | Yes | Designed for it | Medium | **Best** | First full-duplex prototype |
| WebRTC AEC3 + duplex stream | Yes | Designed for it | Medium-high | Good | Best controlled/portable fallback |
| SpeexDSP | Yes | Basic support | Medium | Good | Lightweight baseline/fallback |
| GStreamer WebRTC DSP | Yes | Designed for it | Medium-high | Fair | Viable, not first choice |
| PipeWire echo-cancel | Yes | Designed for it | Low on Linux | Poor | Linux-only future option |
| Neural AEC alone | Yes | Model-dependent | High | Variable | Evaluate after I/O/reference is correct |
| Fingerprint and mute | No | **No** | Medium | Good | Do not use as AEC |
| AudioSeal and mute | No | **No** | Medium-high | Good | Provenance/telemetry only |
| Transcript rejection | No | Audio already lost | Low | Excellent | Defense in depth only |

## 11. Concrete recommendation

1. Fix the current strict-turn-taking path first: add an explicit playback-state channel shared by Kokoro playback and the listener, gate before VAD, include Tink, and use a measured post-playback hangover.
2. Build a minimal Swift `AVAudioEngine` proof of concept with one built-in mic/speaker route. Feed a known Kokoro WAV through the engine and save raw/render/processed diagnostics.
3. Test DONNA-only, user-only, and double-talk before connecting commands. Confirm that processed input—not raw input—feeds Silero.
4. If Apple voice processing meets quality and routing needs, make the helper the sole owner of assistant playback and microphone capture. Remove `afplay` from the voice path.
5. If Apple routing or quality is insufficient, prototype WebRTC AEC3 using the same unified-duplex architecture. Evaluate `pywebrtc-audio` for speed of validation, but use a pinned/audited native dependency for production.
6. Add rolling render/mic correlation and recent-TTS transcript similarity for observability and command safety.
7. Add AudioSeal only if DONNA also needs provenance for exported audio. Keep its detector outside the AEC control loop.

The key architectural rule is simple: **the component that writes samples to the speaker must also expose those exact samples and their timing to the echo canceller.** A hidden marker is weaker information than DONNA already possesses.

## Sources

### Platform and conventional AEC

- [Apple: What’s New in AVAudioEngine, WWDC19](https://developer.apple.com/videos/play/wwdc2019/510/)
- [Apple: AVAudioIONode](https://developer.apple.com/documentation/avfaudio/avaudioionode)
- [Apple: setVoiceProcessingEnabled](https://developer.apple.com/documentation/avfaudio/avaudioionode/setvoiceprocessingenabled%28_%3A%29)
- [Apple: VoiceProcessingIO audio unit](https://developer.apple.com/documentation/audiotoolbox/kaudiounitsubtype_voiceprocessingio)
- [WebRTC: EchoCanceller3 interface](https://webrtc.googlesource.com/src/+/master/modules/audio_processing/aec3/echo_canceller3.h)
- [WebRTC: Audio Processing implementation](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/audio_processing_impl.cc)
- [WebRTC: license](https://webrtc.googlesource.com/src/+/refs/heads/main/LICENSE)
- [Speex: AEC manual and troubleshooting](https://www.speex.org/docs/manual/speex-manual/node7.html)
- [SpeexDSP: echo API](https://github.com/xiph/speexdsp/blob/master/include/speex/speex_echo.h)
- [GStreamer: webrtcdsp](https://gstreamer.freedesktop.org/documentation/webrtcdsp/webrtcdsp.html)
- [GStreamer: webrtcechoprobe](https://gstreamer.freedesktop.org/documentation/webrtcdsp/webrtcechoprobe.html)
- [PipeWire: echo-cancel module](https://pipewire.pages.freedesktop.org/pipewire/page_module_echo_cancel.html)
- [W3C: Media Capture and Streams](https://www.w3.org/TR/mediacapture-streams/)
- [python-sounddevice](https://python-sounddevice.readthedocs.io/en/latest/)
- [PortAudio](https://portaudio.com/docs/v19-doxydocs/)

### Evaluation and neural AEC

- [Microsoft AEC Challenge repository](https://github.com/microsoft/AEC-Challenge)
- [ICASSP 2023 AEC Challenge paper](https://arxiv.org/abs/2309.12553)
- [Microsoft AECMOS](https://www.microsoft.com/en-us/research/publication/aecmos-a-speech-quality-assessment-metric-for-echo-impairment/)
- [ITU-T G.168](https://www.itu.int/epublications/es/publication/itu-t-g-168-2015-cor-1-2022-12-reference-error-corrections)
- [ITU-T P.831](https://www.itu.int/rec/T-REC-P.831-199812-I/en)
- [DTLN-AEC](https://github.com/breizhn/DTLN-aec)
- [LocalVQE](https://github.com/localai-org/LocalVQE)
- [NVIDIA Maxine AFX](https://github.com/NVIDIA-Maxine/Maxine-AFX-SDK)
- [pywebrtc-audio](https://github.com/strands-labs/pywebrtc-audio)

### Fingerprinting, watermarking, and provenance

- [Anthropic Transparency Hub: voluntary commitments](https://www.anthropic.com/transparency/voluntary-commitments/security%26privacy)
- [Anthropic: Building safeguards for Claude](https://www.anthropic.com/news/building-safeguards-for-claude)
- [Google DeepMind: SynthID](https://deepmind.google/models/synthid/)
- [Meta: AudioSeal](https://github.com/facebookresearch/audioseal)
- [Chromaprint](https://github.com/acoustid/chromaprint)
- [Avery Wang: An Industrial-Strength Audio Search Algorithm](https://www.ee.columbia.edu/~dpwe/papers/Wang03-shazam.pdf)
- [C2PA technical specification](https://spec.c2pa.org/specifications/specifications/2.4/specs/C2PA_Specification.html)
- [C2PA UX guidance](https://spec.c2pa.org/specifications/specifications/2.2/ux/UX_Recommendations.html)
