# Owner development: backlog-first orchestration

Status: existing owner-development contract; collaboration continuation amendment added 2026-10-06. Earlier implementation/version evidence is historical. [Current collaboration design](20-basic-collaboration-and-personal-timeline.md) and [implementation task](../prompts/basic-collaboration-prototype-20261006.md) define the next slice. PR #87 remains paused.

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
   - run tests and the project's **existing** CI, publish its candidate PR and inspect real evidence;
   - prepare a reviewable result, but do not merge/deploy before review; no second CI controller is created.
   - If the owner profile is unavailable, execution fails closed and asks the owner to choose an exact available native model/effort. No silent substitution.

3. **Independent review / acceptance** — same quality thread, `gpt-6-astra high`
   - review source, tests, docs, acceptance evidence and regressions;
   - return exactly `ACCEPTED` or `REWORK_REQUIRED`.

4. **Rework** — same implementation thread
   - review findings go back to implementation;
   - retest;
   - return to independent review;
   - bounded review/rework cycles, currently maximum 2 (not the previous code's drift to 12).

5. **Delivery**
   - only after accepted review;
   - Native Codex in the implementation thread receives a delivery continuation and performs the **existing** merge/CI/deploy/release process itself;
   - Codex verifies the live/published result and reports actual CI and delivery evidence. The durable worker only records the terminal receipt, not an independent CI/deploy system;
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
- one owner development execution runs at a time; new accepted requests may wait durably in a bounded queue instead of being discarded because the worker is occupied.

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

## 6 October amendment: questions and durable continuation

An execution that needs a human decision emits typed addressed questions with the evidence/context, blocking dependency and allowed continuation. `needs_owner` must not be a dead-end Markdown status. The owner can answer, say unknown, skip or defer; these outcomes never fabricate an approval. A sufficient answer resumes the already authorized scope exactly once. A materially new implementation goal or changed authorization still requires an explicit owner command. Ordinary note discussion, accepting an analysis recommendation and creating a backlog item do not authorize implementation.

Execution progress belongs to a bounded durable backend worker, independent of browser polling and Live session lifetime. `status` is read-only. Answer plus continuation intent are persisted together; retries/restarts use leases, idempotency keys and external receipt/readback. The MCP connection worker introduced in PR #114/#115 is transport lifecycle management and must be preserved; it does not replace durable product orchestration.

Keep the existing design/implementation/review/rework/delivery path above and the reviewed model profiles. The separate Codex consultation ladder in [20](20-basic-collaboration-and-personal-timeline.md) does not silently replace the owner quality pipeline. The nearest prototype includes one actual resumed owner execution plus ordinary project collaboration; it does not build a generic workflow designer.

## 8 October clarification: native Codex delivery and owner results

Tasks explicitly launched through Mira use **Native Codex only**. The
`gpt-6-astra high` quality thread and the implementation thread stay distinct;
design, implementation, independent review, bounded rework and delivery are
mandatory. Mira never treats a discussion or backlog creation as launch
authorization. New runs use Codex to publish the candidate, run the project's
existing CI and deliver after accepted review. There is no parallel
Projects Hub CI controller. Existing pre-upgrade runs retain their previous
recovery route to preserve in-flight work. Each target project controls its
own CI, artifacts and release steps; the orchestrator remains cross-project.

The owner can inspect the latest run independently of an open Live connection
and see completed tasks across all projects from the past seven days by default.
Completed work is presented at app re-entry and passed to the same Live Mira
for a natural greeting. The Android native client shows a system completion
notification via a battery-friendly persisted OS job using its already paired
device credential, nominally every 15 minutes (OS delays possible). This is
not instant FCM push; it is a minimal first-party notification path without a
new vendor/backend. Notifications never start the microphone, and only a real
signed Android version bump triggers an update prompt.


## 9 October: direct owner voice tools at startup

Physical Samsung acceptance after PR #148 showed a repeated false inability to
create development tasks and change theme. The Live model understood and answered
the requests but did not invoke the progressive router. The product fix is to
expose the most frequently used tools directly in the **initial** Live function
bundle, still capped at nine provider declarations. Theme read/set are available
for signed-in users; backlog list/create/execute are included only for verified
platform + workspace owners. Every call rechecks current owner authorization,
and creating a backlog item is not development authorization: executing it still
requires the owner's explicit instruction, backend quota/model checks and the
existing durable two-thread Native Codex pipeline.

The other domain capabilities remain progressive. This is not a second semantic
router, a new scheduler or a bypass of the AI resource authority. Provider-backed
and Samsung physical acceptance remain separately required.

## 11 October: owner-check tracking on existing task results

Status: owner-approved design addition, not an implemented task/UI capability.
This applies to results produced by a person, Mira/ChatGPT, direct MCP work or a
coding agent. It does not require the owner-development pipeline above.

Keep released results that the owner has not personally checked discoverable on
the existing task, with an optional feature reference. Reuse result metadata when
sufficient; do not introduce a second backlog, QA entity or mandatory task type.

The small owner-check record identifies:
- the concrete result/scenario and its task/optional feature reference;
- release version, commit or artifact link;
- state: `unknown`, `not_checked`, `confirmed` or `issue`;
- the exact scope checked, the reporting person, time and source message reference;
- a link to short verification steps and the expected result in an existing
  GitHub result/specification document.

`unknown` means there is no reliable report of a personal check; silence does
not prove that the owner never tried it. `not_checked` records explicit knowledge
that the scenario has not been checked. `confirmed` requires the owner's explicit
report about that scenario. `issue` records a reported difficulty and does not
invent its technical cause.

Engineering checks, release delivery and owner confirmation remain separate.
One scenario's confirmation never accepts every feature in the release. A task
may be done for its agreed delivered outcome and still appear in the owner-check
list. Owner acceptance blocks that task only when it was explicitly part of its
agreed outcome; it is not a new gate on every release.

The model should associate delivery and later owner feedback with the same
result, keeping the source and scope. Proposed queries include “what is already
released but I have not checked?” and filtering by project or version. Include
both `unknown` and `not_checked`, even for completed tasks; show reported issues
separately. This list must not disappear with the existing seven-day completion
view. Group technical commits into meaningful user scenarios.

Keep historical confirmation tied to its checked candidate. Do not silently
transfer it to a changed result, or require every unchanged scenario to be
retested after every release. Prefer the current available result for a later
check while retaining the old evidence. No automatic reminders or new mandatory
review process are implied.

Document bodies and test instructions stay in GitHub. The database stores
references, versions and check metadata, not a second editable specification.
A concrete scoped example is recorded in the
[calendar recovery incident](../incidents/2026-10-11-calendar-live-recovery.md#owner-check-on-11-october-2026).
