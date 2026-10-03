# Projects Hub — DevCoveer runtime

This document describes the first deployable product vertical. It is runtime
documentation, not a claim that the complete product specification is accepted.

## Release shape

The backend is a single Python ASGI process bound to loopback port **8196**.
Each deployment is an exact Git SHA under
`/home/dev/.local/share/projects-hub/releases/<sha>`; the
`current` symlink moves only after the release venv and PWA build succeed.

Durable application state for this single-host vertical lives under
`/home/dev/.local/state/projects-hub`. SQLite runs in WAL mode with FULL
synchronous writes. This is intentionally behind `DurableStore`; PostgreSQL is
the target before a multi-instance rollout, not a prerequisite for proving the
first vertical.

The service is `projects-hub.service`. Normal output is structured JSON to the
user journal and to the bounded rotating
`/home/dev/.local/state/projects-hub/logs/backend.jsonl`. Logs contain IDs,
status, tool name and timing; they do not contain audio, transcripts, provider
credentials or session cookies.

DevCoveer registers the runtime under the `backend` aliases for service, health
and logs. Use typed `logs`/`log_search` for investigations. The latter is the
bounded equivalent of grep over operational evidence and supports query, time
window, result limit and small context without arbitrary host-file access.

## Shared Live dependencies

The consumer is pinned to:

- `live-interaction` **0.3.8** via stable release tag `v0.3.8` for browser and Python; the tag resolves to release commit `f756a90f864bee16a671b54da7a53189e2e4a94e`, and the published release asset SHA-256 is `99b8a4ea7c04a81062547a6a63b8e161220fb62a3b6d947ddda1954c2a90ab0`. Version/tag is the dependency interface; commit/digest are verification evidence. Every framework update still requires consumer-specific acceptance;
- deployment-installed `ai-resource-control` 0.1.7:
  `51e9c043ce40dfefea8b2cb4f4956019819bd9d4`.

The product does not contain a second provider transport, ASR, semantic router,
classifier or summarizer. Server-side product tools are deterministic.

Provider credentials are selected from `/home/dev/.env` by the deploy
installer and copied to a private `0600` provider environment using an explicit
allowlist. Projects Hub may receive only its own `GOOGLE_API_KEY4` raw fallback
alias; other consumer fallback keys are never copied into its runtime.

## Realtime and buffered audio

Normal PWA microphone sessions use `audio_mode=realtime` and provider automatic
activity detection.

Deliberate buffered/offline turns use `audio_mode=buffered`. The product adapter
then sets `manual_activity_detection=true` in the shared framework and accepts only
the explicit sequence:

`activity_start → one or more PCM16/16k audio chunks → activity_end`.

In that mode `audio_stream_end` is rejected by the shared host. Received PCM is
still fsynced by the product adapter before the shared host queues it to Gemini Live.

The trusted full provider input transcript is written to the source journal before
any bounded 2000-character UI projection.

## Service state and health

`GET /healthz` is content-free and reports storage readiness, installed shared
Live/resource packages, exact deployment SHA and auth mode.

The application source records are separate from the bounded Live event ring.
A durable memory commit is not considered successful until the generated Markdown
has been written and read back with the expected SHA-256.

## Authentication boundary of this first vertical

The current DevCoveer runtime keeps loopback dev-auth for bounded local canaries
and also has the public Yandex/Supabase identity boundary configured. Health reports
`auth_mode=public_yandex+loopback_dev` and the fixed public origin is
`https://projects-hub.kenigevents.ru`.

This does **not** mean the public pilot is reachable yet. The bounded edge publisher
currently fails before DNS mutation because the installed Yandex Cloud CLI lacks a
working non-interactive credential. Until that owner-controlled credential is restored
and the publisher passes DNS/TLS/HTTPS readback, the public edge remains blocked.
GitHub authentication is still not the user identity system.

## Acceptance evidence

### Current WSS runtime · 3 October 2026

- deployed exact release: `720f44d8771043804a5e6e4fe6cb56e38790d79b` with `live-interaction v0.3.8`;
- `projects-hub.service` is active/running with zero restart count after rollout; health returns the same release SHA;
- source/integration acceptance: **7 WSS tests PASS**, **104 backend tests PASS**, clean `npm ci` + PWA production build PASS;
- deployed real-provider WSS roundtrip PASS: `hello_ack`, `configuration_ready`, binary PCM ingress + `audio_ack`, HTTP input rejection with `LIVE_TRANSPORT_MISMATCH` after WSS attach, provider `output_transcript`, binary output audio, `turn_complete`, and server-driven close after Stop;
- deployed concurrency canary PASS: two real Gemini Live provider sessions for the same actor coexist and both complete WSS handshake/close; a third concurrent session is rejected with HTTP 429 / `LIVE_BUSY`, proving the per-actor fairness boundary;
- deterministic regression proves four simultaneous actors can each hold an authenticated WSS handshake, cross-actor socket-ticket renewal stays hidden, and the fifth session hits the global capacity boundary; a 3+ independent-user real-provider production soak is still pending;
- the first ad-hoc runtime canary produced one `LIVE_SOCKET_IO` only because the test client closed immediately after Stop; the corrected canary waits for server close and subsequent roundtrip/concurrency runs produced no new `socket_failed` evidence;
- public edge WSS is **blocked before TLS/WebSocket acceptance** because `projects-hub.kenigevents.ru` currently does not resolve in DNS from DevCoveer; DNS/TLS/HTTP probes all fail at name resolution;
- durable operational check is `scripts/devcoveer_wss_canary.py`: run `--mode roundtrip` and then `--mode concurrency` sequentially under the loopback dev actor. The two modes must not be run concurrently because the roundtrip itself occupies one of the actor's bounded Live slots.

Current Android/MVP evidence on 28 September 2026:

- deployed runtime release: `a6c650e2f0944884dd01b57524ab77211e8db0aa`; repository `main` later advanced through docs/test-only commits;
- backend deployment health PASS, service active/running with zero restarts;
- post-deploy real Gemini Live function canary PASS with backend readback, voice output and `turn_complete`;
- Android build + unit PASS and Android emulator install + `MainActivity` launch PASS;
- signed GitHub Releases `android-v1` through `android-v5`; v5 is 0.1.5 and the signed release workflow passed `apksigner verify`;
- Android updater fetches the latest release manifest, requires increasing versionCode, retries transient network failures, rechecks on resume, verifies APK SHA-256 and delegates installation to the Android package installer; hosted Android 14 handoff PASS proved `android-v4 → android-v5`, update availability/button readiness, verified download, Package Installer handoff, same-signature in-place `versionCode 4→5` and preserved UID; only the final human installer confirmation remains a physical gate;
- device-command backend and typed receipts are implemented with claim/digest/session binding and Calendar Provider readback semantics;
- event-readiness cards, `generic`/`podcast` checklists and follow-up task lifecycle are implemented;
- backend suite after readiness: 80 pytest PASS; PWA production build PASS;
- physical Android microphone/calendar acceptance remains `not_run` because no physical ADB device is connected to DevCoveer;
- production GitHub App code exists, but runtime health reports `github_app_configured=false`;
- public edge remains BLOCKED by Yandex Cloud DNS authentication before any DNS/TLS mutation.

Implemented and deterministically covered:

- dark-first floating-islands PWA shell;
- signed server session and workspace bootstrap;
- actor/workspace/conversation-scoped central Live session;
- shared realtime microphone transport wiring;
- shared manual-activity buffered turn wiring;
- accessible-project catalogue and model-owned focus changes;
- durable received PCM source;
- lossless trusted provider transcript journal;
- `memory_commit_voice_source`, project memory read and ephemeral disposition;
- deterministic command identity and exact Markdown readback;
- structured operational logging, health and typed log search.

Deployed provider acceptance already proves a real central Gemini Live session can
call `conversation_set_focus`, receive the deterministic function result, continue
with voice output and complete the turn.

The real-audio memory canary is `scripts/devcoveer_voice_memory_canary.py`. On
release `69e3fe75799f425b3c930e10efd68eeba5cb87d2` it passed with provider input
transcription, `memory_commit_voice_source`, durable source metadata, project-memory
readback, voice/output response and `turn_complete`. The accepted source contained
392508 bytes across 62 PCM chunks and produced transcript revision 1 / memory revision 1.

A deliberate `projects-hub.service` restart then changed the process and
`scripts/devcoveer_restart_recall_canary.py` passed in a fresh Live session:
`memory_read_project` returned successfully, voice output completed, and the memory
set remained unchanged (1 before, 1 after, same IDs/revisions). No source replay or
duplicate memory write was needed.

On deployed release `f4d395ef86eaf4048308e9175775bc5e2bcb188f`, the offline retry
canary passes as well. The first buffered attempt persists only part of the source and
stops. A second attempt with the same stable `client_source_id` reuses the same server
source, clears only its incomplete server copy, replays the full durable PCM, receives
provider input transcription, performs `memory_commit_voice_source`, produces exactly
one memory object, speaks the answer and reaches `turn_complete`.

The PWA foreground offline queue stores accepted shared-capture PCM in IndexedDB with
per-chunk SHA-256. Interrupted local `capturing/replaying` state is recovered on app
startup, and local PCM is removed only after terminal server disposition. The product
does not contain its own `getUserMedia`, AudioContext/VAD or WebSocket transport.

On release `c179ac0d00df3a3600df744c836a847bd3744bb0`, a real-audio
mixed-project canary passed in one central Live turn: one private voice source produced
provider input transcription, two successful `memory_commit_voice_source` calls,
two distinct memory objects targeted to Projects Hub and Wonderful Lections, voice
output and `turn_complete`. No routing model or project classifier was added.

The same deployment migrated existing legacy memory rows. A post-migration recall
canary read the existing memory through `memory_read_project`, kept the memory set
unchanged (1 → 1), and completed the voice turn. Project-memory Markdown is now
semantic-only; full provider transcription lives once in the actor-private source
archive and is referenced by digest.

All synthetic-audio canaries explicitly keep
`physical_microphone_acceptance=not_run`; deterministic PCM is not a substitute for a
real browser/device microphone gate.

Not yet claimed:

- physical microphone acceptance on the user's actual browser/device;
- crash-durable online startup capture while the Live session POST itself is pending;
- physical Android calendar/microphone acceptance and Android offline/reboot queue;
- public DNS/TLS/WSS edge acceptance for the configured Yandex IdP boundary; current blocker is unresolved DNS, not an application WebSocket failure;
- production GitHub App registration/credentials plus installation/callback acceptance;
- long 3/10/30-minute buffered-source acceptance;
- live multi-actor / 4+ actor soak, same-conversation multi-device takeover, and broader collaboration gates.

## Deployment

`deploy/devcoveer_install.py --sha <exact-sha>` is the owning deployment path.
It builds an exact release, creates the runtime venv, installs the two pinned
shared dependencies, builds the PWA, writes private runtime configuration,
atomically activates the release, restarts the user service and requires a
matching healthy `release_sha`. A failed health check restores the prior
`current` target and service environment when a prior release exists.


## WSS runtime status · 3 October 2026

The single DevCoveer backend **is WSS-migrated as a deployed candidate** at exact Projects Hub release `720f44d8771043804a5e6e4fe6cb56e38790d79b` and now runs the versioned `live-interaction v0.3.8` consumer. Local acceptance is 104 pytest plus clean `npm ci`/PWA build; post-deploy real-provider roundtrip and two-session concurrency both PASS, with no new `socket_failed`. HTTP remains bootstrap/auth/control and is not a silent media fallback after WSS attach.

What is accepted on the deployed loopback backend:
1. provider-ready session bootstrap;
2. one-use ticket/subprotocol handshake and `hello_ack`;
3. binary PCM ingress + relay ACK;
4. pushed provider transcript/audio/events;
5. HTTP media fallback rejection after WSS attach;
6. graceful Stop/server close;
7. two simultaneous real provider sessions for one actor plus deterministic per-actor admission of the third request;
8. deterministic four-actor WSS handshake/cross-ticket isolation/global capacity plus duplicate buffered-source exclusion.

What is **not** accepted yet:
- public `wss://projects-hub.kenigevents.ru` Upgrade, because DNS does not currently resolve;
- 20-socket/30-minute deterministic soak and 3+ independent-user real-provider acceptance;
- native Android WSS/capture edge and physical microphone/noise/poor-network acceptance;
- post-WSS physical Android in-app update;
- Regional Knowledge delegated OAuth + `knowledge_search` E2E. The Projects Hub adapter/tool contract is implemented fail-closed and the full backend suite is 104 PASS, but the Regional Knowledge project explicitly has no real Supabase/OAuth resource deployment yet, so the tool remains absent from normal Live sessions until an actor/workspace-bound delegated provider exists.

Until public DNS is restored, the correct next transport task is the edge/DNS publication gate, not another rewrite of the WSS runtime.
