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


def test_android_release_uses_product_version_name():
    workflow = (ROOT / ".github" / "workflows" / "android-release.yml").read_text(
        encoding="utf-8"
    )
    assert 'VERSION_NAME="0.1.${VERSION_CODE}"' not in workflow
    assert '"versionName": f"0.1.{version_code}"' not in workflow
    assert 'src/projects_hub/version.py' in workflow
    assert 'TITLE="Projects Hub Android ${PRODUCT_VERSION}"' in workflow
