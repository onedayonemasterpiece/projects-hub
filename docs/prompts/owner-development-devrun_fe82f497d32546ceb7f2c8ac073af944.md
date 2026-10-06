# Implementation brief: voice-controlled light/dark theme

## Authority, scope and nearest verifiable result

- Development batch: `devrun_fe82f497d32546ceb7f2c8ac073af944`.
- Selected task only: `tsk_fde9eb2523d743b4be8e71b40c5f9f20`, «Смена темы оформления через голосовые команды».
- Owner acceptance: the user requests light/dark theme by voice, Mira understands and applies it, and the user hears confirmation.
- This document is the quality/design deliverable, not implementation, merge, deployment, release approval or evidence of product acceptance. The implementation thread must retain the existing batch and delivery authorization boundaries. PR #87 remains owner-paused.
- Nearest product result: in an authenticated PWA and installed Android app, say «Мира, включи светлую тему», observe the app change, hear truthful confirmation from the same Mira Live conversation, then say «Верни тёмную тему» and continue talking without losing microphone, transcript, focus or reading position. Reload restores the actor's saved choice; another actor is unaffected.
- Implement personal theme preference, its small Live capability, readback/application acknowledgement, accessible web palette, Android presentation and the necessary regression coverage. Adaptive onboarding, new board/collaboration features, general settings UI, system/automatic themes, new speech recognizers and unrelated repairs are outside this batch. Deferring onboarding here does not remove its contract requirement.

## Inspected baseline and evidence limits

Analysis date: 2026-10-06. Repository HEAD when inspected: `6215eb13776f7d07df86b0bd108b22d4460a14e3`.
Initial mandatory `git status --short --branch` returned `## main...origin/main` with no changes. A later pre-write check showed an unrelated untracked `scripts/.tmp_probe_current_execution.py`; it was not read, changed or removed. Recheck status and every edit target before implementation; preserve parallel work and use this repository directly, without another checkout/worktree.

During final verification, another process changed the branch to `chatgpt/interrupted-owner-development-recovery-20261006` at the same HEAD; the unrelated temporary file was no longer listed. This design thread did not switch branches or delete that file. Its only write is this brief. Documentation validation: all 15 no-argument checks in `tests/test_product_spec.py` and `tests/test_live_product_contract.py` passed by direct Python invocation; the environment has `python3` but no `python` command or installed `pytest`, so this is not a pytest/CI run. Brief structure/whitespace and manifest digest checks also passed.

Protected manifest: `.devcoveer/requirements.json`, SHA-256 `b18bda6de9f0a16935b37fba18e1c85362694e61eb59360d97190ab09a00c0f7`, verified during analysis. Do not edit the manifest, reinterpret a critical requirement or claim this task completes unrelated requirements. Any conflict is reported explicitly; intentional requirement changes need the separate authorized requirements-update path.

Read first: `AGENTS.md`, `docs/vision.md`, `docs/resource-rollout.md`, `docs/product/03-product-and-ux.md` (theme/onboarding update), `docs/product/13-ui-floating-islands.md` (themes/accessibility), `docs/product/12-central-live-agent.md`, `docs/product/16-wss-multi-user-reliability.md`. The first two product documents already specify actor-level persistence, typed readback and continuity; they need no requirement rewrite.

Shared architecture inspected locally at `/home/dev/projects/live-interaction/docs/live-agent-architecture.md`, plus `docs/integration.md`, `docs/native-wss.md` and `skills/live-interaction/SKILL.md` in that repository. The advertised installed skill path was missing; the canonical repository copy was used. Python and browser pins in this product both identify shared release `0.3.27`, commit `557532aa6277b5fc06424eb171d99e96f1445387`; the pinned Python session host was inspected using `git show`. Do not infer support solely from an unpinned working checkout or historical resource-rollout version text.

| Area | Observed code | Consequence |
| --- | --- | --- |
| Web theme | `web/src/styles.css` has dark root variables and many literal dark backgrounds/light foregrounds; `web/index.html` and `web/public/manifest.webmanifest` have dark theme colors | Replacing one background variable is insufficient. Audit every existing visible surface, focus ring, button, overlay and status. |
| Web state | `web/src/App.tsx` owns bootstrap, conversation, Live client and `applyLiveEvent`; `web/src/api.ts` bootstrap contains actor/workspace/projects, no preference | Add actor preference hydration and an independent theme application path; do not recreate the Live client when theme changes. |
| Tool delivery | `App.tsx` handles successful `tool_result` by fetching affected domain state | The shared host emits tool name/status/revision, not the entire tool result. Do not expect `event.result.theme` to exist. |
| Live adapter | `src/projects_hub/live_adapter.py`: `_functions`, `SYSTEM_INSTRUCTION`, `initialize`, `execute_tool`; no theme tools or `resolve_capability` | Existing setup eagerly exposes 16 base tools, plus optional grants/owner tools. Adding two more eager tools would violate the required architecture. |
| Shared capability support | Pinned `python/live_interaction/session_host.py` supports `resolve_capability`, validated transition specs, shared resumption and at most nine functions per transition | Adopt that existing interface; no local provider reconnect implementation or new framework release is assumed necessary. |
| Persistence | `src/projects_hub/store.py`: SQLite/WAL, actors, sources, commands, revision/readback patterns; no actor theme table | Add a small additive migration and atomic preference/receipt operations to this durable store. No new broker or preferences service. |
| Current command identity | Adapter `_command_id` hashes source ID, mutable transcript revision, tool name and args | Blind reuse can generate another ID after late canonical transcription. Theme needs a stable accepted-turn command identity; do not rewrite unrelated mutations in this batch. |
| Authentication | `app.py` resolves cookie session to actor, validates conversation/resource and binds Live to actor/workspace/conversation | Preference target is server-derived stable actor identity, never display name, email, project or model-supplied user ID. |
| Android | `MainActivity.java` loads the hosted app in WebView, hardcodes dark root/WebView/system bars/update button; `res/values/styles.xml` is dark | Web CSS reaches installed apps via hosted delivery, but complete Android chrome parity requires a narrow native change and signed update. Keep the current WebView voice transport. |
| CI | `.github/workflows/tests.yml`, `android.yml`, `android-release.yml`, `android-self-update.yml` | Backend/PWA checks exist; Android build/emulator jobs are path-filtered. Release publishes signed APK and SHA-256 manifest. |

Observed architectural discrepancy: `live_runtime.py` also starts `CaptionSidecar` using `gemini-3.5-transcribe-live`; `App.tsx` renders its captions. The supplied `CORE-CENTRAL-MIRA` contract forbids a second hidden ASR/router. This analysis does not declare the sidecar compliant or silently amend that requirement. Theme intent and authorization MUST come exclusively from the central Live model's function calls, never captions or text matching. Record this existing discrepancy in the implementation handoff; do not expand this task into a transcription rewrite or claim whole-product contract compliance. If successful theme operation depends on the sidecar, that is a concrete conflict to resolve before accepting the feature.

No provider, microphone, browser, Android device, production, Telegram or release acceptance was performed during this design pass. Historical scaffold warnings and later delivery summaries are not proof for the new candidate.

## Requirements and invariant mapping

1. `UX-ADAPTIVE-ONBOARDING-THEME`: a small deterministic preference executor behind Mira. Theme metadata is private actor state. Do not implement onboarding merely because both appear in one requirement.
2. `CORE-CENTRAL-MIRA`: raw speech follows the existing central Live audio route. No regex over captions/transcripts, separate ASR, local command classifier, second LLM, synthetic text replay or separate TTS to recognize/confirm theme commands. The normal Mira output audio carries confirmation.
3. `CORE-LIVE-WSS`, `CORE-DURABLE-VOICE`: retain same-origin authenticated WSS, one-use ticket/subprotocol, binary PCM bounds, fresh generations, damaged-turn exclusion and no HTTP audio fallback. A preference ACK is a product control request, never a new audio route. Durable deliberate replay and transcript-only recovery remain distinct.
4. `CORE-MULTIUSER-ISOLATION`, `CORE-FIRST-PARTY-IDENTITY-AUTHZ`: any admitted ordinary actor can change their own theme, including users without editor or owner rights. This grants no project or development access. Personal state is keyed by the existing first-party actor record; Live calls still validate current workspace/conversation membership. No new identity migration.
5. `CORE-PERSONAL-TIMELINE`, `CORE-INLINE-ONE-BOARD`, `CORE-BOARD-VOICE-LLM-FIRST`: theme changes presentation only. Preserve message IDs, open objects, selected project, scroll, board viewport/camera if present, subscriptions and capture/playback. No text composer, board mutation or new project chat.
6. `CORE-OWNER-SELF-DEVELOPMENT`, `CORE-ANDROID-SELF-UPDATE`: theme is an ordinary-user tool; development remains independently owner-only. Native changes use the established signed release and in-app verified update path after authorized delivery.
7. All other protected requirements remain unchanged. Theme handling must not read project repositories, publish notes, answer collaboration questions, broaden audiences, launch tasks or acquire cross-resource grants.

## Product behavior decisions

- Supported stored values: `light` and `dark`. Default for an actor with no saved choice is `dark`, matching current UX. No OS-follow/system value and no automatic schedule in this task.
- Scope: actor-wide within Projects Hub, across that actor's projects/workspaces and devices. The server is authoritative. Other open clients reconcile on launch, focus/resume, reconnect and their next Live use; simultaneous instant cross-device broadcast is not a completion gate. The initiating client applies immediately through the receipt event.
- «Включи светлую/тёмную тему» is sufficient authorization for this reversible personal preference; no extra confirmation question. «Переключи тему» reads current state and sets the opposite explicitly. Never persist a toggle operation that flips again on retry.
- «Уже светлая» is an idempotent no-op with current revision and truthful spoken acknowledgement. «Какая сейчас тема?» only reads. Ambiguous «сделай светлее» warrants a brief Mira clarification if the target is unclear; this capability cannot alter device brightness. Negation, quoted commands, discussion about themes and background noise are not user commands.
- On success, a short Russian phrase such as «Включила светлую тему» comes from Mira after persistence/readback and initiating-client application ACK. Do not hardcode an exact model sentence as a test oracle. Speech may be interrupted normally by Stop; do not repeat saved commands to force confirmation.
- If saved but UI application is not confirmed, say only «Настройка сохранена, но применение на этом экране пока не подтверждено». If persistence fails, retain the previous theme and explain failure. No optimistic success, fake ACK or silent local-only save.
- Offline speech retains the existing durable source flow. No immediate offline theme recognition is added. Deliberate actionable replay through central Mira may perform the request once; `recovery_only` replay remains nonmutating. Returning online alone never executes previously unaccepted audio commands.
- Login/unauthenticated screens start dark. Authenticated bootstrap applies the current actor's preference before exposing the main interactive surface. Clear in-memory theme and pending apply state on logout/actor change; never flash the prior actor's preference after an account switch. Avoid a new localStorage cache unless needed; if used it is actor-keyed, nonauthoritative and failure-tolerant.

## Backend persistence and typed tool contract

Add an `actor_preferences` table (actor foreign key/primary key, constrained theme, nonnegative revision, updated time). An absent row reads `{theme:"dark", revision:0}` without writing. A real change increments revision once. Repeating the current value with the current revision produces a verified no-op receipt without revision churn.

Use existing durable command storage where possible. Store the immutable request hash, actor/source/accepted-turn binding and resulting preference receipt atomically with the update; add a narrowly scoped receipt table only if the existing schema cannot represent that binding safely. `BEGIN IMMEDIATE`/equivalent transaction must cover revision compare, write and receipt, with rollback on error. Migration must work for both existing and empty stores without rewriting other data.

Expose exactly these domain functions in the preferences bundle:

| Function | Model arguments | Result |
| --- | --- | --- |
| `preferences_get` | Empty object, no additional properties | `theme`, `revision`; current authoritative value, no project content |
| `preferences_set_theme` | `theme: enum(light,dark)`, `expected_revision: integer >= 0`; both required, reject extra properties | `command_id`, `theme`, `revision`, `changed`, `persistence_status: verified`, `application_status: applied/pending/superseded` |

The actor, workspace, conversation, source, accepted-turn identity and execution origin are trusted session context. Model args cannot supply actor ID, repository, permission, raw CSS, DOM selector, provider/key, command namespace or transport session.

For command identity, derive and persist a key from stable actor/conversation/source + accepted utterance ID + tool operation + canonical explicit target/expected revision. Freeze the utterance binding before awaiting application ACK; late transcript revisions and provider call IDs must not change it. Existing adapter utterance IDs/source events are the starting point. Buffered mode can bind to its durable source identity. A later deliberate light→dark→light request is a new accepted turn, not a replay of the first. For capability continuation preserve the original accepted intent/turn identity in product state, never invent a fresh authorization from the shared continuation text. If no trustworthy accepted turn is available, fail closed.

Authenticate and verify ownership before any receipt lookup. A repeated key with the identical immutable payload returns the existing receipt; a key reused with a different payload is `COMMAND_CONFLICT`. Look up the accepted receipt before revision comparison for an exact retry. Stale new writes return `REVISION_CONFLICT` with current preference; Mira reads and asks the user before overwriting a competing newer choice. No blind retries with refreshed revision/new keys after an unknown outcome. An exact old receipt must not reapply its old theme over a newer preference; report `superseded` and return current state separately.

Suggested structured error codes: `INVALID_ARGUMENT`, `UNAUTHENTICATED`/`FORBIDDEN`, `TOOL_NOT_AVAILABLE`, `REVISION_CONFLICT`, `COMMAND_CONFLICT`, existing `LIVE_INPUT_DAMAGED`, and `PREFERENCE_STORE_UNAVAILABLE`. Map them through existing HTTP/tool error conventions; never return raw SQL, stack traces or credentials.

## Small capability integration, without a transport fork

Introduce `activate_capability({capability, intent})` through the pinned shared `resolve_capability(session, call)` hook. `intent` is bounded continuation context, not authority. Return the shared spec `{capability, configuration, context, continuation, response}`; transition only through the host/worker. Preserve model/key, manual activity detection, voice, transcription settings, source binding and authoritative product context. No custom reconnect loop, copied framework or dependency upgrade solely for this feature.

Start with core router + `projects_list_accessible`, `conversation_set_focus`, `runtime_versions_get` (four tools). Core advertises allowed capability IDs/descriptions, not every tool schema. Preferences has router + the two preference tools (three). Use `visual_context=none` semantically: no screenshot needed to know an enum preference. Split existing declarations into the following mechanical bundles to preserve reachability without retaining the eager setup:

| Bundle | Existing tools plus router | Count |
| --- | --- | --- |
| `memory` | `memory_read_project`, `voice_source_read`, `memory_commit_voice_source`, `memory_finish_ephemeral` | 5 |
| `repositories` | `github_repositories_list`, `github_repository_read` | 3 |
| `calendar` | `devices_list_capabilities`, `calendar_create_event_on_device`, `calendar_list_events_on_device` | 4 |
| `readiness` | `event_cards_list`, `event_readiness_set`, `task_create_follow_up`, `task_set_state` | 5 |
| `knowledge` | `knowledge_search`, only with its existing resource-specific grant | 2 |
| `expert_reviews` | Existing five expert-review tools, only with current grant/profile | 6 |
| `owner_development` | Existing five backlog/development tools, explicit owner checks unchanged | 6 |

This wiring is the minimum prerequisite for adding the preferences capability under `AGENTS.md`, not permission to redesign those domains. Preserve their schemas/authorization and move only their prompt instructions into their matching overlays. Core stays small: identity/language, direct intent, truthfulness, continuity, router, safety. Do not reference absent function names in active prompt layers. Keep buffered disposition instructions applicable: actionable buffered turns can transition to `memory` for final disposition; `recovery_only` exposes no router or mutations.

Check active tool allowlist again in the product adapter before execution: the shared host's `execute_tool` delegation does not itself establish product capability authorization. Router allowlist is actor/mode/grant-derived and revalidated on activation. Ordinary-user sessions cannot activate or directly call owner-development functions. A read-only/recovery mode cannot obtain mutation tools through the router. Preserve trusted original-turn binding across shared transitions; mark a router call's accepted semantics consistently with the adapter's existing utterance accounting.

Use shared `capability_ready`/transition failure events for UI state. Never call browser Stop/Start or create a new conversation just to switch theme. A provider connection may be resumed by the shared framework; that does not imply a new product conversation. Test return to the prior domain via router without asking the user to repeat the original theme request. Capability counts apply to every actual setup, including optional grants and buffered mode.

## Delivering and acknowledging the applied preference

Persisting an enum is not proof that a visible client changed. Implement a small product application-receipt boundary:

1. Include `preferences:{theme,revision}` in authenticated bootstrap and expose `GET /api/preferences` for reconciliation. These operations never execute writes or advance a pending command.
2. After durable theme commit/readback, publish a bounded `preferences_changed` event on the initiating session using the shared adapter `emit` callback (currently discarded by `**_shared`). Payload: version, command ID, actor/session/conversation correlation, theme and revision. No shared project broadcast, transcript, secret or arbitrary style payload.
3. Client validates enum/schema, current actor and current Live client/generation before applying. It uses authoritative revision ordering; older events or fetch results never roll back a newer theme. Equal revision/same theme is safe. Equal revision/different theme is an error requiring readback.
4. Apply root theme attribute, `color-scheme` and document `theme-color`; retain component identity and Live refs. After the DOM style update and a render opportunity, read back the applied attribute/computed theme marker, then send an authenticated same-origin product ACK.
5. Suggested ACK route: `POST /api/live/{conversation_id}/sessions/{session_id}/preferences/{command_id}/applied`, body `{revision,theme}` only. Bind it to the current authenticated actor, owned conversation, exact original session/application request and persisted receipt. Enforce existing origin/CSRF policy. It cannot select/change a preference, create commands or acknowledge another client session. Unknown, mismatched, stale and foreign ACKs fail closed. Duplicate matching ACK is harmless.
6. Bound ACK wait in the executing tool to 3 seconds after event publication; return `application_status=applied` only on matching ACK. Otherwise return verified persistence + `pending` (or `superseded` when current revision advanced). Do not keep a Live tool or queue unbounded. Stop/session close cleans in-memory waiters without reverting the saved preference. One pending apply per session is sufficient under serialized tools; independent sessions remain isolated.
7. ACK processing must be independent of the waiting serialized tool path to avoid deadlock; never feed it to the model as user speech. Reconciliation after process restart needs only stored preference/command readback; no ACK worker or new broker. Reading a receipt never re-executes the setter. Do not issue unsolicited voice confirmation when a late ACK arrives after the tool returned.

An application ACK is evidence of client-reported rendering, not a security permission or proof the person heard sound. Physical/browser acceptance below verifies the actual visible/audio result. Expose bounded application status inline in the existing notice/status surface when necessary, without a new text editor or modal confirmation ceremony.

## Web and Android presentation

Create semantic light/dark tokens for canvas, surfaces/overlays, foreground, secondary text, borders, focus, accents, disabled controls, warnings, success/error and shadows. Convert hardcoded colors where their contrast depends on theme; preserve intentional brand/content colors with contrasting text. Keep layout, floating islands, safe areas, 44dp targets and existing voice animations. Set `color-scheme` so native form controls/scrollbars are legible. Avoid broad animated recoloring or flashes; honor reduced motion. Maintain at least 4.5:1 ordinary text contrast, 3:1 large text and essential control/focus graphics in both palettes. Inspect actual rendered compositing over translucent surfaces, not only token pairs.

Cover conversation bubbles/transcripts, project/context sheet, memory reader, event/readiness/task cards, backlog/development panel, GitHub controls, login/error/empty states, microphone/listening/answering/wait/offline states and update actions. If parallel work adds board/widgets, theme those existing surfaces and preserve viewport resources; do not build missing boards here. Static web manifest colors may remain the documented neutral dark launch fallback; runtime document chrome must follow the selected theme.

Android scope includes root/WebView background, status/navigation bar icon contrast and native update button. Keep one WebView and one shared browser microphone/Live client; no Activity recreation, `loadUrl`, app reload or native socket switch on theme change. Add only a narrow theme presentation bridge, with exact trusted Projects Hub origin and main-frame validation, bounded enum/revision input, no secrets or generic code execution. Prefer an origin-scoped AndroidX WebKit message listener (pin a verified compatible version if adding that dependency); do not expose a universal `addJavascriptInterface` to foreign frames. Reuse `WebOriginPolicy`. Apply colors on the UI thread and acknowledge native application back to the initiating trusted page. Disable/remove the channel on untrusted navigation; foreign OAuth pages cannot invoke it. Clear native presentation state on actor reset, use dark before trusted page hydration, and rehydrate on resume.

New Android clients include native chrome application in the UI ACK. Hosted web must remain compatible with old installed APKs: missing bridge does not break theme, voice, authentication or updater. Report web application separately from unsupported native chrome and show an ordinary in-app update indication when needed; do not claim full new Android presentation on an old binary. No OS-wide theme or brightness changes. Because this planned slice includes native code, signed Android release/update-manifest delivery is required for full completion; a hosted-web-only intermediate result is explicitly partial.

## Regression and acceptance matrix

Tests must assert behavior and authoritative state, not only search source strings. Use existing isolated SQLite/test fixtures and shared fake provider contracts for deterministic coverage. Mocks do not prove actual speech recognition or microphone acceptance.

| Case | Required assertion / evidence |
| --- | --- |
| Fresh and existing database | Default dark/revision 0; additive migration preserves actors, sources, commands and prior content; stored choice survives store reopen/server restart. |
| Both directions and same-value request | Correct enum, one revision increment per real change, no-op receipt, appropriate confirmation; light→dark→light across separate turns all work. |
| Strict inputs | Reject invalid theme, bool/float/negative revision, extra actor/project/CSS fields and oversized arguments; no mutation. |
| Private identity | Actor A changes theme while B talks in same workspace; B's preference/client stays unchanged. A sees same choice across projects/workspaces; no editor/owner requirement for own preference. |
| Direct attacks | Unauthenticated GET/ACK, foreign actor/session/conversation, wrong origin, stale session, forged revision/command ACK and out-of-bundle function call fail; ordinary user cannot obtain development tools. |
| Idempotency/restart | Same accepted command under duplicate provider call, changed provider call ID, late transcription and backend reopen returns one durable result. Lost response reconciles the same command. Old light receipt cannot overwrite newer dark. |
| Concurrent writes | Two sessions using one expected revision have one winner; stale setter returns conflict. Same command concurrent dispatch creates one receipt. No global lock while waiting for UI ACK. |
| Persistence/application failures | Transaction failure rolls back both preference and receipt; missing/delayed ACK yields pending, not applied; exact duplicate ACK safe; server/client restart preserves preference; no stale waiter leaks. |
| Event ordering | Event from old client generation/actor ignored; delayed GET cannot revert newer event; duplicate event doesn't double-confirm; event gap/reconnect fetches authoritative preference without replaying PCM/mutation. |
| Central speech semantics | Live hears raw microphone audio; explicit RU commands and natural paraphrases select the correct tool. Questions, negation, quoted instructions, ambiguous brightness request and noise cause no unwanted write. Caption events alone never invoke a setter. |
| Progressive tools | Core/preferences/other bundle counts are bounded; grants checked on transition and call; transition retains original accepted turn, same user conversation and domain context; return to normal project work works. Test buffered disposition and recovery-only restrictions. |
| Damaged/offline input | Mid-turn disconnect cannot change theme until a later clean turn; deliberate saved-audio replay applies once only when actionable, recovery-only never writes; source deletion remains tied to confirmed disposition. |
| Voice/UI continuity | Theme toggles during active conversation do not recreate capture, stop output, clear transcript, reset project/reader scroll, mount another board, duplicate subscriptions or break the next turn. Stop remains immediate, including ACK wait/transition. |
| Visual/accessibility | Desktop and mobile PWA plus Android: readable surfaces and statuses in both palettes, focus/keyboard navigation, enlarged text, reduced motion, no color-only critical status. Screenshot comparison includes open panels and error/offline states. |
| Android boundary | Trusted main frame can change chrome; foreign page/iframe/malformed message cannot; resume restores theme without microphone reload; old APK with new web remains usable. |
| Android delivery | Normal update discovery, increasing versionCode, SHA-256 verified APK, stable signing identity/system installer, preserved login/device pairing/preference, failure remains retryable. |

### Required automated checks for the implementation candidate

- Add focused preference store/API/Live tests, executable browser state/application tests, capability allowlist/transition tests and Android theme/origin tests. Existing `tests/test_live_adapter.py`, `test_live_wss.py`, `test_live_utterance_durability.py`, `test_input_boundaries.py`, `test_buffered_product_mode.py`, `test_caption_sidecar_lifecycle.py`, `test_public_auth.py` and `web/tests/*.test.mjs` identify affected seams; do not weaken old assertions just to fit new bundles. Update setup assertions to exercise the real router contract.
- Backend: `python -m pytest -q` (same complete suite as CI). PWA, from `web`: `npm ci --ignore-scripts --no-audit --no-fund`, `npm run test:live-framework`, `npm run build`. Run installation only where dependencies need restoring; reuse valid results for unchanged candidate inputs.
- Android native change: Java 17/SDK 35, `gradle -p android testDebugUnitTest assembleDebug --stacktrace` and existing emulator instrumentation/smoke path (`scripts/android_emulator_smoke.sh`). Add theme assertions to the installed UI test; current launch-only smoke cannot establish light-theme behavior.
- CI jobs `backend`, `pwa`, Android `build` and `emulator-smoke` must pass for the implementation candidate. For an authorized signed release, release signing/manifest job and existing Android self-update E2E also pass. If path filters omit an affected job, run its equivalent explicitly in the authorized delivery phase and retain the result.
- This document-only design pass needs documentation integrity/link checks and manifest preservation, not application rebuilds or provider calls.

### Browser, emulator and physical acceptance

Browser acceptance is required because CSS tokens, asynchronous events and actual playback are user-visible. Use authenticated production-equivalent same-origin HTTPS/WSS on the authorized candidate, desktop and narrow mobile viewport. Run at least ten consecutive spoken turns including both theme directions, repeat/no-op, normal project query before/after the switch, a capability return, a negative/read-only utterance and Stop/restart. Include two actors and reload persistence. Confirm screenshots, server readback, real output audio and retained conversation/microphone identity. Prepared PCM can test model/tool sequence but must be labelled as prepared PCM.

Android emulator acceptance is required for this native slice: install/launch, both palettes in WebView and chrome, update button, trusted-origin bridge, background/resume, rotation/lifecycle and preserved state. Existing CI emulator uses `-noaudio`; it cannot prove audible confirmation or physical microphone behavior.

Physical Android acceptance is required before claiming the backlog complete on Android: installed signed candidate, real microphone → central Mira → theme receipt/application → audible confirmation, dark and light, then a normal next turn. Check system bars, update action, pause/resume and the normal in-place update path. Record device/Android/WebView versions. OLED/LCD readability follows existing UI requirements; reuse relevant unchanged visual evidence where available and label device coverage honestly. Do not make unrelated platform certification a new gate.

Before any Telegram evidence lookup/read/send, read `docs/telegram-routing.md`; use only its canonical Projects Hub topic. External audit links are not routing authority. Do not send messages unless separately authorized. Use the devserver-artifacts skill for screenshots/logs and other non-source acceptance artifacts; no credentials, raw sessions, full private transcripts or media in routine logs.

## Delivery, rollback and Definition of Done

The later delivery phase needs authorized backend + hosted-web deployment because both tool/persistence and presentation change. Deploy additive backend support before clients that use it; fail visibly if capability is unavailable during a mixed-version rollout. Record candidate commit, framework pin, test/CI links, deployed backend/web identity, sanitized theme command/revision/application receipt and actual client versions. A real public TLS/WSS turn is required for deployed acceptance; reuse unchanged shared transport conformance evidence instead of rerunning an unrelated transport audit.

This planned implementation also changes Android chrome, so deliver through `.github/workflows/android-release.yml` with the existing stable signing identity, higher versionCode and verified `update.json`. Confirm update discovery/readback in the installed app. Do not hand out only a debug APK or mark a web deployment as a signed Android release. Commit/push/deploy/release are not authorized by this design document alone; follow the existing implementation/delivery batch authority, without inventing extra approval gates.

Rollback restores the prior known frontend/backend/native release through existing delivery controls; preserve the additive preference/receipt tables. An older app may render dark while stored preference remains intact. Never drop receipts or change a request ID to bypass an unknown outcome. Android rollback/recovery obeys signing/version rules; do not instruct an unsafe package downgrade or clear user data.

Definition of Done for the implementation batch:

- [ ] All three selected backlog acceptance criteria are demonstrated end-to-end on browser and physical Android, with real Mira voice confirmation after readback/application.
- [ ] Actor-scoped durable preference, default/no-op/conflict/retry semantics, bounded application ACK and reload/restart behavior pass the matrix.
- [ ] Small shared capability transitions work; ordinary capabilities remain reachable, owner permissions remain isolated, no second semantic theme path or local transport fork exists.
- [ ] Both themes cover existing visible UI and Android chrome with accessible contrast and no conversation/capture/scroll regression.
- [ ] Applicable automated checks and CI pass for the exact candidate; emulator and physical results are distinct and traceable.
- [ ] Authorized backend/web deployment is verified; signed Android update is discoverable/installable through the normal verified manifest path, preserving existing app state.
- [ ] Manifest digest/requirements are preserved; unrelated existing architecture discrepancies are disclosed without being normalized or falsely reported solved.
- [ ] Handoff clearly distinguishes implemented/tested, deployed, signed-release and physically accepted status. If a concrete mandatory gate is unavailable, report a partial result and the exact remaining gate; do not call mocks product completion.

No extra model tournament, broad cleanup, unrelated audit, onboarding implementation or new workflow engine is required to finish this task.


## Owner rework instructions (cycles 1–2)

The external owner rework instructions retain this development batch and its scope. Candidate `68999e2` restored the version/SHA wording; all other reviewed seams require corrections and fresh candidate evidence. Deployment and release remain prohibited in this implementation thread.

- Preserve strict TypeScript checking and type the theme controller interface explicitly.
- Dispose the complete private UI on account change or authentication expiry; old Live events and pending reads cannot populate the new account. Exercise another-tab A→B and login after 401.
- Bind theme command receipts and retries to the accepted intent through capability transitions, including B beginning while A is finishing.
- Buffered replay waits for completed transitions, a final provider boundary and authoritative terminal disposition within the existing timeout. Intermediate turn boundaries do not stop the session or resend PCM.
- Native requests and timers correlate by request ID. Application ACKs report `web_status=applied` and `native_status` independently (`applied`, `not_required`, `unsupported`, `failed`); unsupported Android chrome is never full application success. Existing minimal ACKs remain compatible; native clients without native confirmation remain pending.
- Browser coverage uses the mounted App and shared WebSocket client through the real application ACK endpoint, alongside continuity and identity isolation checks. Prepared fixtures remain distinct from actual provider/microphone acceptance.
- Required backend, PWA, Android unit/build/emulator and candidate CI evidence remains required. Unavailable execution facilities must be reported explicitly; authored tests alone are not passing evidence or product completion.


### Rework cycle 2 verification in the restricted implementation session

The candidate is the commit containing this appendix on `chatgpt/voice-theme-devrun-fe82f497-20261006`, based on `68999e2`. It is a partial implementation checkpoint, not ready-for-review or product-completion evidence.

- Full backend suite: `PYTHONPATH=src /home/dev/projects/projects-hub/.venv/bin/python -m pytest -q` — **198 passed** (27.66 seconds) after the final backend changes. Focused document/Live contract checks after this appendix's rework instructions: **15 passed**. Backend source did not change afterward.
- Eight focused repository JavaScript tests executed successfully in the tool V8 runtime, using fixture DOM/bridge/UUID/URL/microtask surfaces and assertions. Includes overlap timeout/ACK correlation, unsupported native status, stale actor/generation, revision ordering and core→preferences→memory completion coordination. This is additional behavior evidence, **not** the required Node suite, TypeScript build, Chromium, microphone or provider acceptance.
- `npm run test:live-framework`, `npm run build`, and `node tests/theme-browser.mjs` could not start: exit 127, executable not found. `/usr/local/bin/node` points to an unavailable `/opt/node-v22.22.3-linux-x64/bin/node`. Node download connectivity was checked once and failed DNS resolution.
- `gradle -p android testDebugUnitTest assembleDebug --stacktrace` and `bash scripts/android_emulator_smoke.sh` could not start: exit 127, Gradle unavailable. Java/ADB are unavailable too. No emulator/device outcome is claimed.
- The revised Chromium harness is authored but unexecuted. It drives shared WebSocket events into the mounted App, verifies HTTP preference ACKs, microphone/component continuity, another-tab A→B, a delayed old 401, login after current 401, and durable replay across two capability handoffs without repeated PCM or premature stop. Fixtures use loopback HTTP/WS; they do not establish production-equivalent TLS/WSS, real Live speech or audible confirmation.
- Requirements SHA-256 remains `b18bda6de9f0a16935b37fba18e1c85362694e61eb59360d97190ab09a00c0f7`; whitespace checks pass. The unrelated untracked recovery script is preserved and excluded.
- Required PWA, Android build/emulator, exact-candidate CI and real browser/Live acceptance remain outstanding. Missing installed Android/voice/artifact skills were not treated as acceptance evidence. No merge, deployment, signing, release or production action is authorized or performed. The existing caption-sidecar architecture discrepancy remains disclosed above and outside theme routing.
