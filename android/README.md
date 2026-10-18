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

## Repeatable local and Actions voice checks

Use the Gradle command above for local unit tests and APK compilation; it does
not require KVM. Full microphone/WebView/AudioTrack instrumentation uses the
existing `scripts/android_emulator_smoke.sh`, with a controlled server fixture.
It is separate from a real provider check and from physical handset acceptance.
The current workspace and DevCoveer host have no available KVM. Use Actions for
this full path until a suitable Linux host is available; a Kaggle notebook is
not part of this test setup.

The single candidate profile is JDK 17, Gradle 8.10.2, compile SDK 35, Android
emulator build **15507667** (36.6.11), API **31** `google_apis` / `x86_64`,
`pixel_7`, Intel HDA (`-feature -VirtioSndCard`), Xvfb `:99` and the separate
48 kHz mono `ph_ci_mic` / `ph_ci_speaker` Pulse sinks. Keep the emulator flags
below unchanged between local and Actions runs. PA latency **5 ms** alone did
not fix the full audio gate in run 39115240772. The next candidate additionally
sets `QEMU_AUDIO_TIMER_PERIOD=1000` (Hz, requesting a 1 ms timer); its audio
result is still unverified. These two variables apply only to the emulator,
not to `paplay`, recording tools or the verification script.

On a Linux KVM host, prepare a **dedicated Android SDK and AVD directory** once.
Install platform 35/build-tools 35.0.0, platform-tools, and the API31 Google APIs
x86_64 package with `sdkmanager`; install the same pinned emulator build from
[Google's build archive](https://dl.google.com/android/repository/emulator-linux_x64-15507667.zip).
Use `avdmanager create avd --name ph_voice_e2e --package
'system-images;android-31;google_apis;x86_64' --device pixel_7` once in that
dedicated `ANDROID_AVD_HOME`. The local test only supports this disposable
emulator. Disconnect other adb devices before running it.

Install the same host dependencies as `.github/workflows/android.yml`:
Python 3.12 in a virtual environment, Node 22, PulseAudio tools, ffmpeg,
espeak-ng, Xvfb, OpenSSL, libxcb-cursor0 and ripgrep. The read-only KVM
preflight must precede expensive apt/pip/npm preparation. With the dedicated
paths set, the repeated run uses these existing commands:

```sh
export ANDROID_HOME=/absolute/path/to/ph-android-sdk
export ANDROID_AVD_HOME=/absolute/path/to/ph-android-avds
export ORG_GRADLE_PROJECT_liveInteractionAndroidDir=/absolute/path/to/reviewed/live-interaction/android
export PH_ANDROID_DISPOSABLE=1
export ANDROID_SERIAL=emulator-5554
export PH_ANDROID_IMAGE_CACHE=/absolute/path/to/ph-android-image-cache
export PH_ANDROID_EVIDENCE_DIR="$PWD/android/smoke-evidence/local-$(date -u +%Y%m%dT%H%M%S)-$$"
export PH_ANDROID_STATE_DIR="$(mktemp -d)"
export DISPLAY=:99 QT_QPA_PLATFORM=xcb QEMU_AUDIO_DRV=pa
export QEMU_PA_SOURCE=ph_ci_mic.monitor QEMU_PA_SINK=ph_ci_speaker
export PULSE_SOURCE=ph_ci_mic.monitor PULSE_SINK=ph_ci_speaker

bash scripts/android_emulator_smoke.sh --preflight
python -m pip install -e . 'numpy>=2,<3'
npm ci --prefix web
npm run build --prefix web
bash scripts/android_emulator_smoke.sh --prepare
python scripts/android_emulator_image.py
```

The image helper verifies SHA-256
`7e7081f5784e98dd391ddae52573a75bc1db17a2fd286cb20be46d3eec251f94`
and **1,470,822,277 bytes** before extraction. This exact receipt comes from
run 39112612715, artifact 12131276923. The archive is named `31_r14`, but its
package revision is **12**. A verified cache hit avoids that download; a bad
cache entry is replaced only after a new download verifies. `RUNNER_TEMP` is
optional locally. Do not run `sdkmanager` image updates after this pin step.

In a second terminal with the same environment, keep this emulator in the
foreground so its ownership and shutdown are explicit:

```sh
env PULSE_LATENCY_MSEC=5 QEMU_AUDIO_TIMER_PERIOD=1000 \
  "$ANDROID_HOME/emulator/emulator" -port 5554 -avd ph_voice_e2e \
  -gpu swiftshader_indirect -no-snapshot -wipe-data -no-boot-anim \
  -camera-back none -camera-front none -timezone Etc/UTC \
  -allow-host-audio -feature -VirtioSndCard
```

After that emulator reports boot complete, run in the first terminal:

```sh
env -u PULSE_LATENCY_MSEC -u QEMU_AUDIO_TIMER_PERIOD \
  bash scripts/android_emulator_smoke.sh
bash scripts/android_emulator_smoke.sh --cleanup
```

Stop that foreground emulator after the run. The harness checks the selected
serial, rejects additional/offline devices before `adb root` or installation,
and passes `ANDROID_SERIAL` to Gradle. It rechecks the device before connected
tests. `--prepare` refuses old evidence and outstanding ownership state; a run
refuses an existing fixture DB or prior run receipt. Choose a fresh evidence
and state directory for each attempt. No old recording or database is deleted.
Preparation failure and test exit clean only recorded, identity-checked Pulse
modules and Xvfb. `--cleanup` is also safe to call when preparation or emulator
boot failed: it sends no adb commands and never kills the shared Pulse daemon
or changes its defaults. Temporary CA keys stay outside the evidence directory.

Actions uses the same prepare/run/cleanup commands and uploads evidence even
on failure. It also retains Android test reports and UTP additional output.
Preserve `prepare-status.json` / `run-status.json`, `system-image.json`, logcat,
the two host recordings, fixture state and instrumentation receipts when
reporting a run. A cached archive is a build input, not a cached test result;
keep `-wipe-data -no-snapshot` and the full waveform assertions for acceptance.

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
