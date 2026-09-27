from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import stat


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_private_secret(path: Path) -> str:
    info = path.lstat()
    if path.is_symlink() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("PROJECTS_HUB_SESSION_SECRET_FILE must be a private regular file")
    if not path.is_file():
        raise RuntimeError("PROJECTS_HUB_SESSION_SECRET_FILE must be a regular file")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise RuntimeError("PROJECTS_HUB_SESSION_SECRET_FILE is too short")
    return value


def _dev_session_secret(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    path = root / ".session-secret"
    if path.exists():
        return _read_private_secret(path)
    value = secrets.token_urlsafe(48)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    static_dir: Path
    session_secret: str
    dev_auth: bool = False
    cookie_secure: bool = True
    model: str = "gemini-3.8-live"
    release_sha: str = "development"

    @classmethod
    def from_env(cls, environment: dict[str, str] | None = None) -> "Settings":
        env = os.environ if environment is None else environment
        dev_auth = _flag(env.get("PROJECTS_HUB_DEV_AUTH"))
        root = Path(env.get("PROJECTS_HUB_DATA_DIR") or ".projects-hub").expanduser()

        direct_secret = str(env.get("PROJECTS_HUB_SESSION_SECRET") or "").strip()
        secret_file = str(env.get("PROJECTS_HUB_SESSION_SECRET_FILE") or "").strip()
        if direct_secret and secret_file:
            raise RuntimeError("Configure only one Projects Hub session secret source")
        if direct_secret:
            if len(direct_secret) < 32:
                raise RuntimeError("PROJECTS_HUB_SESSION_SECRET is too short")
            secret = direct_secret
        elif secret_file:
            secret = _read_private_secret(Path(secret_file).expanduser())
        elif dev_auth:
            secret = _dev_session_secret(root)
        else:
            raise RuntimeError("Projects Hub session secret is required")

        static = Path(env.get("PROJECTS_HUB_STATIC_DIR") or "web/dist").expanduser()
        return cls(
            data_dir=root,
            static_dir=static,
            session_secret=secret,
            dev_auth=dev_auth,
            cookie_secure=_flag(env.get("PROJECTS_HUB_COOKIE_SECURE"), default=not dev_auth),
            model=str(env.get("PROJECTS_HUB_LIVE_MODEL") or "gemini-3.8-live"),
            release_sha=str(env.get("PROJECTS_HUB_DEPLOY_SHA") or "development"),
        )
