from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import stat
from urllib.parse import urlsplit

EXPECTED_AUTH_SUPABASE_URL = "https://epyznmylqmchteykjsqj.supabase.co"
EXPECTED_AUTH_PROVIDER = "custom:yandex"


def _flag(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _read_private_text(path: Path, label: str, *, min_length: int = 32) -> str:
    info = path.lstat()
    if path.is_symlink() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError(f"{label} must be a private regular file")
    if not path.is_file():
        raise RuntimeError(f"{label} must be a regular file")
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < min_length:
        raise RuntimeError(f"{label} is too short")
    return value


def _read_private_secret(path: Path) -> str:
    return _read_private_text(
        path,
        "PROJECTS_HUB_SESSION_SECRET_FILE",
        min_length=32,
    )


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
    github_app_id: int | None = None
    github_app_slug: str = ""
    github_app_private_key: str = ""
    github_app_webhook_secret: str = ""

    @property
    def public_auth_enabled(self) -> bool:
        return bool(
            self.public_origin
            and self.auth_supabase_url
            and self.auth_supabase_publishable_key
        )

    @property
    def github_app_enabled(self) -> bool:
        return bool(
            self.github_app_id
            and self.github_app_slug
            and self.github_app_private_key
            and self.github_app_webhook_secret
        )

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
        public_origin = str(env.get("PROJECTS_HUB_PUBLIC_ORIGIN") or "").strip().rstrip("/")
        auth_url = str(env.get("PROJECTS_HUB_AUTH_SUPABASE_URL") or "").strip().rstrip("/")
        auth_key = str(env.get("PROJECTS_HUB_AUTH_SUPABASE_PUBLISHABLE_KEY") or "").strip()
        auth_provider = str(env.get("PROJECTS_HUB_AUTH_PROVIDER") or EXPECTED_AUTH_PROVIDER).strip()

        configured = (bool(public_origin), bool(auth_url), bool(auth_key))
        if any(configured) and not all(configured):
            raise RuntimeError("Projects Hub public auth configuration is incomplete")
        if all(configured):
            parsed_origin = urlsplit(public_origin)
            if (
                parsed_origin.scheme != "https"
                or not parsed_origin.hostname
                or parsed_origin.username
                or parsed_origin.password
                or parsed_origin.path not in ("", "/")
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise RuntimeError("PROJECTS_HUB_PUBLIC_ORIGIN must be an HTTPS origin")
            if auth_url != EXPECTED_AUTH_SUPABASE_URL:
                raise RuntimeError("Projects Hub public auth Supabase project is unexpected")
            if auth_provider != EXPECTED_AUTH_PROVIDER:
                raise RuntimeError("Projects Hub public auth provider is unexpected")
            if len(auth_key) < 20 or len(auth_key) > 4096:
                raise RuntimeError("Projects Hub public auth publishable key is invalid")

        github_app_id_raw = str(env.get("PROJECTS_HUB_GITHUB_APP_ID") or "").strip()
        github_slug = str(env.get("PROJECTS_HUB_GITHUB_APP_SLUG") or "").strip()
        github_key_file = str(env.get("PROJECTS_HUB_GITHUB_APP_PRIVATE_KEY_FILE") or "").strip()
        github_webhook_file = str(env.get("PROJECTS_HUB_GITHUB_APP_WEBHOOK_SECRET_FILE") or "").strip()
        github_parts = (
            bool(github_app_id_raw),
            bool(github_slug),
            bool(github_key_file),
            bool(github_webhook_file),
        )
        if any(github_parts) and not all(github_parts):
            raise RuntimeError("Projects Hub GitHub App configuration is incomplete")

        github_app_id: int | None = None
        github_private_key = ""
        github_webhook_secret = ""
        if all(github_parts):
            try:
                github_app_id = int(github_app_id_raw)
            except ValueError as exc:
                raise RuntimeError("PROJECTS_HUB_GITHUB_APP_ID must be numeric") from exc
            if github_app_id <= 0:
                raise RuntimeError("PROJECTS_HUB_GITHUB_APP_ID must be positive")
            if (
                len(github_slug) > 100
                or github_slug.lower() != github_slug
                or not all(ch.isalnum() or ch == "-" for ch in github_slug)
                or github_slug.startswith("-")
                or github_slug.endswith("-")
            ):
                raise RuntimeError("PROJECTS_HUB_GITHUB_APP_SLUG is invalid")
            github_private_key = _read_private_text(
                Path(github_key_file).expanduser(),
                "PROJECTS_HUB_GITHUB_APP_PRIVATE_KEY_FILE",
                min_length=100,
            )
            if "BEGIN" not in github_private_key or "PRIVATE KEY" not in github_private_key:
                raise RuntimeError("Projects Hub GitHub App private key format is invalid")
            github_webhook_secret = _read_private_text(
                Path(github_webhook_file).expanduser(),
                "PROJECTS_HUB_GITHUB_APP_WEBHOOK_SECRET_FILE",
                min_length=20,
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
            model=str(env.get("PROJECTS_HUB_LIVE_MODEL") or "gemini-3.8-live"),
            release_sha=str(env.get("PROJECTS_HUB_DEPLOY_SHA") or "development"),
            public_origin=public_origin,
            auth_supabase_url=auth_url,
            auth_supabase_publishable_key=auth_key,
            auth_provider=auth_provider,
            github_app_id=github_app_id,
            github_app_slug=github_slug,
            github_app_private_key=github_private_key,
            github_app_webhook_secret=github_webhook_secret,
        )
