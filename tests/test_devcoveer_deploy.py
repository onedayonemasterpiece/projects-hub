import os
from pathlib import Path

import pytest

from deploy import devcoveer_install as deploy
from deploy.devcoveer_install import (
    DeployError,
    prune_old_releases,
    render_env,
    select_provider_environment,
)


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


def test_deploy_no_longer_depends_on_supabase_or_yandex_auth_mode():
    source = Path("deploy/devcoveer_install.py").read_text(encoding="utf-8")
    assert '"first_party_invite+loopback_dev"' in source
    assert '"public_yandex+loopback_dev"' not in source
    assert "AUTH_SUPABASE_URL" not in source
    assert "AUTH_PUBLISHABLE_ALIASES" not in source


def _release_dir(root: Path, sha: str, *, mtime_ns: int) -> Path:
    path = root / sha
    path.mkdir()
    marker = path / ".release.json"
    marker.write_text('{"status":"ready"}\n', encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def test_release_retention_preserves_current_and_previous(tmp_path, monkeypatch):
    releases = tmp_path / "releases"
    releases.mkdir()
    current = "1" * 40
    previous = "2" * 40
    old_a = "3" * 40
    old_b = "4" * 40
    for index, sha in enumerate((current, previous, old_a, old_b), 1):
        _release_dir(releases, sha, mtime_ns=index * 1_000_000)

    monkeypatch.setattr(deploy, "RELEASES_ROOT", releases)
    monkeypatch.setattr(deploy, "CURRENT_LINK", tmp_path / "current")

    result = prune_old_releases(current, str(releases / previous))

    assert result["preserved"] == sorted([current, previous])
    assert result["removed"] == sorted([old_a, old_b])
    assert result["skipped"] == []
    assert sorted(path.name for path in releases.iterdir()) == sorted(
        [current, previous]
    )


def test_release_retention_keeps_newest_rollback_when_previous_missing(
    tmp_path, monkeypatch
):
    releases = tmp_path / "releases"
    releases.mkdir()
    current = "a" * 40
    older = "b" * 40
    newest = "c" * 40
    _release_dir(releases, current, mtime_ns=1_000_000)
    _release_dir(releases, older, mtime_ns=2_000_000)
    _release_dir(releases, newest, mtime_ns=3_000_000)

    monkeypatch.setattr(deploy, "RELEASES_ROOT", releases)
    monkeypatch.setattr(deploy, "CURRENT_LINK", tmp_path / "current")

    result = prune_old_releases(current, None)

    assert result["preserved"] == sorted([current, newest])
    assert result["removed"] == [older]
    assert sorted(path.name for path in releases.iterdir()) == sorted([current, newest])


def test_production_devcoveer_escapes_backend_mount_namespace_without_weakening_it():
    installer = Path("deploy/devcoveer_install.py").read_text(encoding="utf-8")
    bridge = Path("scripts/run_devcoveer_mcp.sh").read_text(encoding="utf-8")

    assert '"PrivateTmp=true"' in installer
    assert '"ProtectSystem=strict"' in installer
    assert '"ProtectHome=read-only"' in installer
    assert '"NoNewPrivileges=true"' in installer
    assert '"PROJECTS_HUB_DEVCOVEER_COMMAND"' in installer
    assert 'source/scripts/run_devcoveer_mcp.sh' in installer

    assert "/usr/bin/systemd-run --user --pipe --wait --collect --quiet" in bridge
    assert "/home/dev/.local/bin/codex-mcp-server" in bridge
    assert "ProtectSystem" not in bridge
    assert "ProtectHome" not in bridge
