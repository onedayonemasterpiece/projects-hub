import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
EXPECTED_VERSION = "0.3.8"
EXPECTED_REF = "v0.3.8"
EXPECTED_RELEASE_COMMIT = "f756a90f864bee16a671b54da7a53189e2e4a94e"
EXPECTED_RELEASE_ASSET_SHA256 = "99b8a4ea7c04a81062547a6a63b8e161220fb62a3b6d947ddda1954c2a90ab0"


def test_live_framework_uses_versioned_release_contract():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    assert package["liveFramework"] == {
        "repository": "https://github.com/onedayonemasterpiece/live-interaction",
        "version": EXPECTED_VERSION,
        "ref": EXPECTED_REF,
        "release_commit": EXPECTED_RELEASE_COMMIT,
        "release_asset_sha256": EXPECTED_RELEASE_ASSET_SHA256,
    }
    dependency = package["dependencies"]["@onedayonemasterpiece/live-interaction"]
    assert dependency == (
        "git+https://github.com/onedayonemasterpiece/live-interaction.git#"
        + EXPECTED_REF
    )
    assert not re.search(r"#[0-9a-f]{40}$", dependency)


def test_python_runtime_uses_same_versioned_release_ref():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    expected = (
        "live-interaction @ "
        "git+https://github.com/onedayonemasterpiece/live-interaction.git@"
        + EXPECTED_REF
    )
    assert expected in pyproject
    assert not re.search(
        r"live-interaction @ git\+https://github\.com/"
        r"onedayonemasterpiece/live-interaction\.git@[0-9a-f]{40}",
        pyproject,
    )

def test_browser_lock_resolves_versioned_release_to_expected_commit():
    lock = json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))
    root = lock["packages"][""]
    assert root["dependencies"]["@onedayonemasterpiece/live-interaction"].endswith(
        "#" + EXPECTED_REF
    )
    installed = lock["packages"]["node_modules/@onedayonemasterpiece/live-interaction"]
    assert installed["version"] == EXPECTED_VERSION
    assert installed["resolved"].endswith("#" + EXPECTED_RELEASE_COMMIT)
