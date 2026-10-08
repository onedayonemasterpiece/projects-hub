from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
MAIN_ACTIVITY = (
    ROOT
    / "android"
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "kenigevents"
    / "projectshub"
    / "MainActivity.java"
)


def test_pending_update_resumes_after_unknown_sources_settings_return():
    source = MAIN_ACTIVITY.read_text(encoding="utf-8")

    assert "private final Runnable pendingUpdateResume" in source
    assert re.search(
        r"protected void onNewIntent\(Intent intent\)[\s\S]*?"
        r"updater\.resumePendingInstall\(\);",
        source,
    )
    assert "main.postDelayed(pendingUpdateResume, 500L);" in source
    assert "main.postDelayed(pendingUpdateResume, 1500L);" in source
    assert re.search(
        r"protected void onPause\(\)[\s\S]*?"
        r"main\.removeCallbacks\(pendingUpdateResume\);",
        source,
    )


def test_android_completion_job_survives_activity_close_without_microphone_or_second_backend():
    root = MAIN_ACTIVITY.parent
    job = (root / "DevelopmentCompletionJob.java").read_text(encoding="utf-8")
    manifest = (ROOT / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    main = MAIN_ACTIVITY.read_text(encoding="utf-8")
    assert "extends JobService" in job
    assert "setPersisted(true)" in job
    assert "setPeriodic(PERIOD_MS)" in job
    assert "15 * 60 * 1000L" in job
    assert 'new SecureStore(context).getDeviceToken()' in job
    assert "isUserUnlocked()" in job
    assert '"api/device/development/completed"' in (root / "ApiClient.java").read_text(encoding="utf-8")
    assert "DevelopmentCompletionJob.schedule(this)" in main
    assert 'android:name=".DevelopmentCompletionJob"' in manifest
    assert "RECEIVE_BOOT_COMPLETED" in manifest
    assert "RECORD_AUDIO" not in job
    assert "Microphone" not in job
