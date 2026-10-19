#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
EVIDENCE="${PH_ANDROID_EVIDENCE_DIR:-$(pwd)/android/smoke-evidence}"
CERTS="${PH_ANDROID_STATE_DIR:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/projects-hub-voice-e2e-tls}"
RESOURCES="$CERTS/resources.tsv"
PACKAGE=com.kenigevents.projectshub.debug
MODE="${1:-run}"
ADB_BINARY="$(type -P adb || true)"
PACTL_BINARY="$(type -P pactl || true)"
pactl() { timeout 10 "$PACTL_BINARY" "$@"; }

require_profile() {
  test "${PH_ANDROID_DISPOSABLE:-}" = 1 || { echo 'Use only a dedicated SDK/disposable AVD; set PH_ANDROID_DISPOSABLE=1.' >&2; return 1; }
  [[ "${ANDROID_HOME:-}" = /* ]] || { echo 'ANDROID_HOME must name the dedicated SDK with an absolute path.' >&2; return 1; }
  [[ "${ANDROID_SERIAL:-}" =~ ^emulator-[0-9]+$ ]] || { echo 'Set ANDROID_SERIAL to the single disposable emulator, e.g. emulator-5554.' >&2; return 1; }
}

if [ "$MODE" = --preflight ]; then
  # Deliberately read-only and usable before apt/pip/npm/SDK setup.
  require_profile
  test "$(uname -s)" = Linux && test -c /dev/kvm && test -r /dev/kvm && test -w /dev/kvm || {
    echo 'Full Android voice E2E requires readable/writable Linux /dev/kvm. Gradle unit/build tasks do not.' >&2
    exit 1
  }
  echo 'ANDROID_VOICE_KVM_PREFLIGHT_PASS'
  exit 0
fi

process_starttime() {
  local record fields
  read -r record < "/proc/$1/stat" || return 1
  record="${record##*) }"
  read -r -a fields <<< "$record"
  printf '%s\n' "${fields[19]}"
}

cleanup_owned() {
  test -f "$RESOURCES" || return 0
  local kind value stamp owner tag modules result=0
  IFS=$'\t' read -r tag owner < "$RESOURCES"
  [[ "$tag" = owner && "$owner" =~ ^ph_e2e_[a-f0-9_]+$ ]] || { echo 'Unrecognised audio ownership file; nothing was stopped.' >&2; return 1; }
  while IFS=$'\t' read -r kind value stamp; do
    case "$kind" in
      owner) ;;
      pulse)
        [[ "$value" =~ ^[0-9]+$ ]] || { result=1; continue; }
        modules="$(pactl list short modules)" || { result=1; continue; }
        # IDs can be reused after a daemon restart. Require this run's marker.
        if awk -F '\t' -v id="$value" -v marker="sink_properties=device.description=$owner" \
            '$1 == id && $2 == "module-null-sink" && index(" " $3 " ", " " marker " ") {found=1} END {exit !found}' <<< "$modules"; then
          pactl unload-module "$value" || result=1
        fi
        ;;
      xvfb)
        # pidfd closes the check/kill PID-reuse race; a changed starttime is not ours.
        python - "$value" "$stamp" <<'PY' || result=1
import os, select, signal, sys
pid, expected = int(sys.argv[1]), sys.argv[2]
try:
    descriptor = os.pidfd_open(pid)
except ProcessLookupError:
    sys.exit(0)
try:
    with open(f"/proc/{pid}/stat") as source:
        actual = source.read().rsplit(") ", 1)[1].split()[19]
    if actual == expected:
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        poll = select.poll()
        poll.register(descriptor, select.POLLIN)
        if not poll.poll(2000):
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
except (FileNotFoundError, ProcessLookupError):
    pass
finally:
    os.close(descriptor)
PY
        ;;
      *) echo 'Unrecognised audio resource; ownership state was retained.' >&2; result=1 ;;
    esac
  done < "$RESOURCES"
  if [ "$result" -eq 0 ]; then rm "$RESOURCES"; fi
  return "$result"
}

if [ "$MODE" = --cleanup ]; then cleanup_owned; exit $?; fi
case "$MODE" in --prepare|run) ;; *) echo 'Usage: android_emulator_smoke.sh [--preflight|--prepare|--cleanup]' >&2; exit 2 ;; esac
require_profile
export ANDROID_SERIAL
umask 077

adb() { timeout "${PH_ADB_TIMEOUT:-20}" "$ADB_BINARY" -s "$ANDROID_SERIAL" "$@"; }

validate_device() {
  local inventory connected
  inventory="$(timeout 10 "$ADB_BINARY" devices)"
  mapfile -t connected < <(awk 'NR > 1 && NF {print $1 " " $2}' <<< "$inventory")
  test "${#connected[@]}" -eq 1 && test "${connected[0]}" = "$ANDROID_SERIAL device" || {
    echo 'Expected exactly the selected disposable emulator; another/offline/unknown device is connected.' >&2
    return 1
  }
  test "$(adb shell getprop ro.kernel.qemu | tr -d '\r')" = 1
  test "$(adb shell getprop ro.build.version.sdk | tr -d '\r')" = 31
}

record_disposable_guest() {
  require_profile
  validate_device
  local inventory name line status reason readback present
  local -a installed=()
  local receipt="$EVIDENCE/guest-background.json" rows="$EVIDENCE/guest-background.tsv"
  test ! -e "$receipt" && test ! -e "$rows" || { echo 'Guest diagnostics already exist; use a fresh run.' >&2; return 1; }
  printf '{"collected":false,"package_mutations":false}\n' > "$receipt"
  : > "$rows"
  if ! inventory="$(adb shell pm list packages --user 0 | tr -d '\r')"; then
    printf '{"collected":false,"package_mutations":false,"failure_reason":"inventory_command_failed"}\n' > "$receipt"
    return 1
  fi
  if [[ -z "$inventory" ]]; then
    printf '{"collected":false,"package_mutations":false,"failure_reason":"inventory_empty"}\n' > "$receipt"
    return 1
  fi
  while IFS= read -r line; do
    if [[ ! "$line" =~ ^package:[a-zA-Z0-9_.]+$ ]]; then
      printf '{"collected":false,"package_mutations":false,"failure_reason":"inventory_invalid"}\n' > "$receipt"
      return 1
    fi
    installed+=("$line")
  done <<< "$inventory"
  # Observe the stock image. Disabling GMS in run 39130565827 left dependent
  # services reporting SERVICE_DISABLED and a Contacts Sync error dialog.
  # These files are diagnostics, not enabled/stopped-state acceptance gates.
  for name in com.google.android.gms com.google.android.googlequicksearchbox; do
    status=absent reason= readback= present=0
    for line in "${installed[@]}"; do
      if [[ "$line" = "package:$name" ]]; then present=1; break; fi
    done
    if [ "$present" -eq 1 ]; then
      readback="$name-dumpsys.txt"
      # Keep full targeted stdout/stderr, including failed commands. The
      # existing adb timeout and per-file limit (at most 2 MiB) remain bounded.
      if (ulimit -f 2048 && adb shell dumpsys package "$name") > "$EVIDENCE/$readback" 2> "$EVIDENCE/$name-dumpsys-error.txt"; then
        status=collected
      else
        status=collection_failed reason=readback_command_failed
      fi
    fi
    printf '%s\t%s\t%s\t%s\n' "$name" "$status" "$reason" "$readback" >> "$rows"
    if [ -n "$reason" ]; then break; fi
  done
  python - "$rows" "$receipt" "$ANDROID_SERIAL" <<'PY'
import json, pathlib, sys
names = ("com.google.android.gms", "com.google.android.googlequicksearchbox")
states = {}
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    name, status, reason, readback = line.split("\t")
    states[name] = {"package": name, "status": status, "failure_reason": reason or None,
                   "readback_file": readback or None}
packages = [states.get(name, {"package": name, "status": "not_attempted"}) for name in names]
collected = all(item["status"] in ("absent", "collected") for item in packages)
pathlib.Path(sys.argv[2]).write_text(json.dumps({
    "collected": collected, "profile": "stock-disposable-api31-google-apis",
    "package_mutations": False, "serial": sys.argv[3], "user_id": 0, "packages": packages,
    "failure_reason": next((item["failure_reason"] for item in packages if item.get("failure_reason")), None),
    "scope": "Read-only package diagnostics; no package-state acceptance predicates.",
}, indent=2) + "\n")
sys.exit(0 if collected else 1)
PY
}

start_screen_recording() {
  # Optional visual diagnostics must not add a software video encoder to the
  # default audio acceptance workload. Step PNGs and sound capture are separate.
  case "${PH_ANDROID_RECORD_SCREEN:-0}" in
    0)
      printf '{"enabled":false,"launch_requested":false,"configured_size":null}\n' > "$EVIDENCE/screen-recording.json"
      return 0 ;;
    1) ;;
    *) echo 'PH_ANDROID_RECORD_SCREEN must be 0 or 1.' >&2; return 2 ;;
  esac
  printf '{"enabled":true,"launch_requested":true,"configured_size":{"width":540,"height":1200},"bit_rate":2000000,"time_limit_seconds":180}\n' > "$EVIDENCE/screen-recording.json"
  PH_ADB_TIMEOUT=190 adb shell screenrecord --size 540x1200 --bit-rate 2000000 --time-limit 180 /sdcard/voice-e2e.mp4 \
    > "$EVIDENCE/screenrecord.log" 2>&1 &
  video_pid=$!
}

prepare() {
  local owner module_id xvfb_pid xvfb_stamp name
  owner="ph_e2e_$(python -c 'import secrets; print(secrets.token_hex(16))')"
  printf 'owner\t%s\n' "$owner" > "$RESOURCES"
  # Separate devices prevent fixture replies from becoming microphone input.
  pulseaudio --start --exit-idle-time=-1 --log-target="file:$EVIDENCE/pulseaudio.log"
  if pactl list short sinks | awk '$2 == "ph_ci_mic" || $2 == "ph_ci_speaker" {found=1} END {exit !found}'; then
    echo 'The ph_ci audio devices already exist; refusing to reuse or remove another run.' >&2
    return 1
  fi
  for name in ph_ci_mic ph_ci_speaker; do
    module_id="$(pactl load-module module-null-sink "sink_name=$name" rate=48000 channels=1 "sink_properties=device.description=$owner")"
    printf 'pulse\t%s\n' "$module_id" >> "$RESOURCES"
  done
  pactl list short sinks > "$EVIDENCE/pulse-sinks.txt"
  pactl list short sources > "$EVIDENCE/pulse-sources.txt"
  test ! -e /tmp/.X99-lock && test ! -S /tmp/.X11-unix/X99 || { echo 'Display :99 already exists; nothing there will be stopped.' >&2; return 1; }
  nohup Xvfb :99 -screen 0 1080x1920x24 -nolisten tcp > "$EVIDENCE/xvfb.log" 2>&1 &
  xvfb_pid=$!
  xvfb_stamp="$(process_starttime "$xvfb_pid")"
  printf 'xvfb\t%s\t%s\n' "$xvfb_pid" "$xvfb_stamp" >> "$RESOURCES"

  # Private keys stay outside evidence; only this job's public CA is packaged.
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -sha256 \
    -subj /CN=ProjectsHub-Emulator-Only-CA \
    -addext basicConstraints=critical,CA:TRUE -addext keyUsage=critical,keyCertSign,cRLSign \
    -keyout "$CERTS/ca.key" -out "$CERTS/ca.pem"
  openssl req -new -newkey rsa:2048 -nodes -sha256 -subj /CN=10.0.2.2 \
    -keyout "$CERTS/server.key" -out "$CERTS/server.csr"
  cat > "$CERTS/server.ext" <<'TLS'
subjectAltName=IP:10.0.2.2,IP:127.0.0.1,DNS:localhost
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
TLS
  openssl x509 -req -in "$CERTS/server.csr" -CA "$CERTS/ca.pem" -CAkey "$CERTS/ca.key" \
    -CAcreateserial -days 2 -sha256 -extfile "$CERTS/server.ext" -out "$CERTS/server.pem"
  mkdir -p android/app/src/voiceE2e/res/raw
  cp "$CERTS/ca.pem" android/app/src/voiceE2e/res/raw/native_test_ca.pem

  make_phrase() {
    espeak-ng -v ru -s 155 -w "$EVIDENCE/audio/$1-raw.wav" "$2"
    ffmpeg -nostdin -hide_banner -loglevel error -y -i "$EVIDENCE/audio/$1-raw.wav" \
      -af volume=0.65 -ac 1 -ar 24000 -c:a pcm_s16le "$EVIDENCE/audio/$1.wav"
    rm "$EVIDENCE/audio/$1-raw.wav"
  }
  make_phrase hello 'Мира, проверка голосового разговора, слышишь меня?'
  make_phrase overlap 'Мира, второй вопрос после восстановления связи'
  make_phrase offline 'Эта запись сделана без соединения и должна сохраниться'
  make_phrase short 'Мира, подтверди одним словом, что слышишь меня.'
  make_phrase tail 'Мира, воспроизведи тестовый сигнал с паузой.'
  make_phrase reply 'Проверка голоса пройдена'
}

fixture_pid= audio_pid= mic_monitor_pid= video_pid= pulse_watch_pid=
device_ready=0
phase=not_started
collect_smoke_evidence() {
  local status=$?
  trap - EXIT
  if [ "$MODE" = --prepare ]; then
    printf '{"phase":"prepare","exit_code":%s}\n' "$status" > "$EVIDENCE/prepare-status.json"
    if [ "$status" -ne 0 ]; then cleanup_owned || true; fi
    exit "$status"
  fi
  if [ -n "$pulse_watch_pid" ]; then kill "$pulse_watch_pid" 2>/dev/null || true; wait "$pulse_watch_pid" 2>/dev/null || true; fi
  if [ "$device_ready" -eq 1 ] && [ -n "$video_pid" ]; then
  adb shell pkill -2 screenrecord >/dev/null 2>&1 || true
  fi
  if [ -n "$audio_pid" ]; then kill -INT "$audio_pid" 2>/dev/null || true; wait "$audio_pid" 2>/dev/null || true; fi
  if [ -n "$mic_monitor_pid" ]; then kill -INT "$mic_monitor_pid" 2>/dev/null || true; wait "$mic_monitor_pid" 2>/dev/null || true; fi
  if [ -n "$video_pid" ]; then wait "$video_pid" 2>/dev/null || true; fi
  if [ "$device_ready" -eq 1 ]; then
  if [ -n "$video_pid" ]; then adb pull /sdcard/voice-e2e.mp4 "$EVIDENCE/voice-e2e.mp4" >/dev/null 2>&1 || true; fi
  adb pull "/sdcard/Android/data/$PACKAGE/files/voice-e2e/." "$EVIDENCE/" >/dev/null 2>&1 || true
  adb exec-out screencap -p > "$EVIDENCE/final-screen.png" 2>/dev/null || true
  adb logcat -d -v threadtime > "$EVIDENCE/logcat.txt" 2>&1 || true
  adb shell dumpsys activity activities > "$EVIDENCE/activities.txt" 2>&1 || true
  adb shell dumpsys media.audio_flinger > "$EVIDENCE/audio-flinger.txt" 2>&1 || true
  adb shell dumpsys media.audio_policy > "$EVIDENCE/audio-policy.txt" 2>&1 || true
  fi
  pactl list source-outputs > "$EVIDENCE/pulse-source-outputs.txt" 2>&1 || true
  pactl list sink-inputs > "$EVIDENCE/pulse-sink-inputs.txt" 2>&1 || true
  curl --silent --show-error --max-time 10 --cacert "$CERTS/ca.pem" \
    https://127.0.0.1:8443/_e2e/state > "$EVIDENCE/fixture-final.json" 2>/dev/null || true
  if [ -n "$fixture_pid" ]; then kill "$fixture_pid" 2>/dev/null || true; wait "$fixture_pid" 2>/dev/null || true; fi
  if ! cleanup_owned && [ "$status" -eq 0 ]; then status=1; fi
  printf '{"phase":"%s","exit_code":%s}\n' "$phase" "$status" > "$EVIDENCE/run-status.json"
  exit "$status"
}

if [ "$MODE" = --prepare ]; then
  if { [ -d "$EVIDENCE" ] && [ -n "$(ls -A "$EVIDENCE")" ]; } || [ -e "$RESOURCES" ]; then
    echo 'Previous evidence/resources exist. Use new PH_ANDROID_EVIDENCE_DIR/PH_ANDROID_STATE_DIR; --cleanup only removes owned processes/modules.' >&2
    exit 1
  fi
  mkdir -p "$EVIDENCE/audio" "$CERTS"
  trap collect_smoke_evidence EXIT
  prepare
  exit 0
fi

test -d "$EVIDENCE" || { echo 'Run --prepare first.' >&2; exit 1; }
# Never reopen a prior fixture DB or combine outputs from two executions.
test ! -e "$EVIDENCE/fixture-data" && test ! -e "$EVIDENCE/run-status.json" || { echo 'This evidence directory was already run; choose a fresh directory.' >&2; exit 1; }
trap collect_smoke_evidence EXIT

phase=device_validation
test -f "$RESOURCES"
test "$(cat "$EVIDENCE/prepare-status.json")" = '{"phase":"prepare","exit_code":0}'
validate_device
device_ready=1
phase=guest_diagnostics
record_disposable_guest
phase=fixture_start
test -f "$CERTS/server.pem"
test -f android/app/src/voiceE2e/res/raw/native_test_ca.pem
python scripts/android_voice_fixture.py --data-dir "$EVIDENCE/fixture-data" \
  --audio-dir "$EVIDENCE/audio" --cert "$CERTS/server.pem" --key "$CERTS/server.key" \
  > "$EVIDENCE/fixture.log" 2>&1 &
fixture_pid=$!
ready=0
for _ in $(seq 1 90); do
  if curl --fail --silent --show-error --max-time 2 --cacert "$CERTS/ca.pem" \
      https://127.0.0.1:8443/_e2e/state > "$EVIDENCE/fixture-initial.json" 2>/dev/null; then ready=1; break; fi
  kill -0 "$fixture_pid"
  sleep 1
 done
test "$ready" -eq 1

adb wait-for-device
test "$(adb shell getprop sys.boot_completed | tr -d '\r')" = 1
"${ANDROID_HOME}/emulator/emulator" -version > "$EVIDENCE/emulator-version.txt" 2>&1
# Configure only this disposable google_apis emulator. API 35 did not auto-load
# Intel HDA after VirtioSndCard was disabled: its HAL silently supplied a 220 Hz
# synthetic microphone and NullSink. Require a real guest card before app tests.
# No app permission, security policy or input implementation is altered here.
adb root > "$EVIDENCE/adb-emulator-root.txt" 2>&1
adb wait-for-device
adb shell cat /proc/asound/cards > "$EVIDENCE/android-audio-cards.txt" 2>&1
if [[ ! "$(< "$EVIDENCE/android-audio-cards.txt")" =~ HDA|Intel ]]; then
  adb shell 'for d in /vendor/lib/modules /vendor_dlkm/lib/modules /lib/modules; do
    if [ -f "$d/snd-hda-intel.ko" ]; then
      modprobe -a -d "$d" snd-hda-intel || exit 1
      exit 0
    fi
  done
  echo "Intel HDA kernel module unavailable in this emulator image" >&2
  exit 1' > "$EVIDENCE/android-audio-module.txt" 2>&1
  adb shell cat /proc/asound/cards > "$EVIDENCE/android-audio-cards.txt" 2>&1
fi
adb shell ls -l /dev/snd > "$EVIDENCE/android-audio-devices.txt" 2>&1
[[ "$(< "$EVIDENCE/android-audio-cards.txt")" =~ HDA|Intel ]]
adb emu avd hostmicon | tee "$EVIDENCE/host-mic.txt"
while IFS= read -r line; do
  if [[ "$line" = KO:* ]]; then exit 1; fi
done < "$EVIDENCE/host-mic.txt"
gradle -p android -PvoiceE2e=true assembleDebug assembleDebugAndroidTest --stacktrace
adb install -r android/app/build/outputs/apk/debug/app-debug.apk
adb shell pm grant "$PACKAGE" android.permission.RECORD_AUDIO
if [ "$(adb shell getprop ro.build.version.sdk | tr -d '\r')" -ge 33 ]; then
  adb shell pm grant "$PACKAGE" android.permission.POST_NOTIFICATIONS
fi
adb logcat -c
ffmpeg -nostdin -hide_banner -loglevel warning -y -f pulse -i ph_ci_speaker.monitor \
  -ac 1 -ar 48000 -c:a pcm_s16le "$EVIDENCE/speaker.wav" > "$EVIDENCE/speaker-capture.log" 2>&1 &
audio_pid=$!
# Independent recording of the signal presented to QEMU distinguishes a silent
# Android capture from a broken host fixture without injecting into the app.
ffmpeg -nostdin -hide_banner -loglevel warning -y -f pulse -i ph_ci_mic.monitor \
  -ac 1 -ar 48000 -c:a pcm_s16le "$EVIDENCE/host-microphone.wav" > "$EVIDENCE/host-microphone-capture.log" 2>&1 &
mic_monitor_pid=$!
start_screen_recording
(
  while true; do
    date -u +%FT%TZ >> "$EVIDENCE/pulse-live-source-outputs.txt"
    pactl list source-outputs >> "$EVIDENCE/pulse-live-source-outputs.txt"
    date -u +%FT%TZ >> "$EVIDENCE/pulse-live-sink-inputs.txt"
    pactl list sink-inputs >> "$EVIDENCE/pulse-live-sink-inputs.txt"
    sleep 2
  done
) &
pulse_watch_pid=$!

# Actual Activity/WebView, AudioRecord, MediaCodec, TLS WSS and AudioTrack.
# Only the server's external AI runner is a controlled fixture.
phase=instrumentation
validate_device
gradle -p android -PvoiceE2e=true connectedDebugAndroidTest --stacktrace
kill -INT "$audio_pid"
wait "$audio_pid" || true
audio_pid=
curl --fail --silent --show-error --cacert "$CERTS/ca.pem" \
  https://127.0.0.1:8443/_e2e/state > "$EVIDENCE/fixture-final.json"
phase=audio_verification
python scripts/android_voice_fixture.py --verify-output "$EVIDENCE/speaker.wav" \
  --audio-dir "$EVIDENCE/audio" --state "$EVIDENCE/fixture-final.json" \
  --report "$EVIDENCE/audio-verification.json"
echo ANDROID_NATIVE_VOICE_FLOW_PASS
phase=completed
