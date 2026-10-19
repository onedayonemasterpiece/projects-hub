"""Clearly distinguish debug and signed app when both are installed."""
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]

def test_debug_variant_has_distinct_launcher_label_release_unchanged():
    app = ROOT / "android/app"
    release = ET.parse(app / "src/main/res/values/strings.xml").getroot()
    debug = ET.parse(app / "src/debug/res/values/strings.xml").getroot()
    def label(root):
        return next((s.text or "").strip() for s in root.findall("string") if s.attrib.get("name")=="app_name")
    assert label(release) == "Projects Hub"
    assert label(debug) == "Projects Hub (DEBUG)"
    assert 'android:label="@string/app_name"' in (app / "src/main/AndroidManifest.xml").read_text()
    gradle = (app / "build.gradle").read_text()
    assert 'applicationIdSuffix ".debug"' in gradle
    assert 'versionNameSuffix "-debug"' in gradle
    assert 'if (!BuildConfig.DEBUG) return false' in (app / "src/main/java/com/kenigevents/projectshub/Updater.java").read_text()
