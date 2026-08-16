# Deep Research: `afplay` alternatives for DONNA

**Research date:** 2026-08-16  
**Target:** Apple Silicon macOS today, with a plausible path to Linux/Windows  
**Current stack:** Python 3.12, Kokoro TTS, NumPy/SoundFile, `sounddevice`/PortAudio capture, Silero VAD, Whisper, and `/usr/bin/afplay` playback

## Executive decision

DONNA can do substantially better than `afplay`, but replacing it with a Rust or C++ executable is not the first move.

The best immediate replacement is the PortAudio stack already present through `python-sounddevice`. Change the listener from an input-only `sounddevice.InputStream` to one persistent full-duplex `sounddevice.Stream`, and make that process the sole owner of microphone capture and assistant playback. Send Kokoro's in-memory PCM to it over a local socket or pipe. This removes the per-file subprocess, supports continuous chunk queues and immediate interruption, exposes render samples and ADC/DAC timestamps, and creates the playback reference needed by echo cancellation.

Use native code only after measuring that the Python callback, queue, or resampler misses deadlines, or when integrating a native echo canceller:

1. **macOS-only, best echo cancellation:** a small Swift/Objective-C++ `AVAudioEngine` helper with Apple voice processing. This is better aligned with the actual product problem than Rust or portable C++.
2. **Cross-platform playback plus future WebRTC AEC:** C++ with PortAudio or miniaudio, WebRTC Audio Processing Module, and nanobind. It provides the shortest integration path to AEC3.
3. **Cross-platform safe playback engine without AEC:** Rust with CPAL, Rubato, PyO3, and maturin. This is a good engineering choice for a player, but CPAL exposes separate input and output streams and does not expose Apple's VoiceProcessingIO. Rust is not automatically the better AEC choice.

Do not use Rodio, mpv, `ffplay`, or a direct `afplay` replacement as the long-term audio owner. They can improve file playback, but they do not solve synchronized duplex I/O or provide the render reference required for robust AEC.

## What was verified locally

### Current runtime path

The repository and the local Kokoro MCP server currently form this path:

```text
Kokoro generator
  -> NumPy audio array
  -> SoundFile writes 24 kHz mono WAV
  -> blocking afplay subprocess
  -> selected macOS output route

sounddevice.InputStream
  -> 16 kHz mono float32 microphone frames
  -> Python queue
  -> Silero VAD
  -> Whisper
```

Concrete findings:

- `/Users/pedro/.mcp/kokoro-local/mcp_server.py` writes every generated Kokoro chunk to a WAV, then invokes `run(["afplay", path])` and waits for it.
- The server creates `~/.claude/scripts/.voice-playback` around the whole playback sequence. The listener discards microphone frames while that flag exists and for a 250 ms hangover.
- `stt-local/listen_daemon.py` already depends on `sounddevice` 0.5.5 and opens a persistent mono `InputStream`.
- A sampled generated file is PCM signed 16-bit, mono, 24,000 Hz. Kokoro's value before writing is already an in-memory tensor/NumPy-compatible audio array.
- The Tink cue also starts a separate asynchronous `afplay` process.
- Installed local player/toolchain: `/usr/bin/afplay`, Homebrew `ffplay`/FFmpeg 8.1.1, Swift, Rust/Cargo, Clang, CMake, Ninja, and pkg-config. mpv, SoX, GStreamer, PyAudio, Python miniaudio, and Python SoXR were not installed in the checked environments.

The playback flag is a sensible temporary feedback gate, but it prevents barge-in by definition. A better audio owner should eventually replace the flag with actual render state plus AEC-cleaned microphone audio.

### What `afplay` actually offers

The locally installed help reports a single required `audio_file` and only basic volume, time, rate, and rate-conversion-quality options. It has no raw-PCM stdin contract, persistent queue, programmatic buffer completion, sample counter, output callback, device-clock timestamps, capture stream, or AEC reference interface.

This creates six practical costs:

1. **Disk and format round-trip.** Playback cannot start until each chunk is encoded as a WAV and opened again.
2. **Process boundary.** Each chunk launches a new program and creates a new player lifecycle.
3. **Chunk gaps.** Consecutive Kokoro chunks are separate files and processes, so gaplessness is accidental rather than scheduled.
4. **Coarse cancellation.** Python can terminate a process, but cannot clear a native queue at an exact sample boundary or apply a short click-free fade.
5. **No render truth.** The app knows the source WAV, not which frames the device has accepted or when their first sample reaches the DAC.
6. **No duplex ownership.** Capture lives in PortAudio while playback lives in an unrelated Core Audio process. This is the wrong boundary for AEC and barge-in.

The critical distinction is not “CLI versus library.” It is whether DONNA owns the render callback and timing.

## Required behavior for a voice-assistant player

An `afplay` replacement should be evaluated against voice interaction, not music playback.

### Functional requirements

- Accept mono `float32` PCM directly from Kokoro without requiring a temporary file.
- Keep the output device open across utterances.
- Queue multiple chunks gaplessly.
- Start as soon as a useful PCM prefix is available.
- Expose `enqueue`, `pause`, `drain`, `clear`, `stop`, `abort`, and `close` semantics.
- Make stop audible quickly and avoid clicks with a short ramp.
- Report queue depth, rendered frames, underruns, device, sample rate, and estimated latency.
- Survive default-device changes and surface route failures.
- Allow Tink and TTS to share the same mixer/queue.

### AEC and barge-in requirements

- Capture and playback should share one stream or at least timestamped clocks.
- The exact PCM placed in the output buffer must be copied to the AEC render path.
- Input and output latency estimates must be observable.
- The pipeline must define whether “played” means consumed by the app, rendered into the host buffer, or estimated at the DAC.
- The microphone path must continue while TTS plays so the user can interrupt.
- AEC, VAD, and playback must agree on sample rate, channel layout, frame size, and discontinuities.

### Real-time requirements

- Audio callbacks cannot perform file I/O, logging, model inference, heap allocation, blocking locks, or Python callbacks with unpredictable work.
- Python/native crossings happen on control or worker threads, not once per device sample or DSP frame.
- A bounded single-producer/single-consumer PCM ring owns its memory.
- Underrun produces zeroes and a counter; it does not block the callback.
- Queue clearing is an atomic state transition. Destruction and device stop happen outside the callback.

`python-sounddevice` documents that its stream callback runs at very high or real-time priority and must avoid allocation, file access, blocking calls, and unpredictable library work. It also exposes synchronized ADC, callback, and DAC timestamps. [sounddevice stream API](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html)

## Option map

| Option | Direct PCM | Persistent queue | Fine stop | Duplex/timing | Built-in AEC | Python packaging | Fit for DONNA |
|---|---:|---:|---:|---:|---:|---:|---|
| `afplay` | No practical stdin API | No | Process kill | No | No | System tool | Current baseline only |
| `ffplay` subprocess | Yes, via pipe | One continuous pipe | Process/pipe | No shared capture | No | External binary | Useful diagnostic fallback |
| mpv subprocess/IPC | Yes with setup | Yes | Good commands | Playback-only clock | No | External runtime | Better player, wrong AEC boundary |
| GStreamer | Yes | Yes | Excellent | Strong clocks/pipelines | WebRTC DSP elements | Large runtime | Capable but heavy |
| `sounddevice.Stream` | Yes, NumPy | App-owned | Excellent | One PortAudio duplex stream | No | Already installed | Best first implementation |
| Python miniaudio | Yes | Generator/app-owned | Good | Duplex class | No | Wheels available | Good experiment, unnecessary now |
| Apple `AVAudioEngine` | Yes | Scheduled buffers | Excellent | Native full duplex | Yes, voice processing | Helper/bridge needed | Best macOS production path |
| PortAudio C/C++ | Yes | App-owned | Excellent | Native duplex + timestamps | No | Native extension | Strong portable base |
| miniaudio C/C++ | Yes | App-owned | Excellent | One duplex callback | No | Vendor one C source | Strong compact portable base |
| CPAL Rust | Yes | App-owned | Excellent | Separate input/output streams | No | PyO3/maturin | Strong player; more AEC work |
| Rodio Rust | Yes | Built-in source queue | Good | Output-oriented | No | Custom PyO3 wrapper | Too high-level for audio owner |

Ratings in the table describe exposed architecture, not measured performance on this Mac. Latency must be measured on each output route.

## Python-first options

### 1. `sounddevice`: recommended first move

`sounddevice.Stream` is a PortAudio stream for simultaneous input and output. Its callback receives `indata`, `outdata`, frame count, status flags, and one time structure containing:

- ADC capture time for the first input sample;
- current callback time;
- DAC output time for the first output sample.

The stream supports device selection, low/high or explicit requested latency, actual latency reporting, immediate `abort()`, draining `stop()`, variable optimal block sizes, and blocking or callback modes. The documentation explicitly presents a simultaneous input-to-output example. [sounddevice stream API](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html), [sounddevice usage](https://python-sounddevice.readthedocs.io/en/latest/usage.html)

This is already a compiled C audio backend. Replacing it with Rust does not remove Python from the current microphone callback unless the entire callback/queue path moves native.

Recommended structure:

```text
Kokoro MCP process
  -> local Unix socket: enqueue PCM + sample rate + utterance ID

audio owner process
  -> worker: convert/copy/resample into bounded playback ring
  -> one sounddevice.Stream callback:
       outdata <- playback ring or zeroes
       AEC render ring <- exact outdata
       capture ring <- indata + ADC/DAC timestamps
  -> DSP worker: AEC -> VAD -> clip queue
```

Why not just call `sounddevice.play(audio, 24000)`? The convenience functions are intended for small scripts and interactive use. A persistent lower-level stream is required for continuous recording, timing, explicit queue ownership, and exact render-reference capture. [sounddevice usage](https://python-sounddevice.readthedocs.io/en/latest/usage.html#callback-streams)

Risks:

- The callback is still invoked through CFFI into Python.
- NumPy slicing/copying and `queue.Queue` locks can jitter under CPU or memory pressure.
- The current listener uses `indata[:, 0].copy()` and a Python queue; adding output work increases callback load.
- A Python callback is adequate for a measured prototype, not an assumption of hard real-time safety.

Mitigation:

- Use preallocated buffers and a purpose-built ring rather than `queue.Queue` for output.
- Use `blocksize=0` unless a DSP stage strictly requires fixed blocks; sounddevice warns that a nonzero callback block size may add buffering.
- Request `latency="low"`, record the actual `stream.latency`, and test underruns.
- Keep resampling and all AEC/VAD work outside the callback.
- Treat the first Python version as an instrumented reference implementation. Promote only the proven hot path to native code.

### 2. Python miniaudio

The maintained `miniaudio` package is a CFFI wrapper around the single-file C library. PyPI showed version 1.71, released 2026-04-29, with CPython macOS ARM64 wheels, Python 3.8+, playback, capture, decoding, conversion, device enumeration, and a duplex stream. [Python miniaudio on PyPI](https://pypi.org/project/miniaudio/)

Advantages:

- Direct PCM and generator-based streaming.
- A duplex abstraction that sends captured audio into the playback generator.
- Built-in conversion and broader file decoding than DONNA needs.
- Easy isolated prototype.

Disadvantages:

- DONNA already carries PortAudio through `sounddevice`.
- The Python wrapper's documented default buffer is 200 ms, much too large for responsive interruption unless tuned.
- Generator callbacks still cross into Python on the audio path.
- Replacing one mature C backend with another does not itself add AEC.

Verdict: useful as a comparison benchmark, not the default migration.

### 3. PyAudio

PyAudio is another PortAudio binding. It would reproduce capabilities already available through `sounddevice` while giving up the existing NumPy-oriented API. There is no architectural benefit for DONNA.

### 4. PyObjC and AVFoundation

PyObjC can access AVFoundation, but it is a poor hot-path bridge here. Its own API notes state that `AVAudioBuffer.floatChannelData`, `int16ChannelData`, and `int32ChannelData` require manual wrappers and are not available from Python. [PyObjC AVFoundation API notes](https://pyobjc.readthedocs.io/en/latest/apinotes/AVFoundation.html)

A direct PyObjC prototype may schedule files, but a production PCM/AEC path would require manual native wrapping anyway. A small Swift or Objective-C++ helper is clearer and safer.

## Native macOS options

### `AVAudioEngine` plus `AVAudioPlayerNode`: best Apple-native player

`AVAudioPlayerNode` schedules `AVAudioPCMBuffer` objects or audio-file segments. Buffers scheduled with no explicit time follow the preceding command, which provides deliberate gapless chunk playback. It exposes player/node time conversion, last render time, latency, presentation latency, completion stages, pause, and a `stop()` that clears scheduled events and resets the player timeline. [Apple `AVAudioPlayerNode`](https://developer.apple.com/documentation/avfaudio/avaudioplayernode)

An engine graph can own all of these in one process:

```text
PCM source/player -> mixer -> output node
                         \-> render-reference tap
input node -> Apple voice processing -> sink/tap -> Python IPC
```

Apple introduced AVAudioEngine voice processing primarily for echo cancellation and VoIP. Enabling it makes both input and output nodes participate, must happen while the engine is stopped, and is available only while rendering to an audio device. [Apple WWDC19: What's New in AVAudioEngine](https://developer.apple.com/videos/play/wwdc2019/510/), [Apple voice-processing sample](https://developer.apple.com/documentation/avfaudio/using-voice-processing)

Implementation choices:

- `AVAudioPlayerNode` for discrete Kokoro buffers and Tink.
- `AVAudioSourceNode` for a pull-based ring buffer when utterance chunks arrive continuously.
- `AVAudioSinkNode` or an input-node tap to export cleaned microphone PCM.
- A local Unix-domain socket or framed stdin/stdout protocol between Python and the helper.

Advantages:

- Native Core Audio clock and route handling.
- Apple-supported voice processing instead of independently tuning AEC3.
- Buffer scheduling, completion semantics, and output timing.
- No Python code on the render thread.

Costs:

- macOS-only helper and lifecycle management.
- IPC copies and protocol design.
- Apple voice processing can color audio; it must be tested against Whisper/VAD and different routes.
- AirPods and other Bluetooth routes can impose latency outside the app's chosen buffer.

Verdict: preferred production architecture if DONNA remains macOS-first and true barge-in/AEC is a core requirement.

### Audio Queue Services

Audio Queue Services is a C API for playback and recording queues. It supports allocating/enqueuing buffers, start, prime, pause, flush, reset, stop, timestamps, and processing taps. [Apple Audio Queue Services](https://developer.apple.com/documentation/audiotoolbox/audio-queue-services)

It is a competent lightweight C `afplay` replacement, but separate queue objects do not automatically provide the integrated voice-processing graph. For this project AVAudioEngine is the higher-value Apple abstraction.

### AUHAL / Audio Units

The HAL Output Audio Unit gives direct Core Audio device input/output callbacks and maximum control. It is appropriate when AVAudioEngine cannot meet routing, format, or latency requirements. It also requires substantially more device, property, format, and lifecycle code. [Apple TN2091: device input using the HAL Output Audio Unit](https://developer.apple.com/library/archive/technotes/tn2091/_index.html)

Use AUHAL only after an AVAudioEngine prototype demonstrates a concrete limitation.

## Rust options

### CPAL: the correct Rust layer

CPAL 0.18.1 is a low-level cross-platform input/output library. Current documentation lists Core Audio on macOS, ALSA with optional JACK/PipeWire/PulseAudio on Linux, and WASAPI with optional ASIO/JACK on Windows. It supports device enumeration and stable IDs, formats, callbacks, play/pause, negotiated buffer-size queries, stream clocks, and callback timing. [CPAL documentation](https://docs.rs/cpal/latest/cpal/), [CPAL repository](https://github.com/RustAudio/cpal)

Its callback runs on a dedicated high-priority thread on modern platforms. Version 0.18 added a more explicit clock/buffer API and consistent paused-on-creation behavior. [CPAL 0.18 upgrade guide](https://github.com/RustAudio/cpal/blob/master/UPGRADING.md)

The important limitation: the public `DeviceTrait` exposes `build_input_stream` and `build_output_stream`, not a single portable full-duplex callback. [CPAL `DeviceTrait`](https://docs.rs/cpal/latest/cpal/traits/trait.DeviceTrait.html)

That does not prevent capture plus playback, but it means:

- two callbacks and potentially two stream clocks must be related;
- capture/render buffering and drift need explicit handling;
- an accurate AEC delay estimator becomes part of the application;
- Apple VoiceProcessingIO is not available through CPAL's portable interface.

CPAL is a strong foundation when the goal is a safe cross-platform player. It is less direct than PortAudio/miniaudio or AVAudioEngine for DONNA's eventual full-duplex AEC.

### Rodio: convenient, but too high-level

Rodio 0.22.2 is a Rust playback library built on CPAL. It offers a queue, mixer, sources, pause/play, clear/stop, position, volume, speed, seeking, and decoding through Symphonia or optional decoders. Its `Player.stop()` empties the queue. [Rodio repository](https://github.com/RustAudio/rodio), [Rodio `Player`](https://docs.rs/rodio/latest/rodio/struct.Player.html)

Rodio deliberately configures a default 100 ms output buffer for robust playback, although `DeviceSinkBuilder` permits a smaller size. That default is a poor match for fast barge-in until tuned and measured. [Rodio stream buffer documentation](https://docs.rs/rodio/latest/rodio/stream/index.html), [Rodio `DeviceSinkBuilder`](https://docs.rs/rodio/latest/rodio/stream/struct.DeviceSinkBuilder.html)

Rodio is output-oriented. DONNA does not need its file decoders because Kokoro already emits decoded float PCM, and Rodio does not solve capture synchronization or AEC. It is a good general `afplay` library replacement, not the right audio-engine boundary.

### Rubato: useful supporting component

Kokoro emits 24 kHz PCM, while interactive device/AEC pipelines commonly run at 48 kHz. Rubato 4.0 provides fixed-ratio and asynchronous resampling, including variable ratios for separate-clock drift. Its `process_into_buffer` path is designed to avoid allocation, but its documentation still recommends keeping heavy resampling out of the audio callback. [Rubato documentation](https://docs.rs/rubato/latest/rubato/)

Use it in a Rust worker before enqueueing playback. For a fixed 24-to-48 kHz conversion, the fixed-ratio path is sufficient. Use asynchronous ratio control only if measurements show input/output clock drift.

### Apple APIs from Rust

The generated `objc2-avf-audio` bindings expose `AVAudioEngine`, `AVAudioPlayerNode`, scheduling methods, and other AVFAudio types. The player node type is explicitly neither `Send` nor `Sync`, reflecting Objective-C framework threading constraints. [Rust `objc2-avf-audio` player node](https://docs.rs/objc2-avf-audio/latest/objc2_avf_audio/struct.AVAudioPlayerNode.html)

This route is possible, but it combines Rust FFI, Objective-C object ownership, blocks/callbacks, framework thread affinity, and PyO3. It loses the simplicity that motivates Rust. For Apple-only voice processing, Swift is the more direct implementation language.

### Rust/Python binding design

Use PyO3 and maturin if Rust is selected. PyO3 creates native Python extension modules; maturin builds platform wheels and supports stable-ABI configurations. [PyO3 build/distribution guide](https://pyo3.rs/main/building-and-distribution), [maturin guide](https://www.maturin.rs/)

Suggested Python surface:

```python
engine = AudioEngine(
    sample_rate=48_000,
    input_channels=1,
    output_channels=2,
    latency="low",
)
engine.enqueue(pcm_f32, source_rate=24_000, utterance_id="...")
engine.stop(fade_ms=8)
engine.drain(timeout=...)
stats = engine.stats()
engine.close()
```

Native ownership rules:

- Validate a contiguous `float32` NumPy view, then copy it once into native-owned memory.
- Never retain a borrowed NumPy pointer after returning to Python.
- Native workers and callbacks never hold or acquire the Python interpreter lock.
- `enqueue` may release/detach from Python during longer conversion; PyO3 documents `Python::detach` for native work. [PyO3 parallelism](https://pyo3.rs/main/parallelism)
- Preallocate the ring, resampler state, fade workspace, and statistics atomics before starting the stream.
- Send completion/events back through polling or a normal worker thread, not a Python call from the real-time thread.

“Zero copy” is the wrong goal at the Python boundary. A single bounded copy gives the native engine stable ownership and is tiny compared with TTS inference. The target is zero allocation and zero Python interaction in the real-time callback.

### Rust verdict

Rust is a good choice if the desired deliverable is a reusable, cross-platform, memory-safe audio player extension. It is not the shortest path from the current code to better playback, and it does not give DONNA an AEC implementation for free.

## C and C++ options

### PortAudio: already under DONNA

PortAudio is the C library beneath `sounddevice`. It supports callback and blocking streams, input, output, and full duplex. Its callback timing structure carries ADC capture time, callback time, and DAC output time. [PortAudio API overview](https://portaudio.com/docs/v19-doxydocs/api_overview.html), [PortAudio callback timing](https://portaudio.com/docs/v19-doxydocs/structPaStreamCallbackTimeInfo.html)

Advantages:

- Existing behavior and device enumeration can be retained.
- A single native full-duplex stream maps directly to AEC render/capture processing.
- Cross-platform and mature.
- Native promotion can happen after a Python `sounddevice.Stream` prototype, keeping the same conceptual API.

Disadvantages:

- PortAudio does not provide AEC, resampling policy, queueing, or mixing.
- Distribution must bundle or link a suitable PortAudio build.
- The C callback requires careful ownership and lifecycle code.

Verdict: lowest-risk portable native base because the project already uses and understands it.

### miniaudio: compact native alternative

miniaudio is a single-source C library with low-level device I/O and higher-level decoding, conversion, resampling, mixing, and node graphs. It supports Core Audio, WASAPI, ALSA, PulseAudio, JACK, and other backends. [miniaudio repository](https://github.com/mackron/miniaudio), [miniaudio manual](https://miniaud.io/docs/manual/index.html)

Its duplex device callback receives input and output buffers together. Capture and playback may use different formats and channel counts, although a full-duplex device uses one sample rate and the application must convert mismatched formats. The manual also prohibits starting, stopping, initializing, or uninitializing a device inside the callback because doing so can deadlock.

Advantages:

- One vendorable C source/header and no installed runtime dependency.
- A true duplex callback is a natural place to fork output into an AEC render path.
- Built-in format conversion and resampling reduce supporting dependencies.
- Straightforward C ABI or C++ wrapper.

Disadvantages:

- New backend behavior to validate even though PortAudio is already present.
- No built-in acoustic echo cancellation.
- The library does not promise a stable shared-library ABI across arbitrary upgrades; pin/vendor the source into the extension.

Verdict: strongest compact alternative to PortAudio if a clean native engine is being built from scratch.

### RtAudio

RtAudio provides a small C++ API for real-time input/output and duplex streams with Core Audio, JACK, ALSA, PulseAudio, WASAPI, and ASIO backends. [RtAudio project](https://github.com/thestk/rtaudio), [RtAudio documentation](https://caml.music.mcgill.ca/~gary/rtaudio/)

It is credible, but offers no material project-specific advantage over already-used PortAudio or the more easily vendored miniaudio. It also provides no AEC.

### SDL3

SDL3 routes playback and recording through `SDL_AudioStream`, supports queued data or callbacks, performs format conversion, and can follow changes to the default device. [SDL3 audio header](https://github.com/libsdl-org/SDL/blob/main/include/SDL3/SDL_audio.h), [SDL3 `SDL_OpenAudioDeviceStream`](https://wiki.libsdl.org/SDL3/SDL_OpenAudioDeviceStream)

SDL is a broad multimedia dependency, and its convenient device streams are opened for playback or recording rather than presenting DONNA's desired AEC-oriented duplex callback. It is a weak fit.

### C++ plus WebRTC Audio Processing Module

If portable echo cancellation is committed, this is the strongest reason to choose C++.

WebRTC's Audio Processing Module applies microphone speech enhancements including AEC, noise suppression, and automatic gain control, and can be used outside the complete WebRTC pipeline. [WebRTC APM overview](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/g3doc/audio_processing_module.md), [current APM source tree](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/)

The native engine can feed:

```text
exact output PCM -> APM reverse/render stream
microphone PCM   -> APM capture stream -> VAD/STT
```

This pairing eliminates the most important flaw in `afplay`: APM receives the precise ordered render samples from the same code that fills the audio device, plus device delay/timing estimates.

Costs are significant: WebRTC's source/build system, update cadence, compile time, binary size, and API surface are much larger than the player itself. Vendor or pin a known revision and wrap only the small stable interface DONNA needs.

### C++/Python binding choice

Recommended: nanobind plus scikit-build-core and CMake.

Nanobind exposes C++ types and NumPy-compatible n-dimensional arrays, documents GIL release guards, and supports CPython stable-ABI builds from Python 3.12. Its packaging guide uses scikit-build-core. [nanobind documentation](https://nanobind.readthedocs.io/en/latest/), [nanobind NumPy/array API](https://nanobind.readthedocs.io/en/latest/ndarray.html), [nanobind packaging](https://nanobind.readthedocs.io/en/latest/packaging.html), [scikit-build-core guide](https://scikit-build-core.readthedocs.io/en/latest/guide/getting_started.html)

pybind11 remains a mature alternative and explicitly documents `gil_scoped_release`/`gil_scoped_acquire`. [pybind11 GIL documentation](https://pybind11.readthedocs.io/en/stable/advanced/misc.html#global-interpreter-lock-gil)

The binding should expose a coarse object API, not the audio callback. Both nanobind and pybind11 make it possible to call Python from C++; doing so from the real-time thread would still be the wrong design.

### C/C++ verdict

C or C++ is favored over Rust when the near-term roadmap includes WebRTC AEC3, because the DSP implementation and integration surface are native C++. PortAudio or miniaudio can own duplex I/O, while a thin C++ binding keeps Python responsible for TTS, orchestration, configuration, and consuming cleaned PCM.

## External process alternatives

### `ffplay`: best zero-install experiment on this Mac

`ffplay` is installed and can accept raw PCM from stdin when given format, channel, and sample-rate parameters. It is a portable FFmpeg/SDL test player with `-nodisp` and `-autoexit`. [FFplay documentation](https://ffmpeg.org/ffplay.html)

Example shape, not a recommended final architecture:

```text
ffplay -nodisp -autoexit -f f32le -ar 24000 -ac 1 -
```

This can remove the WAV playback dependency and prove whether earlier PCM delivery improves perceived latency. It still leaves a subprocess, opaque device buffering, coarse interruption, and no capture clock or AEC render callback. A long-lived stdin stream also has no clean utterance-level clear operation; closing the pipe destroys the player.

### mpv

mpv offers persistent playback, gapless modes, JSON IPC, and a client API. Its manual recommends the client API when embedding. It also documents a default minimum audio buffer of 0.2 seconds and warns that manual buffer tuning is mainly for testing. [mpv manual](https://mpv.io/manual/stable/)

It is a much better general media player than `afplay`. It is also heavy for 24 kHz mono PCM and still separates playback from capture and AEC when driven as a subprocess.

### GStreamer

GStreamer's `appsrc` injects application data into a timestamped pipeline with an internal queue and backpressure. [GStreamer `appsrc`](https://gstreamer.freedesktop.org/documentation/applib/gstappsrc.html)

GStreamer can become a complete media graph and has WebRTC DSP elements, so it is the only external-framework option that can plausibly own playback, capture, clocks, and AEC together. It also adds a large runtime, plugin discovery, native packaging, and operational complexity. Nothing in DONNA currently requires a general media graph.

Verdict: avoid unless the roadmap expands to networking, multiple codecs, routing graphs, or cross-platform media pipelines beyond speech.

## Sample-rate policy

Kokoro's current output is 24 kHz mono. The listener currently captures at 16 kHz. AEC and hardware I/O become simpler with one internal device/DSP rate, normally 48 kHz:

```text
Kokoro 24 kHz mono
  -> worker resampler -> 48 kHz mono
  -> mix/upmap -> device output channels
  -> exact 48 kHz render reference

device input 48 kHz mono
  -> AEC at 48 kHz
  -> optional downsample -> 16 kHz VAD/Whisper path
```

Do not run the hardware at 24 kHz merely because Kokoro emits 24 kHz. Query the selected input/output devices, prefer a mutually supported native rate, and resample deliberately.

Python phase: `python-soxr` wraps libsoxr, accepts mono or multichannel NumPy arrays, and supplies a streaming `ResampleStream`. Version 1.1.0 added experimental variable-rate resampling and improved streaming performance. [python-soxr](https://github.com/dofuuz/python-soxr)

Rust phase: Rubato. C/C++ phase: miniaudio's converter, libsoxr, or WebRTC APM's supported input conversion at the worker boundary.

Resampling should occur before the playback ring when possible. The device callback should mostly copy and zero-fill.

## Recommended architecture

### Stage 1: Python reference implementation

Keep Python orchestration and the existing `sounddevice` dependency.

```text
                                  +-----------------------------+
Kokoro ndarray -- framed IPC ---->| audio owner                 |
                                  |                             |
Tink PCM ------------------------>| resample/mix worker         |
                                  |     |                       |
                                  | playback SPSC ring          |
                                  |     |                       |
                                  | sounddevice.Stream callback |
                                  |   /                   \     |
                                  | mic ring      output + ref  |
                                  |   |                   |     |
                                  | AEC placeholder/gate  stats |
                                  +---|-------------------------+
                                      v
                                  VAD -> Whisper
```

The audio owner should live with the listener, not inside Kokoro. That provides one long-lived duplex stream. Kokoro sends messages such as:

```text
ENQUEUE {utterance_id, sample_rate, channels, frame_count} + PCM payload
CLEAR   {utterance_id, fade_ms}
DRAIN   {utterance_id}
STATUS
```

Recordings may still be written asynchronously for diagnostics, but files stop being the playback transport.

Minimum telemetry:

- enqueue-to-first-DAC estimate;
- requested and actual input/output latency;
- playback queue depth in frames;
- rendered and dropped frames per utterance;
- input overflow and output underflow counts;
- callback CPU load;
- selected device IDs and negotiated formats;
- stop request to last audible frame estimate;
- route/configuration changes.

### Stage 2A: Apple production helper

Move the audio owner to Swift and use AVAudioEngine voice processing. Keep the same IPC protocol, so the Python/Kokoro side does not change. Export cleaned 16 or 48 kHz mono PCM to the Python listener.

Choose this if:

- macOS is the product target;
- speakerphone barge-in matters;
- the built-in voice processor performs well enough;
- minimal portable-native maintenance is preferred.

### Stage 2B: portable native extension

Move only the stream callback, rings, resampler, and AEC into a native module.

Preferred stack if AEC is required:

```text
C++17
  PortAudio or vendored miniaudio
  WebRTC Audio Processing Module
  nanobind + scikit-build-core
```

Preferred stack if the deliverable is a reusable safe player and AEC is deferred:

```text
Rust
  CPAL 0.18.x
  Rubato
  PyO3 + maturin
```

Keep the Python API identical across Stage 1 and Stage 2. That lets measurements, integration tests, and the Kokoro MCP contract survive a native rewrite.

## Why a native rewrite may or may not be faster

### What native code improves

- Predictable callback execution without Python interpreter scheduling.
- Lock-free/preallocated rings and atomics.
- Lower overhead for per-frame mixing, fading, resampling, and AEC.
- Cleaner ownership during interpreter shutdown.
- Easier real-time priority and platform callbacks.
- Native WebRTC or Apple DSP integration.

### What it does not improve automatically

- Kokoro model generation latency.
- Bluetooth/AirPlay transport latency.
- Hardware buffer size selected by the route.
- Poor queue depth policy.
- Waiting for a full utterance before enqueueing.
- AEC quality without an accurate render reference and delay model.
- Perceived latency if the dominant cost is TTS first-chunk generation.

The Python-to-native call overhead is irrelevant if each call enqueues tens of milliseconds or a whole generated chunk. It becomes harmful only if Python is invoked for every small render block or sample.

## Decision matrix

Scores: 5 is best for DONNA; maintenance is scored as ease, not power.

| Candidate | Playback control | Duplex/AEC foundation | Low-latency potential | Portability | Packaging | Maintenance | Total / 30 |
|---|---:|---:|---:|---:|---:|---:|---:|
| Persistent `sounddevice.Stream` | 5 | 5 | 3 | 5 | 5 | 5 | **28** |
| Swift AVAudioEngine helper | 5 | 5 | 5 | 1 | 4 | 4 | **24** |
| C++ PortAudio + WebRTC APM | 5 | 5 | 5 | 5 | 2 | 1 | **23** |
| C/miniaudio native extension | 5 | 4 | 5 | 5 | 4 | 4 | **27** |
| Rust CPAL + PyO3 | 5 | 3 | 5 | 5 | 4 | 3 | **25** |
| Rodio + PyO3 | 5 | 2 | 3 | 5 | 4 | 4 | **23** |
| GStreamer | 5 | 5 | 4 | 5 | 1 | 2 | **22** |
| mpv IPC | 4 | 1 | 2 | 5 | 2 | 4 | **18** |
| `ffplay` pipe | 2 | 1 | 2 | 5 | 3 | 5 | **18** |
| `afplay` | 1 | 1 | 2 | 1 | 5 | 5 | **15** |

The high score for Python `sounddevice` reflects migration value, not a claim that Python has the best hard real-time behavior. The high score for native miniaudio assumes playback/duplex I/O only; add WebRTC build complexity if AEC is included.

## Benchmark plan before choosing a rewrite

No audio was played during this research. Performance claims should be decided by a controlled local harness using the actual BRIO microphone, built-in speakers, AirPods, and expected CPU load.

### Playback metrics

1. Process/API call to first callback.
2. Enqueue time to `outputBufferDacTime` for the first nonzero frame.
3. Inter-chunk gap over 20 short Kokoro chunks.
4. Stop request to last nonzero submitted frame.
5. Output underruns while Kokoro and Whisper run concurrently.
6. Long-run drift and memory over 30 minutes.

### Duplex/AEC metrics

1. Relative ADC/DAC timestamps and reported latency.
2. Echo return loss enhancement or residual echo energy.
3. False Silero VAD triggers during assistant speech.
4. Whisper false transcripts of the assistant's voice.
5. User speech detection latency during double-talk.
6. Route changes and stream recovery.

### Candidates to benchmark

- Current WAV + `afplay` baseline.
- Raw PCM + one `ffplay` process as a quick subprocess comparison.
- Python persistent `sounddevice.Stream`.
- Swift AVAudioEngine voice-processing prototype.
- Only then, a native CPAL or miniaudio/PortAudio prototype.

Target acceptance criteria should be stated before measurement. Reasonable starting targets for a local assistant are no audible chunk gaps, zero underruns in a 30-minute stress run, a stop-to-silence path under 50 ms on built-in output, and no self-transcription during normal speaker playback. Bluetooth routes require separate targets.

## Concrete migration sequence

### Phase 0: preserve observability

- Keep WAV output as an optional asynchronous diagnostic artifact.
- Add utterance IDs and timestamps before changing playback.
- Record route, sample format, queue depth, underflow/overflow flags, and current playback-gate transitions.

### Phase 1: eliminate `afplay` from TTS

- Add one persistent `sounddevice.Stream` in the audio-owner process.
- Add a bounded playback ring and control queue.
- Convert Kokoro PCM to contiguous `float32` and send it over framed local IPC.
- Resample 24 kHz to the negotiated device rate on a worker.
- Route Tink through the same engine.
- Implement clear plus a 5–10 ms output ramp.
- Retain the playback flag only as a fallback safety gate.

### Phase 2: enable synchronized duplex experiments

- Capture and render in the same stream callback.
- Store ADC/DAC timestamps with frame counters.
- Copy exact submitted output into a render-reference ring.
- Keep VAD/STT off the callback.
- Run an offline aligned-recording experiment before adding real-time AEC.

### Phase 3: select native path from evidence

- If Python meets callback and latency targets, keep it and add the macOS Swift voice-processing helper only for AEC.
- If Python jitters but portable playback is the goal, replace the callback/rings with CPAL + PyO3.
- If portable AEC is the goal, replace the callback/rings/DSP with C++ PortAudio or miniaudio + WebRTC APM + nanobind.
- Do not rewrite TTS, MCP orchestration, VAD state logic, or file diagnostics in native code without a measured reason.

### Phase 4: remove the gate

- Remove microphone muting during playback only after AEC passes speaker, volume, distance, and double-talk tests.
- Keep a runtime fallback to gated half-duplex mode for unsupported routes or AEC failure.

## Failure modes to design for

- **Queue underrun:** zero-fill, increment a counter, preserve timeline discontinuity metadata for AEC.
- **Queue overrun:** bound latency; reject or drop stale utterance data rather than growing memory.
- **Stale chunk after interruption:** every PCM block carries an utterance generation ID; clear invalidates older IDs.
- **Click on clear:** short ramp or render one final fade block.
- **Device change:** rebuild outside callbacks and report a discontinuity to AEC/VAD.
- **Sample-rate change:** reset resampler and AEC state; never reinterpret PCM at the new rate.
- **Python shutdown:** explicitly close the engine before extension/module teardown.
- **Callback exception:** native callback cannot throw across C/Objective-C ABI boundaries.
- **Bluetooth latency:** do not equate software queue depth with acoustic delay.
- **Separate input/output devices:** expect clock drift; use timestamps, adaptive resampling, or constrain supported configurations.
- **AEC reference mismatch:** feed post-mix, post-volume samples actually submitted to the output path, not the original Kokoro array.

## Final recommendation

Implement the replacement first in Python with the existing PortAudio backend. The key change is architectural: one persistent audio owner and one duplex stream, not merely swapping `afplay` for another command.

Then choose the native endpoint based on product scope:

- **DONNA stays on macOS and needs barge-in:** Swift AVAudioEngine with Apple voice processing.
- **DONNA becomes cross-platform and needs AEC:** C++ PortAudio/miniaudio plus WebRTC APM, bound with nanobind.
- **DONNA needs a clean cross-platform playback engine but not native AEC yet:** Rust CPAL/Rubato, bound with PyO3/maturin.

Rust can produce a better player than `afplay`, but so can the C library already installed under `sounddevice`. The strongest reason to add native code is not raw playback speed. It is moving the real-time callback, render-reference ownership, resampling, and echo cancellation out of Python while keeping a small, stable Python control surface.

## Primary sources

### Existing Python and PortAudio path

- [python-sounddevice stream API](https://python-sounddevice.readthedocs.io/en/latest/api/streams.html)
- [python-sounddevice usage and callback examples](https://python-sounddevice.readthedocs.io/en/latest/usage.html)
- [PortAudio API overview](https://portaudio.com/docs/v19-doxydocs/api_overview.html)
- [PortAudio stream callback timing](https://portaudio.com/docs/v19-doxydocs/structPaStreamCallbackTimeInfo.html)
- [python-miniaudio on PyPI](https://pypi.org/project/miniaudio/)
- [miniaudio programming manual](https://miniaud.io/docs/manual/index.html)
- [python-soxr](https://github.com/dofuuz/python-soxr)

### Apple audio

- [AVAudioPlayerNode](https://developer.apple.com/documentation/avfaudio/avaudioplayernode)
- [AVAudioEngine](https://developer.apple.com/documentation/avfaudio/avaudioengine)
- [AVAudioSourceNode](https://developer.apple.com/documentation/avfaudio/avaudiosourcenode)
- [Audio Queue Services](https://developer.apple.com/documentation/audiotoolbox/audio-queue-services)
- [WWDC19: What's New in AVAudioEngine](https://developer.apple.com/videos/play/wwdc2019/510/)
- [Using voice processing](https://developer.apple.com/documentation/avfaudio/using-voice-processing)
- [TN2091: Device input using the HAL Output Audio Unit](https://developer.apple.com/library/archive/technotes/tn2091/_index.html)
- [PyObjC AVFoundation API notes](https://pyobjc.readthedocs.io/en/latest/apinotes/AVFoundation.html)

### Rust

- [CPAL documentation](https://docs.rs/cpal/latest/cpal/)
- [CPAL `DeviceTrait`](https://docs.rs/cpal/latest/cpal/traits/trait.DeviceTrait.html)
- [CPAL repository](https://github.com/RustAudio/cpal)
- [CPAL 0.18 upgrade guide](https://github.com/RustAudio/cpal/blob/master/UPGRADING.md)
- [Rodio repository](https://github.com/RustAudio/rodio)
- [Rodio player API](https://docs.rs/rodio/latest/rodio/struct.Player.html)
- [Rodio stream/buffer API](https://docs.rs/rodio/latest/rodio/stream/index.html)
- [Rubato resampler](https://docs.rs/rubato/latest/rubato/)
- [`objc2-avf-audio` AVAudioPlayerNode](https://docs.rs/objc2-avf-audio/latest/objc2_avf_audio/struct.AVAudioPlayerNode.html)
- [PyO3 build and distribution](https://pyo3.rs/main/building-and-distribution)
- [PyO3 parallelism and interpreter detachment](https://pyo3.rs/main/parallelism)
- [maturin](https://www.maturin.rs/)

### C/C++, bindings, and AEC

- [miniaudio repository](https://github.com/mackron/miniaudio)
- [RtAudio repository](https://github.com/thestk/rtaudio)
- [SDL3 audio API](https://github.com/libsdl-org/SDL/blob/main/include/SDL3/SDL_audio.h)
- [WebRTC Audio Processing Module overview](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/g3doc/audio_processing_module.md)
- [WebRTC APM source tree](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/)
- [nanobind documentation](https://nanobind.readthedocs.io/en/latest/)
- [nanobind packaging](https://nanobind.readthedocs.io/en/latest/packaging.html)
- [scikit-build-core](https://scikit-build-core.readthedocs.io/en/latest/guide/getting_started.html)
- [pybind11 GIL documentation](https://pybind11.readthedocs.io/en/stable/advanced/misc.html#global-interpreter-lock-gil)
- [NumPy C API](https://numpy.org/doc/stable/reference/c-api/index.html)

### External player/framework alternatives

- [FFplay documentation](https://ffmpeg.org/ffplay.html)
- [mpv manual](https://mpv.io/manual/stable/)
- [GStreamer `appsrc`](https://gstreamer.freedesktop.org/documentation/applib/gstappsrc.html)
- [SDL3 audio stream callback](https://wiki.libsdl.org/SDL3/SDL_AudioStreamCallback)
