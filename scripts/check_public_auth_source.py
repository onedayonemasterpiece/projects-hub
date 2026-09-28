#!/usr/bin/env python3
"""Check whether DevCoveer can supply Projects Hub browser-safe public auth config.

Prints names and booleans only. The Supabase URL and provider identifier are pinned
in Projects Hub source; the host environment needs only a publishable key.
"""
from pathlib import Path
import re
import stat

HOST_ENV = Path("/home/dev/.env")
KEYS = (
    "PUBLIC_PERSONALIZATION_SUPABASE_PUBLISHABLE_KEY",
    "STATIC_SITE_PUBLIC_PERSONALIZATION_SUPABASE_PUBLISHABLE_KEY",
    "PERSONALIZATION_SUPABASE_PUBLISHABLE_KEY",
)
KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


def main() -> int:
    info = HOST_ENV.lstat()
    if HOST_ENV.is_symlink() or stat.S_IMODE(info.st_mode) & 0o077:
        raise SystemExit("host env permissions are unsafe")
    values = {key: False for key in KEYS}
    for raw in HOST_ENV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if key in values and KEY_RE.fullmatch(key):
            values[key] = bool(value.strip().strip("'\""))
    for key in KEYS:
        print(f"{key}={'present' if values[key] else 'missing'}")
    ready = any(values.values())
    print("PROJECTS_HUB_AUTH_SUPABASE_URL=pinned")
    print("PROJECTS_HUB_AUTH_PROVIDER=custom:yandex")
    print(f"PROJECTS_HUB_PUBLIC_AUTH_SOURCE={'ready' if ready else 'missing'}")
    return 0 if ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
