import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
EXPECTED_COMMIT = "c9de297020087235d80ce4155e52f63f88c642bf"


def test_live_framework_candidate_is_immutable_and_pinned():
    package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
    assert package["liveFramework"] == {
        "repository": "https://github.com/onedayonemasterpiece/live-interaction",
        "version": "0.2.6-candidate",
        "commit": EXPECTED_COMMIT,
    }
    dependency = package["dependencies"]["@onedayonemasterpiece/live-interaction"]
    assert dependency == (
        "git+https://github.com/onedayonemasterpiece/live-interaction.git#"
        + EXPECTED_COMMIT
    )


def test_python_runtime_uses_same_live_framework_commit():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (
        "live-interaction @ "
        "git+https://github.com/onedayonemasterpiece/live-interaction.git@"
        + EXPECTED_COMMIT
    ) in pyproject
