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

- `live-interaction` **0.3.7-rc.1** is pinned to exact commit `b6a051a7cf53f84433ebf48a52b623d91fcc6478` for the WSS migration candidate.
  to exact commit `c9de297020087235d80ce4155e52f63f88c642bf`. The commit contains the
  shared durability-first browser capture primitive; it remains an exact commit pin
  until the corresponding GitHub Release/tag is published.
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
same IDs/revisions).

Deployed release `f4d395ef86eaf4048308e9175775bc5e2bcb188f` adds the PWA offline
source queue. Accepted offline PCM is written to IndexedDB through the shared
`createDurableMicrophoneCapture` path, not a Projects Hub microphone/VAD fork. A real
offline-retry canary proves partial first delivery → same `client_source_id` retry →
same server source → reset of only the incomplete copy → full buffered replay →
provider transcript → one memory object → voice response and `turn_complete`.

Physical browser/device microphone acceptance remains separate.

Deployed release `c179ac0d00df3a3600df744c836a847bd3744bb0` extends the same
voice path to mixed-project utterances. A real buffered-audio canary produced one
private source, provider input transcription, two confirmed
`memory_commit_voice_source` calls, and exactly two project memories — one for
Projects Hub and one for Wonderful Lections — followed by voice output and
`turn_complete`. Project-memory Markdown no longer contains the full provider
transcript; the transcript is archived once under actor-private `sources/` storage.
Legacy one-source/one-memory data migrates in place and passed post-migration Live
readback.

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

Physical microphone acceptance on a real user device, Android offline/reboot capture,
external IdP/OIDC, GitHub App installation/callback, long 3/10/30-minute buffered-source
product acceptance, multi-user collaboration and the full release-gate corpus remain
subsequent work. The PWA foreground offline queue is implemented; its mid-start online
RAM handoff is not yet claimed crash-durable until the shared framework extends
durability into that startup window.
The existing Record Idea Hub is not disabled until its replacement path is actually
accepted.
