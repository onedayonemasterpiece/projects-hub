# Android native voice candidate

This continues the existing Android shell. `MainActivity` and the typed
`projectsHubVoice` bridge own visible user permission requests and UI attachment.
`NativeVoiceService` is the single foreground owner across WebView reloads. The
shared `live-interaction` Android platform SDK owns capture, codec, VAD, playback,
audio focus and the `wl-live-v2` socket. The product supplies authenticated session
routes and a store scoped to actor, workspace and conversation.

The bridge carries commands and presentation events only. It has no audio or
credential parameter. Its exact-origin and main-frame checks are the same trust
boundary as the existing shell. An unavailable native codec/bridge/server profile
is explicit; native mode never silently starts WebView capture or switches to PCM.
`attach`, `state`, document reload and UI detach do not start or stop microphone
capture. Explicit Stop, logout/account change, or a fatal owner error stop audio.
`finish_turn` closes the current speech portion while recording continues.

## Candidate build

This feature branch is paired with the shared `0.4.0-rc.2` source candidate at
`132d59b73a137051904bc75bb03dd7d7d13bc49c`, recorded in
`android/live-interaction-candidate.json`. Its Python and web pins target the
same candidate. These feature-branch bindings do not change the production
deployment or satisfy release qualification.

Android CI and the existing signed release workflow check out that immutable
commit and set `ORG_GRADLE_PROJECT_liveInteractionAndroidDir`. This supplies the
SDK to debug, emulator smoke and signed release builds. A local pairing build
uses the same reviewed checkout through the property below. The source candidate
is therefore a concrete build dependency; stable SDK promotion, provider R1,
physical device acceptance and release approval remain separate qualification
steps.

Requirements: JDK 17, Gradle 8.10.2, Android SDK platform 35 and the build tools
selected by the unchanged Android Gradle Plugin 8.7.3. The minimum SDK remains 26.
Core library desugaring 2.1.5 makes the used collection APIs available on that
minimum API level.

```sh
export JAVA_HOME=/absolute/path/to/jdk-17
export ANDROID_HOME=/absolute/path/to/android-sdk
export GRADLE_USER_HOME=/absolute/path/to/isolated-gradle-cache
export REVIEWED_LIVE_ANDROID=/absolute/path/to/reviewed/live-interaction/android

gradle -p android \
  -PliveInteractionAndroidDir="$REVIEWED_LIVE_ANDROID" \
  --no-daemon --console=plain --max-workers=2 \
  testDebugUnitTest assembleDebug
```

Run from the Projects Hub repository root. `android/app/build/outputs/apk/debug/`
contains the debug APK. Record both source revisions with the build result; a
path alone does not identify the paired SDK. The signed workflow retains its
existing signing key checks, APK verification, update manifest and publication
steps. It is prepared to build this source candidate, but it is not an automatic
RC publication channel: its existing publish step creates a normal release.
Running it and approving that release remain separate delivery decisions.

## Durability and lifecycle boundary

`NativeVoiceStore` retains original compressed input and output in one bounded,
checksummed append-only journal format under the private no-backup directory.
The root lock prevents a second native store owner. Each receipt is returned only
after the record has been synced. Source metadata, codec init and chunk identity
are immutable. An incomplete unreceipted tail is recovered to its verified
prefix; a complete corrupt record fails visibly and is retained for diagnosis.

Input device receipts, server chunk commits and server source-seal receipts are
independent. Output device receipts and actual played positions are independent.
Receiving or scheduling audio never marks it played. Scope isolation covers both
directions and the quota covers retained data from every scope on the device.
The current limit is 128 MiB and 4,096 sources; exhaustion is an explicit storage
error and never evicts uncommitted speech. The candidate retains output originals
locally; a product disposition/retention policy is still required before routine
long-term use can reclaim them safely.

Native owner generation fences late permission/auth/network callbacks. Local
Stop releases hardware before awaiting local encoder finalization and durable
tail writes. Remote session cleanup uses a separate bounded request queue, so a
late HTTP Start response can still be stopped after its UI/API instance closes.
A lost HTTP Start response retains its exact attempt key. A confirmed closed or
missing session, or an explicit closed-attempt receipt, permits a fresh key only
while the same native Start remains active. Stop fences late retry/rotation.

## Verification and remaining acceptance

The local candidate build compiles the actual Android platform SDK and consumer,
processes the manifest/resources, runs Android Gradle unit tests and packages a
debug APK. Store tests cover original-byte recovery, independent receipts and
playback cursors, conflicting retries, codec identity, quota, corruption, scope,
single ownership and the last-chunk-ACK/source-seal crash window. JS bridge tests
cover handshake, accepted intent versus actual state, fresh Stop, reload,
stale owner fencing, finish-turn and rejection of audio in the UI bridge.
`NativeSessionRequestTest` also writes the actual normal/recovery Start JSON to
`android/app/build/native-contract/native-live-start.generated.json`; the shared
fixture is checked against the backend's real `LiveStart` model in
`tests/test_native_start_contract.py`. This checks `realtime`/`buffered` request
semantics independently of APK compilation. Native attempt lifecycle tests cover
uncertain replies, confirmed terminal retries and late terminal replies after Stop.

These checks do not qualify handset behavior. A paired installed APK/server still
must prove actual microphone/codec/AudioTrack operation, screen-off and background
capture, process death with recovery, permission denial, audio focus and device
routes, repeated Start/Stop/reload, constrained bandwidth/jitter/loss/outages and
Wi-Fi/mobile handover, and the original long-session/latency acceptance. A browser
or JVM pass is not evidence for those physical device gates. Provider R1, stable
SDK promotion and release approval remain separate prerequisites for release
qualification.
