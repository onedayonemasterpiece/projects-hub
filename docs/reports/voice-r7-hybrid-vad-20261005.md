# Voice R7 — low-latency explicit client VAD acceptance

**Date:** 2026-10-05
**Final product target:** Projects Hub 0.1.35
**Shared Live target:** live-interaction 0.3.25 @ `4d5589bfc52ca74b2fc451e5a27788b42196823a`

## Goal

Reduce speech-to-reply latency without reintroducing premature turn splitting, parasite turns from short noises, or a second semantic ASR/LLM path.

Transcribe Live remains explicitly out of scope. Mira remains the only semantic conversational agent and receives microphone audio directly.

## Google guidance evaluated

Google Live API guidance supports both automatic activity detection and explicit/manual client activity boundaries. We evaluated Google's hybrid pattern (provider speech-start + client `audio_stream_end`) because it can reduce endpoint latency while keeping provider prefix handling.

Shared browser audio batching was reduced from 80 ms to 40 ms, within the recommended small realtime-chunk range. Resource-control was checked first: PCM uses bulk grants, so the smaller browser batch does not cause one central grant RPC per audio frame.

## Hybrid experiment and rejection

Projects Hub 0.1.34 temporarily enabled provider automatic VAD for realtime speech start and used client `audio_stream_end` for speech end.

Deterministic contracts passed, and a real WSS/provider roundtrip with PCM + `audio_stream_end` passed transport/output lifecycle.

However, a real-provider speech lifecycle canary then streamed 3 seconds of synthesized speech and sent `audio_stream_end`. Transport ACKs and `input_timing` were present, but Gemini 3.8 Live produced no `input_transcript` for the turn. This reproduced the earlier failure family where provider speech-start did not reliably open on accepted realtime PCM.

Conclusion: hybrid/provider-owned speech start is not reliable enough for this conversational 3.8 Live path. It is rejected for production R7.

Production 0.1.34 was immediately rolled back to the physically accepted 0.1.33 while the final R7 path was prepared. The queued Android 0.1.34 release was cancelled before publication.

## Final R7 behavior

Realtime returns to the proven explicit client activity boundary:

- `manualActivityDetection: true`;
- provider automatic VAD disabled;
- sustained speech-start admission remains 180 ms;
- ordinary speech-end silence: 650 ms;
- after 8 seconds of admitted speech, speech-end silence: 1400 ms;
- accepted speech is bracketed by `activity_start/activity_end`;
- shared audio batch: 40 ms.

This keeps the R4/R6 impulse rejection that physically rejected keyboard/finger-snap noise, while removing 1.35 seconds from the previous ordinary 2-second endpoint wait.

Buffered/recovery audio remains on its existing explicit/manual boundary contract.

## Why this is the reliable choice

The 180 ms client speech-start path has already been physically accepted on Android and does not rely on provider speech-start detection.

The 650 ms ordinary endpoint remains above the overly aggressive 100–200 ms range that tends to split natural speech. Long speech gets a wider 1400 ms thinking pause after 8 seconds, reducing fragmentation of monologues.

This is deliberately less clever than the rejected hybrid approach: one deterministic client VAD opens/closes the provider activity, while Gemini 3.8 Live still owns transcription, semantics, tools and response audio.

## Observability

R7 also exposes existing non-content `input_timing` fields in backend JSON logs:

- `audio_chunks`;
- `max_stdin_delay_ms`;
- `max_ws_send_ms`;
- `audio_stream_end_sent_at`;
- `activity_end_sent_at`.

The initial 0.1.34 adapter emitted these fields but the JSON formatter whitelist dropped them; the final R7 follow-up corrects the whitelist and covers it with a formatter test.

No microphone bytes or transcript text are added to structured diagnostics.

## Acceptance gates

Before final deployment:

- shared live-interaction: 88/88 Node and 53/53 Python;
- Projects Hub full backend: 141/141;
- WSS contracts: 12/12;
- PWA contracts: 28/28 plus production build;
- real-provider manual-activity lifecycle must pass after the final 0.1.35 deployment.

## Deferred work

Do not mix these into R7 before physical acceptance:

1. visible interim user text via Transcribe Live UI-only sidecar;
2. first-session startup/resource-admission optimization (observed session bootstrap roughly 1.9–3.3 s in recent production runs);
3. bounded preconnect/warm session only if quota/session cleanup remains explicit;
4. reducing the first output 400 ms jitter reserve only after Android playback-underflow telemetry;
5. longer real provider GoAway/session-resumption acceptance.
