from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import stat
from urllib.parse import urlsplit

EXPECTED_PUBLIC_ORIGIN = "https://projects-hub.kenigevents.ru"
EXPECTED_AUTH_SUPABASE_URL = "https://epyznmylqmchteykjsqj.supabase.co"
EXPECTED_AUTH_PROVIDER = "custom:yandex"


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_private_secret(path: Path) -> str:
    info = path.lstat()
    if path.is_symlink() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError(
            "PROJECTS_HUB_SESSION_SECRET_FILE must be a private regular file"
        )
    if not path.is_file():
        raise RuntimeError(
            "PROJECTS_HUB_SESSION_SECRET_FILE must be a regular file"
        )
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
    public_origin: str = ""
    auth_supabase_url: str = ""
    auth_supabase_publishable_key: str = ""
    auth_provider: str = EXPECTED_AUTH_PROVIDER

    @property
    def public_auth_enabled(self) -> bool:
        return bool(
            self.public_origin
            and self.auth_supabase_url
            and self.auth_supabase_publishable_key
        )

    @property
    def auth_mode(self) -> str:
        if self.public_auth_enabled and self.dev_auth:
            return "public_yandex+loopback_dev"
        if self.public_auth_enabled:
            return "public_yandex"
        if self.dev_auth:
            return "loopback_dev"
        return "session"

    @classmethod
    def from_env(
        cls,
        environment: dict[str, str] | None = None,
    ) -> "Settings":
        env = os.environ if environment is None else environment
        dev_auth = _flag(env.get("PROJECTS_HUB_DEV_AUTH"))
        root = Path(
            env.get("PROJECTS_HUB_DATA_DIR") or ".projects-hub"
        ).expanduser()

        direct_secret = str(
            env.get("PROJECTS_HUB_SESSION_SECRET") or ""
        ).strip()
        secret_file = str(
            env.get("PROJECTS_HUB_SESSION_SECRET_FILE") or ""
        ).strip()
        if direct_secret and secret_file:
            raise RuntimeError(
                "Configure only one Projects Hub session secret source"
            )
        if direct_secret:
            if len(direct_secret) < 32:
                raise RuntimeError(
                    "PROJECTS_HUB_SESSION_SECRET is too short"
                )
            secret = direct_secret
        elif secret_file:
            secret = _read_private_secret(
                Path(secret_file).expanduser()
            )
        elif dev_auth:
            secret = _dev_session_secret(root)
        else:
            raise RuntimeError("Projects Hub session secret is required")

        static = Path(
            env.get("PROJECTS_HUB_STATIC_DIR") or "web/dist"
        ).expanduser()
        public_origin = str(
            env.get("PROJECTS_HUB_PUBLIC_ORIGIN") or ""
        ).strip().rstrip("/")
        auth_url = str(
            env.get("PROJECTS_HUB_AUTH_SUPABASE_URL") or ""
        ).strip().rstrip("/")
        auth_key = str(
            env.get(
                "PROJECTS_HUB_AUTH_SUPABASE_PUBLISHABLE_KEY"
            )
            or ""
        ).strip()
        auth_provider = str(
            env.get("PROJECTS_HUB_AUTH_PROVIDER")
            or EXPECTED_AUTH_PROVIDER
        ).strip()

        configured = (
            bool(public_origin),
            bool(auth_url),
            bool(auth_key),
        )
        if any(configured) and not all(configured):
            raise RuntimeError(
                "Projects Hub public auth configuration is incomplete"
            )
        if all(configured):
            parsed_origin = urlsplit(public_origin)
            if (
                public_origin != EXPECTED_PUBLIC_ORIGIN
                or parsed_origin.scheme != "https"
                or parsed_origin.hostname
                != "projects-hub.kenigevents.ru"
                or parsed_origin.username
                or parsed_origin.password
                or parsed_origin.path not in ("", "/")
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise RuntimeError(
                    "PROJECTS_HUB_PUBLIC_ORIGIN is unexpected"
                )
            if auth_url != EXPECTED_AUTH_SUPABASE_URL:
                raise RuntimeError(
                    "Projects Hub public auth Supabase project is unexpected"
                )
            if auth_provider != EXPECTED_AUTH_PROVIDER:
                raise RuntimeError(
                    "Projects Hub public auth provider is unexpected"
                )
            if (
                len(auth_key) < 20
                or len(auth_key) > 4096
                or any(ch.isspace() for ch in auth_key)
            ):
                raise RuntimeError(
                    "Projects Hub public auth publishable key is invalid"
                )

        return cls(
            data_dir=root,
            static_dir=static,
            session_secret=secret,
            dev_auth=dev_auth,
            cookie_secure=_flag(
                env.get("PROJECTS_HUB_COOKIE_SECURE"),
                default=bool(public_origin) or not dev_auth,
            ),
            model=str(
                env.get("PROJECTS_HUB_LIVE_MODEL")
                or "gemini-3.8-live"
            ),
            release_sha=str(
                env.get("PROJECTS_HUB_DEPLOY_SHA")
                or "development"
            ),
            public_origin=public_origin,
            auth_supabase_url=auth_url,
            auth_supabase_publishable_key=auth_key,
            auth_provider=auth_provider,
        )
