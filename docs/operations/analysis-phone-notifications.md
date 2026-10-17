# Analysis result delivery to Android

This is an OS notification backed by the existing persisted JobScheduler job,
not instant remote push. Android schedules checks at a nominal 15-minute period;
Doze, battery restrictions, offline state, force-stop and OEM policy can delay or
prevent them. No Firebase project, persistent socket or new credentials are used.

The paired-device credential authenticates `GET /api/device/analysis/completed`.
The server selects only seven-day `question_result_ready` events addressed to that
actor in that workspace with current project/source access and a published,
verified result artifact. The feed is newest-first, at most 20 records and does
not alter Mira's briefing cursor or claim that Slack delivery succeeded. The
Android development endpoint remains separate: ordinary users can receive their
own results without platform-owner development permissions.

Android keeps an independent device-bound notification receipt set. It delivers
up to three pending results per check, including the first sync after upgrading.
Permission-disabled/channel-disabled and burst-capped results remain retryable
within the feed window. Repeated checks do not repost already accepted notices.
Re-pairing clears the local receipt namespace. Notification body is the project
name, not the analysis text; the lockscreen public version is generic.

Tapping opens an authorized shared PWA result reader using an opaque analysis ID.
Warm Activity taps send only a presentation event, without restarting Live. Cold
starts preserve the target through login. The reader exposes a concise summary,
expandable plain text, verified artifact/source `.md` links and receipt-backed
Slack links when present. It does not announce or consume Mira's briefing itself.
The backend links/briefing patch must ship alongside this reader. Links never
establish that a mutable document remains current.

## Release and acceptance

1. Integrate with latest main without reverting concurrent voice-framework work.
2. Pass backend suite, web TypeScript/build/tests, Android unit/build checks and
   emulator smoke on the exact final commit. Native SDK build is required; source
   contract tests alone are not Android acceptance.
3. Deploy backend + PWA, then let the existing Android release workflow build and
   sign with the existing stable signing material. Verify workflow commit,
   released APK SHA/version and update manifest before asking the owner to update.
4. On a paired physical Android phone, allow notifications, close Activity, create
   a real authorized analysis, await OS scheduling and verify exactly one notice.
   Tap cold and warm, read summary/source/result/Slack links, then ask Mira for a
   brief spoken summary. Repeat check and confirm no duplicate; do not infer this
   acceptance from a mocked result or manually forced scheduler alone.
5. Check disabled/re-enabled notifications, offline/reconnect, reboot/unlock,
   several results in one interval, logout/re-pair, removed project access,
   invalid target, close/reopen, and notification tap during active Live voice.

A true remote-push upgrade is separate: no configured FCM/WebPush provider was
found in this repository. It would require an approved delivery platform,
persistent credential/registration design, device-token storage and revocation,
and user-approved credential/access setup. This change makes no such claim.
