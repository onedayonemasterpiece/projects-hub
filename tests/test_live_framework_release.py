import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

LIVE_VERSION = "0.4.0-rc.3"
LIVE_REF = "d221c2e7a81f9dcd991f2f5ca8be4db59c60990c"
LIVE_RELEASE_COMMIT = "d221c2e7a81f9dcd991f2f5ca8be4db59c60990c"
LIVE_ARCHIVE = (
    "https://github.com/onedayonemasterpiece/live-interaction/releases/download/"
    "v0.4.0-rc.3/onedayonemasterpiece-live-interaction-0.4.0-rc.3.tgz"
)
LIVE_SHA256 = "ccc35aac25cf870a8678fa00692fcc368290f2414ecae059b83f5a0d6097f949"
LIVE_INTEGRITY = "sha512-Pra+ZIGKvbRebpNuNs39vOH4ewyfflJvRNv6qCQBIdDfUbV04r4Kzw1tPdPWWNgH1ZOHsCqabvXEnx4n/YImqA=="


def test_browser_live_framework_uses_candidate_version_contract():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    assert package["liveFramework"] == {
        "repository": "https://github.com/onedayonemasterpiece/live-interaction",
        "binding": "browser",
        "version": LIVE_VERSION,
        "ref": LIVE_REF,
        "release_commit": LIVE_RELEASE_COMMIT,
        "channel": "candidate",
    }
    dependency = package["dependencies"]["@onedayonemasterpiece/live-interaction"]
    assert dependency == LIVE_ARCHIVE


def test_python_runtime_uses_same_candidate_version_contract():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    expected = (
        "live-interaction[audio] @ "
        "git+https://github.com/onedayonemasterpiece/live-interaction.git@"
        + LIVE_REF
    )
    assert expected in pyproject
    assert re.search(
        r"live-interaction\[audio\] @ git\+https://github\.com/"
        r"onedayonemasterpiece/live-interaction\.git@[0-9a-f]{40}",
        pyproject,
    )


def test_browser_lock_resolves_version_ref_to_release_commit():
    lock = json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))
    root = lock["packages"][""]
    assert root["dependencies"]["@onedayonemasterpiece/live-interaction"] == LIVE_ARCHIVE
    installed = lock["packages"]["node_modules/@onedayonemasterpiece/live-interaction"]
    assert installed["version"] == LIVE_VERSION
    assert installed["resolved"] == LIVE_ARCHIVE
    assert installed["integrity"] == LIVE_INTEGRITY


def test_release_commit_matches_packaged_native_candidate_and_runtime():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    dependency = package["dependencies"]["@onedayonemasterpiece/live-interaction"]
    assert dependency == LIVE_ARCHIVE
    assert package["liveFramework"]["release_commit"] == LIVE_RELEASE_COMMIT
    android = json.loads((ROOT / "android/live-interaction-candidate.json").read_text())
    assert (android["ref"], android["version"], android["channel"]) == (LIVE_REF, LIVE_VERSION, "candidate")
    assert android["release_commit"] == LIVE_RELEASE_COMMIT
    assert android["artifact_type"] == "npm_tgz" and android["archive_url"] == LIVE_ARCHIVE
    assert android["archive_sha256"] == LIVE_SHA256 and android["archive_bytes"] == 182413
    for name in ("android.yml", "android-release.yml"):
        workflow = (ROOT / ".github/workflows" / name).read_text()
        assert "python scripts/prepare_live_framework.py" in workflow
        assert "repository: onedayonemasterpiece/live-interaction" not in workflow
    assert '"shared_artifact": verify_prepared(' in (ROOT / ".github/workflows/android.yml").read_text()
    from live_interaction import __version__
    assert __version__ == LIVE_VERSION, "Tests must exercise the pinned framework candidate, not a stale installation"


def test_historical_installed_apk_receipt_is_preserved():
    # The user installed the earlier rc.1 APK. Keep that evidence unchanged;
    # the current archive candidate requires its own Android audio CI.
    pairing = json.loads((ROOT / "tests/fixtures/native-v2-installed-candidate.json").read_text())
    android = pairing["framework"]
    assert (android["ref"], android["version"], android["channel"]) == (
        "c564f0c44c6fe783fd4932bd4633fada34e05c73", "0.4.0-rc.1", "candidate"
    )
    assert pairing["consumer_source_head"] == "20020927de8ba733922d763146d71fa75872727e"
    assert pairing["apk"]["apk_sha256"] == "8cc466f881f44c68eec7276de94cacd158b2d557b8cd61330e70691042d04c76"
    assert pairing["apk"]["artifact_id"] == 12099042605
    assert pairing["start_fixture"]["source_ref"] == pairing["consumer_source_head"]
