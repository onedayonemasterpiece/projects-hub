#!/usr/bin/env python3
"""Install one exact Projects Hub Git revision on DevCoveer.

The installer is deliberately product-specific. It never prints credentials and
never deploys the mutable working tree: the release source is produced by
`git archive <exact sha>`, built in its own venv, then activated atomically.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

REPOSITORY = "onedayonemasterpiece/projects-hub"
SERVICE = "projects-hub.service"
PORT = 8196
AI_RESOURCE_CONTROL_SHA = "6ec1929dc3183f4bafb112de96ee0571334c0b40"
AI_RESOURCE_CONTROL_URL = (
    "git+https://github.com/onedayonemasterpiece/"
    f"ai-resource-control.git@{AI_RESOURCE_CONTROL_SHA}"
)
PUBLIC_ORIGIN = "https://projects-hub.kenigevents.ru"
REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASES_ROOT = Path("/home/dev/.local/share/projects-hub/releases")
CURRENT_LINK = Path("/home/dev/.local/share/projects-hub/current")
STATE_ROOT = Path("/home/dev/.local/state/projects-hub")
DATA_ROOT = STATE_ROOT / "data"
LOG_ROOT = STATE_ROOT / "logs"
BACKEND_LOG = LOG_ROOT / "backend.jsonl"
PROVIDER_ENV = STATE_ROOT / "providers.env"
SERVICE_ENV = STATE_ROOT / "service.env"
SESSION_SECRET_FILE = STATE_ROOT / "session-secret"
DEPLOY_LOCK = STATE_ROOT / "deploy.lock"
DEPLOY_LOCK_TIMEOUT_SECONDS = 15 * 60
HOST_ENV = Path("/home/dev/.env")
UNIT_ROOT = Path.home() / ".config/systemd/user"
UNIT_FILE = UNIT_ROOT / SERVICE
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RELEASE_RETENTION_COUNT = 2
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

PROVIDER_KEYS = (
    "AI_RESOURCE_CONTROL_URL",
    "AI_RESOURCE_CONTROL_SERVICE_KEY",
    "GOOGLE_AI_LIMITER_SUPABASE_URL",
    "GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY",
    "AI_SUPABASE_URL",
    "AI_SUPABASE_SECRET_KEY",
    "AI_RESOURCE_LEDGER_ID",
    "GOOGLE_API_KEY4",
)


class DeployError(RuntimeError):
    pass


def _safe_output(value: str) -> str:
    value = re.sub(r"(?i)Bearer\s+[^\s]+", "Bearer [redacted]", value)
    value = re.sub(
        r"(?i)((?:token|secret|password|api[_-]?key)[=:]\s*)[^\s]+",
        r"\1[redacted]",
        value,
    )
    return value[:2400]


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: int = 180,
    sensitive: bool = False,
) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"command unavailable: {argv[0]} ({type(exc).__name__})") from None
    if result.returncode:
        detail = "sensitive command failed" if sensitive else _safe_output(result.stdout or "")
        raise DeployError(f"{argv[0]} failed ({result.returncode}): {detail}") from None
    return result.stdout or ""


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@contextlib.contextmanager
def deployment_lock(timeout_seconds: float = DEPLOY_LOCK_TIMEOUT_SECONDS):
    """Serialize exact-SHA build/activate/rollback transactions across processes."""

    timeout_seconds = float(timeout_seconds)
    if timeout_seconds <= 0:
        raise DeployError("deploy lock timeout must be positive")

    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    fd = os.open(
        DEPLOY_LOCK,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    acquired = False
    try:
        os.fchmod(fd, 0o600)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise DeployError(
                        f"deploy lock failed: {type(exc).__name__}"
                    ) from None
                if time.monotonic() >= deadline:
                    raise DeployError(
                        "another Projects Hub deploy is still running"
                    ) from None
                time.sleep(0.25)
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def _parse_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise DeployError(f"required host environment is unavailable: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not ENV_KEY_RE.fullmatch(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def select_provider_environment(values: Mapping[str, str]) -> dict[str, str]:
    selected = {
        key: str(values.get(key) or "").strip()
        for key in PROVIDER_KEYS
        if str(values.get(key) or "").strip()
    }
    explicit = bool(
        selected.get("AI_RESOURCE_CONTROL_URL")
        and selected.get("AI_RESOURCE_CONTROL_SERVICE_KEY")
    )
    host_alias = bool(
        selected.get("AI_SUPABASE_URL") and selected.get("AI_SUPABASE_SECRET_KEY")
    )
    compatibility = bool(
        selected.get("GOOGLE_AI_LIMITER_SUPABASE_URL")
        and selected.get("GOOGLE_AI_LIMITER_SUPABASE_SERVICE_KEY")
    )
    if not (explicit or host_alias or compatibility):
        raise DeployError("shared ai-resource-control authority binding is unavailable")
    return selected


def render_env(values: Mapping[str, str]) -> str:
    lines: list[str] = []
    for key in sorted(values):
        if not ENV_KEY_RE.fullmatch(key):
            raise DeployError(f"invalid environment key: {key}")
        value = str(values[key])
        if "\n" in value or "\r" in value or "\x00" in value:
            raise DeployError(f"invalid multiline environment value: {key}")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    return "\n".join(lines) + "\n"


def ensure_session_secret() -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    if SESSION_SECRET_FILE.exists():
        if SESSION_SECRET_FILE.is_symlink() or not SESSION_SECRET_FILE.is_file():
            raise DeployError("unsafe Projects Hub session-secret file")
        if _mode(SESSION_SECRET_FILE) != 0o600:
            raise DeployError("Projects Hub session-secret permissions must be 0600")
        if len(SESSION_SECRET_FILE.read_text(encoding="utf-8").strip()) < 32:
            raise DeployError("Projects Hub session-secret is too short")
        return
    private_write(SESSION_SECRET_FILE, secrets.token_urlsafe(48))


def systemd_env() -> dict[str, str]:
    uid = os.getuid()
    runtime = Path(f"/run/user/{uid}")
    bus = runtime / "bus"
    if not bus.exists():
        raise DeployError("user-systemd bus is unavailable")
    env = os.environ.copy()
    env["HOME"] = str(Path.home())
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def resolve_sha(value: str) -> str:
    if not SHA_RE.fullmatch(value):
        raise DeployError("release SHA must be exact 40-hex")
    resolved = run(["git", "rev-parse", f"{value}^{{commit}}"], cwd=REPO_ROOT, timeout=30).strip()
    if resolved != value:
        raise DeployError("release SHA does not resolve exactly in this repository")
    return resolved


def _safe_release_dir(sha: str) -> Path:
    path = RELEASES_ROOT / sha
    if path.parent != RELEASES_ROOT or not SHA_RE.fullmatch(path.name):
        raise DeployError("unsafe release path")
    return path


def _release_sha_from_link_target(target: str | None) -> str | None:
    if not target:
        return None
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = CURRENT_LINK.parent / candidate
    try:
        relative = candidate.resolve(strict=False).relative_to(
            RELEASES_ROOT.resolve(strict=False)
        )
    except (OSError, ValueError):
        return None
    if len(relative.parts) != 1 or not SHA_RE.fullmatch(relative.name):
        return None
    return relative.name


def prune_old_releases(
    current_sha: str,
    previous_target: str | None,
    *,
    keep_count: int = RELEASE_RETENTION_COUNT,
) -> dict[str, list[str]]:
    """Best-effort release retention after successful production readback.

    Only exact 40-hex directories directly under RELEASES_ROOT are candidates.
    The current release and previous symlink target are always preserved. When
    there is no distinct previous target, the newest other release is kept as a
    rollback candidate.
    """

    if not SHA_RE.fullmatch(current_sha):
        raise DeployError("current release SHA is invalid for retention")
    keep_count = max(2, int(keep_count))
    if not RELEASES_ROOT.is_dir():
        return {"removed": [], "skipped": [], "preserved": [current_sha]}

    entries = [
        entry
        for entry in RELEASES_ROOT.iterdir()
        if not entry.is_symlink()
        and entry.is_dir()
        and SHA_RE.fullmatch(entry.name)
    ]
    preserve = {current_sha}
    previous_sha = _release_sha_from_link_target(previous_target)
    if previous_sha:
        preserve.add(previous_sha)

    if len(preserve) < keep_count:
        extras = sorted(
            (entry for entry in entries if entry.name not in preserve),
            key=lambda entry: entry.stat().st_mtime_ns,
            reverse=True,
        )
        for entry in extras[: keep_count - len(preserve)]:
            preserve.add(entry.name)

    removed: list[str] = []
    skipped: list[str] = []
    for entry in entries:
        if entry.name in preserve:
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            skipped.append(entry.name)
        else:
            removed.append(entry.name)
    return {
        "removed": sorted(removed),
        "skipped": sorted(skipped),
        "preserved": sorted(preserve),
    }


def _archive_source(sha: str, destination: Path) -> None:
    archive = destination.parent / ".source.tar"
    with archive.open("wb") as handle:
        result = subprocess.run(
            ["git", "archive", "--format=tar", sha],
            cwd=REPO_ROOT,
            stdout=handle,
            stderr=subprocess.PIPE,
            timeout=60,
            check=False,
        )
    try:
        if result.returncode:
            raise DeployError(
                f"git archive failed ({result.returncode}): "
                + _safe_output(result.stderr.decode("utf-8", "replace"))
            )
        destination.mkdir(parents=True, exist_ok=False)
        with tarfile.open(archive, "r:") as bundle:
            bundle.extractall(destination, filter="data")
    finally:
        with contextlib.suppress(FileNotFoundError):
            archive.unlink()


def _build_release(sha: str) -> Path:
    RELEASES_ROOT.mkdir(parents=True, exist_ok=True)
    release = _safe_release_dir(sha)
    marker = release / ".release.json"
    if marker.is_file():
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            state = {}
        if state.get("sha") == sha and state.get("status") == "ready":
            return release
    if release.exists():
        shutil.rmtree(release)
    release.mkdir(mode=0o755)
    source = release / "source"
    _archive_source(sha, source)
    venv = release / "venv"
    run(["/usr/bin/python3", "-m", "venv", str(venv)], timeout=180)
    python = venv / "bin/python"
    run(
        [str(python), "-m", "pip", "install", str(source)],
        timeout=360,
        sensitive=True,
    )
    run(
        [str(python), "-m", "pip", "install", AI_RESOURCE_CONTROL_URL],
        timeout=360,
        sensitive=True,
    )
    run(["/usr/local/bin/npm", "ci"], cwd=source / "web", timeout=360)
    run(["/usr/local/bin/npm", "run", "build"], cwd=source / "web", timeout=360)
    if not (source / "web/dist/index.html").is_file():
        raise DeployError("PWA build did not produce web/dist/index.html")
    marker.write_text(
        json.dumps(
            {
                "sha": sha,
                "status": "ready",
                "ai_resource_control_sha": AI_RESOURCE_CONTROL_SHA,
                "built_at": int(time.time()),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return release


def _replace_current(release: Path) -> str | None:
    previous = None
    if CURRENT_LINK.is_symlink():
        previous = os.readlink(CURRENT_LINK)
    elif CURRENT_LINK.exists():
        raise DeployError("Projects Hub current path exists and is not a symlink")
    CURRENT_LINK.parent.mkdir(parents=True, exist_ok=True)
    temp = CURRENT_LINK.parent / f".current-{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        temp.unlink()
    os.symlink(str(release), temp)
    os.replace(temp, CURRENT_LINK)
    return previous


def _restore_current(previous: str | None) -> None:
    if previous is None:
        with contextlib.suppress(FileNotFoundError):
            CURRENT_LINK.unlink()
        return
    temp = CURRENT_LINK.parent / f".rollback-{os.getpid()}"
    with contextlib.suppress(FileNotFoundError):
        temp.unlink()
    os.symlink(previous, temp)
    os.replace(temp, CURRENT_LINK)


def write_runtime_environment(sha: str) -> bytes | None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    DATA_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    LOG_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    os.chmod(DATA_ROOT, 0o700)
    os.chmod(LOG_ROOT, 0o700)
    host_values = _parse_env(HOST_ENV)
    provider = select_provider_environment(host_values)
    private_write(PROVIDER_ENV, render_env(provider))
    ensure_session_secret()
    old_service = SERVICE_ENV.read_bytes() if SERVICE_ENV.is_file() else None
    private_write(
        SERVICE_ENV,
        render_env(
            {
                "PROJECTS_HUB_DATA_DIR": str(DATA_ROOT),
                "PROJECTS_HUB_STATIC_DIR": str(CURRENT_LINK / "source/web/dist"),
                "PROJECTS_HUB_SESSION_SECRET_FILE": str(SESSION_SECRET_FILE),
                "PROJECTS_HUB_DEVCOVEER_COMMAND": str(
                    CURRENT_LINK / "source/scripts/run_devcoveer_mcp.sh"
                ),
                "PROJECTS_HUB_DEV_AUTH": "1",
                "PROJECTS_HUB_COOKIE_SECURE": "1",
                "PROJECTS_HUB_LIVE_MODEL": "gemini-3.8-live",
                "PROJECTS_HUB_DEPLOY_SHA": sha,
                "PROJECTS_HUB_LOG_FILE": str(BACKEND_LOG),
                "PROJECTS_HUB_PORT": str(PORT),
                "PROJECTS_HUB_PUBLIC_ORIGIN": PUBLIC_ORIGIN,
            }
        ),
    )
    return old_service


def restore_service_environment(content: bytes | None) -> None:
    if content is None:
        with contextlib.suppress(FileNotFoundError):
            SERVICE_ENV.unlink()
        return
    private_write(SERVICE_ENV, content.decode("utf-8"))


def install_unit() -> None:
    UNIT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    unit = "\n".join(
        (
            "[Unit]",
            "Description=Projects Hub backend (exact DevCoveer release)",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"WorkingDirectory={CURRENT_LINK}/source",
            f"EnvironmentFile={PROVIDER_ENV}",
            f"EnvironmentFile={SERVICE_ENV}",
            f"ExecStart={CURRENT_LINK}/venv/bin/python {CURRENT_LINK}/source/scripts/run_server.py",
            "Restart=on-failure",
            "RestartSec=3",
            "TimeoutStopSec=30",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            f"ReadWritePaths={STATE_ROOT}",
            "UMask=0077",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        )
    )
    private_write(UNIT_FILE, unit)
    env = systemd_env()
    run(["systemctl", "--user", "daemon-reload"], env=env, timeout=30)
    run(["systemctl", "--user", "enable", SERVICE], env=env, timeout=30)


def restart_service() -> None:
    run(["systemctl", "--user", "restart", SERVICE], env=systemd_env(), timeout=90)


def stop_service() -> None:
    with contextlib.suppress(DeployError):
        run(["systemctl", "--user", "stop", SERVICE], env=systemd_env(), timeout=60)


def service_status() -> dict[str, str]:
    raw = run(
        [
            "systemctl",
            "--user",
            "show",
            SERVICE,
            "--property=ActiveState,SubState,MainPID,ExecMainStatus,NRestarts",
        ],
        env=systemd_env(),
        timeout=30,
    )
    fields = dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)
    return {
        "active": fields.get("ActiveState", "unknown"),
        "substate": fields.get("SubState", "unknown"),
        "main_pid": fields.get("MainPID", "0"),
        "exec_status": fields.get("ExecMainStatus", "unknown"),
        "restarts": fields.get("NRestarts", "unknown"),
    }


def health(timeout: float = 3.0) -> dict[str, Any]:
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/healthz",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError, ValueError) as exc:
        raise DeployError(f"health request failed: {type(exc).__name__}") from None


def wait_healthy(sha: str, seconds: float = 30.0) -> dict[str, Any]:
    deadline = time.monotonic() + seconds
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = health()
            required = (
                result.get("ok") is True
                and result.get("static_ready") is True
                and result.get("live_interaction_available") is True
                and result.get("resource_control_available") is True
                and result.get("release_sha") == sha
                and result.get("auth_mode") == "first_party_invite+loopback_dev"
            )
            if required:
                return result
            last = DeployError("health response is incomplete")
        except DeployError as exc:
            last = exc
        time.sleep(0.5)
    raise DeployError(f"Projects Hub did not become healthy: {last}")


def _deploy_serialized(sha: str) -> dict[str, Any]:
    sha = resolve_sha(sha)
    release = _build_release(sha)
    old_service_env = SERVICE_ENV.read_bytes() if SERVICE_ENV.is_file() else None
    previous = _replace_current(release)
    try:
        write_runtime_environment(sha)
        install_unit()
        restart_service()
        live_health = wait_healthy(sha)
    except Exception:
        _restore_current(previous)
        restore_service_environment(old_service_env)
        if previous is None:
            stop_service()
        else:
            with contextlib.suppress(Exception):
                restart_service()
        raise
    status = service_status()
    if status["active"] != "active" or status["substate"] != "running":
        raise DeployError(f"service state is {status['active']}/{status['substate']}")
    try:
        release_retention = prune_old_releases(sha, previous)
    except Exception as exc:
        release_retention = {
            "removed": [],
            "skipped": [],
            "preserved": [sha],
            "warning": [type(exc).__name__],
        }
    return {
        "repository": REPOSITORY,
        "release_sha": sha,
        "release": str(release),
        "current": str(CURRENT_LINK),
        "service": status,
        "health": live_health,
        "log_file": str(BACKEND_LOG),
        "public_origin": PUBLIC_ORIGIN,
        "public_auth": "first_party_invite",
        "release_retention": release_retention,
        "provider_environment_keys": sorted(select_provider_environment(_parse_env(HOST_ENV))),
        "provider_environment_values_exposed": False,
    }


def deploy(sha: str) -> dict[str, Any]:
    with deployment_lock():
        return _deploy_serialized(sha)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sha", required=True)
    args = parser.parse_args()
    try:
        result = deploy(args.sha)
    except DeployError as exc:
        print(json.dumps({"ok": False, "error": _safe_output(str(exc))}, separators=(",", ":")))
        return 1
    print(json.dumps({"ok": True, **result}, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
