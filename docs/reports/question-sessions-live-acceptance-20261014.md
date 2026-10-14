# Question sessions: real acceptance checkpoint, 14 October 2026

## Outcome and limits

The real owner phone → Mira questions → saved answers → MCP continuation → dot analysis → verified GitHub result → original Slack thread path passed on release **0.1.62**, deployed SHA `b44a1f58c6fcf2de8b24b1fdd5ef96ca29c5b90a`.

**The final Mira notification initially failed at 07:40 UTC, then worked in the post-repair real phone session at 08:26–08:27 UTC.** Saved provider transcripts confirm a result announcement and, after the owner's request, a short substantive summary. Audio output and the matching personal cursor are recorded; the owner's 08:42 feedback was cautiously positive. Backend `mira_state=available` still means readable, not delivered/heard. The observed bounded happy-path cycle is verified; this is not exhaustive acceptance of every voice scenario. This report does not claim that the product requirements discussed in the note have been implemented.

No new deployment, question package, answer, grant, subscription or binding was created to prepare this report. Raw audio and answer transcripts are not reproduced here.

## Release evidence

- [PR167](https://github.com/onedayonemasterpiece/projects-hub/pull/167): durable question workflow and verified result delivery.
- [PR168](https://github.com/onedayonemasterpiece/projects-hub/pull/168): guarded explicit owner CLI question permission.
- [PR169](https://github.com/onedayonemasterpiece/projects-hub/pull/169): source provenance read contract and explicit SHA validation.
- [PR170](https://github.com/onedayonemasterpiece/projects-hub/pull/170): immediately executable question tools at Live startup, consent/reconnect handling.
- Final code candidate `f553446c27325ba8a6cc2dff83d4634b5632a424`: **416 backend tests passed**, then [CI38527941846](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/38527941846) passed backend and PWA. Independent read-only review found no remaining release blocker in the reviewed changes.
- The installer completed with exit0; independent health returned version0.1.62, the exact deployed SHA above, static/live/resource readiness true.
- Earlier public-edge real-provider WSS roundtrip passed on0.1.59. That earlier canary is not presented as phone acceptance of0.1.62.

### Failed phone attempt and repair

At07:07UTC on0.1.61, Mira offered four questions and then claimed system restrictions after consent. Session `live_5e5f4c8c32bd4db7a34d3c3df22d9fea` logs showed accepted audio/transcripts but no tool calls. No ACL refusal was established by the model's generic statement.

Core startup exposed only the pending count, leaving question access behind an uncalled capability switch. PR170 exposes the existing inbox and answer/session-control functions directly when the ACL-checked inbox has open questions, keeping the nine-tool limit and retaining the existing owner-development router. Actual accept/resume stores the received-offer marker; a restarted session can still read and continue unfinished questions.

Deterministic regression covers core initialization, inbox, accept, important-only, pause, new-session resume, answer, explicit completion and ACL revocation. It does not replace the subsequent real owner phone test.

### Failed final notification, 07:39–07:40 UTC

The owner reported the missing notice in [the original task thread](https://app.slack.com/archives/C0C872SJFA7/p1791963627219879?thread_ts=1791920569.001289&cid=C0C872SJFA7). Registered logs identify session `live_14ce9691171f45fc8c7f8b5d60099841`, 07:39:36–07:40:04 UTC, on client/backend 0.1.62 and the deployed SHA above. Audio/transcript events and completed turns were present; no result-reading tool call occurred in that session.

Code inspection found that core initialization supplies pending questions and owner-development completions, but not the completed question-analysis personal notification. With all questions completed, the pending count is zero and ordinary onboarding can win the greeting. Backend availability and a confirmed Slack receipt therefore did not establish a working startup notice. PR171 repaired the startup path and was deployed at 08:02 UTC; the subsequent real spoken check is recorded below. Existing answers, analysis and delivery receipts remain valid.

### Notification repair release, 08:02 UTC

- [PR171](https://github.com/onedayonemasterpiece/projects-hub/pull/171), candidate `c3e78c53d5751e4ecb2d506a626f3c62f296ca8a`, merged and deployed as `76bc5f2d84424f26bf3d0056687ae6a5dfcbde81`.
- 427 backend tests passed, with two existing dependency warnings; [CI38531834280](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/38531834280) passed. Independent read-only review approved the two-file change.
- Core startup now includes the existing bounded personal brief and validates current source/binding/project access for result notices. The existing brief_seen function fits the nine-tool limit alongside question controls.
- Initialization and reads do not acknowledge. A model acknowledgment requires observed provider output and a valid presented prefix; it records announced, while playback remains unverified. Any provider output is not semantic proof that the specific summary was spoken, and no state claims heard/delivered.
- The installer succeeded after fetching the new merge object; independent health confirmed exact deployed SHA, static/live/resource readiness. Version remains 0.1.62 for this backend-only repair.
- Post-deployment readback confirmed the same analysis/response, four owner receipts, backend stored, Mira available and Slack confirmed with the original message ID.
- Subsequent real check: the new session below announced the completed analysis before optional onboarding and gave a short substantive summary on request. Full-document spoken reading remains unverified.

### Real notification retry, 08:26–08:27 UTC

- Session `live_1b2d2c4cf1734337bf0d9d60fe35a193`, source `src_d32137a9a4f344cdbdc05e9d13988633`, ran 08:26:44–08:27:42 UTC on deployed SHA `76bc5f2d84424f26bf3d0056687ae6a5dfcbde81`, client/backend version0.1.62.
- Fixed-source, actor-scoped read-only SQLite queries (URI mode=ro, query_only=ON; no app instantiation) recovered the saved provider output. The first reply announced the completed collaboration analysis for Projects Hub and said the requirements were clarified.
- After the owner explicitly asked for the analysis information, Mira summarized the preserved personal feed/project switch, verified GitHub note save, and merging compatible edits. It referred to GitHub for more detail; it did not read the full result document or the remaining unresolved decisions.
- Registered turn-complete telemetry records19 output-audio events /304800bytes for the first announcement and46 events /762240bytes for the substantive summary. This proves generated output, not independent acoustic verification at the phone.
- The existing notification event47 is `question_result_ready` for analysis `ca_afaa230429de18c3f826a698cbe337e0`. The owner's personal cursor became47 at08:27:02.364UTC, after the first announcement. This corroborates model acknowledgment; no separately persisted command receipt or named tool-log entry was found for brief_seen.
- Fresh MCP readback retained response `mcp_response_064828c2163b3156a9b484a7c4a073ba`, the same four answer receipts, stored/available/confirmed states and original Slack message `1791963451.804759`.
- [Owner feedback at08:42:11UTC](https://app.slack.com/archives/C0C872SJFA7/p1791967331846669?thread_ts=1791920569.001289&cid=C0C872SJFA7): “Вроде бы отработало”. Treat this as cautious confirmation of this retry, not detailed acceptance of every scenario.
- No question package, answer, analysis, application deployment or test run was created for this read-only verification. Only the documentation checkpoint was updated.

## Authorized test scope

- Project: `prj_9a50a8e9e45a495387cfd6d7413e833e`.
- Source note: `note_275b783b7bf022167bbebcfcb275b3f8`, revision1.
- Source repository: `onedayonemasterpiece/projects-hub-notes`.
- Source path: `docs/notes/27/note-275b783b7bf022167bbebcfc.md`.
- Source blob SHA: `534dedc962dbd59fe915db1cf8b9d62ea5cd2254`.
- Binding: `mcp_binding_3d7213f4d5346ecfca68a126ff8f1ead`, revision2, active, questions_allowed=true.
- Original test destination: Slack workspace `T0C82495TK5`, channel `C0C872SJFA7`, root `1791913871.135149`.
- Content permission is restricted to that note/result and destination. Code remains in projects-hub; the notes repository is for project content.

## Real trace and authoritative readback

1. Question-start request `mcp_test_3f5ebe398e78872d6d88fa75990b5cf6`, event `evt_ba08508dcb6b80bd5eaf1fdb569998cc`, produced one four-question package:
   `ca_afaa230429de18c3f826a698cbe337e0`.
2. The initial request completed. Its later TTL expiry did not erase the durable analysis. Owner status readback confirmed the same package; no replacement request/package was created.
3. The owner answered the four questions in the real Mira phone flow and explicitly completed the session. Independent MCP readback confirms four receipts authored by `usr_ce285a30e91941f1bf9f7cf04627c267`:
   - `cmd_35eee9e20e9a7f214b3f773c3fad02cb8808f49c`
   - `cmd_beb9e694734833e39714919659cba0fbf20e7421`
   - `cmd_5705608ac4c8565b8b4ae79fd4626e020a240f0a`
   - `cmd_d1b11498e6d5bef28f011715e27a96ea076976da`
4. Consent receipt: `cmd_62e2ea39bb35e7abff4b5ae1322a0f1a65334c41`. Completion receipt: `cmd_523bc88b24d402cb798415198d05fdac5feda316`, completion_scope=full.
5. Actual questions_ready request `mcp_test_e813d143459a9b1d7ebe5aef241f0d86`, event `evt_b6bee5bf907dfa0cbd2cde9206e8a4f6`, reached dot. Dot saved a substantive analysis_result using all four receipts.
6. [Verified result](https://github.com/onedayonemasterpiece/projects-hub-notes/blob/92f4bcbd5146465d85996ea3fb4db0e0ddf3f283/docs/notes/27/note-275b783b7bf022167bbebcfc.question-result-ca_afaa230429de18c3f826a698cbe337e0.md):
   - Commit `92f4bcbd5146465d85996ea3fb4db0e0ddf3f283`.
   - Blob `973ea99f7f5acb4c61559ed71250ffe7b82578e8`.
   - Source-linked sibling Markdown, not an overwrite of the original note.
   - Independent exact-commit GitHub readback matched this blob.
7. Response `mcp_response_064828c2163b3156a9b484a7c4a073ba`; claim `claim_da1bbe62ab6f6d663b5626db9d6fd5e7`.
8. [Slack result message](https://app.slack.com/archives/C0C872SJFA7/p1791963451804759?thread_ts=1791913871.135149&cid=C0C872SJFA7), timestamp `1791963451.804759`, was sent to the original test root. The coordinating delivery verified the original message; independent backend readback confirms slack_state=confirmed with this message ID.
9. At07:38UTC independent MCP readback: request completed, package completed, backend_state=stored, mira_state=available, slack_state=confirmed. The subsequent real phone check failed: at 07:40:27 UTC the owner reported that Mira mentioned only the theme, not the completed analysis.

## What is and is not accepted

**Verified live:** actual owner voice answers and explicit completion, correlated MCP continuation, receipt-accounted analysis result, immutable GitHub result readback, original Slack claim/send/receipt, result readable by Mira.

**Verified deterministically, not exhaustively live:** stale source/binding/ACL rejection, idempotent replay, pause/filter/reconnect/partial completion, transactional request/outbox and restart recovery cases in the test suite.

**Still unverified:** full-document spoken reading; a full phone interruption/pause/restart scenario; prolonged idle/restart delivery adversity beyond the observed real event; all unrelated product behavior described in the resulting requirements analysis. The earlier final-notification failure was repaired and the bounded real retry passed as described below.

An answered question is a receipt, not proof that every product ambiguity is resolved. The result explicitly retains undecided GitHub/model outage behavior and other open design details.

## Read-only plan for the next analytical track

Source: [project-information-unified-access-20261014](https://github.com/onedayonemasterpiece/projects-hub/blob/7036e16eea6bebc0603e99588056d7a81dbb6f5b/docs/product/21-project-information-unified-access.md), revision1, document blob `a3d73920f029219acd2a64d9fe38f9839ee618f3`. Requested originating Slack root: `1791919589.989969`, distinct from the test root.

No source object, binding or question package for that track was created by this assessment.

### Required bounded steps

1. Finish the remaining Mira notification gate of the current test. Re-read the track document/current canonical revision and resolve its actual Hub project and owner/editor audience; do not infer these from the old test project.
2. Reuse or create one track-specific analytical anchor through the existing collaboration note service, with a short purpose and immutable source reference to document21. This anchor is project content; it must not become a second editable copy of the architectural document. Read back its own ID, revision, repository/path/SHA and project audience.
3. Verify the actual Slack workspace/channel/root and current owner authority for the new route. Require explicit bounded permission for recurring question/result processing of this track and exact output destination. Existing test consent cannot authorize this new binding.
4. Inspect whether that new note or Slack root is already bound. Current binding uniqueness is per(project,note) and per(workspace,channel,thread). Never rebind the old test note or move its destination. Create a new binding through the existing owner CLI/MCPBindings.bind with expected_revision=0; then enable questions explicitly on the unchanged new binding at its current revision, retaining audit/history.
5. Extend the existing automation's allowlist only after the new scope is authorized and read back. No new credentials, subscription or broad all-project permission is inherently required. Do not initiate a package until both source and route checks pass.
6. Start only the smallest priority package from document21 after the owner is ready; use its own IDs and receipt chain.

### Technical gate: canonical document publication

The current production workflow remains note-centric. start_question_session resolves a bound note; the result writer verifies the note's scoped project-content repository and writes a sibling result beside it. A project_document enum plus a client-supplied SHA is **not** a verified arbitrary-document route, and source_verified is not automatically true for non-note sources.

Document21 explicitly stays canonical in projects-hub/docs and calls for updating that same analytical document after answers. Merely making a new notes binding cannot guarantee that outcome. Before enabling this track, choose and authorize a narrow supported contract:

- Extend the existing source/gateway path to bind and verify the immutable document reference and its approved canonical result destination with expected revision/SHA, without adding a new framework; or
- Explicitly accept a bounded notes-side derived analysis plus a separately authorized, verified update of the canonical projects-hub document. Do not describe the notes-side save alone as the canonical document update.

This is an identified integration gap, not permission to copy document21 into notes, publish to another thread or use the old test binding. The exact new Hub project/note/binding IDs, current repository scopes and recurring workflow approval remain to be resolved.
