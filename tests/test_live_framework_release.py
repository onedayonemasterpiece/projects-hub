import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"

LIVE_VERSION = "0.3.20"
LIVE_REF = "v0.3.20"
LIVE_RELEASE_COMMIT = "b036c298cfe9600b86a9a599f1281bfe5340acb2"


def test_browser_live_framework_uses_stable_version_contract():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    assert package["liveFramework"] == {
        "repository": "https://github.com/onedayonemasterpiece/live-interaction",
        "binding": "browser",
        "version": LIVE_VERSION,
        "ref": LIVE_REF,
        "release_commit": LIVE_RELEASE_COMMIT,
    }
    dependency = package["dependencies"]["@onedayonemasterpiece/live-interaction"]
    assert dependency.endswith("#" + LIVE_REF)
    assert not re.search(r"#[0-9a-f]{40}$", dependency)


def test_python_runtime_uses_same_stable_version_contract():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    expected = (
        "live-interaction @ "
        "git+https://github.com/onedayonemasterpiece/live-interaction.git@"
        + LIVE_REF
    )
    assert expected in pyproject
    assert not re.search(
        r"live-interaction @ git\+https://github\.com/"
        r"onedayonemasterpiece/live-interaction\.git@[0-9a-f]{40}",
        pyproject,
    )


def test_browser_lock_resolves_version_ref_to_release_commit():
    lock = json.loads((WEB / "package-lock.json").read_text(encoding="utf-8"))
    root = lock["packages"][""]
    assert root["dependencies"]["@onedayonemasterpiece/live-interaction"].endswith(
        "#" + LIVE_REF
    )
    installed = lock["packages"]["node_modules/@onedayonemasterpiece/live-interaction"]
    assert installed["version"] == LIVE_VERSION
    assert installed["resolved"].endswith("#" + LIVE_RELEASE_COMMIT)


def test_release_commit_is_provenance_not_dependency_interface():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    dependency = package["dependencies"]["@onedayonemasterpiece/live-interaction"]
    assert LIVE_RELEASE_COMMIT not in dependency
    assert package["liveFramework"]["release_commit"] == LIVE_RELEASE_COMMIT
