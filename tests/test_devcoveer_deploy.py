import pytest

from deploy.devcoveer_install import DeployError, render_env, select_provider_environment


def test_provider_environment_is_minimal_and_uses_only_projects_hub_fallback():
    selected = select_provider_environment(
        {
            "AI_SUPABASE_URL": "https://authority.example",
            "AI_SUPABASE_SECRET_KEY": "authority-secret",
            "GOOGLE_API_KEY": "wrong-one",
            "GOOGLE_API_KEY2": "wrong-two",
            "GOOGLE_API_KEY3": "wrong-three",
            "GOOGLE_API_KEY4": "projects-hub-fallback",
            "GOOGLE_API_KEY5": "wrong-five",
            "SUPABASE_URL": "unrelated-product-db",
            "SUPABASE_KEY": "unrelated-product-key",
        }
    )
    assert selected == {
        "AI_SUPABASE_URL": "https://authority.example",
        "AI_SUPABASE_SECRET_KEY": "authority-secret",
        "GOOGLE_API_KEY4": "projects-hub-fallback",
    }


def test_provider_environment_requires_shared_authority_pair():
    with pytest.raises(DeployError):
        select_provider_environment({"GOOGLE_API_KEY4": "fallback-only"})


def test_render_env_rejects_multiline_values():
    with pytest.raises(DeployError):
        render_env({"AI_RESOURCE_CONTROL_URL": "one\ntwo"})
