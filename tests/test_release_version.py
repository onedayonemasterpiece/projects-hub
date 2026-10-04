from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_version_matches_project_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = project["project"]["version"]
    source = (ROOT / "src" / "projects_hub" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', source)
    assert match is not None
    assert match.group(1) == expected
