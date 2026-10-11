# Calendar readback and Live reconnect incident

Status: candidate repair verified with real provider; stable pin and APK delivery in progress.

## User-visible impact

Android calendar creation could succeed locally while Mira kept waiting for confirmation. Switching away from the live conversation was followed by a disconnected microphone. Repeated starts sometimes returned a generic 503.

## Evidence and cause boundaries

- Android create-event receipts include `calendar_id` after provider insertion and readback. The deployed backend rejected this field as unknown. The incident log contains a receipt HTTP 400 followed by a claimed result after the tool's eight-second wait. The rejected request body was not retained, so the exact cause of that individual 400 is inferred from the reproduced deployed contract mismatch.
- Pending creation outcomes were presented as a request for human confirmation. No bounded tool exposed the existing command's eventual result to Mira after the first wait.
- Browser sender failure could stop the whole Live session before the socket-close reconnect handler ran. Controlled regression tests reproduce this race. An old socket could also reject a replacement socket's pending frames before checking its generation.
- Resource token-budget errors were flattened into generic provider failures by the shared session host and then lost at the HTTP/client boundary. Logs show repeated full starts and rolling-window denials. There is no evidence of a leaked reservation; quota/admission rules are unchanged.
- The original handset's initial socket loss is not proven by backend logs. Controlled transport tests establish recovery behavior, not physical-device background execution.

## Comparison with previous incidents

- `inc_c30092947653d2bc42a14cf8`: earlier 503 from unsupported provider schema; same visible status, different cause.
- `inc_620a2a6a801dc6c269c428ab`: microphone permission/admission and detached-session reclamation. Preserve same-conversation reclamation and cross-actor isolation.
- `inc_aae28418343acf9cc9ad3812`: provider transcript gaps. Preserve the existing presentation-only caption sidecar and canonical Mira transcript ownership.
- Product PR 157 improved generic failure wording on October 10; it did not repair the October 11 receipt/reconnect paths.

## Candidate repair

- Accept only a bounded integer calendar identifier, retaining strict unknown-field, event-ID and verified-readback validation.
- Bind calendar commands to their authenticated conversation and expose bounded status reads of the same command. Pending is an awaited device result, not a request to manually inspect the calendar.
- Accept a delayed verified applied receipt after an unknown outcome only from the original authenticated device/session with the exact payload and claim hashes. Never requeue or recreate the event.
- Persist an idempotent confirmation in the conversation timeline; reconnect context also reads durable command outcomes. Receipt persistence and timeline upsert are separate writes, so a crash may require receipt replay to repair the timeline.
- Record the existing event card idempotently when a delayed status read confirms success.
- Use the shared framework's same-session reconnect and stale-socket guards. Preserve user Stop and suppress stale audio replay. Forward typed resource errors and bounded retry metadata.
- Provide explicit deployment retention for this incident to preserve all existing rollback releases.

## Acceptance

Automated gates cover Android-shaped receipt validation, actual database close/reopen, wrong claim rejection, conversation isolation, one creation command, idempotent timeline/event card, resource-error HTTP/UI mapping, shared sender/socket races, and retained release history.

Verified candidate gates: 311 backend tests, 67 browser tests, production TypeScript/Vite build, framework contracts/native CI, and Projects Hub PR158 CI. The candidate deployed at exact SHA `458259295e7530a7b3526922ac45f3e016c2e01d` with framework `0.3.28rc1` at `f1ef688de8d2ce3813ce50716727e443771c2b45`.

The real-provider reconnect canary passed: completed first turn, controlled socket close, rotated ticket for the same session, generation-2 attach with cursor, fresh synthetic PCM ACK and distinct transcript/audio response, then Stop/server close. Durable receipt: `job_3ea4352630604f0d8154a941`. This is protocol continuity using prepared silence plus text, not a physical microphone or handset background test.

Stable framework `0.3.28` is published at immutable source `3e675077e30b06002fe6477d8cdd5fbae6c1331e`; its functional code is unchanged from the accepted candidate. Archive SHA256: `90d8161b267c9b52aa7edc43c966b4f3a317ebf7e88835a9f98f0d8de6b86a04`. Framework PR50 and final source CI run38138904434 passed.

Final product release gates: exact deployed SHA health, signed APK/update manifest integrity, and automatic-update workflow result. Release delivery results are recorded in incident `inc_66519d591551a6b378dabd44`.

Rollback: restore the last known-good product release if health, provider recovery or update delivery fails. Preserve prior release directories during incident acceptance. No quota expansion, credential changes, or other consumer deployment is part of this repair.
