#!/usr/bin/env python3
"""Fail-closed readiness probe for Projects Hub public Yandex authentication."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://epyznmylqmchteykjsqj.supabase.co"
PROVIDER = "custom:yandex"
REDIRECT_URL = "https://projects-hub.kenigevents.ru/"
HOST_ENV = Path("/home/dev/.env")
KEY_ALIASES = (
    "PERSONALIZATION_SUPABASE_PUBLISHABLE_KEY",
    "PUBLIC_PERSONALIZATION_SUPABASE_PUBLISHABLE_KEY",
    "STATIC_SITE_PUBLIC_PERSONALIZATION_SUPABASE_PUBLISHABLE_KEY",
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def parse_host_env(path: Path) -> dict[str, str]:
    info = path.lstat()
    if path.is_symlink() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("host environment is not a private regular file")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def publishable_key(values: dict[str, str]) -> tuple[str, list[str]]:
    present = [
        (name, values[name].strip())
        for name in KEY_ALIASES
        if values.get(name, "").strip()
    ]
    if not present:
        raise RuntimeError("public auth publishable key is unavailable")
    distinct = {value for _name, value in present}
    if len(distinct) != 1:
        raise RuntimeError("public auth publishable aliases disagree")
    value = present[0][1]
    if len(value) < 20 or len(value) > 4096 or any(ch.isspace() for ch in value):
        raise RuntimeError("public auth publishable key is invalid")
    return value, [name for name, _value in present]


def authorize_probe(key: str) -> dict[str, object]:
    query = urllib.parse.urlencode(
        {
            "provider": PROVIDER,
            "redirect_to": REDIRECT_URL,
        }
    )
    request = urllib.request.Request(
        AUTH_URL + "/auth/v1/authorize?" + query,
        headers={
            "apikey": key,
            "User-Agent": "projects-hub-public-auth-readiness",
        },
    )
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(request, timeout=10)
        status = 200
        location = ""
    except urllib.error.HTTPError as exc:
        status = exc.code
        location = exc.headers.get("Location") or ""

    parsed = urllib.parse.urlparse(location)
    yandex = (
        status in {302, 303, 307, 308}
        and parsed.scheme == "https"
        and parsed.hostname in {"oauth.yandex.ru", "oauth.yandex.com"}
        and "localhost" not in location
    )
    return {
        "ok": yandex,
        "http_status": status,
        "provider_location_host": parsed.hostname or "",
        "localhost_fallback": "localhost" in location,
    }


def main() -> int:
    try:
        key, aliases = publishable_key(parse_host_env(HOST_ENV))
        provider = authorize_probe(key)
        result = {
            "ok": bool(provider["ok"]),
            "auth_url": AUTH_URL,
            "provider": PROVIDER,
            "redirect_origin": "projects-hub.kenigevents.ru",
            "publishable_aliases_present": aliases,
            "provider_authorize": provider,
            "credential_values_exposed": False,
        }
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "credential_values_exposed": False,
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
