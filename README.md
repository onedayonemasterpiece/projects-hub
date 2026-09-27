# Projects Hub

Voice-first project workspace built around **one central Live agent**. The current
implementation follows the product specification in [docs/product](docs/product/README.md)
and is the first working vertical, not the complete product.

## First vertical

The PWA is dark-first and uses the floating-islands UI: a compact context island,
an on-demand work island and a stable voice island. There is no chat-style transcript
feed and no free-text composer.

The backend is a small FastAPI modular monolith. A Live conversation is bound to
actor + workspace + conversation, while each project tool checks its target again.
Received PCM is fsynced to the durable source before it is queued to the shared
provider transport. Full provider input transcription is persisted before the bounded
UI event projection.

The central Gemini Live model owns semantic decisions and has typed deterministic tools
for project catalogue/focus and durable memory. The backend does not contain another
ASR, LLM router, classifier or summarizer.

Realtime microphone sessions keep provider automatic activity detection. Deliberate
buffered/offline audio sessions use the shared `live-interaction` manual
`activityStart → audio… → activityEnd` contract, so internal pauses do not terminate
the buffered turn early.

## Shared Live/runtime contracts

- `live-interaction` **v0.2.5** is pinned as an immutable browser release archive
  (`web/vendor/live-interaction-0.2.5.tgz`, SHA-256
  `0f6b8d11b98af14004669812a4512a399aecff7907154c091e5a95e951c9a232`)
  and as the same semantic version in the Python runtime.
- DevCoveer deployment pins `ai-resource-control` **0.1.7** at
  `51e9c043ce40dfefea8b2cb4f4956019819bd9d4`.
- Projects Hub uses only its `GOOGLE_API_KEY4` authority-outage fallback source alias;
  the shared SDK owns fallback policy. No provider credential reaches the browser.

The shared framework release has real-provider evidence for deliberate buffered
`activityStart → PCM → activityEnd` delivery. The deployed Projects Hub runtime also
has a real central-Live/function canary proving Gemini Live →
`conversation_set_focus` → deterministic backend readback → voice response.

On deployed release `69e3fe75799f425b3c930e10efd68eeba5cb87d2`, the real-audio
memory canary passes end to end: buffered PCM produces provider input transcription,
Gemini Live calls `memory_commit_voice_source`, durable source and project-memory
readback succeed, and the same Live session returns voice output and `turn_complete`.
A deliberate service restart then passes a second canary: a fresh Live session calls
`memory_read_project`, answers by voice, and the memory set stays unchanged (1 → 1,
same IDs/revisions). Physical browser/device microphone acceptance remains separate.

## DevCoveer runtime

`deploy/devcoveer_install.py --sha <exact-commit>` is the owning deployment path.
It materializes the exact Git revision under `/home/dev/.local/share/projects-hub/releases`,
builds its own venv and PWA, atomically moves `current`, writes private runtime
configuration, installs/restarts `projects-hub.service`, and requires `/healthz` to
read back the same deployment SHA. Durable state lives under
`/home/dev/.local/state/projects-hub`.

The first deployed pilot binds only to `127.0.0.1:8196`. Development login is accepted
only on a direct loopback Host and is rejected for forwarded/public requests. It must
not be exposed as the public authentication scheme; external access requires the
supported IdP/OIDC boundary from the product specification.

Operational logs are structured JSON in the user journal and in a bounded rotating
`/home/dev/.local/state/projects-hub/logs/backend.jsonl`. They contain IDs, statuses,
tool names and timings, not transcripts/audio/credentials. Runtime investigations use
the registered DevCoveer `backend` log alias and typed `log_search`, for example a
bounded search for `memory_commit_voice_source` or a conversation ID. No arbitrary
host-file grep is needed.

See [docs/runtime-devcoveer.md](docs/runtime-devcoveer.md) for the runtime contract.

## Verification

```sh
python -m pytest -q
cd web && npm ci --ignore-scripts --no-audit --no-fund && npm run build
```

Deterministic acceptance is kept separate from real-provider/device acceptance in
`docs/product/08-reliability.md`.

## Still outside this first vertical

Physical microphone acceptance on a real user device, client-side offline/restart-safe
capture before the server receives audio, Android, external IdP/OIDC, GitHub App
installation/callback, long 3/10/30-minute buffered-source product acceptance,
multi-user collaboration and the full release-gate corpus remain subsequent work.
The existing Record Idea Hub is not disabled until its replacement path is actually
accepted.
