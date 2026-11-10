# Voice Router Ideas

## Classification

Routing precedence:

1. `#coding` → `qwen3-coder:30b`
2. `#non-coding` → `qwen3.8:27b-mlx`
3. `#unknown` → clarification or an explicitly authorized higher-tier agent

`#question` and `#statement` describe turn behavior rather than model selection. A coding question remains `#coding`; the question attribute controls acknowledgement and dispatch timing.

All default inference stays local. Claude, Codex, Bedrock, or another paid service must never be selected without explicit authorization.

## Turn Boundaries

A 30-second silence is definitive, but too slow as the normal voice boundary. Use it as a hard fallback for unfinished monologues.

- Complete question: speculative commit after roughly 1–2 seconds of silence.
- Incomplete utterance or statement: continue collecting.
- New speech: extend the interval and invalidate stale speculative work.
- Thirty seconds of silence: force completion.

Question-mark punctuation from Whisper is evidence, not proof. Whisper can omit or hallucinate punctuation.

## Multi-Agent Design

Use distinct roles rather than two peers rewriting each other:

```text
Agent B: continuous shadow intent observer
Agent A: response generator
Coordinator: dispatch, cancellation, sequencing, and playback authority
```

Agent B analyzes utterances asynchronously and maintains structured state:

```json
{
  "revision": 12,
  "evidence_through_seq": 48,
  "domain": "coding",
  "intent": "debug_rust_lifetime",
  "complete": true,
  "addressed_to_agent": true,
  "confidence": 0.91,
  "constraints": ["preserve existing API"]
}
```

Agent A receives the frozen transcript interval and Agent B's latest intent hypothesis.

## Coordination Rules

- B finishes before A starts: include B's analysis.
- B materially changes intent while A is generating: cancel and restart A.
- B changes intent after A finishes but before TTS: discard A's stale response.
- B changes intent after TTS starts: interrupt only for barge-in, safety, wrong addressee, or a fundamentally invalid response.
- Never rewrite content that has already been spoken.
- Every analysis and response references immutable utterance sequence numbers and an intent revision.
- New utterances do not automatically cause cloud escalation.

```text
LISTENING
  → VAD_END
  → CLASSIFY
  → GRACE_WINDOW
  → FREEZE_INTERVAL
  → DISPATCH
  → VERIFY_GENERATION
  → SPEAK
```

## Question Acknowledgement

Do not repeat every question automatically. Repetition adds latency, may expose sensitive speech, and feeds TTS back into the microphone.

If inference exceeds an acknowledgement threshold, use a neutral local earcon or a brief acknowledgement such as "Got it." Before playback, verify that no newer utterance or intent revision has invalidated the response.

## Escalation

Escalate only when:

- Domain confidence is low.
- Coding and general content are genuinely mixed.
- Required tools are unavailable to the selected model.
- The selected model fails or returns an invalid response.
- The task is high-risk.
- The user explicitly requests a higher-tier agent.

`#unknown` should normally go to the best local general model or produce a clarification. It must not silently invoke a paid service.

## Risks

- Two large local models running concurrently may create memory pressure, eviction, and worse latency than one model.
- The intent observer should be a small resident model or deterministic rules where possible.
- Self-reported confidence is unreliable; retain evidence sequence numbers and alternative interpretations.
- Similar models may make correlated errors, so multiple agents do not guarantee independent verification.
- Repeated intent revisions can starve response generation while the user continues speaking.
- Sentence-level answer rewriting can create audible contradictions.

The intent observer should improve the coordinator's semantic state. It should not directly edit Agent A's answer while that answer is being spoken.
