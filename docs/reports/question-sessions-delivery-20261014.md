# Question sessions delivery checkpoint — 2026-10-14

Status: implementation verified locally; deployment and real acceptance pending.
Version: 0.1.59. Base SHA: 0de03c1b23113354bc7431f06298b15644d54277.
Scope: existing question-session delivery specification revision 4 and corrections revision 2.
No requirement or permission change is included.

## Preserved implementation and completed gaps

The existing 11 dirty source/test files were preserved. Their tracked workspace digest before the first write was c30c0ac8a3b1940871055acb5a330c17429fd6f50be557af6a80da3c174a47e1. No reset, clean, external model executor, new subscription, credential or grant was used.

- Dot-authored typed question packages retain source identity/revision, addressed actor, priority and approved binding.
- Mira offers questions before asking. Explicit controls persist consent, pause/resume and important-only selection; answers never automatically start dot continuation.
- Explicit full/partial completion is idempotent. Unresolved blockers survive partial completion. Answers, command receipts and the existing request/outbox are transactional.
- Expired event transport resumes unanswered question requests when the same authorized subscription is renewed; accepted deliveries are not emitted twice.
- MCP exposes answer receipt IDs and accepts typed analysis_result. The existing scoped GitHub gateway publishes one deterministic linked substantive result, checks source SHA before/after publication, verifies exact readback and rechecks authorization.
- Claimed incoming artifact paths/SHAs are rejected. Result publication only uses the existing private project-docs repository binding. Source drift is an explicit conflict.
- Slack retains existing claim/receipt and unknown-outcome reconciliation. Questions cannot be sent through the result-claim path.
- A confirmed result delivery creates a personally addressed durable Mira notification; the existing read tool exposes final result content and brief seen-cursors suppress repetition.
- Administrative binding-status is read-only and reports the existing source repository/path/revision. It cannot grant question permission.

## Validation evidence

- Initial complete backend baseline after restoring missing project dependencies: 369 passed, 4 failed.
- Integrated final backend run: 394 passed, 2 dependency deprecation warnings (job_a14f26b9fddddeb9286757d0).
- PWA check:release: 67 tests passed, TypeScript check and production build passed (job_e792dd4b192b3be501488a58).
- New deterministic regressions cover source drift, forged artifact receipt, answer/control idempotency, restart, partial blockers, grant/binding revocation and subscription renewal recovery.
- No separately configured lint command exists in the inspected workflow. The PWA build includes tsc --noEmit.
- Local browser theme suite not run: this checkout lacks the workflow's isolated Playwright dependency. Hosted CI remains required.
- These tests use controlled fixtures and are not a claim of real idle-dot, Slack, physical microphone or Live acceptance.

## Deployment and real acceptance

Observed production before release remains healthy on b48566f7a6eb28a4f250f238081ec42311bc47e9, version 0.1.58.
Use the existing deploy/devcoveer_install.py exact-main-SHA procedure after CI.
Final remote, CI, deployed SHA and runtime readback will be recorded after release.

The existing projects_hub.request.created subscription is active. Its automation and note binding currently authorize the older workflow; question-workflow authorization is being requested separately and is not modified here. Acceptance must stay within the owner-approved test note/binding and original test Slack thread.

Remaining product gates:
1. Verify hosted CI on the release candidate and exact deployed SHA.
2. Verify current test binding, source repository and explicit question-workflow permission.
3. Real MCP question_start and questions_ready events must wake idle dot, save one verified result and confirm one Slack receipt in the original thread.
4. Owner performs the real Mira voice offer/consent, important-only, answer, interruption/resume and completion; Mira then notifies and reads the result on request.
5. Controlled restart preserves version/receipts/delivery/notification without duplicates.

No callback 2xx, test fixture or prepared audio is represented as completion of these live gates.

## Release review fixes — 05:40 UTC

Independent source review identified and fixed three concrete defects:
- Reads now recheck dot-package source revision and current requester/addressee grants and binding authorization before returning questions.
- Any durable completion intent freezes new answers, including awaiting_delivery; old command replay remains immutable.
- Pause/resume preserves the important-only filter when omitted; explicit false clears it.

Full backend validation after these fixes: **405 passed** (job_8ac21139aef8376a2b3b907a).
Hosted CI run 38517500548 passed the complete PWA job including browser theme checks. Its backend failure was a pre-existing main-branch Android contract/test mismatch: the owner-approved native target replaced the old WebView target, while runtime remains WebView. The test now distinguishes the target contract from current implementation without changing voice code or requirements. CI rerun remains pending.
