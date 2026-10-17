#!/usr/bin/env bash
set -euo pipefail

# Preserve evidence even when instrumentation fails; do not turn failure green.
collect_smoke_evidence() {
  local status=$?
  mkdir -p android/smoke-evidence
  adb logcat -d -v threadtime > android/smoke-evidence/logcat.txt 2>&1 || true
  adb shell dumpsys activity activities > android/smoke-evidence/activities.txt 2>&1 || true
  return "$status"
}
trap collect_smoke_evidence EXIT

adb_retry() {
  local attempt=0
  while [ "$attempt" -lt 30 ]; do
    if adb "$@"; then
      return 0
    fi
    adb start-server >/dev/null 2>&1 || true
    attempt=$((attempt + 1))
    sleep 2
  done
  return 1
}

gradle -p android assembleDebug --stacktrace
adb_retry wait-for-device

booted=0
for _ in $(seq 1 45); do
  if [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; then
    booted=1
    break
  fi
  adb start-server >/dev/null 2>&1 || true
  sleep 2
done
test "$booted" -eq 1

adb_retry install -r android/app/build/outputs/apk/debug/app-debug.apk
adb_retry shell am start -W -n com.kenigevents.projectshub.debug/com.kenigevents.projectshub.MainActivity
sleep 3
PID="$(adb_retry shell pidof com.kenigevents.projectshub.debug | tr -d '\r')"
test -n "$PID"
echo "ANDROID_LAUNCH_PASS pid=$PID"

# Installed presentation/origin checks; emulator has no audible/physical acceptance.
gradle -p android connectedDebugAndroidTest --stacktrace
