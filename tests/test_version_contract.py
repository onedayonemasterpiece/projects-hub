from __future__ import annotations

import tomllib
from pathlib import Path

from projects_hub.version import __version__

ROOT = Path(__file__).resolve().parents[1]


def test_backend_version_matches_project_release_version():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__ == "0.1.21"
