# PH-VOICE-2026-10-05-R1 — acceptance report

Date: 2026-10-05
Task ID: `PH-VOICE-2026-10-05-R1`
Original implementation prompt: [docs/prompts/voice-long-input-fix-20261005.md](../prompts/voice-long-input-fix-20261005.md)
Root-cause audit: [docs/audits/voice-long-input-20261005.md](../audits/voice-long-input-20261005.md)

## Executive status

The reproducible ~30 second voice cutoff caused by the obsolete resource-control client is fixed and the final product release is deployed. Three real-provider five-minute runs stayed alive through 30/60/90/180/300 seconds with every PCM frame acknowledged, durable audio preserved, and no resource denial.

The task is **not fully accepted** because V03 and V11 are intentionally not being overclaimed:

- **V03 BLOCKED:** the deployed `gemini-3.8-live` conversational session produced final `input_transcript` only after an explicit speech boundary in the measured runs; it produced no `interim_input_transcript` during uninterrupted five-minute speech. Adding a second ASR/transcription model is forbidden by the protected `CORE-CENTRAL-MIRA` requirement and was not done.
- **V11 PENDING:** no physical Android device is registered in the available DevCoveer host surface. Emulator and synthetic/provider acceptance are not labelled as a physical-phone test.

All other available development, provider, concurrency, recovery, release, and emulator checks were executed. This report is the handoff for independent audit; it is not a declaration that V03/V11 passed.

## Causal chain

The verified production incident was not a microphone timeout. Projects Hub had pinned obsolete `ai-resource-control 0.1.7` installer SHA `51e9c043ce40dfefea8b2cb4f4956019819bd9d4`.

The failing ledger sequence was:

1. initial reservation: 16,384 units;
2. setup/provider use raised reserved total;
3. audio start received another 16,384;
4. after the local grant lease aged out at roughly 29 seconds, the next small PCM frame requested another fixed 16,384-unit block;
5. reserved usage was already `55,286 / 60,000`; only 4,714 units remained;
6. the oversized grant was rejected as `RESOURCE_TOKEN_BUDGET`, and the Live session closed.

The fix does **not** raise the policy limit. Projects Hub now installs the current resource SDK, whose voice grant contract uses the current 1,024-unit quantum/right-sizing behavior. The exact `55,286 / 60,000` regression is executable in the SDK repository.

The missing live user text was treated as a separate defect. Projects Hub now preserves full transcript payloads, has explicit interim/final UI paths, removes the old 4,000/1,200-character UI truncations, exposes typed terminal failures, and uses explicit activity boundaries for reliable final transcription. Real-provider testing nevertheless showed no provider interim transcription from `gemini-3.8-live` during uninterrupted speech; see V03.

## Delivered repositories and exact revisions

| Component | Delivered revision | Notes |
|---|---|---|
| ai-resource-control | `26278d0330fbb090802326c59e13487dbecd5e24` | merged exact voice-budget regression; production release marker pins this SHA |
| live-interaction | `0.3.20`, tag `v0.3.20`, commit `b036c298cfe9600b86a9a599f1281bfe5340acb2` | lossless transcript projection, typed terminal outcome, barge-in capture path |
| Projects Hub product | `0.1.26`, main `54311437d82873e42501c9706cb6e1951bd8a941` | PH-VOICE implementation plus acceptance hardening |
| Production backend/PWA | `54311437d82873e42501c9706cb6e1951bd8a941` | health 200, `static_ready=true`, Live/resource control available |
| Android product release | `android-v28`, versionName `0.1.26` | signed APK built from `5431143…`; SHA-256 `4e61d97d49b41555e5b1b02afc249b5dd580c2073ac5f51e25ef62df8ad63434` |

Public delivery objects:

- ai-resource-control: https://github.com/onedayonemasterpiece/ai-resource-control/commit/26278d0330fbb090802326c59e13487dbecd5e24
- live-interaction: https://github.com/onedayonemasterpiece/live-interaction/commit/b036c298cfe9600b86a9a599f1281bfe5340acb2
- Projects Hub implementation PR: https://github.com/onedayonemasterpiece/projects-hub/pull/84
- Projects Hub acceptance-hardening PR: https://github.com/onedayonemasterpiece/projects-hub/pull/88
- final product main: https://github.com/onedayonemasterpiece/projects-hub/commit/54311437d82873e42501c9706cb6e1951bd8a941
- signed Android release: https://github.com/onedayonemasterpiece/projects-hub/releases/tag/android-v28

## What changed

### Resource admission

- Projects Hub installer pin moved from the obsolete resource-control revision to `26278d0330fbb090802326c59e13487dbecd5e24`.
- The resource SDK has an explicit regression for the exact `55,286 / 60,000` state and a small PCM chunk; it requests the current 1,024-unit grant instead of the legacy 16,384 bulk grant.
- Policy capacity was not raised and quota denial semantics remain intact.

### Shared Live framework

- `live-interaction 0.3.20` no longer truncates projected input transcript payloads.
- provider/resource/connection/capture failures preserve typed terminal outcome and code through cleanup;
- capture during playback is not silently discarded when the product allows barge-in;
- transcript timing contains safe type/length/time metadata rather than user speech text.

### Projects Hub

- final/interim transcript is routed separately; provisional text is not committed as history;
- user bubbles are no longer cut at 4,000 chars and provisional UI is no longer cut at 1,200 chars;
- terminal resource/provider/connection/capture failures no longer render as ordinary idle;
- source audio/transcript remains actor/conversation private and can be recovered explicitly;
- recovery-only buffered sessions expose no mutation tools;
- a fresh normal Live session receives refs to unfinished prior sources and can page them through `voice_source_read`;
- shared manual activity boundaries provide reliable final transcript and post-pause microphone lifecycle without adding a second ASR;
- production transcript merge / terminal-state logic is shared with executable Node tests;
- backend logs correlate session/source/attempt/client/backend/build plus safe event counters/codes without putting speech text into diagnostic fields.

## Acceptance evidence

### V01 — exact budget regression

ai-resource-control acceptance:

- exact starting state: reserved `55,286 / 60,000`;
- small PCM estimated cost below the 1,024 grant quantum;
- grant attempts: `[1024]`;
- no legacy `16,384` request;
- delivery validation: 91 Python tests passed, PostgreSQL contract 63 assertions passed, provider-limit semantics 32 assertions passed.

**Status: PASS.**

### V02 — three real-provider 300 s runs

DevCoveer job: `job_889e1c999023281f6e53d89e`
Window: 2026-10-05 11:22:06–11:37:38 UTC.
Evidence level: synthetic PCM at real-time pace through deployed WSS and the real Live provider. It is not a physical microphone test.

| Run | Stream | Frames / ACK | Durable PCM | checkpoints | resource grants | denials | terminal |
|---|---:|---:|---:|---|---:|---:|---|
| 1 | 300.001 s | 3000 / 3000 | 9,600,000 B | 30/60/90/180/300 all alive | 23 requested / 23 granted | 0 | none |
| 2 | 300.001 s | 3000 / 3000 | 9,600,000 B | 30/60/90/180/300 all alive | 23 / 23 | 0 | none |
| 3 | 300.000 s | 3000 / 3000 | 9,600,000 B | 30/60/90/180/300 all alive | 23 / 23 | 0 | none |

The combined canary returned non-zero because it also required V03 interim text and a stricter semantic-marker check. Its per-run `v02_transport_ok` was true for all three runs.

**Status: PASS.**

### V03 — visible user text before speech end

The same V02 runs produced:

- `interim_events = 0` for all three;
- first any transcript at about 302.5 seconds, as final `input_transcript` after the speech boundary;
- final transcript length 4,439 chars in each run;
- native `input_audio_transcription: {}` did not cause provider interim events;
- lowering automatic VAD silence alone did not cause provider interim events;
- manual activity boundaries reliably produce final transcript, but a final chunk after a boundary is not relabelled as provider interim/live text.

No capture counter, mock provider event or DOM placeholder is treated as acceptance.

**Status: BLOCKED.** A second transcription model was not added because the protected product contract requires the single Mira Live agent and forbids a hidden second ASR/LLM router.

### V04 — >4k lossless transcript

Executable evidence:

- live-interaction session-host test preserves an input transcript of >10k chars exactly from provider adapter to client projection, without a `truncated` marker;
- Projects Hub store pages provisional and final transcripts in 4,000-char windows and reconstructs exact full text;
- late final after `turn_complete` remains ordered and becomes the durable final;
- production `mergeTranscript` helper is executed with >4k start/middle/end content and a provider-corrected cumulative final.

The 300 s provider corpus did not pass its separate middle-marker semantic check, so this report does not use that corpus as proof of provider semantic fidelity.

**Status: PASS for losslessness/order contract.**

### V05 — pause/playback/microphone lifecycle

DevCoveer job: `job_1ac6f34305be6f497cd9ee07`
Window: 2026-10-05 11:39:04–11:40:08 UTC.

Real-provider result:

- 180/180 frames acknowledged;
- barge-in began during model playback and follow-up input transcript was received;
- provider emitted one `interrupted`;
- subsequent speech after pauses of 1, 3, 6 and 10 seconds all produced input transcripts in the same session;
- no resource denials and no tool calls.

**Status: PARTIAL.** Browser/provider lifecycle passed; acoustic echo on a physical handset is not proven and remains part of V11.

### V06 — failure outcomes and correlated diagnostics

Executable evidence:

- live-interaction maps `RESOURCE_TOKEN_BUDGET` to terminal `resource_denial` and preserves the exact error code;
- production Projects Hub classifier distinguishes `resource_denial`, `provider_failure`, `connection_failure`, and `capture_error` from ordinary `off`;
- backend executable test injects controlled resource denial, provider error and transport gap and verifies correlation by session/source/attempt, client/backend version, backend release SHA, kind/code/status and connection generation;
- diagnostic records do not contain transcript text;
- real provider long runs show grants and no quota bypass.

**Status: PASS (executable forced-failure contract).** Production quota was not destructively exhausted merely to prove denial.

### V07 — durable recovery into a fresh Live session

DevCoveer job: `job_e600975e7300fb4afdb35523`
Window: 2026-10-05 12:18:00–12:18:36 UTC.

Real-provider result:

- recovery-only session used no mutation tools;
- recovered source reached `agent_disposition_pending`, transcript revision 1;
- a new normal Live session saw the pending source ref and called `voice_source_read`;
- the answer contained all three start/middle/end control markers;
- no mutation calls and no provider errors.

Additional real-provider retry evidence: `job_15e28a60bc774bd99c5aec88`, 2026-10-05 11:39:59–11:40:13 UTC — partial server source persisted, retry reused/reset the source, final input/output/turn complete was observed and exactly one memory object was committed.

**Status: PASS.**

### V08 — exactly-once new mutation after recovery

Executable fresh-session integration:

- recovered old source is read through `voice_source_read`;
- a new instruction creates one `task_create_follow_up`;
- repeating the identical semantic mutation returns the same task through the deterministic command id;
- task list contains exactly one matching task;
- a different new instruction receives a different task id;
- old source text is not substituted into the new task description.

Real-provider retry also produced exactly one memory object for the replayed source.

**Status: PASS.**

### V09 — two users concurrently for 180 s

DevCoveer job: `job_b4d8af61de9d0c021c626fad`
Window: 2026-10-05 11:42:07–11:45:19 UTC.

Two distinct actors/workspaces ran concurrently against the real provider:

- each streamed 180.001 s;
- each sent/received 1800/1800 PCM ACK;
- 30/60/90/180 checkpoints remained alive;
- one final input transcript each;
- no resource denials, errors or tool calls;
- own-source reads succeeded;
- cross-actor source reads were denied in both directions.

**Status: PASS.**

### V10 — release provenance and Android update chain

Final product identity:

- repo product main: `54311437d82873e42501c9706cb6e1951bd8a941`;
- production health: `0.1.26`, same release SHA;
- production release marker resource SDK: `26278d0330fbb090802326c59e13487dbecd5e24`;
- Python dependency: `live-interaction @ v0.3.20`;
- built-web metadata: `live-interaction 0.3.20`, release commit `b036c298cfe9600b86a9a599f1281bfe5340acb2`;
- Android release workflow `37309579575`: signing material, tests/build, `apksigner verify`, manifest and release publication succeeded;
- `android-v28` APK digest: `4e61d97d49b41555e5b1b02afc249b5dd580c2073ac5f51e25ef62df8ad63434`, built from `5431143…`.

Self-update history:

- run `37307193654`: product update dialog existed, but the old harness looked for title-case `Обновить` while Android rendered `ОБНОВИТЬ`;
- run `37309758124`: case issue fixed; emulator was blocked by an unrelated `Pixel Launcher isn't responding` system ANR;
- run `37310407449`: product dialog was found and confirmed, then the harness incorrectly waited for `update_download_start` before handling the legitimate `install_permission_required` branch;
- run `37311047753`: **PASS**. The emulator upgraded `android-v27 → android-v28` and emitted:
  - `PACKAGE_INSTALLER_HANDOFF_PASS`;
  - `SELF_UPDATE_HANDOFF_PASS versionCode=27->28`;
  - `manifest_sha256_verified=yes`;
  - `product_update_available=yes`;
  - `product_dialog_shown=yes`;
  - `product_dialog_confirmed=yes`;
  - `product_download_verified=yes`;
  - `package_installer_handoff=yes`;
  - `same_signature_in_place_update=yes`;
  - `uid_preserved=yes`.

The harness uses ADB only after the product-owned verified PackageInstaller handoff to prove that the exact already-digest-verified signed APK is accepted as a same-signature in-place update on the hosted emulator. Normal product delivery remains the in-app update route; the final human PackageInstaller confirmation is a physical-device gate covered by V11.

**Status: PASS.**

### V11 — physical handset

No physical Android target is registered in the available DevCoveer capabilities/host inventory; only backend/logs/releases/runtime are exposed.

**Status: PENDING.**

Owner acceptance on the final signed release:

1. Update/install the final signed release through the in-app update route.
2. Start a new Live conversation and speak continuously for five minutes with memorable facts near beginning/middle/end and one explicit correction.
3. Confirm no microphone/session stop around 30/60/90/180/300 seconds.
4. Confirm whether user text appears on the right **before** the utterance ends. Current provider evidence says this V03 condition is not met during uninterrupted speech.
5. Finish the utterance, verify the full final transcript remains available, and continue dialogue without repeating the facts.
6. Start speaking during one Mira response; verify barge-in and check for audible feedback/echo or hidden loss.
7. Record local test time so backend diagnostics can correlate the run without recording speech text in diagnostic fields.

A physical PASS must not be inferred from emulator or injected PCM.

## V01–V12 matrix

| ID | Status | Evidence level | Result |
|---|---|---|---|
| V01 | PASS | executable SDK/ledger | exact 55,286/60,000 regression requests 1,024, not 16,384 |
| V02 | PASS | real provider + real-time PCM | 3 × 300 s; 3000/3000 ACK each; no terminal/denial |
| V03 | BLOCKED | real provider | zero provider interim events; first transcript only after boundary |
| V04 | PASS | executable shared/product contracts | >10k server projection + >4k UI/store/correction/reorder losslessness |
| V05 | PARTIAL | real provider | pause/barge-in lifecycle passes; physical acoustic echo not tested |
| V06 | PASS | executable shared/product failure contract | typed terminal UI + correlated redacted diagnostics |
| V07 | PASS | real provider | fresh Live session reads recovered source and recalls all 3 markers |
| V08 | PASS | real provider + executable mutation contract | replay/source exactly-once plus new task exactly once |
| V09 | PASS | 2-user real provider | two isolated users, 180 s concurrent, cross-read denied |
| V10 | PASS | release + emulator | signed v28 published; self-update run 37311047753 passed v27→v28 with verified manifest/download, PackageInstaller handoff, same-signature update and UID preservation |
| V11 | PENDING | physical | no physical target available in this execution window |
| V12 | PASS on report/checkpoint merge | docs/checkpoint | report contains V01–V11 evidence, limitations and exact next steps; checkpoint is updated after merge |

## CI and local regression

Final acceptance-hardening delivery before the report branch:

- full Projects Hub backend suite: **136 passed**;
- executable web voice/framework suite: **20 passed**;
- production PWA TypeScript/Vite build: PASS;
- voice/Android acceptance scripts: `py_compile` PASS;
- PR #88 backend GitHub check: PASS;
- PR #88 PWA GitHub check: PASS.

PR #84, which delivered the core PH-VOICE implementation, also had green backend/PWA checks before merge.

## Production observability

Production backend diagnostics correlate voice events by safe metadata:

- session/source/attempt ids;
- client/backend versions and backend release SHA;
- event kind/count and provider timestamp;
- resource requested/granted/denied units and code;
- connection generation and transport diagnostics.

Transcript text and audio payload are not copied into the structured diagnostic fields.

## Workspace and disk cleanup

Cleanup was included in the delivery instead of leaving build debris:

- worktrees were reduced to **2**;
- managed source handoffs were reduced to **0**;
- one useful old checkout delta, `docs/telegram-routing.md`, was preserved via PR #85 before cleanup;
- five superseded managed handoff worktrees were removed only after their functionality was verified as present in current Projects Hub;
- 31 stale valid release directories were removed in the first cleanup pass, freeing approximately **6.27 GB**;
- current production and rollback releases were preserved.

After that cleanup, another concurrent Projects Hub task reused the canonical checkout as branch `docs/board-analysis-20261005` with staged unrelated work. PH-VOICE did not reset or delete it. Worktree count remains two; the PH-VOICE/report work stays isolated in `projects-hub-owner`.

## Known limitations and exact next step

1. **V03 is the remaining product-level blocker in provider acceptance.** `gemini-3.8-live` did not emit interim input transcript during uninterrupted speech in the measured conversational path. Do not mask this with capture counters or a second ASR without explicit owner requirement change.
2. **V11 remains pending** until the final signed APK is exercised on a physical handset.
3. If independent audit confirms that low-latency interim input transcription cannot be obtained from the required single-Mira `gemini-3.8-live` path, the next step is a product/architecture decision: obtain an upstream capability that preserves single-Mira semantics, or explicitly change `CORE-CENTRAL-MIRA` before introducing a separate transcription model.
4. If the same 3.8 conversational model can expose interim through a supported provider option not yet enabled, implement only that narrow setup and rerun V03/V11; do not reopen the already-fixed resource cutoff architecture.

## Audit conclusion

The resource-budget 30-second cutoff is fixed and released. Durable source recovery, long-transcript losslessness, pause/barge-in lifecycle, two-user isolation, typed failures, signed Android delivery and release provenance have concrete evidence.

The audit must **not** be marked fully closed until V03 and V11 meet their literal acceptance criteria. V10 is closed by successful signed-release and self-update evidence from run `37311047753`.