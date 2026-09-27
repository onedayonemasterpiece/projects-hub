import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
EXPECTED_VERSION = "0.2.5"
EXPECTED_SHA256 = "0f6b8d11b98af14004669812a4512a399aecff7907154c091e5a95e951c9a232"


def test_live_framework_release_is_immutable_and_pinned():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    metadata = package["liveFramework"]
    assert metadata == {
        "repository": "https://github.com/onedayonemasterpiece/live-interaction",
        "version": EXPECTED_VERSION,
        "sha256": EXPECTED_SHA256,
    }
    dependency = package["dependencies"]["@onedayonemasterpiece/live-interaction"]
    assert dependency == f"file:vendor/live-interaction-{EXPECTED_VERSION}.tgz"

    archive = WEB / "vendor" / f"live-interaction-{EXPECTED_VERSION}.tgz"
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == EXPECTED_SHA256
    with tarfile.open(archive, "r:gz") as bundle:
        packed = json.load(bundle.extractfile("package/package.json"))
    assert packed["name"] == "@onedayonemasterpiece/live-interaction"
    assert packed["version"] == EXPECTED_VERSION


def test_python_runtime_uses_same_live_framework_release():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        "live-interaction @ "
        "git+https://github.com/onedayonemasterpiece/live-interaction.git@v0.2.5"
    ) in pyproject
