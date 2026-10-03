import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

BROWSER_VERSION = "0.3.11"
BROWSER_REF = "v0.3.11"
BROWSER_RELEASE_COMMIT = "129edcd38208e3d3daa4fb033c75288d83575dea"
BROWSER_RELEASE_ASSET_SHA256 = "e713de774b5a7c513f4ec8f5281865a160ebf8ed3878987316eb4bc233a8c58c"

PYTHON_VERSION = "0.3.8"
PYTHON_REF = "v0.3.8"
PYTHON_RELEASE_COMMIT = "f756a90f864bee16a671b54da7a53189e2e4a94e"
PYTHON_RELEASE_ASSET_SHA256 = "99b8a4ea7c04a81062547a6a63b8e161220fb62a3b6d947ddda1954c2a90ab0"


def test_browser_live_framework_uses_versioned_release_contract():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    assert package["liveFramework"] == {
        "repository": "https://github.com/onedayonemasterpiece/live-interaction",
        "binding": "browser",
        "version": BROWSER_VERSION,
        "ref": BROWSER_REF,
        "release_commit": BROWSER_RELEASE_COMMIT,
        "release_asset_sha256": BROWSER_RELEASE_ASSET_SHA256,
    }
    dependency = package["dependencies"]["@onedayonemasterpiece/live-interaction"]
    assert dependency == (
        "git+https://github.com/onedayonemasterpiece/live-interaction.git#"
        + BROWSER_REF
    )
    assert not re.search(r"#[0-9a-f]{40}$", dependency)


def test_python_runtime_keeps_accepted_independent_release_ref():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    expected = (
        "live-interaction @ "
        "git+https://github.com/onedayonemasterpiece/live-interaction.git@"
        + PYTHON_REF
    )
    assert expected in pyproject
    assert not re.search(
        r"live-interaction @ git\+https://github\.com/"
        r"onedayonemasterpiece/live-interaction\.git@[0-9a-f]{40}",
        pyproject,
    )
    assert BROWSER_REF != PYTHON_REF


def test_browser_lock_resolves_versioned_release_to_expected_commit():
    lock = json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))
    root = lock["packages"][""]
    assert root["dependencies"]["@onedayonemasterpiece/live-interaction"].endswith(
        "#" + BROWSER_REF
    )
    installed = lock["packages"]["node_modules/@onedayonemasterpiece/live-interaction"]
    assert installed["version"] == BROWSER_VERSION
    assert installed["resolved"].endswith("#" + BROWSER_RELEASE_COMMIT)


def test_release_evidence_is_binding_specific():
    assert BROWSER_RELEASE_COMMIT != PYTHON_RELEASE_COMMIT
    assert BROWSER_RELEASE_ASSET_SHA256 != PYTHON_RELEASE_ASSET_SHA256