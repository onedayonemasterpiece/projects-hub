#!/usr/bin/env bash
set -euo pipefail

EVIDENCE="$(pwd)/android/smoke-evidence"
CERTS="${RUNNER_TEMP:-/tmp}/projects-hub-voice-e2e-tls"
PACKAGE=com.kenigevents.projectshub.debug
mkdir -p "$EVIDENCE/audio" "$CERTS"

if [ "${1:-}" = --prepare ]; then
  # Separate devices prevent fixture replies from becoming microphone input.
  pulseaudio --start --exit-idle-time=-1 --log-target="file:$EVIDENCE/pulseaudio.log"
  pactl load-module module-null-sink sink_name=ph_ci_mic rate=48000 channels=1
  pactl load-module module-null-sink sink_name=ph_ci_speaker rate=48000 channels=1
  pactl set-default-source ph_ci_mic.monitor
  pactl set-default-sink ph_ci_speaker
  pactl list short sinks > "$EVIDENCE/pulse-sinks.txt"
  pactl list short sources > "$EVIDENCE/pulse-sources.txt"
  nohup Xvfb :99 -screen 0 1080x1920x24 -nolisten tcp > "$EVIDENCE/xvfb.log" 2>&1 &

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
  make_phrase reply 'Проверка голоса пройдена'
  exit 0
fi

fixture_pid= audio_pid= mic_monitor_pid= video_pid= pulse_watch_pid=
collect_smoke_evidence() {
  local status=$?
  trap - EXIT
  if [ -n "$pulse_watch_pid" ]; then kill "$pulse_watch_pid" 2>/dev/null || true; wait "$pulse_watch_pid" 2>/dev/null || true; fi
  adb shell pkill -2 screenrecord >/dev/null 2>&1 || true
  if [ -n "$audio_pid" ]; then kill -INT "$audio_pid" 2>/dev/null || true; wait "$audio_pid" 2>/dev/null || true; fi
  if [ -n "$mic_monitor_pid" ]; then kill -INT "$mic_monitor_pid" 2>/dev/null || true; wait "$mic_monitor_pid" 2>/dev/null || true; fi
  if [ -n "$video_pid" ]; then wait "$video_pid" 2>/dev/null || true; fi
  adb pull /sdcard/voice-e2e.mp4 "$EVIDENCE/voice-e2e.mp4" >/dev/null 2>&1 || true
  adb pull "/sdcard/Android/data/$PACKAGE/files/voice-e2e/." "$EVIDENCE/" >/dev/null 2>&1 || true
  adb exec-out screencap -p > "$EVIDENCE/final-screen.png" 2>/dev/null || true
  adb logcat -d -v threadtime > "$EVIDENCE/logcat.txt" 2>&1 || true
  adb shell dumpsys activity activities > "$EVIDENCE/activities.txt" 2>&1 || true
  adb shell dumpsys media.audio_flinger > "$EVIDENCE/audio-flinger.txt" 2>&1 || true
  adb shell dumpsys media.audio_policy > "$EVIDENCE/audio-policy.txt" 2>&1 || true
  pactl list source-outputs > "$EVIDENCE/pulse-source-outputs.txt" 2>&1 || true
  pactl list sink-inputs > "$EVIDENCE/pulse-sink-inputs.txt" 2>&1 || true
  curl --silent --show-error --max-time 10 --cacert "$CERTS/ca.pem" \
    https://127.0.0.1:8443/_e2e/state > "$EVIDENCE/fixture-final.json" 2>/dev/null || true
  if [ -n "$fixture_pid" ]; then kill "$fixture_pid" 2>/dev/null || true; wait "$fixture_pid" 2>/dev/null || true; fi
  exit "$status"
}
trap collect_smoke_evidence EXIT

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
if ! rg -q 'HDA|Intel' "$EVIDENCE/android-audio-cards.txt"; then
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
rg -q 'HDA|Intel' "$EVIDENCE/android-audio-cards.txt"
adb emu avd hostmicon | tee "$EVIDENCE/host-mic.txt"
if rg -q '^KO:' "$EVIDENCE/host-mic.txt"; then exit 1; fi
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
adb shell screenrecord --bit-rate 2000000 --time-limit 180 /sdcard/voice-e2e.mp4 \
  > "$EVIDENCE/screenrecord.log" 2>&1 &
video_pid=$!
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
gradle -p android -PvoiceE2e=true connectedDebugAndroidTest --stacktrace
kill -INT "$audio_pid"
wait "$audio_pid" || true
audio_pid=
curl --fail --silent --show-error --cacert "$CERTS/ca.pem" \
  https://127.0.0.1:8443/_e2e/state > "$EVIDENCE/fixture-final.json"
python scripts/android_voice_fixture.py --verify-output "$EVIDENCE/speaker.wav" \
  --audio-dir "$EVIDENCE/audio" --state "$EVIDENCE/fixture-final.json" \
  --report "$EVIDENCE/audio-verification.json"
echo ANDROID_NATIVE_VOICE_FLOW_PASS
