# Voice R7 — Google-style Hybrid VAD acceptance

**Date:** 2026-10-05
**Product target:** Projects Hub 0.1.34
**Shared Live target:** live-interaction 0.3.25 @ `4d5589bfc52ca74b2fc451e5a27788b42196823a`

## Goal

Reduce speech-to-reply latency without reintroducing premature turn splitting, parasite turns from short noises, or a second semantic ASR/LLM path.

Transcribe Live is explicitly out of scope for R7. Mira remains the only semantic conversational agent and still receives microphone audio directly.

## Google guidance used

Current Gemini Live documentation recommends Hybrid VAD when the client can detect speech end:

- keep automatic provider VAD enabled so the provider detects speech start and keeps prefix padding;
- let client-side VAD detect end of speech and send `audio_stream_end`;
- provider auto-VAD remains a fallback if the client misses the end;
- client endpointing should use at least about 500 ms of silence; 100–200 ms is too aggressive for natural pauses;
- realtime audio should be streamed in small 20–40 ms chunks.

References:
- https://ai.google.dev/gemini-api/docs/live-api/capabilities
- https://ai.google.dev/gemini-api/docs/live-api/best-practices

## Previous R6 behavior

Realtime Projects Hub disabled provider VAD and used explicit client `activity_start/activity_end`.

The client waited 2000 ms of silence before ending a normal utterance. Production v33 evidence showed that once the final user transcript reached Gemini, the first output transcript could arrive in roughly 0.7 s, so a material part of the perceived >5 s first-turn delay was local/session lifecycle rather than model generation.

The 180 ms speech-onset admission successfully prevented keyboard/finger-snap impulses from creating semantic user turns in physical v33 acceptance. It is preserved.

## R7 behavior

### Realtime

Provider automatic VAD is enabled:

- start sensitivity: `START_SENSITIVITY_LOW`;
- end sensitivity: `END_SENSITIVITY_LOW`;
- provider fallback silence: 1600 ms;
- prefix padding: 250 ms.

Client-side endpointing:

- sustained speech onset admission: 180 ms;
- ordinary end silence: 650 ms;
- after 8 s of admitted speech, end silence: 1400 ms;
- local end emits `audio_stream_end`, not manual `activity_end`.

The 1600 ms provider silence is deliberately a conservative fallback rather than the normal endpoint. The expected normal short-turn endpoint is the 650 ms client boundary.

### Buffered/recovery audio

Buffered and recovery sessions retain the existing explicit/manual boundary contract. R7 does not change offline replay semantics.

### Transport

Shared browser Live batching changes from 80 ms to 40 ms.

The shared resource-control layer was audited before accepting this change: PCM admission uses bulk token grants, so 40 ms payloads do not cause one central grant RPC per audio chunk.

## Observability

Projects Hub now logs the existing non-content `input_timing` provider event with:

- `audio_chunks`;
- `max_stdin_delay_ms`;
- `max_ws_send_ms`;
- `audio_stream_end_sent_at` / `activity_end_sent_at`.

No microphone bytes or transcript text are added to structured diagnostics.

## Deterministic acceptance

Shared live-interaction:
- Node: 88/88 pass;
- Python: 53/53 pass;
- native GitHub job: pass.

Projects Hub:
- backend: 141/141 pass against shared 0.3.25 source;
- WSS contract: 12/12 pass;
- PWA Live/release tests: 28/28 pass;
- production Vite build: pass;
- browser and Python dependencies pin the exact merged shared SHA.

An initially hanging WSS contract was traced to the test still sending obsolete manual `activity_start/activity_end`. The product correctly rejected that message in hybrid mode. The test was updated to the real R7 wire contract: PCM followed by `audio_stream_end`.

## Physical / real-provider acceptance still required

After deployment, measure:

1. short phrase such as “Мира, привет”;
2. several natural 5–15 s turns;
3. a long monologue with 0.5–1.2 s thinking pauses;
4. deliberate >1.5 s pause to confirm a long turn eventually closes;
5. barge-in while Mira is speaking;
6. keyboard click/finger snap/noise to confirm R4/R6 impulse rejection did not regress.

Use production `input_timing`, final input transcript, first output/audio timestamps and provider `interrupted` events to distinguish endpoint latency from model/provider latency.

## Deferred optimizations

Do not combine these with R7 before physical acceptance:

1. First-session startup/resource-admission latency. Recent production start was about 1.96 s and is the next likely major bottleneck.
2. Bounded warm/preconnect when entering voice UI, only if it can preserve quotas and detached-session cleanup.
3. Reduce the first output jitter reserve from 400 ms only after Android underflow telemetry proves it is safe.
4. Longer real-provider session-resumption/GoAway canary.
5. Transcribe Live as a later UI-only interim-caption sidecar. It must not replace the audio path into Mira.
