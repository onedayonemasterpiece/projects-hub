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

- `live-interaction` **0.2.6 candidate** exact commit
  `c9de297020087235d80ce4155e52f63f88c642bf` for both browser and Python;
  this exact pin is used until a matching versioned GitHub Release/tag exists;
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

The first DevCoveer deployment is **loopback only** and uses
`PROJECTS_HUB_DEV_AUTH=1` for product/browser acceptance. The session secret is
a separate private file; it is not committed or printed.

Do **not** expose this loopback dev-auth deployment as the public pilot. Public
access requires the product identity-provider/OIDC boundary from the product
specification. GitHub authentication is not the user identity system.

## Acceptance evidence

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
- Android application and Android offline/reboot queue;
- public IdP/OIDC;
- GitHub App installation/callback;
- long 3/10/30-minute buffered-source acceptance;
- multi-user/collaboration gates.

## Deployment

`deploy/devcoveer_install.py --sha <exact-sha>` is the owning deployment path.
It builds an exact release, creates the runtime venv, installs the two pinned
shared dependencies, builds the PWA, writes private runtime configuration,
atomically activates the release, restarts the user service and requires a
matching healthy `release_sha`. A failed health check restores the prior
`current` target and service environment when a prior release exists.
