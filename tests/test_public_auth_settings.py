from pathlib import Path

import pytest

from projects_hub.settings import Settings


def base_env(tmp_path: Path) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "session-secret"
    secret.write_text("s" * 48, encoding="utf-8")
    secret.chmod(0o600)
    return {
        "PROJECTS_HUB_DATA_DIR": str(tmp_path / "data"),
        "PROJECTS_HUB_SESSION_SECRET_FILE": str(secret),
    }


def test_public_auth_needs_only_the_projects_hub_https_origin(tmp_path: Path):
    env = base_env(tmp_path)
    env["PROJECTS_HUB_PUBLIC_ORIGIN"] = "https://projects-hub.kenigevents.ru"
    settings = Settings.from_env(env)
    assert settings.public_auth_enabled is True
    assert settings.cookie_secure is True

    invalid = dict(env)
    invalid["PROJECTS_HUB_PUBLIC_ORIGIN"] = "http://projects-hub.kenigevents.ru"
    with pytest.raises(RuntimeError, match="HTTPS origin"):
        Settings.from_env(invalid)


def test_no_public_origin_means_no_public_login(tmp_path: Path):
    settings = Settings.from_env(base_env(tmp_path))
    assert settings.public_auth_enabled is False
