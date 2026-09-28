from pathlib import Path
import os

import pytest

from projects_hub.settings import Settings


def write_private(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)


def base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "PROJECTS_HUB_DEV_AUTH": "1",
        "PROJECTS_HUB_DATA_DIR": str(tmp_path / "data"),
        "PROJECTS_HUB_COOKIE_SECURE": "0",
    }


def test_github_app_settings_are_optional_and_fail_closed_when_partial(tmp_path: Path):
    settings = Settings.from_env(base_env(tmp_path))
    assert settings.github_app_enabled is False

    env = base_env(tmp_path)
    env["PROJECTS_HUB_GITHUB_APP_ID"] = "9001"
    with pytest.raises(RuntimeError, match="GitHub App configuration is incomplete"):
        Settings.from_env(env)


def test_github_app_reads_private_key_and_webhook_secret_only_from_private_files(tmp_path: Path):
    key_file = tmp_path / "github-app.pem"
    webhook_file = tmp_path / "github-webhook-secret"
    write_private(
        key_file,
        "-----BEGIN PRIVATE KEY-----\n"
        + ("A" * 160)
        + "\n-----END PRIVATE KEY-----\n",
    )
    write_private(webhook_file, "w" * 48)

    env = {
        **base_env(tmp_path),
        "PROJECTS_HUB_GITHUB_APP_ID": "9001",
        "PROJECTS_HUB_GITHUB_APP_SLUG": "projects-hub",
        "PROJECTS_HUB_GITHUB_APP_PRIVATE_KEY_FILE": str(key_file),
        "PROJECTS_HUB_GITHUB_APP_WEBHOOK_SECRET_FILE": str(webhook_file),
    }
    settings = Settings.from_env(env)
    assert settings.github_app_enabled is True
    assert settings.github_app_id == 9001
    assert settings.github_app_slug == "projects-hub"
    assert "PRIVATE KEY" in settings.github_app_private_key
    assert settings.github_app_webhook_secret == "w" * 48


def test_github_app_rejects_group_or_world_readable_secret_files(tmp_path: Path):
    key_file = tmp_path / "github-app.pem"
    webhook_file = tmp_path / "github-webhook-secret"
    write_private(
        key_file,
        "-----BEGIN PRIVATE KEY-----\n"
        + ("A" * 160)
        + "\n-----END PRIVATE KEY-----\n",
    )
    write_private(webhook_file, "w" * 48)
    os.chmod(webhook_file, 0o640)

    env = {
        **base_env(tmp_path),
        "PROJECTS_HUB_GITHUB_APP_ID": "9001",
        "PROJECTS_HUB_GITHUB_APP_SLUG": "projects-hub",
        "PROJECTS_HUB_GITHUB_APP_PRIVATE_KEY_FILE": str(key_file),
        "PROJECTS_HUB_GITHUB_APP_WEBHOOK_SECRET_FILE": str(webhook_file),
    }
    with pytest.raises(RuntimeError, match="private regular file"):
        Settings.from_env(env)
