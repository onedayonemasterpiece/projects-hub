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
user journal. Logs contain IDs, status, tool name and timing; they do not contain
audio, transcripts, provider credentials or session cookies.

## Shared Live dependencies

The consumer is pinned to:

- `live-interaction` merged 0.2.5 implementation:
  `2758d52fa77e04b21978f5bef18ccb5d776049da`;
- deployment-installed `ai-resource-control` 0.1.7:
  `51e9c043ce40dfefea8b2cb4f4956019819bd9d4`.

The product does not contain a second provider transport, ASR, semantic router,
classifier or summarizer. Server-side product tools are deterministic.

Provider credentials are selected from `/home/dev/.env` by the deploy
installer and copied to a private `0600` provider environment using an explicit
allowlist. Projects Hub may receive only its own `GOOGLE_API_KEY4` raw fallback
alias; other consumer fallback keys are never copied into its runtime.

## Service state and health

`GET /healthz` is content-free and reports storage readiness, installed shared
Live/resource packages, exact deployment SHA and auth mode. The DevCoveer
operator registry should expose the runtime through a `backend` alias for
service, journal logs and health.

The application source records are separate from the bounded Live event ring.
Audio accepted by the backend is fsynced before being queued to the provider.
The trusted full provider input transcript is written to the source journal
before any 2000-character UI projection.

## Authentication boundary of this first vertical

The first DevCoveer deployment is **loopback only** and uses
`PROJECTS_HUB_DEV_AUTH=1` for product/browser acceptance. The session secret is
a separate private file; it is not committed or printed.

Do **not** expose this loopback dev-auth deployment as the public pilot. Public
access requires the product identity-provider/OIDC boundary from the product
specification. GitHub authentication is not the user identity system.

## What this vertical proves

Implemented paths:

- dark-first floating-islands PWA shell;
- signed server session and workspace bootstrap;
- actor/workspace/conversation-scoped central Live session;
- shared microphone transport through `live-interaction`;
- accessible-project catalogue and model-owned focus changes;
- durable received PCM source;
- lossless trusted provider transcript journal;
- `memory_commit_voice_source`, project memory read and ephemeral disposition;
- deterministic command identity and exact Markdown readback;
- structured operational logging and health.

Not yet claimed by this vertical:

- client-side offline durable queue / restart recovery before the server receives
  audio;
- physical microphone acceptance on the user's own browser/device;
- Android application;
- public IdP/OIDC;
- GitHub App installation/callback;
- long 3/10/30 minute buffered source product acceptance;
- multi-user/collaboration gates.

The shared framework's 0.2.5 PR has separate real-provider evidence for deliberate
buffered raw audio with manual activity boundaries. That evidence does not by
itself make the Projects Hub physical-microphone gate pass.

## Deployment

`deploy/devcoveer_install.py --sha <exact-sha>` is the owning deployment path.
It builds an exact release, creates the runtime venv, installs the two pinned
shared dependencies, builds the PWA, writes private runtime configuration,
atomically activates the release, restarts the user service and requires a
matching healthy `release_sha`. A failed health check restores the prior
`current` target and service environment when a prior release exists.
