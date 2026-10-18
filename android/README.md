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

This feature branch consumes the published shared
[`v0.4.0-rc.2` SDK archive](https://github.com/onedayonemasterpiece/live-interaction/releases/tag/v0.4.0-rc.2).
`android/live-interaction-candidate.json` records its release source commit
`b8b4ba15cdf1b4cfafc428eb3adec02cb44d92d2`, source tree
`b2de6b175a55f6088e4003d2d5c076655ed4664e`, archive URL, **177,490 bytes** and
SHA-256 `8ff9df3888a0db10367878545b526c370118d262a50bb75c4b4ba63f63ee243a`.

Android build, emulator and signed RC jobs use the same
`scripts/prepare_live_framework.py`. It checks size/SHA before unpacking the
58-file npm archive into `.dependencies/live-interaction`; the existing
Gradle composite build includes its `android/` directory. Web installs that
same release URL with npm-generated SHA-512 integrity in `package-lock.json`.
Python remains an exact Git dependency at the release commit: this npm archive
contains the Python runtime but lacks the root `pyproject.toml` needed to
install it with pip. All three bindings identify one SDK release.

The helper retains the archive and an explicit `npm_tgz` receipt, checks package
identity and every extracted source byte on reuse, and refuses Git checkouts,
foreign directories, links, unsafe paths and extra source files. Ordinary
Gradle output directories may coexist with the verified sources. A download
is bounded to the declared size and time; use `--archive /path/to/sdk.tgz` for
an offline copy with identical validation, or `--verify-only` to verify an
existing preparation. Incomplete preparation is refused on reuse; choose a
fresh destination after an I/O failure. The helper does not delete old SDKs.

This is a published SDK prerelease, and product qualification remains separate.
The successful source-based Android run
[39118401444](https://github.com/onedayonemasterpiece/projects-hub/actions/runs/39118401444)
belongs to application `5b676ed54b8e9a118a6931d9b88a365e64bc9e68` with the
earlier shared source pin. It is preserved as historical evidence. The archive
candidate requires a new Android run of its own exact source and SDK artifact.

Requirements: Python 3.12, JDK 17, Gradle 8.10.2, Android SDK platform 35 and the build tools
selected by the unchanged Android Gradle Plugin 8.7.3. The minimum SDK remains 26.
Core library desugaring 2.1.5 makes the used collection APIs available on that
minimum API level.

```sh
export JAVA_HOME=/absolute/path/to/jdk-17
export ANDROID_HOME=/absolute/path/to/android-sdk
export GRADLE_USER_HOME=/absolute/path/to/isolated-gradle-cache
python scripts/prepare_live_framework.py
export ORG_GRADLE_PROJECT_liveInteractionAndroidDir="$PWD/.dependencies/live-interaction/android"

gradle -p android \
  --no-daemon --console=plain --max-workers=2 \
  testDebugUnitTest assembleDebug
```

Run from the Projects Hub repository root. `android/app/build/outputs/apk/debug/`
contains the debug APK. Record the application source and the helper's archive
receipt with the build result; an archive directory is not a Git checkout.
The signed workflow retains its
existing production package, key and update manifest, with the explicit RC
publication conditions below.

## Controlled signed release candidate

The existing `.github/workflows/android-release.yml` now accepts only a manual
`workflow_dispatch`. A push or merge does not publish an APK. Every publication
from this candidate workflow is a GitHub **prerelease**, explicitly excluded
from latest; stable is not a selectable mode while qualification remains open.

Dispatch from a reviewed publisher ref containing this workflow and its
`scripts/android_release_evidence.py` and `scripts/prepare_live_framework.py`.
Select the full application SHA separately:

```sh
gh workflow run android-release.yml --ref "$REVIEWED_PUBLISHER_REF" \
  -f source_sha="$REVIEWED_SOURCE_SHA" \
  -f android_run_id="$ANDROID_AUDIO_RUN_ID"
```

Set those variables to the reviewed ref, full 40-character lowercase source SHA
and successful Android run ID. The publisher stays in its own checkout; the
selected application is built in `source/`. The manifest, tag and notes record
the actual source checkout SHA, not the dispatch workflow's SHA. Dispatching
an old historical workflow ref would use that historical release logic; use
the current reviewed publisher, not an earlier automatic-release revision.

Before any signing secret is accessed, the validator requires:

- The successful `Android` workflow/run in this repository, successful build and
  API31 emulator jobs, and one unexpired evidence artifact from the current
  attempt, verified against its GitHub SHA-256 and size.
- `checkout.json` agrees with the run and selected candidate head. The actual
  tested checkout tree is resolved through GitHub and must equal the selected
  source tree. A PR merge checkout is accepted only with this tree equality;
  PR head and merge SHA are not treated as interchangeable.
- Successful prepare/completed run receipts, all full waveform/compression/
  reconnect checks, the native UI voice receipt and all six current
  instrumentation cases without failures or skips. Missing receipts, partial
  runs and the older failed audio artifacts stop the release.
- Matching Android archive, Python release commit, web release metadata,
  dependency URL and both lockfile entries, including the archive's computed
  SHA-512. The published SDK tag must still point to its recorded source commit
  and tree, and GitHub must report the pinned archive digest and size. The
  emulator's `shared_artifact` receipt must equal the verified archive used to
  build this APK. An older source-checkout receipt cannot qualify this build.
  The API31 system image must also match its pinned digest; these checks do
  not promote the SDK or application to stable.
- A new, increasing `versionCode` from the same workflow's `GITHUB_RUN_NUMBER`
  and an unused tag. Published stable and RC versions both count. An existing
  tag, draft or release is never overwritten; start a new workflow run for a
  new APK. Publication is serialized and monotonicity is rechecked immediately
  before creation.
- The exact previous production APK `android-v63`: release ID `415723841`,
  asset `650139002`, 1,736,797 bytes, SHA-256
  `ec5ba31cd8a32fb566b74415b6cb59d14b29d2a3863f5889a8f85332c1a5c89f`.
  After verifying these bytes, `apksigner` supplies its public signing
  certificate digest as the anchor; `aapt` verifies package/version identity.

The normal release build keeps `com.kenigevents.projectshub` and the existing
keystore. Only the display version gains `-rc.N` from
`src/projects_hub/version.py`; `versionCode` remains `N`. The signed result must
have that package, version and the same certificate as the pinned production
APK, and must not be debuggable. The decoded keystore is removed even on a
failed build and is never included in artifacts. After publication, the
workflow verifies the tag target, prerelease status, uploaded asset digests and
exclusion from the stable latest feed.

The release carries `projects-hub.apk`, `update.json` and
`release-evidence.json`. The Actions artifact also records release notes and
successful publication readback, when available. Failed-build evidence keeps
its explicit incomplete state; it is not a successful signed release. Raw
provider credentials, the keystore and the reference APK are not uploaded.

Install an RC from its explicit APK link. The existing updater reads
`/releases/latest`, which excludes prereleases, so current stable installations
do not automatically receive an RC. A later stable with the same package/key
and a larger versionCode uses the existing update path. The separate
`.debug` package cannot update to this package in place; its local data is
not transferred or deleted by releasing the signed RC.

This workflow does not deploy backend/PWA or invoke a real provider. The APK
keeps its production `HUB_URL`; verify the paired deployed backend/PWA before
handset use and record the signed APK's post-release Start/Stop smoke.
Emulator acceptance uses the real mic/codec/socket/store/AudioTrack path with
a controlled AI runner. It does not replace physical handset, 90-minute,
unstable-mobile-network or four-voice-with-RAG qualification. These remain
explicit open gates in the RC evidence, alongside SDK promotion and remaining
provider acceptance. Optional captions remain degraded until their own clock
contract is proven. The original specification permits this honest RC when
physical acceptance is unavailable; it does not permit describing the RC as
fully qualified stable. See the [integrated delivery record](../docs/operations/voice-delivery-integration-20261018.md).

The validator's adverse cases are reproducible without Android, signing
secrets, GitHub access or provider traffic:

```sh
python -m pytest -q tests/test_prepare_live_framework.py tests/test_android_release_evidence.py tests/test_release_version.py
```

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
not fix the full audio gate in run 39115240772. The same profile with
`QEMU_AUDIO_TIMER_PERIOD=1000` (Hz, requesting a 1 ms timer) passed the full
audio gate in source-based run 39118401444. Keep that profile for the archive
candidate's fresh acceptance run. These two variables apply only to the
emulator, not to `paplay`, recording tools or the verification script.

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
export ORG_GRADLE_PROJECT_liveInteractionAndroidDir="$PWD/.dependencies/live-interaction/android"
export PH_ANDROID_DISPOSABLE=1
export ANDROID_SERIAL=emulator-5554
export PH_ANDROID_IMAGE_CACHE=/absolute/path/to/ph-android-image-cache
export PH_ANDROID_EVIDENCE_DIR="$PWD/android/smoke-evidence/local-$(date -u +%Y%m%dT%H%M%S)-$$"
export PH_ANDROID_STATE_DIR="$(mktemp -d)"
export DISPLAY=:99 QT_QPA_PLATFORM=xcb QEMU_AUDIO_DRV=pa
export QEMU_PA_SOURCE=ph_ci_mic.monitor QEMU_PA_SINK=ph_ci_speaker
export PULSE_SOURCE=ph_ci_mic.monitor PULSE_SINK=ph_ci_speaker

bash scripts/android_emulator_smoke.sh --preflight
python scripts/prepare_live_framework.py
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
Preserve `prepare-status.json` / `run-status.json`, `system-image.json`,
`checkout.json` with the explicit `shared_artifact` identity, logcat,
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
