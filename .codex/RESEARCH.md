# Voice gateway for any agent: architecture and setup research

Research date: 2026-08-22

## Executive conclusion

The reliable design is not “microphone input injected into an already-open agent
terminal.” It is a voice gateway that owns, or has a supported bidirectional API
to, each agent session:

```text
microphone / WebRTC
        ↓
AEC + noise suppression + turn detection
        ↓
streaming STT
        ↓
durable utterance log → agent router → agent adapter → owned agent session
                                               ↓
                                      streamed text events
                                               ↓
                                    sentence chunker + TTS
                                               ↓
                                         audio playback
```

For DONNA, the recommended route is:

1. Keep the existing local microphone, Silero VAD, Whisper, SQLite queue, and
   playback control while replacing the legacy mutable buffer and Claude Code
   `Stop` hook with explicit agent adapters.
2. Implement Claude through `ClaudeSDKClient` and Codex through `codex
   app-server` or `@openai/codex-sdk`. Both expose persistent sessions, streamed
   events, and interruption primitives.
3. Treat Hermes Agent as both a reference implementation and an optional agent
   backend. Its native voice mode already implements the correct high-level
   loop, including local/cloud STT and pluggable TTS, including ElevenLabs.
4. Keep the default speech path local: whisper.cpp for transcription, Silero
   VAD for turn detection, and kokoro-local for synthesis. Cloud speech and
   inference providers require explicit opt-in.
5. Add a WebRTC or native voice-processing front end before enabling real
   full-duplex barge-in. Raw `sounddevice` capture plus independent `afplay`
   playback has no echo reference and cannot reliably distinguish the user from
   the system's own voice.
6. Standardize the public agent boundary with A2A where possible. Keep MCP for
   agent-to-tool access, not as the primary user-message transport.

This supports “any agent” that exposes an SDK, API, app-server protocol, or A2A
endpoint. It cannot make every arbitrary interactive terminal session safely
injectable; unsupported PTY keystroke automation remains brittle and should not
be the core architecture.

## What “any agent” requires

An agent is more than an LLM endpoint. Claude Code, Codex, and Hermes carry
workspace state, tools, permissions, session history, and long-running tasks.
Changing an ElevenLabs LLM dropdown from one model to another does not connect
to those existing agent runtimes.

The gateway therefore needs a small, stable contract:

```python
class AgentAdapter:
    async def start_or_resume(session_key, workspace, policy): ...
    async def send(utterance_id, text): ...
    async def events(): ...
    async def interrupt(reason): ...
    async def answer_approval(request_id, decision): ...
    async def health(): ...
    async def close(): ...
```

Normalize backend output into events such as:

```text
session.started
turn.started
text.delta
tool.started
approval.required
tool.completed
turn.completed
turn.interrupted
turn.failed
```

The voice layer should know nothing about Claude, Codex, Hermes, model vendors,
or tool protocols. It routes a finalized utterance to one adapter and consumes
the same event stream from every adapter.

For an external interoperability boundary, A2A is the best current match. Its
official description distinguishes MCP as agent-to-tool communication and A2A
as agent-to-agent communication. A2A supplies Agent Cards, stateful tasks,
messages, SSE streaming, and asynchronous updates. It is suitable for wrapping
remote or independently deployed agents, though native support is not yet
universal and adapters will still be needed. [A2A overview](https://a2a-protocol.org/latest/),
[A2A specification](https://a2a-protocol.org/latest/specification/)

## Hermes Agent findings

Hermes Agent is the closest open implementation found to the desired user
experience. Its voice support is part of the agent runtime rather than an
external hook:

- CLI voice mode records from the microphone, detects silence, transcribes,
  sends the result directly into the full agent pipeline, streams the reply,
  and restarts listening.
- Telegram and Discord accept voice messages and return spoken responses.
- Discord voice-channel mode listens per user, detects silence, transcribes,
  runs the normal session/tools/memory pipeline, and speaks back.
- STT providers include local Faster Whisper, Groq, OpenAI, Mistral, and xAI.
- TTS providers include Edge, ElevenLabs, OpenAI, NeuTTS, MiniMax, Mistral,
  Gemini, xAI, KittenTTS, and Piper.
- It exposes direct-versus-draft submission in the TUI, which is useful for
  deciding whether risky speech is immediately executed or first shown for
  confirmation.
- It pauses listening during TTS in Discord voice channels to prevent echo.
  This is safe half-duplex behavior, not true barge-in.

The official setup is:

```sh
# Install Hermes itself after reviewing the installer.
curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash

# Verify text mode before adding voice.
hermes

# Install CLI voice support and premium TTS support.
cd "${HERMES_AGENT_DIR}"
uv pip install -e '.[voice,tts-premium]'

# macOS audio dependencies.
brew install portaudio ffmpeg opus
```

Then configure `~/.hermes/config.yaml` with local STT and ElevenLabs TTS:

```yaml
voice:
  record_key: ctrl+b
  submit_mode: direct
  max_recording_seconds: 120
  auto_tts: true
  silence_threshold: 200
  silence_duration: 1.0

stt:
  provider: local
  local:
    model: base

tts:
  provider: elevenlabs
  elevenlabs:
    voice_id: YOUR_VOICE_ID
    model_id: eleven_multilingual_v2
```

Store `ELEVENLABS_API_KEY` in `~/.hermes/.env`, then run `/voice on` inside
Hermes. The Hermes documentation recommends starting with local STT plus free
Edge TTS, then adding ElevenLabs for higher voice quality. The exact silence
duration should be tested; Hermes defaults to 3 seconds, which is conservative
but too slow for a low-latency assistant. [Hermes voice reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/voice-mode.md),
[Hermes practical setup guide](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/guides/use-voice-mode-with-hermes.md)

What DONNA should borrow from Hermes:

- Voice input enters the agent runtime through a supported session API.
- STT and TTS are provider interfaces rather than agent-specific code.
- Direct and draft submission are explicit policies.
- The same session, tools, memory, and approvals are used for text and voice.
- Voice controls are strict commands, not loose substring matches.
- Text remains visible alongside audio for debugging and accessibility.

What Hermes does not solve for DONNA:

- Native voice mode speaks with Hermes, not arbitrary already-running agents.
- Pausing capture during TTS prevents echo but also prevents natural barge-in.
- Its default silence endpoint is deliberately slow.
- It is a complete agent runtime; adopting it solely as a voice shell may add a
  second memory/session system when DONNA already has agent backends.

## ElevenLabs findings

ElevenLabs can be used at two very different layers.

### Option 1: ElevenLabs only for TTS

This is the least invasive choice. DONNA keeps local capture, VAD, Whisper,
routing, agent sessions, and tools. Text deltas from any agent are grouped into
speakable chunks and sent to ElevenLabs streaming TTS. This preserves backend
independence and keeps transcript processing local until the response text is
sent for synthesis.

Hermes validates this pattern: ElevenLabs is one selectable TTS provider and
its streaming TTS path plays sentence-by-sentence rather than waiting for the
entire answer. For DONNA this is the recommended first ElevenLabs integration.

### Option 2: ElevenAgents as the speech runtime

ElevenAgents coordinates STT, an LLM or custom LLM, TTS, and a proprietary
turn-taking model. Clients can send audio and receive audio over one WebSocket;
the platform also supports web/mobile SDKs, Twilio, and SIP. It exposes user
transcripts, agent responses, VAD scores, tool calls, contextual updates, and
interruption/turn settings. [ElevenAgents architecture](https://elevenlabs.io/docs/eleven-agents/overview/),
[conversation WebSocket](https://elevenlabs.io/docs/eleven-agents/api-reference/eleven-agents/websocket),
[conversation flow](https://elevenlabs.io/docs/eleven-agents/customization/conversation-flow)

For a custom backend, ElevenLabs offers two patterns:

- A custom LLM HTTP endpoint compatible with OpenAI Chat Completions or
  Responses. Responses must stream with SSE; function calling is required for
  ElevenLabs system tools.
- The Speech Engine SDK, where ElevenLabs opens a “brain” WebSocket to the
  developer's server and exchanges transcripts for streamed generated text.
  This is documented particularly for Twilio bridges.

[Custom LLM endpoint](https://elevenlabs.io/docs/eleven-agents/customization/llm/custom-llm),
[Speech Engine custom-LLM bridge](https://elevenlabs.io/docs/eleven-agents/phone-numbers/twilio-integration/custom-llm-integration)

An OpenAI-compatible endpoint is not automatically a Claude Code, Codex, or
Hermes session. DONNA would still need an adapter/proxy that translates each
incoming conversation to the target runtime, preserves its session ID, streams
text back, and surfaces approvals. That proxy is the important part.

ElevenAgents advantages:

- Fastest route to polished turn-taking, interruption handling, hosted audio,
  web/mobile clients, and telephony.
- One audio WebSocket and fewer custom realtime components.
- Native model selection plus custom LLM support.
- Built-in testing, analytics, and conversation monitoring.

Tradeoffs:

- Cloud audio and transcript processing create a larger privacy boundary.
- Platform, model, and telephony charges are separate. Current public pricing
  is subscription/minute based and should be checked again before adoption.
- Custom agent runtimes still require adapters.
- The custom endpoint must be publicly reachable from ElevenLabs and secured.
- Turn-taking and audio behavior become coupled to a proprietary control plane.
- Zero Retention Mode is available, but reduces debugging data and has feature
  restrictions; default retention behavior is not equivalent to local-only
  processing. [ElevenAgents pricing](https://elevenlabs.io/pricing/agents),
  [privacy controls](https://elevenlabs.io/docs/eleven-agents/customization/privacy),
  [Zero Retention Mode](https://elevenlabs.io/docs/eleven-agents/customization/privacy/zrm)

Recommendation for this project: do not enable ElevenLabs by default. Retain it
as an evaluated, explicit opt-in option only; the selected implementation is
whisper.cpp, Silero VAD, and kokoro-local.

## Framework and provider comparison

| Choice | Best use | Agent portability | Audio/turn plumbing | Self-host option | Main tradeoff |
|---|---|---:|---:|---:|---|
| Hermes native voice | Personal Hermes assistant | Low outside Hermes | Included | Yes | Fastest working local reference, but it is the agent runtime |
| ElevenLabs TTS only | High-quality output for DONNA | High | TTS only | No | Keeps architecture open; still requires STT, turns, routing |
| ElevenAgents | Managed web/mobile/phone agent | Medium through custom LLM proxy | Included | No | Low implementation effort, higher lock-in/privacy/cost |
| LiveKit Agents | Production WebRTC, apps, telephony | High | Excellent | Yes | More infrastructure than a single-user local daemon |
| Pipecat | Modular Python pipeline | High | Strong, explicit pipeline | Yes | Developer owns deployment and realtime operations |
| Deepgram Voice Agent | Managed STT/LLM/TTS WebSocket | Medium | Included | Service-dependent | Similar managed tradeoff to ElevenAgents, stronger STT heritage |
| OpenAI Realtime | Direct speech-to-speech agent | Low-to-medium | Included | No | Very low latency, but voice and agent brain are coupled |

LiveKit is the strongest fit when DONNA needs a browser/mobile client,
networked users, true realtime media, or telephony. Its open-source Agents SDK
supports provider plugins, WebRTC transport, turn detection, interruptions,
agent handoffs, and self-hosted or cloud deployment. [LiveKit Agents overview](https://docs.livekit.io/agents/),
[LiveKit turn handling](https://docs.livekit.io/agents/logic/turns/)

Pipecat is the leaner framework for keeping the current Python architecture.
Its pipeline is explicitly `transport → STT → context/agent → TTS → transport`,
and processors can be swapped without rewriting the rest of the path. Its
high-priority system frames include speech-start and interruption events, while
data/control frames retain ordering. [Pipecat pipeline](https://docs.pipecat.ai/pipecat/learn/pipeline)

Deepgram Voice Agent is another managed single-WebSocket option with configurable
STT, LLM, TTS, endpointing, functions, telephony, and multi-agent handoffs. It is
worth evaluating if cloud STT accuracy and endpointing become the bottleneck.
[Deepgram Voice Agent](https://developers.deepgram.com/docs/voice-agent),
[function calling](https://developers.deepgram.com/docs/voice-agents-function-calling)

OpenAI Realtime models accept realtime audio/text and expose function calling
over WebRTC, WebSocket, or SIP. This is attractive for a standalone voice agent,
but less suitable as a neutral shell around Claude Code, Codex, and Hermes
because the realtime model itself becomes the conversational brain. [Official
OpenAI Realtime model documentation](https://developers.openai.com/api/docs/models/gpt-realtime-2.1)

## Concrete agent adapters

### Claude

Use the Claude Agent SDK, not a Claude Code `Stop` hook. Streaming input mode is
the documented preferred mode for a persistent interactive session. It supports
queued messages, partial output, tools, permissions, session continuity, and
interrupts. `ClaudeSDKClient` can keep one conversation across queries and can
interrupt a running task; after interruption the client must drain the old
result before submitting and reading the next response. [Claude streaming input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode),
[Python SDK and interruption contract](https://code.claude.com/docs/en/agent-sdk/python),
[streaming output](https://code.claude.com/docs/en/agent-sdk/streaming-output)

The existing `claude_bridge.py` is directionally correct because it owns an SDK
session. It should be promoted behind the generic adapter and become the only
Claude delivery path after cutover.

### Codex

Use `codex app-server` for the richest integration. It is the interface used by
Codex graphical clients, speaks bidirectional JSON-RPC over stdio, WebSocket, or
Unix socket, and models conversations as threads containing turns and streamed
items. It also sends server-initiated approval requests that the voice UI must
present and answer. Generate the protocol schema from the installed Codex
version rather than copying shapes from documentation.

For a smaller TypeScript integration, `@openai/codex-sdk` exposes persistent
threads, `runStreamed()`, structured events, and thread resume. [Codex app-server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md),
[Codex TypeScript SDK](https://github.com/openai/codex/blob/main/sdk/typescript/README.md)

### Hermes

For a Hermes-only experience, use Hermes native voice mode and avoid duplicating
its voice stack. For unified DONNA routing, wrap the Hermes agent/gateway behind
the same adapter or an A2A server. Preserve its own session, memory, skills, and
approval policy; do not reduce it to a stateless LLM completion endpoint.

### Other agents

Adapter priority:

1. Native persistent agent SDK or app-server protocol.
2. A2A endpoint with streaming.
3. Stateful HTTP/WebSocket agent API.
4. Structured CLI execution with machine-readable streaming events.
5. PTY automation only as an explicitly unsupported compatibility adapter.

## Audio and turn-taking design

### Start half-duplex, then add real AEC

The current playback gate is a valid safe first step: stop feeding microphone
audio into STT while TTS is active, then resume after a short hangover. It
prevents self-transcription but cannot support barge-in.

For full duplex, use a capture/playback system with the speaker signal as an
echo reference. WebRTC's Audio Processing Module provides acoustic echo
cancellation, noise suppression, and automatic gain control. Browser capture
also exposes standardized `echoCancellation`, `noiseSuppression`, and
`autoGainControl` constraints. On Apple-native clients, use Voice-Processing
I/O rather than unrelated input/output processes. [WebRTC Audio Processing Module](https://webrtc.googlesource.com/src/+/refs/heads/main/modules/audio_processing/g3doc/audio_processing_module.md),
[W3C Media Capture constraints](https://www.w3.org/TR/mediacapture-streams/),
[Apple Voice I/O](https://developer.apple.com/documentation/AudioToolbox/audio-unit-voice-i-o)

### Turn detection

Use VAD to detect speech start quickly, but do not equate the first silence with
semantic end-of-turn. A robust policy combines:

- VAD speech start/end;
- streaming STT endpoint/final markers;
- a configurable minimum and maximum silence window;
- optional semantic turn detection;
- a manual push-to-talk/draft mode for risky commands.

Only finalized utterances enter the durable queue. Partial transcripts may be
shown in the UI but must not execute tools.

### Streaming TTS

Do not wait for the complete agent response. Strip markdown and non-spoken
artifacts, buffer text into sentence or clause-sized chunks, synthesize chunks
in order, and maintain a cancellable playback job per turn. On barge-in:

1. stop queued/current TTS;
2. record exactly how much output the user heard;
3. signal the agent adapter to interrupt if policy allows;
4. drain its terminal event according to that backend's contract;
5. submit the new finalized utterance.

Streaming and pipelining matter more than choosing one nominally fast component.
A 2026 implementation study using streaming Deepgram STT, a streamed LLM, and
ElevenLabs TTS measured 947 ms median time to first audio and attributed
realtime behavior to pipelining across all stages. Treat that number as one
published implementation result, not a vendor-independent guarantee. [Realtime
voice-agent implementation study](https://arxiv.org/abs/2603.05413)

## Durable state and delivery semantics

Replace the mutable text file with an append-only event model. Minimum durable
records:

```text
sessions
  session_key, agent_id, backend_session_id, workspace, policy, state

utterances
  utterance_id, session_key, sequence, audio_ref, transcript,
  transcript_finalized_at, state, attempt_count, last_error

agent_events
  event_id, utterance_id, backend_event_id, type, payload, created_at

playback_jobs
  job_id, utterance_id, chunk_sequence, state, started_at, stopped_at
```

Required invariants:

- Assign an immutable utterance ID before routing.
- Never delete/truncate input before the adapter accepts it.
- Distinguish “accepted by agent,” “turn completed,” and “spoken.”
- Preserve strict ordering per session; allow concurrency across sessions.
- Make retries idempotent where the backend supports idempotency.
- Keep shadow rows permanently non-deliverable.
- Recover expired leases after crashes.
- Never infer completion from a socket notification; the database is
  authoritative.
- Store transcript/audio only under an explicit retention policy.

The existing SQLite work is a good base. The legacy buffer/Stop path violates
several of these rules and should remain only as a temporary rollback path.

## Routing and user experience

Keep routing deterministic. Examples:

```text
“Donna, switch to Hermes.”
“Donna, ask Codex to inspect this repository.”
“Donna, continue with Claude.”
“Donna, stop speaking.”
“Donna, cancel that task.”
```

Do not let an LLM silently choose an agent for commands that can mutate files or
external systems. Maintain an explicit active agent, announce switches, and
require confirmation for ambiguous routing.

Separate control intent from agent text:

```text
control.switch_agent
control.cancel_turn
control.mute
control.unmute
control.stop_listening
message.user
approval.answer
```

Strictly match destructive confirmations. A voice transcript such as “yes”
should authorize only the currently presented approval, with a short expiry and
request ID. Never treat ambient or stale speech as approval.

## Security and privacy requirements

- Visible microphone/listening indicator and explicit start/stop control.
- Allowlist users for networked or Discord/phone access.
- Keep provider keys in environment/secret storage, never config committed to
  the repository.
- Encrypt remote transports and authenticate both voice clients and agent
  adapters.
- Redact secrets from transcripts, logs, and spoken responses.
- Default to no raw-audio retention; make transcript retention configurable.
- Keep agent permissions identical between typed and spoken use.
- Surface approvals and clarification requests through text and voice.
- Rate-limit wake triggers and reject replayed utterance IDs.
- Treat STT output as untrusted user input, not system instructions.
- If ElevenLabs handles sensitive content, configure retention explicitly and
  verify Zero Retention Mode eligibility and restrictions.

## Recommended implementation sequence for DONNA

### Phase 0: restore the compatibility path

- Register the existing Stop hook with `asyncRewake` and a timeout longer than
  its one-hour poll.
- Send the exit-code-2 payload to stderr for conventional compatibility.
- Add readiness checks for hook registration, `asyncRewake`, timeout, script
  executability, and buffer writability.
- Package the Stop script and correct hook snippet.

This restores current behavior but does not make the design reliable or
agent-agnostic.

### Phase 1: direct agent gateway

- Define `AgentAdapter` and normalized event types.
- Put the existing Claude SDK bridge behind `ClaudeAdapter`.
- Add `CodexAdapter` using app-server or the TypeScript SDK.
- Add explicit session/agent routing.
- Deliver only new non-shadow rows; never replay historical shadow rows.
- Add adapter conformance tests with deterministic fake agents.

Exit criterion: the same synthetic utterance can be routed to Claude or Codex,
stream output, survive worker restart, and never deliver twice.

### Phase 2: local streaming TTS

- Put kokoro-local behind a TTS provider interface.
- Implement cancellable local streaming output.
- Sentence-chunk normalized `text.delta` events.
- Track first-text and first-audio latency.
- Keep cloud TTS providers disabled unless explicitly selected.

Exit criterion: switching TTS provider does not change agent delivery or
session state.

### Phase 3: conversational audio front end

- Use LiveKit/WebRTC for browser/mobile/network use, or native Apple Voice I/O
  for a Mac-only client.
- Add AEC, noise suppression, and full-duplex barge-in.
- Replace fixed silence-only endpointing with combined VAD/STT/semantic turn
  detection.

Exit criterion: TTS played through speakers does not create an agent command,
and a real user interruption cancels playback and redirects the active turn.

### Phase 4: open agent interoperability

- Expose the gateway's agent boundary through A2A.
- Add Hermes as a native or A2A adapter.
- Publish capabilities, supported input/output modes, approvals, and
  interruption support per agent.
- Add health checks and graceful degradation for unavailable agents.

Exit criterion: adding a compliant agent requires configuration plus an adapter,
not changes to capture, STT, routing, queue, or TTS code.

## Test matrix

| Area | Required tests |
|---|---|
| Capture | silence, clipped first word, long monologue, noisy room, device loss |
| Turn detection | short pause, thinking pause, backchannel, immediate interruption |
| STT | partial/final ordering, duplicate final, empty final, hallucinated silence |
| Routing | explicit switch, ambiguous name, unavailable agent, simultaneous sessions |
| Delivery | crash before accept, crash after accept, duplicate notification, lease expiry |
| Agent | long tool call, approval, clarification, interrupt, failed/retried turn |
| TTS | sentence ordering, markdown stripping, cancellation, provider failure/fallback |
| Echo | speaker leakage, headphones, external USB mic, user barge-in during playback |
| Security | unauthorized speaker/client, stale approval, replayed utterance, secret redaction |

Capture these timestamps for every utterance:

```text
speech_started
speech_ended
transcript_final
agent_accepted
first_text_delta
first_tts_audio
playback_started
turn_completed
```

Report p50, p95, and failure rate. Optimize measured stages rather than tuning
one global timeout.

## Final recommendation

Use Hermes today if the immediate goal is a working voice-native personal agent,
but pin its inference provider to local Ollama when paid services are disallowed.

For DONNA's broader goal, do not replace the current hook with another polling
mechanism. Build the agent-adapter boundary first. Use Claude Agent SDK and
Codex app-server as the first two adapters, whisper.cpp plus Silero for local
input, and kokoro-local for output. Add Hermes through the same boundary.
Standardize remote agents with A2A and retain MCP strictly for tools.

That design makes the voice layer reusable, preserves each agent's real tools
and session, and removes lifecycle hooks, terminal scraping, and mutable files
from the critical delivery path.
