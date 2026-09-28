from pathlib import Path

import pytest

from projects_hub.settings import EXPECTED_AUTH_SUPABASE_URL, Settings


def base_env(tmp_path: Path) -> dict[str, str]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "session-secret"
    secret.write_text("s" * 48, encoding="utf-8")
    secret.chmod(0o600)
    return {
        "PROJECTS_HUB_DATA_DIR": str(tmp_path / "data"),
        "PROJECTS_HUB_SESSION_SECRET_FILE": str(secret),
    }


def test_public_auth_config_is_all_or_nothing_and_pinned(tmp_path: Path):
    env = base_env(tmp_path)
    env.update(
        {
            "PROJECTS_HUB_PUBLIC_ORIGIN": "https://projects-hub.kenigevents.ru",
            "PROJECTS_HUB_AUTH_SUPABASE_URL": EXPECTED_AUTH_SUPABASE_URL,
            "PROJECTS_HUB_AUTH_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_" + ("x" * 40),
            "PROJECTS_HUB_AUTH_PROVIDER": "custom:yandex",
        }
    )
    settings = Settings.from_env(env)
    assert settings.public_auth_enabled is True
    assert settings.cookie_secure is True

    partial = base_env(tmp_path / "partial")
    partial["PROJECTS_HUB_PUBLIC_ORIGIN"] = "https://projects-hub.kenigevents.ru"
    with pytest.raises(RuntimeError, match="incomplete"):
        Settings.from_env(partial)

    wrong = dict(env)
    wrong["PROJECTS_HUB_AUTH_SUPABASE_URL"] = "https://other.supabase.co"
    with pytest.raises(RuntimeError, match="unexpected"):
        Settings.from_env(wrong)
