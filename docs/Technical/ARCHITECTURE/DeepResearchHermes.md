# Hermes Agent, ElevenLabs, and Deepgram Research

## Fit assessment

Hermes can replace the Claude Code portion, ElevenLabs can replace Kokoro, and Deepgram can replace Whisper. The clean mapping is:

| Donna today | Proposed |
|---|---|
| whisper.cpp + Silero | Deepgram Flux |
| Persistent `ClaudeSDKClient` | Hermes agent runtime using Claude |
| Kokoro-local MCP | Hermes ElevenLabs TTS or a dedicated ElevenLabs streamer |
| File/playback gate | Streaming cancellation driven by Flux `StartOfTurn` |
| SQLite delivery queue | Retain unchanged |

Hermes v0.20.1 is the current release as of August 16, 2026. The v0.20 release introduced conversational voice, streaming TTS, barge-in, wake words and voice support across its interfaces. It also added `hermes import-agent` for Claude Code and Codex configurations. [Latest release](https://github.com/NousResearch/hermes-agent/releases/latest), [v0.20 release notes](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.8.3).

## Important distinction: Hermes is not Claude Code

Hermes is its own agent runtime:

- Tool loop
- Terminal and filesystem tools
- Skills
- MCP
- Memory
- Browser tools
- Approval/security system
- Sessions and delegation
- OpenAI-compatible API

It can use Claude through the Anthropic provider, including credentials already created by Claude Code, but execution occurs inside Hermes—not inside the Claude Code CLI. Hermes also exposes `claude` and `claude-code` model aliases. [Hermes provider documentation](https://hermes-agent.nousresearch.com/docs/integrations/providers/).

That means the replacement would be:

```text
Claude model
    inside
Hermes runtime
```

rather than:

```text
Hermes
    controlling
Claude Code
```

Tool behavior, approval semantics, hooks and session behavior will differ from the current persistent [`ClaudeSDKClient`](stt-local/claude_bridge.py#L16). The current bridge’s interrupt-then-drain discipline at [`claude_bridge.py`](stt-local/claude_bridge.py#L69) needs an explicit Hermes equivalent.

## Hermes's native voice path

Hermes already provides most of the desired surface:

- `/voice on` for continuous CLI conversation.
- Incremental sentence/clause TTS.
- Microphone monitoring during generation and playback.
- Spoken barge-in.
- Local wake-word detection.
- ElevenLabs TTS.
- ElevenLabs Scribe, OpenAI, Groq and several other STT providers.
- Custom command and Python transcription providers.

[Voice documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/voice-mode), [TTS/STT providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/tts).

Wake-word support is especially useful here. The Sherpa backend accepts arbitrary typed phrases without training, so it can reproduce “Oh my god, Donna.” Set `start_new_session: false` to retain the current conversational session instead of creating a fresh one after each wake. [Wake-word documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/wake-word).

Illustrative Hermes configuration:

```yaml
wake_word:
  enabled: true
  provider: sherpa
  phrase: "oh my god donna"
  sensitivity: 0.6
  start_new_session: false

tts:
  provider: elevenlabs
  elevenlabs:
    voice_id: YOUR_VOICE_ID
    model_id: eleven_multilingual_v2
    streaming_model_id: eleven_flash_v2_5
```

Use `eleven_flash_v2_5` for interactive streaming. Retain `eleven_multilingual_v2` only where voice quality matters more than latency.

## ElevenLabs integration

### Native Hermes integration

Hermes has a built-in ElevenLabs provider. Its streaming implementation:

1. Buffers model output into speakable clauses or sentences.
2. Removes markdown, emoji and reasoning blocks.
3. Calls ElevenLabs for 24 kHz PCM.
4. Starts playback while more audio is arriving.
5. Allows playback cancellation during barge-in.

The current implementation uses ElevenLabs SDK `text_to_speech.convert` per sentence rather than a single long-lived text-input WebSocket. [Hermes streaming implementation](https://github.com/NousResearch/hermes-agent/blob/main/tools/tts_streaming.py).

This is the best first implementation because it requires no custom TTS layer.

### Higher-performance integration

A custom voice gateway could use ElevenLabs's bidirectional text-to-speech WebSocket:

```text
Hermes text deltas
    → clause accumulator
    → ElevenLabs stream-input WebSocket
    → PCM playback buffer
```

On interruption:

1. Stop the local playback device immediately.
2. Discard queued PCM.
3. Close the active ElevenLabs stream.
4. Cancel the Hermes run.
5. Open a clean stream for the next response.

ElevenLabs recommends WebSockets when text itself arrives incrementally from an LLM. [Realtime WebSocket guide](https://elevenlabs.io/docs/eleven-api/guides/how-to/websockets/realtime-tts), [stream-input API](https://elevenlabs.io/docs/api-reference/text-to-speech/v-1-text-to-speech-voice-id-stream-input).

Latency is not automatically better than Kokoro. ElevenLabs advertises roughly 75 ms inference for Flash, but network TTFB from Japan to its US infrastructure is documented around 250–350 ms before local playback overhead. [Latency guidance](https://elevenlabs.io/docs/api-reference/reducing-latency).

Donna currently records roughly 0.16 seconds for warmed Kokoro calls at [`README.md`](../../../README.md#L136). ElevenLabs is therefore primarily a voice-quality, consistency and voice-library upgrade—not necessarily a raw latency upgrade.

## Deepgram integration

Hermes does not currently ship a native Deepgram transcription provider. Its built-in providers are visible in [`transcription_tools.py`](https://github.com/NousResearch/hermes-agent/blob/main/tools/transcription_tools.py).

There are two materially different integrations.

### 1. Simple Deepgram Nova-3 plugin

Implement a Hermes command or Python transcription provider that:

1. Receives the recorded WAV path.
2. Sends it to Deepgram `/v1/listen`.
3. Returns the best transcript.
4. Configures `nova-3` and smart formatting.

This is easy and useful for comparing Deepgram accuracy against whisper.cpp.

Its limitation is structural: Hermes's custom transcription interface is `transcribe(file_path)`. Hermes must finish recording the utterance before uploading it. Deepgram therefore cannot control endpointing or provide real-time turn events in this mode.

This produces:

```text
Hermes RMS silence detector
    → completed WAV
    → Deepgram Nova-3
    → transcript
```

It replaces Whisper, but does not realize Deepgram's strongest conversational capabilities.

### 2. Deepgram Flux voice gateway

For the intended interactive experience, use Flux directly over WebSocket:

```text
Microphone, 16 kHz mono PCM
        │
        ▼
Local Sherpa wake word
        │
        ▼
Deepgram Flux WebSocket
        │
        ├── StartOfTurn ───────────► stop speech / cancel Hermes
        ├── TurnResumed ───────────► cancel speculative work
        └── EndOfTurn ─────────────► durable enqueue
                                           │
                                           ▼
                                    Hermes /v1/runs
                                           │
                                    Claude + Hermes tools
                                           │
                                           ▼
                                  ElevenLabs streaming PCM
```

Flux provides:

- `StartOfTurn`
- `EagerEndOfTurn`
- `TurnResumed`
- `EndOfTurn`
- Approximately 260 ms end-of-turn detection
- Direct support for 16 kHz linear PCM
- Recommended 80 ms audio chunks

[Flux quickstart](https://developers.deepgram.com/docs/flux/quickstart), [turn-state documentation](https://developers.deepgram.com/docs/flux/state), [voice-agent integration guidance](https://developers.deepgram.com/docs/flux/agent).

Start with final `EndOfTurn` only. Do not enable speculative work from `EagerEndOfTurn` initially. Deepgram reports that eager processing can increase LLM requests by 50–70%, and every `TurnResumed` requires correct cancellation. [Eager EOT guidance](https://developers.deepgram.com/docs/flux/voice-agent-eager-eot).

## Recommended implementation

Retain Donna as the voice gateway and replace its internal services incrementally.

### Keep

- [`voice_queue.py`](stt-local/voice_queue.py#L145): durable delivery, IDs and deduplication.
- [`playback_control.py`](stt-local/playback_control.py): playback ownership and cancellation foundation.
- Audio-device discovery and fallback.
- Existing launch and monitoring infrastructure.
- Local wake detection.

### Replace

- [`listen_daemon.py`](stt-local/listen_daemon.py#L47): whisper.cpp/Silero utterance finalization → persistent Flux connection.
- [`claude_bridge.py`](stt-local/claude_bridge.py#L69): `ClaudeSDKClient` → Hermes run client.
- Kokoro MCP: ElevenLabs sentence streaming.

Hermes can run as a local authenticated sidecar:

```text
API_SERVER_ENABLED=true
API_SERVER_KEY=<secret>
hermes gateway
```

It exposes:

- `/v1/chat/completions`
- `/v1/responses`
- `/v1/runs`
- `/v1/runs/{id}/stop`
- SSE event streaming

The run API is the best match because it provides explicit lifecycle and cancellation. [Programmatic integration](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration), [API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server/).

## Barge-in design

Donna currently discards microphone input while TTS is playing, as documented in [`README.md`](../../../README.md#L155). The new path must keep capture active.

On Flux `StartOfTurn`:

1. Stop `afplay` or the PCM output stream.
2. Empty the playback buffer.
3. Close the ElevenLabs response stream.
4. Call `/v1/runs/{run_id}/stop`.
5. Drain Hermes's terminal response event.
6. Wait for Flux `EndOfTurn`.
7. Enqueue the new transcript.

Do not submit the partial `StartOfTurn` transcript as a user command. Use it only as an interruption signal.

Neither Hermes's RMS-based barge-in nor Flux performs acoustic echo cancellation. With external speakers, model speech can be mistaken for user speech. Use AirPods/headphones initially or add macOS voice-processing/AEC before enabling speaker-mode barge-in.

## Migration plan

1. **Hermes evaluation**
   - Import the Claude Code configuration.
   - Select Claude through `hermes model`.
   - Verify shell approvals, MCP tools, filesystem scope and session persistence.

2. **Native ElevenLabs**
   - Enable Hermes voice mode and ElevenLabs Flash.
   - Measure first audible audio and interruption delay.
   - Keep current Whisper input initially.

3. **Deepgram batch**
   - Add a Nova-3 transcription plugin.
   - A/B test command names, paths and technical vocabulary against `ggml-base.en`.

4. **Flux gateway**
   - Replace file transcription with `/v2/listen`.
   - Use `EndOfTurn` only.
   - Preserve Donna's durable queue between Flux and Hermes.

5. **Full-duplex**
   - Wire `StartOfTurn` to playback and Hermes cancellation.
   - Add AEC or require headphones.
   - Introduce `EagerEndOfTurn` only after cancellation is proven.

6. **ElevenLabs WebSocket**
   - Add only if native Hermes sentence streaming has measurable gaps.

Measure P50/P95 for:

- Speech end → Flux `EndOfTurn`
- `EndOfTurn` → Hermes acceptance
- Hermes acceptance → first text delta
- First speakable clause → first ElevenLabs audio
- User speech start → playback stopped
- Duplicate/lost commands
- False wake and false interruption rates

## Trade-offs

- **Latency:** Flux should substantially improve the current three-second silence-finalization experience. ElevenLabs may be slower than warmed Kokoro from Tokyo.
- **Quality:** ElevenLabs likely improves voice quality. Deepgram should improve names, formatting and conversational turn handling.
- **Privacy:** Wake detection remains local, but Deepgram receives microphone audio and ElevenLabs receives response text.
- **Reliability:** Donna's SQLite queue is stronger than relying on Hermes voice-mode state alone. Preserve it.
- **Cost:** Deepgram Flux currently lists English streaming at $0.0065/minute. ElevenLabs is character-metered. [Deepgram pricing](https://deepgram.com/pricing), [ElevenLabs API pricing](https://join.elevenlabs.io/api/developer-api).
- **Security:** Hermes exposes powerful terminal/filesystem tools. Keep API authentication enabled even on loopback and configure approval mode deliberately. [Hermes security model](https://hermes-agent.nousresearch.com/docs/user-guide/security/).

## Recommended target

Donna remains the durable, low-level voice transport; Deepgram owns conversational turn detection; Hermes owns the agent/tool loop using Claude; ElevenLabs owns synthesis.

Avoid putting ElevenLabs Agents or Deepgram Voice Agent above Hermes. The second orchestration layer would complicate tool approvals, state, cancellation and delivery guarantees.
