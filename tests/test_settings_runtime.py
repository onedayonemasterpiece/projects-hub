import os

import pytest

from projects_hub.settings import Settings


def test_session_secret_file_is_supported_without_putting_secret_in_environment(tmp_path):
    secret_file = tmp_path / "session-secret"
    secret_file.write_text("s" * 48, encoding="utf-8")
    os.chmod(secret_file, 0o600)
    settings = Settings.from_env(
        {
            "PROJECTS_HUB_DATA_DIR": str(tmp_path / "data"),
            "PROJECTS_HUB_STATIC_DIR": str(tmp_path / "dist"),
            "PROJECTS_HUB_SESSION_SECRET_FILE": str(secret_file),
            "PROJECTS_HUB_DEV_AUTH": "1",
            "PROJECTS_HUB_COOKIE_SECURE": "0",
            "PROJECTS_HUB_DEPLOY_SHA": "a" * 40,
        }
    )
    assert settings.session_secret == "s" * 48
    assert settings.release_sha == "a" * 40
    assert settings.dev_auth is True
    assert settings.cookie_secure is False


def test_session_secret_file_must_be_private(tmp_path):
    secret_file = tmp_path / "session-secret"
    secret_file.write_text("s" * 48, encoding="utf-8")
    os.chmod(secret_file, 0o644)
    with pytest.raises(RuntimeError):
        Settings.from_env(
            {
                "PROJECTS_HUB_SESSION_SECRET_FILE": str(secret_file),
                "PROJECTS_HUB_DATA_DIR": str(tmp_path / "data"),
            }
        )
