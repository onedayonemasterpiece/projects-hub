# Owner development: backlog-first orchestration

Status: implementation contract for Projects Hub 0.1.20.

## Product invariant

Backlog is the primary durable work queue. Self-development is only one execution route for existing backlog tasks.

A development task may be:
- created/refined by Mira and left in backlog;
- implemented manually from ChatGPT or Codex;
- launched by the explicit platform owner through Mira;
- launched as a bounded batch of 1–5 tasks from the same Projects Hub project.

Creating, discussing, prioritizing or accepting a backlog item never authorizes execution.

## Authorization

Owner-development tools are exposed only when the current actor is both:
- the explicit platform owner;
- owner of the current workspace.

Backend rechecks authorization on every operation. Ordinary users do not receive these Live tools.

## Execution pipeline

One execution is linked to one or more existing `tasks(kind=development)`.

1. **Design / specification** — `gpt-6-astra high`
   - inspect current project/repository and relevant reusable solutions;
   - identify edge cases and failure modes;
   - define test cases, DoD and required browser/emulator/live acceptance;
   - update/prepare project documentation;
   - write the complete implementation brief to `docs/prompts/owner-development-<execution>.md`;
   - do not implement.

2. **Implementation / debugging** — default owner profile `gpt-6.1-medium`
   - implement the approved brief;
   - add/update tests;
   - debug locally;
   - run required browser/emulator checks;
   - prepare a reviewable result, but do not merge/deploy before review.
   - If the owner profile is unavailable, execution fails closed and asks the owner to choose an exact available native model/effort. No silent substitution.

3. **Independent review / acceptance** — same quality thread, `gpt-6-astra high`
   - review source, tests, docs, acceptance evidence and regressions;
   - return exactly `ACCEPTED` or `REWORK_REQUIRED`.

4. **Rework** — same implementation thread
   - review findings go back to implementation;
   - retest;
   - return to independent review;
   - bounded review/rework cycles, currently maximum 2.

5. **Delivery**
   - only after accepted review;
   - implementation thread performs normal project merge/publish/test/deploy/release path;
   - backlog tasks become done only after terminal successful delivery.

Two long-lived execution threads are intentionally reused: one quality thread and one implementation thread. This preserves context/cache and avoids spawning a new model session for every phase.

## Status model

Execution has a technical status and a product phase.

Technical status:
- `starting`
- `running`
- `blocked`
- `completed`
- `failed`
- `cancelled`

Product phases include:
- `queued`
- `designing`
- `implementing`
- `testing`
- `ci`
- `reviewing`
- `reworking`
- `delivering`
- `publishing`
- `releasing`
- `deploying`
- `capacity_wait`
- `needs_owner`
- `ready`
- `failed`

The UI must show the human-readable phase, current model/effort, review cycle, result/error and stage history rather than only a generic `running`.

## Codex capacity

Before each native Codex start/continuation Projects Hub reads DevCoveer `codex_status`.

Rules:
- the canonical Codex quota bucket must be available;
- effective remaining capacity must be strictly greater than 10%;
- unknown quota blocks new inference;
- model and reasoning effort must exist in the live native catalogue;
- no paid/API-key fallback;
- one owner development execution is active at a time.

## Usage accounting

DevCoveer listens to native Codex `thread/tokenUsage/updated` and exposes the current turn's `tokenUsage.last` through `read_task.latestTurn.tokenUsage`.

Projects Hub stores token usage on each execution stage:
- input tokens;
- cached input tokens;
- output tokens;
- reasoning output tokens;
- total tokens.

The execution view aggregates totals by model. Missing usage is unknown, never treated as zero.

## Cross-project routing

The mechanism is not specific to Projects Hub. Each development backlog task carries a Projects Hub `project_id`.

The execution resolves the project through its connected repository/project binding and sends that project hint to DevCoveer. All selected tasks in one execution must belong to the same project.

## Android update result

The execution delivery phase uses each project's normal release path.

For Projects Hub Android changes:
- normal GitHub Actions builds the signed APK/update manifest when `android/**` changes;
- when development execution reaches terminal success, the PWA asks the installed Android shell to check for updates immediately;
- Android verifies the release manifest/APK and shows the normal visible update dialog when a newer signed versionCode exists.

Backend/PWA-only changes do not manufacture an empty APK release.
