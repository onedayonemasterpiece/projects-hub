#!/usr/bin/env python3
"""Probe Projects Hub custom:yandex authorize flow without exposing credentials."""
from __future__ import annotations

import json
from pathlib import Path
import stat
import urllib.error
import urllib.parse
import urllib.request

HOST_ENV = Path("/home/dev/.env")
AUTH_URL = "https://epyznmylqmchteykjsqj.supabase.co"
PROVIDER = "custom:yandex"
REDIRECT = "https://projects-hub.kenigevents.ru/"
ALIASES = (
    "PERSONALIZATION_SUPABASE_PUBLISHABLE_KEY",
    "PUBLIC_PERSONALIZATION_SUPABASE_PUBLISHABLE_KEY",
    "STATIC_SITE_PUBLIC_PERSONALIZATION_SUPABASE_PUBLISHABLE_KEY",
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def key() -> str:
    info = HOST_ENV.lstat()
    if HOST_ENV.is_symlink() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("unsafe host env")
    values = {}
    for raw in HOST_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip("'\"")
    for name in ALIASES:
        if values.get(name):
            return values[name]
    raise RuntimeError("publishable key unavailable")


def main() -> int:
    params = urllib.parse.urlencode({"provider": PROVIDER, "redirect_to": REDIRECT})
    request = urllib.request.Request(
        AUTH_URL + "/auth/v1/authorize?" + params,
        headers={"apikey": key(), "User-Agent": "projects-hub-auth-readiness"},
    )
    opener = urllib.request.build_opener(NoRedirect)
    try:
        opener.open(request, timeout=10)
        status = 200
        location = ""
        body = ""
    except urllib.error.HTTPError as exc:
        status = exc.code
        location = exc.headers.get("Location") or ""
        body = exc.read(300).decode("utf-8", "replace")
    host = urllib.parse.urlparse(location).netloc
    ok = status in {302, 303, 307, 308} and "oauth.yandex" in host and "localhost" not in location
    print(json.dumps({
        "ok": ok,
        "status": status,
        "provider": PROVIDER,
        "redirect_origin": "projects-hub.kenigevents.ru",
        "provider_location_host": host,
        "localhost_fallback": "localhost" in location,
        "error_body_present": bool(body.strip()),
        "credential_values_exposed": False,
    }, sort_keys=True))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
