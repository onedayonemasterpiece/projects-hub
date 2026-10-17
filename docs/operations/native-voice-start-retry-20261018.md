# Native Live Start: recovery after a failed provider setup

Owner report: 18 October 2026, approximately 00:00–00:03 Kaliningrad time
(17 October, 22:00–22:03 UTC). This continues the same Claude native voice work
and the paired backend/PWA delivery in PR #183.

## Observed failure

The installed native candidate now reaches recording and shows `recording_only`
or `connecting`, rather than the previous pre-HTTP microphone error. Actual
production logs at release `9cdc9c68592767e75d48c4510dc2865e898ffed7` show a
provider setup `TimeoutError`, HTTP Start 400 after 117,036 ms, and a separate
Start receiving `LIVE_BUSY` / 429. Some unrelated API reads also took about
15 seconds. The asyncio logger reported a fatal SSL-protocol error, but the
previous JSON formatter discarded the actual exception type and traceback.

The configured readiness wait is 30 seconds, but initialization happens before
it and cleanup is awaited afterward. These observations do not establish whether
the long wall time came from host pressure, a blocked event loop, provider
connectivity or cancellation cleanup. They do not establish a codec defect or a
Python/uvloop incompatibility.

## Changes

- A retry of an already completed failed Start receives `LIVE_ATTEMPT_CLOSED`
  only when no matching session or managed cleanup remains active. The existing
  installed APK already understands this response and makes one new attempt.
- In-flight retries still await the same task. A lost HTTP response, an explicit
  Stop and the scope/identity fences retain their existing semantics. A failed
  attempt receipt is not evicted or reused to start another provider.
- Provider setup timeouts now return HTTP 503 with the Android-safe code
  `LIVE_PROVIDER_SETUP_TIMEOUT` and a bounded retry hint. They no longer look
  like malformed requests or expose raw exception messages.
- Start logs distinguish host initialization from provider setup. Exception
  logs keep the exception type and at most eight source-coordinate frames;
  they do not add exception text, source lines, frame locals or credentials.

Android, the shared SDK pin, transport formats, admission limits, requirements
and deployment mechanism are unchanged. This change does not claim that all
117 seconds have been explained or that a provider outage has been repaired.

## Verification before publication

Fourteen focused tests passed: completed-failure reconciliation, an actual held
runner cleanup, duplicate requests, successful response reconciliation, Stop and
scope fences, safe exception logging, and HTTP timeout classification. A new
attempt remains denied while cleanup is unfinished; after completion it can
succeed without concurrent synthetic providers.

The local broader TestClient/WSS run could not complete: a separate process
importing only AnyIO also hung on `BlockingPortal.call(lambda: 42)`. Both probes
were terminated by their explicit time limits. This is not reported as a
passing WSS check or attributed to the admission change. Full regression
acceptance must come from CI or an independently functioning test environment.

A single read-only measurement on the deployed Python 3.14.4 environment
completed in 774.375 ms. It found shared `c564f0c` / 0.4.0rc1, PyAV 18.1.0,
resource SDK `6ec1929` / 0.1.15, and working Opus/AAC codec constructors.
It made no provider requests. The measured imports and constructors did not
reproduce the 117-second delay. Current memory pressure is relevant operational
evidence, not proof of the earlier failure's cause.

## Delivery and acceptance

Use the existing exact-SHA installer after CI, retaining all current data and
the existing compatible release. Do not restore the pre-v2 SQLite snapshot over
new recordings. Verify the deployed SHA and perform one bounded existing Live
canary to test provider readiness and a real response before asking the owner
to retry the installed APK. Keep the physical-phone incident open until that
retry confirms conversation and Stop.

Four simultaneous speakers remain the agreed initial whole-host Live + captions
+ regional RAG measurement target, not a measured capacity claim. A backend
rewrite is not justified by this failure evidence.
