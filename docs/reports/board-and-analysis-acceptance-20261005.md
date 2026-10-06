# PH-BOARD-ANALYTICS-2026-10-05-R1 — combined acceptance checkpoint

Date: 2026-10-05  
Branch: `docs/board-analysis-20261005`  
Acceptance source SHA: `b7999b9df320a1cdb0cf330b9558d8b79ac5e291`  
Merged voice baseline: `c2ee3ed6ec20753c73670d80954adf711e2f0903` (Projects Hub 0.1.36)  
Tracking: issue #86, draft PR #87.

> **Контракт обновлён 06.10.2026.** Этот файл сохраняет evidence конкретного checkpoint `b7999b9…`; он не является текущей финальной приёмкой после закрепления `CORE-BOARD-VOICE-LLM-FIRST`. Ручной create/edit/drag/resize на доске больше не считается функцией продукта и не может использоваться как положительное acceptance evidence. Нормальный writer — только Mira Live tool path. Также устарела старая формулировка про пользовательское подтверждение «бюджета» NVIDIA: `council_pro` должен запускаться автоматически под bounded dual-slot admission (Kimi K3 + DeepSeek V4.1 Flash), а третий concurrent NVIDIA inference ждёт capacity.

## Decision

The source candidate is **not declared fully accepted/releasable yet**. It is historical evidence for the server-authoritative shared board, isolated analysis path, report storage/materialization, seven-day guest/share path, Android share capability and the then-current voice baseline. It does **not** prove the later voice-first/Mira-only UI contract, and PR #87 must remain draft until that contract and the remaining real gates are accepted.

No voice-owned R2–R7/current fixes were rolled back: the candidate contains the current main baseline through `c2ee3ed6…` as a merge parent, including adaptive endpointing / detached-WSS cleanup, while retaining board/analytics/share integration.

## Source result

Implemented in the candidate:

- one project board with Pixi/WebGL renderer, grid, pan/zoom/pinch, colored sticky notes, server revisions and history; the checkpoint still exposed manual edit/move/resize/delete UI, which is now a **known contract mismatch to remove**, not a retained product feature;
- same Mira Live session exposes typed `board_navigate`, `board_edit`, `board_analysis` and `board_share`; UI commands are scoped to project and per-browsing-context client instance;
- search/focus uses server search and a client-local camera/focus ACK; two tabs do not share one clientInstanceId;
- board WSS has one-use ticket/origin checks, snapshot/event sequencing, bounded peers and deterministic command IDs/revision conflicts;
- AnalysisRun stores frozen board-object revisions, durable request key/provider task id, Markdown, structured result, cancellation/error/source-change state;
- single-model Kimi/DeepSeek uses DevCoveer `provided_only` evidence mode with no ambient project context and durable dispatch/readback;
- free council uses the live OpenCode catalog and exactly two current free participants; it fails closed with no NVIDIA fallback when OpenCode rejects the execution mode;
- the old two-step NVIDIA confirmation path in this checkpoint is superseded: current policy is automatic bounded `council_pro` with Kimi K3 + DeepSeek V4.1 Flash, no user budget-confirmation, two distinct NVIDIA credential slots and capacity waiting for a third concurrent inference;
- Markdown is rendered as text (`<pre>`), never arbitrary HTML;
- optional GitHub materialization is explicit and deterministic at `docs/analysis/<run-id>.md`; only same-project `generated_artifacts` + `app_managed_write` bindings and allowed paths qualify; duplicate content is reused without a commit; public repos require a separate owner confirmation; GitHub failure never removes the internal Markdown;
- seven-day guest links use a fragment bearer token exchanged for HttpOnly guest session; stored bearer material is hashed; guest WSS is read-only and revalidates expiry/revoke; private document cards are projected as closed documents without reference/body/author leakage;
- guest UI is separate from the authenticated Live app, has no microphone/analysis/edit/history controls, and guest routes are network-only in the service worker;
- PWA system share is only called from a user gesture and never claims delivery;
- Android capability is narrowly `share.open_chooser`, accepts HTTPS URL/title only and receipts `chooser_opened=true, delivery_confirmed=false`.

## Automated and CI evidence

### Backend

On `b7999b9…`:

- full local Python suite: **180 passed**;
- source before/after the suite was clean and unchanged.

Coverage includes board idempotency/revisions/history/WSS, analytics lifecycle and isolation contracts, request-key behavior, cancellation, guest projection/revoke/expiry, Android device receipts, GitHub materialization policy and current voice/WSS regression tests.

### PWA

On `b7999b9…`:

- `npm ci`: PASS;
- `npm run test:live-framework`: **39/39 passed**;
- TypeScript/Vite production build: PASS;
- release bundle includes Pixi/WebGL and current shared Live framework;
- only the existing Vite chunk-size warning remains; it is not treated as a performance acceptance result.

### GitHub Actions

PR #87 at `b7999b9…`:

- Projects Hub contracts run `37378224357`: backend **SUCCESS**, PWA **SUCCESS**;
- Android PR run `37378224436`: build **SUCCESS**, Android emulator-smoke **SUCCESS**;
- explicit final Android run `37378388094`: build **SUCCESS**, Android emulator-smoke **SUCCESS**.

This is emulator/CI evidence, not physical-device evidence.

## Provider / analytics evidence

Installed DevCoveer runtime during the work reached the provided-only council/readback line and fixed durable council task lookup. Free council live canaries use the current catalog and do not silently spend NVIDIA.

A real free council task on the current isolation line still returned provider HTTP 403 for both selected participants:

- `opencode/nemotron-3-ultra-free`;
- `opencode/nemotron-3.5-lightning-free`;
- provider message: `OpenCode's free tier can only be used from within OpenCode`;
- actual usage reported `nvidiaCalls: 0`.

Therefore free council is correctly **fail-closed / unavailable**, not accepted as a working council.

The `council_pro` contract was subsequently changed by explicit owner instruction: NVIDIA is a slow bounded-capacity resource, not a user-confirmed budget. A current acceptance run must therefore test automatic Kimi K3 + DeepSeek V4.1 Flash dispatch under two-slot admission; no confirmation token is expected. This historical checkpoint did not contain that transport and cannot be used as evidence that the new council path works.

The original saved Kimi K3 consultation from the task inputs was not repeated, per the owner's instruction not to start a new consultation from scratch.

## Browser evidence

Two independent temporary browser contexts were created against the exact local branch server at `http://127.0.0.1:8196`:

- both loaded Projects Hub with HTTP 200 and independent pages;
- server exposed the first-party invite UI;
- the documented loopback owner-invite endpoint returned HTTP 200 for two temporary invites.

The connector deliberately redacted the invite bearer tokens in task readback. I did **not** bypass that boundary by dumping tokens/cookies to files or reading the database directly. Consequently these two contexts could not be authenticated from this execution surface, so the required authenticated two-browser board/WSS/reconnect walkthrough is **not passed** here.

Real board/guest WebSocket behavior is nevertheless exercised by backend WebSocket integration tests, including guest revoke while the socket is open, one-use tickets, origin checks, event projection and direct-API denials. That is not presented as a substitute for the required real browser gate.

Temporary browser sessions and the local acceptance server were closed after the attempt.

## T01–T28 checkpoint

| Test | Status in this checkpoint |
| --- | --- |
| T01–T12 | PASS at source/backend integration level; covered by full Python suite and board-specific tests. |
| T13 | PASS automated: per-browsing-context client ID; no shared localStorage client identity. Real two-tab browser walkthrough still part of browser gate below. |
| T14 | PASS automated: project/object-scoped UI command and stale-object handling. |
| T15 | PARTIAL: web contracts/build pass; real touch/pinch/IME/reduced-motion/context-loss matrix and frame profiling not executed on devices. |
| T16 | PASS automated for search/author/color/off-screen semantics; no browser visual acceptance in this window. |
| T17 | PASS for isolated single-model contract/source tests; council free-provider transport is unavailable and fails closed. No ambient-context fallback was enabled. |
| T18 | PASS automated: durable request keys, duplicate command/run protection, dispatch_unknown/no blind retry. |
| T19 | PASS automated: cancel/late-result/access checks; no automatic late board publication. |
| T20 | PASS fail-closed behavior: invalid/unavailable model/provider is visible and no hidden native/paid fallback is used. |
| T21 | **BLOCKED for this historical checkpoint**: free live council was rejected by OpenCode free tier. Current target is automatic dual-slot NVIDIA `council_pro` (Kimi+DeepSeek), no user budget-confirmation; it requires a fresh live provider acceptance on the current runtime. |
| T22 | PASS source/automated policy: canonical internal Markdown survives GitHub failure; deterministic/idempotent materialization and public-repo confirmation implemented. **Live GitHub binding/materialization was not exercised against a real configured target.** |
| T23 | PASS backend WebSocket/API integration; **anonymous real-browser guest walkthrough remains blocked by the authenticated-browser setup above.** |
| T24 | PASS backend integration: expiry/revoke terminates new and already-open guest access/event flow. |
| T25 | PASS automated: private document body/reference/author projection, no guest SW cache fallback, Markdown displayed as text. |
| T26 | PASS source/CI/emulator for PWA gesture and Android chooser semantics; **physical chooser/cancel/fullscreen device evidence missing.** |
| T27 | **PARTIAL/BLOCKED**: bounded queues/admission and combined automated suites pass; requested 500-Android / 1000-desktop frame-time, ACK/focus latency, memory and Live-impact profiles were not measured. |
| T28 | PASS source/CI for current voice/updater contracts and Android emulator; **physical signed self-update on a device is not repeated for this candidate.** |

## Remaining mandatory gates

PR #87 should remain draft until the relevant owner/operator evidence is added:

1. **Authenticated browser acceptance:** two independent authenticated browser contexts on the candidate; in context A issue a board mutation **through Mira**, observe the authoritative WSS event in B, force reconnect around receipt/tail recovery, and verify no duplicate mutation or stale focus. No human drag/edit acceptance path.
2. **Council acceptance:** run one current automatic dual-slot NVIDIA `council_pro` with Kimi K3 + DeepSeek V4.1 Flash and verify provided-only evidence, attribution, dissent, request-key/readback and capacity behavior. No user budget-confirmation and no hidden fallback from free OpenCode.
3. **Provider single-model product-path evidence:** do not repeat the saved Kimi consultation merely to manufacture evidence; run only when a new explicit product acceptance call is desired/approved.
4. **Real GitHub materialization:** with a configured same-project `generated_artifacts` binding, verify create → exact duplicate reuse → guarded update; for public repo verify the second explicit publication confirmation.
5. **Physical Android:** microphone/long-speech/barge-in with board open, native share chooser cancel/success semantics, and signed in-app update.
6. **Performance profile:** 500 sticky Android / 1000 desktop; record frame time, memory, board ACK/focus latency and effect on Live. No FPS claim is made without these measurements.

## Cleanup

- temporary local branch server: terminated;
- two temporary browser sessions: closed;
- invite probe task: terminal/not running;
- managed source-handoff inventory was inspected. `cleanupSafeCount=0`; no handoff was force-deleted because all remaining entries contain unique/unsubsumed changes. This deliberately preserves unrelated or unfinished work.

## Release posture

Source candidate `b7999b9…` remains useful historical source/CI evidence, but it predates the normative Mira-only writer amendment and later voice/council work. It is **not a current release candidate**. Do not merge PR #87 or claim production acceptance from this checkpoint; current acceptance must be rerun against the latest branch with voice-first UI, automatic dual-slot NVIDIA council, browser Mira→board WSS scenarios, physical Android and measured performance gates.
