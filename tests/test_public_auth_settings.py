from pathlib import Path

import pytest

from projects_hub.settings import (
    EXPECTED_AUTH_PROVIDER,
    EXPECTED_AUTH_SUPABASE_URL,
    EXPECTED_PUBLIC_ORIGIN,
    Settings,
)


def base_env(tmp_path: Path) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "session-secret"
    secret.write_text("s" * 48, encoding="utf-8")
    secret.chmod(0o600)
    return {
        "PROJECTS_HUB_DATA_DIR": str(tmp_path / "data"),
        "PROJECTS_HUB_SESSION_SECRET_FILE": str(secret),
    }


def public_env(tmp_path: Path) -> dict[str, str]:
    env = base_env(tmp_path)
    env.update(
        {
            "PROJECTS_HUB_PUBLIC_ORIGIN": EXPECTED_PUBLIC_ORIGIN,
            "PROJECTS_HUB_AUTH_SUPABASE_URL": EXPECTED_AUTH_SUPABASE_URL,
            "PROJECTS_HUB_AUTH_SUPABASE_PUBLISHABLE_KEY":
                "sb_publishable_" + ("x" * 40),
            "PROJECTS_HUB_AUTH_PROVIDER": EXPECTED_AUTH_PROVIDER,
            "PROJECTS_HUB_DEV_AUTH": "1",
        }
    )
    return env


def test_public_auth_config_is_all_or_nothing_and_pinned(tmp_path: Path):
    settings = Settings.from_env(public_env(tmp_path))
    assert settings.public_auth_enabled is True
    assert settings.auth_mode == "public_yandex+loopback_dev"
    assert settings.cookie_secure is True

    partial = base_env(tmp_path / "partial")
    partial["PROJECTS_HUB_PUBLIC_ORIGIN"] = EXPECTED_PUBLIC_ORIGIN
    with pytest.raises(RuntimeError, match="incomplete"):
        Settings.from_env(partial)

    wrong_url = public_env(tmp_path / "wrong-url")
    wrong_url["PROJECTS_HUB_AUTH_SUPABASE_URL"] = "https://other.supabase.co"
    with pytest.raises(RuntimeError, match="unexpected"):
        Settings.from_env(wrong_url)

    wrong_origin = public_env(tmp_path / "wrong-origin")
    wrong_origin["PROJECTS_HUB_PUBLIC_ORIGIN"] = "https://other.kenigevents.ru"
    with pytest.raises(RuntimeError, match="unexpected"):
        Settings.from_env(wrong_origin)

    wrong_provider = public_env(tmp_path / "wrong-provider")
    wrong_provider["PROJECTS_HUB_AUTH_PROVIDER"] = "github"
    with pytest.raises(RuntimeError, match="unexpected"):
        Settings.from_env(wrong_provider)
