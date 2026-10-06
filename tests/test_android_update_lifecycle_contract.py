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
